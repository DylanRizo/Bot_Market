"""Publish the local Marketplace monitor snapshot to SGI (ADR-024)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

import marketplace_leads as leads
import marketplace_listings as listings
from marketplace_safety import health_snapshot, load_safety
from marketplace_storage import MarketplaceStore
from marketplace_platform import load_secret, save_secret
from sgi_client import (
    KEY_PATTERN,
    SCRATCH_DIR,
    SgiError,
    load_settings,
    validate_base_url,
)

MONITOR_SECRET_PATH = SCRATCH_DIR / ".sgi_monitor_key.bin"
MONITOR_KEY_ENV = "SGI_MONITOR_KEY"
INSTANCE_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")
SYNC_PATH = "/api/v1/integrations/marketplace-bot/sync"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def build_snapshot(store: MarketplaceStore, config: dict[str, Any]) -> dict[str, Any]:
    today = date.today()
    report = leads.report(store, today=today)
    since = (today - timedelta(days=14)).isoformat()
    with store.connect() as connection:
        awaiting_reply = connection.execute(
            "SELECT COUNT(*) FROM leads WHERE awaiting_reply = 1 AND last_activity >= ?",
            (since,),
        ).fetchone()[0]
    alert_since = (today - timedelta(days=leads.ALERT_DAYS)).isoformat()
    items = listings.list_listings(store)
    twin_groups = listings.twin_index(items)
    active = [row for row in items if row["status"] == "in_stock"]
    active_by_family = Counter(row["family_key"] for row in active)
    labels = {row["family_key"]: row["family_key"].replace("_", " ") for row in report["products"]}
    pending = {row["key"]: row["action"] for row in listings.pending_actions(store)}
    scanned_accounts, lead_counts = leads.listing_leads(store, today=today)
    scheduler = store.worker_state()

    accounts = []
    for key, health in health_snapshot(store, config["accounts"]).items():
        accounts.append({
            "key": key,
            "displayName": key,
            "status": health["status"],
            "publishedToday": health["today"],
            "dailyLimit": health["daily_limit"],
            "publishedWeek": health["week"],
            "weeklyLimit": health["weekly_limit"],
            "pausedUntil": (health["pause"] or {}).get("until"),
        })

    running = store.list_queue(statuses=["running"], limit=1)
    return {
        "worker": {
            "processRunning": True,
            "status": "running" if running else scheduler["status"],
            "currentItem": running[0]["name"] if running else None,
            "enabled": config["enabled"],
            "mode": config["mode"],
            "safetyLevel": load_safety(store)["level"],
        },
        "schedule": {
            "activeDays": list(config.get("active_days") or []),
            "startTime": config.get("start_time", "09:00"),
            "endTime": config.get("end_time", "18:00"),
            "maxPerDay": config.get("max_per_day", 3),
            "intervalMinutes": config.get("interval_minutes", 180),
            "botTime": datetime.now().isoformat(timespec="minutes"),
        },
        "accounts": accounts[:20],
        "leads": {
            "total14d": sum(row["last_14"] for row in report["accounts"]),
            "awaitingReply": awaiting_reply,
            "unansweredRecent": sum(row["last_activity"] >= alert_since for row in report["unanswered"]),
            "unanswered": [
                {
                    "account": row["account"],
                    "title": labels.get(row["family_key"]) or "Producto sin clasificar",
                    "label": labels.get(row["family_key"]) or "Otros",
                    "lastActivity": row["last_activity"],
                    "startedOn": row.get("started_on", ""),
                    "neverAnswered": True,
                }
                for row in report["unanswered"][:30]
            ],
            "products": [
                {
                    "familyKey": row["family_key"],
                    "label": labels[row["family_key"]] or "Otros",
                    "activeListings": active_by_family[row["family_key"]],
                    "clicks14d": row["clicks"],
                    "leads14d": row["leads"],
                    "leadsPer100Clicks": row["rate"],
                    "focus": row["focus"],
                }
                for row in report["products"][:40]
            ],
        },
        "listings": {
            "totalActive": len(active),
            "actionable": sum(row["recommendation"] in listings.EXECUTABLE_ACTIONS for row in items),
            "items": [
                {
                    "key": row["key"],
                    "account": row["account"],
                    "title": row["title"],
                    "status": row["status"],
                    "isExactTwin": len(twin_groups.get(listings.twin_id(row), ())) > 1,
                    "listedOn": row["listed_on"],
                    "ageDays": listings.age_days(row["listed_on"], today),
                    "clicks14d": row["clicks_14d"],
                    "leads14d": (
                        lead_counts.get((row["account"], listings.plain(row["title"])), 0)
                        if row["account"] in scanned_accounts else None
                    ),
                    "recommendation": row["recommendation"],
                    "recommendationLabel": listings.ACTION_LABELS.get(row["recommendation"], ""),
                    "executable": row["recommendation"] in listings.EXECUTABLE_ACTIONS,
                    "pendingAction": pending.get(row["key"]),
                }
                for row in items[:50]
            ],
        },
        "queue": {
            "summary": store.summary()["counts"],
            "upcoming": [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "account": row["account"],
                    "scheduledAt": row["scheduled_at"],
                    "status": row["status"],
                    "approved": bool(row["approved"]),
                    "detail": str(row.get("detail") or "")[:280],
                }
                for row in store.list_queue(statuses=["planned", "queued", "retry", "running"], limit=35)
            ],
        },
        "alerts": [
            {
                "id": row["id"],
                "severity": row["severity"],
                "code": row["code"],
                "message": row["code"],
                "account": row["account"],
                "createdAt": row["created_at"],
            }
            for row in store.list_alerts(limit=25)
        ],
    }


def send_snapshot(
    base_url: str,
    key: str,
    instance_key: str,
    instance_name: str,
    snapshot: dict[str, Any],
    *,
    open_request: Callable[..., Any] | None = None,
    control_enabled: bool = False,
    accept_commands: bool = True,
    command_results: list[dict[str, str]] | None = None,
    timeout: float = 5,
) -> list[dict[str, Any]] | None:
    if not KEY_PATTERN.fullmatch(key) or not INSTANCE_PATTERN.fullmatch(instance_key):
        raise SgiError("NOT_CONFIGURED", "Configura la llave y el identificador del monitor.")
    if not instance_name or len(instance_name) > 80:
        raise SgiError("NOT_CONFIGURED", "El nombre del monitor debe tener entre 1 y 80 caracteres.")
    body = {
        "instanceKey": instance_key,
        "instanceName": instance_name,
        "botVersion": "monitor-control-2" if control_enabled else "monitor-2",
        "snapshot": snapshot,
    }
    if control_enabled:
        # Ocupado (publicando), el bot solo informa: no pide ordenes nuevas,
        # pero entrega los resultados que tenga guardados.
        if accept_commands:
            body["commandProtocolVersion"] = 1
        body["commandResults"] = command_results or []
    request = urllib.request.Request(
        validate_base_url(base_url) + SYNC_PATH,
        data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = open_request or urllib.request.build_opener(NoRedirect()).open
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read(65536))
    except urllib.error.HTTPError as exc:
        raise SgiError(f"MONITOR_HTTP_{exc.code}", "El SGI rechazó el pulso.") from None
    except (OSError, ValueError):
        raise SgiError("MONITOR_UNAVAILABLE", "El SGI no confirmó el pulso del monitor.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise SgiError("MONITOR_RESPONSE", "El SGI devolvió una respuesta inesperada.")
    commands = payload["data"].get("pendingCommands")
    if not isinstance(commands, list) or len(commands) > 25 or any(not isinstance(item, dict) for item in commands):
        raise SgiError("MONITOR_RESPONSE", "El SGI devolvió una lista de órdenes inesperada.")
    if not (control_enabled and accept_commands) and commands:
        raise SgiError("MONITOR_RESPONSE", "El monitor esperaba una respuesta sin órdenes.")
    return commands if control_enabled and accept_commands else None


def save_monitor_key(key: str) -> None:
    value = str(key or "").strip()
    if not KEY_PATTERN.fullmatch(value):
        raise ValueError("La llave del monitor debe tener 43 caracteres válidos.")
    save_secret("sgi_monitor_key", MONITOR_SECRET_PATH, value, "Marketplace bot SGI monitor key")


def pending_result_count(store: MarketplaceStore) -> int:
    """Resultados de ordenes que el SGI todavia no confirmo."""
    monitor = (load_settings().get("sgi") or {}).get("monitor") or {}
    instance_key = str(monitor.get("instance_key") or "").strip() if isinstance(monitor, dict) else ""
    if not isinstance(monitor, dict) or monitor.get("control_enabled") is not True or not INSTANCE_PATTERN.fullmatch(instance_key):
        return 0
    from sgi_command_journal import CommandJournal

    return len(CommandJournal(store, instance_key).pending_results())


def send_configured_snapshot(
    store: MarketplaceStore,
    config: dict[str, Any],
    *,
    accept_commands: bool = True,
    timeout: float = 5,
) -> bool:
    settings = load_settings().get("sgi") or {}
    monitor = settings.get("monitor") or {}
    if not isinstance(monitor, dict) or monitor.get("enabled") is not True:
        return False
    key = os.environ.get(MONITOR_KEY_ENV, "").strip() or load_secret("sgi_monitor_key", MONITOR_SECRET_PATH)
    instance_key = str(monitor.get("instance_key") or "").strip()
    control_enabled = monitor.get("control_enabled") is True
    results = []
    journal = None
    if control_enabled:
        from sgi_command_journal import CommandJournal

        if not INSTANCE_PATTERN.fullmatch(instance_key):
            raise SgiError("NOT_CONFIGURED", "Configura el identificador de la instalación.")
        journal = CommandJournal(store, instance_key)
        journal.recover_interrupted()
        results = journal.pending_results()
    commands = send_snapshot(
        str(settings.get("base_url") or ""),
        key,
        instance_key,
        str(monitor.get("instance_name") or instance_key).strip(),
        build_snapshot(store, config),
        control_enabled=control_enabled,
        accept_commands=accept_commands,
        command_results=results,
        timeout=timeout,
    )
    if journal is not None:
        from sgi_commands import execute_command

        journal.acknowledge([result["id"] for result in results])
        for command in commands or []:
            # Una orden que no se puede empezar no debe frenar a las siguientes:
            # se cierra con FAILED y el SGI deja de reenviarla.
            try:
                execute_command(store, journal, command)
            except Exception:  # noqa: BLE001 - el detalle puede traer datos privados.
                journal.reject(command.get("id"), "El bot rechazó la orden: contenido no válido o repetido con otro contenido.")
    return True

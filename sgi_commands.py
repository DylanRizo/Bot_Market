"""Execute the allowlisted ADR-024 commands through the bot's existing services."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import marketplace_listings as listings
import marketplace_safety as safety
import marketplace_scheduler as scheduler
from marketplace_listing_worker import request_scan
from marketplace_storage import MarketplaceStore, now_iso
from sgi_command_journal import CommandJournal


SAFE_CODE = re.compile(r"[A-Za-z0-9._:-]{1,80}")
# Los nombres de cuenta son los configurados en este equipo ("Dylan Steven"):
# admiten letras con tilde y espacios internos. Igual se exige que existan.
ACCOUNT_CODE = re.compile(r"(?=.{1,64}\Z)[\w.:-](?:[\w .:-]*[\w.:-])?")


def code(value: Any) -> str:
    if not isinstance(value, str) or not SAFE_CODE.fullmatch(value):
        raise ValueError("Identificador de orden no válido.")
    return value


def boolean(value: Any) -> bool:
    if type(value) is not bool:
        raise ValueError("La opción debe ser verdadera o falsa.")
    return value


def account(value: Any) -> str:
    if not isinstance(value, str) or not ACCOUNT_CODE.fullmatch(value.strip()):
        raise ValueError("Nombre de cuenta no válido.")
    name = value.strip()
    configured = json.loads(scheduler.ACCOUNTS_PATH.read_text(encoding="utf-8")).get("accounts", {})
    if name not in configured:
        raise ValueError("La cuenta solicitada no existe en este equipo.")
    return name


def set_bot_enabled(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    config = scheduler.load_config(store)
    config["enabled"] = boolean(payload.get("enabled"))
    scheduler.save_config(store, config)
    if config["enabled"] and config["mode"] == "autonomous":
        scheduler.approve_items(store, include_reuse=False)
    return "Estado de publicaciones actualizado."


def generate_week(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    if boolean(payload.get("replace")):
        scheduler.cancel_future(store)
    scheduler.generate_week(store, scheduler.load_config(store))
    return "Calendario semanal generado."


def approve_queue(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    ids = payload.get("ids")
    if not isinstance(ids, list) or len(ids) > 100:
        raise ValueError("Lista de elementos no válida.")
    requested = [code(value) for value in ids]
    if requested:
        scheduler.approve_items(store, requested)
    return "Aprobación de los elementos solicitados procesada."


def queue_action(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    queue_id = code(payload.get("id"))
    action = payload.get("action")
    item = store.get_queue_item(queue_id)
    if not item or action not in {"approve", "cancel", "retry"}:
        raise ValueError("Elemento o acción de calendario no válido.")
    if item["status"] == "running":
        raise ValueError("El elemento ya está en ejecución.")
    if action == "approve":
        store.update_queue_item(queue_id, status="queued", approved=True)
    else:
        from marketplace_scheduler_worker import clear_intent

        clear_intent(queue_id)
        if action == "cancel":
            store.update_queue_item(queue_id, status="cancelled", detail="Cancelado desde el calendario.")
        else:
            store.update_queue_item(queue_id, status="queued", next_attempt_at=None,
                                    finished_at=None, detail="Reintento manual.")
    return "Elemento del calendario actualizado."


def listing_action(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    key = code(payload.get("key"))
    action = payload.get("action")
    if action not in listings.EXECUTABLE_ACTIONS:
        raise ValueError("Acción de anuncio no válida.")
    existing = next((row for row in listings.pending_actions(store) if row["key"] == key), None)
    if existing and existing["action"] != action:
        raise ValueError("El anuncio ya tiene otra acción pendiente.")
    listings.queue_action(store, key, action)
    return "Acción añadida a la cola local de anuncios."


def cancel_listing_action(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    action_id = payload.get("id")
    if type(action_id) is not int or action_id < 1:
        raise ValueError("Identificador de acción no válido.")
    listings.ensure_schema(store)
    with store.connect() as connection:
        row = connection.execute("SELECT status FROM listing_actions WHERE id=?", (action_id,)).fetchone()
        if not row or row["status"] != "pending":
            raise ValueError("La acción ya no está pendiente.")
        connection.execute("UPDATE listing_actions SET status='cancelled', finished_at=? WHERE id=? AND status='pending'",
                           (now_iso(), action_id))
    return "Acción pendiente cancelada."


def scan(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    selected = payload.get("account")
    request_scan(store, "" if selected is None or selected == "" else account(selected))
    return "Revisión solicitada para el próximo ciclo activo del trabajador."


def sync_catalog(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    from marketplace_scheduler_worker import refresh_from_sgi

    refresh_from_sgi(store)
    return "Catálogo del SGI sincronizado."


def pause_account(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    selected = account(payload.get("account"))
    hours = payload.get("hours")
    if type(hours) is not int or not 1 <= hours <= 720:
        raise ValueError("Duración de pausa no válida.")
    safety.pause_account(store, selected, hours, "Pausa manual desde el SGI.", "MANUAL_PAUSE")
    return "Cuenta pausada."


def resume_account(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    safety.resume_account(store, account(payload.get("account")))
    return "Pausa de la cuenta retirada."


def set_safety_level(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    level = payload.get("level")
    if level not in safety.LEVELS:
        raise ValueError("Nivel de seguridad no válido.")
    safety.save_safety(store, {**safety.load_safety(store), **safety.LEVELS[level], "level": level})
    return "Nivel de seguridad actualizado."


def resolve_alert(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    store.resolve_alert(code(payload.get("id")))
    return "Alerta resuelta."


def resolve_all_alerts(store: MarketplaceStore, payload: dict[str, Any]) -> str:
    for alert in store.list_alerts(limit=500):
        store.resolve_alert(alert["id"])
    return "Alertas abiertas resueltas."


HANDLERS = {
    "SET_BOT_ENABLED": set_bot_enabled,
    "GENERATE_WEEK": generate_week,
    "APPROVE_QUEUE_ITEMS": approve_queue,
    "QUEUE_ITEM_ACTION": queue_action,
    "LISTING_ACTION": listing_action,
    "CANCEL_LISTING_ACTION": cancel_listing_action,
    "REQUEST_SCAN": scan,
    "SYNC_SGI_CATALOG": sync_catalog,
    "PAUSE_ACCOUNT": pause_account,
    "RESUME_ACCOUNT": resume_account,
    "SET_SAFETY_LEVEL": set_safety_level,
    "RESOLVE_ALERT": resolve_alert,
    "RESOLVE_ALL_ALERTS": resolve_all_alerts,
}


def execute_command(store: MarketplaceStore, journal: CommandJournal, command: dict[str, Any]) -> None:
    command_id = command["id"]
    if str(UUID(command_id)) != command_id:
        raise ValueError("Identificador de orden no válido.")
    payload = command.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Contenido de orden no válido.")
    if not journal.begin(command_id, payload):
        return
    try:
        expires = datetime.fromisoformat(str(command.get("expiresAt", "")).replace("Z", "+00:00"))
        if expires.tzinfo is None or expires <= datetime.now(timezone.utc):
            journal.finish(command_id, "FAILED", "La orden venció antes de ejecutarse.")
            return
        handler = HANDLERS.get(payload.get("type"))
        if not handler:
            raise ValueError("Tipo de orden no permitido.")
        detail = handler(store, payload)
    except Exception:
        # Error messages from browser/Drive libraries can contain private data.
        journal.finish(command_id, "FAILED", "No se pudo confirmar la acción. Revisa el panel local antes de solicitarla otra vez.")
        return
    journal.finish(command_id, "COMPLETED", detail)

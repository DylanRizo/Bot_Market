"""Trabajo diario sobre los anuncios ya publicados.

El trabajador lo llama cuando no hay publicaciones pendientes:

1. Ejecuta las acciones que la persona aprobo en el panel (y las automaticas).
2. Una vez al dia, dentro del horario del bot, lee "Tus anuncios" de cada cuenta,
   guarda edad, estado y clics, y recalcula las recomendaciones.
3. Si el SGI ya no tiene stock de un producto con anuncio activo, deja en cola
   marcarlo agotado.

Todo lo que toca Facebook corre en un proceso aparte y con el candado de la
cuenta, igual que las publicaciones.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import marketplace_listings as listings
from marketplace_runtime import AccountLock
from marketplace_safety import record_publication, register_outcome
from marketplace_storage import MarketplaceStore


SCRATCH_DIR = Path(__file__).resolve().parent
LISTINGS_SCRIPT = SCRATCH_DIR / "marketplace_listings.py"
SGI_REPORT_PATH = SCRATCH_DIR / "sgi_sync_report.json"

SCAN_EVERY_SECONDS = 20 * 60 * 60
SCAN_RETRY_SECONDS = 2 * 60 * 60
ACTION_GAP_SECONDS = 90          # entre dos acciones en la misma cuenta
MAX_AUTO_ACTIONS_PER_SCAN = 5
SUBPROCESS_TIMEOUT = 360


def scan_state_key(account: str) -> str:
    return f"listings_scan:{account}"


def action_state_key(account: str) -> str:
    return f"listings_action:{account}"


def within_schedule(config: dict[str, Any], now: datetime | None = None) -> bool:
    """Solo se toca Facebook dentro de los dias y el horario del bot."""
    now = now or datetime.now()
    if now.weekday() not in config.get("active_days", []):
        return False
    start = datetime.strptime(config.get("start_time", "09:00"), "%H:%M").time()
    end = datetime.strptime(config.get("end_time", "18:00"), "%H:%M").time()
    return start <= now.time() <= end


def scan_due(store: MarketplaceStore, account: str, now: float | None = None) -> bool:
    state = store.worker_state(scan_state_key(account))
    last = float(state.get("attempted_at_epoch") or 0)
    wait = SCAN_EVERY_SECONDS if state.get("status") == "ok" else SCAN_RETRY_SECONDS
    return (now if now is not None else time.time()) - last >= wait


def request_scan(store: MarketplaceStore, account: str = "") -> None:
    """Hace que la proxima vuelta del trabajador lea los anuncios (todas las cuentas si no se indica)."""
    accounts = [account] if account else list(_accounts())
    for key in accounts:
        store.heartbeat(scan_state_key(key), {"status": "requested", "attempted_at_epoch": 0})


def _accounts() -> dict[str, Any]:
    try:
        return json.loads((SCRATCH_DIR / "marketplace_accounts.json").read_text(encoding="utf-8")).get("accounts", {})
    except Exception:
        return {}


def load_report() -> dict[str, Any] | None:
    try:
        from marketplace_scheduler_worker import sgi_configured
    except ImportError:
        return None
    if not sgi_configured():
        return None
    try:
        return json.loads(SGI_REPORT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _run(command: list[str]) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command, cwd=str(SCRATCH_DIR), text=True, capture_output=True,
            timeout=SUBPROCESS_TIMEOUT, encoding="utf-8", errors="replace",
        )
        return completed.returncode, (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, output + "\n[listings-error:TIMEOUT] Facebook no respondio a tiempo."


def _error(output: str) -> tuple[str, str]:
    match = re.search(r"\[listings-error:([A-Z_]+)\]\s*(.*)", output)
    if match:
        return match.group(1), match.group(2).strip()[:300]
    lines = [line for line in output.splitlines() if line.strip()]
    return "INTERNAL", (lines[-1] if lines else "El proceso termino sin detalle.")[:300]


def scan_account(
    store: MarketplaceStore,
    account: str,
    address: str,
    run: Callable[[list[str]], tuple[int, str]] = _run,
) -> dict[str, Any]:
    started = time.time()
    try:
        with AccountLock(account, timeout_seconds=5):
            code, output = run([sys.executable, str(LISTINGS_SCRIPT), "scan", "--debugger-address", address])
    except RuntimeError as exc:
        # La cuenta esta publicando: se intenta en la siguiente vuelta.
        return {"ok": False, "code": "ACCOUNT_BUSY", "detail": str(exc)}
    match = re.search(r"\[listings-json\](.*)", output)
    if code != 0 or not match:
        error_code, detail = _error(output)
        store.heartbeat(scan_state_key(account), {
            "status": "error", "attempted_at_epoch": started, "code": error_code, "detail": detail,
        })
        if error_code == "ACCOUNT_RESTRICTED":
            register_outcome(store, account, False, error_code, detail)
        return {"ok": False, "code": error_code, "detail": detail}
    cards = json.loads(match.group(1))
    result = listings.save_scan(store, account, cards)
    store.heartbeat(scan_state_key(account), {
        "status": "ok", "attempted_at_epoch": started, "listings": result["saved"],
        "scanned_at": datetime.now().isoformat(timespec="seconds"),
    })
    return {"ok": True, **result}


def queue_automatic_actions(store: MarketplaceStore) -> int:
    """Encola las acciones que la persona autorizo a hacer sin preguntar."""
    queued = 0
    per_account: dict[str, int] = {}
    pending = {row["key"] for row in listings.pending_actions(store)}
    for listing in listings.list_listings(store):
        if listing["recommendation"] not in listings.AUTO_ACTIONS or listing["key"] in pending:
            continue
        account = listing["account"]
        if per_account.get(account, 0) >= MAX_AUTO_ACTIONS_PER_SCAN:
            continue
        listings.queue_action(store, listing["key"], listing["recommendation"], origin="auto")
        per_account[account] = per_account.get(account, 0) + 1
        queued += 1
    return queued


def execute_action(
    store: MarketplaceStore,
    row: dict[str, Any],
    address: str,
    run: Callable[[list[str]], tuple[int, str]] = _run,
) -> str:
    """Ejecuta una accion en cola. Devuelve su estado final."""
    listing = listings.get_listing(store, row["key"])
    if not listing:
        listings.finish_action(store, row["id"], "failed", "El anuncio ya no esta en la base.")
        return "failed"
    allowed, why = listings.action_allowed(store, row["account"], row["action"])
    if not allowed:
        # Se queda en cola: los limites se liberan solos (manana, tras la pausa).
        with store.connect() as connection:
            connection.execute("UPDATE listing_actions SET detail=? WHERE id=?", (f"En espera: {why}"[:500], row["id"]))
        return "waiting"
    listings.set_action_running(store, row["id"])
    try:
        with AccountLock(row["account"], timeout_seconds=5):
            code, output = run([
                sys.executable, str(LISTINGS_SCRIPT), "act", "--debugger-address", address,
                "--title", listing["title"], "--listed-on", listing["listed_on"], "--action", row["action"],
            ])
    except RuntimeError as exc:
        with store.connect() as connection:
            connection.execute(
                "UPDATE listing_actions SET status='pending', detail=? WHERE id=?",
                (f"En espera: {exc}"[:500], row["id"]),
            )
        return "waiting"
    except Exception as exc:  # noqa: BLE001 - la accion no debe quedar "en curso" para siempre.
        listings.finish_action(store, row["id"], "failed", f"INTERNAL: {exc}")
        return "failed"
    store.heartbeat(action_state_key(row["account"]), {"at_epoch": time.time()})
    done = re.search(r"\[listings-done\](.*)", output)
    if code == 0 and done:
        detail = done.group(1).strip()
        if row["action"] == "delete" and row.get("origin") == "manual":
            detail += " Entra primero en el proximo calendario de esta cuenta, con texto nuevo."
        listings.finish_action(store, row["id"], "done", detail)
        if row["action"] == "relist":
            record_publication(store, row["account"], listing["title"], "republicado")
        request_scan(store, row["account"])
        return "done"
    error_code, detail = _error(output)
    listings.finish_action(store, row["id"], "failed", f"{error_code}: {detail}")
    if error_code == "ACCOUNT_RESTRICTED":
        register_outcome(store, row["account"], False, error_code, detail)
    return "failed"


def run_listing_work(
    store: MarketplaceStore,
    config: dict[str, Any],
    ensure_chrome: Callable[[str, dict[str, Any]], tuple[bool, str]],
    log: Callable[[str], None] = print,
    now: datetime | None = None,
    run: Callable[[list[str]], tuple[int, str]] = _run,
) -> bool:
    """Una vuelta de trabajo sobre anuncios. True si hizo algo."""
    listings.ensure_schema(store)
    if not config.get("enabled") or config.get("mode") == "simulation":
        return False
    if not within_schedule(config, now):
        return False
    accounts = _accounts()

    for row in listings.pending_actions(store):
        if row["status"] == "running":
            continue
        account = accounts.get(row["account"])
        if not account:
            listings.finish_action(store, row["id"], "failed", "La cuenta ya no existe.")
            continue
        last = float(store.worker_state(action_state_key(row["account"])).get("at_epoch") or 0)
        if time.time() - last < ACTION_GAP_SECONDS:
            continue
        ok, detail = ensure_chrome(row["account"], account)
        if not ok:
            continue
        status = execute_action(store, row, str(account.get("debugger_address") or ""), run)
        if status == "waiting":
            continue
        log(f"Anuncio {row['action']} ({row['account']}): {status}")
        return True

    for key in config.get("accounts") or []:
        account = accounts.get(key)
        if not account or not scan_due(store, key):
            continue
        ok, detail = ensure_chrome(key, account)
        if not ok:
            store.heartbeat(scan_state_key(key), {
                "status": "error", "attempted_at_epoch": time.time(), "code": "SESSION_BLOCKED", "detail": detail,
            })
            continue
        result = scan_account(store, key, str(account.get("debugger_address") or ""), run)
        counts = listings.refresh_recommendations(store, load_report(), config.get("selected_families") or [])
        queued = queue_automatic_actions(store)
        if result.get("ok"):
            log(f"Anuncios leidos en {key}: {result['saved']}. Recomendaciones: {counts}. Automaticas en cola: {queued}.")
        else:
            log(f"No pude leer los anuncios de {key}: {result.get('code')} {result.get('detail')}")
        return True
    return False

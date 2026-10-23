from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from marketplace_runtime import alias_images_path
from marketplace_safety import backfill_events, check_can_publish
from marketplace_safety import load_safety
from marketplace_scheduler import apply_photo_order, generate_week, load_config, schedule_window
from marketplace_storage import DEFAULT_DB, MarketplaceStore, now_iso


SCRATCH_DIR = Path(__file__).resolve().parent
RUNNER_PATH = SCRATCH_DIR / "marketplace_campaign_runner.py"
CAMPAIGN_PATH = SCRATCH_DIR / "marketplace_campaign_example.json"
ACCOUNTS_PATH = SCRATCH_DIR / "marketplace_accounts.json"
ACTIVITY_PATH = SCRATCH_DIR / "marketplace_activity.json"
WORKER_JOBS_DIR = SCRATCH_DIR / "marketplace_worker_jobs"
PUBLISH_INTENTS_DIR = SCRATCH_DIR / "marketplace_publish_intents"
OPEN_SESSION_SCRIPT = SCRATCH_DIR / "abrir_sesion_marketplace_cuenta.ps1"

BLOCKING_ERRORS = {
    "SESSION_BLOCKED", "PUBLISH_OUTCOME_UNKNOWN", "DATA_INVALID", "IMAGE_REUSED",
    "ACCOUNT_RESTRICTED", "CONTENT_POLICY",
}
# La proteccion de cuenta frena el anuncio pero no es un fallo: se reprograma.
SAFETY_HOLD_CODE = "SAFETY_HOLD"
SAFETY_HOLD_RETRY_MINUTES = 60
PUBLISHING_MODES = {"semiautomatic", "autonomous"}
UNCERTAIN_PUBLISH_DETAIL = (
    "Se pulso Publish pero no hubo confirmacion. Revisa Marketplace antes de reintentar: "
    "el anuncio puede existir ya."
)

SGI_STATE_KEY = "sgi_sync"
SGI_SYNC_INTERVAL_SECONDS = 60 * 60
SGI_RETRY_AFTER_FAILURE_SECONDS = 10 * 60
# Motivos para no publicar que no son fallas: se omite el trabajo y se sigue.
SGI_SKIP_CODES = {"OUT_OF_STOCK", "PRICE_ISSUE", "NO_PHOTOS"}
SGI_RETRY_CODES = {"UNAVAILABLE", "RATE_LIMITED", "INVALID_RESPONSE"}


def log(message: str) -> None:
    print(f"[scheduler {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def chrome_status(address: str) -> tuple[bool, str]:
    if not address:
        return False, "La cuenta no tiene puerto de navegador configurado."
    try:
        with urllib.request.urlopen(f"http://{address}/json/version", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return True, str(payload.get("Browser") or "Chrome activo")
    except Exception as exc:
        return False, f"La sesion de Chrome no responde en {address}: {exc}"


# Chrome que abrio el propio trabajador: se cierra al terminar para liberar
# memoria (cada cuenta ocupa ~800 MB). Uno que ya estaba abierto (lo abrio la
# persona para responder mensajes) no se toca.
_BOT_OPENED: dict[str, str] = {}


def close_chrome(address: str) -> bool:
    """Cierra Chrome por su puerto de depuracion, como al cerrar la ventana (guarda la sesion)."""
    import websocket

    try:
        with urllib.request.urlopen(f"http://{address}/json/version", timeout=2) as response:
            url = json.loads(response.read().decode("utf-8", errors="replace"))["webSocketDebuggerUrl"]
        # Sin el encabezado Origin: Chrome rechaza (403) las conexiones que lo traen.
        connection = websocket.create_connection(url, timeout=5, suppress_origin=True)
        try:
            connection.send(json.dumps({"id": 1, "method": "Browser.close"}))
            try:
                connection.recv()
            except Exception:  # noqa: BLE001 - Chrome puede cerrar antes de responder
                pass
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - si no responde, ya esta cerrado
        return False
    return True


def release_idle_chrome(close: Any = close_chrome) -> list[str]:
    """Cierra los Chrome que abrio el bot. Devuelve las cuentas cerradas."""
    closed = []
    for account_key, address in list(_BOT_OPENED.items()):
        _BOT_OPENED.pop(account_key, None)
        if close(address):
            closed.append(account_key)
    return closed


def ensure_chrome(account_key: str, account: dict[str, Any]) -> tuple[bool, str]:
    address = str(account.get("debugger_address") or "")
    ok, detail = chrome_status(address)
    if ok:
        return ok, detail
    try:
        port = int(address.rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return False, detail
    profile = os.path.expandvars(str(account.get("chrome_profile") or ""))
    command = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OPEN_SESSION_SCRIPT),
        "-Account", account_key, "-Port", str(port),
    ]
    if profile:
        command.extend(["-ProfileDir", profile])
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    subprocess.Popen(command, cwd=str(SCRATCH_DIR), creationflags=creation_flags)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        time.sleep(2)
        ok, detail = chrome_status(address)
        if ok:
            _BOT_OPENED[account_key] = address
            return True, f"Chrome reiniciado: {detail}"
    return False, detail


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return alias_images_path(path if path.is_absolute() else SCRATCH_DIR / path)


def preflight(item: dict[str, Any], config: dict[str, Any]) -> tuple[bool, str, str]:
    accounts = load_json(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {})
    account = accounts.get(item["account"])
    if not account:
        return False, "DATA_INVALID", f"No existe la cuenta {item['account']}."
    paused = set(config.get("paused_accounts") or [])
    if item["account"] in paused:
        return False, "SESSION_BLOCKED", f"La cuenta {item['account']} esta pausada."
    if config.get("mode") == "simulation":
        browser_detail = "Simulacion: no se requiere navegador."
    else:
        browser_ok, browser_detail = ensure_chrome(item["account"], account)
        if not browser_ok:
            return False, "SESSION_BLOCKED", browser_detail
    job = item.get("job") or {}
    source = resolve_path(job.get("source_excel") or job.get("excel"))
    images = resolve_path(job.get("images_root"))
    if not source or not source.is_file():
        return False, "DATA_INVALID", f"No existe el inventario configurado: {source}."
    if not images or not images.is_dir():
        return False, "DATA_INVALID", f"No existe la carpeta de imagenes: {images}."
    if not job.get("name"):
        return False, "DATA_INVALID", "El trabajo no tiene nombre."
    return True, "", browser_detail


def classify_failure(
    output: str,
    returncode: int,
    publishing: bool = False,
    timed_out: bool = False,
) -> tuple[str, str]:
    code_match = re.search(r"\[error-code:([A-Z_]+)\]", output)
    if timed_out:
        code = "TIMEOUT"
    elif code_match:
        code = code_match.group(1)
    elif "ya esta siendo usada por otra publicacion" in output:
        code = "ACCOUNT_BUSY"
    elif "timed out" in output.lower() or "timeout" in output.lower():
        code = "TIMEOUT"
    elif "No existe" in output or "Job sin" in output:
        code = "DATA_INVALID"
    else:
        code = "PUBLISHER_FAILED"
    # Un timeout en modo publicacion puede haber cortado el proceso despues de
    # crear el anuncio. Reintentar a ciegas duplicaria la publicacion.
    if publishing and code == "TIMEOUT":
        code = "PUBLISH_OUTCOME_UNKNOWN"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    # El runner cierra con "Fallo el job 0: <titulo>", que no dice que paso. La
    # linea ERROR del publicador si lo dice.
    errors = [line.split("ERROR:", 1)[1].strip() for line in lines if "[marketplace-browser] ERROR:" in line]
    detail = errors[-1] if errors else (lines[-1] if lines else f"El publicador termino con codigo {returncode}.")
    if timed_out:
        detail = f"El publicador no respondio en 10 minutos y se corto. {detail}"
    return code, detail[:1000]


def listing_url_from_output(output: str) -> str:
    matches = re.findall(r"\[listing-url:(.+?)\]", output)
    return matches[-1].strip() if matches else ""


def intent_path_for(queue_id: str) -> Path:
    return PUBLISH_INTENTS_DIR / f"{queue_id}.json"


def clear_intent(queue_id: str) -> None:
    try:
        intent_path_for(queue_id).unlink()
    except FileNotFoundError:
        pass


def pending_intent_ids() -> set[str]:
    if not PUBLISH_INTENTS_DIR.is_dir():
        return set()
    return {path.stem for path in PUBLISH_INTENTS_DIR.glob("*.json")}


def prepare_reuse(store: MarketplaceStore, item: dict[str, Any], attempt_id: int, images: list[Path]) -> bool:
    """Antes de volver a subir un producto: sigue permitido y se borra el anuncio agotado anterior."""
    from marketplace_scheduler import reuse_check

    queue_id = item["id"]
    ok, reuse = reuse_check(store, item["account"], str(item.get("family_key") or ""), images, load_safety(store))
    if not ok:
        detail = "Ya no se puede repetir: la cuenta tiene otra vez un anuncio activo de este producto o no paso el plazo."
        store.finish_attempt(attempt_id, "skipped", "IMAGE_REUSED", detail)
        store.update_queue_item(queue_id, status="skipped", detail=detail, finished_at=now_iso())
        log(f"Repeticion cancelada: {item['name']} | cuenta={item['account']}")
        return False
    old = (reuse or {}).get("replace_listing")
    if not old:
        return True
    import marketplace_listings
    from marketplace_listing_worker import LISTINGS_SCRIPT, _error, _run
    from marketplace_runtime import AccountLock

    account = load_json(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {}).get(item["account"]) or {}
    chrome_ok, chrome_detail = ensure_chrome(item["account"], account)
    if not chrome_ok:
        store.postpone(queue_id, datetime.fromtimestamp(time.time() + 30 * 60).isoformat(timespec="seconds"),
                       f"En espera: {chrome_detail}")
        return False
    try:
        with AccountLock(item["account"], timeout_seconds=30):
            code, output = _run([
                sys.executable, str(LISTINGS_SCRIPT), "act", "--debugger-address", str(account.get("debugger_address") or ""),
                "--title", old["title"], "--listed-on", old["listed_on"], "--action", "delete",
            ])
    except RuntimeError as exc:
        store.postpone(queue_id, datetime.fromtimestamp(time.time() + 30 * 60).isoformat(timespec="seconds"), f"En espera: {exc}")
        return False
    if code == 0 and "[listings-done]" in output:
        action_id = marketplace_listings.queue_action(store, old["key"], "delete", origin="reuse")
        marketplace_listings.finish_action(store, action_id, "done", "Borrado para volver a publicarlo (aprobado en el calendario).")
        log(f"Anuncio anterior borrado para volver a subir {item['name']} | cuenta={item['account']}")
        return True
    error_code, detail = _error(output)
    message = f"No se pudo borrar el anuncio anterior ({old['title']}, {old['listed_on']}): {detail}"
    store.finish_attempt(attempt_id, "blocked", error_code, message, output)
    store.update_queue_item(queue_id, status="blocked", detail=message, finished_at=now_iso())
    store.create_alert("warning", "REUSE_DELETE_FAILED", message, item["account"], queue_id)
    return False


def with_photo_order(store: MarketplaceStore, item: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Copia del trabajo con las fotos en el orden que toca (manual o portada rotada)."""
    job = dict(item.get("job") or {})
    overrides = dict(job.get("listing_overrides") or {})
    paths = overrides.get("image_paths") or job.get("image_paths") or []
    if not paths:
        return item
    ordered, mode = apply_photo_order(
        store, config, load_safety(store), item["account"], str(item.get("family_key") or ""), list(paths)
    )
    overrides["image_paths"] = ordered
    job["listing_overrides"] = overrides
    log(f"Fotos ({mode}) para {item['name']} | cuenta={item['account']} | portada={Path(ordered[0]).parent.name}/{Path(ordered[0]).name}")
    return {**item, "job": job}


def campaign_for_item(item: dict[str, Any], config: dict[str, Any]) -> Path:
    base = load_json(CAMPAIGN_PATH, {"defaults": {}})
    defaults = dict(base.get("defaults") or {})
    defaults["ai_descriptions"] = bool(config.get("ai_descriptions", False))
    job = dict(item["job"])
    for key in ("source_excel", "excel", "images_root", "output_dir"):
        value = job.get(key)
        if value:
            resolved = resolve_path(str(value))
            job[key] = str(resolved if Path(str(value)).is_absolute() else resolved.resolve())
    job["intent_file"] = str(intent_path_for(item["id"]))
    payload = {
        "default_account": item["account"],
        "default_interval_minutes": 0,
        "defaults": defaults,
        "jobs": [job],
    }
    WORKER_JOBS_DIR.mkdir(parents=True, exist_ok=True)
    path = WORKER_JOBS_DIR / f"{item['id']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def sgi_configured() -> bool:
    """El SGI se usa solo con direccion y llave configuradas; si no, el bot sigue con su Excel."""
    try:
        from sgi_client import integration_key_configured, load_settings
    except ImportError:
        return False
    return bool((load_settings().get("sgi") or {}).get("base_url")) and integration_key_configured()


def sgi_sync_due(store: MarketplaceStore, now: float | None = None) -> bool:
    state = store.worker_state(SGI_STATE_KEY)
    attempted = float(state.get("attempted_at_epoch") or 0)
    wait = SGI_SYNC_INTERVAL_SECONDS if state.get("status") == "ok" else SGI_RETRY_AFTER_FAILURE_SECONDS
    return (now if now is not None else time.time()) - attempted >= wait


def refresh_from_sgi(store: MarketplaceStore) -> dict[str, Any]:
    """Regenera el Excel y las fotos desde el SGI y Drive, registra el resultado y relanza el error."""
    from sgi_sync import sync_from_local_configuration

    attempted = time.time()
    try:
        report = sync_from_local_configuration()
    except Exception as exc:
        store.heartbeat(
            SGI_STATE_KEY,
            {
                "attempted_at_epoch": attempted,
                "code": sgi_failure_code(exc),
                "detail": str(exc)[:300],
                "status": "error",
            },
        )
        raise
    store.heartbeat(
        SGI_STATE_KEY,
        {
            "attempted_at_epoch": attempted,
            "drive_offline": bool(report.get("drive_offline")),
            "generated_at": report["generated_at"],
            "price_issues": len(report["price_issues"]),
            "publishable": len(report["publishable"]),
            "status": "ok",
            "without_photos": len(report["without_photos"]),
        },
    )
    if report.get("drive_offline"):
        store.create_alert(
            "warning",
            "DRIVE_NOT_AUTHORIZED",
            "Google Drive no esta autorizado. El bot sigue publicando con las fotos ya descargadas; "
            "autoriza Drive en el panel para traer fotos nuevas.",
        )
    else:
        # Drive volvio a responder: el aviso anterior ya no aplica.
        with store.connect() as connection:
            connection.execute(
                "UPDATE alerts SET status='resolved', updated_at=? WHERE status='open' AND code='DRIVE_NOT_AUTHORIZED'",
                (now_iso(),),
            )
    with store.connect() as connection:
        connection.execute(
            "UPDATE alerts SET status='resolved', updated_at=? WHERE status='open' AND code LIKE 'SGI%'",
            (now_iso(),),
        )
    return report


def sgi_failure_code(exc: Exception) -> str:
    if exc.__class__.__name__ == "DriveNotAuthorized":
        return "DRIVE_NOT_AUTHORIZED"
    code = str(getattr(exc, "code", "") or "")
    if not code or code in SGI_RETRY_CODES:
        return "SGI_UNAVAILABLE"
    return f"SGI_{code}"


def sgi_gate(store: MarketplaceStore, item: dict[str, Any]) -> tuple[bool, str, str]:
    """Justo antes de publicar revalida contra el SGI el stock, el precio y las fotos.

    Tambien regenera el Excel: un anuncio agrupado no debe incluir una talla que
    se vendio despues de armar la semana.
    """
    if not sgi_configured():
        return True, "", ""
    prefixes = (item.get("job") or {}).get("sku_prefixes") or []
    if not prefixes:
        return True, "", ""
    from sgi_sync import revalidate_from_report

    try:
        report = refresh_from_sgi(store)
    except Exception as exc:
        return False, sgi_failure_code(exc), str(exc)[:500]
    return revalidate_from_report(report, prefixes)


def run_item(store: MarketplaceStore, item: dict[str, Any], config: dict[str, Any]) -> None:
    queue_id = item["id"]
    mode = config["mode"]
    publishing = mode in PUBLISHING_MODES

    # La aprobacion se comprueba antes que nada: no tiene sentido levantar Chrome
    # para un elemento que despues vamos a devolver a la cola.
    if mode == "semiautomatic" and not item["approved"]:
        detail = "Pendiente de aprobacion humana."
        store.update_queue_item(queue_id, status="planned", detail=detail)
        log(f"Pendiente de aprobacion: {item['name']}")
        return

    if publishing:
        inside, next_start = schedule_window(config)
        if not inside:
            store.postpone(
                queue_id,
                next_start.isoformat(timespec="seconds"),
                "Atrasado: se publica al empezar el proximo horario del bot.",
            )
            log(f"Fuera de horario: {item['name']} | cuenta={item['account']} queda para {next_start:%Y-%m-%d %H:%M}")
            return
        allowed, hold_code, hold_detail, retry_at = check_can_publish(store, item["account"])
        if not allowed:
            store.postpone(queue_id, retry_at, f"Esperando por proteccion de cuenta: {hold_detail}")
            log(f"En espera {item['name']} | cuenta={item['account']} | {hold_code} hasta {retry_at}")
            return

    attempt_id = store.begin_attempt(queue_id, item["attempts"])
    if store.publication_exists(item["account"], item["fingerprint"]):
        detail = "Omitido porque esta huella ya figura como publicada en la cuenta."
        store.finish_attempt(attempt_id, "skipped", "DUPLICATE", detail)
        store.update_queue_item(queue_id, status="skipped", detail=detail, finished_at=now_iso())
        log(f"Duplicado omitido: {item['name']}")
        return

    images = store.job_image_paths(item.get("job") or {})
    conflicts = store.media_conflicts(item["account"], images)
    if publishing and conflicts and (item.get("job") or {}).get("reuse") and item.get("approved"):
        # Repeticion aprobada: se vuelve a comprobar y se borra el anuncio viejo.
        handled = prepare_reuse(store, item, attempt_id, images)
        if not handled:
            return
        conflicts = []
    if publishing and conflicts:
        detail = (
            f"La cuenta {item['account']} ya publico {len(conflicts)} de estas fotos. "
            "Agrega fotos nuevas para volver a publicar este producto en la misma cuenta."
        )
        store.finish_attempt(attempt_id, "skipped", "IMAGE_REUSED", detail)
        store.update_queue_item(queue_id, status="skipped", detail=detail, finished_at=now_iso())
        log(f"Omitido por fotos ya usadas: {item['name']} | cuenta={item['account']}")
        return

    ok, gate_code, gate_detail = sgi_gate(store, item)
    if not ok:
        if gate_code in SGI_SKIP_CODES:
            store.finish_attempt(attempt_id, "skipped", gate_code, gate_detail)
            store.update_queue_item(queue_id, status="skipped", detail=gate_detail, finished_at=now_iso())
            # Agotarse es normal; un precio dudoso o una foto faltante piden accion.
            if gate_code != "OUT_OF_STOCK":
                store.create_alert("warning", gate_code, gate_detail, item["account"], queue_id)
            log(f"Omitido {item['name']}: {gate_detail}")
            return
        if gate_code == "SGI_UNAVAILABLE":
            store.finish_attempt(attempt_id, "failed", gate_code, gate_detail)
            store.schedule_retry(queue_id, int(config["retry_delay_minutes"]), gate_detail)
            log(f"SGI no disponible para {item['name']}; se reintentara.")
            return
        store.finish_attempt(attempt_id, "blocked", gate_code, gate_detail)
        store.update_queue_item(queue_id, status="blocked", detail=gate_detail, finished_at=now_iso())
        store.create_alert("critical", gate_code, gate_detail, item["account"], queue_id)
        log(f"Bloqueado {item['name']}: {gate_detail}")
        return

    ok, error_code, detail = preflight(item, config)
    if not ok:
        store.finish_attempt(attempt_id, "blocked", error_code, detail)
        store.update_queue_item(queue_id, status="blocked", detail=detail, finished_at=now_iso())
        store.create_alert("critical", error_code, detail, item["account"], queue_id)
        log(f"Bloqueado {item['name']}: {detail}")
        return

    item = with_photo_order(store, item, config)
    campaign_path = campaign_for_item(item, config)
    command = [
        sys.executable,
        str(RUNNER_PATH),
        "--campaign",
        str(campaign_path),
        "--activity-file",
        str(Path(config.get("activity_path") or ACTIVITY_PATH)),
        "--fast",
        "--max-jobs",
        "1",
    ]
    if mode == "simulation":
        command.append("--plan-only")
    elif publishing:
        command.append("--confirm-publish")

    # Cualquier intencion vieja se descarta ahora: a partir de aqui, si el archivo
    # aparece, lo escribio este intento.
    clear_intent(queue_id)

    log(f"Ejecutando {item['name']} | cuenta={item['account']} | modo={mode}")
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=str(SCRATCH_DIR),
            text=True,
            capture_output=True,
            timeout=600,
        )
        output = (completed.stdout or "") + (completed.stderr or "")
    except subprocess.TimeoutExpired as exc:
        # El aviso de timeout viene de la excepcion, no de lo que imprimio el hijo:
        # hay que arrastrarlo como bandera o la salida parece un fallo cualquiera.
        timed_out = True
        output = (exc.stdout or "") + (exc.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        completed = subprocess.CompletedProcess(command, 124, output, "")

    if completed.returncode == 0:
        if publishing:
            listing_url = listing_url_from_output(output)
            store.mark_published(queue_id, listing_url)
            clear_intent(queue_id)
            final_status = "published"
            detail = "Publicacion confirmada por el ejecutor."
            if listing_url:
                detail += f" URL: {listing_url}"
        elif mode == "simulation":
            final_status = "tested"
            detail = "Simulacion completada sin abrir Facebook."
            store.update_queue_item(queue_id, status=final_status, detail=detail, finished_at=now_iso())
        else:
            final_status = "tested"
            detail = "Formulario probado sin publicar."
            store.update_queue_item(queue_id, status=final_status, detail=detail, finished_at=now_iso())
        store.finish_attempt(attempt_id, final_status, detail=detail, output=output)
        log(f"Completado {item['name']}: {final_status}")
        return

    error_code, detail = classify_failure(
        output, completed.returncode, publishing=publishing, timed_out=timed_out
    )
    if error_code == SAFETY_HOLD_CODE and not intent_path_for(queue_id).exists():
        store.finish_attempt(attempt_id, "skipped", error_code, detail, output)
        retry_at = datetime.fromtimestamp(time.time() + SAFETY_HOLD_RETRY_MINUTES * 60).isoformat(timespec="seconds")
        store.postpone(queue_id, retry_at, detail)
        log(f"En espera {item['name']}: {detail}")
        return
    # Si quedo archivo de intencion, el clic en Publish ya salio. No importa como
    # haya fallado despues: reintentarlo crearia un segundo anuncio.
    if intent_path_for(queue_id).exists():
        error_code = "PUBLISH_OUTCOME_UNKNOWN"
        detail = f"{UNCERTAIN_PUBLISH_DETAIL} Detalle del publicador: {detail}"[:1000]
    store.finish_attempt(attempt_id, "failed", error_code, detail, output)
    if error_code in BLOCKING_ERRORS:
        store.update_queue_item(queue_id, status="blocked", detail=detail, finished_at=now_iso())
        store.create_alert("critical", error_code, detail, item["account"], queue_id)
    else:
        store.schedule_retry(queue_id, int(config["retry_delay_minutes"]), detail)
        current = store.get_queue_item(queue_id)
        if current and current["status"] == "blocked":
            store.create_alert("warning", error_code, detail, item["account"], queue_id)
    log(f"Fallo {item['name']}: {error_code} - {detail}")


CALENDAR_STATE_KEY = "calendar_generation"
EMPTY_CALENDAR_RETRY_SECONDS = 60 * 60


def ensure_future_calendar(store: MarketplaceStore, config: dict[str, Any]) -> None:
    pending = store.list_queue(statuses=["planned", "queued", "running", "retry"], limit=2000)
    future = [item for item in pending if item["scheduled_at"] >= now_iso()]
    if future:
        return
    # Si la ultima vez no salio nada que programar, se espera una hora: antes lo
    # intentaba cada 30 segundos (4.078 veces entre el 9 y el 17 de octubre).
    state = store.worker_state(CALENDAR_STATE_KEY)
    if state.get("created") == 0 and time.time() - float(state.get("attempted_at_epoch") or 0) < EMPTY_CALENDAR_RETRY_SECONDS:
        return
    result = generate_week(store, config)
    store.heartbeat(CALENDAR_STATE_KEY, {"attempted_at_epoch": time.time(), "created": result["created"]})
    log(f"Calendario generado automaticamente: {result['created']} publicaciones.")
    with store.connect() as connection:
        if result["created"]:
            connection.execute(
                "UPDATE alerts SET status='resolved', updated_at=? WHERE status='open' AND code='CALENDAR_EMPTY'",
                (now_iso(),),
            )
    if not result["created"]:
        store.create_alert(
            "warning",
            "CALENDAR_EMPTY",
            "No hay nada que programar: cada cuenta ya uso las fotos de todos los productos en rotacion. "
            "Los productos se pueden repetir cuando pasen los dias de espera y la cuenta no tenga un anuncio "
            "activo de ellos; mientras tanto, sube fotos nuevas o agrega productos a la rotacion.",
        )


def run_once(store: MarketplaceStore) -> bool:
    config = load_config(store)
    recovered = store.recover_stale(uncertain_ids=pending_intent_ids())
    for queue_id in recovered.get("blocked", []):
        item = store.get_queue_item(queue_id)
        store.create_alert(
            "critical",
            "PUBLISH_OUTCOME_UNKNOWN",
            UNCERTAIN_PUBLISH_DETAIL,
            item["account"] if item else "",
            queue_id,
        )
        log(f"Resultado incierto tras la interrupcion: {item['name'] if item else queue_id}")
    store.heartbeat("scheduler", {"status": "idle", "pid": __import__("os").getpid(), "enabled": config["enabled"]})
    if not config["enabled"]:
        return False
    if sgi_configured() and sgi_sync_due(store):
        try:
            report = refresh_from_sgi(store)
            log(
                f"Sincronizado con el SGI: {len(report['publishable'])} publicables, "
                f"{len(report['price_issues'])} con precio dudoso, {len(report['without_photos'])} sin foto."
            )
        except Exception as exc:
            code = sgi_failure_code(exc)
            store.create_alert("warning" if code == "SGI_UNAVAILABLE" else "critical", code, str(exc)[:500])
            log(f"No se pudo sincronizar con el SGI: {code}")
            # Sin catalogo fresco no se arma una semana con existencias viejas.
            return False
    ensure_future_calendar(store, config)
    item = store.claim_due()
    if not item:
        # Sin publicaciones pendientes: turno de revisar los anuncios ya publicados.
        from marketplace_listing_worker import run_listing_work

        try:
            return run_listing_work(store, config, ensure_chrome, log)
        except Exception as exc:  # noqa: BLE001 - revisar anuncios no debe frenar las publicaciones.
            log(f"Error al revisar anuncios: {exc}")
            return False
    store.heartbeat("scheduler", {"status": "running", "queue_id": item["id"], "name": item["name"]})
    run_item(store, item, config)
    store.heartbeat("scheduler", {"status": "idle", "last_queue_id": item["id"]})
    return True


WORKER_PID_PATH = SCRATCH_DIR / "marketplace_scheduler_worker.pid"


_INSTANCE_LOCK = None


def acquire_single_instance() -> bool:
    """Impide dos trabajadores a la vez (la tarea de Windows y el panel pueden lanzar uno cada uno)."""
    global _INSTANCE_LOCK
    import atexit

    from marketplace_runtime import AccountLock

    lock = AccountLock("scheduler_worker", timeout_seconds=0)
    try:
        lock.acquire()
    except RuntimeError:
        return False
    _INSTANCE_LOCK = lock
    atexit.register(lock.release)
    return True


def register_pid(path: Path = WORKER_PID_PATH) -> None:
    """Deja su numero de proceso para el panel.

    La tarea de Windows arranca el trabajador sin pasar por el panel; sin este
    archivo el panel lo mostraba detenido y podia lanzar una segunda copia.
    """
    import atexit

    pid = os.getpid()
    path.write_text(str(pid), encoding="utf-8")

    def cleanup() -> None:
        try:
            if path.read_text(encoding="utf-8").strip() == str(pid):
                path.unlink()
        except OSError:
            pass

    atexit.register(cleanup)


def main() -> None:
    parser = argparse.ArgumentParser(description="Trabajador persistente del calendario de Marketplace.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    store = MarketplaceStore(args.db)
    if not acquire_single_instance():
        log("Ya hay otro trabajador corriendo; este se cierra para no duplicar el calendario.")
        return
    log(f"Trabajador iniciado. Base={store.path}")
    register_pid()
    try:
        backfill_events(store, ACTIVITY_PATH)
    except Exception as exc:  # noqa: BLE001 - sin historial los limites arrancan en cero.
        log(f"No pude cargar el historial de publicaciones: {exc}")
    while True:
        try:
            worked = run_once(store)
        except KeyboardInterrupt:
            store.heartbeat("scheduler", {"status": "stopped"})
            raise
        except Exception as exc:
            log(f"Error del trabajador: {exc}")
            # Si la base falla (un "disk I/O error" pasajero), registrar el error
            # tambien falla; el trabajador debe seguir vivo y reintentar.
            try:
                store.heartbeat("scheduler", {"status": "error", "error": str(exc)})
                store.create_alert("critical", "WORKER_ERROR", str(exc))
            except Exception as nested:  # noqa: BLE001
                log(f"No pude registrar el error en la base: {nested}")
            worked = False
        try:
            for account_key in release_idle_chrome():
                log(f"Chrome de {account_key} cerrado para liberar memoria; se abre solo cuando haga falta.")
        except Exception as exc:  # noqa: BLE001 - cerrar Chrome nunca debe frenar al trabajador
            log(f"No pude cerrar Chrome: {exc}")
        if args.once:
            break
        time.sleep(2 if worked else max(5, args.poll_seconds))


if __name__ == "__main__":
    main()

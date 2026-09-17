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

from marketplace_safety import backfill_events, check_can_publish
from marketplace_scheduler import generate_week, load_config
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
            return True, f"Chrome reiniciado: {detail}"
    return False, detail


def resolve_path(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else SCRATCH_DIR / path


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


def campaign_for_item(item: dict[str, Any], config: dict[str, Any]) -> Path:
    base = load_json(CAMPAIGN_PATH, {"defaults": {}})
    defaults = dict(base.get("defaults") or {})
    defaults["ai_descriptions"] = bool(config.get("ai_descriptions", False))
    job = dict(item["job"])
    for key in ("source_excel", "excel", "images_root", "output_dir"):
        value = job.get(key)
        if value and not Path(str(value)).is_absolute():
            job[key] = str((SCRATCH_DIR / str(value)).resolve())
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
            "generated_at": report["generated_at"],
            "price_issues": len(report["price_issues"]),
            "publishable": len(report["publishable"]),
            "status": "ok",
            "without_photos": len(report["without_photos"]),
        },
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
    if publishing and conflicts:
        detail = (
            f"La cuenta {item['account']} ya publico {len(conflicts)} de estas fotos. "
            "Agrega fotos nuevas para volver a publicar este producto en la misma cuenta."
        )
        store.finish_attempt(attempt_id, "blocked", "IMAGE_REUSED", detail)
        store.update_queue_item(queue_id, status="blocked", detail=detail, finished_at=now_iso())
        store.create_alert("warning", "IMAGE_REUSED", detail, item["account"], queue_id)
        log(f"Foto repetida bloqueada: {item['name']} | cuenta={item['account']}")
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


def ensure_future_calendar(store: MarketplaceStore, config: dict[str, Any]) -> None:
    pending = store.list_queue(statuses=["planned", "queued", "running", "retry"], limit=2000)
    future = [item for item in pending if item["scheduled_at"] >= now_iso()]
    if not future:
        result = generate_week(store, config)
        log(f"Calendario generado automaticamente: {result['created']} publicaciones.")


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
        return False
    store.heartbeat("scheduler", {"status": "running", "queue_id": item["id"], "name": item["name"]})
    run_item(store, item, config)
    store.heartbeat("scheduler", {"status": "idle", "last_queue_id": item["id"]})
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Trabajador persistente del calendario de Marketplace.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    args = parser.parse_args()
    store = MarketplaceStore(args.db)
    log(f"Trabajador iniciado. Base={store.path}")
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
        if args.once:
            break
        time.sleep(2 if worked else max(5, args.poll_seconds))


if __name__ == "__main__":
    main()

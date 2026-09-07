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

from marketplace_scheduler import generate_week, load_config
from marketplace_storage import DEFAULT_DB, MarketplaceStore, now_iso


SCRATCH_DIR = Path(__file__).resolve().parent
RUNNER_PATH = SCRATCH_DIR / "marketplace_campaign_runner.py"
CAMPAIGN_PATH = SCRATCH_DIR / "marketplace_campaign_example.json"
ACCOUNTS_PATH = SCRATCH_DIR / "marketplace_accounts.json"
ACTIVITY_PATH = SCRATCH_DIR / "marketplace_activity.json"
WORKER_JOBS_DIR = SCRATCH_DIR / "marketplace_worker_jobs"
OPEN_SESSION_SCRIPT = SCRATCH_DIR / "abrir_sesion_marketplace_cuenta.ps1"

BLOCKING_ERRORS = {"SESSION_BLOCKED", "PUBLISH_OUTCOME_UNKNOWN", "DATA_INVALID", "IMAGE_REUSED"}


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


def classify_failure(output: str, returncode: int) -> tuple[str, str]:
    code_match = re.search(r"\[error-code:([A-Z_]+)\]", output)
    if code_match:
        code = code_match.group(1)
    elif "ya esta siendo usada por otra publicacion" in output:
        code = "ACCOUNT_BUSY"
    elif "timed out" in output.lower() or "timeout" in output.lower():
        code = "TIMEOUT"
    elif "No existe" in output or "Job sin" in output:
        code = "DATA_INVALID"
    else:
        code = "PUBLISHER_FAILED"
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    detail = lines[-1] if lines else f"El publicador termino con codigo {returncode}."
    return code, detail[:1000]


def campaign_for_item(item: dict[str, Any], config: dict[str, Any]) -> Path:
    base = load_json(CAMPAIGN_PATH, {"defaults": {}})
    defaults = dict(base.get("defaults") or {})
    defaults["ai_descriptions"] = bool(config.get("ai_descriptions", False))
    job = dict(item["job"])
    for key in ("source_excel", "excel", "images_root", "output_dir"):
        value = job.get(key)
        if value and not Path(str(value)).is_absolute():
            job[key] = str((SCRATCH_DIR / str(value)).resolve())
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


def run_item(store: MarketplaceStore, item: dict[str, Any], config: dict[str, Any]) -> None:
    queue_id = item["id"]
    attempt_id = store.begin_attempt(queue_id, item["attempts"])
    if store.publication_exists(item["account"], item["fingerprint"]):
        detail = "Omitido porque esta huella ya figura como publicada en la cuenta."
        store.finish_attempt(attempt_id, "skipped", "DUPLICATE", detail)
        store.update_queue_item(queue_id, status="skipped", detail=detail, finished_at=now_iso())
        log(f"Duplicado omitido: {item['name']}")
        return

    images = store.job_image_paths(item.get("job") or {})
    conflicts = store.media_conflicts(item["account"], images)
    if config["mode"] not in {"simulation", "dry_run", "supervised"} and conflicts:
        detail = (
            f"La cuenta {item['account']} ya publico {len(conflicts)} de estas fotos. "
            "Agrega fotos nuevas para volver a publicar este producto en la misma cuenta."
        )
        store.finish_attempt(attempt_id, "blocked", "IMAGE_REUSED", detail)
        store.update_queue_item(queue_id, status="blocked", detail=detail, finished_at=now_iso())
        store.create_alert("warning", "IMAGE_REUSED", detail, item["account"], queue_id)
        log(f"Foto repetida bloqueada: {item['name']} | cuenta={item['account']}")
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
    mode = config["mode"]
    if mode == "simulation":
        command.append("--plan-only")
    elif mode in {"semiautomatic", "autonomous"}:
        if mode == "semiautomatic" and not item["approved"]:
            detail = "Pendiente de aprobacion humana."
            store.finish_attempt(attempt_id, "planned", "APPROVAL_REQUIRED", detail)
            store.update_queue_item(queue_id, status="planned", detail=detail)
            return
        command.append("--confirm-publish")

    log(f"Ejecutando {item['name']} | cuenta={item['account']} | modo={mode}")
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
        output = (exc.stdout or "") + (exc.stderr or "")
        completed = subprocess.CompletedProcess(command, 124, output, "")

    if completed.returncode == 0:
        if mode in {"semiautomatic", "autonomous"}:
            store.mark_published(queue_id)
            final_status = "published"
            detail = "Publicacion confirmada por el ejecutor."
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

    error_code, detail = classify_failure(output, completed.returncode)
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
    store.recover_stale()
    store.heartbeat("scheduler", {"status": "idle", "pid": __import__("os").getpid(), "enabled": config["enabled"]})
    if not config["enabled"]:
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
    while True:
        try:
            worked = run_once(store)
        except KeyboardInterrupt:
            store.heartbeat("scheduler", {"status": "stopped"})
            raise
        except Exception as exc:
            store.heartbeat("scheduler", {"status": "error", "error": str(exc)})
            store.create_alert("critical", "WORKER_ERROR", str(exc))
            log(f"Error del trabajador: {exc}")
            worked = False
        if args.once:
            break
        time.sleep(2 if worked else max(5, args.poll_seconds))


if __name__ == "__main__":
    main()

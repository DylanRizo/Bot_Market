from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from marketplace_runtime import AccountLock


SCRATCH_DIR = Path(__file__).resolve().parent
AUTOMATOR = SCRATCH_DIR / "facebook_marketplace_browser_automator.py"
LISTING_BUILDER = SCRATCH_DIR / "marketplace_listing_builder.py"
DEFAULT_ACCOUNTS = SCRATCH_DIR / "marketplace_accounts.json"
DEFAULT_ACTIVITY = SCRATCH_DIR / "marketplace_activity.json"


def log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[campaign {timestamp}] {message}", flush=True)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def load_activity(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"items": []}
    try:
        payload = load_json(path)
        if not isinstance(payload.get("items"), list):
            payload["items"] = []
        return payload
    except Exception:
        return {"items": []}


def save_activity(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)


def add_activity_items(path: Path, jobs: list[dict[str, Any]], default_account: str | None) -> list[str]:
    payload = load_activity(path)
    items = payload["items"]
    run_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    scheduled = datetime.now()
    ids: list[str] = []
    for index, job in enumerate(jobs):
        delay = float(job.get("delay_minutes") or 0)
        scheduled += timedelta(minutes=delay)
        item_id = f"{run_id}-{index}"
        ids.append(item_id)
        items.append(
            {
                "id": item_id,
                "run_id": run_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "scheduled_at": scheduled.isoformat(timespec="seconds"),
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "name": job.get("name", f"job-{index}"),
                "account": job.get("account") or default_account or "",
                "status": "pending" if job.get("enabled", True) is not False else "skipped",
                "description_source": job.get("description_source", ""),
                "detail": "",
            }
        )
    payload["items"] = items[-300:]
    save_activity(path, payload)
    return ids


def update_activity(path: Path, item_id: str, status: str, detail: str = "") -> None:
    payload = load_activity(path)
    for item in payload["items"]:
        if item.get("id") == item_id:
            item["status"] = status
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if detail:
                item["detail"] = detail[:500]
            break
    save_activity(path, payload)


def expand_path(value: str | None, base_dir: Path) -> Path | None:
    if not value:
        return None
    expanded = os.path.expandvars(value)
    path = Path(expanded)
    if not path.is_absolute():
        path = base_dir / path
    return path


def bool_arg(args: list[str], enabled: bool, flag: str) -> None:
    if enabled:
        args.append(flag)


def list_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(item) for item in value)
    return str(value)


def builder_mode(job: dict[str, Any]) -> str:
    mode = job.get("listing_mode") or job.get("mode")
    if not mode:
        return ""
    group_by = job.get("group_by")
    if mode == "grouped" and group_by in {"color", "size"}:
        return f"grouped_by_{group_by}"
    return str(mode)


def build_dynamic_jobs(job: dict[str, Any], defaults: dict[str, Any], campaign_dir: Path) -> list[dict[str, Any]]:
    mode = builder_mode(job)
    if not mode:
        return [job]

    source_excel = expand_path(job.get("source_excel") or job.get("excel"), campaign_dir)
    images_root = expand_path(job.get("images_root"), campaign_dir)
    if not source_excel or not images_root:
        raise ValueError(f"Job dinamico sin source_excel/excel o images_root: {job.get('name', '<sin nombre>')}")

    output_dir = expand_path(job.get("output_dir") or "marketplace_campaign_generated", SCRATCH_DIR) or (
        SCRATCH_DIR / "marketplace_campaign_generated"
    )
    command = [
        sys.executable,
        str(LISTING_BUILDER),
        "--source-excel",
        str(source_excel),
        "--images-root",
        str(images_root),
        "--output-dir",
        str(output_dir),
        "--mode",
        mode,
        "--name",
        str(job.get("name", "")),
        "--category",
        str(job.get("category") or defaults.get("category") or "Men's clothing & shoes"),
        "--condition",
        str(job.get("condition") or defaults.get("condition") or "New"),
        "--json",
    ]
    optional_args = [
        ("row_indices", "--row-indices"),
        ("skus", "--skus"),
        ("sku_prefixes", "--sku-prefixes"),
        ("title_contains", "--title-contains"),
        ("exclude_title_contains", "--exclude-title-contains"),
    ]
    for key, flag in optional_args:
        value = list_value(job.get(key))
        if value:
            command.extend([flag, value])
    if job.get("max_items"):
        command.extend(["--max-items", str(job["max_items"])])
    listing_overrides = job.get("listing_overrides") or {}
    if listing_overrides.get("price") not in (None, ""):
        command.extend(["--price-override", str(listing_overrides["price"])])
    image_paths = listing_overrides.get("image_paths") or []
    if image_paths:
        command.extend(["--image-paths-json", json.dumps(image_paths, ensure_ascii=False)])
    tags = listing_overrides.get("tags") or []
    if tags:
        command.extend(["--tags-json", json.dumps(tags, ensure_ascii=False)])
    ai_enabled = bool(job.get("ai_descriptions", defaults.get("ai_descriptions", False)))
    if ai_enabled:
        command.append("--ai-descriptions")
    command.extend(
        [
            "--ai-model",
            str(job.get("ai_model") or defaults.get("ai_model") or "gpt-5.6-luna"),
            "--ai-tone",
            str(job.get("ai_tone") or defaults.get("ai_tone") or "directo, cercano y profesional"),
        ]
    )

    completed = subprocess.run(command, cwd=str(SCRATCH_DIR), text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"No pude construir listings para {job.get('name', '<sin nombre>')}: {completed.stderr}")

    payload = json.loads(completed.stdout)
    generated = payload.get("jobs", [])
    expanded: list[dict[str, Any]] = []
    item_interval = job.get("item_interval_minutes")
    for index, generated_job in enumerate(generated):
        next_job = dict(job)
        next_job.pop("listing_mode", None)
        next_job.pop("mode", None)
        next_job.pop("group_by", None)
        next_job.update(generated_job)
        if index == 0:
            next_job["delay_minutes"] = job.get("delay_minutes", 0)
        else:
            next_job["delay_minutes"] = item_interval if item_interval is not None else job.get("delay_minutes", 0)
        expanded.append(next_job)
    return expanded


def expand_jobs(jobs: list[dict[str, Any]], defaults: dict[str, Any], campaign_dir: Path) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("enabled", True) is False:
            expanded.append(job)
            continue
        expanded.extend(build_dynamic_jobs(job, defaults, campaign_dir))
    return expanded


def build_command(
    job: dict[str, Any],
    defaults: dict[str, Any],
    account: dict[str, Any],
    campaign_dir: Path,
    confirm_publish: bool,
    advance_only: bool,
) -> list[str]:
    excel = expand_path(job.get("excel"), campaign_dir)
    images_root = expand_path(job.get("images_root"), campaign_dir)
    if not excel:
        raise ValueError(f"Job sin excel: {job.get('name', '<sin nombre>')}")
    if not images_root:
        raise ValueError(f"Job sin images_root: {job.get('name', '<sin nombre>')}")

    debugger_address = job.get("debugger_address") or account.get("debugger_address")
    if not debugger_address:
        raise ValueError(f"Cuenta sin debugger_address para job {job.get('name', '<sin nombre>')}")

    command = [
        sys.executable,
        str(AUTOMATOR),
        "--debugger-address",
        str(debugger_address),
        "--excel",
        str(excel),
        "--images-root",
        str(images_root),
        "--row-index",
        str(job.get("row_index", 0)),
        "--category",
        str(job.get("category") or defaults.get("category") or "Men's clothing & shoes"),
        "--condition",
        str(job.get("condition") or defaults.get("condition") or "New"),
    ]

    bool_arg(command, bool(job.get("meet_public", defaults.get("meet_public", True))), "--meet-public")
    bool_arg(command, bool(job.get("door_pickup", defaults.get("door_pickup", True))), "--door-pickup")
    bool_arg(command, bool(job.get("door_dropoff", defaults.get("door_dropoff", True))), "--door-dropoff")
    bool_arg(command, bool(job.get("skip_sku_field", defaults.get("skip_sku_field", False))), "--skip-sku-field")

    if advance_only or confirm_publish:
        command.append("--publish")
    if confirm_publish:
        command.append("--confirm-publish")
        command.append("--strict-details")
        intent_file = job.get("intent_file")
        if intent_file:
            command.extend(["--intent-file", str(intent_file)])

    return command


def sleep_countdown(minutes: float, fast: bool) -> None:
    seconds = max(0, int(minutes * 60))
    if fast or seconds == 0:
        return
    while seconds > 0:
        chunk = min(seconds, 60)
        log(f"Esperando {seconds // 60}m {seconds % 60}s para la siguiente publicacion...")
        time.sleep(chunk)
        seconds -= chunk


def main() -> None:
    parser = argparse.ArgumentParser(description="Ejecuta una campana de publicaciones de Marketplace con intervalos.")
    parser.add_argument("--campaign", type=Path, default=SCRATCH_DIR / "marketplace_campaign_example.json")
    parser.add_argument("--accounts", type=Path, default=DEFAULT_ACCOUNTS)
    parser.add_argument("--from-job", type=int, default=0)
    parser.add_argument("--max-jobs", type=int, default=0)
    parser.add_argument("--fast", action="store_true", help="Ignora esperas; util para pruebas.")
    parser.add_argument("--plan-only", action="store_true", help="Muestra los jobs expandidos sin abrir Facebook.")
    parser.add_argument("--advance-only", action="store_true", help="Presiona Next pero no Publish.")
    parser.add_argument("--confirm-publish", action="store_true", help="Publica realmente cada job.")
    parser.add_argument("--activity-file", type=Path, default=DEFAULT_ACTIVITY)
    args = parser.parse_args()

    campaign_path = args.campaign.resolve()
    campaign_dir = campaign_path.parent
    campaign = load_json(campaign_path)
    accounts = load_json(args.accounts).get("accounts", {})
    defaults = campaign.get("defaults", {})
    jobs = expand_jobs(campaign.get("jobs", []), defaults, campaign_dir)
    if not jobs:
        raise SystemExit("La campana no tiene jobs.")

    selected_jobs = jobs[args.from_job :]
    if args.max_jobs:
        selected_jobs = selected_jobs[: args.max_jobs]

    default_account = campaign.get("default_account")
    default_interval = float(campaign.get("default_interval_minutes", 0))
    activity_path = args.activity_file.resolve()
    activity_ids = add_activity_items(activity_path, selected_jobs, default_account)

    for offset, job in enumerate(selected_jobs):
        original_index = args.from_job + offset
        activity_id = activity_ids[offset]
        if job.get("enabled", True) is False:
            log(f"Saltando job deshabilitado {original_index}: {job.get('name', original_index)}")
            continue
        delay = job.get("delay_minutes")
        if delay is None and original_index > 0:
            delay = default_interval
        delay = float(delay or 0)
        sleep_countdown(delay, args.fast)

        account_key = job.get("account") or default_account
        if not account_key:
            update_activity(activity_path, activity_id, "failed", "El job no tiene una cuenta asignada.")
            raise SystemExit(f"Job sin cuenta: {job.get('name', original_index)}")
        account = accounts.get(account_key)
        if not account:
            update_activity(activity_path, activity_id, "failed", f"No existe la cuenta {account_key}.")
            raise SystemExit(f"No existe la cuenta '{account_key}' en {args.accounts}")

        name = job.get("name", f"job-{original_index}")
        if args.plan_only:
            log(
                "PLAN "
                f"job={original_index} name={name} cuenta={account_key} "
                f"delay={delay} excel={job.get('excel')} row={job.get('row_index', 0)}"
            )
            update_activity(activity_path, activity_id, "planned", "Plan revisado; no se abrió Facebook.")
            continue
        update_activity(activity_path, activity_id, "running", "Preparando el anuncio en Facebook Marketplace.")
        log(f"Iniciando job {original_index}: {name} | cuenta={account_key}")
        command = build_command(job, defaults, account, campaign_dir, args.confirm_publish, args.advance_only)
        with AccountLock(str(account_key)):
            completed = subprocess.run(command, cwd=str(SCRATCH_DIR), text=True)
        if completed.returncode != 0:
            update_activity(activity_path, activity_id, "failed", f"El publicador terminó con código {completed.returncode}.")
            raise SystemExit(f"Fallo el job {original_index}: {name}")
        if args.confirm_publish:
            final_status = "published"
            detail = "Publicación enviada a Facebook Marketplace."
        elif args.advance_only:
            final_status = "prepared"
            detail = "Anuncio preparado hasta el paso Next; no se publicó."
        else:
            final_status = "tested"
            detail = "Prueba completada; no se publicó."
        update_activity(activity_path, activity_id, final_status, detail)
        log(f"Job completado: {name}")


if __name__ == "__main__":
    main()

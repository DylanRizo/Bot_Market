from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from marketplace_catalog import ASSISTANT_PRESETS, build_family_jobs, catalog_families
from marketplace_custom_products import custom_rotation_jobs
from marketplace_safety import load_safety, warmup_cap
from marketplace_storage import DEFAULT_DB, MarketplaceStore, listing_fingerprint


SCRATCH_DIR = Path(__file__).resolve().parent
CAMPAIGN_PATH = SCRATCH_DIR / "marketplace_campaign_example.json"
ACCOUNTS_PATH = SCRATCH_DIR / "marketplace_accounts.json"

DEFAULT_AUTONOMY_CONFIG: dict[str, Any] = {
    "enabled": False,
    "mode": "supervised",
    "account": "cuenta1",
    "accounts": ["cuenta1"],
    "account_strategy": "round_robin",
    "active_days": [0, 1, 2, 3, 4, 5],
    "start_time": "09:00",
    "end_time": "18:00",
    "max_per_day": 3,
    "interval_minutes": 180,
    "horizon_days": 7,
    "cooldown_days": 3,
    "max_attempts": 2,
    "retry_delay_minutes": 30,
    "slot_jitter_minutes": 7,
    "selected_families": list(ASSISTANT_PRESETS),
    "family_modes": {},
    "ai_descriptions": False,
}


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def parse_clock(value: str, fallback: str) -> time:
    try:
        return datetime.strptime(str(value), "%H:%M").time()
    except ValueError:
        return datetime.strptime(fallback, "%H:%M").time()


def normalize_config(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_AUTONOMY_CONFIG)
    config.update(raw or {})
    config["enabled"] = bool(config.get("enabled", False))
    # La IA solo se usa si la persona la enciende en Automatizacion.
    config["ai_descriptions"] = config.get("ai_descriptions") is True
    if config.get("mode") not in {"simulation", "dry_run", "supervised", "semiautomatic", "autonomous"}:
        config["mode"] = "supervised"
    config["active_days"] = sorted({int(day) for day in config.get("active_days", []) if 0 <= int(day) <= 6})
    accounts = config.get("accounts") or [config.get("account") or "cuenta1"]
    config["accounts"] = list(dict.fromkeys(str(account) for account in accounts if str(account).strip()))
    config["account"] = config["accounts"][0]
    if config.get("account_strategy") not in {"round_robin", "all_accounts"}:
        config["account_strategy"] = "round_robin"
    config["max_per_day"] = max(1, min(int(config.get("max_per_day") or 3), 20))
    config["interval_minutes"] = max(30, int(config.get("interval_minutes") or 180))
    config["horizon_days"] = max(1, min(int(config.get("horizon_days") or 7), 31))
    config["cooldown_days"] = max(0, int(config.get("cooldown_days") or 0))
    config["max_attempts"] = max(1, min(int(config.get("max_attempts") or 2), 5))
    config["retry_delay_minutes"] = max(5, int(config.get("retry_delay_minutes") or 30))
    config["slot_jitter_minutes"] = max(0, min(int(config.get("slot_jitter_minutes") or 0), 30))
    config["selected_families"] = [
        key for key in config.get("selected_families", []) if key in ASSISTANT_PRESETS
    ] or list(ASSISTANT_PRESETS)
    config["family_modes"] = {
        key: str(value) for key, value in dict(config.get("family_modes") or {}).items() if key in ASSISTANT_PRESETS
    }
    account_media: dict[str, dict[str, list[str]]] = {}
    for account, assignments in dict(config.get("account_media") or {}).items():
        if not isinstance(assignments, dict):
            continue
        account_media[str(account)] = {
            str(family): [str(path) for path in paths[:10] if str(path).strip()]
            for family, paths in assignments.items()
            if isinstance(paths, list) and paths
        }
    config["account_media"] = account_media
    # Productos en los que la persona fijo el orden de las fotos (la primera es
    # la portada). En los demas el bot rota la portada en cada publicacion.
    manual_order: dict[str, dict[str, bool]] = {}
    for account, families in dict(config.get("photo_order_manual") or {}).items():
        if isinstance(families, dict):
            chosen = {str(family): True for family, enabled in families.items() if enabled}
            if chosen:
                manual_order[str(account)] = chosen
    config["photo_order_manual"] = manual_order
    start = parse_clock(config.get("start_time", "09:00"), "09:00")
    end = parse_clock(config.get("end_time", "18:00"), "18:00")
    if end <= start:
        raise ValueError("La hora final debe ser posterior a la hora inicial.")
    config["start_time"] = start.strftime("%H:%M")
    config["end_time"] = end.strftime("%H:%M")
    return config


def load_config(store: MarketplaceStore) -> dict[str, Any]:
    raw = store.get_setting("autonomy", {})
    if not raw:
        campaign = load_json(CAMPAIGN_PATH, {})
        accounts = load_json(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {})
        first_account = campaign.get("default_account") or next(iter(accounts), "cuenta1")
        raw = {"account": first_account, "accounts": [first_account]}
    return normalize_config(raw)


def save_config(store: MarketplaceStore, raw: dict[str, Any]) -> dict[str, Any]:
    config = normalize_config(raw)
    accounts = load_json(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {})
    missing = [account for account in config["accounts"] if account not in accounts]
    if missing:
        raise ValueError(f"No existen las cuentas: {', '.join(missing)}.")
    store.set_setting("autonomy", config)
    return config


def schedule_window(config: dict[str, Any], now: datetime | None = None) -> tuple[bool, datetime]:
    """(estamos dentro del horario del bot, cuando empieza el proximo horario)."""
    current = now or datetime.now()
    start_clock = parse_clock(config.get("start_time", "09:00"), "09:00")
    end_clock = parse_clock(config.get("end_time", "18:00"), "18:00")
    days = set(config.get("active_days") or [])
    if current.weekday() in days and start_clock <= current.time() <= end_clock:
        return True, current
    for offset in range(0, 8):
        day = current.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        start = datetime.combine(day, start_clock)
        if start > current:
            return False, start
    return False, current + timedelta(days=1)


def jitter_span_minutes(config: dict[str, Any]) -> int:
    """Cuantos minutos puede moverse un espacio, sin llegar a solaparse con el siguiente."""
    requested = max(0, int(config.get("slot_jitter_minutes") or 0))
    room = max(0, int(config["interval_minutes"]) // 2 - 1)
    return min(requested, room)


def slot_offset(config: dict[str, Any], moment: datetime, index: int) -> timedelta:
    """Desplazamiento pseudoaleatorio pero estable para un espacio del calendario.

    Publicar siempre a las 09:00, 12:00 y 15:00 clavadas es un patron mecanico.
    La semilla depende del dia y de la posicion, asi que volver a generar la misma
    semana da el mismo horario y el calendario no se baraja entre regeneraciones.
    """
    span = jitter_span_minutes(config)
    if span <= 0:
        return timedelta()
    seed = f"{moment.date().isoformat()}|{index}|{config.get('start_time')}|{config.get('interval_minutes')}"
    return timedelta(minutes=random.Random(seed).randint(-span, span))


def build_slots(config: dict[str, Any], start_date: date | None = None, now: datetime | None = None) -> list[datetime]:
    current = now or datetime.now()
    first_date = start_date or current.date()
    start_clock = parse_clock(config["start_time"], "09:00")
    end_clock = parse_clock(config["end_time"], "18:00")
    interval = timedelta(minutes=config["interval_minutes"])
    slots: list[datetime] = []
    for day_offset in range(config["horizon_days"]):
        target_date = first_date + timedelta(days=day_offset)
        if target_date.weekday() not in config["active_days"]:
            continue
        start_at = datetime.combine(target_date, start_clock)
        end_at = datetime.combine(target_date, end_clock)
        candidate = start_at
        count = 0
        while candidate <= end_at and count < config["max_per_day"]:
            shifted = candidate + slot_offset(config, candidate, count)
            shifted = min(max(shifted, start_at), end_at)
            if shifted > current + timedelta(minutes=2):
                slots.append(shifted)
                count += 1
            candidate += interval
    return slots


def _eligible_candidate(
    candidate: dict[str, Any],
    scheduled_at: datetime,
    last_used: dict[str, datetime],
    cooldown_days: int,
) -> bool:
    family = str(candidate.get("family_key") or "")
    if not family or family not in last_used:
        return True
    return scheduled_at - last_used[family] >= timedelta(days=cooldown_days)


def rotate_cover(paths: list[str], offset: int) -> list[str]:
    """Empieza la lista de fotos en otra posicion para que cada cuenta tenga otra portada.

    Las fotos de una familia vienen alternando colores (blanca, gris, negra...),
    asi que correr una posicion cambia tambien el color de la portada.
    """
    if not paths or offset % len(paths) == 0:
        return list(paths)
    shift = offset % len(paths)
    return list(paths[shift:]) + list(paths[:shift])


def job_for_account(
    job: dict[str, Any], account: str, config: dict[str, Any], cover_offset: int = 0
) -> dict[str, Any]:
    account_job = dict(job)
    account_job["account"] = account
    family = str(job.get("family_key") or "")
    assigned = list((config.get("account_media") or {}).get(account, {}).get(family) or [])
    prefixes = tuple(str(prefix).upper() for prefix in job.get("sku_prefixes") or [])
    if prefixes:
        # Una seleccion guardada antes del SGI apunta a carpetas con codigos
        # viejos (TSBK-M): esas fotos ya no son de la familia y no se publican.
        assigned = [
            path for path in assigned if Path(path).parent.name.upper().startswith(prefixes) and Path(path).is_file()
        ]
    if assigned:
        overrides = dict(job.get("listing_overrides") or {})
        overrides["image_paths"] = list(assigned[:10])
        account_job["listing_overrides"] = overrides
    if cover_offset:
        overrides = dict(account_job.get("listing_overrides") or {})
        base = overrides.get("image_paths") or account_job.get("image_paths") or []
        if base:
            overrides["image_paths"] = rotate_cover(list(base), cover_offset)
            account_job["listing_overrides"] = overrides
    return account_job


def published_count(store: MarketplaceStore, family: str) -> int:
    """Cuantas veces publico el bot este producto, en cualquier cuenta."""
    with store.connect() as connection:
        row = connection.execute("SELECT COUNT(*) AS total FROM publications WHERE family_key=?", (family,)).fetchone()
    return int(row["total"] or 0)


def apply_photo_order(
    store: MarketplaceStore,
    config: dict[str, Any],
    safety: dict[str, Any],
    account: str,
    family: str,
    paths: list[str],
    extra_offset: int = 0,
) -> tuple[list[str], str]:
    """Orden final de las fotos de una publicacion: (fotos, modo).

    - "manual": el orden que la persona fijo en el panel para esa cuenta.
    - "auto": cada publicacion nueva del producto empieza con la foto siguiente,
      asi se prueban todas las portadas y se comparan en el panel.
    - "fijo": tal como vienen (variar la portada esta apagado).
    Se aplica al publicar, de modo que tambien vale para lo ya programado.
    """
    paths = [str(path) for path in paths]
    if not paths:
        return paths, "fijo"
    if (config.get("photo_order_manual") or {}).get(account, {}).get(family):
        preferred = [str(path) for path in (config.get("account_media") or {}).get(account, {}).get(family) or []]
        chosen = [path for path in preferred if path in paths]
        return chosen + [path for path in paths if path not in chosen], "manual"
    if not safety.get("vary_cover", True):
        return paths, "fijo"
    return rotate_cover(paths, published_count(store, family) + extra_offset), "auto"


def reuse_check(
    store: MarketplaceStore,
    account: str,
    family: str,
    images: list[Path] | list[str],
    safety: dict[str, Any],
    now: datetime | None = None,
) -> tuple[bool, dict[str, Any] | None]:
    """Si se puede publicar en la cuenta un producto con estas fotos.

    Devuelve (permitido, informacion de repeticion). Fotos nuevas en la cuenta:
    (True, None). Fotos ya usadas: solo despues de ``reuse_days`` y si la ultima
    revision de "Tus anuncios" no muestra un anuncio activo de ese producto; la
    informacion trae el anuncio agotado que habria que borrar antes de volver a
    subirlo. Sin revision de esa cuenta no se arriesga.
    """
    conflicts = store.media_conflicts(account, list(images))
    if not conflicts:
        return True, None
    current = now or datetime.now()
    last = max(datetime.fromisoformat(str(row["used_at"])) for row in conflicts)
    days = (current - last).days
    if days < int(safety.get("reuse_days", 14)):
        return False, None
    scan = store.worker_state(f"listings_scan:{account}")
    if scan.get("status") != "ok":
        return False, None
    import marketplace_listings

    same = [item for item in marketplace_listings.list_listings(store, account) if item["family_key"] == family]
    if any(item["status"] in {"in_stock", "pending"} for item in same):
        return False, None
    old = sorted((item for item in same if item["status"] == "out_of_stock"), key=lambda item: item["listed_on"])
    replace = old[-1] if old else None
    return True, {
        "last_used": last.isoformat(timespec="seconds"),
        "days": days,
        "replace_listing": {
            "key": replace["key"], "title": replace["title"], "listed_on": replace["listed_on"],
        } if replace else None,
    }


def reuse_detail(reuse: dict[str, Any]) -> str:
    text = f"Volver a publicar: esta cuenta ya lo publico hace {reuse['days']} dias con estas fotos."
    old = reuse.get("replace_listing")
    if old:
        return text + f" Al aprobar, se borra el anuncio anterior (agotado, del {old['listed_on']}) y se sube de nuevo."
    return text + " Ya no tiene un anuncio activo de este producto; al aprobar se sube de nuevo."


def calendar_lock(store: MarketplaceStore):
    """Un solo generador a la vez por base de datos.

    El 1 de octubre dos procesos armaron la semana al mismo tiempo, sin ver lo
    del otro, y dejaron dos publicaciones por espacio con las mismas fotos.
    """
    import hashlib

    from marketplace_runtime import AccountLock

    key = hashlib.sha256(str(Path(store.path).resolve()).encode("utf-8")).hexdigest()[:12]
    return AccountLock(f"calendar_{key}", timeout_seconds=90)


def generate_week(
    store: MarketplaceStore,
    config: dict[str, Any] | None = None,
    start_date: date | None = None,
) -> dict[str, Any]:
    with calendar_lock(store):
        return _generate_week(store, config, start_date)


def _generate_week(
    store: MarketplaceStore,
    config: dict[str, Any] | None = None,
    start_date: date | None = None,
) -> dict[str, Any]:
    config = normalize_config(config or load_config(store))
    safety = load_safety(store)
    # Con la proteccion activa, repetir un producto en la misma cuenta espera al
    # menos lo que marca la proteccion: dos anuncios iguales activos a la vez son
    # lo primero que Marketplace marca como duplicado.
    cooldown_days = config["cooldown_days"]
    if safety["enabled"]:
        cooldown_days = max(cooldown_days, safety["family_repeat_days"])
    family_jobs = build_family_jobs(
        config["account"], config["selected_families"], config.get("family_modes"), CAMPAIGN_PATH
    ) + custom_rotation_jobs(store)
    if not family_jobs:
        raise RuntimeError("No hay familias con precio e imagenes validas para calendarizar.")

    slots = build_slots(config, start_date=start_date)
    if not slots:
        raise RuntimeError("No hay espacios futuros dentro de los dias y horarios configurados.")

    last_used: dict[tuple[str, str], datetime] = {}
    for account in config["accounts"]:
        for family, value in store.recent_family_publications(account).items():
            try:
                last_used[(account, family)] = datetime.fromisoformat(value)
            except ValueError:
                continue
    existing = store.list_queue(statuses=["planned", "queued", "running", "retry", "published"], limit=2000)
    reserved_media: set[tuple[str, str]] = set()
    per_account_day: dict[tuple[str, str], int] = {}
    taken_slots: set[tuple[str, str]] = set()
    first_publication = {account: store.first_publish_event(account) for account in config["accounts"]}

    def day_cap(account: str, moment: datetime) -> int:
        # Una cuenta sin historial cuenta su calentamiento desde su primer anuncio.
        cap = warmup_cap(first_publication.get(account) or moment.isoformat(), safety, moment)
        return safety["max_per_account_day"] if cap is None else cap
    for item in existing:
        day_key = (item["account"], str(item.get("scheduled_at") or "")[:10])
        per_account_day[day_key] = per_account_day.get(day_key, 0) + 1
        taken_slots.add((item["account"], str(item.get("scheduled_at") or "")[:16]))
        family = item.get("family_key") or ""
        if not family:
            continue
        try:
            moment = datetime.fromisoformat(item["scheduled_at"])
        except ValueError:
            continue
        key = (item["account"], family)
        if key not in last_used or moment > last_used[key]:
            last_used[key] = moment
        # Solo reserva lo que falta publicar; las fotos ya publicadas las controla
        # reuse_check (14 dias y sin anuncio activo).
        if item["status"] == "published":
            continue
        for image in store.job_image_paths(item.get("job") or {}):
            reserved_media.add((item["account"], store.image_hash(image)))

    jobs = sorted(
        family_jobs,
        key=lambda job: min(
            (last_used.get((account, str(job.get("family_key"))), datetime.min) for account in config["accounts"]),
            default=datetime.min,
        ),
    )
    created: list[str] = []
    skipped_slots = 0
    cursor = 0
    initial_status = "queued" if config["mode"] == "autonomous" else "planned"
    approved = config["mode"] == "autonomous"
    for slot_index, slot in enumerate(slots):
        target_accounts = (
            list(config["accounts"])
            if config["account_strategy"] == "all_accounts"
            else [config["accounts"][slot_index % len(config["accounts"])]]
        )
        selected: dict[str, Any] | None = None
        selected_accounts: list[str] = []
        for offset in range(len(jobs)):
            candidate = jobs[(cursor + offset) % len(jobs)]
            family = str(candidate.get("family_key") or "")
            eligible_accounts: list[str] = []
            for account in target_accounts:
                account_candidate = job_for_account(candidate, account, config)
                images = store.job_image_paths(account_candidate)
                used_at = last_used.get((account, family))
                cooldown_ok = used_at is None or slot - used_at >= timedelta(days=cooldown_days)
                slot_taken = (account, slot.isoformat(timespec="minutes")) in taken_slots
                day_full = slot_taken or (
                    safety["enabled"]
                    and per_account_day.get((account, slot.date().isoformat()), 0) >= day_cap(account, slot)
                )
                reuse_ok, _reuse = reuse_check(store, account, family, images, safety, slot)
                published_conflict = not reuse_ok
                reserved_conflict = any((account, store.image_hash(image)) in reserved_media for image in images)
                if cooldown_ok and not day_full and not published_conflict and not reserved_conflict:
                    eligible_accounts.append(account)
            if eligible_accounts:
                selected = candidate
                selected_accounts = eligible_accounts
                cursor = (cursor + offset + 1) % len(jobs)
                break
        if not selected:
            skipped_slots += 1
            continue
        family = str(selected.get("family_key") or "")
        # El mismo producto en varias cuentas a minutos de diferencia parece una
        # campana coordinada. Se reparte dentro del intervalo del espacio.
        stagger = 7
        if safety["enabled"] and len(selected_accounts) > 1:
            room = max(7, (config["interval_minutes"] - 10) // len(selected_accounts))
            stagger = min(max(7, safety["cross_account_gap_minutes"]), room)
        for account_index, account in enumerate(selected_accounts):
            account_slot = slot + timedelta(minutes=account_index * stagger)
            scheduled_text = account_slot.isoformat(timespec="seconds")
            account_job = job_for_account(selected, account, config)
            _ok, reuse = reuse_check(store, account, family, store.job_image_paths(account_job), safety, account_slot)
            if reuse:
                account_job["reuse"] = reuse
            fingerprint = listing_fingerprint(account_job, account)
            queue_id, was_created = store.enqueue(
                {
                    "week_key": f"{slot.isocalendar().year}-W{slot.isocalendar().week:02d}",
                    "scheduled_at": scheduled_text,
                    "account": account,
                    "family_key": family,
                    "name": selected.get("name") or ASSISTANT_PRESETS.get(family, {}).get("label") or selected.get("name"),
                    "job": account_job,
                    "fingerprint": fingerprint,
                    # Repetir fotos en una cuenta siempre espera la aprobacion de la persona.
                    "status": "planned" if reuse else initial_status,
                    "approved": False if reuse else approved,
                    "detail": reuse_detail(reuse) if reuse else "",
                    "max_attempts": config["max_attempts"],
                    "dedupe_key": f"{scheduled_text}|{account}|{family}",
                }
            )
            if was_created:
                created.append(queue_id)
                day_key = (account, account_slot.date().isoformat())
                per_account_day[day_key] = per_account_day.get(day_key, 0) + 1
                last_used[(account, family)] = account_slot
                taken_slots.add((account, scheduled_text[:16]))
                for image in store.job_image_paths(account_job):
                    reserved_media.add((account, store.image_hash(image)))
    return {
        "created": len(created),
        "ids": created,
        "available_slots": len(slots),
        "skipped_slots": skipped_slots,
        "families": len(jobs),
        "config": config,
    }


def approve_items(store: MarketplaceStore, ids: list[str] | None = None, include_reuse: bool = True) -> int:
    """Aprueba lo pendiente. La aprobacion automatica del modo autonomo pasa
    ``include_reuse=False``: borrar un anuncio y volver a subirlo lo decide la persona."""
    candidates = store.list_queue(statuses=["planned"], limit=2000)
    requested = set(ids or [])
    changed = 0
    for item in candidates:
        if requested and item["id"] not in requested:
            continue
        if not include_reuse and (item.get("job") or {}).get("reuse"):
            continue
        if store.update_queue_item(item["id"], status="queued", approved=True):
            changed += 1
    return changed


def cancel_future(store: MarketplaceStore) -> int:
    changed = 0
    for item in store.list_queue(statuses=["planned", "queued", "retry"], limit=2000):
        if store.update_queue_item(item["id"], status="cancelled", detail="Cancelado al regenerar el calendario."):
            changed += 1
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Planificador semanal persistente para Marketplace.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--generate", action="store_true")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--cancel-future", action="store_true")
    parser.add_argument("--show", action="store_true")
    args = parser.parse_args()
    store = MarketplaceStore(args.db)
    if args.cancel_future:
        print(json.dumps({"cancelled": cancel_future(store)}, ensure_ascii=False))
    if args.generate:
        print(json.dumps(generate_week(store), ensure_ascii=False, indent=2))
    if args.approve:
        print(json.dumps({"approved": approve_items(store)}, ensure_ascii=False))
    if args.show or not any((args.generate, args.approve, args.cancel_future)):
        print(json.dumps({"config": load_config(store), "queue": store.list_queue()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

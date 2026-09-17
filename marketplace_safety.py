"""Proteccion de cuenta: cuanto y como publica cada cuenta de Marketplace.

Facebook limita el alcance (o bloquea) a las cuentas que publican mucho de golpe,
repiten el mismo anuncio, insisten tras un error o se saltan sus avisos. Este
modulo concentra las reglas que el bot respeta antes de cada anuncio real:

- limite por cuenta al dia y a la semana, y separacion minima entre anuncios;
- calentamiento: una cuenta que recien empieza a vender sube su ritmo poco a poco;
- pausa automatica tras fallos seguidos o si Facebook muestra un aviso de
  restriccion, en vez de seguir insistiendo;
- revision del texto del anuncio (enlaces, telefonos, mayusculas, titulos largos).

No intenta esconder la automatizacion. Publicar menos y mejor es lo que protege
la cuenta; los trucos para parecer humano no cambian lo que Facebook evalua del
anuncio y si los detecta el castigo es mayor.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from marketplace_storage import MarketplaceStore, now_iso


SAFETY_KEY = "safety"
PAUSES_KEY = "account_pauses"
HEALTH_KEY = "account_health"
BACKFILL_KEY = "publish_events_backfilled"

LEVELS: dict[str, dict[str, int]] = {
    "conservative": {
        "max_per_account_day": 2,
        "max_per_account_week": 8,
        "min_gap_minutes": 180,
        "cross_account_gap_minutes": 120,
        "family_repeat_days": 10,
    },
    "balanced": {
        "max_per_account_day": 3,
        "max_per_account_week": 12,
        "min_gap_minutes": 120,
        "cross_account_gap_minutes": 90,
        "family_repeat_days": 7,
    },
    "active": {
        "max_per_account_day": 5,
        "max_per_account_week": 20,
        "min_gap_minutes": 90,
        "cross_account_gap_minutes": 60,
        "family_repeat_days": 5,
    },
}

DEFAULT_SAFETY: dict[str, Any] = {
    "enabled": True,
    "level": "balanced",
    **LEVELS["balanced"],
    "warmup_enabled": True,
    "warmup_days": 14,
    "failure_pause_threshold": 2,
    "failure_pause_hours": 12,
    "restriction_pause_hours": 72,
    "vary_copy": True,
}

# Fallos que dicen algo de la cuenta o del formulario. Un SGI caido o una foto
# repetida no son culpa de Facebook y no cuentan para pausar.
COUNTED_FAILURES = {
    "FORM_FIELD", "PUBLISHER_FAILED", "INTERNAL", "IMAGE_INVALID", "TIMEOUT",
    "SESSION_BLOCKED", "PUBLISH_OUTCOME_UNKNOWN",
}
RESTRICTION_CODE = "ACCOUNT_RESTRICTED"

# Textos con los que Facebook avisa de limites o restricciones. Se comparan en
# minusculas y sin tildes.
RESTRICTION_PHRASES = (
    "you can't sell on marketplace",
    "you cant sell on marketplace",
    "you can't post",
    "you're temporarily blocked",
    "youre temporarily blocked",
    "temporarily restricted",
    "your account is restricted",
    "your access to marketplace",
    "we limit how often you can",
    "you've reached your limit",
    "youve reached your limit",
    "listing limit",
    "doesn't follow our commerce policies",
    "doesnt follow our commerce policies",
    "we removed your listing",
    "confirm your identity",
    "help us confirm it's you",
    "no puedes vender en marketplace",
    "bloqueado temporalmente",
    "restringimos tu",
    "tu cuenta esta restringida",
    "alcanzaste el limite",
    "no cumple nuestras politicas de comercio",
    "eliminamos tu publicacion",
    "confirma tu identidad",
)


def _int(value: Any, fallback: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = fallback
    return max(low, min(number, high))


def normalize_safety(raw: dict[str, Any] | None = None) -> dict[str, Any]:
    config = dict(DEFAULT_SAFETY)
    config.update(raw or {})
    config["enabled"] = bool(config.get("enabled", True))
    if config.get("level") not in {*LEVELS, "custom"}:
        config["level"] = "balanced"
    config["max_per_account_day"] = _int(config.get("max_per_account_day"), 3, 1, 20)
    config["max_per_account_week"] = _int(config.get("max_per_account_week"), 12, 1, 100)
    config["max_per_account_week"] = max(config["max_per_account_week"], config["max_per_account_day"])
    config["min_gap_minutes"] = _int(config.get("min_gap_minutes"), 120, 0, 24 * 60)
    config["cross_account_gap_minutes"] = _int(config.get("cross_account_gap_minutes"), 90, 0, 12 * 60)
    config["family_repeat_days"] = _int(config.get("family_repeat_days"), 7, 0, 60)
    config["warmup_enabled"] = bool(config.get("warmup_enabled", True))
    config["warmup_days"] = _int(config.get("warmup_days"), 14, 1, 60)
    config["failure_pause_threshold"] = _int(config.get("failure_pause_threshold"), 2, 1, 10)
    config["failure_pause_hours"] = _int(config.get("failure_pause_hours"), 12, 1, 24 * 7)
    config["restriction_pause_hours"] = _int(config.get("restriction_pause_hours"), 72, 1, 24 * 30)
    config["vary_copy"] = bool(config.get("vary_copy", True))
    return config


def load_safety(store: MarketplaceStore) -> dict[str, Any]:
    return normalize_safety(store.get_setting(SAFETY_KEY, {}))


def save_safety(store: MarketplaceStore, raw: dict[str, Any]) -> dict[str, Any]:
    config = normalize_safety(raw)
    store.set_setting(SAFETY_KEY, config)
    return config


def _parse(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def backfill_events(store: MarketplaceStore, activity_path: Path | None = None) -> int:
    """Carga una sola vez las publicaciones anteriores a esta tabla.

    Sin esto, una cuenta que ya publico apareceria como nueva y los limites
    arrancarian en cero.
    """
    if store.get_setting(BACKFILL_KEY, False):
        return 0
    events: set[tuple[str, str, str]] = set()
    with store.connect() as connection:
        for row in connection.execute("SELECT account, name, published_at FROM publications").fetchall():
            events.add((str(row["account"]), str(row["published_at"])[:16], str(row["name"])))
    if activity_path and activity_path.exists():
        try:
            items = json.loads(activity_path.read_text(encoding="utf-8")).get("items", [])
        except Exception:
            items = []
        for item in items:
            if item.get("status") == "published" and item.get("account") and item.get("updated_at"):
                events.add((str(item["account"]), str(item["updated_at"])[:16], str(item.get("name") or "")))
    # El trabajador deja la misma publicacion en las dos fuentes con segundos de
    # diferencia: se cuenta una vez por cuenta y minuto.
    seen: set[tuple[str, str]] = set()
    inserted = 0
    for account, minute, name in sorted(events):
        if (account, minute) in seen:
            continue
        seen.add((account, minute))
        store.record_publish_event(account, name, "historial", f"{minute}:00")
        inserted += 1
    store.set_setting(BACKFILL_KEY, True)
    return inserted


# --- pausas y salud ------------------------------------------------------------

def active_pauses(store: MarketplaceStore, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    current = now or datetime.now()
    pauses = store.get_setting(PAUSES_KEY, {}) or {}
    return {
        account: pause
        for account, pause in pauses.items()
        if (_parse(pause.get("until", "")) or datetime.min) > current
    }


def pause_account(store: MarketplaceStore, account: str, hours: int, reason: str, code: str = "ACCOUNT_PAUSED") -> str:
    until = (datetime.now() + timedelta(hours=max(1, int(hours)))).isoformat(timespec="seconds")
    pauses = store.get_setting(PAUSES_KEY, {}) or {}
    current = pauses.get(account) or {}
    # Una pausa mas corta nunca acorta una restriccion que ya estaba vigente.
    if (_parse(current.get("until", "")) or datetime.min) > datetime.fromisoformat(until):
        return current["until"]
    pauses[account] = {"until": until, "reason": reason[:300], "code": code, "since": now_iso()}
    store.set_setting(PAUSES_KEY, pauses)
    return until


def resume_account(store: MarketplaceStore, account: str) -> bool:
    pauses = store.get_setting(PAUSES_KEY, {}) or {}
    removed = pauses.pop(account, None) is not None
    store.set_setting(PAUSES_KEY, pauses)
    health = store.get_setting(HEALTH_KEY, {}) or {}
    if account in health:
        health[account]["consecutive_failures"] = 0
        store.set_setting(HEALTH_KEY, health)
    return removed


def register_outcome(store: MarketplaceStore, account: str, success: bool, error_code: str = "", detail: str = "") -> None:
    """Lleva la cuenta de fallos seguidos y pausa la cuenta cuando toca."""
    if not account:
        return
    config = load_safety(store)
    health = store.get_setting(HEALTH_KEY, {}) or {}
    entry = health.get(account) or {"consecutive_failures": 0}
    entry["last_result_at"] = now_iso()
    if success:
        entry["consecutive_failures"] = 0
        entry["last_success_at"] = entry["last_result_at"]
        entry["last_error"] = ""
    elif error_code == RESTRICTION_CODE or error_code in COUNTED_FAILURES:
        entry["consecutive_failures"] = int(entry.get("consecutive_failures") or 0) + 1
        entry["last_error"] = error_code
    health[account] = entry
    store.set_setting(HEALTH_KEY, health)
    if success or not config["enabled"]:
        return

    if error_code == RESTRICTION_CODE:
        until = pause_account(store, account, config["restriction_pause_hours"], detail or "Facebook mostro un aviso de restriccion.", RESTRICTION_CODE)
        store.create_alert(
            "critical",
            RESTRICTION_CODE,
            f"Facebook mostro un aviso de limite o restriccion. La cuenta queda en pausa hasta {until[:16].replace('T', ' ')}. {detail}".strip(),
            account,
        )
    elif entry["consecutive_failures"] >= config["failure_pause_threshold"]:
        until = pause_account(
            store, account, config["failure_pause_hours"],
            f"{entry['consecutive_failures']} fallos seguidos ({error_code}).",
        )
        entry["consecutive_failures"] = 0
        health[account] = entry
        store.set_setting(HEALTH_KEY, health)
        store.create_alert(
            "warning",
            "ACCOUNT_PAUSED",
            f"{config['failure_pause_threshold']} fallos seguidos. La cuenta descansa hasta "
            f"{until[:16].replace('T', ' ')} para no insistir sobre Facebook.",
            account,
        )


# --- limites -----------------------------------------------------------------

def warmup_cap(first_at: str, config: dict[str, Any], now: datetime) -> int | None:
    """Limite diario durante el calentamiento, o None si ya termino."""
    if not config["warmup_enabled"]:
        return None
    first = _parse(first_at) if first_at else None
    days = 0 if first is None else (now.date() - first.date()).days
    if days >= config["warmup_days"]:
        return None
    # Primera mitad: 1 al dia. Segunda mitad: 2 al dia (o el maximo si es menor).
    step = 1 if days < config["warmup_days"] / 2 else 2
    return min(step, config["max_per_account_day"])


def account_usage(store: MarketplaceStore, account: str, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    week_start = current - timedelta(days=7)
    events = store.publish_events(account, week_start.isoformat(timespec="seconds"))
    today = current.date().isoformat()
    return {
        "today": sum(1 for value in events if value[:10] == today),
        "week": len(events),
        "week_events": events,
        "last_at": events[-1] if events else "",
        "first_at": store.first_publish_event(account),
    }


def daily_limit(store: MarketplaceStore, account: str, config: dict[str, Any] | None = None, now: datetime | None = None) -> int:
    config = config or load_safety(store)
    current = now or datetime.now()
    cap = warmup_cap(store.first_publish_event(account), config, current)
    return config["max_per_account_day"] if cap is None else cap


def check_can_publish(
    store: MarketplaceStore, account: str, now: datetime | None = None
) -> tuple[bool, str, str, str]:
    """(permitido, codigo, explicacion, cuando volver a intentar)."""
    config = load_safety(store)
    current = now or datetime.now()
    if not config["enabled"]:
        return True, "", "", ""
    pause = active_pauses(store, current).get(account)
    if pause:
        return False, "ACCOUNT_PAUSED", f"Cuenta en pausa: {pause.get('reason', '')}", pause["until"]

    usage = account_usage(store, account, current)
    limit = daily_limit(store, account, config, current)
    tomorrow = datetime.combine(current.date() + timedelta(days=1), datetime.min.time()).replace(hour=9)
    if usage["today"] >= limit:
        warm = " (cuenta en calentamiento)" if limit < config["max_per_account_day"] else ""
        return (
            False, "DAILY_LIMIT",
            f"La cuenta ya publico {usage['today']} de {limit} anuncios hoy{warm}.",
            tomorrow.isoformat(timespec="seconds"),
        )
    if usage["week"] >= config["max_per_account_week"]:
        oldest = _parse(usage["week_events"][0]) or current
        retry = max(oldest + timedelta(days=7, minutes=5), current + timedelta(minutes=30))
        return (
            False, "WEEKLY_LIMIT",
            f"La cuenta ya publico {usage['week']} de {config['max_per_account_week']} anuncios en 7 dias.",
            retry.isoformat(timespec="seconds"),
        )
    last = _parse(usage["last_at"]) if usage["last_at"] else None
    if last and config["min_gap_minutes"] and current - last < timedelta(minutes=config["min_gap_minutes"]):
        retry = last + timedelta(minutes=config["min_gap_minutes"])
        return (
            False, "MIN_GAP",
            f"Faltan {int((retry - current).total_seconds() // 60) + 1} min para respetar la separacion entre anuncios.",
            retry.isoformat(timespec="seconds"),
        )
    return True, "", "", ""


def record_publication(store: MarketplaceStore, account: str, name: str = "", source: str = "") -> None:
    store.record_publish_event(account, name, source)
    register_outcome(store, account, True)


# --- revision del texto ------------------------------------------------------

TITLE_MAX = 100
TITLE_RECOMMENDED = 70
URL_RE = re.compile(r"(https?://|www\.|\b[\w-]+\.(com|net|org|shop|store|ni|me|ly)\b)", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(\+?505[\s-]?)?\d{4}[\s-]?\d{4}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
OFF_PLATFORM_RE = re.compile(r"\b(whats\s?app|wsp|wa\.me|telegram|instagram\.com|t\.me)\b", re.IGNORECASE)


def lint_listing(title: str, description: str, price: Any, tags: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(errores que impiden publicar, avisos)."""
    errors: list[str] = []
    warnings: list[str] = []
    title = str(title or "").strip()
    description = str(description or "").strip()
    if not title:
        errors.append("El anuncio no tiene titulo.")
    if len(title) > TITLE_MAX:
        errors.append(f"El titulo tiene {len(title)} caracteres; Facebook acepta hasta {TITLE_MAX}.")
    elif len(title) > TITLE_RECOMMENDED:
        warnings.append(f"Titulo largo ({len(title)} caracteres). Los primeros 40 son los que se ven en el feed.")
    try:
        price_value = float(str(price).replace("C$", "").replace(",", "").strip() or 0)
    except ValueError:
        price_value = 0
    if price_value <= 0:
        errors.append("El precio debe ser mayor que cero.")
    text = f"{title}\n{description}"
    if URL_RE.search(text):
        errors.append("El anuncio incluye un enlace. Marketplace penaliza los enlaces externos.")
    if EMAIL_RE.search(text):
        errors.append("El anuncio incluye un correo. Pide que te escriban por Messenger.")
    if PHONE_RE.search(text):
        warnings.append("El anuncio incluye un telefono. Facebook reduce el alcance de anuncios con datos de contacto.")
    if OFF_PLATFORM_RE.search(text):
        warnings.append("El anuncio menciona otra app de mensajeria; mejor cerrar la venta por Messenger.")
    letters = [char for char in title if char.isalpha()]
    if len(letters) >= 10 and sum(char.isupper() for char in letters) / len(letters) > 0.6:
        warnings.append("El titulo esta casi todo en mayusculas.")
    if re.search(r"([!?$*])\1{2,}", text):
        warnings.append("Hay signos repetidos (!!!, $$$). Parecen spam.")
    if len(description) < 40:
        warnings.append("La descripcion es muy corta; agrega material, tallas o uso.")
    return errors, warnings


# --- resumen para el panel ---------------------------------------------------

def health_snapshot(store: MarketplaceStore, accounts: list[str], now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now()
    config = load_safety(store)
    pauses = active_pauses(store, current)
    health = store.get_setting(HEALTH_KEY, {}) or {}
    result: dict[str, Any] = {}
    for account in accounts:
        usage = account_usage(store, account, current)
        cap = warmup_cap(usage["first_at"], config, current)
        first = _parse(usage["first_at"]) if usage["first_at"] else None
        days_active = 0 if first is None else (current.date() - first.date()).days
        ok, code, detail, retry_at = check_can_publish(store, account, current)
        if account in pauses:
            status = "paused"
        elif not ok:
            status = "waiting"
        elif cap is not None and config["enabled"]:
            status = "warming"
        else:
            status = "ok"
        result[account] = {
            "status": status,
            "today": usage["today"],
            "week": usage["week"],
            "daily_limit": daily_limit(store, account, config, current),
            "weekly_limit": config["max_per_account_week"],
            "last_at": usage["last_at"],
            "warmup_day": days_active + 1 if cap is not None else None,
            "warmup_days": config["warmup_days"],
            "pause": pauses.get(account),
            "blocked_code": code,
            "blocked_detail": detail,
            "next_allowed_at": retry_at,
            "consecutive_failures": int((health.get(account) or {}).get("consecutive_failures") or 0),
            "last_error": (health.get(account) or {}).get("last_error", ""),
        }
    return result

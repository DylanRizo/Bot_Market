"""Seguimiento de los anuncios ya publicados en Marketplace.

Una vez al dia el trabajador lee "Tus anuncios" de cada cuenta y guarda, por
anuncio, su estado, la fecha en que se publico y los clics de los ultimos 14
dias. Con eso calcula cuantos dias lleva cada anuncio y recomienda que hacer:
esperar, renovarlo, revisar el precio, cambiar la foto, republicarlo, marcarlo
agotado o eliminarlo.

Las acciones se ejecutan solo cuando la persona las aprueba desde el panel,
salvo una: si el SGI ya no tiene stock de un producto y su anuncio sigue activo,
se marca como agotado solo para que nadie escriba por algo que no hay.

Uso desde la linea de comandos (lo lanza el trabajador en un proceso aparte):
    python marketplace_listings.py scan --debugger-address 127.0.0.1:9222
    python marketplace_listings.py act --debugger-address ... --title "..." --listed-on 2026-09-20 --action renew
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from marketplace_storage import MarketplaceStore, now_iso


SELLING_URL = "https://www.facebook.com/marketplace/you/selling"

# Umbrales de las recomendaciones. Los clics son los que muestra Facebook:
# visitas a la ficha del anuncio en los ultimos 14 dias.
RULES: dict[str, int] = {
    "new_days": 3,              # antes de esto no se juzga un anuncio
    "renew_after_days": 7,      # Facebook permite renovar a partir del dia 7
    "renew_cooldown_days": 7,   # no renovar el mismo anuncio mas de una vez por semana
    "low_clicks": 5,            # menos que esto en 14 dias es poco interes
    "high_clicks": 15,          # desde aqui hay interes; si no se vende, algo falla
    "stale_days": 21,           # renovado y sin interes: toca cambiar foto o titulo
    "dead_days": 45,            # mas viejo que esto y sin interes: eliminar
    "renews_before_edit": 2,
    "max_renews_per_day": 3,    # por cuenta; renovar mucho de golpe es una senal de spam
}

EXECUTABLE_ACTIONS = {"renew", "delete", "relist", "mark_out_of_stock", "mark_available"}
AUTO_ACTIONS = {"mark_out_of_stock"}
ACTION_LABELS = {
    "wait": "Esperar",
    "keep": "Va bien",
    "renew": "Renovar",
    "review": "Revisar precio y mensajes",
    "edit": "Cambiar foto o titulo",
    "delete": "Eliminar",
    "relist": "Republicar",
    "mark_out_of_stock": "Marcar agotado",
    "mark_available": "Marcar disponible",
    "none": "Nada que hacer",
}

STATUS_WORDS = {
    "in stock": "in_stock",
    "active": "in_stock",
    "activo": "in_stock",
    "available": "in_stock",
    "disponible": "in_stock",
    "en stock": "in_stock",
    "out of stock": "out_of_stock",
    "agotado": "out_of_stock",
    "sin stock": "out_of_stock",
    "sold": "sold",
    "vendido": "sold",
    "pending": "pending",
    "pendiente": "pending",
}

# Palabras del titulo que identifican la familia del producto. El orden importa:
# "manga larga" antes que "compresion", "youngla" antes que "short".
FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    # "Chaleco de lana sin mangas" contiene "sin mangas": va antes que las camisas.
    ("chalecos", ("chaleco",)),
    # "Calcetas de compresion" contiene "compresion": van antes que las camisas.
    ("calcetas_multicolor", ("calcetas multicolor", "calcetas - multicolor", "calceta multicolor", "calcetas locas")),
    ("calcetas", ("calceta", "medias")),
    ("compression_long", ("manga larga",)),
    ("compression_sleeveless", ("sin mangas",)),
    ("mangas_brazo", ("manga para brazo", "mangas para brazo", "manga de basquet")),
    ("compression_short", ("compresion",)),
    ("bershka", ("bershka", "bersek", "berserk", "berkserk")),
    ("enterizos_largos", ("enterizo largo", "enterizos largos")),
    ("enterizos", ("enterizo",)),
    ("leggins", ("leggin",)),
    ("shorts_youngla", ("youngla",)),
    ("shorts", ("short",)),
    ("faldas", ("falda",)),
    ("tops", ("top ", "top deportivo", "tops")),
    ("bolsos", ("bolso",)),
    ("mochilas", ("mochila",)),
    ("durags", ("durag",)),
    ("munequeras", ("munequera",)),
    ("straps", ("strap", "correas de levantamiento")),
    ("joggers", ("jogger",)),
    ("rodilleras", ("rodillera",)),
    ("cinturones", ("cinturon",)),
]


def plain(text: str) -> str:
    text = unicodedata.normalize("NFD", str(text or ""))
    text = "".join(char for char in text if unicodedata.category(char) != "Mn")
    return re.sub(r"\s+", " ", text).strip().lower()


def family_for_title(title: str) -> str:
    value = f" {plain(title)} "
    for family, words in FAMILY_KEYWORDS:
        if any(word in value for word in words):
            return family
    return ""


# --- lectura de tarjetas -----------------------------------------------------

def parse_listed_on(text: str, today: date) -> date | None:
    """"Listed on 9/25" o "Publicado el 25/9". Sin ano: el mas reciente que no sea futuro."""
    match = re.search(r"(?:listed on|publicado el)\s+(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", plain(text))
    if not match:
        return None
    first, second, year = int(match.group(1)), int(match.group(2)), match.group(3)
    spanish = "publicado el" in plain(text)
    month, day = (second, first) if spanish else (first, second)
    if year:
        full_year = int(year) + (2000 if len(year) == 2 else 0)
        try:
            return date(full_year, month, day)
        except ValueError:
            return None
    try:
        candidate = date(today.year, month, day)
    except ValueError:
        return None
    if candidate > today:
        candidate = date(today.year - 1, month, day)
    return candidate


def parse_card(raw: dict[str, Any], today: date) -> dict[str, Any] | None:
    """Convierte el texto de una tarjeta de "Tus anuncios" en datos."""
    text = str(raw.get("texto") or "")
    lines = [line.strip() for line in text.splitlines() if line.strip() and line.strip() not in {"·", "\xa0"}]
    if not lines:
        return None
    title = lines[0]
    price_match = re.search(r"(NIO|C\$)\s?([\d,]+(?:\.\d+)?)", text)
    price = float(price_match.group(2).replace(",", "")) if price_match else None
    status = "unknown"
    for line in lines[1:6]:
        head = plain(line.split("·")[0])
        if head in STATUS_WORDS:
            status = STATUS_WORDS[head]
            break
    clicks_match = re.search(r"([\d,.]+)\s+(?:clicks?|clics?)\b", text, re.IGNORECASE)
    clicks = int(clicks_match.group(1).replace(",", "").replace(".", "")) if clicks_match else 0
    listed_on = parse_listed_on(text, today)
    fb_id = ""
    for href in raw.get("hrefs") or []:
        found = re.search(r"target_id=(\d+)", str(href)) or re.search(r"/marketplace/item/(\d+)", str(href))
        if found:
            fb_id = found.group(1)
            break
    buttons = " | ".join(str(button) for button in raw.get("botones") or [])
    return {
        "title": title,
        "price": price,
        "status": status,
        "listed_on": listed_on.isoformat() if listed_on else "",
        "clicks_14d": clicks,
        "in_groups": "group" in plain(text) or "grupo" in plain(text),
        "fb_id": fb_id,
        "can_relist": "relist this item" in plain(buttons) or "volver a publicar" in plain(buttons),
    }


def listing_key(account: str, title: str, listed_on: str) -> str:
    raw = f"{account}|{plain(title)}|{listed_on}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


# --- recomendaciones ---------------------------------------------------------

def age_days(listed_on: str, today: date) -> int | None:
    try:
        return max(0, (today - date.fromisoformat(listed_on)).days)
    except (TypeError, ValueError):
        return None


def recommend(
    listing: dict[str, Any],
    today: date,
    *,
    has_stock: bool | None,
    newer_active_duplicate: bool = False,
    in_rotation: bool = False,
    active_same_family: bool = False,
    last_renew: str = "",
    renew_count: int = 0,
    rules: dict[str, int] | None = None,
) -> tuple[str, str]:
    """(accion, motivo) para un anuncio."""
    rules = {**RULES, **(rules or {})}
    status = listing.get("status")
    age = age_days(str(listing.get("listed_on") or ""), today)
    clicks = int(listing.get("clicks_14d") or 0)
    window = min(age if age is not None else 14, 14) or 1

    if status == "sold":
        return "none", "Vendido."
    if status == "pending":
        return "none", "Marcado como pendiente de entrega."
    if status == "out_of_stock":
        if has_stock and not active_same_family:
            if in_rotation:
                return "none", "Agotado aqui; el bot lo vuelve a publicar en su rotacion."
            price = listing.get("price")
            price_note = f" Se republica tal cual, a C${price:g}: si el precio o las fotos cambiaron, mejor publica uno nuevo." if price else ""
            return "relist", "Hay stock en el SGI y no tienes otro anuncio activo de este producto." + price_note
        if active_same_family:
            return "none", "Agotado; ya tienes otro anuncio activo de este producto."
        return "none", "Agotado."
    if status != "in_stock":
        return "none", "No pude leer el estado del anuncio."

    if has_stock is False:
        return "mark_out_of_stock", "El SGI ya no tiene stock de este producto."
    if newer_active_duplicate:
        return "delete", "Duplicado: tienes otro anuncio activo del mismo producto en esta cuenta con mas interes."
    if age is None:
        return "keep", f"{clicks} clics en los ultimos 14 dias."
    if age < rules["new_days"]:
        return "wait", f"Recien publicado ({age} dia{'s' if age != 1 else ''}); todavia no se puede juzgar."
    if age >= rules["dead_days"] and clicks < rules["low_clicks"]:
        return "delete", f"Lleva {age} dias y solo {clicks} clics en 14 dias. Ocupa espacio sin vender."
    if clicks >= rules["high_clicks"]:
        if age >= rules["renew_after_days"]:
            return "review", (
                f"Mucho interes ({clicks} clics) pero sigue sin venderse despues de {age} dias: "
                "revisa el precio y responde rapido los mensajes."
            )
        return "keep", f"Buen interes: {clicks} clics en {age} dias."
    if age >= rules["renew_after_days"] and clicks < rules["low_clicks"]:
        renewed_days = age_days(last_renew[:10], today) if last_renew else None
        if renewed_days is not None and renewed_days < rules["renew_cooldown_days"]:
            return "wait", f"Se renovo hace {renewed_days} dias; esperando el efecto."
        if renew_count >= rules["renews_before_edit"] and age >= rules["stale_days"]:
            return "edit", (
                f"Ya se renovo {renew_count} veces y sigue con {clicks} clics: "
                "cambia la foto principal, el titulo o el precio."
            )
        return "renew", f"Poco interes ({clicks} clics en {window} dias): renovarlo lo vuelve a subir en Marketplace."
    return "keep", f"{clicks} clics en {window} dias."


# --- base de datos -----------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    key TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    fb_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    family_key TEXT NOT NULL DEFAULT '',
    price REAL,
    status TEXT NOT NULL,
    listed_on TEXT NOT NULL DEFAULT '',
    clicks_14d INTEGER NOT NULL DEFAULT 0,
    in_groups INTEGER NOT NULL DEFAULT 0,
    can_relist INTEGER NOT NULL DEFAULT 0,
    by_bot INTEGER NOT NULL DEFAULT 0,
    recommendation TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_listings_account ON listings(account, present, status);

CREATE TABLE IF NOT EXISTS listing_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    clicks_14d INTEGER NOT NULL DEFAULT 0,
    price REAL
);
CREATE INDEX IF NOT EXISTS idx_listing_snapshots_key ON listing_snapshots(key, seen_at);

CREATE TABLE IF NOT EXISTS listing_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    account TEXT NOT NULL,
    action TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL DEFAULT 'pending',
    detail TEXT NOT NULL DEFAULT '',
    requested_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_listing_actions_status ON listing_actions(status, requested_at);
"""


def ensure_schema(store: MarketplaceStore) -> None:
    with store.connect() as connection:
        connection.executescript(SCHEMA)


def save_scan(store: MarketplaceStore, account: str, cards: list[dict[str, Any]], today: date | None = None) -> dict[str, int]:
    """Guarda lo leido en Facebook. Los anuncios que ya no aparecen quedan como ausentes."""
    ensure_schema(store)
    today = today or date.today()
    timestamp = now_iso()
    bot_titles = {plain(name) for name in _bot_titles(store, account)}
    seen: set[str] = set()
    with store.connect() as connection:
        for card in cards:
            key = listing_key(account, card["title"], card["listed_on"])
            if key in seen:
                continue
            seen.add(key)
            values = (
                account, card.get("fb_id") or "", card["title"], family_for_title(card["title"]),
                card.get("price"), card["status"], card["listed_on"], int(card.get("clicks_14d") or 0),
                int(bool(card.get("in_groups"))), int(bool(card.get("can_relist"))),
                int(plain(card["title"]) in bot_titles), timestamp,
            )
            connection.execute(
                """INSERT INTO listings(account, fb_id, title, family_key, price, status, listed_on, clicks_14d,
                       in_groups, can_relist, by_bot, last_seen, key, first_seen, present)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(key) DO UPDATE SET
                       fb_id=CASE WHEN excluded.fb_id <> '' THEN excluded.fb_id ELSE listings.fb_id END,
                       title=excluded.title, family_key=excluded.family_key, price=excluded.price,
                       status=excluded.status, clicks_14d=excluded.clicks_14d, in_groups=excluded.in_groups,
                       can_relist=excluded.can_relist, by_bot=MAX(listings.by_bot, excluded.by_bot),
                       last_seen=excluded.last_seen, present=1""",
                (*values, key, timestamp),
            )
            connection.execute(
                "INSERT INTO listing_snapshots(key, seen_at, status, clicks_14d, price) VALUES (?, ?, ?, ?, ?)",
                (key, timestamp, card["status"], int(card.get("clicks_14d") or 0), card.get("price")),
            )
        present_before = connection.execute(
            "SELECT COUNT(*) AS total FROM listings WHERE account=? AND present=1", (account,)
        ).fetchone()["total"]
        # Una lectura que trae muchos menos anuncios que la anterior suele ser una
        # carga incompleta de la pagina, no anuncios borrados.
        partial = present_before >= 5 and len(seen) < present_before * 0.6
        if seen and not partial:
            placeholders = ",".join("?" for _ in seen)
            connection.execute(
                f"UPDATE listings SET present=0 WHERE account=? AND key NOT IN ({placeholders})",
                [account, *seen],
            )
    return {"read": len(cards), "saved": len(seen)}


def _bot_titles(store: MarketplaceStore, account: str) -> list[str]:
    with store.connect() as connection:
        rows = connection.execute("SELECT name FROM publish_events WHERE account=?", (account,)).fetchall()
    return [str(row["name"]) for row in rows]


def list_listings(store: MarketplaceStore, account: str = "", present_only: bool = True) -> list[dict[str, Any]]:
    ensure_schema(store)
    clauses, params = [], []
    if account:
        clauses.append("account=?")
        params.append(account)
    if present_only:
        clauses.append("present=1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with store.connect() as connection:
        rows = connection.execute(f"SELECT * FROM listings {where} ORDER BY account, listed_on DESC", params).fetchall()
    return [dict(row) for row in rows]


def get_listing(store: MarketplaceStore, key: str) -> dict[str, Any] | None:
    ensure_schema(store)
    with store.connect() as connection:
        row = connection.execute("SELECT * FROM listings WHERE key=?", (key,)).fetchone()
    return dict(row) if row else None


def action_history(store: MarketplaceStore, key: str, action: str) -> tuple[str, int]:
    """(ultima vez hecha, cuantas veces) de una accion sobre un anuncio."""
    with store.connect() as connection:
        row = connection.execute(
            "SELECT MAX(finished_at) AS last, COUNT(*) AS total FROM listing_actions WHERE key=? AND action=? AND status='done'",
            (key, action),
        ).fetchone()
    return str(row["last"] or ""), int(row["total"] or 0)


def family_stock(report: dict[str, Any] | None) -> dict[str, bool] | None:
    """Que familias tienen stock segun el ultimo reporte del SGI. None si no hay SGI."""
    if not report:
        return None
    from marketplace_catalog import ASSISTANT_PRESETS

    stocked = {str(code).upper() for code in report.get("publishable") or []}
    stocked |= {str(entry.get("code", "")).upper() for entry in report.get("without_photos") or []}
    stocked |= {str(entry.get("code", "")).upper() for entry in report.get("price_issues") or []}
    result: dict[str, bool] = {}
    for family, preset in ASSISTANT_PRESETS.items():
        prefixes = tuple(str(prefix).upper() for prefix in preset.get("sku_prefixes") or [])
        result[family] = any(code.startswith(prefixes) for code in stocked) if prefixes else False
    return result


def interest(listing: dict[str, Any], today: date) -> tuple[float, str]:
    """Clics por dia en la ventana de 14 dias; a igualdad gana el mas reciente."""
    age = age_days(str(listing.get("listed_on") or ""), today)
    window = max(1, min(age if age is not None else 14, 14))
    return int(listing.get("clicks_14d") or 0) / window, str(listing.get("listed_on") or "")


def cover_report(store: MarketplaceStore, today: date | None = None) -> list[dict[str, Any]]:
    """Que foto de portada atrae mas, por producto.

    Cruza cada anuncio publicado por el bot (leido en "Tus anuncios") con la
    portada con la que se publico. Ojo: las cuentas no tienen el mismo alcance,
    asi que la comparacion es mas justa dentro de una misma cuenta.
    """
    today = today or date.today()
    ensure_schema(store)
    with store.connect() as connection:
        events = [dict(row) for row in connection.execute(
            "SELECT account, name, published_at, cover_path FROM publish_events WHERE source <> 'historial'"
        ).fetchall()]
        publications = [dict(row) for row in connection.execute(
            "SELECT account, published_at, payload_json FROM publications"
        ).fetchall()]
    by_title: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = (event["account"], plain(event["name"]))
        if key not in by_title or event["published_at"] > by_title[key]["published_at"]:
            by_title[key] = event

    def cover_for(event: dict[str, Any]) -> str:
        if event.get("cover_path"):
            return str(event["cover_path"])
        # Publicaciones anteriores a registrar la portada: se toma la primera foto
        # del trabajo que el trabajador marco publicado en el mismo momento.
        moment = str(event["published_at"])[:15]
        for publication in publications:
            if publication["account"] == event["account"] and str(publication["published_at"])[:15] == moment:
                try:
                    job = json.loads(publication["payload_json"])
                except ValueError:
                    continue
                paths = (job.get("listing_overrides") or {}).get("image_paths") or []
                if paths:
                    return str(paths[0])
        return ""

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for listing in list_listings(store, present_only=False):
        if not listing["by_bot"] or not listing["family_key"]:
            continue
        event = by_title.get((listing["account"], plain(listing["title"])))
        if not event:
            continue
        cover = cover_for(event)
        if not cover:
            continue
        label = "/".join(cover.replace("\\", "/").split("/")[-2:])
        group = groups.setdefault((listing["family_key"], label), {
            "family_key": listing["family_key"], "cover": label, "cover_path": cover,
            "listings": 0, "clicks": 0, "rate_sum": 0.0, "sold": 0, "accounts": set(),
        })
        group["listings"] += 1
        group["clicks"] += int(listing["clicks_14d"] or 0)
        group["rate_sum"] += interest(listing, today)[0]
        group["sold"] += int(listing["status"] == "sold")
        group["accounts"].add(listing["account"])
    result = []
    for group in groups.values():
        result.append({
            **{key: value for key, value in group.items() if key not in {"rate_sum", "accounts"}},
            "clicks_per_day": round(group["rate_sum"] / group["listings"], 2),
            "accounts": sorted(group["accounts"]),
        })
    result.sort(key=lambda item: (item["family_key"], -item["clicks_per_day"]))
    return result


def refresh_recommendations(
    store: MarketplaceStore,
    report: dict[str, Any] | None,
    rotation: Iterable[str] = (),
    today: date | None = None,
) -> dict[str, int]:
    """Recalcula la recomendacion de cada anuncio presente."""
    today = today or date.today()
    stock = family_stock(report)
    rotation = set(rotation)
    listings = list_listings(store)
    counts: dict[str, int] = {}
    by_account_family: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for listing in listings:
        if listing["status"] == "in_stock" and listing["family_key"]:
            by_account_family.setdefault((listing["account"], listing["family_key"]), []).append(listing)
    with store.connect() as connection:
        for listing in listings:
            family = listing["family_key"]
            siblings = by_account_family.get((listing["account"], family), []) if family else []
            # Entre duplicados se conserva el de mas interes por dia, no el mas
            # nuevo: borrar uno con 50 clics por ser viejo era un error. Un
            # anuncio recien publicado todavia no se compara.
            age = age_days(listing["listed_on"], today)
            newer = (
                listing["status"] == "in_stock"
                and age is not None
                and age >= RULES["new_days"]
                and any(
                    other["key"] != listing["key"] and interest(other, today) > interest(listing, today)
                    for other in siblings
                )
            )
            last_renew, renew_count = action_history(store, listing["key"], "renew")
            action, reason = recommend(
                listing,
                today,
                has_stock=(stock.get(family) if stock is not None and family else None),
                newer_active_duplicate=newer,
                in_rotation=family in rotation,
                active_same_family=bool(siblings),
                last_renew=last_renew,
                renew_count=renew_count,
            )
            listing["recommendation"], listing["reason"] = action, reason
        # Republicar varios anuncios viejos del mismo producto crea duplicados: se
        # recomienda solo el que tuvo mas interes (y a igualdad, el mas reciente).
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for listing in listings:
            if listing["recommendation"] != "relist" or not listing["family_key"]:
                continue
            group = (listing["account"], listing["family_key"])
            current = best.get(group)
            if current is None or (listing["clicks_14d"], listing["listed_on"]) > (current["clicks_14d"], current["listed_on"]):
                best[group] = listing
        for listing in listings:
            group = (listing["account"], listing["family_key"])
            if listing["recommendation"] == "relist" and group in best and best[group]["key"] != listing["key"]:
                listing["recommendation"] = "none"
                listing["reason"] = "Agotado; se recomienda republicar otro anuncio de este producto con mas interes."
            counts[listing["recommendation"]] = counts.get(listing["recommendation"], 0) + 1
            connection.execute(
                "UPDATE listings SET recommendation=?, reason=? WHERE key=?",
                (listing["recommendation"], listing["reason"], listing["key"]),
            )
    return counts


# --- acciones ----------------------------------------------------------------

def queue_action(store: MarketplaceStore, key: str, action: str, origin: str = "manual") -> int:
    ensure_schema(store)
    if action not in EXECUTABLE_ACTIONS:
        raise ValueError("Esa accion no la puede hacer el bot; hazla desde Facebook.")
    listing = get_listing(store, key)
    if not listing:
        raise ValueError("No encontre ese anuncio. Vuelve a revisar los anuncios.")
    with store.connect() as connection:
        existing = connection.execute(
            "SELECT id FROM listing_actions WHERE key=? AND status IN ('pending', 'running')", (key,)
        ).fetchone()
        if existing:
            return int(existing["id"])
        cursor = connection.execute(
            "INSERT INTO listing_actions(key, account, action, origin, requested_at) VALUES (?, ?, ?, ?, ?)",
            (key, listing["account"], action, origin, now_iso()),
        )
    return int(cursor.lastrowid)


def pending_actions(store: MarketplaceStore, account: str = "") -> list[dict[str, Any]]:
    ensure_schema(store)
    sql = "SELECT * FROM listing_actions WHERE status IN ('pending', 'running')"
    params: list[Any] = []
    if account:
        sql += " AND account=?"
        params.append(account)
    with store.connect() as connection:
        rows = connection.execute(sql + " ORDER BY requested_at", params).fetchall()
    return [dict(row) for row in rows]


def finish_action(store: MarketplaceStore, action_id: int, status: str, detail: str) -> None:
    with store.connect() as connection:
        connection.execute(
            "UPDATE listing_actions SET status=?, detail=?, finished_at=? WHERE id=?",
            (status, detail[:500], now_iso(), action_id),
        )


def set_action_running(store: MarketplaceStore, action_id: int) -> None:
    with store.connect() as connection:
        connection.execute("UPDATE listing_actions SET status='running' WHERE id=?", (action_id,))


def recent_actions(store: MarketplaceStore, limit: int = 30) -> list[dict[str, Any]]:
    ensure_schema(store)
    with store.connect() as connection:
        rows = connection.execute(
            """SELECT a.*, l.title FROM listing_actions a LEFT JOIN listings l ON l.key=a.key
               ORDER BY a.requested_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def renews_today(store: MarketplaceStore, account: str) -> int:
    today = date.today().isoformat()
    with store.connect() as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS total FROM listing_actions WHERE account=? AND action='renew' AND status='done' AND finished_at >= ?",
            (account, today),
        ).fetchone()
    return int(row["total"] or 0)


def action_allowed(store: MarketplaceStore, account: str, action: str) -> tuple[bool, str]:
    """Limites para las acciones sobre anuncios existentes."""
    if action == "relist":
        from marketplace_safety import check_can_publish

        ok, _, detail, _ = check_can_publish(store, account)
        return ok, detail
    if action == "renew" and renews_today(store, account) >= RULES["max_renews_per_day"]:
        return False, f"Ya se renovaron {RULES['max_renews_per_day']} anuncios hoy en esta cuenta."
    from marketplace_safety import active_pauses

    pause = active_pauses(store).get(account)
    if pause:
        return False, f"Cuenta en pausa: {pause.get('reason', '')}"
    return True, ""


# --- navegador ---------------------------------------------------------------

# Cada tarjeta se identifica por la linea "N clicks on listing"; se sube hasta el
# contenedor mas pequeno con imagen, precio y una sola linea de clics.
SCAN_JS = r"""
const CLICS = /\d[\d,.]*\s+(clicks?|clics?)\b/gi;
const own = el => [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join('');
const count = t => (String(t || '').match(CLICS) || []).length;
const leaves = [...document.querySelectorAll('div, span')].filter(el => /\d\s+(clicks?|clics?)\b/i.test(own(el)));
const out = [];
const seen = new Set();
for (const leaf of leaves) {
  let card = leaf;
  for (let i = 0; i < 15 && card; i++, card = card.parentElement) {
    if (card.querySelector('img') && count(card.innerText) === 1 && /(NIO|C\$)\s?\d/.test(card.innerText)) break;
  }
  if (!card || seen.has(card)) continue;
  seen.add(card);
  out.push({
    texto: card.innerText,
    hrefs: [...card.querySelectorAll('a[href]')].map(a => a.getAttribute('href')),
    botones: [...card.querySelectorAll('[role="button"]')].map(b => (b.getAttribute('aria-label') || b.innerText || '').trim()).filter(Boolean),
  });
}
return out;
"""

# Facebook dibuja dos botones con la misma etiqueta por anuncio y uno esta
# oculto; pulsar el oculto no abre nada (asi fallo "eliminar" el 4 de octubre).
FIND_BUTTON_JS = r"""
const wanted = arguments[0].toLowerCase();
const matches = [...document.querySelectorAll('[role="button"][aria-label]')]
  .filter(b => b.getAttribute('aria-label').trim().toLowerCase() === wanted);
const visible = b => {
  const r = b.getBoundingClientRect();
  const st = getComputedStyle(b);
  return r.width > 1 && r.height > 1 && st.visibility !== 'hidden' && st.display !== 'none';
};
const shown = matches.filter(visible);
const button = shown[0] || matches[0] || null;
if (button) button.scrollIntoView({block: 'center'});
return button;
"""

MENU_ITEM_JS = r"""
const starts = arguments[0].map(s => s.toLowerCase());
const menus = [...document.querySelectorAll('[role="menu"], [role="dialog"]')].filter(m => m.getBoundingClientRect().width > 50);
for (const menu of menus.reverse()) {
  for (const item of menu.querySelectorAll('[role="menuitem"], [role="button"]')) {
    const text = (item.innerText || item.getAttribute('aria-label') || '').trim().split('\n')[0];
    if (starts.some(s => text.toLowerCase().startsWith(s))) return {item, text};
  }
}
return null;
"""


def _driver(debugger_address: str):
    from selenium import webdriver
    from selenium.webdriver import ChromeOptions

    import facebook_marketplace_browser_automator as fb

    options = ChromeOptions()
    options.add_experimental_option("debuggerAddress", debugger_address)
    driver = webdriver.Chrome(options=options)
    fb.prepare_window(driver)
    return driver


def _open_selling(driver) -> list[dict[str, Any]]:
    import facebook_marketplace_browser_automator as fb

    driver.get(SELLING_URL)
    time.sleep(9)
    fb.check_account_restriction(driver)
    # Facebook carga los anuncios a medida que se baja. Una sola vuelta sin
    # anuncios nuevos no basta: con la ventana en segundo plano la carga tarda, y
    # cortar antes dejaba anuncios fuera y los marcaba como desaparecidos.
    previous, unchanged = -1, 0
    for _ in range(40):
        cards = driver.execute_script(SCAN_JS)
        if len(cards) == previous:
            unchanged += 1
            if unchanged >= 3:
                return cards
        else:
            unchanged = 0
        previous = len(cards)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(3.5)
    return driver.execute_script(SCAN_JS)


def scan_in_browser(debugger_address: str) -> list[dict[str, Any]]:
    driver = _driver(debugger_address)
    original = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        today = date.today()
        cards = [parse_card(raw, today) for raw in _open_selling(driver)]
        return [card for card in cards if card]
    finally:
        driver.close()
        driver.switch_to.window(original)


def _card_matches(raw: dict[str, Any], title: str, listed_on: str) -> bool:
    card = parse_card(raw, date.today())
    return bool(card) and plain(card["title"]) == plain(title) and (not listed_on or card["listed_on"] == listed_on)


def act_in_browser(debugger_address: str, title: str, listed_on: str, action: str) -> str:
    """Ejecuta una accion sobre un anuncio y comprueba el resultado."""
    import facebook_marketplace_browser_automator as fb
    from selenium.webdriver.common.keys import Keys

    driver = _driver(debugger_address)
    original = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        cards = _open_selling(driver)
        if not any(_card_matches(raw, title, listed_on) for raw in cards):
            raise RuntimeError("No encontre el anuncio en Tus anuncios; puede que ya no exista.")

        def current_state() -> list[dict[str, Any]]:
            """Vuelve a leer la pagina y devuelve las tarjetas con este titulo."""
            refreshed = _open_selling(driver)
            today = date.today()
            found = [parse_card(raw, today) for raw in refreshed]
            return [card for card in found if card and plain(card["title"]) == plain(title)]

        def press(label: str) -> None:
            button = driver.execute_script(FIND_BUTTON_JS, f"{label} {title}")
            if button is None:
                raise RuntimeError(f"Facebook no ofrece '{label}' para este anuncio ahora mismo.")
            fb.click_like_a_person(driver, button)
            time.sleep(3)

        def menu(options: list[str]) -> str:
            press("More options for")
            found = driver.execute_script(MENU_ITEM_JS, options)
            if not found:
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                raise RuntimeError(f"El menu del anuncio no tiene {' / '.join(options)}.")
            text = found["text"]
            if re.search(r"\(\s*\d+\s*(day|dia)", plain(text)):
                driver.switch_to.active_element.send_keys(Keys.ESCAPE)
                raise RuntimeError(f"Facebook todavia no deja hacerlo: {text}.")
            fb.click_like_a_person(driver, found["item"])
            time.sleep(3)
            return text

        today_text = date.today().isoformat()
        if action == "mark_out_of_stock":
            press("Mark out of stock")
        elif action == "mark_available":
            if driver.execute_script(FIND_BUTTON_JS, f"Mark as in stock {title}") is not None:
                press("Mark as in stock")
            else:
                menu(["Mark as available", "Marcar como disponible"])
        elif action == "relist":
            # "Relist this item" republica al instante, sin formulario ni Publish
            # (verificado el 6 de octubre).
            press("Relist this item")
            time.sleep(5)
        elif action == "renew":
            menu(["Renew", "Renovar"])
        elif action == "delete":
            menu(["Delete listing", "Eliminar publicacion", "Eliminar anuncio"])
            time.sleep(1.5)
            if not fb.click_button(driver, "Delete", timeout=10):
                raise RuntimeError("No aparecio la confirmacion para eliminar.")
        else:
            raise ValueError(f"Accion no soportada: {action}")
        time.sleep(4)
        fb.check_account_restriction(driver)

        # Se comprueba en la pagina que el cambio quedo hecho.
        state = current_state()
        if action == "delete":
            if any(card["listed_on"] == listed_on for card in state):
                raise RuntimeError("Facebook no elimino el anuncio: sigue en Tus anuncios.")
        elif action == "relist":
            if not any(card["listed_on"] == today_text and card["status"] == "in_stock" for card in state):
                raise RuntimeError("Despues de republicar no aparece el anuncio activo con fecha de hoy.")
        elif action == "mark_out_of_stock":
            if not any(card["status"] == "out_of_stock" for card in state):
                raise RuntimeError("El anuncio no quedo marcado como agotado.")
        elif action == "mark_available":
            if not any(card["status"] == "in_stock" for card in state):
                raise RuntimeError("El anuncio no quedo disponible.")
        return f"{ACTION_LABELS.get(action, action)}: hecho y comprobado en Facebook."
    finally:
        driver.close()
        driver.switch_to.window(original)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lee o actua sobre los anuncios ya publicados.")
    parser.add_argument("command", choices=["scan", "act"])
    parser.add_argument("--debugger-address", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--listed-on", default="")
    parser.add_argument("--action", default="")
    args = parser.parse_args()
    try:
        if args.command == "scan":
            print("[listings-json]" + json.dumps(scan_in_browser(args.debugger_address), ensure_ascii=False))
        else:
            print("[listings-done]" + act_in_browser(args.debugger_address, args.title, args.listed_on, args.action))
    except Exception as exc:  # noqa: BLE001 - el trabajador clasifica por el texto.
        import facebook_marketplace_browser_automator as fb

        print(f"[listings-error:{fb.error_code(exc)}] {exc}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()

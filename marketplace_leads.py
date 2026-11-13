"""Cuenta los interesados que escriben por Marketplace (pestana de ventas de la bandeja).

Solo lee la lista: no abre ninguna conversacion, porque abrirla la marcaria como
leida, y no escribe nada. De cada fila se guarda el anuncio, la fecha y si falta
responder. El nombre solo entra en la huella que distingue una conversacion de otra
y el texto de los mensajes no sale del proceso que lee la pagina.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import date, timedelta
from typing import Any

import marketplace_listings as listings
from marketplace_storage import MarketplaceStore, now_iso


INBOX_URL = "https://www.facebook.com/marketplace/inbox/"
MAX_SCROLLS = 25
LOOKBACK_DAYS = 45

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    account TEXT NOT NULL,
    thread TEXT NOT NULL,
    title TEXT NOT NULL,
    family_key TEXT NOT NULL DEFAULT '',
    started_on TEXT NOT NULL,
    last_activity TEXT NOT NULL,
    awaiting_reply INTEGER NOT NULL DEFAULT 0,
    never_answered INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    PRIMARY KEY (account, thread)
);
CREATE INDEX IF NOT EXISTS idx_leads_started ON leads(started_on);
"""

# La primera pestana de la bandeja es la de ventas; la segunda, la de compras.
SELECT_SELLING_JS = r"""
const main = document.querySelector('[role="main"]');
const tab = main && main.querySelector('[role="tab"]');
if (!tab) return false;
if (tab.getAttribute('aria-selected') !== 'true') tab.click();
return true;
"""

# Cada fila trae el nombre, "· titulo del anuncio", el ultimo mensaje y la hora, y
# ningun enlace ni atributo propio: se sube desde el punto hasta el contenedor mas
# grande que solo tenga esa fila.
READ_ROWS_JS = r"""
const main = document.querySelector('[role="main"]');
if (!main) return null;
const text = el => (el.textContent || '').trim();
// El punto va en su propio elemento, entre el texto del nombre y el del titulo.
const isMark = el => text(el) === '·' && el.nextSibling && el.nextSibling.nodeType === 3 && el.nextSibling.textContent.trim();
const marks = [...main.querySelectorAll('span')].filter(isMark);
const rows = [];
const seen = new Set();
for (const mark of marks) {
  let row = mark;
  while (row.parentElement && row.parentElement !== main
         && marks.filter(m => row.parentElement.contains(m)).length === 1) row = row.parentElement;
  if (!seen.has(row)) { seen.add(row); rows.push(row); }
}
if (arguments[0] && rows.length) rows[rows.length - 1].scrollIntoView({block: 'end'});
return rows.map(row => (row.innerText || '').split('\n').map(line => line.trim()).filter(Boolean));
"""

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "lun": 0, "mar": 1, "mie": 2, "jue": 3, "vie": 4, "sab": 5, "dom": 6,
}
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "ene": 1, "abr": 4, "ago": 8, "set": 9, "dic": 12,
}
AGE_DAYS = {"d": 1, "w": 7, "sem": 7, "y": 365, "a": 365}
OWN_MESSAGE_PREFIXES = ("you:", "you sent", "you replied", "tu:", "enviaste", "respondiste")
# Mientras nadie conteste, Facebook muestra este aviso en lugar del ultimo mensaje.
FIRST_CONTACT_PHRASES = ("sent you a message about your listing", "te envio un mensaje sobre tu")
# Los productos en los que se concentra la venta: el panel y el calendario los ponen primero.
FOCUS_FAMILIES = ("calcetas", "compression_short", "bershka")
RECENT_DAYS = 14  # la misma ventana de los clics de Facebook
ALERT_DAYS = 3  # un interesado sin respuesta se enfria rapido
# Menos de un lead por anuncio en dos semanas: el producto descansa del calendario.
WEAK_SCORE = 1.0


def ensure_schema(store: MarketplaceStore) -> None:
    with store.connect() as connection:
        connection.executescript(SCHEMA)


def parse_when(label: str, today: date) -> date | None:
    """La hora que muestra la bandeja: "7:08 AM", "Sat", "2w", "Oct 12", "Oct 12, 2025"."""
    value = listings.plain(label).rstrip(".")
    if re.fullmatch(r"\d{1,2}:\d{2}(\s*[ap]\.?\s*m\.?)?", value) or value in {"just now", "ahora"}:
        return today
    match = re.fullmatch(r"(\d+)\s*([a-z]+)", value)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        if unit in {"m", "min", "h", "hr", "hrs"}:
            return today
        return today - timedelta(days=amount * AGE_DAYS[unit]) if unit in AGE_DAYS else None
    if value[:3] in WEEKDAYS and re.fullmatch(r"[a-z]+", value):
        # Facebook usa el dia de la semana solo para los ultimos siete dias.
        return today - timedelta(days=(today.weekday() - WEEKDAYS[value[:3]]) % 7 or 7)
    match = re.fullmatch(r"([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s*(\d{4}))?", value)
    if match and match.group(1) in MONTHS:
        month, day = MONTHS[match.group(1)], int(match.group(2))
        try:
            when = date(int(match.group(3) or today.year), month, day)
        except ValueError:
            return None
        return when.replace(year=when.year - 1) if not match.group(3) and when > today else when
    return None


def parse_row(lines: list[str], today: date) -> dict[str, Any] | None:
    """Los datos de una fila, sin el nombre ni el mensaje."""
    if len(lines) < 3 or not lines[1].startswith("·"):
        return None
    when = parse_when(lines[-1], today)
    title = lines[1].lstrip("· ").strip()
    if when is None or not title:
        return None
    message = listings.plain(" ".join(lines[2:-1]))
    return {
        "thread": hashlib.sha256(f"{listings.plain(lines[0])}|{listings.plain(title)}".encode("utf-8")).hexdigest()[:24],
        "title": title,
        "last_activity": when.isoformat(),
        "awaiting_reply": not message.startswith(OWN_MESSAGE_PREFIXES),
        "never_answered": any(phrase in message for phrase in FIRST_CONTACT_PHRASES),
    }


def save_rows(store: MarketplaceStore, account: str, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Guarda lo leido. La conversacion se cuenta como nueva una sola vez."""
    ensure_schema(store)
    timestamp = now_iso()
    new = 0
    with store.connect() as connection:
        for row in rows:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO leads
                   (account, thread, title, family_key, started_on, last_activity, awaiting_reply, never_answered,
                    first_seen, last_seen)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (account, row["thread"], row["title"], listings.family_for_title(row["title"]), row["last_activity"],
                 row["last_activity"], int(row["awaiting_reply"]), int(row["never_answered"]), timestamp, timestamp),
            )
            if cursor.rowcount:
                new += 1
                continue
            connection.execute(
                """UPDATE leads SET last_activity=max(last_activity, ?), awaiting_reply=?, never_answered=?, last_seen=?
                   WHERE account=? AND thread=?""",
                (row["last_activity"], int(row["awaiting_reply"]), int(row["never_answered"]), timestamp, account, row["thread"]),
            )
    return {
        "read": len(rows),
        "new": new,
        "awaiting": sum(1 for row in rows if row["awaiting_reply"]),
        "never_answered": sum(1 for row in rows if row["never_answered"]),
    }


def report(store: MarketplaceStore, today: date | None = None) -> dict[str, Any]:
    """Lo que muestra el panel. Solo consulta la base, no toca Facebook.

    Los productos y los pendientes usan 14 dias, la misma ventana de los clics de Facebook.
    """
    today = today or date.today()
    ensure_schema(store)
    listings.ensure_schema(store)
    since = {days: (today - timedelta(days=days)).isoformat() for days in (7, 14, 30)}
    with store.connect() as connection:
        accounts = connection.execute(
            """SELECT account, COUNT(*) AS total, SUM(last_activity >= ?) AS last_7, SUM(last_activity >= ?) AS last_14,
                      SUM(last_activity >= ?) AS last_30, SUM(never_answered = 1 AND last_activity >= ?) AS unanswered
               FROM leads GROUP BY account ORDER BY account""",
            (since[7], since[14], since[30], since[14]),
        ).fetchall()
        by_family = connection.execute(
            "SELECT family_key, account, COUNT(*) AS leads FROM leads WHERE last_activity >= ? GROUP BY family_key, account",
            (since[14],),
        ).fetchall()
        clicks = connection.execute(
            """SELECT family_key, SUM(clicks_14d) AS clicks FROM listings
               WHERE present = 1 AND status = 'in_stock' AND family_key <> '' GROUP BY family_key"""
        ).fetchall()
        unanswered = connection.execute(
            """SELECT account, title, family_key, last_activity FROM leads
               WHERE never_answered = 1 AND last_activity >= ? ORDER BY last_activity DESC, account""",
            (since[14],),
        ).fetchall()

    leads_by_family: dict[str, dict[str, int]] = {}
    for row in by_family:
        leads_by_family.setdefault(row["family_key"], {})[row["account"]] = row["leads"]
    clicks_by_family = {row["family_key"]: row["clicks"] or 0 for row in clicks}
    products = []
    for family in {*FOCUS_FAMILIES, *leads_by_family, *clicks_by_family}:
        by_account = leads_by_family.get(family, {})
        total = sum(by_account.values())
        family_clicks = clicks_by_family.get(family, 0)
        products.append({
            "family_key": family,
            "focus": family in FOCUS_FAMILIES,
            "leads": total,
            "by_account": by_account,
            "clicks": family_clicks,
            # Los clics son de los anuncios activos y los leads tambien de los ya borrados:
            # con menos clics que leads el porcentaje no significaria nada.
            "rate": round(100 * total / family_clicks) if family_clicks >= max(total, 1) else None,
        })
    focus_rank = {family: rank for rank, family in enumerate(FOCUS_FAMILIES)}
    products.sort(key=lambda item: (focus_rank.get(item["family_key"], len(focus_rank)), -item["leads"], item["family_key"]))
    return {
        "accounts": [dict(row) for row in accounts],
        "products": products,
        "unanswered": [
            {**dict(row), "age_days": (today - date.fromisoformat(row["last_activity"])).days} for row in unanswered
        ],
    }


def listing_leads(store: MarketplaceStore, today: date | None = None) -> tuple[set[str], dict[tuple[str, str], int]]:
    """Las cuentas con la bandeja ya leida y los leads recientes de cada (cuenta, titulo normalizado)."""
    today = today or date.today()
    ensure_schema(store)
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    with store.connect() as connection:
        accounts = {row[0] for row in connection.execute("SELECT DISTINCT account FROM leads")}
        rows = connection.execute(
            "SELECT account, title, COUNT(*) FROM leads WHERE last_activity >= ? GROUP BY account, title", (since,)
        ).fetchall()
    counts: dict[tuple[str, str], int] = {}
    for account, title, total in rows:
        key = (account, listings.plain(title))
        counts[key] = counts.get(key, 0) + total
    return accounts, counts


def family_scores(store: MarketplaceStore, today: date | None = None) -> dict[str, float]:
    """Leads de las ultimas dos semanas por cada anuncio de ese producto, en las cuentas ya leidas.

    Cuentan los anuncios de mas de una semana vistos en esas dos semanas, tambien los
    que ya se borraron. Sin datos de un producto no hay puntaje.
    """
    today = today or date.today()
    ensure_schema(store)
    listings.ensure_schema(store)
    since = (today - timedelta(days=RECENT_DAYS)).isoformat()
    settled = (today - timedelta(days=listings.RULES["renew_after_days"])).isoformat()
    with store.connect() as connection:
        accounts = [row[0] for row in connection.execute("SELECT DISTINCT account FROM leads")]
        if not accounts:
            return {}
        marks = ",".join("?" * len(accounts))
        seen = dict(connection.execute(
            f"""SELECT family_key, COUNT(*) FROM listings
                WHERE family_key <> '' AND status IN ('in_stock', 'pending') AND last_seen >= ?
                  AND listed_on <> '' AND listed_on <= ? AND account IN ({marks})
                GROUP BY family_key""",
            (since, settled, *accounts),
        ).fetchall())
        found = dict(connection.execute(
            "SELECT family_key, COUNT(*) FROM leads WHERE family_key <> '' AND last_activity >= ? GROUP BY family_key",
            (since,),
        ).fetchall())
    return {family: found.get(family, 0) / max(1, seen.get(family, 0)) for family in {*seen, *found}}


def unanswered_recent(store: MarketplaceStore, account: str, today: date | None = None) -> int:
    """Primeros mensajes de los ultimos dias que nadie contesto en la cuenta."""
    today = today or date.today()
    ensure_schema(store)
    since = (today - timedelta(days=ALERT_DAYS)).isoformat()
    with store.connect() as connection:
        return int(connection.execute(
            "SELECT COUNT(*) FROM leads WHERE account = ? AND never_answered = 1 AND last_activity >= ?",
            (account, since),
        ).fetchone()[0])


def read_inbox(driver, today: date) -> list[list[str]]:
    driver.get(INBOX_URL)
    time.sleep(9)
    # No se revisa el texto de la pagina en busca de avisos, como en "Tus anuncios":
    # aqui estan los mensajes de los compradores, y cualquiera podria contener la frase.
    if "/checkpoint" in str(driver.current_url).lower():
        raise RuntimeError("Facebook pidio una verificacion de la cuenta (checkpoint).")
    if not driver.execute_script(SELECT_SELLING_JS):
        raise RuntimeError("No encontre la pestana de ventas de la bandeja.")
    time.sleep(3)
    rows: list[list[str]] = []
    previous, unchanged = -1, 0
    for _ in range(MAX_SCROLLS):
        rows = driver.execute_script(READ_ROWS_JS, True) or []
        oldest = parse_row(rows[-1], today) if rows else None
        if oldest and (today - date.fromisoformat(oldest["last_activity"])).days > LOOKBACK_DAYS:
            break
        unchanged = unchanged + 1 if len(rows) == previous else 0
        if unchanged >= 3:
            break
        previous = len(rows)
        time.sleep(3.5)
    return rows


def scan_in_browser(debugger_address: str) -> list[dict[str, Any]]:
    driver = listings._driver(debugger_address)
    original = driver.current_window_handle
    driver.switch_to.new_window("tab")
    try:
        today = date.today()
        parsed = [parse_row(lines, today) for lines in read_inbox(driver, today)]
        return [row for row in parsed if row]
    finally:
        driver.close()
        driver.switch_to.window(original)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cuenta los interesados de la bandeja de Marketplace.")
    parser.add_argument("command", choices=["scan"])
    parser.add_argument("--debugger-address", required=True)
    args = parser.parse_args()
    try:
        print("[leads-json]" + json.dumps(scan_in_browser(args.debugger_address), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - el trabajador clasifica por el texto.
        import facebook_marketplace_browser_automator as fb

        print(f"[listings-error:{fb.error_code(exc)}] {exc}", flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()

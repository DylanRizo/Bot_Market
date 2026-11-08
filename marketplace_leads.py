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

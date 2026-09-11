from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import uuid
from contextlib import contextmanager
from functools import lru_cache
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


SCRATCH_DIR = Path(__file__).resolve().parent


def default_db_path() -> Path:
    """Ruta de la base, resuelta en el momento de usarla.

    MARKETPLACE_BOT_DB permite apuntar a otra base sin tocar el codigo: lo usan
    las pruebas para no rozar la base real, y sirve para trabajar sobre una
    copia. Se lee en cada llamada a proposito: si se fijara al importar el
    modulo, quien lo importe antes de definir la variable se llevaria la ruta
    equivocada.
    """
    return Path(os.environ.get("MARKETPLACE_BOT_DB") or SCRATCH_DIR / "marketplace_bot.db")


DEFAULT_DB = default_db_path()
DEFAULT_ACTIVITY = SCRATCH_DIR / "marketplace_activity.json"
FINAL_STATUSES = {"published", "tested", "prepared", "skipped", "cancelled"}
RETRY_MAX_DELAY_MINUTES = 6 * 60
UNCERTAIN_PUBLISH_DETAIL = (
    "El trabajador se interrumpio despues de pulsar Publish. Revisa Marketplace: "
    "el anuncio puede haberse creado. No se reintenta solo para no duplicarlo."
)


@lru_cache(maxsize=8192)
def resolved_path(raw: str) -> Path:
    """Path.resolve() cacheado.

    Resolver una ruta en Windows cuesta varias llamadas al sistema, y el
    planificador resuelve las mismas fotos cientos de veces al armar la semana.
    Resolver es una operacion sobre el texto de la ruta, asi que cachearla es
    seguro: la existencia del archivo se sigue comprobando aparte.
    """
    return Path(raw).resolve()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def listing_fingerprint(job: dict[str, Any], account: str = "") -> str:
    override = job.get("listing_overrides") or {}
    stable = {
        "account": account or job.get("account") or "",
        "family_key": job.get("family_key") or "",
        "name": job.get("name") or "",
        "mode": job.get("listing_mode") or job.get("mode") or "",
        "group_by": job.get("group_by") or "",
        "skus": job.get("skus") or [],
        "sku_prefixes": job.get("sku_prefixes") or [],
        "title_contains": job.get("title_contains") or [],
        "row_index": job.get("row_index"),
        "price": override.get("price"),
        "images": override.get("image_paths") or [],
    }
    return hashlib.sha256(json_text(stable).encode("utf-8")).hexdigest()


class MarketplaceStore:
    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY,
                    week_key TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL,
                    account TEXT NOT NULL,
                    family_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'planned',
                    approved INTEGER NOT NULL DEFAULT 0,
                    priority INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 2,
                    next_attempt_at TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    listing_url TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    published_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_queue_due
                    ON queue(status, scheduled_at, next_attempt_at, priority);
                CREATE INDEX IF NOT EXISTS idx_queue_family
                    ON queue(family_key, status, published_at);

                CREATE TABLE IF NOT EXISTS attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    queue_id TEXT NOT NULL REFERENCES queue(id) ON DELETE CASCADE,
                    attempt_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    output TEXT NOT NULL DEFAULT '',
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS publications (
                    id TEXT PRIMARY KEY,
                    queue_id TEXT UNIQUE REFERENCES queue(id),
                    account TEXT NOT NULL,
                    family_key TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    listing_url TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_publications_family
                    ON publications(account, family_key, published_at);

                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    severity TEXT NOT NULL,
                    code TEXT NOT NULL,
                    message TEXT NOT NULL,
                    account TEXT NOT NULL DEFAULT '',
                    queue_id TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alerts_open ON alerts(status, severity, created_at);

                CREATE TABLE IF NOT EXISTS legacy_activity (
                    id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS worker_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS media_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account TEXT NOT NULL,
                    image_hash TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    queue_id TEXT REFERENCES queue(id),
                    family_key TEXT NOT NULL DEFAULT '',
                    used_at TEXT NOT NULL,
                    UNIQUE(account, image_hash)
                );

                CREATE INDEX IF NOT EXISTS idx_media_usage_account
                    ON media_usage(account, image_hash);

                CREATE TABLE IF NOT EXISTS custom_products (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    price REAL NOT NULL,
                    description TEXT NOT NULL,
                    category TEXT NOT NULL,
                    condition TEXT NOT NULL,
                    location TEXT NOT NULL,
                    sku TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL,
                    image_paths_json TEXT NOT NULL,
                    job_json TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    include_in_rotation INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO settings(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, json_text(value), now_iso()),
            )

    def get_setting(self, key: str, fallback: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        if not row:
            return fallback
        try:
            return json.loads(row["value_json"])
        except Exception:
            return fallback

    def migrate_activity(self, path: Path | str = DEFAULT_ACTIVITY) -> int:
        source = Path(path)
        if not source.exists():
            return 0
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            return 0
        imported = 0
        with self.connect() as connection:
            for item in payload.get("items", []):
                item_id = str(item.get("id") or uuid.uuid4())
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO legacy_activity(id, payload_json, imported_at) VALUES (?, ?, ?)",
                    (item_id, json_text(item), now_iso()),
                )
                imported += max(0, cursor.rowcount)
        return imported

    def enqueue(self, item: dict[str, Any]) -> tuple[str, bool]:
        queue_id = str(item.get("id") or uuid.uuid4())
        job = dict(item.get("job") or {})
        account = str(item.get("account") or job.get("account") or "")
        fingerprint = str(item.get("fingerprint") or listing_fingerprint(job, account))
        scheduled_at = str(item["scheduled_at"])
        dedupe_key = str(item.get("dedupe_key") or f"{scheduled_at}|{account}|{fingerprint}")
        timestamp = now_iso()
        values = (
            queue_id,
            str(item.get("week_key") or scheduled_at[:10]),
            scheduled_at,
            account,
            str(item.get("family_key") or job.get("family_key") or ""),
            str(item.get("name") or job.get("name") or "Publicacion"),
            json_text(job),
            fingerprint,
            dedupe_key,
            str(item.get("status") or "planned"),
            int(bool(item.get("approved", False))),
            int(item.get("priority") or 0),
            int(item.get("max_attempts") or 2),
            str(item.get("detail") or ""),
            timestamp,
            timestamp,
        )
        with self.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT id, status FROM queue WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing:
                if existing["status"] not in {"cancelled", "skipped"}:
                    return str(existing["id"]), False
                connection.execute(
                    """UPDATE queue SET week_key=?, scheduled_at=?, account=?, family_key=?, name=?,
                       job_json=?, fingerprint=?, status=?, approved=?, priority=?, attempts=0,
                       max_attempts=?, next_attempt_at=NULL, detail=?, listing_url='', started_at=NULL,
                       finished_at=NULL, published_at=NULL, updated_at=? WHERE id=?""",
                    (
                        values[1], values[2], values[3], values[4], values[5], values[6], values[7],
                        values[9], values[10], values[11], values[12], values[13], timestamp, existing["id"],
                    ),
                )
                return str(existing["id"]), True
            cursor = connection.execute(
                """INSERT INTO queue(
                       id, week_key, scheduled_at, account, family_key, name, job_json,
                       fingerprint, dedupe_key, status, approved, priority, max_attempts,
                       detail, created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                values,
            )
        return queue_id, cursor.rowcount > 0

    def list_queue(
        self,
        start: str | None = None,
        end: str | None = None,
        statuses: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("scheduled_at >= ?")
            params.append(start)
        if end:
            clauses.append("scheduled_at < ?")
            params.append(end)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 2000)))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM queue{where} ORDER BY scheduled_at, priority DESC LIMIT ?", params
            ).fetchall()
        return [self._queue_row(row) for row in rows]

    def get_queue_item(self, queue_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM queue WHERE id=?", (queue_id,)).fetchone()
        return self._queue_row(row) if row else None

    def update_queue_item(self, queue_id: str, **updates: Any) -> bool:
        allowed = {
            "scheduled_at", "status", "approved", "priority", "attempts", "next_attempt_at",
            "detail", "listing_url", "started_at", "finished_at", "published_at", "job_json",
            "name", "account", "family_key",
        }
        fields: list[str] = []
        params: list[Any] = []
        for key, value in updates.items():
            if key not in allowed:
                continue
            if key == "approved":
                value = int(bool(value))
            elif key == "job_json" and not isinstance(value, str):
                value = json_text(value)
            fields.append(f"{key}=?")
            params.append(value)
        if not fields:
            return False
        fields.append("updated_at=?")
        params.extend([now_iso(), queue_id])
        with self.connect() as connection:
            cursor = connection.execute(f"UPDATE queue SET {', '.join(fields)} WHERE id=?", params)
        return cursor.rowcount > 0

    def claim_due(self, now: str | None = None) -> dict[str, Any] | None:
        current = now or now_iso()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT * FROM queue
                   WHERE status IN ('queued', 'retry')
                     AND scheduled_at <= ?
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY priority DESC, scheduled_at ASC LIMIT 1""",
                (current, current),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE queue SET status='running', attempts=attempts+1,
                   started_at=?, updated_at=? WHERE id=? AND status IN ('queued', 'retry')""",
                (current, current, row["id"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute("SELECT * FROM queue WHERE id=?", (row["id"],)).fetchone()
        return self._queue_row(claimed)

    def begin_attempt(self, queue_id: str, attempt_number: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO attempts(queue_id, attempt_number, status, started_at) VALUES (?, ?, 'running', ?)",
                (queue_id, attempt_number, now_iso()),
            )
        return int(cursor.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        status: str,
        error_code: str = "",
        detail: str = "",
        output: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE attempts SET status=?, error_code=?, detail=?, output=?, finished_at=? WHERE id=?""",
                (status, error_code, detail[:1000], output[-20000:], now_iso(), attempt_id),
            )

    def mark_published(self, queue_id: str, listing_url: str = "") -> None:
        item = self.get_queue_item(queue_id)
        if not item:
            raise KeyError(queue_id)
        timestamp = now_iso()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE queue SET status='published', listing_url=?, published_at=?,
                   finished_at=?, updated_at=? WHERE id=?""",
                (listing_url, timestamp, timestamp, timestamp, queue_id),
            )
            connection.execute(
                """INSERT OR IGNORE INTO publications(
                       id, queue_id, account, family_key, name, fingerprint,
                       listing_url, published_at, payload_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()), queue_id, item["account"], item["family_key"], item["name"],
                    item["fingerprint"], listing_url, timestamp, json_text(item["job"]),
                ),
            )
        self.record_media_usage(queue_id)

    def image_hash(self, path: Path) -> str:
        """SHA-256 del contenido de una foto, con cache por instancia.

        El planificador pregunta por el mismo archivo miles de veces al armar una
        semana (espacios x familias x cuentas). La clave incluye fecha y tamano,
        asi que reemplazar la foto invalida la entrada sola.
        """
        path = Path(path)
        key: tuple[str, int, int] | None
        try:
            stats = path.stat()
            key = (str(path), stats.st_mtime_ns, stats.st_size)
        except OSError:
            key = None
        if key is not None:
            cached = self._hash_cache.get(key)
            if cached:
                return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        if key is not None:
            self._hash_cache[key] = value
        return value

    def _image_hash(self, path: Path) -> str:
        """Alias historico. Usar image_hash."""
        return self.image_hash(path)

    @staticmethod
    def job_image_paths(job: dict[str, Any]) -> list[Path]:
        raw = (job.get("listing_overrides") or {}).get("image_paths") or job.get("image_paths") or []
        paths: list[Path] = []
        for value in raw:
            path = resolved_path(str(value))
            if path.is_file() and path not in paths:
                paths.append(path)
        return paths

    def media_conflicts(self, account: str, image_paths: list[Path] | list[str]) -> list[dict[str, str]]:
        candidates: list[tuple[str, str]] = []
        for raw in image_paths:
            path = resolved_path(str(raw))
            if path.is_file():
                candidates.append((self.image_hash(path), str(path)))
        if not candidates:
            return []
        placeholders = ",".join("?" for _ in candidates)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT image_hash, image_path, used_at FROM media_usage WHERE account=? AND image_hash IN ({placeholders})",
                [account, *[item[0] for item in candidates]],
            ).fetchall()
        return [dict(row) for row in rows]

    def record_media_usage(self, queue_id: str) -> int:
        item = self.get_queue_item(queue_id)
        if not item:
            return 0
        paths = self.job_image_paths(item["job"])
        inserted = 0
        with self.connect() as connection:
            for path in paths:
                cursor = connection.execute(
                    """INSERT OR IGNORE INTO media_usage(
                           account, image_hash, image_path, queue_id, family_key, used_at
                       ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        item["account"], self._image_hash(path), str(path), queue_id,
                        item.get("family_key") or "", now_iso(),
                    ),
                )
                inserted += max(0, cursor.rowcount)
        return inserted

    def save_custom_product(self, product: dict[str, Any]) -> str:
        product_id = str(product.get("id") or uuid.uuid4())
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO custom_products(
                       id, title, price, description, category, condition, location, sku,
                       tags_json, image_paths_json, job_json, active, include_in_rotation,
                       created_at, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       title=excluded.title, price=excluded.price, description=excluded.description,
                       category=excluded.category, condition=excluded.condition, location=excluded.location,
                       sku=excluded.sku, tags_json=excluded.tags_json,
                       image_paths_json=excluded.image_paths_json, job_json=excluded.job_json,
                       active=excluded.active, include_in_rotation=excluded.include_in_rotation,
                       updated_at=excluded.updated_at""",
                (
                    product_id, str(product["title"]), float(product["price"]), str(product["description"]),
                    str(product["category"]), str(product["condition"]), str(product["location"]),
                    str(product.get("sku") or ""), json_text(product.get("tags") or []),
                    json_text(product.get("image_paths") or []), json_text(product.get("job") or {}),
                    int(bool(product.get("active", True))), int(bool(product.get("include_in_rotation", True))),
                    str(product.get("created_at") or timestamp), timestamp,
                ),
            )
        return product_id

    def list_custom_products(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = " WHERE active=1" if active_only else ""
        with self.connect() as connection:
            rows = connection.execute(f"SELECT * FROM custom_products{where} ORDER BY created_at DESC").fetchall()
        products: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for source, target in (("tags_json", "tags"), ("image_paths_json", "image_paths"), ("job_json", "job")):
                try:
                    item[target] = json.loads(item.pop(source))
                except Exception:
                    item[target] = [] if target != "job" else {}
            item["active"] = bool(item["active"])
            item["include_in_rotation"] = bool(item["include_in_rotation"])
            products.append(item)
        return products

    def set_custom_product_active(self, product_id: str, active: bool) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE custom_products SET active=?, updated_at=? WHERE id=?",
                (int(bool(active)), now_iso(), product_id),
            )
        return cursor.rowcount > 0

    @staticmethod
    def retry_delay_minutes(base_minutes: int, attempts: int, jitter: bool = True) -> int:
        """Espera creciente entre reintentos.

        Un fallo temporal de Facebook rara vez se resuelve en el mismo minuto, y
        golpear la cuenta cada media hora con el mismo error es justo lo que
        dispara una revision. Cada intento duplica la espera hasta un tope de seis
        horas, con una variacion de +-20% para no caer siempre en el mismo minuto.
        """
        base = max(1, int(base_minutes))
        exponent = max(0, int(attempts) - 1)
        delay = base * (2 ** min(exponent, 10))
        delay = min(delay, RETRY_MAX_DELAY_MINUTES)
        if jitter:
            delay = int(delay * random.uniform(0.8, 1.2))
        return max(1, min(delay, RETRY_MAX_DELAY_MINUTES))

    def schedule_retry(self, queue_id: str, delay_minutes: int, detail: str) -> None:
        item = self.get_queue_item(queue_id)
        if not item:
            return
        if item["attempts"] >= item["max_attempts"]:
            self.update_queue_item(queue_id, status="blocked", detail=detail, finished_at=now_iso())
            return
        wait = self.retry_delay_minutes(delay_minutes, item["attempts"])
        retry_at = (datetime.now() + timedelta(minutes=wait)).isoformat(timespec="seconds")
        self.update_queue_item(queue_id, status="retry", next_attempt_at=retry_at, detail=detail)

    def recover_stale(
        self,
        age_minutes: int = 30,
        uncertain_ids: set[str] | None = None,
    ) -> dict[str, list[str]]:
        """Rescata trabajos que quedaron 'running' tras una interrupcion.

        Un trabajo que alcanzo a pulsar Publish puede haber creado el anuncio de
        verdad, asi que reintentarlo duplicaria la publicacion. El trabajador pasa
        en `uncertain_ids` los que dejaron archivo de intencion; esos se bloquean
        para que una persona revise Marketplace antes de volver a intentarlo.
        """
        cutoff = (datetime.now() - timedelta(minutes=max(1, age_minutes))).isoformat(timespec="seconds")
        uncertain = {str(value) for value in (uncertain_ids or set())}
        timestamp = now_iso()
        recovered: dict[str, list[str]] = {"retry": [], "blocked": []}
        with self.transaction(immediate=True) as connection:
            stale = [
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM queue WHERE status='running' AND started_at < ?", (cutoff,)
                ).fetchall()
            ]
            for queue_id in stale:
                if queue_id in uncertain:
                    connection.execute(
                        """UPDATE queue SET status='blocked', detail=?, finished_at=?, updated_at=?
                           WHERE id=? AND status='running'""",
                        (UNCERTAIN_PUBLISH_DETAIL, timestamp, timestamp, queue_id),
                    )
                    recovered["blocked"].append(queue_id)
                else:
                    connection.execute(
                        """UPDATE queue SET status='retry', next_attempt_at=?, detail=?, updated_at=?
                           WHERE id=? AND status='running'""",
                        (
                            timestamp,
                            "Recuperado despues de una interrupcion del trabajador.",
                            timestamp,
                            queue_id,
                        ),
                    )
                    recovered["retry"].append(queue_id)
        return recovered

    def recent_family_publications(self, account: str = "") -> dict[str, str]:
        params: list[Any] = []
        where = ""
        if account:
            where = "WHERE account=?"
            params.append(account)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT family_key, MAX(published_at) AS last_at FROM publications {where} GROUP BY family_key",
                params,
            ).fetchall()
        return {str(row["family_key"]): str(row["last_at"]) for row in rows if row["family_key"]}

    def publication_exists(self, account: str, fingerprint: str, within_hours: int = 12) -> bool:
        cutoff = (datetime.now() - timedelta(hours=max(1, within_hours))).isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM publications WHERE account=? AND fingerprint=? AND published_at >= ? LIMIT 1",
                (account, fingerprint, cutoff),
            ).fetchone()
        return bool(row)

    def create_alert(
        self,
        severity: str,
        code: str,
        message: str,
        account: str = "",
        queue_id: str | None = None,
    ) -> str:
        alert_id = str(uuid.uuid4())
        timestamp = now_iso()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO alerts(id, severity, code, message, account, queue_id, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
                (alert_id, severity, code, message, account, queue_id, timestamp, timestamp),
            )
        return alert_id

    def list_alerts(self, status: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM alerts WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, max(1, min(limit, 500))),
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve_alert(self, alert_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alerts SET status='resolved', updated_at=? WHERE id=?", (now_iso(), alert_id)
            )
        return cursor.rowcount > 0

    def heartbeat(self, key: str, value: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO worker_state(key, value_json, updated_at) VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
                (key, json_text(value), now_iso()),
            )

    def worker_state(self, key: str = "scheduler") -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM worker_state WHERE key=?", (key,)).fetchone()
        if not row:
            return {"status": "not-started", "updated_at": ""}
        try:
            value = json.loads(row["value_json"])
        except Exception:
            value = {}
        value["updated_at"] = row["updated_at"]
        return value

    def summary(self) -> dict[str, Any]:
        today = datetime.now().date().isoformat()
        with self.connect() as connection:
            status_rows = connection.execute("SELECT status, COUNT(*) AS total FROM queue GROUP BY status").fetchall()
            today_published = connection.execute(
                "SELECT COUNT(*) AS total FROM publications WHERE published_at >= ?", (today,)
            ).fetchone()["total"]
            next_item = connection.execute(
                """SELECT id, scheduled_at, name, account, status FROM queue
                   WHERE status IN ('planned', 'queued', 'retry') ORDER BY scheduled_at LIMIT 1"""
            ).fetchone()
        return {
            "counts": {row["status"]: row["total"] for row in status_rows},
            "published_today": today_published,
            "next": dict(next_item) if next_item else None,
            "open_alerts": len(self.list_alerts()),
        }

    def report(self, days: int = 7) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        start = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        with self.connect() as connection:
            queue_rows = connection.execute(
                "SELECT status, COUNT(*) AS total FROM queue WHERE updated_at >= ? GROUP BY status", (start,)
            ).fetchall()
            family_rows = connection.execute(
                """SELECT family_key, COUNT(*) AS total, MAX(published_at) AS last_at
                   FROM publications WHERE published_at >= ? GROUP BY family_key ORDER BY total DESC""",
                (start,),
            ).fetchall()
            account_rows = connection.execute(
                """SELECT account, COUNT(*) AS total, MAX(published_at) AS last_at
                   FROM publications WHERE published_at >= ? GROUP BY account ORDER BY total DESC""",
                (start,),
            ).fetchall()
            error_rows = connection.execute(
                """SELECT error_code, COUNT(*) AS total FROM attempts
                   WHERE started_at >= ? AND error_code <> '' GROUP BY error_code ORDER BY total DESC""",
                (start,),
            ).fetchall()
        return {
            "days": days,
            "from": start,
            "statuses": {row["status"]: row["total"] for row in queue_rows},
            "families": [dict(row) for row in family_rows],
            "accounts": [dict(row) for row in account_rows],
            "errors": [dict(row) for row in error_rows],
        }

    @staticmethod
    def _queue_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["job"] = json.loads(item.pop("job_json"))
        except Exception:
            item["job"] = {}
            item.pop("job_json", None)
        item["approved"] = bool(item.get("approved"))
        return item


def initialize_default_store() -> MarketplaceStore:
    store = MarketplaceStore(DEFAULT_DB)
    store.migrate_activity(DEFAULT_ACTIVITY)
    return store


if __name__ == "__main__":
    active_store = initialize_default_store()
    print(json.dumps({"database": str(active_store.path), "summary": active_store.summary()}, ensure_ascii=False, indent=2))

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pandas as pd

from marketplace_catalog import ASSISTANT_PRESETS, STYLE_TO_MODE
from marketplace_custom_products import CUSTOM_ROOT, create_custom_product
from marketplace_runtime import process_exists
from marketplace_scheduler import (
    approve_items,
    cancel_future,
    catalog_families,
    generate_week,
    load_config as load_autonomy_config,
    save_config as save_autonomy_config,
)
from marketplace_storage import MarketplaceStore, default_db_path

from marketplace_ai_descriptions import (
    DEFAULT_MODEL,
    ai_status,
    generate_product_tags,
    generate_sales_description,
    local_product_tags,
    sanitize_tags,
    save_api_key,
)
from sgi_client import (
    integration_key_configured,
    load_settings as load_integration_settings,
    save_integration_key,
)


SCRATCH_DIR = Path(__file__).resolve().parent
ACCOUNTS_PATH = SCRATCH_DIR / "marketplace_accounts.json"
CAMPAIGN_PATH = SCRATCH_DIR / "marketplace_campaign_example.json"
ACTIVITY_PATH = SCRATCH_DIR / "marketplace_activity.json"
INVENTORY_PATH = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"
CAMPAIGN_UPLOADS_ROOT = SCRATCH_DIR / "marketplace_campaign_uploads"
RUNNER_PATH = SCRATCH_DIR / "marketplace_campaign_runner.py"
OPEN_SESSION_SCRIPT = SCRATCH_DIR / "abrir_sesion_marketplace_cuenta.ps1"
WORKER_PATH = SCRATCH_DIR / "marketplace_scheduler_worker.py"
WORKER_PID_PATH = SCRATCH_DIR / "marketplace_scheduler_worker.pid"
WORKER_LOG_PATH = SCRATCH_DIR / "marketplace_scheduler_worker.log"
PUBLISH_INTENTS_DIR = SCRATCH_DIR / "marketplace_publish_intents"
DASHBOARD_TOKEN_PATH = SCRATCH_DIR / "marketplace_dashboard_token.txt"
SESSION_COOKIE = "mb_session"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SESSION_TOKEN = ""
STORE = MarketplaceStore(default_db_path())
STORE.migrate_activity(ACTIVITY_PATH)

SGI_DRIVE_TOKEN_PATH = SCRATCH_DIR / "token_google_drive.json"
SGI_REPORT_PATH = SCRATCH_DIR / "sgi_sync_report.json"
_drive_authorization_lock = threading.Lock()
_drive_authorization: dict[str, Any] = {"error": "", "running": False}


def json_load(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def sgi_status() -> dict[str, Any]:
    """Estado de la conexion con el SGI y Drive. Nunca incluye la llave."""
    settings = load_integration_settings()
    report = json_load(SGI_REPORT_PATH, {})
    return {
        "base_url": str((settings.get("sgi") or {}).get("base_url") or ""),
        "drive_authorized": SGI_DRIVE_TOKEN_PATH.is_file(),
        "drive_authorizing": bool(_drive_authorization["running"]),
        "drive_error": str(_drive_authorization["error"]),
        "drive_folders": len((settings.get("drive") or {}).get("root_folder_ids") or []),
        "key_configured": integration_key_configured(),
        "report": {
            "catalog_items": report.get("catalog_items", 0),
            "generated_at": report.get("generated_at"),
            "local_photos": report.get("local_photos", []),
            "price_issues": report.get("price_issues", []),
            "publishable": report.get("publishable", []),
            "without_photos": report.get("without_photos", []),
        },
        "state": STORE.worker_state("sgi_sync"),
    }


def start_drive_authorization() -> None:
    """Abre el consentimiento de Google en el navegador sin bloquear el panel."""
    with _drive_authorization_lock:
        if _drive_authorization["running"]:
            return
        _drive_authorization.update(error="", running=True)

    def authorize() -> None:
        try:
            from drive_photos import DriveLibrary

            DriveLibrary.connect(interactive=True)
        except Exception as exc:
            _drive_authorization["error"] = str(exc)[:300]
        finally:
            _drive_authorization["running"] = False

    threading.Thread(target=authorize, daemon=True).start()


def json_save(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def expand_env(text: str) -> str:
    return os.path.expandvars(text or "")


def worker_pid() -> int:
    try:
        return int(WORKER_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def worker_running() -> bool:
    return process_exists(worker_pid())


def start_worker() -> int:
    current = worker_pid()
    if current and process_exists(current):
        return current
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_handle = WORKER_LOG_PATH.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(WORKER_PATH)],
        cwd=str(SCRATCH_DIR),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        creationflags=creation_flags,
    )
    log_handle.close()
    WORKER_PID_PATH.write_text(str(process.pid), encoding="utf-8")
    return process.pid


def stop_worker() -> bool:
    pid = worker_pid()
    if not pid or not process_exists(pid):
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
    try:
        WORKER_PID_PATH.unlink()
    except FileNotFoundError:
        pass
    return True


def autonomy_payload() -> dict[str, Any]:
    config = load_autonomy_config(STORE)
    families = catalog_families()
    for family in families:
        family["images"] = []
        for raw_path in family.get("image_paths") or []:
            try:
                family["images"].append(media_descriptor(Path(raw_path)))
            except (ValueError, FileNotFoundError):
                continue
    custom_products = STORE.list_custom_products()
    for product in custom_products:
        product["images"] = []
        for raw_path in product.get("image_paths") or []:
            try:
                product["images"].append(media_descriptor(Path(raw_path)))
            except (ValueError, FileNotFoundError):
                continue
    photo_assignments: dict[str, dict[str, list[dict[str, str]]]] = {}
    for account, assignments in (config.get("account_media") or {}).items():
        photo_assignments[account] = {}
        for family, paths in assignments.items():
            descriptors = []
            for raw_path in paths:
                try:
                    descriptors.append(media_descriptor(Path(raw_path)))
                except (ValueError, FileNotFoundError):
                    continue
            if descriptors:
                photo_assignments[account][family] = descriptors
    return {
        "config": config,
        "queue": STORE.list_queue(limit=500),
        "summary": STORE.summary(),
        "report": STORE.report(7),
        "alerts": STORE.list_alerts(),
        "worker": {**STORE.worker_state(), "process_running": worker_running(), "pid": worker_pid()},
        "families": families,
        "custom_products": custom_products,
        "photo_assignments": photo_assignments,
        "sgi": sgi_status(),
    }


def debug_status(address: str) -> dict[str, Any]:
    if not address:
        return {"ok": False, "message": "sin puerto"}
    url = f"http://{address}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
        return {"ok": True, "message": payload.get("Browser", "activo")}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def inventory_rows(limit: int = 500) -> list[dict[str, Any]]:
    if not INVENTORY_PATH.exists():
        return []
    df = pd.read_excel(INVENTORY_PATH)
    rows: list[dict[str, Any]] = []
    for index, row in df.head(limit).iterrows():
        rows.append(
            {
                "index": int(index),
                "sku": str(row.get("Sku", "") or ""),
                "title": str(row.get("Titulo", "") or ""),
                "price": row.get("Precio", ""),
                "category": str(row.get("Categoria", "") or ""),
                "condition": str(row.get("Condicion", "") or ""),
                "description": str(row.get("Descripcion", "") or ""),
                "location": str(row.get("Ubicacion", "") or ""),
                "imageFolder": str(row.get("CarpetaImg", "") or ""),
                "imageName": str(row.get("NombreImg", "") or ""),
            }
        )
    return rows


def split_semicolon(value: Any) -> list[str]:
    return [part.strip() for part in str(value or "").split(";") if part.strip()]


def resolve_inventory_images(row: dict[str, Any]) -> list[Path]:
    root = IMAGES_ROOT.resolve()
    folders = split_semicolon(row.get("imageFolder"))
    names = split_semicolon(row.get("imageName")) or ["foto_1"]
    images: list[Path] = []
    for index, name in enumerate(names):
        folder = folders[index] if index < len(folders) else (folders[0] if folders else "")
        candidate = (root / folder / name).resolve()
        candidates = [candidate]
        if candidate.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            candidates.extend(candidate.with_suffix(ext) for ext in (".jpg", ".jpeg", ".png", ".webp"))
        for item in candidates:
            try:
                item.relative_to(root)
            except ValueError:
                continue
            if item.is_file() and item not in images:
                images.append(item)
                break
    return images[:10]


def media_id_for_path(path: Path) -> str:
    path = path.resolve()
    for prefix, root in (
        ("original", IMAGES_ROOT.resolve()),
        ("upload", CAMPAIGN_UPLOADS_ROOT.resolve()),
        ("custom", CUSTOM_ROOT.resolve()),
    ):
        try:
            return f"{prefix}:{path.relative_to(root).as_posix()}"
        except ValueError:
            continue
    raise ValueError("La foto no pertenece al inventario ni a la campaña.")


def media_path_from_id(media_id: str) -> Path:
    prefix, separator, relative = str(media_id or "").partition(":")
    roots = {
        "original": IMAGES_ROOT.resolve(),
        "upload": CAMPAIGN_UPLOADS_ROOT.resolve(),
        "custom": CUSTOM_ROOT.resolve(),
    }
    root = roots.get(prefix)
    if not separator or not root or not relative:
        raise ValueError("Referencia de foto no valida.")
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("Ruta de foto no permitida.") from exc
    if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise FileNotFoundError("Foto no encontrada.")
    return path


def media_descriptor(path: Path) -> dict[str, str]:
    media_id = media_id_for_path(path)
    return {
        "id": media_id,
        "name": path.name,
        "url": "/api/media?" + urllib.parse.urlencode({"id": media_id}),
    }


def family_tag_facts(family_key: str, preset: dict[str, Any], matches: list[dict[str, Any]]) -> dict[str, Any]:
    titles = [str(row.get("title") or "") for row in matches]
    return {
        "titulo": preset["label"],
        "categoria": preset.get("category") or "",
        "variantes": titles[:8],
        "uso": "entrenamiento y uso diario",
        "familia": family_key,
    }


def family_review_data(families: list[str], persisted_overrides: dict[str, dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = inventory_rows()
    review: list[dict[str, Any]] = []
    for family_key in families:
        preset = ASSISTANT_PRESETS.get(family_key)
        if not preset:
            continue
        matches = [row for row in rows if preset_matches_row(preset, row)]
        prices = [float(row["price"]) for row in matches if isinstance(row.get("price"), (int, float))]
        seen: set[str] = set()
        images: list[dict[str, str]] = []
        for row in matches:
            for image_path in resolve_inventory_images(row):
                descriptor = media_descriptor(image_path)
                if descriptor["id"] not in seen:
                    images.append(descriptor)
                    seen.add(descriptor["id"])
        override = (persisted_overrides or {}).get(family_key) or {}
        override_images: list[dict[str, str]] = []
        for raw_path in override.get("image_paths", []):
            try:
                override_images.append(media_descriptor(Path(str(raw_path))))
            except (ValueError, FileNotFoundError):
                continue
        tags = sanitize_tags(override.get("tags"), local_product_tags(family_tag_facts(family_key, preset, matches)))
        review.append(
            {
                "key": family_key,
                "label": preset["label"],
                "price": override.get("price", min(prices) if prices else ""),
                "images": override_images or images[:10],
                "tags": tags,
            }
        )
    return review


def normalize_listing_overrides(payload: dict[str, Any], families: list[str]) -> dict[str, dict[str, Any]]:
    raw_overrides = payload.get("listing_overrides") or {}
    if not isinstance(raw_overrides, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for family_key in families:
        raw = raw_overrides.get(family_key)
        if not isinstance(raw, dict):
            continue
        override: dict[str, Any] = {}
        price = raw.get("price")
        if price not in (None, ""):
            try:
                price_number = float(price)
            except (TypeError, ValueError) as exc:
                raise ValueError("El precio debe ser un numero valido.") from exc
            if price_number <= 0:
                raise ValueError("El precio debe ser mayor que cero.")
            override["price"] = price_number
        media_ids = raw.get("image_ids") or []
        if media_ids:
            if not isinstance(media_ids, list):
                raise ValueError("Las fotos deben ser una lista.")
            override["image_paths"] = [str(media_path_from_id(media_id)) for media_id in media_ids[:10]]
        if "tags" in raw:
            raw_tags = raw.get("tags")
            if not isinstance(raw_tags, list):
                raise ValueError("Las etiquetas deben ser una lista.")
            tags = sanitize_tags(raw_tags)
            if not tags:
                raise ValueError("Agrega al menos una etiqueta valida.")
            override["tags"] = tags
        if override:
            normalized[family_key] = override
    return normalized


def preset_matches_row(preset: dict[str, Any], row: dict[str, Any]) -> bool:
    sku = row.get("sku", "").lower()
    title = row.get("title", "").lower()
    prefixes = [str(value).lower() for value in preset.get("sku_prefixes", [])]
    includes = [str(value).lower() for value in preset.get("title_contains", [])]
    excludes = [str(value).lower() for value in preset.get("exclude_title_contains", [])]
    if prefixes and not any(sku.startswith(prefix) for prefix in prefixes):
        return False
    if includes and not any(value in title for value in includes):
        return False
    if excludes and any(value in title for value in excludes):
        return False
    return bool(prefixes or includes)


def assistant_presets_with_inventory(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, preset in ASSISTANT_PRESETS.items():
        item = dict(preset)
        matches = [row for row in rows if preset_matches_row(preset, row)]
        item["variantCount"] = len(matches)
        prices = [row.get("price") for row in matches if isinstance(row.get("price"), (int, float))]
        if prices:
            item["priceFrom"] = min(prices)
        for row in matches:
            folder = row.get("imageFolder", "").split(";", 1)[0].strip()
            name = row.get("imageName", "").split(";", 1)[0].strip()
            if folder and name:
                item["imageUrl"] = "/api/image?" + urllib.parse.urlencode({"folder": folder, "name": name})
                break
        result[key] = item
    return result


def assistant_description_preview(payload: dict[str, Any]) -> dict[str, Any]:
    family_key = str(payload.get("family") or "")
    preset = ASSISTANT_PRESETS.get(family_key)
    if not preset:
        raise ValueError("Selecciona un producto para generar la vista previa.")
    matches = [row for row in inventory_rows() if preset_matches_row(preset, row)]
    if not matches:
        raise ValueError("No encontre variantes para este producto.")
    colors: list[str] = []
    sizes: list[str] = []
    size_values = {"XS", "S", "M", "L", "XL", "XXL", "UNI", "X"}
    for row in matches:
        title = row.get("title", "")
        variant = title.rsplit(" - ", 1)[-1].strip() if " - " in title else ""
        for part in [value.strip() for value in variant.split(",") if value.strip()]:
            if part.upper() in size_values:
                if part.upper() not in sizes:
                    sizes.append(part.upper())
            elif part not in colors:
                colors.append(part)
    prices = [row.get("price") for row in matches if isinstance(row.get("price"), (int, float))]
    price = min(prices) if prices else ""
    price_text = f"{price:g}" if isinstance(price, (int, float)) else str(price)
    location = matches[0].get("location") or "Managua, Nicaragua"
    fallback_lines = [f"{preset['label']} ideales para entrenar con comodidad y completar tu outfit deportivo."]
    if colors:
        fallback_lines.append(f"Colores disponibles: {', '.join(colors)}.")
    if sizes:
        fallback_lines.append(f"Tallas disponibles: {', '.join(sizes)}.")
    fallback_lines.extend(
        [
            f"Precio desde C${price_text}.",
            f"Ubicacion: {location}.",
            "Escribeme para confirmar la variante y coordinar la entrega.",
        ]
    )
    fallback = "\n".join(fallback_lines)
    description, source = generate_sales_description(
        {
            "titulo": preset["label"],
            "precio": f"C${price_text}",
            "colores": colors,
            "tallas": sizes,
            "ubicacion": location,
            "condicion": "Nuevo",
        },
        fallback,
        enabled=bool(payload.get("enabled", True)),
        model=str(payload.get("model") or DEFAULT_MODEL),
    )
    return {"description": description, "source": source}


def assistant_product_tags(payload: dict[str, Any]) -> dict[str, Any]:
    family_key = str(payload.get("family") or "")
    preset = ASSISTANT_PRESETS.get(family_key)
    if not preset:
        raise ValueError("Selecciona un producto para generar etiquetas.")
    matches = [row for row in inventory_rows() if preset_matches_row(preset, row)]
    if not matches:
        raise ValueError("No encontre variantes para este producto.")
    tags, source = generate_product_tags(
        family_tag_facts(family_key, preset, matches),
        enabled=bool(payload.get("enabled", True)),
        model=str(payload.get("model") or DEFAULT_MODEL),
    )
    return {"tags": tags, "source": source}


def build_assistant_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    accounts = json_load(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {})
    current_campaign = json_load(CAMPAIGN_PATH, {"jobs": [], "defaults": {}})
    account = payload.get("account") or current_campaign.get("default_account") or next(iter(accounts), "cuenta1")
    interval = int(payload.get("interval_minutes") or current_campaign.get("default_interval_minutes") or 60)
    start_delay = int(payload.get("start_delay_minutes") or 0)
    style = payload.get("style") or "grouped"
    mode, group_by = STYLE_TO_MODE.get(style, ("grouped", None))
    families = payload.get("families") or []
    if isinstance(families, str):
        families = [families]
    if not families:
        raise ValueError("Selecciona al menos una familia de productos.")
    listing_overrides = normalize_listing_overrides(payload, families)

    jobs: list[dict[str, Any]] = []
    for index, family_key in enumerate(families):
        preset = ASSISTANT_PRESETS.get(family_key)
        if not preset:
            raise ValueError(f"Familia no soportada: {family_key}")
        job: dict[str, Any] = {
            "name": f"{preset['label']} - {label_for_style(style)}",
            "family_key": family_key,
            "enabled": True,
            "delay_minutes": start_delay if index == 0 else interval,
            "item_interval_minutes": interval,
            "account": account,
            "listing_mode": mode,
            "source_excel": "ArticulosGenerados.xlsx",
            "images_root": "imagenes_firupost",
        }
        if group_by:
            job["group_by"] = group_by
        for key in ["sku_prefixes", "skus", "title_contains", "exclude_title_contains"]:
            if key in preset:
                job[key] = preset[key]
        if preset.get("category"):
            job["category"] = preset["category"]
        if listing_overrides.get(family_key):
            job["listing_overrides"] = listing_overrides[family_key]
        jobs.append(job)

    campaign = {
        "default_account": account,
        "default_interval_minutes": interval,
        "defaults": {
            "category": "Men's clothing & shoes",
            "condition": "New",
            "meet_public": True,
            "door_pickup": True,
            "door_dropoff": True,
            "skip_sku_field": False,
            "ai_descriptions": bool(payload.get("ai_descriptions", True)),
            "ai_model": str(payload.get("ai_model") or DEFAULT_MODEL),
            "ai_tone": "directo, cercano y profesional",
        },
        "jobs": jobs,
    }
    return campaign


def label_for_style(style: str) -> str:
    return {
        "grouped": "agrupado",
        "individual": "individual",
        "grouped_by_color": "agrupado por color",
        "grouped_by_size": "agrupado por talla",
    }.get(style, style)


class ProcessManager:
    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.logs: list[str] = []
        self.status = "idle"
        self.started_at = ""
        self.lock = threading.Lock()

    def add_log(self, line: str) -> None:
        with self.lock:
            self.logs.append(line.rstrip())
            self.logs = self.logs[-1000:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "status": "running" if running else self.status,
                "startedAt": self.started_at,
                "logs": list(self.logs),
                "running": running,
            }

    def start(self, command: list[str], cwd: Path) -> None:
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("Ya hay un proceso ejecutandose.")
            self.logs = []
            self.status = "running"
            self.started_at = now_text()
            self.logs.append(f"[dashboard {self.started_at}] $ {' '.join(command)}")
            self.process = subprocess.Popen(
                command,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            process = self.process

        def reader() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.add_log(line)
            code = process.wait()
            with self.lock:
                self.status = "complete" if code == 0 else f"failed ({code})"
                self.logs.append(f"[dashboard {now_text()}] proceso terminado: {self.status}")

        threading.Thread(target=reader, daemon=True).start()

    def stop(self) -> None:
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self.status = "idle"
                return
            self.process.terminate()
            self.status = "stopping"
            self.logs.append(f"[dashboard {now_text()}] solicitando detener proceso")


PROCESS_MANAGER = ProcessManager()


STATIC_DIR = SCRATCH_DIR / "static"
STATIC_FILES = {
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


def render_index() -> bytes:
    """Sirve la pagina con el token de la sesion inyectado.

    El HTML, el CSS y el JS viven en `static/`. Solo el token se sustituye al
    vuelo, porque es distinto en cada arranque y no puede quedar en un archivo.
    """
    plantilla = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return plantilla.replace("__MB_TOKEN__", SESSION_TOKEN).encode("utf-8")


def clear_publish_intent(queue_id: str) -> None:
    """Borra la marca de 'se pulso Publish' de un trabajo.

    Solo debe hacerlo una persona que ya reviso Marketplace, porque a partir de
    ahi el trabajador vuelve a tratar el trabajo como reintentable.
    """
    if not queue_id:
        return
    try:
        (PUBLISH_INTENTS_DIR / f"{queue_id}.json").unlink()
    except FileNotFoundError:
        pass


def issue_session_token() -> str:
    """Crea el token de esta sesion del panel y lo deja en un archivo local.

    El panel maneja el bot completo (arrancar el trabajador, publicar ahora,
    cambiar cuentas), asi que no puede quedar abierto a cualquiera que alcance el
    puerto. El token vive solo mientras corre el proceso.
    """
    global SESSION_TOKEN
    SESSION_TOKEN = secrets.token_urlsafe(32)
    DASHBOARD_TOKEN_PATH.write_text(SESSION_TOKEN, encoding="utf-8")
    return SESSION_TOKEN


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MarketplaceBotDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value.strip()
        return ""

    def query_token(self) -> str:
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        return query.get("token", [""])[0]

    def same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urllib.parse.urlparse(origin).netloc == (self.headers.get("Host") or "")

    def authorized(self, require_header: bool = False) -> bool:
        if not SESSION_TOKEN:
            return True
        presented = self.cookie_token() or self.query_token()
        if not secrets.compare_digest(presented, SESSION_TOKEN):
            return False
        if not require_header:
            return True
        # Una pagina de otro sitio puede provocar un POST, pero no puede poner una
        # cabecera propia ni falsear el Origin. Eso cierra el CSRF.
        if not secrets.compare_digest(self.headers.get("X-Marketplace-Token", ""), SESSION_TOKEN):
            return False
        return self.same_origin()

    def deny(self) -> None:
        self.send_json(
            {
                "ok": False,
                "error": (
                    "No autorizado. Abre el panel con el enlace que imprime "
                    "iniciar_panel_marketplace.ps1, que incluye el token de la sesion."
                ),
            },
            status=401,
        )

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        return json.loads(raw)

    def handle_error(self, exc: Exception) -> None:
        self.send_json({"ok": False, "error": str(exc)}, status=500)

    def do_GET(self) -> None:
        try:
            if not self.authorized():
                self.deny()
                return
            if self.path == "/" or self.path.startswith("/?"):
                data = render_index()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={SESSION_TOKEN}; HttpOnly; SameSite=Strict; Path=/",
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path in STATIC_FILES:
                # Lista fija de archivos: no se construye ninguna ruta con lo que
                # venga en la peticion, asi que no hay forma de salirse de static/.
                nombre, tipo = STATIC_FILES[self.path]
                data = (STATIC_DIR / nombre).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", tipo)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/api/image?"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                folder = query.get("folder", [""])[0]
                name = query.get("name", [""])[0]
                root = IMAGES_ROOT.resolve()
                image_path = (root / folder / name).resolve()
                try:
                    image_path.relative_to(root)
                except ValueError as exc:
                    raise RuntimeError("Ruta de imagen no permitida.") from exc
                if not image_path.is_file():
                    candidates = [image_path.with_suffix(suffix) for suffix in (".jpg", ".jpeg", ".png", ".webp")]
                    image_path = next((candidate for candidate in candidates if candidate.is_file()), image_path)
                    if not image_path.is_file():
                        self.send_json({"ok": False, "error": "Imagen no encontrada"}, status=404)
                        return
                data = image_path.read_bytes()
                content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/api/media?"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                image_path = media_path_from_id(query.get("id", [""])[0])
                data = image_path.read_bytes()
                content_type = mimetypes.guess_type(image_path.name)[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/api/assistant/review?"):
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                families = [key for key in query.get("families", [""])[0].split(",") if key]
                campaign = json_load(CAMPAIGN_PATH, {"jobs": []})
                persisted = {
                    str(job.get("family_key")): job.get("listing_overrides")
                    for job in campaign.get("jobs", [])
                    if job.get("family_key") and job.get("listing_overrides")
                }
                self.send_json({"ok": True, "families": family_review_data(families, persisted)})
                return
            if self.path == "/api/state":
                accounts = json_load(ACCOUNTS_PATH, {"accounts": {}})
                campaign = json_load(CAMPAIGN_PATH, {"jobs": [], "defaults": {}})
                inventory = inventory_rows()
                statuses = {
                    key: debug_status(account.get("debugger_address", ""))
                    for key, account in accounts.get("accounts", {}).items()
                }
                self.send_json(
                    {
                        "ok": True,
                        "accounts": accounts,
                        "campaign": campaign,
                        "inventory": inventory,
                        "statuses": statuses,
                        "assistantPresets": assistant_presets_with_inventory(inventory),
                        "ai": ai_status(),
                        "activity": json_load(ACTIVITY_PATH, {"items": []}),
                        "process": PROCESS_MANAGER.snapshot(),
                    }
                )
                return
            if self.path == "/api/activity":
                self.send_json({"ok": True, "activity": json_load(ACTIVITY_PATH, {"items": []})})
                return
            if self.path == "/api/autonomy":
                self.send_json({"ok": True, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/run/status":
                self.send_json({"ok": True, "process": PROCESS_MANAGER.snapshot()})
                return
            self.send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self) -> None:
        try:
            if not self.authorized(require_header=True):
                self.deny()
                return
            manejador = self.POST_ROUTES.get(self.path)
            if manejador is None:
                self.send_json({"ok": False, "error": "Not found"}, status=404)
                return
            manejador(self)
        except Exception as exc:
            self.handle_error(exc)

    def _post_assistant_upload_image(self) -> None:
        """POST /api/assistant/upload-image"""
        payload = self.read_json()
        filename = Path(str(payload.get("filename") or "foto.jpg")).name
        suffix = Path(filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise ValueError("Solo puedes agregar fotos JPG, PNG o WEBP.")
        encoded = str(payload.get("data") or "")
        if "," in encoded:
            encoded = encoded.split(",", 1)[1]
        try:
            content = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("No pude leer la foto seleccionada.") from exc
        if not content or len(content) > 8 * 1024 * 1024:
            raise ValueError("Cada foto debe pesar menos de 8 MB.")
        CAMPAIGN_UPLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).stem)[:80] or "foto"
        output = CAMPAIGN_UPLOADS_ROOT / f"{uuid.uuid4().hex}_{safe_name}{suffix}"
        output.write_bytes(content)
        self.send_json({"ok": True, "media": media_descriptor(output)})

    def _post_save(self) -> None:
        """POST /api/save"""
        payload = self.read_json()
        if "accounts" in payload:
            json_save(ACCOUNTS_PATH, payload["accounts"])
        if "campaign" in payload:
            json_save(CAMPAIGN_PATH, payload["campaign"])
        self.send_json({"ok": True})

    def _post_autonomy_config(self) -> None:
        """POST /api/autonomy/config"""
        payload = self.read_json()
        raw_config = dict(payload.get("config") or payload)
        raw_assignments = raw_config.pop("account_photo_assignments", None)
        if raw_assignments is not None:
            if not isinstance(raw_assignments, dict):
                raise ValueError("La configuración de fotos por cuenta no es válida.")
            account_media: dict[str, dict[str, list[str]]] = {}
            for account, families in raw_assignments.items():
                if not isinstance(families, dict):
                    continue
                account_media[str(account)] = {}
                for family, media_ids in families.items():
                    if not isinstance(media_ids, list):
                        continue
                    paths = [str(media_path_from_id(str(media_id))) for media_id in media_ids[:10]]
                    if paths:
                        account_media[str(account)][str(family)] = paths
            raw_config["account_media"] = account_media
        config = save_autonomy_config(STORE, raw_config)
        if config["enabled"]:
            if config["mode"] == "autonomous":
                approve_items(STORE)
            start_worker()
        self.send_json({"ok": True, "autonomy": autonomy_payload()})

    def _post_custom_product(self) -> None:
        """POST /api/custom-product"""
        payload = self.read_json()
        media_ids = payload.get("image_ids") or []
        if not isinstance(media_ids, list):
            raise ValueError("Las fotos deben ser una lista.")
        source_images = [media_path_from_id(str(media_id)) for media_id in media_ids[:10]]
        product = create_custom_product(STORE, payload, source_images)
        self.send_json({"ok": True, "product": product, "autonomy": autonomy_payload()})

    def _post_custom_product_active(self) -> None:
        """POST /api/custom-product/active"""
        payload = self.read_json()
        changed = STORE.set_custom_product_active(str(payload.get("id") or ""), bool(payload.get("active")))
        self.send_json({"ok": True, "changed": changed, "autonomy": autonomy_payload()})

    def _post_autonomy_generate(self) -> None:
        """POST /api/autonomy/generate"""
        payload = self.read_json()
        if payload.get("replace"):
            cancel_future(STORE)
        result = generate_week(STORE, load_autonomy_config(STORE))
        self.send_json({"ok": True, "result": result, "autonomy": autonomy_payload()})

    def _post_autonomy_approve(self) -> None:
        """POST /api/autonomy/approve"""
        payload = self.read_json()
        changed = approve_items(STORE, payload.get("ids"))
        self.send_json({"ok": True, "approved": changed, "autonomy": autonomy_payload()})

    def _post_autonomy_item(self) -> None:
        """POST /api/autonomy/item"""
        payload = self.read_json()
        queue_id = str(payload.get("id") or "")
        action = str(payload.get("action") or "")
        if action == "cancel":
            clear_publish_intent(queue_id)
            STORE.update_queue_item(queue_id, status="cancelled", detail="Cancelado desde el calendario.")
        elif action == "retry":
            # La persona ya reviso Marketplace: se descarta la intencion vieja
            # para que el trabajador no vuelva a marcar el resultado como incierto.
            clear_publish_intent(queue_id)
            STORE.update_queue_item(queue_id, status="queued", next_attempt_at=None, finished_at=None, detail="Reintento manual.")
        elif action == "move":
            scheduled_at = str(payload.get("scheduled_at") or "")
            try:
                scheduled = datetime.fromisoformat(scheduled_at)
            except ValueError as exc:
                raise ValueError("Selecciona una fecha y hora validas.") from exc
            if scheduled <= datetime.now():
                raise ValueError("La nueva hora debe estar en el futuro.")
            STORE.update_queue_item(queue_id, scheduled_at=scheduled.isoformat(timespec="seconds"))
        elif action == "approve":
            STORE.update_queue_item(queue_id, status="queued", approved=True)
        else:
            raise ValueError("Accion de calendario no soportada.")
        self.send_json({"ok": True, "autonomy": autonomy_payload()})

    def _post_autonomy_alert_resolve(self) -> None:
        """POST /api/autonomy/alert/resolve"""
        payload = self.read_json()
        STORE.resolve_alert(str(payload.get("id") or ""))
        self.send_json({"ok": True, "autonomy": autonomy_payload()})

    def _post_autonomy_worker_start(self) -> None:
        """POST /api/autonomy/worker/start"""
        pid = start_worker()
        self.send_json({"ok": True, "pid": pid, "autonomy": autonomy_payload()})

    def _post_autonomy_worker_stop(self) -> None:
        """POST /api/autonomy/worker/stop"""
        stopped = stop_worker()
        self.send_json({"ok": True, "stopped": stopped, "autonomy": autonomy_payload()})

    def _post_assistant_apply(self) -> None:
        """POST /api/assistant/apply"""
        payload = self.read_json()
        campaign = build_assistant_campaign(payload)
        json_save(CAMPAIGN_PATH, campaign)
        self.send_json({"ok": True, "campaign": campaign})

    def _post_assistant_description_preview(self) -> None:
        """POST /api/assistant/description-preview"""
        payload = self.read_json()
        self.send_json({"ok": True, **assistant_description_preview(payload)})

    def _post_assistant_product_tags(self) -> None:
        """POST /api/assistant/product-tags"""
        payload = self.read_json()
        self.send_json({"ok": True, **assistant_product_tags(payload)})

    def _post_ai_configure(self) -> None:
        """POST /api/ai/configure"""
        payload = self.read_json()
        save_api_key(str(payload.get("api_key") or ""))
        self.send_json({"ok": True, "ai": ai_status()})

    def _post_activity_clear(self) -> None:
        """POST /api/activity/clear"""
        json_save(ACTIVITY_PATH, {"items": []})
        self.send_json({"ok": True})

    def _post_preview_job(self) -> None:
        """POST /api/preview-job"""
        payload = self.read_json()
        campaign = payload.get("campaign", {})
        job = payload.get("job", {})
        temp = dict(campaign)
        temp["jobs"] = [job]
        temp_path = SCRATCH_DIR / ".dashboard_preview_campaign.json"
        json_save(temp_path, temp)
        command = [
            sys.executable,
            str(RUNNER_PATH),
            "--campaign",
            str(temp_path),
            "--plan-only",
            "--fast",
            "--activity-file",
            str(SCRATCH_DIR / ".dashboard_preview_activity.json"),
        ]
        completed = subprocess.run(command, cwd=str(SCRATCH_DIR), text=True, capture_output=True, timeout=120)
        output = (completed.stdout or "") + (completed.stderr or "")
        self.send_json({"ok": completed.returncode == 0, "output": output, "error": output if completed.returncode else ""})

    def _post_open_session(self) -> None:
        """POST /api/open-session"""
        payload = self.read_json()
        account_key = payload.get("account")
        accounts = json_load(ACCOUNTS_PATH, {"accounts": {}}).get("accounts", {})
        account = accounts.get(account_key)
        if not account:
            raise RuntimeError(f"No existe cuenta {account_key}")
        address = account.get("debugger_address", "127.0.0.1:9222")
        port = address.rsplit(":", 1)[-1]
        profile = expand_env(account.get("chrome_profile", ""))
        command = [
            "powershell",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(OPEN_SESSION_SCRIPT),
            "-Account",
            str(account_key),
            "-Port",
            str(port),
        ]
        if profile:
            command.extend(["-ProfileDir", profile])
        subprocess.Popen(command, cwd=str(SCRATCH_DIR))
        self.send_json({"ok": True})

    def _post_run_start(self) -> None:
        """POST /api/run/start"""
        payload = self.read_json()
        mode = payload.get("mode", "plan")
        command = [
            sys.executable,
            str(RUNNER_PATH),
            "--campaign",
            str(CAMPAIGN_PATH),
            "--activity-file",
            str(ACTIVITY_PATH),
        ]
        if payload.get("fast"):
            command.append("--fast")
        max_jobs = int(payload.get("maxJobs") or 0)
        if max_jobs:
            command.extend(["--max-jobs", str(max_jobs)])
        if mode == "plan":
            command.append("--plan-only")
        elif mode == "dry":
            pass
        elif mode == "advance":
            command.append("--advance-only")
        elif mode == "publish":
            command.append("--confirm-publish")
        else:
            raise RuntimeError(f"Modo no soportado: {mode}")
        PROCESS_MANAGER.start(command, SCRATCH_DIR)
        self.send_json({"ok": True, "process": PROCESS_MANAGER.snapshot()})

    def _post_run_stop(self) -> None:
        """POST /api/run/stop"""
        PROCESS_MANAGER.stop()
        activity = json_load(ACTIVITY_PATH, {"items": []})
        for item in reversed(activity.get("items", [])):
            if item.get("status") == "running":
                item["status"] = "stopped"
                item["detail"] = "Proceso detenido desde el panel."
                item["updated_at"] = datetime.now().isoformat(timespec="seconds")
                break
        json_save(ACTIVITY_PATH, activity)
        self.send_json({"ok": True, "process": PROCESS_MANAGER.snapshot()})

    def _post_sgi_key(self) -> None:
        """POST /api/sgi/key"""
        payload = self.read_json()
        save_integration_key(str(payload.get("key") or ""))
        # La llave no vuelve al navegador: solo se confirma que quedo guardada.
        self.send_json({"ok": True, "sgi": sgi_status()})

    def _post_sgi_sync(self) -> None:
        """POST /api/sgi/sync"""
        from marketplace_scheduler_worker import refresh_from_sgi, sgi_failure_code

        try:
            refresh_from_sgi(STORE)
        except Exception as exc:
            self.send_json(
                {"code": sgi_failure_code(exc), "error": str(exc)[:500], "ok": False, "sgi": sgi_status()},
                status=502,
            )
            return
        self.send_json({"autonomy": autonomy_payload(), "ok": True, "sgi": sgi_status()})

    def _post_sgi_drive_authorize(self) -> None:
        """POST /api/sgi/drive/authorize"""
        start_drive_authorization()
        self.send_json(
            {"message": "Se abrió el navegador para autorizar Google Drive en solo lectura.", "ok": True}
        )

    # Mapa de rutas POST. Tenerlas en una tabla en vez de una cadena de ifs
    # permite enumerarlas: las pruebas comprueban que el frontend no llame a
    # ninguna ruta que no exista.
    POST_ROUTES = {
        "/api/assistant/upload-image": _post_assistant_upload_image,
        "/api/save": _post_save,
        "/api/autonomy/config": _post_autonomy_config,
        "/api/custom-product": _post_custom_product,
        "/api/custom-product/active": _post_custom_product_active,
        "/api/autonomy/generate": _post_autonomy_generate,
        "/api/autonomy/approve": _post_autonomy_approve,
        "/api/autonomy/item": _post_autonomy_item,
        "/api/autonomy/alert/resolve": _post_autonomy_alert_resolve,
        "/api/autonomy/worker/start": _post_autonomy_worker_start,
        "/api/autonomy/worker/stop": _post_autonomy_worker_stop,
        "/api/assistant/apply": _post_assistant_apply,
        "/api/assistant/description-preview": _post_assistant_description_preview,
        "/api/assistant/product-tags": _post_assistant_product_tags,
        "/api/ai/configure": _post_ai_configure,
        "/api/activity/clear": _post_activity_clear,
        "/api/preview-job": _post_preview_job,
        "/api/open-session": _post_open_session,
        "/api/run/start": _post_run_start,
        "/api/run/stop": _post_run_stop,
        "/api/sgi/key": _post_sgi_key,
        "/api/sgi/sync": _post_sgi_sync,
        "/api/sgi/drive/authorize": _post_sgi_drive_authorize,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz grafica local para administrar el bot de Marketplace.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permite escuchar fuera de esta computadora. El panel controla el bot: usalo solo si sabes por que.",
    )
    args = parser.parse_args()

    if args.host not in LOOPBACK_HOSTS and not args.allow_remote:
        raise SystemExit(
            f"El panel solo escucha en esta computadora ({', '.join(sorted(LOOPBACK_HOSTS))}).\n"
            f"Pediste --host {args.host}, que lo expone a la red y deja el bot al alcance de otros.\n"
            "Si de verdad lo necesitas, agrega --allow-remote."
        )

    token = issue_session_token()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/?token={token}"
    print(f"Marketplace Bot Dashboard: {url}", flush=True)
    print(f"Token de la sesion guardado en: {DASHBOARD_TOKEN_PATH}", flush=True)
    if not args.no_open:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

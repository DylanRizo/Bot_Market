from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
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
from marketplace_storage import MarketplaceStore

from marketplace_ai_descriptions import (
    DEFAULT_MODEL,
    ai_status,
    generate_product_tags,
    generate_sales_description,
    local_product_tags,
    sanitize_tags,
    save_api_key,
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
STORE = MarketplaceStore(SCRATCH_DIR / "marketplace_bot.db")
STORE.migrate_activity(ACTIVITY_PATH)

_LEGACY_ASSISTANT_PRESETS: dict[str, dict[str, Any]] = {
    "compression_short": {
        "label": "Camisas manga corta de compresion",
        "description": "Manga corta",
        "sku_prefixes": ["TS"],
        "exclude_title_contains": ["sin mangas"],
        "category": "Men's clothing & shoes",
    },
    "compression_sleeveless": {
        "label": "Camisas sin mangas de compresion",
        "description": "Sin mangas",
        "title_contains": ["sin mangas"],
        "category": "Men's clothing & shoes",
    },
    "leggins": {
        "label": "Leggins de campana",
        "description": "Varios colores y tallas",
        "title_contains": ["leggins"],
        "category": "Men's clothing & shoes",
    },
    "enterizos": {
        "label": "Enterizos de entrenamiento",
        "description": "Varios colores y tallas",
        "title_contains": ["enterizo"],
        "category": "Men's clothing & shoes",
    },
    "shorts": {
        "label": "Shorts deportivos",
        "description": "Varios colores y tallas",
        "title_contains": ["shorts"],
        "category": "Men's clothing & shoes",
    },
    "bolsos": {
        "label": "Bolsos deportivos",
        "description": "Bolsos Nike",
        "title_contains": ["bolsos"],
        "category": "Sports & Outdoors",
    },
    "durags": {
        "label": "Durags",
        "description": "Varios colores",
        "title_contains": ["durags"],
        "category": "Men's clothing & shoes",
    },
    "munequeras": {
        "label": "Muñequeras deportivas",
        "description": "Rojo, azul y gris",
        "title_contains": ["muñequeras"],
        "category": "Sports & Outdoors",
    },
    "straps": {
        "label": "Straps para gimnasio",
        "description": "Negro y verde",
        "title_contains": ["straps"],
        "category": "Sports & Outdoors",
    },
}

_LEGACY_STYLE_TO_MODE: dict[str, tuple[str, str | None]] = {
    "grouped": ("grouped", None),
    "individual": ("individual", None),
    "grouped_by_color": ("grouped", "color"),
    "grouped_by_size": ("grouped", "size"),
}


def json_load(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


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


HTML = r"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Marketplace Bot Dashboard</title>
  <style>
    :root {
      --bg: #f3f5f7;
      --panel: #ffffff;
      --text: #18212b;
      --muted: #65717f;
      --line: #dce1e7;
      --blue: #1769e0;
      --blue-dark: #0d4fad;
      --green: #12804a;
      --green-soft: #e9f7ef;
      --red: #bd3c33;
      --yellow: #a05c00;
      --shadow: 0 1px 2px rgba(24,33,43,.07);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Segoe UI, system-ui, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      height: 60px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 3;
    }
    h1 { font-size: 18px; margin: 0; }
    .brand { display: flex; align-items: center; gap: 10px; }
    .brand-mark {
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border-radius: 7px;
      background: #1877f2;
      color: #fff;
      font-weight: 800;
    }
    h2 { font-size: 15px; margin: 0 0 10px; }
    button, input, select, textarea {
      font: inherit;
    }
    button {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      min-height: 36px;
      padding: 0 12px;
      border-radius: 6px;
      cursor: pointer;
    }
    button.primary { background: var(--blue); border-color: var(--blue); color: #fff; }
    button.primary:hover { background: var(--blue-dark); }
    button.danger { color: var(--red); border-color: #efb7b0; }
    button:disabled { opacity: .55; cursor: default; }
    .layout {
      display: grid;
      grid-template-columns: 232px minmax(0, 1fr);
      min-height: calc(100vh - 60px);
    }
    nav {
      border-right: 1px solid var(--line);
      background: #fff;
      padding: 18px 12px;
    }
    nav button {
      width: 100%;
      display: flex;
      justify-content: flex-start;
      align-items: center;
      gap: 8px;
      border: 0;
      background: transparent;
      margin-bottom: 5px;
      min-height: 40px;
      font-weight: 500;
    }
    nav button.active { background: #eaf2fd; color: var(--blue-dark); }
    main { padding: 24px 28px 40px; overflow: auto; }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 14px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      box-shadow: var(--shadow);
    }
    .span-4 { grid-column: span 4; }
    .span-5 { grid-column: span 5; }
    .span-6 { grid-column: span 6; }
    .span-7 { grid-column: span 7; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
    input, select, textarea {
      width: 100%;
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fff;
      color: var(--text);
    }
    textarea { min-height: 90px; resize: vertical; }
    .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 10px; align-items: end; }
    .col-2 { grid-column: span 2; }
    .col-3 { grid-column: span 3; }
    .col-4 { grid-column: span 4; }
    .col-5 { grid-column: span 5; }
    .col-6 { grid-column: span 6; }
    .col-7 { grid-column: span 7; }
    .col-8 { grid-column: span 8; }
    .col-12 { grid-column: span 12; }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .muted { color: var(--muted); }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      background: #fff;
      white-space: nowrap;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
    .ok .dot { background: var(--green); }
    .bad .dot { background: var(--red); }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid var(--line); padding: 8px; text-align: left; vertical-align: top; font-size: 13px; }
    th { color: var(--muted); font-weight: 600; background: #fafbfc; position: sticky; top: 0; }
    .table-wrap { max-height: 500px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
    .jobs { display: flex; flex-direction: column; gap: 10px; }
    .job {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fff;
    }
    .family-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(270px, 1fr));
      gap: 12px;
    }
    .family {
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr) 22px;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
      cursor: pointer;
      min-height: 94px;
      transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
    }
    .family:hover { border-color: #9cb8dc; box-shadow: 0 2px 8px rgba(24,33,43,.08); }
    .family input { width: 20px; min-height: 20px; margin: 0; }
    .family strong { display: block; margin-bottom: 4px; }
    .family.selected { border-color: var(--blue); background: #f3f7fd; box-shadow: 0 0 0 1px var(--blue); }
    .family-thumb {
      width: 72px;
      height: 72px;
      object-fit: contain;
      border-radius: 6px;
      background: #fff;
    }
    .family-meta { display: flex; gap: 7px; flex-wrap: wrap; margin-top: 7px; }
    .family-meta span {
      color: var(--muted);
      background: #eef1f4;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
    }
    .summary {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: #fafbfc;
      min-height: 74px;
    }
    .job.disabled { opacity: .62; }
    .logs {
      background: #111827;
      color: #e5e7eb;
      min-height: 260px;
      max-height: 420px;
      overflow: auto;
      border-radius: 8px;
      padding: 12px;
      white-space: pre-wrap;
      font-family: Consolas, monospace;
      font-size: 12px;
    }
    .hidden { display: none !important; }
    .assistant-shell { max-width: 1180px; margin: 0 auto; }
    .assistant-head {
      display: flex;
      justify-content: space-between;
      gap: 20px;
      align-items: flex-start;
      margin-bottom: 22px;
    }
    .assistant-head h2 { font-size: 25px; margin: 0 0 7px; }
    .assistant-head p { margin: 0; }
    .assistant-destination { display: flex; align-items: flex-end; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    .assistant-account-picker { min-width: 210px; }
    .assistant-account-picker label { font-size: 11px; margin-bottom: 4px; }
    .account-status {
      display: flex;
      align-items: center;
      gap: 9px;
      flex-wrap: wrap;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 9px 11px;
      min-width: 205px;
    }
    .account-status .dot { flex: 0 0 auto; }
    .account-status button { margin-left: auto; min-height: 30px; }
    .account-status strong { display: block; font-size: 13px; }
    .account-status span:last-child { display: block; font-size: 11px; color: var(--muted); margin-top: 2px; }
    .assistant-section {
      border-top: 1px solid var(--line);
      padding: 22px 0;
    }
    .section-heading {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-end;
      margin-bottom: 13px;
    }
    .section-heading h3 { font-size: 16px; margin: 2px 0 0; }
    .step-label {
      color: var(--blue-dark);
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
    }
    .selection-badge {
      color: var(--muted);
      font-size: 12px;
      border: 1px solid var(--line);
      background: #fff;
      border-radius: 999px;
      padding: 4px 9px;
    }
    .review-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }
    .review-card { border: 1px solid var(--line); border-radius: 8px; padding: 14px; background: #fff; }
    .review-card-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; margin-bottom: 12px; }
    .review-card h4 { margin: 0; font-size: 14px; }
    .review-price { width: 130px; flex: 0 0 130px; }
    .review-price label { font-size: 11px; margin-bottom: 4px; }
    .review-tags { margin: 0 0 12px; }
    .review-tags-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 5px; }
    .review-tags label { margin: 0; font-size: 12px; font-weight: 700; }
    .review-tags input { min-height: 36px; }
    .review-tags small { display: block; color: var(--muted); margin-top: 4px; font-size: 11px; }
    .review-tags button { min-height: 28px; font-size: 11px; padding: 4px 8px; }
    .review-photos { display: grid; grid-template-columns: repeat(auto-fill, minmax(108px, 1fr)); gap: 9px; }
    .review-photo { position: relative; border: 1px solid var(--line); border-radius: 6px; padding: 5px; background: #fafbfc; min-width: 0; }
    .review-photo img { width: 100%; height: 94px; object-fit: contain; display: block; border-radius: 4px; background: #fff; cursor: zoom-in; }
    .review-photo-name { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 10px; color: var(--muted); margin-top: 5px; }
    .review-photo-controls { display: flex; gap: 4px; margin-top: 5px; }
    .icon-button { min-height: 28px; width: 28px; padding: 0; display: inline-grid; place-items: center; font-size: 15px; }
    .icon-button.danger { color: var(--red); }
    .add-photo { min-height: 126px; border: 1px dashed #9db0c6; border-radius: 6px; display: grid; place-items: center; padding: 8px; text-align: center; color: var(--blue-dark); font-size: 12px; cursor: pointer; background: #f7faff; }
    .review-empty { color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; padding: 22px; text-align: center; }
    .format-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 10px;
    }
    .format-choice {
      display: block;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 13px;
      cursor: pointer;
      min-height: 90px;
    }
    .format-choice:has(input:checked) {
      border-color: var(--blue);
      box-shadow: 0 0 0 1px var(--blue);
      background: #f3f7fd;
    }
    .format-choice input { width: 18px; min-height: 18px; margin: 0 0 10px; }
    .format-choice strong { display: block; font-size: 13px; }
    .format-choice span { display: block; color: var(--muted); font-size: 11px; margin-top: 4px; }
    .schedule-grid {
      display: grid;
      grid-template-columns: minmax(240px, 1.5fr) repeat(2, minmax(150px, .75fr));
      gap: 12px;
      max-width: 760px;
    }
    .assistant-footer {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 16px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      background: #fff;
      box-shadow: 0 4px 18px rgba(24,33,43,.08);
      position: sticky;
      bottom: 12px;
      z-index: 2;
    }
    .assistant-footer .summary { border: 0; padding: 0; min-height: 0; background: transparent; }
    .assistant-footer .summary p { margin: 3px 0; font-size: 12px; }
    .ai-control {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 14px;
      margin-top: 12px;
      padding: 12px 13px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .ai-control label {
      display: flex;
      align-items: center;
      gap: 9px;
      margin: 0;
      color: var(--text);
      font-size: 13px;
      cursor: pointer;
    }
    .ai-control input { width: 18px; min-height: 18px; margin: 0; }
    .ai-control small { display: block; color: var(--muted); margin-top: 3px; }
    .description-preview {
      margin-top: 10px;
      border-left: 3px solid var(--blue);
      padding: 11px 13px;
      background: #fff;
      white-space: pre-wrap;
      font-size: 13px;
      line-height: 1.5;
    }
    .activity-shell { max-width: 1180px; margin: 0 auto; }
    .activity-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: flex-start;
      margin-bottom: 20px;
    }
    .activity-head h2 { font-size: 25px; margin-bottom: 7px; }
    .metric-strip {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      margin-bottom: 18px;
    }
    .metric {
      padding: 14px 16px;
      border-right: 1px solid var(--line);
    }
    .metric:last-child { border-right: 0; }
    .metric strong { display: block; font-size: 24px; margin-bottom: 2px; }
    .metric span { color: var(--muted); font-size: 12px; }
    .activity-table { background: #fff; }
    .status-tag {
      display: inline-block;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
      background: #edf0f3;
      color: #4f5965;
    }
    .autonomy-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--border);
    }
    .autonomy-switch { display: flex; align-items: center; gap: 10px; font-weight: 700; }
    .autonomy-switch input { width: 20px; height: 20px; }
    .weekday-row, .family-row { display: flex; flex-wrap: wrap; gap: 8px; }
    .weekday-choice, .family-choice {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid var(--border);
      padding: 8px 10px;
      background: #fff;
      border-radius: 6px;
    }
    .weekday-choice input, .family-choice input { width: 16px; height: 16px; }
    .calendar-week {
      display: grid;
      grid-template-columns: repeat(7, minmax(150px, 1fr));
      gap: 1px;
      overflow-x: auto;
      background: var(--border);
      border: 1px solid var(--border);
    }
    .calendar-day { min-height: 170px; min-width: 150px; padding: 10px; background: #fff; }
    .calendar-day h4 { margin: 0 0 9px; font-size: 13px; }
    .calendar-item {
      border-left: 3px solid var(--blue);
      background: #f7f9fc;
      padding: 8px;
      margin-bottom: 8px;
      border-radius: 4px;
      font-size: 12px;
    }
    .calendar-item strong { display: block; margin: 3px 0; font-size: 12px; }
    .calendar-item input { width: 100%; margin-top: 6px; font-size: 11px; padding: 5px; }
    .calendar-actions { display: flex; gap: 4px; margin-top: 6px; }
    .calendar-actions button { padding: 5px 7px; font-size: 11px; }
    .alert-row { display: flex; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .alert-row:last-child { border-bottom: 0; }
    .custom-form { display: grid; grid-template-columns: repeat(12, minmax(0, 1fr)); gap: 12px; }
    .custom-form > div { grid-column: span 4; }
    .custom-form .wide { grid-column: span 8; }
    .custom-form .full { grid-column: 1 / -1; }
    .custom-photo-preview { display: flex; flex-wrap: wrap; gap: 8px; min-height: 42px; }
    .custom-photo-preview img { width: 72px; height: 72px; object-fit: cover; border: 1px solid var(--border); border-radius: 4px; }
    .custom-product-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
    .custom-product-row:last-child { border-bottom: 0; }
    .photo-assignment-list { display: grid; gap: 12px; }
    .photo-assignment-product { border: 1px solid var(--border); border-radius: 8px; background: #fff; padding: 13px; }
    .photo-assignment-product > h4 { margin: 0 0 11px; font-size: 14px; }
    .photo-account-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 10px; }
    .photo-account { border-top: 1px solid var(--border); padding-top: 9px; min-width: 0; }
    .photo-account > strong { display: block; margin-bottom: 7px; font-size: 12px; }
    .photo-picker { display: flex; flex-wrap: wrap; gap: 7px; }
    .photo-choice { position: relative; width: 72px; height: 72px; margin: 0; cursor: pointer; }
    .photo-choice img { width: 72px; height: 72px; object-fit: contain; border: 2px solid transparent; border-radius: 5px; background: #f7f8fa; }
    .photo-choice input { position: absolute; top: 5px; left: 5px; width: 17px; height: 17px; margin: 0; accent-color: var(--blue); }
    .photo-choice:has(input:checked) img { border-color: var(--blue); }
    .photo-count { display: block; color: var(--muted); font-size: 11px; margin-top: 6px; }
    .status-published { background: var(--green-soft); color: var(--green); }
    .status-running { background: #eaf2fd; color: var(--blue-dark); }
    .status-failed, .status-stopped { background: #faeceb; color: var(--red); }
    .status-pending { background: #fff4da; color: var(--yellow); }
    .status-tested, .status-planned, .status-prepared { background: #edf0f3; color: #4f5965; }
    .empty-activity {
      padding: 42px 20px;
      text-align: center;
      color: var(--muted);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .publish { background: var(--green); border-color: var(--green); color: #fff; }
    .publish:hover { background: #0e693d; }
    .confirm-overlay {
      position: fixed;
      inset: 0;
      z-index: 10;
      display: grid;
      place-items: center;
      background: rgba(18,25,33,.46);
      padding: 20px;
    }
    .confirm-dialog {
      width: min(440px, 100%);
      border-radius: 8px;
      background: #fff;
      box-shadow: 0 18px 54px rgba(0,0,0,.28);
      padding: 20px;
    }
    .confirm-dialog h2 { font-size: 18px; margin-bottom: 8px; }
    .confirm-dialog p { color: var(--muted); line-height: 1.5; }
    .toast {
      position: fixed;
      right: 16px;
      bottom: 16px;
      background: #17202a;
      color: #fff;
      padding: 10px 12px;
      border-radius: 8px;
      box-shadow: 0 4px 18px rgba(0,0,0,.2);
      z-index: 5;
    }
    @media (max-width: 980px) {
      .layout { grid-template-columns: 1fr; }
      nav { border-right: 0; border-bottom: 1px solid var(--line); display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; }
      nav button { margin: 0; }
      .span-4, .span-5, .span-6, .span-7, .span-8 { grid-column: span 12; }
      .col-2, .col-3, .col-4, .col-5, .col-6, .col-7, .col-8 { grid-column: span 12; }
      main { padding: 20px 16px 32px; }
      .format-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .schedule-grid { grid-template-columns: 1fr; max-width: none; }
      .metric-strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .metric { border-bottom: 1px solid var(--line); }
      .metric:last-child { border-bottom: 0; }
    }
    @media (max-width: 620px) {
      header { padding: 0 12px; }
      header .toolbar #saveBtn, header .toolbar #refreshBtn { display: none; }
      nav { grid-template-columns: repeat(3, 1fr); }
      .assistant-head { display: block; }
      .assistant-head .account-status { margin-top: 14px; width: 100%; }
      .family-grid, .format-grid { grid-template-columns: 1fr; }
      .review-grid { grid-template-columns: 1fr; }
      .assistant-footer { position: static; grid-template-columns: 1fr; }
      .assistant-footer .toolbar button { flex: 1 1 145px; }
      .ai-control, .activity-head { align-items: flex-start; flex-direction: column; }
      .activity-table th:nth-child(1), .activity-table td:nth-child(1),
      .activity-table th:nth-child(4), .activity-table td:nth-child(4) { display: none; }
      .autonomy-top { align-items: flex-start; flex-direction: column; }
      .custom-form > div, .custom-form .wide { grid-column: 1 / -1; }
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-mark">F</div>
      <h1>FiruPost Marketplace Bot</h1>
    </div>
    <div class="toolbar">
      <span id="saveState" class="pill">Sin guardar</span>
      <button id="refreshBtn">Actualizar</button>
      <button id="saveBtn" class="primary">Guardar</button>
    </div>
  </header>
  <div class="layout">
    <nav>
      <button data-tab="assistant" class="active">Nueva publicación</button>
      <button data-tab="autonomy">Automatización</button>
      <button data-tab="overview">Actividad</button>
      <button data-tab="inventory">Inventario</button>
      <button data-tab="accounts">Cuentas</button>
      <button data-tab="campaign">Avanzado</button>
      <button data-tab="run">Diagnóstico</button>
    </nav>
    <main>
      <section id="assistant" class="tab">
        <div class="assistant-shell">
          <div class="assistant-head">
            <div>
              <h2>Nueva campaña</h2>
              <p class="muted">Elige los productos y el bot prepara fotos, descripción y variantes.</p>
            </div>
            <div class="assistant-destination">
              <div class="assistant-account-picker">
                <label for="assistantAccount">Cuenta de Marketplace</label>
                <select id="assistantAccount"></select>
              </div>
              <div id="assistantAccountStatus" class="account-status">
                <span class="dot"></span>
                <div><strong>Comprobando cuenta</strong><span>Facebook Marketplace</span></div>
              </div>
            </div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Paso 1</span><h3>¿Qué quieres publicar?</h3></div>
              <span id="assistantSelectionCount" class="selection-badge">0 seleccionados</span>
            </div>
            <div id="assistantFamilies" class="family-grid"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Revisión</span><h3>Fotos y precio antes de publicar</h3></div>
              <span class="selection-badge">Solo para esta campaña</span>
            </div>
            <p class="muted">Revisa las fotos, cambia su orden, quita las que no quieras y ajusta el precio. El inventario original no se modifica.</p>
            <div id="assistantReview" class="review-empty">Selecciona un producto para revisar sus fotos y precio.</div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Paso 2</span><h3>¿Cómo agrupamos las variantes?</h3></div>
            </div>
            <div class="format-grid">
              <label class="format-choice">
                <input type="radio" name="assistantStyle" value="grouped" checked>
                <strong>Todo en un anuncio</strong>
                <span>Recomendado para empezar</span>
              </label>
              <label class="format-choice">
                <input type="radio" name="assistantStyle" value="individual">
                <strong>Una por variante</strong>
                <span>Separa cada talla y color</span>
              </label>
              <label class="format-choice">
                <input type="radio" name="assistantStyle" value="grouped_by_color">
                <strong>Una por color</strong>
                <span>Agrupa las tallas</span>
              </label>
              <label class="format-choice">
                <input type="radio" name="assistantStyle" value="grouped_by_size">
                <strong>Una por talla</strong>
                <span>Agrupa los colores</span>
              </label>
            </div>
            <div class="ai-control">
              <div>
                <label><input id="assistantAiEnabled" type="checkbox" checked> Mejorar las descripciones automáticamente</label>
                <small id="assistantAiStatus">Comprobando servicio de redacción...</small>
              </div>
              <div class="toolbar">
                <button id="assistantConfigureAiBtn">Configurar IA</button>
                <button id="assistantPreviewDescriptionBtn">Ver descripción de ejemplo</button>
              </div>
            </div>
            <div id="assistantDescriptionPreview" class="description-preview hidden"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Paso 3</span><h3>Programación</h3></div>
            </div>
            <div class="schedule-grid">
              <div>
                <label>Primera publicación</label>
                <select id="assistantStartDelay">
                  <option value="0">Ahora</option>
                  <option value="15">En 15 minutos</option>
                  <option value="30">En 30 minutos</option>
                  <option value="60">En 1 hora</option>
                </select>
              </div>
              <div>
                <label>Tiempo entre anuncios</label>
                <select id="assistantInterval">
                  <option value="30">30 minutos</option>
                  <option value="60" selected>1 hora</option>
                  <option value="120">2 horas</option>
                  <option value="240">4 horas</option>
                </select>
              </div>
            </div>
          </div>

          <div class="assistant-footer">
            <div id="assistantSummary" class="summary"></div>
            <div class="toolbar">
              <button id="assistantBuildBtn">Guardar campaña</button>
              <button id="assistantDryBtn" class="primary">Probar primero</button>
              <button id="assistantPublishBtn" class="publish">Publicar campaña</button>
            </div>
          </div>
        </div>
      </section>

      <section id="autonomy" class="tab hidden">
        <div class="assistant-shell">
          <div class="autonomy-top">
            <div>
              <h2>Semana automática</h2>
              <p class="muted">El bot selecciona productos, respeta el calendario y se detiene si Facebook necesita atención.</p>
            </div>
            <label class="autonomy-switch"><input id="autoEnabled" type="checkbox"> Bot activo</label>
          </div>

          <div class="metric-strip">
            <div class="metric"><strong id="autoPublishedToday">0</strong><span>Publicadas hoy</span></div>
            <div class="metric"><strong id="autoPending">0</strong><span>Pendientes</span></div>
            <div class="metric"><strong id="autoBlocked">0</strong><span>Bloqueadas</span></div>
            <div class="metric"><strong id="autoWorker">Detenido</strong><span>Trabajador</span></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading"><div><span class="step-label">Reglas</span><h3>Cuándo debe trabajar</h3></div></div>
            <div class="schedule-grid">
              <div><label>Modo</label><select id="autoMode">
                <option value="simulation">Simulación</option>
                <option value="dry_run">Prueba sin publicar</option>
                <option value="supervised">Supervisado</option>
                <option value="semiautomatic">Calendario aprobado</option>
                <option value="autonomous">Autónomo</option>
              </select></div>
              <div><label>Uso de cuentas</label><select id="autoAccountStrategy">
                <option value="round_robin">Rotar entre cuentas</option>
                <option value="all_accounts">Publicar en todas</option>
              </select></div>
              <div><label>Desde</label><input id="autoStart" type="time"></div>
              <div><label>Hasta</label><input id="autoEnd" type="time"></div>
              <div><label>Máximo diario</label><input id="autoMaxDaily" type="number" min="1" max="20"></div>
              <div><label>Intervalo (minutos)</label><input id="autoInterval" type="number" min="30" step="15"></div>
              <div><label>Días antes de repetir</label><input id="autoCooldown" type="number" min="0" max="30"></div>
            </div>
            <label>Cuentas activas</label>
            <div id="autoAccounts" class="weekday-row"></div>
            <label>Días activos</label>
            <div id="autoDays" class="weekday-row"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading"><div><span class="step-label">Producto nuevo</span><h3>Agregar fuera del inventario</h3></div></div>
            <div class="custom-form">
              <div class="wide"><label>Nombre</label><input id="customTitle" placeholder="Ej: Guantes deportivos Nike"></div>
              <div><label>Precio</label><input id="customPrice" type="number" min="1" step="0.01"></div>
              <div><label>Categoría</label><select id="customCategory"><option>Sports &amp; Outdoors</option><option>Men's clothing &amp; shoes</option><option>Women's clothing &amp; shoes</option><option>Other</option></select></div>
              <div><label>Condición</label><select id="customCondition"><option>New</option><option>Used - Like New</option><option>Used - Good</option></select></div>
              <div><label>Ubicación</label><input id="customLocation" value="Managua, Nicaragua"></div>
              <div class="wide"><label>Etiquetas</label><input id="customTags" placeholder="deportivo, entrenamiento, gimnasio"></div>
              <div class="full"><label>Descripción</label><textarea id="customDescription" rows="4" placeholder="Déjala vacía para generar una descripción local"></textarea></div>
              <div class="wide"><label>Fotos nuevas</label><input id="customPhotos" type="file" accept="image/jpeg,image/png,image/webp" multiple></div>
              <div><label><input id="customRotation" type="checkbox" checked> Incluir en rotación semanal</label></div>
              <div id="customPhotoPreview" class="custom-photo-preview full"></div>
              <div class="full toolbar"><button id="customSaveBtn" class="primary">Guardar producto</button></div>
            </div>
            <div id="customProductsList"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading"><div><span class="step-label">Catálogo</span><h3>Productos disponibles</h3></div></div>
            <div id="autoFamilies" class="family-row"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Fotos</span><h3>Fotos definidas por producto y cuenta</h3></div>
              <span class="selection-badge">Máximo 10 por publicación</span>
            </div>
            <p class="muted">Marca las fotos que usará cada cuenta. Una misma foto puede quedar seleccionada en cuentas diferentes.</p>
            <div id="autoPhotoAssignments" class="photo-assignment-list"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading">
              <div><span class="step-label">Calendario</span><h3>Próximas publicaciones</h3></div>
              <div class="toolbar">
                <button id="autoSaveBtn">Guardar reglas</button>
                <button id="autoGenerateBtn" class="primary">Generar semana</button>
                <button id="autoApproveBtn">Aprobar pendientes</button>
              </div>
            </div>
            <div id="autonomyCalendar" class="calendar-week"></div>
          </div>

          <div class="assistant-section">
            <div class="section-heading"><div><span class="step-label">Atención</span><h3>Alertas</h3></div></div>
            <div id="autoAlerts" class="review-empty">No hay alertas abiertas.</div>
          </div>

          <div class="assistant-section">
            <div class="section-heading"><div><span class="step-label">Resumen</span><h3>Últimos 7 días</h3></div></div>
            <div id="autoReport" class="table-wrap"></div>
          </div>
        </div>
      </section>

      <section id="overview" class="tab hidden">
        <div class="activity-shell">
          <div class="activity-head">
            <div>
              <h2>Actividad</h2>
              <p class="muted">Seguimiento de publicaciones preparadas, pendientes y completadas.</p>
            </div>
            <div class="toolbar">
              <button id="openSessionQuick">Abrir sesión</button>
              <button id="clearActivityBtn">Limpiar historial</button>
            </div>
          </div>
          <div class="metric-strip">
            <div class="metric"><strong id="activityPending">0</strong><span>Pendientes</span></div>
            <div class="metric"><strong id="activityRunning">0</strong><span>En proceso</span></div>
            <div class="metric"><strong id="activityPrepared">0</strong><span>Preparadas</span></div>
            <div class="metric"><strong id="activityPublished">0</strong><span>Publicadas</span></div>
            <div class="metric"><strong id="activityFailed">0</strong><span>Fallidas</span></div>
          </div>
          <div id="activityEmpty" class="empty-activity">Todavía no hay actividad. Crea una prueba o publica una campaña para verla aquí.</div>
          <div id="activityTableWrap" class="table-wrap activity-table hidden">
            <table>
              <thead><tr><th>Hora</th><th>Producto</th><th>Estado</th><th>Cuenta</th><th>Detalle</th></tr></thead>
              <tbody id="activityTable"></tbody>
            </table>
          </div>
        </div>
      </section>

      <section id="campaign" class="tab hidden">
        <div class="grid">
          <div class="panel span-12">
            <h2>Opciones generales</h2>
            <div class="row">
              <div class="col-3">
                <label>Cuenta por defecto</label>
                <select id="defaultAccount"></select>
              </div>
              <div class="col-3">
                <label>Intervalo entre publicaciones (min)</label>
                <input id="defaultInterval" type="number" min="0" step="1" />
              </div>
              <div class="col-3">
                <label>Categoria</label>
                <input id="defaultCategory" />
              </div>
              <div class="col-3">
                <label>Condicion</label>
                <select id="defaultCondition">
                  <option>New</option>
                  <option>Used - Like New</option>
                  <option>Used - Good</option>
                  <option>Used - Fair</option>
                </select>
              </div>
            </div>
          </div>
          <div class="panel span-12">
            <div class="toolbar" style="justify-content: space-between;">
              <h2>Jobs de publicacion</h2>
              <button id="addJobBtn">Agregar job</button>
            </div>
            <div id="jobs" class="jobs"></div>
          </div>
        </div>
      </section>

      <section id="accounts" class="tab hidden">
        <div class="grid">
          <div class="panel span-12">
            <div class="toolbar" style="justify-content: space-between;">
              <h2>Cuentas Marketplace</h2>
              <button id="addAccountBtn">Agregar cuenta</button>
            </div>
            <div id="accountsList" class="jobs"></div>
          </div>
        </div>
      </section>

      <section id="inventory" class="tab hidden">
        <div class="grid">
          <div class="panel span-12">
            <h2>Inventario</h2>
            <div class="row">
              <div class="col-8">
                <label>Buscar por SKU, titulo o categoria</label>
                <input id="inventorySearch" placeholder="Ej: sin mangas, leggings, TSBK" />
              </div>
              <div class="col-4">
                <label>Filtro rapido</label>
                <select id="inventoryQuick">
                  <option value="">Todo</option>
                  <option value="camisa de compresion">Camisas compresion</option>
                  <option value="sin mangas">Sin mangas</option>
                  <option value="leggins">Leggins</option>
                  <option value="enterizo">Enterizos</option>
                </select>
              </div>
            </div>
            <div class="table-wrap" style="margin-top: 12px;">
              <table>
                <thead><tr><th>#</th><th>SKU</th><th>Titulo</th><th>Precio</th><th>Imagen</th><th>Usar</th></tr></thead>
                <tbody id="inventoryTable"></tbody>
              </table>
            </div>
          </div>
        </div>
      </section>

      <section id="run" class="tab hidden">
        <div class="grid">
          <div class="panel span-12">
            <h2>Ejecutar</h2>
            <div class="row">
              <div class="col-3">
                <label>Modo</label>
                <select id="runMode">
                  <option value="plan">Solo plan</option>
                  <option value="dry">Dry-run sin publicar</option>
                  <option value="advance">Avanzar a Next</option>
                  <option value="publish">Publicar</option>
                </select>
              </div>
              <div class="col-2">
                <label>Ignorar esperas</label>
                <select id="runFast">
                  <option value="true">Si</option>
                  <option value="false">No</option>
                </select>
              </div>
              <div class="col-2">
                <label>Max jobs</label>
                <input id="runMaxJobs" type="number" min="0" step="1" value="0" />
              </div>
              <div class="col-5">
                <label>Confirmacion para publicar</label>
                <input id="publishConfirm" placeholder="Escribe PUBLICAR para permitir modo Publicar" />
              </div>
            </div>
            <div class="toolbar" style="margin-top: 12px;">
              <button id="startRunBtn" class="primary">Iniciar</button>
              <button id="stopRunBtn" class="danger">Detener</button>
              <span id="processStatus" class="pill">idle</span>
            </div>
          </div>
          <div class="panel span-12">
            <h2>Logs</h2>
            <div id="logs" class="logs"></div>
          </div>
        </div>
      </section>
    </main>
  </div>
  <div id="toast" class="toast hidden"></div>
  <div id="publishDialog" class="confirm-overlay hidden">
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="publishDialogTitle">
      <h2 id="publishDialogTitle">Confirmar publicación</h2>
      <p id="publishDialogText">El bot abrirá Marketplace y publicará los productos seleccionados en la cuenta indicada.</p>
      <div class="toolbar" style="justify-content: flex-end; margin-top: 16px;">
        <button id="cancelPublishBtn">Cancelar</button>
        <button id="confirmPublishBtn" class="publish">Sí, publicar</button>
      </div>
    </div>
  </div>
  <div id="aiDialog" class="confirm-overlay hidden">
    <div class="confirm-dialog" role="dialog" aria-modal="true" aria-labelledby="aiDialogTitle">
      <h2 id="aiDialogTitle">Configurar descripciones con IA</h2>
      <p>Introduce tu clave de OpenAI. Se guardará cifrada para tu usuario de Windows y no se mostrará nuevamente.</p>
      <label>Clave de OpenAI</label>
      <input id="aiKeyInput" type="password" autocomplete="off" placeholder="sk-..." />
      <div class="toolbar" style="justify-content: flex-end; margin-top: 16px;">
        <button id="cancelAiBtn">Cancelar</button>
        <button id="saveAiBtn" class="primary">Guardar clave</button>
      </div>
    </div>
  </div>
  <script>
    let state = { accounts: { accounts: {} }, campaign: { jobs: [], defaults: {} }, inventory: [], process: {}, assistantPresets: {}, activity: { items: [] }, ai: {}, autonomy: {} };
    let assistantReview = {};
    let customPhotos = [];
    let autoPhotoDraft = {};
    let autoPhotoDraftDirty = false;
    let dirty = false;
    const $ = (id) => document.getElementById(id);

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
      }[character]));
    }

    function toast(message) {
      const el = $("toast");
      el.textContent = message;
      el.classList.remove("hidden");
      setTimeout(() => el.classList.add("hidden"), 2800);
    }

    function setDirty(value = true) {
      dirty = value;
      $("saveState").textContent = dirty ? "Cambios sin guardar" : "Guardado";
    }

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "Error");
      return payload;
    }

    function activeAccountKey() {
      return $("defaultAccount").value || Object.keys(state.accounts.accounts || {})[0] || "cuenta1";
    }

    function automaticProducts() {
      const inventory = (state.autonomy?.families || []).map(family => ({
        key: family.key,
        label: family.label,
        images: family.images || [],
      }));
      const custom = (state.autonomy?.custom_products || []).filter(product => product.active).map(product => ({
        key: product.job?.family_key || `custom:${product.id}`,
        label: product.title,
        images: product.images || [],
      }));
      return [...inventory, ...custom];
    }

    function resetAutoPhotoDraft() {
      const persisted = state.autonomy?.photo_assignments || {};
      const accounts = Object.keys(state.accounts?.accounts || {});
      autoPhotoDraft = {};
      accounts.forEach(account => {
        autoPhotoDraft[account] = {};
        automaticProducts().forEach(product => {
          const defined = persisted[account]?.[product.key];
          autoPhotoDraft[account][product.key] = (defined?.length ? defined : product.images).map(image => image.id);
        });
      });
      autoPhotoDraftDirty = false;
    }

    function renderAutoPhotoAssignments() {
      const accounts = state.accounts?.accounts || {};
      const selectedAccounts = [...$("autoAccounts").querySelectorAll("input:checked")].map(input => input.value);
      const products = automaticProducts();
      if (!selectedAccounts.length || !products.length) {
        $("autoPhotoAssignments").innerHTML = `<div class="review-empty">Selecciona una cuenta activa y asegúrate de que haya productos con fotos.</div>`;
        return;
      }
      selectedAccounts.forEach(account => {
        autoPhotoDraft[account] ||= {};
        products.forEach(product => {
          autoPhotoDraft[account][product.key] ||= product.images.map(image => image.id);
        });
      });
      $("autoPhotoAssignments").innerHTML = products.map(product => `<article class="photo-assignment-product">
        <h4>${escapeHtml(product.label)}</h4>
        <div class="photo-account-grid">${selectedAccounts.map(account => {
          const selected = new Set(autoPhotoDraft[account]?.[product.key] || []);
          const choices = product.images.map((image, index) => `<label class="photo-choice" title="${escapeHtml(image.name)}">
            <img src="${escapeHtml(image.url)}" alt="${escapeHtml(product.label)} - foto ${index + 1}">
            <input type="checkbox" data-auto-photo-account="${escapeHtml(account)}" data-auto-photo-family="${escapeHtml(product.key)}" value="${escapeHtml(image.id)}" ${selected.has(image.id) ? "checked" : ""}>
          </label>`).join("");
          return `<div class="photo-account"><strong>${escapeHtml(accounts[account]?.display_name || account)}</strong>
            <div class="photo-picker">${choices || '<span class="muted">Sin fotos disponibles</span>'}</div>
            <span class="photo-count">${selected.size} fotos seleccionadas</span>
          </div>`;
        }).join("")}</div>
      </article>`).join("");
      document.querySelectorAll("[data-auto-photo-account]").forEach(input => {
        input.onchange = () => {
          const account = input.dataset.autoPhotoAccount;
          const family = input.dataset.autoPhotoFamily;
          const selected = new Set(autoPhotoDraft[account]?.[family] || []);
          if (input.checked && selected.size >= 10) {
            input.checked = false;
            toast("Solo se pueden usar hasta 10 fotos por publicación");
            return;
          }
          input.checked ? selected.add(input.value) : selected.delete(input.value);
          autoPhotoDraft[account][family] = [...selected];
          autoPhotoDraftDirty = true;
          setDirty();
          renderAutoPhotoAssignments();
        };
      });
    }

    function renderOverview() {
      const items = [...(state.activity?.items || [])].reverse();
      const counts = status => items.filter(item => item.status === status).length;
      $("activityPending").textContent = counts("pending");
      $("activityRunning").textContent = counts("running");
      $("activityPrepared").textContent = items.filter(item => ["planned", "tested", "prepared"].includes(item.status)).length;
      $("activityPublished").textContent = counts("published");
      $("activityFailed").textContent = items.filter(item => ["failed", "stopped"].includes(item.status)).length;
      $("activityEmpty").classList.toggle("hidden", items.length > 0);
      $("activityTableWrap").classList.toggle("hidden", items.length === 0);
      const statusLabels = {
        pending: "Pendiente",
        running: "En proceso",
        published: "Publicada",
        failed: "Fallida",
        tested: "Prueba lista",
        prepared: "Preparada",
        planned: "Plan revisado",
        skipped: "Omitida",
        stopped: "Detenida",
      };
      $("activityTable").innerHTML = items.slice(0, 100).map(item => {
        const date = item.updated_at ? new Date(item.updated_at) : null;
        const time = date && !Number.isNaN(date.getTime())
          ? date.toLocaleString("es-NI", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
          : "";
        const source = String(item.description_source || "").startsWith("openai:")
          ? " · descripción IA"
          : item.description_source ? " · redacción local" : "";
        return `<tr>
          <td>${escapeHtml(time)}</td>
          <td><strong>${escapeHtml(item.name)}</strong><br><span class="muted">${escapeHtml(source.replace(" · ", ""))}</span></td>
          <td><span class="status-tag status-${escapeHtml(item.status)}">${escapeHtml(statusLabels[item.status] || item.status)}</span></td>
          <td>${escapeHtml(item.account)}</td>
          <td>${escapeHtml(item.detail || "")}</td>
        </tr>`;
      }).join("");
    }

    function renderAutonomy() {
      const autonomy = state.autonomy || {};
      const config = autonomy.config || {};
      const accounts = state.accounts.accounts || {};
      $("autoEnabled").checked = Boolean(config.enabled);
      $("autoMode").value = config.mode || "supervised";
      $("autoAccountStrategy").value = config.account_strategy || "round_robin";
      const selectedAccounts = new Set(config.accounts || [config.account]);
      $("autoAccounts").innerHTML = Object.keys(accounts).map(key => `<label class="weekday-choice"><input type="checkbox" value="${escapeHtml(key)}" ${selectedAccounts.has(key) ? "checked" : ""}> ${escapeHtml(accounts[key].display_name || key)}</label>`).join("");
      $("autoAccounts").querySelectorAll("input").forEach(input => input.onchange = () => {
        autoPhotoDraftDirty = true;
        setDirty();
        renderAutoPhotoAssignments();
      });
      $("autoStart").value = config.start_time || "09:00";
      $("autoEnd").value = config.end_time || "18:00";
      $("autoMaxDaily").value = config.max_per_day ?? 3;
      $("autoInterval").value = config.interval_minutes ?? 180;
      $("autoCooldown").value = config.cooldown_days ?? 3;

      const dayNames = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"];
      const activeDays = new Set(config.active_days || []);
      $("autoDays").innerHTML = dayNames.map((name, index) => `<label class="weekday-choice"><input type="checkbox" value="${index}" ${activeDays.has(index) ? "checked" : ""}> ${name}</label>`).join("");

      const selectedFamilies = new Set(config.selected_families || []);
      $("autoFamilies").innerHTML = (autonomy.families || []).map(family => `<label class="family-choice" title="${escapeHtml(family.reason || "")}">
        <input type="checkbox" value="${escapeHtml(family.key)}" ${selectedFamilies.has(family.key) ? "checked" : ""} ${family.eligible ? "" : "disabled"}>
        ${escapeHtml(family.label)} <span class="muted">${family.valid_variant_count}/${family.variant_count}</span>
      </label>`).join("");

      const counts = autonomy.summary?.counts || {};
      $("autoPublishedToday").textContent = autonomy.summary?.published_today || 0;
      $("autoPending").textContent = (counts.planned || 0) + (counts.queued || 0) + (counts.retry || 0);
      $("autoBlocked").textContent = counts.blocked || 0;
      $("autoWorker").textContent = autonomy.worker?.process_running ? "Activo" : "Detenido";

      renderAutonomyCalendar();
      renderAutonomyAlerts();
      renderAutonomyReport();
      renderCustomProducts();
      renderAutoPhotoAssignments();
    }

    function renderAutonomyCalendar() {
      const items = (state.autonomy?.queue || []).filter(item => !["cancelled", "skipped"].includes(item.status));
      const byDay = {};
      items.forEach(item => {
        const key = String(item.scheduled_at || "").slice(0, 10);
        (byDay[key] ||= []).push(item);
      });
      const dates = [];
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      for (let index = 0; index < 7; index += 1) {
        const day = new Date(today);
        day.setDate(today.getDate() + index);
        dates.push(day);
      }
      const labels = { planned: "Por aprobar", queued: "Programada", running: "En proceso", retry: "Reintento", published: "Publicada", tested: "Probada", blocked: "Bloqueada" };
      $("autonomyCalendar").innerHTML = dates.map(day => {
        const key = `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
        const dayItems = byDay[key] || [];
        return `<div class="calendar-day"><h4>${escapeHtml(day.toLocaleDateString("es-NI", { weekday: "short", day: "numeric", month: "short" }))}</h4>
          ${dayItems.length ? dayItems.map(item => {
            const value = String(item.scheduled_at || "").slice(0, 16);
            const canCancel = ["planned", "queued", "retry", "blocked"].includes(item.status);
            return `<div class="calendar-item">
              <span>${escapeHtml(value.slice(11))} · ${escapeHtml(labels[item.status] || item.status)}</span>
              <strong>${escapeHtml(item.name)}</strong>
              <span>${escapeHtml(item.account)}</span>
              <input type="datetime-local" value="${escapeHtml(value)}" data-move-value="${escapeHtml(item.id)}">
              <div class="calendar-actions">
                <button data-auto-action="move" data-id="${escapeHtml(item.id)}" title="Cambiar hora">Mover</button>
                ${item.status === "planned" ? `<button data-auto-action="approve" data-id="${escapeHtml(item.id)}">Aprobar</button>` : ""}
                ${item.status === "blocked" ? `<button data-auto-action="retry" data-id="${escapeHtml(item.id)}">Reintentar</button>` : ""}
                ${canCancel ? `<button data-auto-action="cancel" data-id="${escapeHtml(item.id)}">Omitir</button>` : ""}
              </div>
            </div>`;
          }).join("") : `<span class="muted">Sin publicaciones</span>`}
        </div>`;
      }).join("");
      document.querySelectorAll("[data-auto-action]").forEach(button => {
        button.onclick = () => autonomyItemAction(button.dataset.id, button.dataset.autoAction);
      });
    }

    function renderAutonomyAlerts() {
      const alerts = state.autonomy?.alerts || [];
      $("autoAlerts").classList.toggle("review-empty", alerts.length === 0);
      $("autoAlerts").innerHTML = alerts.length ? alerts.map(alert => `<div class="alert-row">
        <div><strong>${escapeHtml(alert.code)}</strong><br><span>${escapeHtml(alert.message)}</span></div>
        <button data-alert-resolve="${escapeHtml(alert.id)}">Resolver</button>
      </div>`).join("") : "No hay alertas abiertas.";
      document.querySelectorAll("[data-alert-resolve]").forEach(button => {
        button.onclick = async () => {
          const result = await api("/api/autonomy/alert/resolve", { method: "POST", body: JSON.stringify({ id: button.dataset.alertResolve }) });
          state.autonomy = result.autonomy;
          renderAutonomy();
        };
      });
    }

    function renderAutonomyReport() {
      const report = state.autonomy?.report || {};
      const familyLabels = Object.fromEntries((state.autonomy?.families || []).map(item => [item.key, item.label]));
      const families = report.families || [];
      const errors = report.errors || [];
      $("autoReport").innerHTML = `<table>
        <thead><tr><th>Producto</th><th>Publicadas</th><th>Última publicación</th></tr></thead>
        <tbody>${families.length ? families.map(item => `<tr><td>${escapeHtml(familyLabels[item.family_key] || item.family_key || "Sin familia")}</td><td>${item.total}</td><td>${escapeHtml(item.last_at || "")}</td></tr>`).join("") : `<tr><td colspan="3" class="muted">Todavía no hay publicaciones automáticas registradas.</td></tr>`}</tbody>
      </table>${errors.length ? `<p class="muted">Incidencias: ${errors.map(item => `${escapeHtml(item.error_code)} (${item.total})`).join(" · ")}</p>` : ""}`;
    }

    function renderCustomPhotoPreview() {
      $("customPhotoPreview").innerHTML = customPhotos.length
        ? customPhotos.map(photo => `<img src="${escapeHtml(photo.url)}" alt="${escapeHtml(photo.name)}">`).join("")
        : `<span class="muted">Agrega entre 1 y 10 fotos reales del producto.</span>`;
    }

    async function uploadCustomPhotos(files) {
      if (!files.length) return;
      const selected = files.slice(0, Math.max(0, 10 - customPhotos.length));
      try {
        for (const file of selected) {
          if (file.size > 8 * 1024 * 1024) throw new Error(`${file.name} pesa más de 8 MB.`);
          const result = await api("/api/assistant/upload-image", {
            method: "POST",
            body: JSON.stringify({ filename: file.name, data: await dataUrlForFile(file) }),
          });
          customPhotos.push(result.media);
        }
        renderCustomPhotoPreview();
      } catch (error) {
        toast(error.message);
      }
    }

    async function saveCustomProduct() {
      const tags = $("customTags").value.split(/[,;]+/).map(value => value.trim()).filter(Boolean);
      const payload = {
        title: $("customTitle").value.trim(),
        price: Number($("customPrice").value || 0),
        description: $("customDescription").value.trim(),
        category: $("customCategory").value,
        condition: $("customCondition").value,
        location: $("customLocation").value.trim(),
        tags,
        image_ids: customPhotos.map(photo => photo.id),
        include_in_rotation: $("customRotation").checked,
        active: true,
      };
      const result = await api("/api/custom-product", { method: "POST", body: JSON.stringify(payload) });
      state.autonomy = result.autonomy;
      customPhotos = [];
      ["customTitle", "customPrice", "customDescription", "customTags"].forEach(id => $(id).value = "");
      $("customPhotos").value = "";
      renderAutonomy();
      toast("Producto agregado al catálogo automático");
    }

    function renderCustomProducts() {
      const products = state.autonomy?.custom_products || [];
      $("customProductsList").innerHTML = products.length ? products.map(product => `<div class="custom-product-row">
        <div><strong>${escapeHtml(product.title)}</strong><br><span class="muted">C$ ${escapeHtml(product.price)} · ${product.images?.length || 0} fotos${product.include_in_rotation ? " · en rotación" : ""}</span></div>
        <label><input type="checkbox" data-custom-active="${escapeHtml(product.id)}" ${product.active ? "checked" : ""}> Activo</label>
      </div>`).join("") : `<p class="muted">Todavía no has agregado productos manuales.</p>`;
      document.querySelectorAll("[data-custom-active]").forEach(input => {
        input.onchange = async () => {
          const result = await api("/api/custom-product/active", { method: "POST", body: JSON.stringify({ id: input.dataset.customActive, active: input.checked }) });
          state.autonomy = result.autonomy;
          renderAutonomy();
        };
      });
      renderCustomPhotoPreview();
    }

    function collectAutonomyConfig() {
      const current = state.autonomy?.config || {};
      return {
        ...current,
        enabled: $("autoEnabled").checked,
        mode: $("autoMode").value,
        accounts: [...$("autoAccounts").querySelectorAll("input:checked")].map(input => input.value),
        account_strategy: $("autoAccountStrategy").value,
        start_time: $("autoStart").value,
        end_time: $("autoEnd").value,
        max_per_day: Number($("autoMaxDaily").value || 3),
        interval_minutes: Number($("autoInterval").value || 180),
        cooldown_days: Number($("autoCooldown").value || 0),
        active_days: [...$("autoDays").querySelectorAll("input:checked")].map(input => Number(input.value)),
        selected_families: [...$("autoFamilies").querySelectorAll("input:checked")].map(input => input.value),
        account_photo_assignments: autoPhotoDraft,
      };
    }

    async function saveAutonomy() {
      if (!$("autoAccounts").querySelector("input:checked")) throw new Error("Selecciona al menos una cuenta.");
      const result = await api("/api/autonomy/config", { method: "POST", body: JSON.stringify({ config: collectAutonomyConfig() }) });
      state.autonomy = result.autonomy;
      resetAutoPhotoDraft();
      renderAutonomy();
      toast("Reglas automáticas guardadas");
    }

    async function generateAutonomyWeek() {
      await saveAutonomy();
      const hasFuture = (state.autonomy.queue || []).some(item => ["planned", "queued", "retry"].includes(item.status));
      const replace = hasFuture && window.confirm("¿Reemplazar las publicaciones futuras por un calendario nuevo?");
      const result = await api("/api/autonomy/generate", { method: "POST", body: JSON.stringify({ replace }) });
      state.autonomy = result.autonomy;
      renderAutonomy();
      toast(`${result.result.created} publicaciones agregadas al calendario`);
    }

    async function approveAutonomyWeek() {
      const result = await api("/api/autonomy/approve", { method: "POST", body: "{}" });
      state.autonomy = result.autonomy;
      renderAutonomy();
      toast(`${result.approved} publicaciones aprobadas`);
    }

    async function autonomyItemAction(id, action) {
      const payload = { id, action };
      if (action === "move") payload.scheduled_at = document.querySelector(`[data-move-value="${CSS.escape(id)}"]`)?.value;
      const result = await api("/api/autonomy/item", { method: "POST", body: JSON.stringify(payload) });
      state.autonomy = result.autonomy;
      renderAutonomy();
    }

    function renderAssistant() {
      const accounts = state.accounts.accounts || {};
      $("assistantAccount").innerHTML = Object.keys(accounts).map(key => `<option value="${key}">${accounts[key].display_name || key}</option>`).join("");
      $("assistantAccount").value = state.campaign.default_account || Object.keys(accounts)[0] || "";
      const preferredInterval = String(state.campaign.default_interval_minutes ?? 60);
      if ([...$("assistantInterval").options].some(option => option.value === preferredInterval)) {
        $("assistantInterval").value = preferredInterval;
      }
      const activeJob = (state.campaign.jobs || []).find(job => job.enabled !== false);
      const currentStyle = activeJob ? jobModeValue(activeJob) : "grouped";
      const styleControl = document.querySelector(`input[name="assistantStyle"][value="${currentStyle}"]`);
      if (styleControl) styleControl.checked = true;
      $("assistantAiEnabled").checked = state.campaign.defaults?.ai_descriptions !== false;
      renderAssistantAiStatus();
      const presets = state.assistantPresets || {};
      const currentFamilies = selectedFamilies();
      $("assistantFamilies").innerHTML = Object.keys(presets).map((key) => {
        const preset = presets[key];
        const selected = currentFamilies.includes(key);
        const image = preset.imageUrl
          ? `<img class="family-thumb" src="${preset.imageUrl}" alt="${preset.label}">`
          : `<span class="family-thumb" aria-hidden="true"></span>`;
        const variants = Number(preset.variantCount || 0);
        const price = preset.priceFrom ? `<span>Desde C$${preset.priceFrom}</span>` : "";
        return `<label class="family ${selected ? "selected" : ""}">
          ${image}
          <span>
            <strong>${preset.label}</strong>
            <span class="muted">${preset.description || ""}</span>
            <span class="family-meta"><span>${variants} variantes</span>${price}</span>
          </span>
          <input type="checkbox" value="${key}" ${selected ? "checked" : ""}>
        </label>`;
      }).join("");
      $("assistantFamilies").querySelectorAll("input").forEach(input => input.addEventListener("change", () => {
        $("assistantDescriptionPreview").classList.add("hidden");
        renderAssistantSummary();
        renderAssistantSelectionCount();
        $("assistantFamilies").querySelectorAll(".family").forEach(card => {
          const box = card.querySelector("input");
          card.classList.toggle("selected", box.checked);
        });
        refreshAssistantReview();
      }));
      renderAssistantSelectionCount();
      renderAssistantAccountStatus();
      renderAssistantSummary();
      refreshAssistantReview();
    }

    function selectedAssistantFamilyKeys() {
      return [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
    }

    async function refreshAssistantReview() {
      const families = selectedAssistantFamilyKeys();
      if (!families.length) {
        assistantReview = {};
        renderAssistantReview();
        return;
      }
      $("assistantReview").className = "review-empty";
      $("assistantReview").textContent = "Cargando fotos y precio...";
      try {
        const query = new URLSearchParams({ families: families.join(",") });
        const result = await api(`/api/assistant/review?${query.toString()}`);
        const next = {};
        result.families.forEach(item => {
          const existing = assistantReview[item.key];
          next[item.key] = existing && existing.changed
            ? existing
            : { ...item, images: [...(item.images || [])], changed: false };
        });
        assistantReview = next;
        renderAssistantReview();
      } catch (error) {
        $("assistantReview").className = "review-empty";
        $("assistantReview").textContent = "No pude cargar las fotos. Actualiza el panel e inténtalo otra vez.";
        toast(error.message);
      }
    }

    function renderAssistantReview() {
      const entries = Object.values(assistantReview);
      if (!entries.length) {
        $("assistantReview").className = "review-empty";
        $("assistantReview").textContent = "Selecciona un producto para revisar sus fotos y precio.";
        return;
      }
      $("assistantReview").className = "review-grid";
      $("assistantReview").innerHTML = entries.map(item => {
        const images = (item.images || []).map((image, index) => `<div class="review-photo">
          <a href="${escapeHtml(image.url)}" target="_blank" title="Ver foto completa"><img src="${escapeHtml(image.url)}" alt="${escapeHtml(item.label)} - foto ${index + 1}"></a>
          <span class="review-photo-name" title="${escapeHtml(image.name)}">${escapeHtml(image.name)}</span>
          <div class="review-photo-controls">
            <button class="icon-button" data-review-move="${escapeHtml(item.key)}" data-photo-index="${index}" data-delta="-1" title="Mover foto antes" aria-label="Mover foto antes" ${index === 0 ? "disabled" : ""}>↑</button>
            <button class="icon-button" data-review-move="${escapeHtml(item.key)}" data-photo-index="${index}" data-delta="1" title="Mover foto después" aria-label="Mover foto después" ${index === item.images.length - 1 ? "disabled" : ""}>↓</button>
            <button class="icon-button danger" data-review-remove="${escapeHtml(item.key)}" data-photo-index="${index}" title="Quitar foto" aria-label="Quitar foto">×</button>
          </div>
        </div>`).join("");
        const addPhoto = item.images.length < 10 ? `<label class="add-photo" title="Agregar fotos a esta campaña">+ Agregar fotos<input data-review-add="${escapeHtml(item.key)}" type="file" accept="image/jpeg,image/png,image/webp" multiple hidden></label>` : "";
        return `<article class="review-card">
          <div class="review-card-head">
            <div><h4>${escapeHtml(item.label)}</h4><span class="muted">Hasta 10 fotos por publicación</span></div>
            <div class="review-price"><label for="reviewPrice-${escapeHtml(item.key)}">Precio C$</label><input id="reviewPrice-${escapeHtml(item.key)}" data-review-price="${escapeHtml(item.key)}" type="number" min="1" step="1" value="${escapeHtml(item.price)}"></div>
          </div>
          <div class="review-tags">
            <div class="review-tags-head"><label for="reviewTags-${escapeHtml(item.key)}">Etiquetas del producto</label><button type="button" data-review-generate-tags="${escapeHtml(item.key)}">Generar con IA</button></div>
            <input id="reviewTags-${escapeHtml(item.key)}" data-review-tags="${escapeHtml(item.key)}" value="${escapeHtml((item.tags || []).join(', '))}" placeholder="Ej. ropa deportiva, gimnasio, entrenamiento">
            <small>Se agregaran al campo nativo "Etiquetas del producto" de Facebook Marketplace.</small>
          </div>
          <div class="review-photos">${images || '<span class="muted">Sin fotos disponibles.</span>'}${addPhoto}</div>
        </article>`;
      }).join("");
      document.querySelectorAll("[data-review-price]").forEach(input => input.addEventListener("input", event => {
        const item = assistantReview[event.target.dataset.reviewPrice];
        if (!item) return;
        item.price = event.target.value;
        item.changed = true;
        setDirty();
        renderAssistantSummary();
      }));
      document.querySelectorAll("[data-review-tags]").forEach(input => input.addEventListener("input", event => {
        const item = assistantReview[event.target.dataset.reviewTags];
        if (!item) return;
        item.tags = event.target.value.split(/[,;\\n]/).map(tag => tag.trim()).filter(Boolean).slice(0, 8);
        item.changed = true;
        setDirty();
      }));
      document.querySelectorAll("[data-review-remove]").forEach(button => button.addEventListener("click", () => {
        const item = assistantReview[button.dataset.reviewRemove];
        if (!item) return;
        item.images.splice(Number(button.dataset.photoIndex), 1);
        item.changed = true;
        setDirty();
        renderAssistantReview();
      }));
      document.querySelectorAll("[data-review-move]").forEach(button => button.addEventListener("click", () => {
        const item = assistantReview[button.dataset.reviewMove];
        const from = Number(button.dataset.photoIndex);
        const to = from + Number(button.dataset.delta);
        if (!item || to < 0 || to >= item.images.length) return;
        [item.images[from], item.images[to]] = [item.images[to], item.images[from]];
        item.changed = true;
        setDirty();
        renderAssistantReview();
      }));
      document.querySelectorAll("[data-review-add]").forEach(input => input.addEventListener("change", () => uploadReviewPhotos(input.dataset.reviewAdd, [...input.files])));
      document.querySelectorAll("[data-review-generate-tags]").forEach(button => button.addEventListener("click", () => generateReviewTags(button.dataset.reviewGenerateTags, button)));
    }

    async function generateReviewTags(familyKey, button) {
      const item = assistantReview[familyKey];
      if (!item) return;
      button.disabled = true;
      button.textContent = "Generando...";
      try {
        const result = await api("/api/assistant/product-tags", {
          method: "POST",
          body: JSON.stringify({ family: familyKey, enabled: $("assistantAiEnabled").checked, model: state.ai?.model || "gpt-5.6-luna" }),
        });
        item.tags = result.tags || [];
        item.changed = true;
        setDirty();
        renderAssistantReview();
        toast(String(result.source || "").startsWith("openai:") ? "Etiquetas generadas con IA" : "Etiquetas comerciales generadas");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Generar con IA";
      }
    }

    function dataUrlForFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error("No pude leer la foto seleccionada."));
        reader.readAsDataURL(file);
      });
    }

    async function uploadReviewPhotos(familyKey, files) {
      const item = assistantReview[familyKey];
      if (!item || !files.length) return;
      const allowed = Math.max(0, 10 - item.images.length);
      const selected = files.slice(0, allowed);
      if (files.length > allowed) toast("Solo se pueden usar hasta 10 fotos.");
      try {
        for (const file of selected) {
          if (file.size > 8 * 1024 * 1024) throw new Error(`${file.name} pesa más de 8 MB.`);
          const result = await api("/api/assistant/upload-image", {
            method: "POST",
            body: JSON.stringify({ filename: file.name, data: await dataUrlForFile(file) }),
          });
          item.images.push(result.media);
        }
        item.changed = true;
        setDirty();
        renderAssistantReview();
        toast("Fotos agregadas para esta campaña");
      } catch (error) {
        toast(error.message);
      }
    }

    function collectAssistantListingOverrides() {
      const overrides = {};
      selectedAssistantFamilyKeys().forEach(key => {
        const item = assistantReview[key];
        if (!item) return;
        const price = Number(item.price || 0);
        if (!price || price <= 0) throw new Error(`Indica un precio válido para ${item.label}.`);
        if (!item.images.length) throw new Error(`Agrega al menos una foto para ${item.label}.`);
        const tags = (item.tags || []).map(tag => String(tag).trim()).filter(Boolean).slice(0, 8);
        if (!tags.length) throw new Error(`Agrega al menos una etiqueta para ${item.label}.`);
        overrides[key] = { price, image_ids: item.images.map(image => image.id), tags };
      });
      return overrides;
    }

    function renderAssistantAiStatus() {
      const enabled = $("assistantAiEnabled").checked;
      const ai = state.ai || {};
      if (!enabled) {
        $("assistantAiStatus").textContent = "Usará la descripción comercial local del bot.";
      } else if (ai.configured) {
        $("assistantAiStatus").textContent = `IA lista · ${ai.model}`;
      } else {
        $("assistantAiStatus").textContent = "Usará redacción local hasta configurar OPENAI_API_KEY.";
      }
      $("assistantConfigureAiBtn").textContent = ai.configured ? "Cambiar clave" : "Configurar IA";
    }

    function renderAssistantSelectionCount() {
      const count = document.querySelectorAll("#assistantFamilies input:checked").length;
      $("assistantSelectionCount").textContent = `${count} ${count === 1 ? "seleccionado" : "seleccionados"}`;
    }

    function renderAssistantAccountStatus() {
      const key = $("assistantAccount").value;
      const account = (state.accounts.accounts || {})[key] || {};
      const status = (state.statuses || {})[key] || { ok: false, message: "sesion cerrada" };
      const el = $("assistantAccountStatus");
      el.classList.toggle("ok", Boolean(status.ok));
      el.classList.toggle("bad", !status.ok);
      el.innerHTML = `<span class="dot"></span><div><strong>${account.display_name || key || "Sin cuenta"}</strong><span>${status.ok ? "Sesión lista para publicar" : "Abre la sesión de Facebook"}</span></div>${status.ok ? "" : '<button id="assistantOpenSessionBtn">Abrir sesión</button>'}`;
      $("assistantPublishBtn").disabled = !status.ok;
      $("assistantPublishBtn").title = status.ok ? "" : "Abre la sesión de Facebook antes de publicar";
      const openButton = $("assistantOpenSessionBtn");
      if (openButton) openButton.onclick = () => openSession(key);
    }

    function selectedFamilies() {
      const checked = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      if (checked.length) return checked;
      const jobs = state.campaign.jobs || [];
      const inferred = [];
      for (const job of jobs) {
        if (job.enabled === false) continue;
        const title = JSON.stringify(job).toLowerCase();
        if (title.includes("sin mangas")) inferred.push("compression_sleeveless");
        else if (title.includes("leggin")) inferred.push("leggins");
        else if (title.includes("enterizo")) inferred.push("enterizos");
        else if (title.includes("shorts")) inferred.push("shorts");
        else if (title.includes("durag")) inferred.push("durags");
        else if (title.includes("bolso")) inferred.push("bolsos");
        else if (title.includes("muñequera") || title.includes("munequera")) inferred.push("munequeras");
        else if (title.includes("strap")) inferred.push("straps");
        else if (title.includes("\"ts\"") || title.includes("manga corta")) inferred.push("compression_short");
      }
      return [...new Set(inferred)].slice(0, 2);
    }

    function styleText(style) {
      return {
        grouped: "una publicacion agrupada",
        individual: "una publicacion por talla/color",
        grouped_by_color: "una publicacion por color",
        grouped_by_size: "una publicacion por talla",
      }[style] || style;
    }

    function renderAssistantSummary() {
      const presets = state.assistantPresets || {};
      const families = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      const names = families.map(key => presets[key]?.label || key);
      const accountKey = $("assistantAccount").value || "sin cuenta";
      const account = state.accounts.accounts?.[accountKey]?.display_name || accountKey;
      const styleValue = document.querySelector('input[name="assistantStyle"]:checked')?.value || "grouped";
      const style = styleText(styleValue);
      const interval = Number($("assistantInterval").value || 0);
      const startDelay = Number($("assistantStartDelay").value || 0);
      const descriptionMode = $("assistantAiEnabled").checked
        ? (state.ai?.configured ? `descripción con IA ${state.ai.model}` : "redacción comercial local")
        : "redacción comercial local";
      $("assistantSummary").innerHTML = families.length
        ? `<p><b>${names.join(", ")}</b></p>
           <p>${style} · ${account} · ${startDelay ? `inicia en ${startDelay} min` : "inicia ahora"} · cada ${interval} min · ${descriptionMode}</p>`
        : `<p><b>Selecciona al menos un producto</b></p><p class="muted">El bot elegirá las fotos y preparará la descripción.</p>`;
    }

    function renderGeneralCampaign() {
      const accounts = state.accounts.accounts || {};
      $("defaultAccount").innerHTML = Object.keys(accounts).map(key => `<option value="${key}">${key} - ${accounts[key].display_name || ""}</option>`).join("");
      $("defaultAccount").value = state.campaign.default_account || Object.keys(accounts)[0] || "";
      $("defaultInterval").value = state.campaign.default_interval_minutes ?? 60;
      $("defaultCategory").value = state.campaign.defaults?.category || "Men's clothing & shoes";
      $("defaultCondition").value = state.campaign.defaults?.condition || "New";
    }

    function jobModeValue(job) {
      if (job.listing_mode === "grouped" && job.group_by === "color") return "grouped_by_color";
      if (job.listing_mode === "grouped" && job.group_by === "size") return "grouped_by_size";
      if (job.listing_mode) return job.listing_mode;
      return "existing";
    }

    function setJobMode(job, mode) {
      delete job.group_by;
      if (mode === "existing") {
        delete job.listing_mode;
        delete job.source_excel;
        return;
      }
      job.listing_mode = mode;
      if (mode === "grouped_by_color") {
        job.listing_mode = "grouped";
        job.group_by = "color";
      }
      if (mode === "grouped_by_size") {
        job.listing_mode = "grouped";
        job.group_by = "size";
      }
      job.source_excel = job.source_excel || job.excel || "ArticulosGenerados.xlsx";
      job.images_root = job.images_root || "imagenes_firupost";
    }

    function renderJobs() {
      const jobs = state.campaign.jobs || [];
      $("jobs").innerHTML = "";
      jobs.forEach((job, index) => {
        const el = document.createElement("div");
        el.className = "job" + (job.enabled === false ? " disabled" : "");
        el.innerHTML = `
          <div class="row">
            <div class="col-5"><label>Nombre</label><input data-field="name" value="${job.name || ""}"></div>
            <div class="col-2"><label>Activo</label><select data-field="enabled"><option value="true">Si</option><option value="false">No</option></select></div>
            <div class="col-3"><label>Modo</label><select data-field="mode">
              <option value="existing">Excel existente</option>
              <option value="individual">Individual</option>
              <option value="grouped">Agrupado</option>
              <option value="grouped_by_color">Agrupado por color</option>
              <option value="grouped_by_size">Agrupado por talla</option>
            </select></div>
            <div class="col-2"><label>Delay min</label><input data-field="delay_minutes" type="number" min="0" value="${job.delay_minutes ?? 0}"></div>
            <div class="col-3"><label>Cuenta</label><select data-field="account">${accountOptions(job.account || state.campaign.default_account)}</select></div>
            <div class="col-3"><label>Item interval min</label><input data-field="item_interval_minutes" type="number" min="0" value="${job.item_interval_minutes ?? ""}"></div>
            <div class="col-3"><label>Excel/source</label><input data-field="excel" value="${job.source_excel || job.excel || "ArticulosGenerados.xlsx"}"></div>
            <div class="col-3"><label>Imagenes</label><input data-field="images_root" value="${job.images_root || "imagenes_firupost"}"></div>
            <div class="col-3"><label>Row index</label><input data-field="row_index" type="number" min="0" value="${job.row_index ?? 0}"></div>
            <div class="col-3"><label>SKUs</label><input data-field="skus" value="${arrayText(job.skus)}" placeholder="TSBK-M,TSBK-L"></div>
            <div class="col-3"><label>Prefijos SKU</label><input data-field="sku_prefixes" value="${arrayText(job.sku_prefixes)}" placeholder="TS,CC"></div>
            <div class="col-3"><label>Titulo contiene</label><input data-field="title_contains" value="${arrayText(job.title_contains)}" placeholder="sin mangas"></div>
            <div class="col-3"><label>Excluir titulo</label><input data-field="exclude_title_contains" value="${arrayText(job.exclude_title_contains)}" placeholder="sin mangas"></div>
            <div class="col-12 toolbar">
              <button data-action="preview">Previsualizar plan</button>
              <button data-action="duplicate">Duplicar</button>
              <button data-action="delete" class="danger">Eliminar</button>
            </div>
          </div>`;
        $("jobs").appendChild(el);
        el.querySelector('[data-field="enabled"]').value = String(job.enabled !== false);
        el.querySelector('[data-field="mode"]').value = jobModeValue(job);
        el.querySelectorAll("[data-field]").forEach(input => {
          input.addEventListener("input", () => updateJobFromCard(index, el));
          input.addEventListener("change", () => updateJobFromCard(index, el));
        });
        el.querySelector('[data-action="delete"]').onclick = () => { jobs.splice(index, 1); setDirty(); renderJobs(); renderOverview(); };
        el.querySelector('[data-action="duplicate"]').onclick = () => { jobs.splice(index + 1, 0, JSON.parse(JSON.stringify(job))); setDirty(); renderJobs(); renderOverview(); };
        el.querySelector('[data-action="preview"]').onclick = () => previewSingleJob(index);
      });
    }

    function accountOptions(selected) {
      const accounts = state.accounts.accounts || {};
      return Object.keys(accounts).map(key => `<option value="${key}" ${key === selected ? "selected" : ""}>${key}</option>`).join("");
    }

    function arrayText(value) {
      if (!value) return "";
      if (Array.isArray(value)) return value.join(",");
      return String(value);
    }

    function parseList(value) {
      return String(value || "").split(",").map(x => x.trim()).filter(Boolean);
    }

    function updateJobFromCard(index, el) {
      const job = state.campaign.jobs[index];
      job.name = el.querySelector('[data-field="name"]').value;
      job.enabled = el.querySelector('[data-field="enabled"]').value === "true";
      setJobMode(job, el.querySelector('[data-field="mode"]').value);
      job.delay_minutes = Number(el.querySelector('[data-field="delay_minutes"]').value || 0);
      job.account = el.querySelector('[data-field="account"]').value || undefined;
      const itemInterval = el.querySelector('[data-field="item_interval_minutes"]').value;
      if (itemInterval === "") delete job.item_interval_minutes; else job.item_interval_minutes = Number(itemInterval);
      const excel = el.querySelector('[data-field="excel"]').value;
      if (job.listing_mode) job.source_excel = excel; else job.excel = excel;
      job.images_root = el.querySelector('[data-field="images_root"]').value;
      job.row_index = Number(el.querySelector('[data-field="row_index"]').value || 0);
      for (const key of ["skus", "sku_prefixes", "title_contains", "exclude_title_contains"]) {
        const list = parseList(el.querySelector(`[data-field="${key}"]`).value);
        if (list.length) job[key] = list; else delete job[key];
      }
      setDirty();
      renderOverview();
    }

    function renderAccounts() {
      const accounts = state.accounts.accounts || {};
      $("accountsList").innerHTML = "";
      Object.keys(accounts).forEach(key => {
        const account = accounts[key];
        const st = (state.statuses || {})[key] || {};
        const el = document.createElement("div");
        el.className = "job";
        el.innerHTML = `
          <div class="row">
            <div class="col-2"><label>ID</label><input data-field="key" value="${key}"></div>
            <div class="col-3"><label>Nombre visible</label><input data-field="display_name" value="${account.display_name || ""}"></div>
            <div class="col-3"><label>Debugger address</label><input data-field="debugger_address" value="${account.debugger_address || ""}"></div>
            <div class="col-4"><label>Chrome profile</label><input data-field="chrome_profile" value="${account.chrome_profile || ""}"></div>
            <div class="col-12 toolbar">
              <span class="pill ${st.ok ? "ok" : "bad"}"><span class="dot"></span>${st.message || "sin estado"}</span>
              <button data-action="open">Abrir sesion</button>
              <button data-action="delete" class="danger">Eliminar</button>
            </div>
          </div>`;
        $("accountsList").appendChild(el);
        el.querySelectorAll("[data-field]").forEach(input => input.addEventListener("change", () => {
          const newKey = el.querySelector('[data-field="key"]').value.trim();
          const updated = {
            display_name: el.querySelector('[data-field="display_name"]').value,
            debugger_address: el.querySelector('[data-field="debugger_address"]').value,
            chrome_profile: el.querySelector('[data-field="chrome_profile"]').value,
          };
          if (newKey && newKey !== key) {
            delete state.accounts.accounts[key];
            state.accounts.accounts[newKey] = updated;
          } else {
            state.accounts.accounts[key] = updated;
          }
          setDirty();
          renderGeneralCampaign();
          renderAccounts();
          renderOverview();
        }));
        el.querySelector('[data-action="delete"]').onclick = () => {
          delete state.accounts.accounts[key];
          setDirty();
          renderAccounts();
          renderGeneralCampaign();
          renderOverview();
        };
        el.querySelector('[data-action="open"]').onclick = () => openSession(key);
      });
    }

    function renderInventory() {
      const query = ($("inventorySearch").value || $("inventoryQuick").value || "").toLowerCase();
      const rows = state.inventory.filter(row => {
        if (!query) return true;
        return [row.sku, row.title, row.category].join(" ").toLowerCase().includes(query);
      });
      $("inventoryTable").innerHTML = rows.map(row => `
        <tr>
          <td>${row.index}</td>
          <td>${row.sku}</td>
          <td>${row.title}</td>
          <td>${row.price}</td>
          <td>${row.imageFolder}/${row.imageName}</td>
          <td><button data-use-row="${row.index}">Crear job</button></td>
        </tr>`).join("");
      $("inventoryTable").querySelectorAll("[data-use-row]").forEach(btn => {
        btn.onclick = () => {
          const rowIndex = Number(btn.dataset.useRow);
          const row = state.inventory.find(item => item.index === rowIndex);
          state.campaign.jobs.push({
            name: row.title,
            enabled: false,
            delay_minutes: 0,
            excel: "ArticulosGenerados.xlsx",
            images_root: "imagenes_firupost",
            row_index: row.index,
          });
          setDirty();
          showTab("campaign");
          renderJobs();
          renderOverview();
          toast("Job creado deshabilitado");
        };
      });
    }

    function renderProcess() {
      const process = state.process || {};
      $("processStatus").textContent = process.status || "idle";
      $("logs").textContent = (process.logs || []).join("\n");
      $("logs").scrollTop = $("logs").scrollHeight;
    }

    function renderAll() {
      renderAssistant();
      renderAutonomy();
      renderGeneralCampaign();
      renderOverview();
      renderJobs();
      renderAccounts();
      renderInventory();
      renderProcess();
    }

    async function loadState() {
      const [baseState, autonomyState] = await Promise.all([api("/api/state"), api("/api/autonomy")]);
      state = baseState;
      state.autonomy = autonomyState.autonomy;
      resetAutoPhotoDraft();
      renderAll();
      setDirty(false);
    }

    async function saveConfig() {
      collectGeneralCampaign();
      await api("/api/save", { method: "POST", body: JSON.stringify({ accounts: state.accounts, campaign: state.campaign }) });
      setDirty(false);
      toast("Configuracion guardada");
      await loadState();
    }

    async function buildAssistantCampaign(action = "save") {
      const families = [...document.querySelectorAll("#assistantFamilies input:checked")].map(input => input.value);
      if (!families.length) {
        toast("Selecciona al menos un grupo de productos");
        return false;
      }
      let listingOverrides;
      try {
        listingOverrides = collectAssistantListingOverrides();
      } catch (error) {
        toast(error.message);
        return false;
      }
      const payload = {
        account: $("assistantAccount").value,
        style: document.querySelector('input[name="assistantStyle"]:checked')?.value || "grouped",
        interval_minutes: Number($("assistantInterval").value || 0),
        start_delay_minutes: Number($("assistantStartDelay").value || 0),
        ai_descriptions: $("assistantAiEnabled").checked,
        ai_model: state.ai?.model || "gpt-5.6-luna",
        families,
        listing_overrides: listingOverrides,
      };
      const result = await api("/api/assistant/apply", { method: "POST", body: JSON.stringify(payload) });
      state.campaign = result.campaign;
      setDirty(false);
      renderAll();
      toast("Campana creada por el asistente");
      if (action === "plan") await startRun("plan");
      if (action === "dry") {
        $("runFast").value = "true";
        $("runMaxJobs").value = "1";
        await startRun("dry");
      }
      return true;
    }

    async function previewAssistantDescription() {
      const family = document.querySelector("#assistantFamilies input:checked")?.value;
      if (!family) {
        toast("Selecciona un producto primero");
        return;
      }
      const button = $("assistantPreviewDescriptionBtn");
      button.disabled = true;
      button.textContent = "Generando...";
      try {
        const result = await api("/api/assistant/description-preview", {
          method: "POST",
          body: JSON.stringify({
            family,
            enabled: $("assistantAiEnabled").checked,
            model: state.ai?.model || "gpt-5.6-luna",
          }),
        });
        const source = String(result.source || "").startsWith("openai:") ? "Generada con IA" : "Redacción local";
        $("assistantDescriptionPreview").textContent = `${source}\n\n${result.description}`;
        $("assistantDescriptionPreview").classList.remove("hidden");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Ver descripción de ejemplo";
      }
    }

    async function saveAiConfiguration() {
      const apiKey = $("aiKeyInput").value.trim();
      if (!apiKey) {
        toast("Introduce la clave de OpenAI");
        return;
      }
      const button = $("saveAiBtn");
      button.disabled = true;
      button.textContent = "Guardando...";
      try {
        const result = await api("/api/ai/configure", {
          method: "POST",
          body: JSON.stringify({ api_key: apiKey }),
        });
        state.ai = result.ai;
        $("aiKeyInput").value = "";
        $("aiDialog").classList.add("hidden");
        renderAssistantAiStatus();
        renderAssistantSummary();
        toast("IA configurada");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = "Guardar clave";
      }
    }

    function collectGeneralCampaign() {
      state.campaign.default_account = $("defaultAccount").value;
      state.campaign.default_interval_minutes = Number($("defaultInterval").value || 0);
      state.campaign.defaults = state.campaign.defaults || {};
      state.campaign.defaults.category = $("defaultCategory").value;
      state.campaign.defaults.condition = $("defaultCondition").value;
      state.campaign.defaults.meet_public = true;
      state.campaign.defaults.door_pickup = true;
      state.campaign.defaults.door_dropoff = true;
    }

    async function previewSingleJob(index) {
      collectGeneralCampaign();
      const job = JSON.parse(JSON.stringify(state.campaign.jobs[index]));
      job.enabled = true;
      const payload = await api("/api/preview-job", { method: "POST", body: JSON.stringify({ job, campaign: state.campaign }) });
      showTab("run");
      $("logs").textContent = payload.output;
      toast("Plan generado");
    }

    async function openSession(key) {
      await api("/api/open-session", { method: "POST", body: JSON.stringify({ account: key }) });
      toast("Sesion solicitada");
    }

    async function startRun(mode) {
      collectGeneralCampaign();
      await saveConfig();
      const runMode = mode || $("runMode").value;
      if (runMode === "publish" && $("publishConfirm").value !== "PUBLICAR") {
        toast("Escribe PUBLICAR para publicar");
        return;
      }
      const payload = {
        mode: runMode,
        fast: $("runFast").value === "true",
        maxJobs: Number($("runMaxJobs").value || 0),
      };
      await api("/api/run/start", { method: "POST", body: JSON.stringify(payload) });
      showTab("run");
      toast("Proceso iniciado");
      await pollProcess();
    }

    async function stopRun() {
      await api("/api/run/stop", { method: "POST", body: "{}" });
      await pollProcess();
    }

    async function pollProcess() {
      const [processPayload, activityPayload] = await Promise.all([
        api("/api/run/status"),
        api("/api/activity"),
      ]);
      state.process = processPayload.process;
      state.activity = activityPayload.activity;
      renderProcess();
      renderOverview();
    }

    function showTab(name) {
      document.querySelectorAll(".tab").forEach(tab => tab.classList.add("hidden"));
      document.querySelectorAll("nav button").forEach(btn => btn.classList.toggle("active", btn.dataset.tab === name));
      $(name).classList.remove("hidden");
    }

    document.querySelectorAll("nav button").forEach(btn => btn.onclick = () => showTab(btn.dataset.tab));
    $("refreshBtn").onclick = loadState;
    $("saveBtn").onclick = saveConfig;
    $("autoSaveBtn").onclick = saveAutonomy;
    $("autoGenerateBtn").onclick = generateAutonomyWeek;
    $("autoApproveBtn").onclick = approveAutonomyWeek;
    $("autoEnabled").onchange = saveAutonomy;
    $("customPhotos").onchange = () => uploadCustomPhotos([...$("customPhotos").files]);
    $("customSaveBtn").onclick = () => saveCustomProduct().catch(error => toast(error.message));
    $("addJobBtn").onclick = () => {
      state.campaign.jobs.push({
        name: "Nuevo job",
        enabled: false,
        delay_minutes: 0,
        listing_mode: "grouped",
        source_excel: "ArticulosGenerados.xlsx",
        images_root: "imagenes_firupost",
        title_contains: [],
      });
      setDirty();
      renderJobs();
      renderOverview();
    };
    $("addAccountBtn").onclick = () => {
      let key = "cuenta" + (Object.keys(state.accounts.accounts || {}).length + 1);
      state.accounts.accounts[key] = { display_name: key, debugger_address: "127.0.0.1:9223", chrome_profile: "%LOCALAPPDATA%\\MarketplaceBot_" + key };
      setDirty();
      renderAccounts();
      renderGeneralCampaign();
      renderOverview();
    };
    ["assistantInterval", "assistantStartDelay"].forEach(id => $(id).addEventListener("change", renderAssistantSummary));
    $("assistantAccount").addEventListener("change", () => {
      renderAssistantSummary();
      renderAssistantAccountStatus();
    });
    document.querySelectorAll('input[name="assistantStyle"]').forEach(input => input.addEventListener("change", renderAssistantSummary));
    $("assistantAiEnabled").addEventListener("change", () => {
      $("assistantDescriptionPreview").classList.add("hidden");
      renderAssistantAiStatus();
      renderAssistantSummary();
    });
    $("assistantPreviewDescriptionBtn").onclick = previewAssistantDescription;
    $("assistantConfigureAiBtn").onclick = () => {
      $("aiKeyInput").value = "";
      $("aiDialog").classList.remove("hidden");
    };
    $("cancelAiBtn").onclick = () => $("aiDialog").classList.add("hidden");
    $("saveAiBtn").onclick = saveAiConfiguration;
    $("assistantBuildBtn").onclick = () => buildAssistantCampaign("save");
    $("assistantDryBtn").onclick = () => buildAssistantCampaign("dry");
    $("assistantPublishBtn").onclick = () => {
      const count = document.querySelectorAll("#assistantFamilies input:checked").length;
      if (!count) {
        toast("Selecciona al menos un grupo de productos");
        return;
      }
      const account = state.accounts.accounts?.[$("assistantAccount").value]?.display_name || $("assistantAccount").value;
      $("publishDialogText").textContent = `El bot publicará ${count} grupo${count === 1 ? "" : "s"} de productos en la cuenta ${account}.`;
      $("publishDialog").classList.remove("hidden");
    };
    $("cancelPublishBtn").onclick = () => $("publishDialog").classList.add("hidden");
    $("confirmPublishBtn").onclick = async () => {
      $("publishDialog").classList.add("hidden");
      const built = await buildAssistantCampaign("save");
      if (!built) return;
      $("publishConfirm").value = "PUBLICAR";
      await startRun("publish");
    };
    ["defaultAccount", "defaultInterval", "defaultCategory", "defaultCondition"].forEach(id => $(id).addEventListener("change", () => { collectGeneralCampaign(); setDirty(); renderOverview(); }));
    $("inventorySearch").addEventListener("input", renderInventory);
    $("inventoryQuick").addEventListener("change", renderInventory);
    $("openSessionQuick").onclick = () => openSession(activeAccountKey());
    $("clearActivityBtn").onclick = async () => {
      if (!window.confirm("¿Eliminar todo el historial de actividad del bot?")) return;
      await api("/api/activity/clear", { method: "POST", body: "{}" });
      state.activity = { items: [] };
      renderOverview();
      toast("Historial limpiado");
    };
    $("startRunBtn").onclick = () => startRun();
    $("stopRunBtn").onclick = stopRun;
    setInterval(async () => {
      try { await pollProcess(); } catch (e) {}
    }, 2500);
    setInterval(async () => {
      try {
        const payload = await api("/api/autonomy");
        state.autonomy = payload.autonomy;
        renderAutonomy();
      } catch (e) {}
    }, 10000);
    loadState().catch(error => toast(error.message));
  </script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MarketplaceBotDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

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
            if self.path == "/" or self.path.startswith("/?"):
                data = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
            if self.path == "/api/assistant/upload-image":
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
                return
            if self.path == "/api/save":
                payload = self.read_json()
                if "accounts" in payload:
                    json_save(ACCOUNTS_PATH, payload["accounts"])
                if "campaign" in payload:
                    json_save(CAMPAIGN_PATH, payload["campaign"])
                self.send_json({"ok": True})
                return
            if self.path == "/api/autonomy/config":
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
                return
            if self.path == "/api/custom-product":
                payload = self.read_json()
                media_ids = payload.get("image_ids") or []
                if not isinstance(media_ids, list):
                    raise ValueError("Las fotos deben ser una lista.")
                source_images = [media_path_from_id(str(media_id)) for media_id in media_ids[:10]]
                product = create_custom_product(STORE, payload, source_images)
                self.send_json({"ok": True, "product": product, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/custom-product/active":
                payload = self.read_json()
                changed = STORE.set_custom_product_active(str(payload.get("id") or ""), bool(payload.get("active")))
                self.send_json({"ok": True, "changed": changed, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/autonomy/generate":
                payload = self.read_json()
                if payload.get("replace"):
                    cancel_future(STORE)
                result = generate_week(STORE, load_autonomy_config(STORE))
                self.send_json({"ok": True, "result": result, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/autonomy/approve":
                payload = self.read_json()
                changed = approve_items(STORE, payload.get("ids"))
                self.send_json({"ok": True, "approved": changed, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/autonomy/item":
                payload = self.read_json()
                queue_id = str(payload.get("id") or "")
                action = str(payload.get("action") or "")
                if action == "cancel":
                    STORE.update_queue_item(queue_id, status="cancelled", detail="Cancelado desde el calendario.")
                elif action == "retry":
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
                return
            if self.path == "/api/autonomy/alert/resolve":
                payload = self.read_json()
                STORE.resolve_alert(str(payload.get("id") or ""))
                self.send_json({"ok": True, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/autonomy/worker/start":
                pid = start_worker()
                self.send_json({"ok": True, "pid": pid, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/autonomy/worker/stop":
                stopped = stop_worker()
                self.send_json({"ok": True, "stopped": stopped, "autonomy": autonomy_payload()})
                return
            if self.path == "/api/assistant/apply":
                payload = self.read_json()
                campaign = build_assistant_campaign(payload)
                json_save(CAMPAIGN_PATH, campaign)
                self.send_json({"ok": True, "campaign": campaign})
                return
            if self.path == "/api/assistant/description-preview":
                payload = self.read_json()
                self.send_json({"ok": True, **assistant_description_preview(payload)})
                return
            if self.path == "/api/assistant/product-tags":
                payload = self.read_json()
                self.send_json({"ok": True, **assistant_product_tags(payload)})
                return
            if self.path == "/api/ai/configure":
                payload = self.read_json()
                save_api_key(str(payload.get("api_key") or ""))
                self.send_json({"ok": True, "ai": ai_status()})
                return
            if self.path == "/api/activity/clear":
                json_save(ACTIVITY_PATH, {"items": []})
                self.send_json({"ok": True})
                return
            if self.path == "/api/preview-job":
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
                return
            if self.path == "/api/open-session":
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
                return
            if self.path == "/api/run/start":
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
                return
            if self.path == "/api/run/stop":
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
                return
            self.send_json({"ok": False, "error": "Not found"}, status=404)
        except Exception as exc:
            self.handle_error(exc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz grafica local para administrar el bot de Marketplace.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}/"
    print(f"Marketplace Bot Dashboard: {url}", flush=True)
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

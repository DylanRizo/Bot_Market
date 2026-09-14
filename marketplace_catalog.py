from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd


SCRATCH_DIR = Path(__file__).resolve().parent
INVENTORY_PATH = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"

CLOTHING = "Men's clothing & shoes"
SPORTS = "Sports & Outdoors"
DEFAULT_CATEGORY = CLOTHING

# Las familias se reconocen por el prefijo del SKU del SGI (MKT-GUI-001). Las
# nueve primeras conservan su clave porque el historial de publicaciones y de
# fotos usadas por cuenta cuelga de ella. Solo se usan categorias de Marketplace
# ya probadas en el formulario; las familias de tecnologia (HUB, PAD, SOP)
# quedan fuera hasta verificar su categoria.
ASSISTANT_PRESETS: dict[str, dict[str, Any]] = {
    "compression_short": {"label": "Camisas manga corta de compresion", "sku_prefixes": ["CMP-"], "category": CLOTHING},
    "compression_sleeveless": {"label": "Camisas sin mangas", "sku_prefixes": ["CSM-"], "category": CLOTHING},
    "leggins": {"label": "Leggins deportivos", "sku_prefixes": ["LEG-"], "category": CLOTHING},
    "enterizos": {"label": "Enterizos de entrenamiento", "sku_prefixes": ["ENT-"], "category": CLOTHING},
    "shorts": {"label": "Shorts deportivos", "sku_prefixes": ["SHT-"], "category": CLOTHING},
    "bolsos": {"label": "Bolsos deportivos", "sku_prefixes": ["BOL-"], "category": SPORTS},
    "durags": {"label": "Durags", "sku_prefixes": ["DUR-"], "category": CLOTHING},
    "munequeras": {"label": "Munequeras deportivas", "sku_prefixes": ["MUN-"], "category": SPORTS},
    "straps": {"label": "Straps para gimnasio", "sku_prefixes": ["STR-"], "category": SPORTS},
    "compression_long": {"label": "Camisas manga larga de compresion", "sku_prefixes": ["CML-"], "category": CLOTHING},
    "bershka": {"label": "Camisas Bershka", "sku_prefixes": ["BSH-"], "category": CLOTHING},
    "chalecos": {"label": "Chalecos de lana sin mangas", "sku_prefixes": ["CHA-"], "category": CLOTHING},
    "tops": {"label": "Tops deportivos", "sku_prefixes": ["TOP-"], "category": CLOTHING},
    "joggers": {"label": "Pantalones jogger", "sku_prefixes": ["JOG-"], "category": CLOTHING},
    "calcetas": {"label": "Calcetas de compresion", "sku_prefixes": ["CAL-"], "category": CLOTHING},
    "mochilas": {"label": "Mochilas antirrobo", "sku_prefixes": ["MOC-"], "category": SPORTS},
    "rodilleras": {"label": "Rodilleras deportivas", "sku_prefixes": ["ROD-"], "category": SPORTS},
    "cinturones": {"label": "Cinturones de gimnasio", "sku_prefixes": ["CIN-"], "category": SPORTS},
    "mangas_brazo": {"label": "Mangas para brazo", "sku_prefixes": ["MAN-"], "category": SPORTS},
}


def category_for_sku(code: str) -> str:
    value = str(code or "").upper()
    for preset in ASSISTANT_PRESETS.values():
        if any(value.startswith(str(prefix).upper()) for prefix in preset.get("sku_prefixes", [])):
            return str(preset.get("category") or DEFAULT_CATEGORY)
    return DEFAULT_CATEGORY

STYLE_TO_MODE: dict[str, tuple[str, str | None]] = {
    "auto": ("grouped", None),
    "grouped": ("grouped", None),
    "individual": ("individual", None),
    "grouped_by_color": ("grouped", "color"),
    "grouped_by_size": ("grouped", "size"),
}


def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def inventory_rows(path: Path = INVENTORY_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    frame = pd.read_excel(path)
    rows: list[dict[str, Any]] = []
    for index, row in frame.iterrows():
        rows.append(
            {
                "index": int(index),
                "sku": str(row.get("Sku", "") or ""),
                "title": str(row.get("Titulo", "") or ""),
                "price": row.get("Precio", ""),
                "description": str(row.get("Descripcion", "") or ""),
                "image_folder": str(row.get("CarpetaImg", "") or ""),
                "image_name": str(row.get("NombreImg", "") or ""),
            }
        )
    return rows


def preset_matches_row(preset: dict[str, Any], row: dict[str, Any]) -> bool:
    sku = str(row.get("sku") or "").lower()
    title = str(row.get("title") or "").lower()
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


def row_image_paths(row: dict[str, Any], images_root: Path = IMAGES_ROOT) -> list[Path]:
    folders = [value.strip() for value in str(row.get("image_folder") or "").split(";") if value.strip()]
    names = [value.strip() for value in str(row.get("image_name") or "").split(";") if value.strip()]
    if not names:
        return []
    result: list[Path] = []
    for index, name in enumerate(names):
        folder = folders[index] if index < len(folders) else (folders[0] if folders else "")
        candidate = images_root / folder / name
        candidates = [candidate] if candidate.suffix else [candidate.with_suffix(ext) for ext in (".jpg", ".jpeg", ".png", ".webp")]
        found = next((path.resolve() for path in candidates if path.is_file()), None)
        if found and found not in result:
            result.append(found)
    return result


def row_has_image(row: dict[str, Any], images_root: Path = IMAGES_ROOT) -> bool:
    return bool(row_image_paths(row, images_root))


MAX_FAMILY_IMAGE_OPTIONS = 60
_content_hashes: dict[tuple[str, int, int], str] = {}


def image_content_hash(path: Path) -> str:
    """Huella del contenido, recordada mientras el archivo no cambie."""
    stats = path.stat()
    key = (str(path), stats.st_mtime_ns, stats.st_size)
    if key not in _content_hashes:
        _content_hashes[key] = hashlib.md5(path.read_bytes()).hexdigest()
    return _content_hashes[key]


def family_image_options(rows: list[dict[str, Any]], images_root: Path = IMAGES_ROOT) -> list[str]:
    """Fotos distintas de una familia, alternando colores.

    Cada talla tiene su propia copia de las mismas fotos, asi que se descartan
    por contenido. Se alterna entre colores (``CMP-BLA``, ``CMP-GRI``,
    ``CMP-NEG``) para que las primeras fotos no sean todas del mismo color.
    """
    by_color: dict[str, list[str]] = {}
    seen: set[str] = set()
    for row in rows:
        color = "-".join(str(row.get("sku") or "").upper().split("-")[:2]) or str(row.get("sku") or "")
        for image in row_image_paths(row, images_root):
            try:
                digest = image_content_hash(image)
            except OSError:
                continue
            if digest in seen:
                continue
            seen.add(digest)
            by_color.setdefault(color, []).append(str(image))
    ordered: list[str] = []
    queues = [list(images) for images in by_color.values()]
    while queues and len(ordered) < MAX_FAMILY_IMAGE_OPTIONS:
        for queue in queues:
            if queue:
                ordered.append(queue.pop(0))
        queues = [queue for queue in queues if queue]
    return ordered[:MAX_FAMILY_IMAGE_OPTIONS]


def catalog_families(
    selected: list[str] | None = None,
    inventory_path: Path = INVENTORY_PATH,
    images_root: Path = IMAGES_ROOT,
) -> list[dict[str, Any]]:
    rows = inventory_rows(inventory_path)
    requested = selected or list(ASSISTANT_PRESETS)
    result: list[dict[str, Any]] = []
    for family_key in requested:
        preset = ASSISTANT_PRESETS.get(family_key)
        if not preset:
            continue
        matches = [row for row in rows if preset_matches_row(preset, row)]
        valid = [row for row in matches if row_has_image(row, images_root)]
        family_images = family_image_options(valid, images_root)
        prices = [float(row["price"]) for row in valid if isinstance(row.get("price"), (int, float)) and float(row["price"]) > 0]
        result.append(
            {
                "key": family_key,
                "label": preset["label"],
                "category": preset.get("category") or "Men's clothing & shoes",
                "variant_count": len(matches),
                "valid_variant_count": len(valid),
                "price_from": min(prices) if prices else None,
                "eligible": bool(valid and prices),
                "reason": "" if valid and prices else "Sin variantes con precio y foto validos.",
                "image_options": family_images,
                "image_paths": family_images[:10],
            }
        )
    return result


def build_family_jobs(
    account: str,
    selected: list[str] | None = None,
    family_modes: dict[str, str] | None = None,
    campaign_path: Path | None = None,
) -> list[dict[str, Any]]:
    campaign_path = campaign_path or (SCRATCH_DIR / "marketplace_campaign_example.json")
    current = load_json(campaign_path, {"jobs": []})
    existing = {str(job.get("family_key") or ""): job for job in current.get("jobs", []) if job.get("family_key")}
    families = catalog_families(selected)
    jobs: list[dict[str, Any]] = []
    for family in families:
        if not family["eligible"]:
            continue
        key = family["key"]
        source = existing.get(key, {})
        style = (family_modes or {}).get(key) or source.get("autonomy_mode") or "auto"
        mode, group_by = STYLE_TO_MODE.get(style, STYLE_TO_MODE["auto"])
        preset = ASSISTANT_PRESETS[key]
        job: dict[str, Any] = {
            "name": f"{preset['label']} - automatico",
            "family_key": key,
            "enabled": True,
            "delay_minutes": 0,
            "item_interval_minutes": 0,
            "account": account,
            "listing_mode": mode,
            "source_excel": "ArticulosGenerados.xlsx",
            "images_root": "imagenes_firupost",
            "category": preset.get("category") or "Men's clothing & shoes",
        }
        if group_by:
            job["group_by"] = group_by
        for filter_key in ("sku_prefixes", "skus", "title_contains", "exclude_title_contains"):
            if preset.get(filter_key):
                job[filter_key] = preset[filter_key]
        overrides = dict(source.get("listing_overrides") or {})
        if not overrides.get("image_paths"):
            overrides["image_paths"] = family.get("image_paths") or []
        if overrides:
            job["listing_overrides"] = overrides
        jobs.append(job)
    return jobs

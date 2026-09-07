from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


SCRATCH_DIR = Path(__file__).resolve().parent
INVENTORY_PATH = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"

ASSISTANT_PRESETS: dict[str, dict[str, Any]] = {
    "compression_short": {
        "label": "Camisas manga corta de compresion",
        "sku_prefixes": ["TS"],
        "exclude_title_contains": ["sin mangas"],
        "category": "Men's clothing & shoes",
    },
    "compression_sleeveless": {
        "label": "Camisas sin mangas de compresion",
        "title_contains": ["sin mangas"],
        "category": "Men's clothing & shoes",
    },
    "leggins": {
        "label": "Leggins de campana",
        "title_contains": ["leggins"],
        "category": "Men's clothing & shoes",
    },
    "enterizos": {
        "label": "Enterizos de entrenamiento",
        "title_contains": ["enterizo"],
        "category": "Men's clothing & shoes",
    },
    "shorts": {
        "label": "Shorts deportivos",
        "title_contains": ["shorts"],
        "category": "Men's clothing & shoes",
    },
    "bolsos": {
        "label": "Bolsos deportivos",
        "title_contains": ["bolsos"],
        "category": "Sports & Outdoors",
    },
    "durags": {
        "label": "Durags",
        "title_contains": ["durags"],
        "category": "Men's clothing & shoes",
    },
    "munequeras": {
        "label": "Munequeras deportivas",
        "title_contains": ["muñequeras", "munequeras"],
        "category": "Sports & Outdoors",
    },
    "straps": {
        "label": "Straps para gimnasio",
        "title_contains": ["straps"],
        "category": "Sports & Outdoors",
    },
}

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
        family_images: list[str] = []
        for row in valid:
            for image in row_image_paths(row, images_root):
                if str(image) not in family_images:
                    family_images.append(str(image))
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

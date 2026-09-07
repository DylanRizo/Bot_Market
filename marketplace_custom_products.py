from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

import pandas as pd

from marketplace_ai_descriptions import generate_sales_description, local_product_tags, sanitize_tags
from marketplace_storage import MarketplaceStore


SCRATCH_DIR = Path(__file__).resolve().parent
CUSTOM_ROOT = SCRATCH_DIR / "marketplace_custom_products"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def create_custom_product(
    store: MarketplaceStore,
    payload: dict[str, Any],
    source_images: list[Path],
    custom_root: Path = CUSTOM_ROOT,
) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise ValueError("Escribe el nombre del producto.")
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError) as exc:
        raise ValueError("El precio debe ser un numero valido.") from exc
    if price <= 0:
        raise ValueError("El precio debe ser mayor que cero.")
    valid_images = [path.resolve() for path in source_images if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    if not valid_images:
        raise ValueError("Agrega al menos una foto valida.")
    if len(valid_images) > 10:
        valid_images = valid_images[:10]

    product_id = str(payload.get("id") or uuid.uuid4())
    product_root = custom_root / product_id
    images_root = product_root / "images"
    images_root.mkdir(parents=True, exist_ok=True)
    copied_images: list[Path] = []
    for index, source in enumerate(valid_images, start=1):
        destination = images_root / f"photo_{index}{source.suffix.lower()}"
        shutil.copy2(source, destination)
        copied_images.append(destination.resolve())

    category = str(payload.get("category") or "Sports & Outdoors").strip()
    condition = str(payload.get("condition") or "New").strip()
    location = str(payload.get("location") or "Managua, Nicaragua").strip()
    sku = str(payload.get("sku") or "").strip()
    tags = sanitize_tags(payload.get("tags") or local_product_tags({"titulo": title, "categoria": category}))
    description = str(payload.get("description") or "").strip()
    if not description:
        fallback = (
            f"{title} disponible para entrega.\n\n"
            "Ideal para uso diario. Escribeme para confirmar disponibilidad y coordinar la entrega."
        )
        description, _ = generate_sales_description(
            {"titulo": title, "precio": price, "ubicacion": location, "condicion": condition},
            fallback,
            enabled=False,
        )

    image_names = ";".join(path.name for path in copied_images)
    excel_path = product_root / "listing.xlsx"
    pd.DataFrame(
        [
            {
                "Titulo": title,
                "Precio": price,
                "Categoria": category,
                "Condicion": condition,
                "Descripcion": description,
                "Etiqueta": ";".join(tags),
                "Sku": sku,
                "Ubicacion": location,
                "CarpetaImg": "",
                "NombreImg": image_names,
            }
        ]
    ).to_excel(excel_path, index=False)

    family_key = f"custom:{product_id}"
    job = {
        "name": title,
        "family_key": family_key,
        "enabled": True,
        "delay_minutes": 0,
        "account": "",
        "excel": str(excel_path.resolve()),
        "images_root": str(images_root.resolve()),
        "row_index": 0,
        "category": category,
        "condition": condition,
        "listing_overrides": {
            "price": price,
            "image_paths": [str(path) for path in copied_images],
            "tags": tags,
        },
    }
    product = {
        "id": product_id,
        "title": title,
        "price": price,
        "description": description,
        "category": category,
        "condition": condition,
        "location": location,
        "sku": sku,
        "tags": tags,
        "image_paths": [str(path) for path in copied_images],
        "job": job,
        "active": bool(payload.get("active", True)),
        "include_in_rotation": bool(payload.get("include_in_rotation", True)),
    }
    store.save_custom_product(product)
    return next(item for item in store.list_custom_products() if item["id"] == product_id)


def custom_rotation_jobs(store: MarketplaceStore) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for product in store.list_custom_products(active_only=True):
        if not product.get("include_in_rotation"):
            continue
        job = dict(product.get("job") or {})
        if job:
            jobs.append(job)
    return jobs

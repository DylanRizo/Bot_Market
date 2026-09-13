"""Sincroniza el catalogo del SGI y las fotos de Drive con el bot.

Genera los mismos artefactos que el resto del pipeline ya consume:
``ArticulosGenerados.xlsx`` y ``imagenes_firupost/<SKU>/foto_N.ext``. Asi el
publicador, el calendario y la deduplicacion de fotos no cambian.

Reglas (decididas por el propietario):
- solo productos activos con stock, con el precio del SGI;
- si hay precios distintos entre bodegas o precio en revision, no se publica y
  se reporta;
- fotos de Drive por ``FAMILIA-COLOR``; si no hay, respaldo con las fotos locales
  del codigo anterior; si tampoco hay, no se publica y se reporta.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from drive_photos import DrivePhoto, product_group
from sgi_client import PRICE_ISSUE_LABELS, CatalogItem


SCRATCH_DIR = Path(__file__).resolve().parent
INVENTORY_PATH = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"
MAPPING_PATH = SCRATCH_DIR / "sgi_codigos_anteriores.json"
REPORT_PATH = SCRATCH_DIR / "sgi_sync_report.json"
DRIVE_INDEX_PATH = SCRATCH_DIR / "sgi_drive_index.json"

EXCEL_COLUMNS = ["Titulo", "Precio", "Categoria", "Condicion", "Descripcion", "Etiqueta", "Sku", "Ubicacion", "CarpetaImg", "NombreImg"]
LOCAL_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
DEFAULT_LOCATION = "Managua, Nicaragua"
MAX_PHOTOS = 10
STAGING_NAME = ".sgi-sync-tmp"

# Codigos de MKT-GUI-001 y de la plantilla de conteo fisico.
FAMILY_NAMES = {
    "BOL": "Bolso deportivo",
    "BSH": "Camisa Bershka",
    "CAL": "Calcetas de compresion",
    "CHA": "Chaleco de lana sin mangas",
    "CIN": "Cinturon de gimnasio",
    "CML": "Camisa de compresion manga larga",
    "CMP": "Camisa de compresion manga corta",
    "CSM": "Camisa sin mangas",
    "DUR": "Durag",
    "ENT": "Enterizo deportivo",
    "JOG": "Pantalon jogger",
    "LEG": "Leggins deportivos",
    "MAN": "Manga para brazo",
    "MOC": "Mochila antirrobo",
    "MUN": "Munequeras deportivas",
    "ROD": "Rodilleras deportivas",
    "SHT": "Short deportivo",
    "STR": "Straps para gimnasio",
    "TOP": "Top deportivo",
}
COLOR_NAMES = {
    "AMA": "Amarillo",
    "AZU": "Azul",
    "BEI": "Beige",
    "BLA": "Blanco",
    "CAF": "Cafe",
    "GRI": "Gris",
    "MUL": "Multicolor",
    "NEG": "Negro",
    "ROJ": "Rojo",
    "ROS": "Rosa",
    "VAR": "Varios colores",
    "VER": "Verde",
}
COMPRESSION_LEVELS = {"15": "15-20 mmHg", "30": "20-30 mmHg"}
# El constructor de anuncios reconoce XS..XXL, UNI y X como tallas. "2XL" se
# traduce a XXL; la talla unica se omite para que el titulo quede natural.
SIZE_TITLES = {"2XL": "XXL", "U": "", "UNI": "", "X": ""}


def listing_title(item: CatalogItem) -> str:
    """``Base - Color, Talla``, el formato que entienden parse_title y grouped_title."""
    parts = item.code.split("-")
    if len(parts) < 3 or parts[0] not in FAMILY_NAMES:
        return item.name.capitalize() or item.code
    family, color_code, size = parts[0], "-".join(parts[1:-1]), parts[-1]
    base = FAMILY_NAMES[family]
    level = re.fullmatch(r"(\d+)([A-Z]+)", color_code)
    if level:
        base = f"{base} {COMPRESSION_LEVELS.get(level.group(1), level.group(1))}"
        color_code = level.group(2)
    color = COLOR_NAMES.get(color_code, color_code.capitalize())
    size_text = SIZE_TITLES.get(size, size)
    variant = ", ".join(value for value in (color, size_text) if value)
    return f"{base} - {variant}" if variant else base


def load_mapping(path: Path = MAPPING_PATH) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(key).upper(): str(value).upper() for key, value in dict(data).items() if key and value}


def build_mapping_from_workbook(path: Path) -> dict[str, str]:
    """Lee SKU INTUITIVO -> CODIGO ANTERIOR de la plantilla de conteo fisico.

    La plantilla trae un titulo antes de los encabezados, asi que se busca la
    fila que los contiene en vez de suponer que es la primera.
    """
    raw = pd.read_excel(path, header=None, dtype=str).fillna("")
    for index, row in raw.iterrows():
        labels = [plain_label(value) for value in row.tolist()]
        if "sku intuitivo" in labels and "codigo anterior" in labels:
            new_column, old_column = labels.index("sku intuitivo"), labels.index("codigo anterior")
            mapping: dict[str, str] = {}
            for _, data in raw.iloc[index + 1 :].iterrows():
                new_code, old_code = str(data.iloc[new_column]).strip().upper(), str(data.iloc[old_column]).strip().upper()
                if new_code and old_code:
                    mapping[new_code] = old_code
            return mapping
    raise ValueError("La plantilla no tiene las columnas SKU INTUITIVO y CODIGO ANTERIOR.")


def plain_label(value: Any) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    return " ".join(text.lower().split())


def local_photos(old_code: str, images_root: Path = IMAGES_ROOT) -> list[Path]:
    folder = images_root / old_code
    if not folder.is_dir():
        return []
    photos = [path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in LOCAL_IMAGE_EXTENSIONS]
    return sorted(photos, key=lambda path: path.name)[:MAX_PHOTOS]


def _write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_sync(
    catalog: Iterable[CatalogItem],
    drive_groups: dict[str, list[DrivePhoto]],
    download: Callable[[DrivePhoto], Path],
    mapping: dict[str, str],
    *,
    categories: Callable[[str], str] | None = None,
    images_root: Path = IMAGES_ROOT,
    inventory_path: Path = INVENTORY_PATH,
    report_path: Path = REPORT_PATH,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    items = sorted(catalog, key=lambda item: item.code)
    report: dict[str, Any] = {
        "generated_at": now().isoformat(timespec="seconds"),
        "catalog_items": len(items),
        "publishable": [],
        "price_issues": [],
        "without_photos": [],
        "drive_photos": [],
        "local_photos": [],
    }
    rows: list[dict[str, Any]] = []
    images_root.mkdir(parents=True, exist_ok=True)
    staging = images_root / STAGING_NAME
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()

    try:
        for item in items:
            if item.total_quantity <= 0:
                continue
            if item.price_issue or item.unit_price is None:
                issue = item.price_issue or "MISSING"
                report["price_issues"].append(
                    {"code": item.code, "issue": issue, "detail": PRICE_ISSUE_LABELS.get(issue, issue)}
                )
                continue

            sources: list[Path] = [download(photo) for photo in drive_groups.get(product_group(item.code), [])]
            origin = "drive_photos" if sources else ""
            old_code = mapping.get(item.code)
            if not sources and old_code:
                sources = local_photos(old_code, images_root)
                origin = "local_photos" if sources else ""
            if not sources:
                report["without_photos"].append({"code": item.code, "old_code": old_code})
                continue

            folder = staging / item.code
            folder.mkdir()
            names: list[str] = []
            for index, source in enumerate(sources[:MAX_PHOTOS], start=1):
                name = f"foto_{index}"
                shutil.copy2(source, folder / f"{name}{source.suffix.lower()}")
                names.append(name)
            rows.append(
                {
                    "Titulo": listing_title(item),
                    "Precio": item.unit_price,
                    "Categoria": categories(item.code) if categories else "",
                    "Condicion": "Nuevo",
                    "Descripcion": item.description,
                    "Etiqueta": "",
                    "Sku": item.code,
                    "Ubicacion": DEFAULT_LOCATION,
                    "CarpetaImg": item.code,
                    "NombreImg": ";".join(names),
                }
            )
            report["publishable"].append(item.code)
            report[origin].append(item.code)

        # Las carpetas de fotos se reemplazan una por una, ya completas: el
        # trabajador nunca encuentra una carpeta a medio copiar.
        for folder in sorted(staging.iterdir()):
            target = images_root / folder.name
            if target.exists():
                shutil.rmtree(target)
            folder.replace(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    temporary_excel = inventory_path.with_name(inventory_path.stem + ".tmp.xlsx")
    pd.DataFrame(rows, columns=EXCEL_COLUMNS).to_excel(temporary_excel, index=False)
    os.replace(temporary_excel, inventory_path)
    _write_json_atomic(report_path, report)
    return report


def cached_drive_files(
    fetch: Callable[[], list[dict[str, Any]]],
    path: Path = DRIVE_INDEX_PATH,
    max_age_seconds: float = 3600,
    clock: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    """Listar Drive entero es lento: el indice se reutiliza durante una hora."""
    if path.is_file() and clock() - path.stat().st_mtime < max_age_seconds:
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, list):
                return cached
        except (OSError, ValueError):
            pass
    files = fetch()
    _write_json_atomic(path, files)
    return files


def revalidate_family(catalog: Iterable[CatalogItem], sku_prefixes: Iterable[str]) -> tuple[bool, str, str]:
    """Justo antes de publicar: ¿la familia sigue teniendo algo vendible en el SGI?"""
    prefixes = tuple(str(prefix).upper() for prefix in sku_prefixes if prefix)
    matching = [item for item in catalog if prefixes and item.code.startswith(prefixes)]
    publishable = [item for item in matching if item.publishable]
    if publishable:
        return True, "", f"{len(publishable)} variante(s) con stock y precio en el SGI."
    issues = [item for item in matching if item.total_quantity > 0 and item.price_issue]
    if issues:
        detail = PRICE_ISSUE_LABELS.get(str(issues[0].price_issue), str(issues[0].price_issue))
        return False, "PRICE_ISSUE", f"{detail}: {', '.join(item.code for item in issues[:5])}."
    return False, "OUT_OF_STOCK", "Sin stock en el SGI para este producto."


def revalidate_from_report(report: dict[str, Any], sku_prefixes: Iterable[str]) -> tuple[bool, str, str]:
    """Clasifica una familia con el reporte de la ultima sincronizacion.

    A diferencia de ``revalidate_family`` distingue un producto con stock pero sin
    foto de uno agotado: el primero pide trabajo a marketing, el segundo no.
    """
    prefixes = tuple(str(prefix).upper() for prefix in sku_prefixes if prefix)

    def matches(code: str) -> bool:
        return bool(prefixes) and str(code).upper().startswith(prefixes)

    publishable = [code for code in report.get("publishable", []) if matches(code)]
    if publishable:
        return True, "", f"{len(publishable)} variante(s) con stock, precio y foto."
    issues = [issue for issue in report.get("price_issues", []) if matches(issue.get("code", ""))]
    if issues:
        codes = ", ".join(issue["code"] for issue in issues[:5])
        return False, "PRICE_ISSUE", f"{issues[0].get('detail') or 'Precio dudoso en el SGI'}: {codes}."
    missing = [entry for entry in report.get("without_photos", []) if matches(entry.get("code", ""))]
    if missing:
        codes = ", ".join(entry["code"] for entry in missing[:5])
        return False, "NO_PHOTOS", f"Con stock pero sin fotos en Drive ni locales: {codes}."
    return False, "OUT_OF_STOCK", "Sin stock en el SGI para este producto."


def sync_from_local_configuration(*, refresh_drive: bool = False, authorize_drive: bool = False) -> dict[str, Any]:
    from drive_photos import DEFAULT_EXCLUDED_FOLDERS, DriveLibrary, group_photos
    from marketplace_catalog import category_for_sku
    from sgi_client import SgiClient, load_settings

    settings = load_settings()
    drive_settings = settings.get("drive") or {}
    catalog = SgiClient.from_local_configuration().catalog()
    library = DriveLibrary.connect(interactive=authorize_drive)
    roots = [str(folder) for folder in drive_settings.get("root_folder_ids") or []]
    excluded = drive_settings.get("excluded_folder_names") or list(DEFAULT_EXCLUDED_FOLDERS)
    max_age = 0 if refresh_drive else float((settings.get("sync") or {}).get("max_drive_index_age_minutes", 60)) * 60
    files = cached_drive_files(lambda: library.list_image_files(roots, excluded), max_age_seconds=max_age)
    return run_sync(catalog, group_photos(files), library.download, load_mapping(), categories=category_for_sku)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sincroniza el catalogo del SGI y las fotos de Drive con el bot.")
    parser.add_argument("--build-mapping", type=Path, help="Genera sgi_codigos_anteriores.json desde la plantilla de conteo.")
    parser.add_argument("--refresh-drive", action="store_true", help="Ignora el indice de Drive guardado.")
    parser.add_argument("--authorize-drive", action="store_true", help="Abre el navegador para autorizar Drive en solo lectura.")
    args = parser.parse_args()

    if args.build_mapping:
        mapping = build_mapping_from_workbook(args.build_mapping)
        _write_json_atomic(MAPPING_PATH, mapping)
        print(f"Equivalencias guardadas: {len(mapping)}")
        return

    report = sync_from_local_configuration(refresh_drive=args.refresh_drive, authorize_drive=args.authorize_drive)
    print(
        f"Catalogo del SGI: {report['catalog_items']} | publicables: {len(report['publishable'])} "
        f"(Drive {len(report['drive_photos'])}, locales {len(report['local_photos'])}) | "
        f"precio dudoso: {len(report['price_issues'])} | sin foto: {len(report['without_photos'])}"
    )


if __name__ == "__main__":
    main()

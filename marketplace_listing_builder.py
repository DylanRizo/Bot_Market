from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from PIL import Image

from marketplace_ai_descriptions import DEFAULT_MODEL, DEFAULT_TONE, generate_product_tags, generate_sales_description, sanitize_tags


SCRATCH_DIR = Path(__file__).resolve().parent
SIZE_ORDER = ["XS", "S", "M", "L", "XL", "XXL", "UNI", "X"]
SIZE_SET = set(SIZE_ORDER)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def text_value(value: Any) -> str:
    try:
        if pd.isna(value):
            return ""
    except TypeError:
        pass
    return "" if value is None else str(value).strip()


def split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def price_number(value: Any) -> float:
    text = text_value(value).replace("C$", "").replace("NIO", "").replace(",", "").strip()
    try:
        return float(text)
    except ValueError:
        return 0.0


def price_text(value: float) -> str:
    if not value:
        return "consultar"
    if float(value).is_integer():
        return f"C${int(value)}"
    return f"C${value:g}"


def clean_variant_text(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def parse_title(title: str) -> dict[str, str]:
    base = title.strip()
    variant = ""
    color = ""
    size = ""
    if " - " in title:
        base, variant = [part.strip() for part in title.rsplit(" - ", 1)]

    if variant:
        parts = [part.strip() for part in variant.split(",") if part.strip()]
        for part in parts:
            normalized = part.upper()
            if normalized in SIZE_SET:
                size = normalized
            elif not color:
                color = clean_variant_text(part)
        if len(parts) == 1 and not color and not size:
            color = clean_variant_text(parts[0])

    return {"base": base, "variant": variant, "color": color, "size": size}


def sort_sizes(values: list[str]) -> list[str]:
    unique = list(dict.fromkeys(v for v in values if v))
    return sorted(unique, key=lambda item: SIZE_ORDER.index(item) if item in SIZE_ORDER else len(SIZE_ORDER))


def join_human(values: list[str]) -> str:
    values = [value for value in dict.fromkeys(values) if value]
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    if len(values) == 2:
        return f"{values[0]} y {values[1]}"
    return ", ".join(values[:-1]) + f" y {values[-1]}"


def listing_intro(base: str) -> str:
    lower = base.lower()
    if "muñequera" in lower or "munequera" in lower:
        return "Muñequeras deportivas ideales para entrenar con mayor comodidad y soporte en rutinas de fuerza o gimnasio."
    if "strap" in lower:
        return "Straps para gimnasio ideales para mejorar el agarre en rutinas de espalda, peso muerto y remos."
    if "bolso" in lower:
        return f"{base} practico para llevar ropa, calzado y accesorios al gimnasio o a tus actividades diarias."
    if "durag" in lower:
        return f"{base} versatil para completar tu estilo y proteger el cabello durante el uso diario."
    if "enterizo" in lower:
        return f"{base} con estilo deportivo, ideal para entrenar o crear un outfit casual."
    if "short" in lower:
        return f"{base} comodos y versatiles para entrenar o usar en un look casual."
    if "leggin" in lower:
        return f"{base} con estilo deportivo y corte favorecedor."
    if "sin mangas" in lower:
        return f"{base} nueva, comoda para entrenar con libertad de movimiento."
    if "camisa de compresion" in lower or "compresión" in lower:
        return f"{base} nueva, perfecta para entrenar, usar bajo otra prenda o armar un outfit deportivo."
    return f"{base} nuevo, ideal para uso diario o para completar tu estilo."


def _image_candidates(folder_path: Path, image_name: str) -> list[Path]:
    raw = Path(image_name)
    if raw.is_absolute():
        return [raw]
    if image_name:
        exact = folder_path / image_name
        candidates = [exact]
        if exact.suffix.lower() not in IMAGE_EXTENSIONS:
            candidates.extend(folder_path / f"{image_name}{ext}" for ext in IMAGE_EXTENSIONS)
        return candidates
    return [path for path in folder_path.glob("*") if path.suffix.lower() in IMAGE_EXTENSIONS]


def _is_valid_image(candidate: Path) -> bool:
    if not candidate.exists() or candidate.stat().st_size < 1024:
        return False
    try:
        with Image.open(candidate) as image:
            image.verify()
        return True
    except Exception:
        return False


def validate_images(images_root: Path, folder: str, image_name: str, limit: int = 10) -> tuple[str, list[str]]:
    """Fotos validas de una fila. ``NombreImg`` puede traer varias separadas por ``;``
    (``foto_1;foto_2``), igual que las lee el publicador."""
    folder_path = images_root / folder
    names = [value.strip() for value in str(image_name or "").split(";") if value.strip()] or [""]
    valid: list[str] = []
    for name in names:
        found = next((candidate for candidate in _image_candidates(folder_path, name) if _is_valid_image(candidate)), None)
        if found and found.name not in valid:
            valid.append(found.name)
        if len(valid) >= limit:
            break
    if not valid:
        raise FileNotFoundError(f"No encontre imagen valida para {folder}/{image_name}")
    return folder, valid


def validate_image(images_root: Path, folder: str, image_name: str) -> tuple[str, str]:
    folder, names = validate_images(images_root, folder, image_name, limit=1)
    return folder, names[0]


def validated_override_images(value: str) -> list[str]:
    if not value:
        return []
    try:
        raw_paths = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Las fotos modificadas no tienen un formato valido.") from exc
    if not isinstance(raw_paths, list):
        raise ValueError("Las fotos modificadas deben ser una lista.")
    resolved: list[str] = []
    for raw_path in raw_paths[:10]:
        path = Path(str(raw_path)).expanduser().resolve()
        if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
            raise FileNotFoundError(f"Foto modificada no encontrada: {path}")
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            raise ValueError(f"La foto no es valida: {path.name}") from exc
        resolved.append(str(path))
    return resolved


def apply_image_override(listing: dict[str, Any], image_paths: list[str]) -> None:
    if not image_paths:
        return
    # The poster accepts absolute paths in NombreImg, so campaign photos can stay outside the master folder.
    listing["CarpetaImg"] = ""
    listing["NombreImg"] = ";".join(image_paths[:10])


def validated_override_tags(value: str) -> list[str]:
    if not value:
        return []
    try:
        tags = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("Las etiquetas modificadas no tienen un formato valido.") from exc
    if not isinstance(tags, list):
        raise ValueError("Las etiquetas deben ser una lista.")
    clean_tags = sanitize_tags(tags)
    if not clean_tags:
        raise ValueError("Agrega al menos una etiqueta valida.")
    return clean_tags


def apply_tags_override(listing: dict[str, Any], tags: list[str]) -> None:
    if tags:
        listing["Etiqueta"] = ";".join(tags)


def select_rows(df: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    mask = pd.Series(True, index=df.index)
    if args.row_indices:
        indices = [int(item) for item in split_csv(args.row_indices)]
        mask &= df.index.isin(indices)
    if args.skus:
        skus = set(split_csv(args.skus))
        mask &= df["Sku"].astype(str).isin(skus)
    if args.sku_prefixes:
        prefixes = tuple(split_csv(args.sku_prefixes))
        mask &= df["Sku"].astype(str).str.startswith(prefixes, na=False)
    if args.title_contains:
        terms = [term.lower() for term in split_csv(args.title_contains)]
        mask &= df["Titulo"].astype(str).str.lower().apply(lambda title: any(term in title for term in terms))
    if args.exclude_title_contains:
        terms = [term.lower() for term in split_csv(args.exclude_title_contains)]
        mask &= ~df["Titulo"].astype(str).str.lower().apply(lambda title: any(term in title for term in terms))

    selected = df[mask].copy()
    if args.max_items:
        selected = selected.head(args.max_items)
    if selected.empty:
        raise ValueError("La seleccion no encontro productos.")
    return selected


def public_description_for_single(row: pd.Series) -> str:
    title = text_value(row.get("Titulo"))
    parsed = parse_title(title)
    price = price_number(row.get("Precio"))
    location = text_value(row.get("Ubicacion")) or "Managua, Nicaragua"
    lines = [
        listing_intro(title),
    ]
    if parsed["size"]:
        lines.append(f"Talla: {parsed['size']}.")
    if parsed["color"] and parsed["color"].lower() not in title.lower().split(" - ")[0].lower():
        lines.append(f"Color: {parsed['color']}.")
    lines.extend(
        [
            f"Precio: {price_text(price)}.",
            f"Ubicacion: {location}.",
            "Escribeme para confirmar disponibilidad y coordinar la entrega.",
        ]
    )
    return "\n".join(line for line in lines if line)


def row_to_public_listing(
    row: pd.Series,
    images_root: Path,
    category: str,
    condition: str,
    *,
    ai_enabled: bool,
    ai_model: str,
    ai_tone: str,
) -> tuple[dict[str, Any], str]:
    folder, images = validate_images(images_root, text_value(row.get("CarpetaImg")), text_value(row.get("NombreImg")) or "foto_1")
    title = text_value(row.get("Titulo"))
    parsed = parse_title(title)
    price = price_number(row.get("Precio"))
    location = text_value(row.get("Ubicacion")) or "Managua, Nicaragua"
    fallback = public_description_for_single(row)
    description, source = generate_sales_description(
        {
            "titulo": title,
            "precio": price_text(price),
            "color": parsed["color"],
            "talla": parsed["size"],
            "ubicacion": location,
            "condicion": condition,
        },
        fallback,
        enabled=ai_enabled,
        model=ai_model,
        tone=ai_tone,
    )
    tags, _ = generate_product_tags(
        {
            "titulo": title,
            "categoria": category,
            "color": parsed["color"],
            "talla": parsed["size"],
            "uso": "entrenamiento y uso diario",
        },
        enabled=ai_enabled,
        model=ai_model,
    )
    listing = {
        "Titulo": text_value(row.get("Titulo")),
        "Precio": price,
        "Categoria": category,
        "Condicion": condition,
        "Descripcion": description,
        "Etiqueta": ";".join(tags) or text_value(row.get("Etiqueta")),
        "Sku": text_value(row.get("Sku")),
        "Ubicacion": location,
        "CarpetaImg": folder,
        "NombreImg": ";".join(images),
    }
    return listing, source


def group_key(row: pd.Series, mode: str) -> tuple[str, str]:
    parsed = parse_title(text_value(row.get("Titulo")))
    if mode == "grouped_by_color":
        return (parsed["base"], parsed["color"] or "Variantes")
    if mode == "grouped_by_size":
        return (parsed["base"], parsed["size"] or "Variantes")
    return ("grouped", "all")


def grouped_title(rows: pd.DataFrame, mode: str, fallback_name: str) -> str:
    parsed_rows = [parse_title(text_value(row.get("Titulo"))) for _, row in rows.iterrows()]
    bases = [parsed["base"] for parsed in parsed_rows if parsed["base"]]
    base = bases[0] if bases else fallback_name
    colors = join_human([parsed["color"] for parsed in parsed_rows])
    sizes = ", ".join(sort_sizes([parsed["size"] for parsed in parsed_rows]))

    if mode == "grouped_by_color" and colors:
        return f"{base} - {colors}" + (f" ({sizes})" if sizes else "")
    if mode == "grouped_by_size" and sizes:
        return f"{base} - Talla {sizes}" + (f" ({colors})" if colors else "")
    pieces = []
    if colors:
        pieces.append(colors)
    if sizes:
        pieces.append(f"({sizes})")
    return f"{base} - {' '.join(pieces)}".strip(" -") if pieces else fallback_name


def grouped_description(rows: pd.DataFrame, title: str) -> str:
    parsed_rows = [parse_title(text_value(row.get("Titulo"))) for _, row in rows.iterrows()]
    colors = join_human([parsed["color"] for parsed in parsed_rows])
    sizes = ", ".join(sort_sizes([parsed["size"] for parsed in parsed_rows]))
    prices = [price_number(row.get("Precio")) for _, row in rows.iterrows() if price_number(row.get("Precio"))]
    unique_prices = sorted(set(prices))
    location = text_value(rows.iloc[0].get("Ubicacion")) or "Managua, Nicaragua"
    base = parse_title(title)["base"] or title

    if len(unique_prices) <= 1:
        price_line = f"Precio: {price_text(unique_prices[0] if unique_prices else 0)} cada uno."
    else:
        price_line = f"Precio desde {price_text(unique_prices[0])}, segun variante."

    lines = [
        listing_intro(base),
    ]
    if colors:
        lines.append(f"Colores disponibles: {colors}.")
    if sizes:
        lines.append(f"Tallas disponibles: {sizes}.")
    lines.extend(
        [
            price_line,
            f"Ubicacion: {location}.",
            f"Escribeme para confirmar {' y '.join(value for value in ['color' if colors else '', 'talla' if sizes else ''] if value) or 'la variante'} y coordinar la entrega.",
        ]
    )
    return "\n".join(line for line in lines if line)


def group_to_public_listing(
    rows: pd.DataFrame,
    images_root: Path,
    category: str,
    condition: str,
    title: str,
    *,
    ai_enabled: bool,
    ai_model: str,
    ai_tone: str,
) -> tuple[dict[str, Any], str]:
    folders: list[str] = []
    images: list[str] = []
    for _, row in rows.iterrows():
        folder, image = validate_image(images_root, text_value(row.get("CarpetaImg")), text_value(row.get("NombreImg")) or "foto_1")
        if folder not in folders:
            folders.append(folder)
            images.append(image)
    price = min(price_number(row.get("Precio")) for _, row in rows.iterrows() if price_number(row.get("Precio")))
    parsed_rows = [parse_title(text_value(row.get("Titulo"))) for _, row in rows.iterrows()]
    colors = join_human([parsed["color"] for parsed in parsed_rows])
    sizes = ", ".join(sort_sizes([parsed["size"] for parsed in parsed_rows]))
    location = text_value(rows.iloc[0].get("Ubicacion")) or "Managua, Nicaragua"
    fallback = grouped_description(rows, title)
    description, source = generate_sales_description(
        {
            "titulo": title,
            "precio": price_text(price),
            "colores": colors,
            "tallas": sizes,
            "ubicacion": location,
            "condicion": condition,
        },
        fallback,
        enabled=ai_enabled,
        model=ai_model,
        tone=ai_tone,
    )
    tags, _ = generate_product_tags(
        {
            "titulo": title,
            "categoria": category,
            "colores": colors,
            "tallas": sizes,
            "uso": "entrenamiento y uso diario",
        },
        enabled=ai_enabled,
        model=ai_model,
    )
    listing = {
        "Titulo": title,
        "Precio": price,
        "Categoria": category,
        "Condicion": condition,
        "Descripcion": description,
        "Etiqueta": ";".join(tags),
        "Sku": "",
        "Ubicacion": location,
        "CarpetaImg": ";".join(folders[:10]),
        "NombreImg": ";".join(images[:10]),
    }
    return listing, source


def write_listing(output_dir: Path, name: str, rows: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")[:80] or "listing"
    path = output_dir / f"{safe}.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    return path


def build_jobs(args: argparse.Namespace) -> list[dict[str, Any]]:
    source_excel = args.source_excel.resolve()
    images_root = args.images_root.resolve()
    output_dir = args.output_dir.resolve()
    df = pd.read_excel(source_excel)
    selected = select_rows(df, args)
    category = args.category or "Men's clothing & shoes"
    condition = args.condition or "New"
    mode = args.mode
    ai_enabled = bool(args.ai_descriptions)
    ai_model = args.ai_model or DEFAULT_MODEL
    ai_tone = args.ai_tone or DEFAULT_TONE
    price_override = price_number(args.price_override) if args.price_override else 0.0
    override_images = validated_override_images(args.image_paths_json)
    override_tags = validated_override_tags(args.tags_json)

    jobs: list[dict[str, Any]] = []
    if mode == "individual":
        for _, row in selected.iterrows():
            if price_override:
                row = row.copy()
                row["Precio"] = price_override
            listing, description_source = row_to_public_listing(
                row,
                images_root,
                category,
                condition,
                ai_enabled=ai_enabled,
                ai_model=ai_model,
                ai_tone=ai_tone,
            )
            apply_image_override(listing, override_images)
            apply_tags_override(listing, override_tags)
            name = text_value(row.get("Sku")) or listing["Titulo"]
            excel = write_listing(output_dir, name, [listing])
            jobs.append(
                {
                    "name": listing["Titulo"],
                    "excel": str(excel),
                    "images_root": str(images_root),
                    "row_index": 0,
                    "description_source": description_source,
                }
            )
        return jobs

    grouped_sets: list[pd.DataFrame]
    if mode == "grouped":
        grouped_sets = [selected]
    elif mode in {"grouped_by_color", "grouped_by_size"}:
        grouped_sets = [group.copy() for _, group in selected.groupby(lambda idx: group_key(selected.loc[idx], mode), sort=False)]
    else:
        raise ValueError(f"Modo no soportado: {mode}")

    for index, group in enumerate(grouped_sets):
        if price_override:
            group = group.copy()
            group["Precio"] = price_override
        title = grouped_title(group, mode, args.name or f"listing_{index}")
        listing, description_source = group_to_public_listing(
            group,
            images_root,
            category,
            condition,
            title,
            ai_enabled=ai_enabled,
            ai_model=ai_model,
            ai_tone=ai_tone,
        )
        apply_image_override(listing, override_images)
        apply_tags_override(listing, override_tags)
        excel = write_listing(output_dir, title, [listing])
        jobs.append(
            {
                "name": title,
                "excel": str(excel),
                "images_root": str(images_root),
                "row_index": 0,
                "description_source": description_source,
            }
        )
    return jobs


def main() -> None:
    parser = argparse.ArgumentParser(description="Crea listings temporales individuales o agrupados para Marketplace.")
    parser.add_argument("--source-excel", type=Path, required=True)
    parser.add_argument("--images-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=SCRATCH_DIR / "marketplace_campaign_generated")
    parser.add_argument("--mode", choices=["individual", "grouped", "grouped_by_color", "grouped_by_size"], required=True)
    parser.add_argument("--name", default="")
    parser.add_argument("--row-indices", default="")
    parser.add_argument("--skus", default="")
    parser.add_argument("--sku-prefixes", default="")
    parser.add_argument("--title-contains", default="")
    parser.add_argument("--exclude-title-contains", default="")
    parser.add_argument("--max-items", type=int, default=0)
    parser.add_argument("--category", default="Men's clothing & shoes")
    parser.add_argument("--condition", default="New")
    parser.add_argument("--ai-descriptions", action="store_true")
    parser.add_argument("--ai-model", default=DEFAULT_MODEL)
    parser.add_argument("--ai-tone", default=DEFAULT_TONE)
    parser.add_argument("--price-override", default="")
    parser.add_argument("--image-paths-json", default="")
    parser.add_argument("--tags-json", default="")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    jobs = build_jobs(args)
    if args.json:
        print(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2))
    else:
        for job in jobs:
            print(f"{job['name']} -> {job['excel']}")


if __name__ == "__main__":
    main()

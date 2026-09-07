from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pandas as pd
from PIL import Image


SCRATCH_DIR = Path(__file__).resolve().parent
DEFAULT_SOURCE_EXCEL = SCRATCH_DIR / "ArticulosGenerados.xlsx"
DEFAULT_SOURCE_IMAGES = SCRATCH_DIR / "imagenes_firupost"
DEFAULT_OUTPUT_DIR = SCRATCH_DIR / "firupost_test_batch"
FIRUPOST_COLUMNS = [
    "Titulo",
    "Precio",
    "Categoria",
    "Condicion",
    "Descripcion",
    "Etiqueta",
    "Sku",
    "Ubicacion",
    "CarpetaImg",
    "NombreImg",
]


def validate_image(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.stat().st_size < 1024:
        raise ValueError(f"Imagen demasiado pequena: {path}")
    with Image.open(path) as img:
        img.verify()


def make_batch(
    source_excel: Path,
    source_images: Path,
    output_dir: Path,
    sku: str | None,
    limit: int,
    layout: str,
    category: str = "",
    condition: str = "",
) -> tuple[Path, Path, int]:
    df = pd.read_excel(source_excel)
    if sku:
        df = df[df["Sku"].astype(str).str.upper() == sku.upper()]
        if df.empty:
            raise ValueError(f"No encontre SKU {sku} en {source_excel}")
    df = df.head(limit).copy()
    if df.empty:
        raise ValueError("No hay productos para generar el lote.")

    images_out = output_dir / "imagenes"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    images_out.mkdir(parents=True)

    rows = []
    for _, row in df.iterrows():
        sku_value = str(row["Sku"]).strip()
        source_folder = source_images / str(row["CarpetaImg"]).strip()
        source_image = source_folder / f"{str(row['NombreImg']).strip()}.jpg"
        if not source_image.exists():
            # Fallback por extension si no era jpg.
            matches = list(source_folder.glob(f"{str(row['NombreImg']).strip()}.*"))
            if matches:
                source_image = matches[0]
        validate_image(source_image)

        next_row = row.to_dict()
        if category:
            next_row["Categoria"] = category
        if condition:
            next_row["Condicion"] = condition
        if layout in {"flat", "direct"}:
            target_stem = f"{sku_value}_{str(row['NombreImg']).strip()}".replace(" ", "_")
            target_image = images_out / f"{target_stem}{source_image.suffix.lower()}"
            shutil.copy2(source_image, target_image)
            next_row["CarpetaImg"] = "" if layout == "direct" else images_out.name
            next_row["NombreImg"] = target_image.name
        else:
            target_folder = images_out / str(row["CarpetaImg"]).strip()
            target_folder.mkdir(parents=True, exist_ok=True)
            target_image = target_folder / source_image.name
            shutil.copy2(source_image, target_image)
            next_row["CarpetaImg"] = target_folder.name
            next_row["NombreImg"] = source_image.name

        rows.append(next_row)

    out_excel = output_dir / "ArticulosGenerados_TEST.xlsx"
    pd.DataFrame(rows, columns=FIRUPOST_COLUMNS).to_excel(out_excel, index=False)
    return out_excel, images_out, len(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crea un lote pequeno para probar FiruPost sin publicar todo el inventario.")
    parser.add_argument("--source-excel", type=Path, default=DEFAULT_SOURCE_EXCEL)
    parser.add_argument("--source-images", type=Path, default=DEFAULT_SOURCE_IMAGES)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sku", default="")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--category", default="Men's clothing & shoes", help="Categoria de Facebook en ingles para el lote de prueba.")
    parser.add_argument("--condition", default="New", help="Condicion de Facebook en ingles para el lote de prueba.")
    parser.add_argument(
        "--layout",
        choices=["flat", "nested", "direct"],
        default="direct",
        help=(
            "flat: output/imagenes/sku.jpg y CarpetaImg=imagenes; "
            "nested: output/imagenes/SKU/foto_1.jpg; "
            "direct: output/imagenes/sku.jpg y CarpetaImg vacio."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    excel, images, count = make_batch(
        source_excel=args.source_excel,
        source_images=args.source_images,
        output_dir=args.output_dir,
        sku=args.sku or None,
        limit=args.limit,
        layout=args.layout,
        category=args.category,
        condition=args.condition,
    )
    print(f"Lote de prueba listo: {count} producto(s)")
    print(f"Excel: {excel}")
    print(f"Imagenes: {images}")
    print(f"Layout: {args.layout}")


if __name__ == "__main__":
    main()

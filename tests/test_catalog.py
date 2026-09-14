"""Pruebas de las fotos que ofrece cada familia del catalogo."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from marketplace_catalog import catalog_families


def crear_catalogo(tmp_path: Path, variantes: dict[str, list[bytes]]) -> tuple[Path, Path]:
    """Excel y carpetas de fotos como los que deja la sincronizacion con el SGI."""
    images = tmp_path / "imagenes"
    filas = []
    for sku, fotos in variantes.items():
        carpeta = images / sku
        carpeta.mkdir(parents=True)
        nombres = []
        for numero, contenido in enumerate(fotos, start=1):
            (carpeta / f"foto_{numero}.jpg").write_bytes(contenido)
            nombres.append(f"foto_{numero}.jpg")
        filas.append(
            {
                "Titulo": f"Camisa de compresion - {sku}",
                "Precio": 450,
                "Categoria": "Men's clothing & shoes",
                "Condicion": "New",
                "Descripcion": "",
                "Etiqueta": "",
                "Sku": sku,
                "Ubicacion": "Managua",
                "CarpetaImg": sku,
                "NombreImg": ";".join(nombres),
            }
        )
    excel = tmp_path / "ArticulosGenerados.xlsx"
    pd.DataFrame(filas).to_excel(excel, index=False)
    return excel, images


def test_las_copias_por_talla_no_llenan_las_fotos_y_aparecen_todos_los_colores(tmp_path):
    blancas = [b"blanca-1", b"blanca-2", b"blanca-3", b"blanca-4", b"blanca-5", b"blanca-6"]
    negras = [f"negra-{n}".encode() for n in range(1, 8)]
    excel, images = crear_catalogo(
        tmp_path,
        {
            # Cada talla trae su copia de las mismas fotos.
            "CMP-BLA-L": blancas,
            "CMP-BLA-M": blancas,
            "CMP-BLA-S": blancas,
            "CMP-GRI-M": [b"gris-1"],
            "CMP-NEG-L": negras,
            "CMP-NEG-S": negras,
        },
    )
    familia = catalog_families(["compression_short"], inventory_path=excel, images_root=images)[0]

    contenidos = [Path(path).read_bytes() for path in familia["image_options"]]
    assert len(contenidos) == len(set(contenidos)) == 14
    portada = [Path(path).parent.name for path in familia["image_paths"][:3]]
    assert portada == ["CMP-BLA-L", "CMP-GRI-M", "CMP-NEG-L"]
    colores = {Path(path).parent.name.rsplit("-", 1)[0] for path in familia["image_paths"]}
    assert colores == {"CMP-BLA", "CMP-GRI", "CMP-NEG"}
    assert len(familia["image_paths"]) == 10


def test_la_familia_ofrece_todas_las_fotos_distintas_aunque_pasen_de_diez(tmp_path):
    excel, images = crear_catalogo(tmp_path, {"CMP-NEG-L": [f"negra-{n}".encode() for n in range(1, 15)]})
    familia = catalog_families(["compression_short"], inventory_path=excel, images_root=images)[0]
    assert len(familia["image_options"]) == 14
    assert len(familia["image_paths"]) == 10

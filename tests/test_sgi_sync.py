"""Pruebas del sincronizador SGI + Drive contra el pipeline existente, sin red."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest

from drive_photos import group_photos
from marketplace_catalog import ASSISTANT_PRESETS, category_for_sku, preset_matches_row, row_image_paths
from marketplace_listing_builder import parse_title
from sgi_client import CatalogItem
from sgi_sync import (
    EXCEL_COLUMNS,
    build_mapping_from_workbook,
    cached_drive_files,
    listing_title,
    revalidate_family,
    run_sync,
)


def item(code: str, quantity: float = 3, price: float | None = 300.0, issue: str | None = None, name: str = "PRODUCTO") -> CatalogItem:
    return CatalogItem(code=code, name=name, description="", total_quantity=quantity, unit_price=price, price_issue=issue)


@pytest.mark.parametrize(
    "code, title, color, size",
    [
        ("CMP-NEG-M", "Camisa de compresion manga corta - Negro, M", "Negro", "M"),
        ("DUR-NEG-U", "Durag - Negro", "Negro", ""),
        ("CAL-15BEI-L", "Calcetas de compresion 15-20 mmHg - Beige, L", "Beige", "L"),
        ("CIN-NEG-2XL", "Cinturon de gimnasio - Negro, XXL", "Negro", "XXL"),
    ],
)
def test_el_titulo_respeta_el_formato_que_agrupa_el_constructor(code, title, color, size):
    assert listing_title(item(code)) == title
    parsed = parse_title(title)
    assert (parsed["color"], parsed["size"]) == (color, size)


def test_un_codigo_desconocido_usa_el_nombre_del_sgi():
    assert listing_title(item("XYZ-1", name="HUB USB DE 8 PUERTOS")) == "Hub usb de 8 puertos"


def test_los_presets_reconocen_el_sku_nuevo_sin_mezclar_familias():
    row = {"sku": "CMP-NEG-M", "title": ""}
    assert preset_matches_row(ASSISTANT_PRESETS["compression_short"], row)
    assert not preset_matches_row(ASSISTANT_PRESETS["compression_sleeveless"], row)
    assert not preset_matches_row(ASSISTANT_PRESETS["compression_long"], row)
    assert category_for_sku("BOL-NEG-U") == "Sports & Outdoors"
    assert category_for_sku("CMP-NEG-M") == "Men's clothing & shoes"


@pytest.fixture()
def workspace(tmp_path: Path):
    images = tmp_path / "imagenes_firupost"
    (images / "TSBL-S").mkdir(parents=True)
    (images / "TSBL-S" / "foto_1.jpg").write_bytes(b"foto local")
    cache = tmp_path / "cache"
    cache.mkdir()
    return images, cache, tmp_path


def fake_drive(cache: Path):
    files = [
        {"id": "1", "name": "CMP-NEG - Camisa negra - 001.JPG", "mimeType": "image/jpeg", "md5Checksum": "a"},
        {"id": "2", "name": "CMP-NEG - Camisa negra - 002.JPG", "mimeType": "image/jpeg", "md5Checksum": "b"},
    ]

    def download(photo):
        path = cache / f"{photo.md5}.jpg"
        path.write_bytes(photo.md5.encode())
        return path

    return group_photos(files), download


def test_genera_el_excel_y_las_carpetas_que_ya_consume_el_publicador(workspace):
    images, cache, base = workspace
    groups, download = fake_drive(cache)
    catalog = [
        item("CMP-NEG-M"),  # foto de Drive
        item("CMP-AZU-S"),  # sin foto en Drive, respaldo local por codigo anterior
        item("BOL-NEG-U"),  # sin foto en ningun lado
        item("DUR-NEG-U", price=None, issue="REVIEW"),  # precio en revision
        item("CHA-GRI-S", quantity=0),  # agotado
    ]
    report = run_sync(
        catalog,
        groups,
        download,
        {"CMP-AZU-S": "TSBL-S", "BOL-NEG-U": "CBBK-UNI"},
        categories=category_for_sku,
        images_root=images,
        inventory_path=base / "ArticulosGenerados.xlsx",
        report_path=base / "sgi_sync_report.json",
    )

    assert report["publishable"] == ["CMP-AZU-S", "CMP-NEG-M"]
    assert report["drive_photos"] == ["CMP-NEG-M"]
    assert report["local_photos"] == ["CMP-AZU-S"]
    assert report["without_photos"] == [{"code": "BOL-NEG-U", "old_code": "CBBK-UNI"}]
    assert [issue["code"] for issue in report["price_issues"]] == ["DUR-NEG-U"]

    frame = pd.read_excel(base / "ArticulosGenerados.xlsx")
    assert list(frame.columns) == EXCEL_COLUMNS
    rows = frame.set_index("Sku")
    assert rows.loc["CMP-NEG-M", "Precio"] == 300.0
    assert rows.loc["CMP-NEG-M", "Titulo"] == "Camisa de compresion manga corta - Negro, M"
    assert rows.loc["CMP-NEG-M", "Categoria"] == "Men's clothing & shoes"

    # El resto del pipeline encuentra las fotos con su propia funcion.
    negra = {"image_folder": "CMP-NEG-M", "image_name": rows.loc["CMP-NEG-M", "NombreImg"]}
    assert len(row_image_paths(negra, images)) == 2
    azul = {"image_folder": "CMP-AZU-S", "image_name": rows.loc["CMP-AZU-S", "NombreImg"]}
    assert row_image_paths(azul, images)[0].read_bytes() == b"foto local"

    # La carpeta del codigo anterior sigue intacta, y no quedan temporales.
    assert (images / "TSBL-S" / "foto_1.jpg").is_file()
    assert not (images / ".sgi-sync-tmp").exists()
    assert not list(base.glob("*.tmp*"))
    assert json.loads((base / "sgi_sync_report.json").read_text(encoding="utf-8"))["catalog_items"] == 5


def test_reemplaza_las_fotos_de_una_sincronizacion_anterior(workspace):
    images, cache, base = workspace
    stale = images / "CMP-NEG-M"
    stale.mkdir()
    (stale / "foto_9.jpg").write_bytes(b"vieja")
    groups, download = fake_drive(cache)
    run_sync([item("CMP-NEG-M")], groups, download, {}, images_root=images, inventory_path=base / "a.xlsx", report_path=base / "r.json")
    assert sorted(path.name for path in stale.iterdir()) == ["foto_1.jpg", "foto_2.jpg"]


def test_lee_las_equivalencias_aunque_la_plantilla_traiga_un_titulo(tmp_path):
    workbook = tmp_path / "plantilla.xlsx"
    pd.DataFrame(
        [
            ["Conteo Fisico Inicial", "", ""],
            ["SKU INTUITIVO", "CÓDIGO ANTERIOR", "NOMBRE DEL PRODUCTO"],
            ["CMP-NEG-M", "TSBK-M", "CAMISA"],
            ["", "", "TOTAL GENERAL PRENDAS FÍSICAS:"],
        ]
    ).to_excel(workbook, header=False, index=False)
    assert build_mapping_from_workbook(workbook) == {"CMP-NEG-M": "TSBK-M"}


def test_el_indice_de_drive_se_reutiliza_mientras_esta_fresco(tmp_path):
    index = tmp_path / "indice.json"
    calls = []

    def fetch():
        calls.append(1)
        return [{"name": f"archivo {len(calls)}"}]

    assert cached_drive_files(fetch, index, max_age_seconds=3600) == [{"name": "archivo 1"}]
    assert cached_drive_files(fetch, index, max_age_seconds=3600) == [{"name": "archivo 1"}]
    assert len(calls) == 1
    old = time.time() - 7200
    os.utime(index, (old, old))
    assert cached_drive_files(fetch, index, max_age_seconds=3600) == [{"name": "archivo 2"}]


def test_revalidacion_antes_de_publicar():
    catalog = [item("CMP-NEG-M"), item("CMP-BLA-M", price=None, issue="MIXED"), item("DUR-NEG-U", quantity=0)]
    assert revalidate_family(catalog, ["CMP-"])[0] is True
    ok, code, detail = revalidate_family([item("CMP-BLA-M", price=None, issue="MIXED")], ["CMP-"])
    assert (ok, code) == (False, "PRICE_ISSUE") and "CMP-BLA-M" in detail
    assert revalidate_family(catalog, ["DUR-"])[:2] == (False, "OUT_OF_STOCK")
    assert revalidate_family(catalog, ["BOL-"])[:2] == (False, "OUT_OF_STOCK")

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
    group_label,
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


def test_las_calcetas_multicolor_no_son_de_compresion_ni_van_con_ellas():
    assert listing_title(item("CAL-MUL-U")) == "Calcetas - Multicolor"
    multicolor = {"sku": "CAL-MUL-U", "title": ""}
    compresion = {"sku": "CAL-15NEG-M", "title": ""}
    assert preset_matches_row(ASSISTANT_PRESETS["calcetas_multicolor"], multicolor)
    assert not preset_matches_row(ASSISTANT_PRESETS["calcetas"], multicolor)
    assert preset_matches_row(ASSISTANT_PRESETS["calcetas"], compresion)


def test_un_codigo_desconocido_usa_el_nombre_del_sgi():
    assert listing_title(item("XYZ-1", name="HUB USB DE 8 PUERTOS")) == "Hub usb de 8 puertos"


def test_los_presets_reconocen_el_sku_nuevo_sin_mezclar_familias():
    row = {"sku": "CMP-NEG-M", "title": ""}
    assert preset_matches_row(ASSISTANT_PRESETS["compression_short"], row)
    assert not preset_matches_row(ASSISTANT_PRESETS["compression_sleeveless"], row)
    assert not preset_matches_row(ASSISTANT_PRESETS["compression_long"], row)
    assert category_for_sku("BOL-NEG-U") == "Bags & Luggage"
    assert category_for_sku("CMP-NEG-M") == "Men's clothing & shoes"


@pytest.fixture()
def workspace(tmp_path: Path):
    images = tmp_path / "imagenes_productos"
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

    # Las fotos quedan en la carpeta del color y el pipeline las encuentra.
    assert rows.loc["CMP-NEG-M", "CarpetaImg"] == "CMP-NEG"
    negra = {"image_folder": rows.loc["CMP-NEG-M", "CarpetaImg"], "image_name": rows.loc["CMP-NEG-M", "NombreImg"]}
    assert len(row_image_paths(negra, images)) == 2
    azul = {"image_folder": rows.loc["CMP-AZU-S", "CarpetaImg"], "image_name": rows.loc["CMP-AZU-S", "NombreImg"]}
    assert row_image_paths(azul, images)[0].read_bytes() == b"foto local"

    # La carpeta del codigo anterior sigue intacta, y no quedan temporales.
    assert (images / "TSBL-S" / "foto_1.jpg").is_file()
    assert not (images / ".sgi-sync-tmp").exists()
    assert not list(base.glob("*.tmp*"))
    assert json.loads((base / "sgi_sync_report.json").read_text(encoding="utf-8"))["catalog_items"] == 5


def test_las_fotos_locales_por_codigo_nuevo_tienen_prioridad_y_no_se_borran(workspace):
    images, cache, base = workspace
    locales = images / "_fotos_locales"
    (locales / "CMP-AZU").mkdir(parents=True)
    (locales / "CMP-AZU" / "CMP-AZU - Camisa azul - 001.jpg").write_bytes(b"consolidada")
    # Va por el numero, no por la descripcion ("Camisa a..." quedaria primero).
    (locales / "CMP-AZU" / "CMP-AZU - Camisa a la medida - 002.jpg").write_bytes(b"segunda")
    # Un producto sin codigo anterior tambien puede usar sus fotos locales.
    (locales / "CAL-MUL").mkdir()
    (locales / "CAL-MUL" / "CAL-MUL - Calcetas - 001.jpg").write_bytes(b"calcetas")
    report = run_sync(
        [item("CMP-AZU-S"), item("CAL-MUL-U")], {}, lambda photo: None, {"CMP-AZU-S": "TSBL-S"},
        images_root=images, inventory_path=base / "a.xlsx", report_path=base / "r.json",
    )
    assert report["local_photos"] == ["CAL-MUL-U", "CMP-AZU-S"]
    assert (images / "CMP-AZU" / "foto_1.jpg").read_bytes() == b"consolidada"
    assert (images / "CMP-AZU" / "foto_2.jpg").read_bytes() == b"segunda"
    assert (images / "CAL-MUL" / "foto_1.jpg").read_bytes() == b"calcetas"
    # Las fotos propias quedan como estaban: la sincronizacion solo copia.
    assert (locales / "CMP-AZU" / "CMP-AZU - Camisa azul - 001.jpg").is_file()


def test_reemplaza_las_fotos_de_una_sincronizacion_anterior(workspace):
    images, cache, base = workspace
    stale = images / "CMP-NEG"
    stale.mkdir()
    (stale / "foto_9.jpg").write_bytes(b"vieja")
    groups, download = fake_drive(cache)
    run_sync([item("CMP-NEG-M")], groups, download, {}, images_root=images, inventory_path=base / "a.xlsx", report_path=base / "r.json")
    assert sorted(path.name for path in stale.iterdir()) == ["foto_1.jpg", "foto_2.jpg"]


def test_todas_las_tallas_de_un_color_comparten_una_sola_carpeta(workspace):
    images, cache, base = workspace
    # Una sincronizacion anterior dejaba una copia por talla.
    (images / "CMP-NEG-M").mkdir()
    (images / "CMP-NEG-M" / "foto_1.jpg").write_bytes(b"copia por talla")
    groups, download = fake_drive(cache)
    report = run_sync(
        [item("CMP-NEG-M"), item("CMP-NEG-L"), item("CMP-NEG-S")],
        groups,
        download,
        {},
        images_root=images,
        inventory_path=base / "a.xlsx",
        report_path=base / "r.json",
    )

    assert report["publishable"] == ["CMP-NEG-L", "CMP-NEG-M", "CMP-NEG-S"]
    assert report["drive_photos"] == ["CMP-NEG-L", "CMP-NEG-M", "CMP-NEG-S"]
    frame = pd.read_excel(base / "a.xlsx")
    assert set(frame["CarpetaImg"]) == {"CMP-NEG"}
    assert sorted(path.name for path in (images / "CMP-NEG").iterdir()) == ["foto_1.jpg", "foto_2.jpg"]
    assert not (images / "CMP-NEG-M").exists()
    assert (images / "TSBL-S" / "foto_1.jpg").is_file()


@pytest.mark.parametrize(
    "group, label",
    [("CMP-BLA", "Blanco"), ("CSM-NEG", "Negro"), ("CAL-15BEI", "Beige 15-20 mmHg"), ("TSBK", "TSBK")],
)
def test_nombre_legible_del_color_de_las_fotos(group, label):
    assert group_label(group) == label


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


# --- sin autorizacion de Drive -------------------------------------------------

def test_sin_drive_usa_las_fotos_ya_descargadas_y_las_carpetas_actuales(workspace):
    from drive_photos import offline_download

    images, cache, base = workspace
    groups, _ = fake_drive(cache)
    (cache / "a.jpg").write_bytes(b"ya descargada")
    (images / "BOL-NEG").mkdir()
    (images / "BOL-NEG" / "foto_1.jpg").write_bytes(b"foto preparada antes")
    report = run_sync(
        [item("CMP-NEG-M"), item("BOL-NEG-U"), item("SHT-GRI-L")],
        groups,
        offline_download(cache),
        {},
        images_root=images,
        inventory_path=base / "ArticulosGenerados.xlsx",
        report_path=base / "sgi_sync_report.json",
        drive_offline="Falta autorizar Drive",
    )
    assert report["drive_offline"] == "Falta autorizar Drive"
    assert report["publishable"] == ["BOL-NEG-U", "CMP-NEG-M"]
    assert [entry["code"] for entry in report["without_photos"]] == ["SHT-GRI-L"]
    assert (images / "CMP-NEG" / "foto_1.jpg").read_bytes() == b"ya descargada"
    assert not (images / "CMP-NEG" / "foto_2.jpg").exists(), "la foto que nunca se bajo no aparece"
    assert (images / "BOL-NEG" / "foto_1.jpg").read_bytes() == b"foto preparada antes"


def test_la_sincronizacion_sigue_si_drive_pierde_el_permiso(monkeypatch, workspace):
    import drive_photos
    import sgi_client
    import sgi_sync

    images, cache, base = workspace
    monkeypatch.setattr(sgi_client, "load_settings", lambda: {"sgi": {"base_url": "https://sgi.test"}, "drive": {}})

    class Cliente:
        @classmethod
        def from_local_configuration(cls):
            return cls()

        def catalog(self):
            return [item("BOL-NEG-U")]

    def sin_permiso(**kwargs):
        raise drive_photos.DriveNotAuthorized("Falta autorizar Drive")

    monkeypatch.setattr(sgi_client, "SgiClient", Cliente)
    monkeypatch.setattr(drive_photos.DriveLibrary, "connect", staticmethod(sin_permiso))
    monkeypatch.setattr(sgi_sync, "load_cached_drive_index", lambda: [])
    monkeypatch.setattr(sgi_sync, "IMAGES_ROOT", images)
    monkeypatch.setattr(sgi_sync, "INVENTORY_PATH", base / "ArticulosGenerados.xlsx")
    monkeypatch.setattr(sgi_sync, "REPORT_PATH", base / "sgi_sync_report.json")
    monkeypatch.setattr(sgi_sync, "load_mapping", lambda: {})
    (images / "BOL-NEG").mkdir()
    (images / "BOL-NEG" / "foto_1.jpg").write_bytes(b"foto preparada antes")

    original = sgi_sync.run_sync

    def run_sync_local(*args, **kwargs):
        kwargs.update(images_root=images, inventory_path=base / "ArticulosGenerados.xlsx",
                      report_path=base / "sgi_sync_report.json")
        return original(*args, **kwargs)

    monkeypatch.setattr(sgi_sync, "run_sync", run_sync_local)
    report = sgi_sync.sync_from_local_configuration()
    assert report["drive_offline"]
    assert report["publishable"] == ["BOL-NEG-U"]

    with pytest.raises(drive_photos.DriveNotAuthorized):
        sgi_sync.sync_from_local_configuration(authorize_drive=True)

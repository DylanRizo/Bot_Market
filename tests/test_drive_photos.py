"""Pruebas de la biblioteca de fotos de Drive, con un servicio falso."""
from __future__ import annotations

import io

import pytest

from drive_photos import DriveLibrary, group_photos, parse_photo, product_group


def drive_file(name: str, md5: str = "", file_id: str = "", mime: str = "image/jpeg") -> dict:
    return {"id": file_id or f"id-{abs(hash(name))}", "name": name, "mimeType": mime, "md5Checksum": md5 or f"md5-{abs(hash(name))}"}


@pytest.mark.parametrize(
    "code, group",
    [("CMP-NEG", "CMP-NEG"), ("CMP-NEG-M", "CMP-NEG"), ("DUR-NEG-U", "DUR-NEG"), ("HUB-8P-U", "HUB-8P"), ("CAL-15BEI", "CAL-15BEI"), ("CIN-NEG-2XL", "CIN-NEG")],
)
def test_el_grupo_ignora_la_talla(code, group):
    assert product_group(code) == group


@pytest.mark.parametrize(
    "name",
    [
        "CMP-NEG - Camisa de compresion manga corta negra - 001.JPG",
        "CMP-BLA - Camisa de compresion manga corta blanca en gimnasio - 001.JPG",
        "CMP-NEG - Demostracion camisa de compresion manga corta negra - 001.jpg",
        "BSH-NEG - Camisa Bershka negra estampado rojo - 002.HEIC",
        "DUR-NEG-U - Durag negro - 002.JPG",
    ],
)
def test_acepta_fotografias_de_producto(name):
    assert parse_photo(drive_file(name)) is not None


@pytest.mark.parametrize(
    "name",
    [
        "CMP-NEG - Publicidad camisa de compresion manga corta negra - 001.jpg",
        "CMP-NEG - Infografia camisa de compresion manga corta negra - 001.JPG",
        "CMP-NEG - Infografía camisa de compresion - 001.JPG",
        "BSH-VAR - Inventario camisas Bershka surtidas - 003.HEIC",
        "CAL-15-VAR - Empaque calcetas de compresion 15-20 mmHg - 001.JPG",
        "MKT-MUL - Publicidad descuentos de temporada multiproducto - 001.jpg",
        "CMP-VAR - Camisas de compresion surtidas - 002.jpg",
        "IMG_8827.JPG",
        "CMP-NEG - Video camisa negra - 001.MOV",
    ],
)
def test_descarta_lo_que_no_es_foto_de_producto(name):
    assert parse_photo(drive_file(name)) is None


def test_sin_huella_md5_no_se_puede_deduplicar_y_se_descarta():
    file = drive_file("CMP-NEG - Camisa negra - 001.JPG")
    file["md5Checksum"] = ""
    assert parse_photo(file) is None


def test_agrupa_ordena_por_numero_y_quita_copias_por_contenido():
    files = [
        drive_file("CMP-NEG - Camisa negra - 002.JPG", md5="b"),
        drive_file("CMP-NEG - Camisa negra - 001.JPG", md5="a"),
        # La misma imagen copiada en la carpeta "Pagina web" con otro numero.
        drive_file("CMP-NEG - Camisa negra - 007.JPG", md5="b"),
        drive_file("DUR-NEG-U - Durag negro - 001.JPG", md5="c"),
    ]
    groups = group_photos(files)
    assert [photo.number for photo in groups["CMP-NEG"]] == [1, 2]
    assert [photo.md5 for photo in groups["DUR-NEG"]] == ["c"]


def test_limita_a_diez_fotos_por_grupo():
    files = [drive_file(f"CMP-NEG - Camisa negra - {n:03d}.JPG", md5=f"m{n}") for n in range(1, 15)]
    assert len(group_photos(files)["CMP-NEG"]) == 10


class FakeRequest:
    def __init__(self, response: dict) -> None:
        self._response = response

    def execute(self) -> dict:
        return self._response


class FakeFiles:
    def __init__(self, tree: dict[str, list[list[dict]]]) -> None:
        self.tree = tree
        self.queried: list[str] = []

    def list(self, q, fields, pageSize, pageToken=None, supportsAllDrives=True, includeItemsFromAllDrives=True):
        folder = q.split("'")[1]
        self.queried.append(folder)
        pages = self.tree.get(folder, [[]])
        index = int(pageToken or 0)
        response: dict = {"files": pages[index]}
        if index + 1 < len(pages):
            response["nextPageToken"] = str(index + 1)
        return FakeRequest(response)

    def get_media(self, fileId):
        raise AssertionError("no deberia descargar")


class FakeService:
    def __init__(self, tree: dict) -> None:
        self._files = FakeFiles(tree)

    def files(self) -> FakeFiles:
        return self._files


def folder(name: str, folder_id: str) -> dict:
    return {"id": folder_id, "name": name, "mimeType": "application/vnd.google-apps.folder"}


def test_recorre_subcarpetas_y_paginas_y_salta_los_flyers():
    service = FakeService(
        {
            "raizImagenes01": [
                [folder("Camisas de compresión ", "carpetaCamisas1"), folder("Flyers publicitarios ", "carpetaFlyers01")],
                [drive_file("CMP-NEG - Camisa negra - 003.JPG")],
            ],
            "carpetaCamisas1": [[drive_file("CMP-NEG - Camisa negra - 001.JPG"), drive_file("clip.mov", mime="video/quicktime")]],
            "carpetaFlyers01": [[drive_file("CMP-NEG - Publicidad camisa - 001.jpg")]],
        }
    )
    files = DriveLibrary(service).list_image_files(["raizImagenes01"])
    assert sorted(file["name"] for file in files) == ["CMP-NEG - Camisa negra - 001.JPG", "CMP-NEG - Camisa negra - 003.JPG"]
    assert "carpetaFlyers01" not in service.files().queried


def test_no_arma_consultas_con_ids_de_carpeta_sospechosos():
    service = FakeService({})
    DriveLibrary(service).list_image_files(["x' or '1'='1"])
    assert service.files().queried == []


def test_una_foto_ya_descargada_no_se_vuelve_a_pedir(tmp_path):
    photo = parse_photo(drive_file("CMP-NEG - Camisa negra - 001.JPG", md5="abc"))
    cached = tmp_path / "abc.jpg"
    cached.write_bytes(b"foto")
    assert DriveLibrary(FakeService({}), cache_dir=tmp_path).download(photo) == cached


def test_convierte_heic_a_jpeg():
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image

    from drive_photos import convert_heif_to_jpeg

    source = io.BytesIO()
    try:
        pillow_heif.from_pillow(Image.new("RGB", (64, 64), "red")).save(source, quality=80)
    except Exception as exc:  # el paquete puede venir sin codificador HEIF
        pytest.skip(f"sin codificador HEIF en este equipo: {exc}")
    jpeg = convert_heif_to_jpeg(source.getvalue())
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        assert image.size == (64, 64)

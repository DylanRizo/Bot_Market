"""Pruebas de la redaccion de anuncios: titulos, tallas, precios e imagenes."""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from marketplace_listing_builder import (
    parse_title,
    price_number,
    price_text,
    sort_sizes,
    validate_image,
    validate_images,
    validated_override_tags,
)


@pytest.mark.parametrize(
    "titulo, esperado",
    [
        ("Durag negro", {"base": "Durag negro", "color": "", "size": ""}),
        ("Camisa de compresion - Negro, L", {"base": "Camisa de compresion", "color": "Negro", "size": "L"}),
        ("Leggins - Gris", {"base": "Leggins", "color": "Gris", "size": ""}),
    ],
)
def test_parse_title_separa_base_color_y_talla(titulo, esperado):
    resultado = parse_title(titulo)
    for clave, valor in esperado.items():
        assert resultado[clave] == valor


def test_sort_sizes_ordena_por_talla_y_no_alfabeticamente():
    assert sort_sizes(["XL", "S", "L", "M"]) == ["S", "M", "L", "XL"]


def test_sort_sizes_elimina_repetidas_y_vacias():
    assert sort_sizes(["M", "M", "", "S"]) == ["S", "M"]


@pytest.mark.parametrize(
    "entrada, esperado",
    [("C$450", 450.0), ("1,200", 1200.0), ("450 NIO", 450.0), ("", 0.0), ("texto", 0.0)],
)
def test_price_number_limpia_el_formato(entrada, esperado):
    assert price_number(entrada) == esperado


def test_price_text_formatea_para_el_anuncio():
    assert price_text(450.0) == "C$450"
    assert price_text(450.5) == "C$450.5"
    assert price_text(0) == "consultar"


def test_validate_image_acepta_una_foto_real(tmp_path: Path):
    carpeta = tmp_path / "DGBK-X"
    carpeta.mkdir()
    destino = carpeta / "foto_1.jpg"
    Image.new("RGB", (600, 600), "black").save(destino, quality=95)
    assert destino.stat().st_size >= 1024
    assert validate_image(tmp_path, "DGBK-X", "foto_1.jpg") == ("DGBK-X", "foto_1.jpg")


def test_validate_image_rechaza_un_archivo_corrupto(tmp_path: Path):
    carpeta = tmp_path / "DGBK-X"
    carpeta.mkdir()
    (carpeta / "foto_1.jpg").write_bytes(b"esto no es una imagen" * 100)
    with pytest.raises(FileNotFoundError):
        validate_image(tmp_path, "DGBK-X", "foto_1.jpg")


def test_validate_image_falla_si_no_hay_nada(tmp_path: Path):
    (tmp_path / "VACIO").mkdir()
    with pytest.raises(FileNotFoundError):
        validate_image(tmp_path, "VACIO", "")


def fotos_de_color(tmp_path: Path, carpeta: str, cantidad: int) -> Path:
    destino = tmp_path / carpeta
    destino.mkdir()
    for numero in range(1, cantidad + 1):
        Image.new("RGB", (600, 600), (numero * 20, 0, 0)).save(destino / f"foto_{numero}.jpg", quality=95)
    return destino


def test_validate_images_lee_varias_fotos_como_las_deja_el_sgi(tmp_path: Path):
    """La sincronizacion escribe todas las fotos del color en una fila: foto_1;foto_2;..."""
    fotos_de_color(tmp_path, "CMP-BLA", 6)
    nombres = ";".join(f"foto_{numero}" for numero in range(1, 7))
    assert validate_images(tmp_path, "CMP-BLA", nombres) == ("CMP-BLA", [f"foto_{numero}.jpg" for numero in range(1, 7)])
    # El anuncio agrupado usa una sola foto por color.
    assert validate_image(tmp_path, "CMP-BLA", nombres) == ("CMP-BLA", "foto_1.jpg")


def test_validate_images_salta_las_que_no_sirven_y_respeta_el_limite(tmp_path: Path):
    carpeta = fotos_de_color(tmp_path, "CMP-NEG", 3)
    (carpeta / "foto_9.jpg").write_bytes(b"esto no es una imagen" * 100)
    assert validate_images(tmp_path, "CMP-NEG", "foto_9;foto_1;foto_2;foto_3", limit=2) == ("CMP-NEG", ["foto_1.jpg", "foto_2.jpg"])


def test_validate_images_falla_si_ninguna_foto_sirve(tmp_path: Path):
    (tmp_path / "CMP-GRI").mkdir()
    with pytest.raises(FileNotFoundError):
        validate_images(tmp_path, "CMP-GRI", "foto_1;foto_2")


def test_validated_override_tags_acepta_json_y_limpia():
    etiquetas = validated_override_tags('["durag", "", "seda", "negro"]')
    assert "" not in etiquetas
    assert len(etiquetas) == len(set(etiquetas)), "no debe repetir etiquetas"


def test_validated_override_tags_rechaza_entradas_invalidas():
    assert validated_override_tags("") == []
    with pytest.raises(ValueError, match="formato valido"):
        validated_override_tags("durag, seda")
    with pytest.raises(ValueError, match="lista"):
        validated_override_tags('{"a": 1}')
    with pytest.raises(ValueError, match="al menos una"):
        validated_override_tags('["", "  "]')

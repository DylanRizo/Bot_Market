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

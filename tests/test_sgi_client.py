"""Pruebas del cliente del SGI: paginacion, llave y manejo de errores, sin red."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

import sgi_client
from sgi_client import SgiClient, SgiError, parse_catalog_item, validate_base_url

KEY = "k" * 43


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def catalog_page(items: list[dict], page: int, total_pages: int) -> dict:
    return {
        "data": {"items": items, "pagination": {"page": page, "pageSize": 100, "totalItems": 0, "totalPages": total_pages}},
        "meta": {"requestId": "prueba"},
    }


def raw_item(code: str = "CMP-NEG-M", quantity: str = "3", price: str | None = "300.00", issue: str | None = None) -> dict:
    return {"code": code, "name": "Producto", "description": None, "totalQuantity": quantity, "unitPrice": price, "priceIssue": issue}


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api-sgi.example.com", code, "error", {}, io.BytesIO(b""))


def client(opener, **kwargs) -> SgiClient:
    return SgiClient("https://api-sgi.example.com", KEY, opener=opener, sleep=lambda seconds: None, **kwargs)


def test_recorre_todas_las_paginas_y_manda_la_llave_como_bearer():
    responses = [catalog_page([raw_item("CMP-NEG-M")], 1, 2), catalog_page([raw_item("DUR-NEG-U")], 2, 2)]
    seen: list[tuple[str, str]] = []

    def opener(request, timeout):
        seen.append((request.full_url, request.get_header("Authorization")))
        return FakeResponse(responses.pop(0))

    items = client(opener).catalog()
    assert [item.code for item in items] == ["CMP-NEG-M", "DUR-NEG-U"]
    assert seen[0][0].endswith("page=1&pageSize=100")
    assert seen[1][0].endswith("page=2&pageSize=100")
    assert all(auth == f"Bearer {KEY}" for _, auth in seen)


def test_llave_rechazada_no_se_reintenta():
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise http_error(401)

    with pytest.raises(SgiError) as error:
        client(opener).catalog()
    assert error.value.code == "KEY_REJECTED"
    assert len(calls) == 1


@pytest.mark.parametrize("status, code", [(403, "FORBIDDEN"), (503, "DISABLED"), (404, "INVALID_RESPONSE")])
def test_errores_definitivos_se_reportan_con_su_codigo(status, code):
    def opener(request, timeout):
        raise http_error(status)

    with pytest.raises(SgiError) as error:
        client(opener).catalog()
    assert error.value.code == code


def test_limite_de_consultas_espera_y_reintenta():
    responses: list = [http_error(429), catalog_page([raw_item()], 1, 1)]
    waits: list[float] = []

    def opener(request, timeout):
        current = responses.pop(0)
        if isinstance(current, Exception):
            raise current
        return FakeResponse(current)

    sgi = SgiClient("https://api-sgi.example.com", KEY, opener=opener, sleep=waits.append)
    assert len(sgi.catalog()) == 1
    assert len(waits) == 1


def test_sin_conexion_agota_los_reintentos():
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise urllib.error.URLError("sin red")

    with pytest.raises(SgiError) as error:
        client(opener, max_retries=2).catalog()
    assert error.value.code == "UNAVAILABLE"
    assert len(calls) == 3


def test_la_llave_nunca_aparece_en_la_representacion_ni_en_el_error():
    def opener(request, timeout):
        raise http_error(401)

    sgi = client(opener)
    assert KEY not in repr(sgi)
    with pytest.raises(SgiError) as error:
        sgi.catalog()
    assert KEY not in str(error.value)
    assert error.value.__cause__ is None


def test_exige_https_fuera_de_esta_computadora():
    with pytest.raises(SgiError):
        validate_base_url("http://api-sgi.example.com")
    assert validate_base_url("http://localhost:3001/api") == "http://localhost:3001"
    assert validate_base_url("https://api-sgi.example.com/") == "https://api-sgi.example.com"


def test_rechaza_una_llave_con_formato_invalido():
    with pytest.raises(SgiError) as error:
        SgiClient("https://api-sgi.example.com", "corta")
    assert error.value.code == "NOT_CONFIGURED"


def test_un_precio_con_problema_nunca_se_publica():
    mixed = parse_catalog_item(raw_item(price=None, issue="MIXED"))
    assert mixed.unit_price is None and mixed.price_issue == "MIXED" and not mixed.publishable
    zero = parse_catalog_item(raw_item(price="0"))
    assert zero.price_issue == "MISSING" and not zero.publishable
    sold_out = parse_catalog_item(raw_item(quantity="0"))
    assert not sold_out.publishable
    ok = parse_catalog_item(raw_item())
    assert ok.publishable and ok.unit_price == 300.0 and ok.total_quantity == 3.0


def test_la_variable_de_entorno_tiene_prioridad(monkeypatch, tmp_path):
    monkeypatch.setenv("SGI_INTEGRATION_KEY", KEY)
    assert sgi_client.load_integration_key(tmp_path / "no-existe.bin") == KEY


def test_no_guarda_una_llave_mal_formada(tmp_path):
    with pytest.raises(ValueError):
        sgi_client.save_integration_key("no-es-una-llave", tmp_path / "llave.bin")
    assert not (tmp_path / "llave.bin").exists()

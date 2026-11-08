"""Pruebas del conteo de interesados de la bandeja de Marketplace."""
from __future__ import annotations

import json
from datetime import date

import pytest

import marketplace_leads as leads
import marketplace_listing_worker as lw

HOY = date(2026, 11, 8)  # domingo


@pytest.mark.parametrize("etiqueta, esperado", [
    ("7:08 AM", date(2026, 11, 8)),
    ("Just now", date(2026, 11, 8)),
    ("5m", date(2026, 11, 8)),
    ("2h", date(2026, 11, 8)),
    ("Sat", date(2026, 11, 7)),
    ("Mon", date(2026, 11, 2)),
    ("Sun", date(2026, 11, 1)),
    ("sáb", date(2026, 11, 7)),
    ("3d", date(2026, 11, 5)),
    ("2w", date(2026, 10, 25)),
    ("1y", date(2025, 11, 8)),
    ("Oct 12", date(2026, 10, 12)),
    ("Dec 20", date(2025, 12, 20)),
    ("Oct 12, 2024", date(2024, 10, 12)),
])
def test_entiende_la_hora_de_la_bandeja(etiqueta, esperado):
    assert leads.parse_when(etiqueta, HOY) == esperado


def test_una_hora_desconocida_no_inventa_fecha():
    assert leads.parse_when("pronto", HOY) is None
    assert leads.parse_row(["Ana", "· Durag - Negro", "Hola", "pronto"], HOY) is None


def test_una_fila_sin_titulo_de_anuncio_no_es_conversacion():
    assert leads.parse_row(["Ana", "Hola", "Sat"], HOY) is None


@pytest.mark.parametrize("mensaje, falta_responder", [
    ("Ana Lopez sent you a message about your listing: Durag - Negro", True),
    ("En S ?", True),
    ("You: Si, disponible", False),
    ("Tú: Si, disponible", False),
])
def test_sabe_si_falta_responder(mensaje, falta_responder):
    assert leads.parse_row(["Ana", "· Durag - Negro", mensaje, "Sat"], HOY)["awaiting_reply"] is falta_responder


def test_reconoce_el_primer_contacto_que_nadie_contesto():
    sin_contestar = leads.parse_row(
        ["Ana", "· Durag", "Ana Lopez sent you a message about your listing: Durag", "Sat"], HOY)
    con_mensaje = leads.parse_row(["Ana", "· Durag", "En S ?", "Sat"], HOY)

    assert sin_contestar["never_answered"] is True
    assert con_mensaje["never_answered"] is False


def test_no_guarda_nombre_ni_mensaje(store):
    fila = leads.parse_row(["Ana", "· Durag - Negro", "Mi numero es 8888", "Sat"], HOY)
    leads.save_rows(store, "cuenta1", [fila])

    with store.connect() as connection:
        guardado = [str(valor) for valor in connection.execute("SELECT * FROM leads").fetchone()]
    assert not any("Ana" in valor or "8888" in valor for valor in guardado)


def test_una_conversacion_se_cuenta_como_nueva_una_sola_vez(store):
    primera = leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Fri"], HOY)
    assert leads.save_rows(store, "cuenta1", [primera]) == {"read": 1, "new": 1, "awaiting": 1, "never_answered": 0}

    respondida = leads.parse_row(["Ana", "· Durag - Negro", "You: Si", "7:08 AM"], HOY)
    assert leads.save_rows(store, "cuenta1", [respondida]) == {"read": 1, "new": 0, "awaiting": 0, "never_answered": 0}

    with store.connect() as connection:
        fila = dict(connection.execute("SELECT * FROM leads").fetchone())
    assert fila["started_on"] == "2026-11-06"
    assert fila["last_activity"] == "2026-11-08"
    assert fila["awaiting_reply"] == 0
    assert fila["family_key"] == "durags"


def test_la_misma_persona_por_otro_anuncio_u_otra_cuenta_es_otro_lead(store):
    durag = leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Sat"], HOY)
    bolso = leads.parse_row(["Ana", "· Bolso deportivo", "Hola", "Sat"], HOY)
    assert leads.save_rows(store, "cuenta1", [durag, bolso])["new"] == 2
    assert leads.save_rows(store, "cuenta2", [durag])["new"] == 1


def test_una_actualizacion_vieja_no_retrocede_la_ultima_actividad(store):
    leads.save_rows(store, "cuenta1", [leads.parse_row(["Ana", "· Durag", "Hola", "7:08 AM"], HOY)])
    leads.save_rows(store, "cuenta1", [leads.parse_row(["Ana", "· Durag", "Hola", "Sat"], HOY)])
    with store.connect() as connection:
        assert connection.execute("SELECT last_activity FROM leads").fetchone()[0] == "2026-11-08"


def test_el_trabajador_guarda_los_leads(store):
    fila = leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Sat"], HOY)

    resultado = lw.scan_leads(store, "cuenta1", "x", run=lambda _: (0, "[leads-json]" + json.dumps([fila])))

    assert resultado == {"ok": True, "read": 1, "new": 1, "awaiting": 1, "never_answered": 0}


def test_si_falla_la_bandeja_no_se_inventan_leads(store):
    resultado = lw.scan_leads(store, "cuenta1", "x", run=lambda _: (1, "[listings-error:INTERNAL] sin bandeja"))

    assert resultado == {"ok": False, "code": "INTERNAL", "detail": "sin bandeja"}
    leads.ensure_schema(store)
    with store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM leads").fetchone()[0] == 0

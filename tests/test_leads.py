"""Pruebas del conteo de interesados de la bandeja de Marketplace."""
from __future__ import annotations

import json
from datetime import date

import pytest

import marketplace_leads as leads
import marketplace_listing_worker as lw
import marketplace_listings as listings

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


def test_el_reporte_separa_cuentas_y_fechas(store):
    reciente = leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Sat"], HOY)
    viejo = leads.parse_row(["Luis", "· Durag - Negro", "Hola", "3w"], HOY)
    sin_contestar = leads.parse_row(["Eva", "· Durag - Negro", "Eva sent you a message about your listing: x", "7:08 AM"], HOY)
    leads.save_rows(store, "cuenta1", [reciente, viejo, sin_contestar])
    leads.save_rows(store, "cuenta2", [reciente])

    reporte = leads.report(store, HOY)

    assert reporte["accounts"] == [
        {"account": "cuenta1", "total": 3, "last_7": 2, "last_14": 2, "last_30": 3, "unanswered": 1},
        {"account": "cuenta2", "total": 1, "last_7": 1, "last_14": 1, "last_30": 1, "unanswered": 0},
    ]
    assert reporte["unanswered"] == [
        {"account": "cuenta1", "title": "Durag - Negro", "family_key": "durags", "last_activity": "2026-11-08", "age_days": 0},
    ]


def test_un_chat_viejo_sin_contestar_no_sale_en_pendientes(store):
    viejo = leads.parse_row(["Eva", "· Durag", "Eva sent you a message about your listing: x", "3w"], HOY)
    leads.save_rows(store, "cuenta1", [viejo])

    reporte = leads.report(store, HOY)

    assert reporte["unanswered"] == []
    assert reporte["accounts"][0]["unanswered"] == 0


def test_el_enfoque_va_primero_aunque_no_tenga_leads(store):
    leads.save_rows(store, "cuenta1", [leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Sat"], HOY)])

    productos = leads.report(store, HOY)["products"]

    assert [p["family_key"] for p in productos] == ["calcetas", "compression_short", "bershka", "durags"]
    assert [p["focus"] for p in productos] == [True, True, True, False]
    assert productos[0]["leads"] == 0
    assert productos[3]["by_account"] == {"cuenta1": 1}


def anuncio(store, clave, familia, clics, status="in_stock", present=1,
            listed_on="2026-10-20", last_seen="2026-11-08T10:00:00"):
    listings.ensure_schema(store)
    with store.connect() as connection:
        connection.execute(
            """INSERT INTO listings (key, account, title, family_key, status, listed_on, clicks_14d, first_seen,
                                     last_seen, present)
               VALUES (?, 'cuenta1', 'x', ?, ?, ?, ?, 'a', ?, ?)""",
            (clave, familia, status, listed_on, clics, last_seen, present),
        )


def test_la_proporcion_usa_solo_los_clics_de_anuncios_activos(store):
    leads.save_rows(store, "cuenta1", [leads.parse_row(["Ana", "· Durag - Negro", "Hola", "Sat"], HOY)])
    anuncio(store, "a", "durags", 50)
    anuncio(store, "b", "durags", 900, status="sold")
    anuncio(store, "c", "durags", 900, present=0)

    durags = next(p for p in leads.report(store, HOY)["products"] if p["family_key"] == "durags")

    assert (durags["clicks"], durags["rate"]) == (50, 2)


@pytest.mark.parametrize("clics", [0, 1])
def test_sin_clics_suficientes_no_se_inventa_proporcion(store, clics):
    filas = [leads.parse_row([nombre, "· Durag - Negro", "Hola", "Sat"], HOY) for nombre in ("Ana", "Luis")]
    leads.save_rows(store, "cuenta1", filas)
    anuncio(store, "a", "durags", clics)

    durags = next(p for p in leads.report(store, HOY)["products"] if p["family_key"] == "durags")

    assert durags["rate"] is None


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


def test_el_puntaje_por_producto_cuenta_tambien_los_anuncios_borrados(store):
    filas = [leads.parse_row([nombre, "· Durag - Negro", "Hola", "Sat"], HOY) for nombre in ("Ana", "Luis")]
    leads.save_rows(store, "cuenta1", filas)
    anuncio(store, "a", "durags", 50)
    anuncio(store, "b", "durags", 0, present=0)
    anuncio(store, "c", "munequeras", 3, present=0)
    anuncio(store, "d", "tops", 3, listed_on="2026-11-05")  # recien publicado: todavia no cuenta
    anuncio(store, "e", "bolsos", 3, last_seen="2026-10-01T10:00:00")  # no se ve hace mas de dos semanas

    assert leads.family_scores(store, HOY) == {"durags": 1.0, "munequeras": 0.0}


def test_sin_bandeja_leida_no_hay_puntajes(store):
    anuncio(store, "a", "munequeras", 3)
    assert leads.family_scores(store, HOY) == {}


def test_avisa_de_los_chats_recientes_sin_respuesta_y_lo_quita_al_contestar(store):
    hoy = date.today()
    sin_contestar = leads.parse_row(["Eva", "· Durag", "Eva sent you a message about your listing: x", "7:08 AM"], hoy)
    lw.scan_leads(store, "cuenta1", "x", run=lambda _: (0, "[leads-json]" + json.dumps([sin_contestar])))
    alertas = store.list_alerts()
    assert [(alerta["code"], alerta["account"]) for alerta in alertas] == [("LEADS_UNANSWERED", "cuenta1")]
    assert alertas[0]["message"].startswith("1 persona escribio en los ultimos 3 dias y nadie le respondio")
    assert "Eva" not in alertas[0]["message"]

    contestado = leads.parse_row(["Eva", "· Durag", "You: Si, disponible", "7:08 AM"], hoy)
    lw.scan_leads(store, "cuenta1", "x", run=lambda _: (0, "[leads-json]" + json.dumps([contestado])))
    assert store.list_alerts() == []


def test_un_chat_sin_respuesta_de_hace_una_semana_no_avisa(store):
    viejo = leads.parse_row(["Eva", "· Durag", "Eva sent you a message about your listing: x", "1w"], date.today())
    lw.scan_leads(store, "cuenta1", "x", run=lambda _: (0, "[leads-json]" + json.dumps([viejo])))
    assert store.list_alerts() == []

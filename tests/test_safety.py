"""Pruebas de la proteccion de cuenta: limites, calentamiento, pausas y texto."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

import marketplace_safety as safety
from conftest import enqueue_job


AHORA = datetime(2026, 9, 17, 12, 0, 0)


def publicar(store, cuenta, momento):
    store.record_publish_event(cuenta, "anuncio", "prueba", momento.isoformat(timespec="seconds"))


@pytest.fixture()
def cuenta_veterana(store):
    """Cuenta que empezo a vender hace un mes: ya paso el calentamiento."""
    publicar(store, "cuenta1", AHORA - timedelta(days=30))
    return "cuenta1"


def test_cuenta_nueva_publica_uno_al_dia_durante_el_calentamiento(store):
    assert safety.check_can_publish(store, "nueva", AHORA)[0]
    publicar(store, "nueva", AHORA - timedelta(hours=3))
    ok, code, detail, retry = safety.check_can_publish(store, "nueva", AHORA)
    assert not ok and code == "DAILY_LIMIT"
    assert "calentamiento" in detail
    assert retry.startswith("2026-09-18T09")


def test_el_calentamiento_sube_a_dos_en_la_segunda_mitad(store):
    publicar(store, "cuenta1", AHORA - timedelta(days=8))
    assert safety.daily_limit(store, "cuenta1", now=AHORA) == 2
    publicar(store, "cuenta1", AHORA - timedelta(days=20))
    assert safety.daily_limit(store, "cuenta1", now=AHORA) == 3


def test_limite_diario_de_cuenta_veterana(store, cuenta_veterana):
    for horas in (9, 6, 3):
        publicar(store, cuenta_veterana, AHORA - timedelta(hours=horas))
    ok, code, _, _ = safety.check_can_publish(store, cuenta_veterana, AHORA)
    assert not ok and code == "DAILY_LIMIT"


def test_separacion_minima_entre_anuncios(store, cuenta_veterana):
    publicar(store, cuenta_veterana, AHORA - timedelta(minutes=30))
    ok, code, _, retry = safety.check_can_publish(store, cuenta_veterana, AHORA)
    assert not ok and code == "MIN_GAP"
    assert retry == (AHORA + timedelta(minutes=90)).isoformat(timespec="seconds")


def test_limite_semanal(store, cuenta_veterana):
    safety.save_safety(store, {"max_per_account_week": 4})
    for dias in (6, 5, 4, 2):
        publicar(store, cuenta_veterana, AHORA - timedelta(days=dias))
    ok, code, _, retry = safety.check_can_publish(store, cuenta_veterana, AHORA)
    assert not ok and code == "WEEKLY_LIMIT"
    assert retry > AHORA.isoformat()


def test_la_proteccion_apagada_no_frena(store):
    safety.save_safety(store, {"enabled": False})
    publicar(store, "nueva", AHORA - timedelta(minutes=1))
    assert safety.check_can_publish(store, "nueva", AHORA)[0]


def test_dos_fallos_seguidos_pausan_la_cuenta(store):
    safety.register_outcome(store, "cuenta1", False, "FORM_FIELD")
    assert not safety.active_pauses(store)
    safety.register_outcome(store, "cuenta1", False, "FORM_FIELD")
    pausa = safety.active_pauses(store)["cuenta1"]
    assert pausa["code"] == "ACCOUNT_PAUSED"
    ok, code, _, _ = safety.check_can_publish(store, "cuenta1")
    assert not ok and code == "ACCOUNT_PAUSED"
    assert [alert["code"] for alert in store.list_alerts()] == ["ACCOUNT_PAUSED"]


def test_un_exito_reinicia_el_contador_de_fallos(store):
    safety.register_outcome(store, "cuenta1", False, "FORM_FIELD")
    safety.register_outcome(store, "cuenta1", True)
    safety.register_outcome(store, "cuenta1", False, "FORM_FIELD")
    assert not safety.active_pauses(store)


def test_fallos_ajenos_a_facebook_no_cuentan(store):
    for _ in range(3):
        safety.register_outcome(store, "cuenta1", False, "SGI_UNAVAILABLE")
    assert not safety.active_pauses(store)


def test_una_restriccion_pausa_de_inmediato_y_una_pausa_corta_no_la_acorta(store):
    safety.register_outcome(store, "cuenta1", False, "ACCOUNT_RESTRICTED", "limite")
    hasta = safety.active_pauses(store)["cuenta1"]["until"]
    assert datetime.fromisoformat(hasta) > datetime.now() + timedelta(hours=70)
    safety.pause_account(store, "cuenta1", 1, "otra")
    assert safety.active_pauses(store)["cuenta1"]["until"] == hasta
    assert safety.resume_account(store, "cuenta1")
    assert not safety.active_pauses(store)


def test_backfill_cuenta_una_vez_por_minuto_y_no_se_repite(store, tmp_path):
    actividad = tmp_path / "actividad.json"
    actividad.write_text(json.dumps({"items": [
        {"status": "published", "account": "cuenta1", "updated_at": "2026-09-14T15:23:42", "name": "A"},
        {"status": "failed", "account": "cuenta1", "updated_at": "2026-09-14T16:00:00", "name": "B"},
        {"status": "published", "account": "cuenta2", "updated_at": "2026-08-30T17:03:01", "name": "C"},
    ]}), encoding="utf-8")
    queue_id = enqueue_job(store, account="cuenta1")
    store.mark_published(queue_id)
    with store.connect() as connection:
        connection.execute("UPDATE publications SET published_at='2026-09-14T15:23:50'")
    assert safety.backfill_events(store, actividad) == 2
    assert safety.backfill_events(store, actividad) == 0
    assert store.first_publish_event("cuenta2").startswith("2026-08-30T17:03")


@pytest.mark.parametrize(
    "titulo, descripcion, precio, esperado",
    [
        ("Camisa", "Visita www.tienda.com para mas", 300, "enlace"),
        ("Camisa", "Escribe a ventas@tienda.com", 300, "correo"),
        ("Camisa", "Descripcion suficiente para el anuncio", 0, "precio"),
        ("X" * 101, "Descripcion suficiente para el anuncio", 300, "caracteres"),
    ],
)
def test_revision_de_texto_bloquea(titulo, descripcion, precio, esperado):
    errores, _ = safety.lint_listing(titulo, descripcion, precio)
    assert any(esperado in error for error in errores)


def test_revision_de_texto_avisa_sin_bloquear():
    errores, avisos = safety.lint_listing(
        "CAMISA DE COMPRESION OFERTA!!!", "Llama al 8888-8888 o por WhatsApp", "C$320"
    )
    assert errores == []
    texto = " ".join(avisos)
    assert "telefono" in texto and "mayusculas" in texto and "mensajeria" in texto and "repetidos" in texto


def test_anuncio_normal_pasa_la_revision():
    errores, avisos = safety.lint_listing(
        "Camisa de compresion manga corta - Blanco, Gris y Negro (S, M, L, XL)",
        "Camisa de compresion nueva, perfecta para entrenar.\nPrecio: C$320 cada uno.\nUbicacion: Managua, Nicaragua.",
        320,
    )
    assert errores == [] and avisos == []


def test_resumen_de_salud(store):
    publicar(store, "cuenta1", datetime.now() - timedelta(minutes=5))
    safety.pause_account(store, "cuenta2", 2, "prueba")
    resumen = safety.health_snapshot(store, ["cuenta1", "cuenta2", "cuenta3"])
    assert resumen["cuenta1"]["status"] == "waiting"
    assert resumen["cuenta1"]["daily_limit"] == 1
    assert resumen["cuenta2"]["status"] == "paused"
    assert resumen["cuenta3"]["status"] == "warming"

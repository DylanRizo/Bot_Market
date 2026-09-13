"""Pruebas de la puerta del SGI en el trabajador: nada de red, ni Chrome, ni Drive."""
from __future__ import annotations

import subprocess
import time
from types import SimpleNamespace

import pytest

import marketplace_scheduler_worker as worker
from conftest import enqueue_job
from drive_photos import DriveNotAuthorized
from sgi_client import SgiError
from sgi_sync import revalidate_from_report

REPORT = {
    "generated_at": "2026-09-12T10:00:00",
    "catalog_items": 4,
    "publishable": ["CMP-NEG-M"],
    "price_issues": [{"code": "CMP-BLA-M", "issue": "MIXED", "detail": "Precios distintos entre bodegas en el SGI"}],
    "without_photos": [{"code": "BOL-NEG-U", "old_code": "CBBK-UNI"}],
    "drive_photos": ["CMP-NEG-M"],
    "local_photos": [],
}


def config(mode: str = "autonomous") -> dict:
    return {"mode": mode, "retry_delay_minutes": 30, "max_attempts": 3, "paused_accounts": [], "enabled": True}


@pytest.fixture()
def gate(monkeypatch, tmp_path):
    intents = tmp_path / "intents"
    intents.mkdir()
    calls = SimpleNamespace(preflight=0, publisher=0, refresh=0)
    monkeypatch.setattr(worker, "PUBLISH_INTENTS_DIR", intents)
    monkeypatch.setattr(worker, "campaign_for_item", lambda item, cfg: tmp_path / "campana.json")

    def preflight(item, cfg):
        calls.preflight += 1
        return True, "", "navegador listo"

    def publisher(command, **kwargs):
        calls.publisher += 1
        return subprocess.CompletedProcess(command, 0, "[listing-url:https://facebook.com/item/1]", "")

    monkeypatch.setattr(worker, "preflight", preflight)
    monkeypatch.setattr(worker.subprocess, "run", publisher)
    monkeypatch.setattr(worker, "sgi_configured", lambda: True)

    def respond(report: dict | None = None, error: Exception | None = None) -> None:
        def refresh(store):
            calls.refresh += 1
            if error:
                raise error
            return report

        monkeypatch.setattr(worker, "refresh_from_sgi", refresh)

    return SimpleNamespace(calls=calls, respond=respond)


def family(prefix: str) -> dict:
    return {"name": "Familia", "family_key": "familia", "sku_prefixes": [prefix]}


def claim(store, job: dict) -> dict:
    enqueue_job(store, scheduled_at="2020-01-01T09:00:00", job=job)
    return store.claim_due()


# --- clasificacion con el reporte --------------------------------------------

def test_clasifica_la_familia_con_el_reporte():
    assert revalidate_from_report(REPORT, ["CMP-NEG-"])[:2] == (True, "")
    ok, code, detail = revalidate_from_report(REPORT, ["CMP-BLA-"])
    assert (ok, code) == (False, "PRICE_ISSUE") and "CMP-BLA-M" in detail
    assert revalidate_from_report(REPORT, ["BOL-"])[:2] == (False, "NO_PHOTOS")
    assert revalidate_from_report(REPORT, ["DUR-"])[:2] == (False, "OUT_OF_STOCK")
    assert revalidate_from_report(REPORT, [])[:2] == (False, "OUT_OF_STOCK")


# --- puerta antes de publicar ------------------------------------------------

def test_con_stock_publica_normalmente(store, gate):
    gate.respond(REPORT)
    item = claim(store, family("CMP-NEG-"))
    worker.run_item(store, item, config())
    assert store.get_queue_item(item["id"])["status"] == "published"
    assert gate.calls.refresh == 1 and gate.calls.publisher == 1


def test_agotado_se_omite_sin_abrir_chrome_ni_alertar(store, gate):
    gate.respond(REPORT)
    item = claim(store, family("DUR-"))
    worker.run_item(store, item, config())
    current = store.get_queue_item(item["id"])
    assert current["status"] == "skipped"
    assert "Sin stock" in current["detail"]
    assert gate.calls.preflight == 0 and gate.calls.publisher == 0
    assert store.list_alerts() == []


@pytest.mark.parametrize("prefix, code", [("CMP-BLA-", "PRICE_ISSUE"), ("BOL-", "NO_PHOTOS")])
def test_precio_dudoso_o_sin_foto_se_omite_y_avisa(store, gate, prefix, code):
    gate.respond(REPORT)
    item = claim(store, family(prefix))
    worker.run_item(store, item, config())
    assert store.get_queue_item(item["id"])["status"] == "skipped"
    assert [alert["code"] for alert in store.list_alerts()] == [code]
    assert gate.calls.publisher == 0


def test_sgi_caido_reintenta_mas_tarde(store, gate):
    gate.respond(error=SgiError("UNAVAILABLE", "sin conexion"))
    item = claim(store, family("CMP-NEG-"))
    worker.run_item(store, item, config())
    current = store.get_queue_item(item["id"])
    assert current["status"] == "retry"
    assert current["next_attempt_at"] is not None
    assert gate.calls.preflight == 0


@pytest.mark.parametrize(
    "error, code",
    [
        (SgiError("KEY_REJECTED", "llave revocada"), "SGI_KEY_REJECTED"),
        (DriveNotAuthorized("falta consentimiento"), "DRIVE_NOT_AUTHORIZED"),
    ],
)
def test_llave_rechazada_o_drive_sin_autorizar_bloquea_con_alerta(store, gate, error, code):
    gate.respond(error=error)
    item = claim(store, family("CMP-NEG-"))
    worker.run_item(store, item, config())
    assert store.get_queue_item(item["id"])["status"] == "blocked"
    assert [alert["code"] for alert in store.list_alerts()] == [code]
    assert gate.calls.publisher == 0


def test_un_producto_manual_sin_familia_del_sgi_no_pasa_por_la_puerta(store, gate):
    gate.respond(error=AssertionError("no deberia consultar el SGI"))
    item = claim(store, {"name": "Producto manual", "family_key": "custom:1"})
    worker.run_item(store, item, config())
    assert store.get_queue_item(item["id"])["status"] == "published"
    assert gate.calls.refresh == 0


def test_sin_sgi_configurado_el_bot_sigue_con_su_excel(store, gate, monkeypatch):
    monkeypatch.setattr(worker, "sgi_configured", lambda: False)
    gate.respond(error=AssertionError("no deberia consultar el SGI"))
    item = claim(store, family("CMP-NEG-"))
    worker.run_item(store, item, config())
    assert store.get_queue_item(item["id"])["status"] == "published"


# --- sincronizacion periodica ------------------------------------------------

def test_sin_catalogo_fresco_no_arma_la_semana(store, gate, monkeypatch):
    gate.respond(error=SgiError("KEY_REJECTED", "llave revocada"))
    monkeypatch.setattr(worker, "load_config", lambda current: config())
    monkeypatch.setattr(worker, "ensure_future_calendar", lambda *args: pytest.fail("no deberia generar la semana"))
    assert worker.run_once(store) is False
    assert [alert["code"] for alert in store.list_alerts()] == ["SGI_KEY_REJECTED"]


def test_espacia_la_sincronizacion_y_reintenta_antes_tras_un_fallo(store):
    now = time.time()
    assert worker.sgi_sync_due(store, now) is True
    store.heartbeat(worker.SGI_STATE_KEY, {"status": "ok", "attempted_at_epoch": now - 30 * 60})
    assert worker.sgi_sync_due(store, now) is False
    store.heartbeat(worker.SGI_STATE_KEY, {"status": "error", "attempted_at_epoch": now - 5 * 60})
    assert worker.sgi_sync_due(store, now) is False
    store.heartbeat(worker.SGI_STATE_KEY, {"status": "error", "attempted_at_epoch": now - 11 * 60})
    assert worker.sgi_sync_due(store, now) is True

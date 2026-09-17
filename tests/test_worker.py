"""Pruebas del trabajador, sobre todo la regla que evita anuncios duplicados."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

import marketplace_scheduler_worker as worker
from conftest import enqueue_job


def config(mode: str = "autonomous", **overrides):
    base = {
        "mode": mode,
        "retry_delay_minutes": 30,
        "max_attempts": 3,
        "paused_accounts": [],
        "enabled": True,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def worker_env(monkeypatch, tmp_path):
    """Aisla el trabajador: sin Chrome, sin subprocesos y con intenciones en tmp."""
    intents = tmp_path / "intents"
    intents.mkdir()
    monkeypatch.setattr(worker, "PUBLISH_INTENTS_DIR", intents)
    monkeypatch.setattr(worker, "campaign_for_item", lambda item, cfg: tmp_path / "campana.json")
    monkeypatch.setattr(worker, "preflight", lambda item, cfg: (True, "", "navegador listo"))

    def set_subprocess(returncode: int, output: str = "", crea_intencion: str | None = None,
                       timeout: bool = False):
        def fake_run(command, **kwargs):
            if crea_intencion:
                (intents / f"{crea_intencion}.json").write_text("{}", encoding="utf-8")
            if timeout:
                raise subprocess.TimeoutExpired(command, 600, output=output, stderr="")
            return subprocess.CompletedProcess(command, returncode, output, "")

        monkeypatch.setattr(worker.subprocess, "run", fake_run)

    return SimpleNamespace(intents=intents, set_subprocess=set_subprocess)


# --- clasificacion de errores ------------------------------------------------

def test_timeout_es_bloqueante_solo_al_publicar():
    assert worker.classify_failure("proceso timed out", 124, publishing=False)[0] == "TIMEOUT"
    assert worker.classify_failure("proceso timed out", 124, publishing=True)[0] == "PUBLISH_OUTCOME_UNKNOWN"


def test_timeout_se_detecta_por_bandera_y_no_por_el_texto():
    """El hijo no imprime 'timed out': el aviso llega en la excepcion."""
    salida = "llenando el formulario..."
    assert worker.classify_failure(salida, 124, publishing=False)[0] == "PUBLISHER_FAILED"
    assert worker.classify_failure(salida, 124, publishing=False, timed_out=True)[0] == "TIMEOUT"
    assert worker.classify_failure(salida, 124, publishing=True, timed_out=True)[0] == "PUBLISH_OUTCOME_UNKNOWN"


def test_classify_failure_respeta_el_codigo_del_publicador():
    salida = "[marketplace-browser] [error-code:SESSION_BLOCKED] stage=open-session"
    assert worker.classify_failure(salida, 1)[0] == "SESSION_BLOCKED"


def test_listing_url_se_extrae_de_la_salida():
    salida = "ruido\n[marketplace-browser] [listing-url:https://facebook.com/item/7]\n[publish-confirmed]"
    assert worker.listing_url_from_output(salida) == "https://facebook.com/item/7"
    assert worker.listing_url_from_output("sin nada") == ""


# --- el bug de los duplicados ------------------------------------------------

def test_fallo_tras_pulsar_publish_bloquea_y_no_reintenta(store, worker_env):
    """Si quedo archivo de intencion, el anuncio pudo crearse: prohibido reintentar."""
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    worker_env.set_subprocess(1, "el navegador murio", crea_intencion=queue_id)
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "blocked"
    assert resultado["next_attempt_at"] is None, "no debe quedar ningun reintento programado"
    alertas = store.list_alerts()
    assert [alerta["code"] for alerta in alertas] == ["PUBLISH_OUTCOME_UNKNOWN"]
    assert (worker_env.intents / f"{queue_id}.json").exists(), "la intencion se conserva hasta revisarla"


def test_timeout_al_publicar_bloquea_aunque_no_haya_intencion(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    worker_env.set_subprocess(0, "quedo colgado", timeout=True)
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "blocked"
    assert resultado["next_attempt_at"] is None


def test_fallo_temporal_sin_intencion_si_se_reintenta(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=3)
    worker_env.set_subprocess(1, "fallo raro del publicador")
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "retry"
    assert resultado["next_attempt_at"] is not None


def test_publicacion_correcta_guarda_la_url_y_limpia_la_intencion(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    salida = "[marketplace-browser] [listing-url:https://facebook.com/item/42]\n[publish-confirmed]"
    worker_env.set_subprocess(0, salida, crea_intencion=queue_id)
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "published"
    assert resultado["listing_url"] == "https://facebook.com/item/42"
    assert not (worker_env.intents / f"{queue_id}.json").exists()


def test_una_intencion_vieja_no_bloquea_el_intento_siguiente(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    (worker_env.intents / f"{queue_id}.json").write_text("{}", encoding="utf-8")
    worker_env.set_subprocess(0, "[listing-url:https://facebook.com/item/8]")
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    assert store.get_queue_item(queue_id)["status"] == "published"


def test_duplicado_por_huella_ya_publicada_se_omite(store, worker_env):
    primero = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", dedupe_key="a")
    store.mark_published(primero)
    segundo = enqueue_job(store, scheduled_at="2020-01-01T10:00:00", dedupe_key="b")
    worker_env.set_subprocess(0, "")
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    assert store.get_queue_item(segundo)["status"] == "skipped"


# --- aprobacion --------------------------------------------------------------

def test_la_aprobacion_se_revisa_antes_de_abrir_chrome(store, worker_env, monkeypatch):
    def preflight_prohibido(item, cfg):
        raise AssertionError("no se debe abrir el navegador para un elemento sin aprobar")

    monkeypatch.setattr(worker, "preflight", preflight_prohibido)
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", approved=False)
    item = store.claim_due()

    worker.run_item(store, item, config("semiautomatic"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "planned"
    assert resultado["detail"] == "Pendiente de aprobacion humana."


def test_modo_simulacion_no_marca_publicado(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    worker_env.set_subprocess(0, "PLAN job=0")
    item = store.claim_due()

    worker.run_item(store, item, config("simulation"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "tested"
    assert store.publication_exists(resultado["account"], resultado["fingerprint"]) is False


# --- proteccion de cuenta ----------------------------------------------------

def test_detalle_legible_usa_la_linea_de_error_del_publicador():
    salida = (
        "[marketplace-browser] [error-code:FORM_FIELD] stage=category\n"
        "[marketplace-browser] ERROR: No pude seleccionar Category=Men's clothing & shoes.\n"
        "Fallo el job 0: Camisa sin mangas"
    )
    code, detail = worker.classify_failure(salida, 1, publishing=True)
    assert code == "FORM_FIELD"
    assert detail == "No pude seleccionar Category=Men's clothing & shoes."


def test_limite_de_cuenta_pospone_sin_gastar_intento(store, worker_env, monkeypatch):
    from marketplace_safety import save_safety

    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=2)
    store.record_publish_event("cuenta1", "anterior", "prueba")
    llamado = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: llamado.append(a))
    save_safety(store, {"min_gap_minutes": 120})
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert not llamado, "no debe abrir Facebook"
    assert resultado["status"] == "queued"
    assert resultado["attempts"] == 0
    assert resultado["next_attempt_at"] > resultado["updated_at"]
    assert "proteccion de cuenta" in resultado["detail"]
    assert store.claim_due() is None, "no se vuelve a tomar antes de tiempo"


def test_prueba_sin_publicar_no_la_frena_la_proteccion(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    store.record_publish_event("cuenta1", "anterior", "prueba")
    worker_env.set_subprocess(0, "formulario probado")
    item = store.claim_due()

    worker.run_item(store, item, config("dry_run"))

    assert store.get_queue_item(queue_id)["status"] == "tested"


def test_espera_del_runner_se_reprograma(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=1)
    salida = "[marketplace-browser] [error-code:SAFETY_HOLD] Proteccion de cuenta: limite\nProteccion de cuenta"
    worker_env.set_subprocess(1, salida)
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    resultado = store.get_queue_item(queue_id)
    assert resultado["status"] == "queued"
    assert resultado["attempts"] == 0
    assert not store.list_alerts()


def test_restriccion_de_facebook_bloquea(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=3)
    worker_env.set_subprocess(1, "[marketplace-browser] [error-code:ACCOUNT_RESTRICTED] stage=open-session")
    item = store.claim_due()

    worker.run_item(store, item, config("autonomous"))

    assert store.get_queue_item(queue_id)["status"] == "blocked"


def test_publicada_limpia_el_detalle_del_fallo_anterior(store, worker_env):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    store.update_queue_item(queue_id, detail="Fallo el job 0: algo")
    store.mark_published(queue_id)
    assert store.get_queue_item(queue_id)["detail"] == "Publicada en Marketplace."

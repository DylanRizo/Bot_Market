"""Pruebas del almacen: cola, reintentos, publicaciones y fotos."""
from __future__ import annotations

import os
from datetime import datetime, timedelta

from conftest import enqueue_job
from marketplace_storage import MarketplaceStore, listing_fingerprint


def test_dedupe_key_impide_encolar_dos_veces(store):
    primero, creado_1 = store.enqueue(
        {
            "scheduled_at": "2026-09-07T09:00:00",
            "account": "cuenta1",
            "name": "Durags",
            "job": {"name": "Durags"},
            "dedupe_key": "clave-unica",
        }
    )
    segundo, creado_2 = store.enqueue(
        {
            "scheduled_at": "2026-09-07T09:00:00",
            "account": "cuenta1",
            "name": "Durags otra vez",
            "job": {"name": "Durags"},
            "dedupe_key": "clave-unica",
        }
    )
    assert creado_1 is True
    assert creado_2 is False
    assert primero == segundo
    assert len(store.list_queue()) == 1


def test_claim_due_no_entrega_el_mismo_trabajo_dos_veces(store):
    enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    primero = store.claim_due()
    segundo = store.claim_due()
    assert primero is not None
    assert segundo is None
    assert primero["status"] == "running"
    assert primero["attempts"] == 1


def test_claim_due_respeta_el_horario_futuro(store):
    futuro = (datetime.now() + timedelta(days=1)).isoformat(timespec="seconds")
    enqueue_job(store, scheduled_at=futuro)
    assert store.claim_due() is None


def test_claim_due_ignora_los_no_aprobados_en_estado_planned(store):
    enqueue_job(store, scheduled_at="2020-01-01T09:00:00", status="planned", approved=False)
    assert store.claim_due() is None


def test_backoff_duplica_la_espera_y_respeta_el_tope():
    espera = [MarketplaceStore.retry_delay_minutes(30, intento, jitter=False) for intento in range(1, 8)]
    assert espera[:5] == [30, 60, 120, 240, 360]
    assert max(espera) == 360


def test_backoff_con_jitter_se_mantiene_en_el_rango():
    valores = {MarketplaceStore.retry_delay_minutes(30, 2) for _ in range(200)}
    assert min(valores) >= 48   # 60 - 20%
    assert max(valores) <= 72   # 60 + 20%


def test_schedule_retry_bloquea_al_agotar_los_intentos(store):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=1)
    store.claim_due()
    store.schedule_retry(queue_id, 30, "fallo temporal")
    assert store.get_queue_item(queue_id)["status"] == "blocked"


def test_schedule_retry_programa_el_siguiente_intento(store):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", max_attempts=3)
    store.claim_due()
    store.schedule_retry(queue_id, 30, "fallo temporal")
    item = store.get_queue_item(queue_id)
    assert item["status"] == "retry"
    assert item["next_attempt_at"] > datetime.now().isoformat(timespec="seconds")


def test_mark_published_registra_publicacion_url_y_fotos(store, photo):
    imagen = photo()
    queue_id = enqueue_job(store, job={"name": "Durags", "image_paths": [str(imagen)]})
    store.mark_published(queue_id, "https://facebook.com/marketplace/item/999")
    item = store.get_queue_item(queue_id)
    assert item["status"] == "published"
    assert item["listing_url"] == "https://facebook.com/marketplace/item/999"
    assert store.publication_exists(item["account"], item["fingerprint"]) is True
    assert store.media_conflicts("cuenta1", [imagen])


def test_una_foto_usada_en_una_cuenta_sigue_libre_en_otra(store, photo):
    imagen = photo()
    queue_id = enqueue_job(store, job={"name": "Durags", "image_paths": [str(imagen)]})
    store.mark_published(queue_id)
    assert store.media_conflicts("cuenta1", [imagen])
    assert store.media_conflicts("cuenta2", [imagen]) == []


def test_recover_stale_reintenta_lo_que_no_llego_a_publicar(store):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    store.claim_due()
    store.update_queue_item(queue_id, started_at="2020-01-01T09:00:00")
    recuperados = store.recover_stale()
    assert recuperados["retry"] == [queue_id]
    assert store.get_queue_item(queue_id)["status"] == "retry"


def test_recover_stale_bloquea_lo_que_pudo_haberse_publicado(store):
    """El caso que creaba anuncios duplicados: se pulso Publish y el proceso murio."""
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    store.claim_due()
    store.update_queue_item(queue_id, started_at="2020-01-01T09:00:00")
    recuperados = store.recover_stale(uncertain_ids={queue_id})
    assert recuperados["blocked"] == [queue_id]
    assert recuperados["retry"] == []
    item = store.get_queue_item(queue_id)
    assert item["status"] == "blocked"
    assert "Revisa Marketplace" in item["detail"]


def test_image_hash_usa_cache_y_se_invalida_al_cambiar_la_foto(store, photo, monkeypatch):
    imagen = photo("contenido original")
    primero = store.image_hash(imagen)
    lecturas = {"n": 0}
    apertura_real = type(imagen).open

    def contar(self, *args, **kwargs):
        lecturas["n"] += 1
        return apertura_real(self, *args, **kwargs)

    monkeypatch.setattr(type(imagen), "open", contar)
    assert store.image_hash(imagen) == primero
    assert lecturas["n"] == 0, "la segunda consulta no debe releer el archivo"

    monkeypatch.undo()
    # La clave de la cache es fecha + tamano. Un disco rapido puede reescribir el
    # archivo sin que cambie la fecha, asi que la foto nueva tiene otro tamano y
    # una fecha distinta explicita, como cuando de verdad se reemplaza una foto.
    anterior = imagen.stat()
    imagen.write_bytes(b"otra foto con contenido distinto")
    os.utime(imagen, ns=(anterior.st_atime_ns, anterior.st_mtime_ns + 2_000_000_000))
    assert store.image_hash(imagen) != primero


def test_publication_exists_solo_dentro_de_la_ventana(store):
    queue_id = enqueue_job(store)
    store.mark_published(queue_id)
    item = store.get_queue_item(queue_id)
    assert store.publication_exists(item["account"], item["fingerprint"], within_hours=12) is True
    assert store.publication_exists(item["account"], "otra-huella", within_hours=12) is False


def test_listing_fingerprint_cambia_con_la_cuenta():
    job = {"family_key": "durags", "name": "Durags", "skus": ["DG-1"]}
    assert listing_fingerprint(job, "cuenta1") != listing_fingerprint(job, "cuenta2")
    assert listing_fingerprint(job, "cuenta1") == listing_fingerprint(dict(job), "cuenta1")

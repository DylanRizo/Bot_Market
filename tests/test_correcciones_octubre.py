"""Correcciones de octubre: horario, separacion global, calendario unico, portadas y duplicados."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import marketplace_listings as listings
import marketplace_scheduler as scheduler
import marketplace_scheduler_worker as worker
from conftest import enqueue_job
from marketplace_safety import check_can_publish, save_safety


# --- horario del bot -----------------------------------------------------------

CONFIG_HORARIO = {"start_time": "09:00", "end_time": "18:00", "active_days": [0, 1, 2, 3, 4, 5]}


def test_ventana_dentro_y_fuera_del_horario():
    dentro, _ = scheduler.schedule_window(CONFIG_HORARIO, datetime(2026, 10, 6, 10, 0))   # martes
    assert dentro
    dentro, proximo = scheduler.schedule_window(CONFIG_HORARIO, datetime(2026, 10, 6, 6, 50))
    assert not dentro and proximo == datetime(2026, 10, 6, 9, 0)
    dentro, proximo = scheduler.schedule_window(CONFIG_HORARIO, datetime(2026, 10, 6, 20, 10))
    assert not dentro and proximo == datetime(2026, 10, 7, 9, 0)
    # Sabado de noche: el domingo no se trabaja, toca el lunes.
    _, proximo = scheduler.schedule_window(CONFIG_HORARIO, datetime(2026, 10, 10, 19, 0))
    assert proximo == datetime(2026, 10, 12, 9, 0)


def test_lo_atrasado_fuera_de_horario_espera_sin_gastar_intento(store, monkeypatch):
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00")
    monkeypatch.setattr(worker, "schedule_window", lambda config: (False, datetime(2030, 1, 1, 9, 0)))
    llamado = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: llamado.append(a))
    item = store.claim_due()
    worker.run_item(store, item, {"mode": "autonomous", "retry_delay_minutes": 30})
    resultado = store.get_queue_item(queue_id)
    assert not llamado
    assert resultado["status"] == "queued" and resultado["attempts"] == 0
    assert resultado["next_attempt_at"] == "2030-01-01T09:00:00"


# --- separacion entre cuentas ---------------------------------------------------

def test_separacion_global_entre_cuentas(store):
    store.record_publish_event("cuenta1", "veterana", "prueba", (datetime.now() - timedelta(days=40)).isoformat())
    store.record_publish_event("cuenta2", "veterana", "prueba", (datetime.now() - timedelta(days=40)).isoformat())
    store.record_publish_event("cuenta1", "recien", "prueba", (datetime.now() - timedelta(minutes=5)).isoformat())
    ok, code, _, _ = check_can_publish(store, "cuenta2")
    assert not ok and code == "GLOBAL_GAP"
    save_safety(store, {"global_gap_minutes": 0})
    assert check_can_publish(store, "cuenta2")[0]


# --- fotos repetidas -------------------------------------------------------------

def test_fotos_repetidas_se_omiten_sin_alerta(store, photo, monkeypatch):
    foto = photo()
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00",
                           job={"name": "Durags", "family_key": "durags", "listing_overrides": {"image_paths": [str(foto)]}})
    previa = enqueue_job(store, scheduled_at="2019-01-01T09:00:00", dedupe_key="previa",
                         job={"name": "Durags", "family_key": "durags", "listing_overrides": {"image_paths": [str(foto)]}})
    store.mark_published(previa)
    monkeypatch.setattr(worker, "schedule_window", lambda config: (True, datetime.now()))
    monkeypatch.setattr(worker, "check_can_publish", lambda store, account: (True, "", "", ""))
    item = store.claim_due()
    worker.run_item(store, item, {"mode": "autonomous", "retry_delay_minutes": 30})
    assert store.get_queue_item(queue_id)["status"] == "skipped"
    assert not store.list_alerts()


# --- calendario y portadas --------------------------------------------------------

@pytest.fixture()
def catalogo(monkeypatch, photo):
    """Familias sinteticas con varias fotos, igual que en test_scheduler."""
    def make(familias: int = 3, fotos_por_familia: int = 3):
        jobs = []
        for indice in range(familias):
            imagenes = [str(photo(f"familia{indice}-foto{n}")) for n in range(fotos_por_familia)]
            jobs.append({
                "name": f"Familia {indice}", "family_key": f"familia{indice}", "image_paths": imagenes,
                "excel": "ArticulosGenerados.xlsx", "images_root": "imagenes_firupost",
            })
        monkeypatch.setattr(scheduler, "build_family_jobs", lambda *a, **k: list(jobs))
        monkeypatch.setattr(scheduler, "custom_rotation_jobs", lambda *a, **k: [])
        return jobs

    return make


def test_rotar_portada_cambia_la_primera_foto():
    fotos = ["BLA/1", "GRI/1", "NEG/1", "BLA/2"]
    assert scheduler.rotate_cover(fotos, 0) == fotos
    assert scheduler.rotate_cover(fotos, 1)[0] == "GRI/1"
    assert scheduler.rotate_cover(fotos, 2)[0] == "NEG/1"
    assert sorted(scheduler.rotate_cover(fotos, 5)) == sorted(fotos)
    assert scheduler.rotate_cover([], 3) == []


def test_portada_automatica_avanza_con_cada_publicacion(store, photo):
    fotos = [str(photo(f"foto{n}")) for n in range(3)]
    config = scheduler.normalize_config({"accounts": ["cuenta1", "cuenta2"], "account": "cuenta1"})
    seguridad = {"vary_cover": True}
    orden, modo = scheduler.apply_photo_order(store, config, seguridad, "cuenta1", "bolsos", fotos)
    assert modo == "auto" and orden[0] == fotos[0]
    previa = enqueue_job(store, family_key="bolsos", dedupe_key="previa")
    store.mark_published(previa)
    orden, _ = scheduler.apply_photo_order(store, config, seguridad, "cuenta2", "bolsos", fotos)
    assert orden[0] == fotos[1], "la siguiente publicacion abre con otra foto"
    orden, _ = scheduler.apply_photo_order(store, config, seguridad, "cuenta2", "bolsos", fotos, extra_offset=1)
    assert orden[0] == fotos[2]
    assert scheduler.apply_photo_order(store, config, {"vary_cover": False}, "cuenta1", "bolsos", fotos) == (fotos, "fijo")


def test_orden_manual_respeta_lo_elegido_en_el_panel(store, photo):
    fotos = [str(photo(f"foto{n}")) for n in range(3)]
    config = scheduler.normalize_config({
        "accounts": ["cuenta1"], "account": "cuenta1",
        "account_media": {"cuenta1": {"bolsos": [fotos[2], fotos[0]]}},
        "photo_order_manual": {"cuenta1": {"bolsos": True, "otra": False}},
    })
    assert config["photo_order_manual"] == {"cuenta1": {"bolsos": True}}
    orden, modo = scheduler.apply_photo_order(store, config, {"vary_cover": True}, "cuenta1", "bolsos", fotos)
    assert modo == "manual"
    assert orden == [fotos[2], fotos[0], fotos[1]]


def test_el_trabajador_aplica_el_orden_al_publicar(store, photo, monkeypatch):
    fotos = [str(photo(f"foto{n}")) for n in range(3)]
    previa = enqueue_job(store, family_key="bolsos", dedupe_key="previa")
    store.mark_published(previa)
    item = {"account": "cuenta1", "family_key": "bolsos", "name": "Bolsos",
            "job": {"listing_overrides": {"image_paths": fotos}}}
    ordenado = worker.with_photo_order(store, item, scheduler.normalize_config({"accounts": ["cuenta1"]}))
    assert ordenado["job"]["listing_overrides"]["image_paths"][0] == fotos[1]
    assert item["job"]["listing_overrides"]["image_paths"] == fotos, "no modifica el trabajo guardado"


def test_no_se_programan_dos_publicaciones_en_el_mismo_espacio(store, catalogo):
    catalogo(familias=4)
    config = scheduler.normalize_config({
        "accounts": ["cuenta1"], "account": "cuenta1", "active_days": list(range(7)),
        "start_time": "09:00", "end_time": "18:00", "max_per_day": 3, "interval_minutes": 180,
        "horizon_days": 1, "cooldown_days": 0, "slot_jitter_minutes": 0,
    })
    save_safety(store, {"warmup_enabled": False})
    inicio = date.today() + timedelta(days=1)
    scheduler.generate_week(store, config, start_date=inicio)
    # Una segunda generacion sobre la misma semana no agrega nada encima.
    scheduler.generate_week(store, config, start_date=inicio)
    horas = [item["scheduled_at"][:16] for item in store.list_queue()]
    assert len(horas) == len(set(horas))


def test_un_solo_trabajador_a_la_vez(monkeypatch, tmp_path):
    import marketplace_runtime

    monkeypatch.setattr(marketplace_runtime, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(worker, "_INSTANCE_LOCK", None)
    assert worker.acquire_single_instance()
    otro = marketplace_runtime.AccountLock("scheduler_worker")
    with pytest.raises(RuntimeError):
        otro.acquire()
    worker._INSTANCE_LOCK.release()


# --- duplicados y reporte de portadas ------------------------------------------------

HOY = date(2026, 10, 6)


def tarjeta(titulo, fecha, clics):
    texto = "\n".join([titulo, "NIO320", "In stock", f" · Listed on {fecha}", "Listed on Marketplace", f"{clics} clicks on listing"])
    return listings.parse_card({"texto": texto, "hrefs": [], "botones": []}, HOY)


def test_entre_duplicados_se_conserva_el_de_mas_interes(store):
    listings.save_scan(store, "cuenta1", [
        tarjeta("Camisa sin mangas vieja", "9/14", 50),
        tarjeta("Camisa sin mangas nueva", "9/25", 3),
    ], today=HOY)
    listings.refresh_recommendations(store, {"publishable": ["CSM-BLA-L"], "without_photos": [], "price_issues": []}, today=HOY)
    rec = {item["title"]: item["recommendation"] for item in listings.list_listings(store)}
    assert rec["Camisa sin mangas vieja"] != "delete"
    assert rec["Camisa sin mangas nueva"] == "delete"


def test_reporte_de_portadas_por_producto(store, photo):
    blanca, negra = photo("blanca"), photo("negra")
    store.record_publish_event("cuenta1", "Bolso deportivo - Negro", "publicador", "2026-10-01T10:00:00", cover=str(blanca))
    store.record_publish_event("cuenta2", "Bolso deportivo - Negro", "publicador", "2026-10-01T13:00:00", cover=str(negra))
    listings.save_scan(store, "cuenta1", [tarjeta("Bolso deportivo - Negro", "10/1", 10)], today=HOY)
    listings.save_scan(store, "cuenta2", [tarjeta("Bolso deportivo - Negro", "10/1", 40)], today=HOY)
    reporte = listings.cover_report(store, today=HOY)
    assert [(item["family_key"], item["listings"]) for item in reporte] == [("bolsos", 1), ("bolsos", 1)]
    assert reporte[0]["clicks_per_day"] > reporte[1]["clicks_per_day"]
    assert reporte[0]["accounts"] == ["cuenta2"]


def test_una_sincronizacion_buena_cierra_las_alertas_del_sgi(store, monkeypatch):
    store.create_alert("warning", "SGI_UNAVAILABLE", "timeout")
    import sgi_sync

    monkeypatch.setattr(sgi_sync, "sync_from_local_configuration", lambda: {
        "generated_at": "x", "price_issues": [], "publishable": [], "without_photos": [], "drive_offline": "",
    })
    worker.refresh_from_sgi(store)
    assert not store.list_alerts()

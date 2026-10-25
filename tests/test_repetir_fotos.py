"""Volver a publicar en una cuenta un producto con fotos ya usadas: 14 dias, sin anuncio activo y con aprobacion."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import marketplace_listings as listings
import marketplace_scheduler as scheduler
import marketplace_scheduler_worker as worker
from conftest import enqueue_job
from marketplace_safety import load_safety, save_safety


def usar_fotos(store, cuenta, fotos, hace_dias, familia="bolsos"):
    queue_id = enqueue_job(store, account=cuenta, family_key=familia, dedupe_key=f"uso-{cuenta}-{hace_dias}",
                           fingerprint=f"huella-vieja-{cuenta}-{hace_dias}",
                           job={"name": "Bolsos", "family_key": familia,
                                "listing_overrides": {"image_paths": [str(f) for f in fotos]}})
    store.mark_published(queue_id)
    momento = (datetime.now() - timedelta(days=hace_dias)).isoformat(timespec="seconds")
    with store.connect() as connection:
        connection.execute("UPDATE media_usage SET used_at=? WHERE account=?", (momento, cuenta))
        connection.execute("UPDATE publications SET published_at=? WHERE queue_id=?", (momento, queue_id))


def revisar_anuncios(store, cuenta, *tarjetas):
    hoy = date.today()
    listings.save_scan(store, cuenta, [listings.parse_card({"texto": t, "hrefs": [], "botones": []}, hoy) for t in tarjetas], today=hoy)
    store.heartbeat(f"listings_scan:{cuenta}", {"status": "ok", "attempted_at_epoch": 0})


def tarjeta(titulo, estado, fecha):
    return "\n".join([titulo, "NIO650", estado, f" · Listed on {fecha}", "Listed on Marketplace", "3 clicks on listing"])


def test_fotos_nuevas_siempre_se_permiten(store, photo):
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", [photo()], load_safety(store)) == (True, None)


def test_antes_de_14_dias_no_se_repite(store, photo):
    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=10)
    revisar_anuncios(store, "cuenta1")
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))[0] is False


def test_despues_de_14_dias_sin_anuncio_activo_se_propone_borrar_el_agotado(store, photo):
    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=20)
    hace = (date.today() - timedelta(days=20))
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "Out of Stock", f"{hace.month}/{hace.day}"))
    ok, reuse = scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))
    assert ok and reuse["days"] >= 20
    assert reuse["replace_listing"]["title"] == "Bolso deportivo - Negro"
    assert "se borra el anuncio anterior" in scheduler.reuse_detail(reuse)


def test_con_anuncio_activo_no_se_repite(store, photo):
    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=30)
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "In stock", "9/1"))
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))[0] is False


def test_sin_revision_de_la_cuenta_no_se_arriesga(store, photo):
    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=30)
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))[0] is False


def test_el_calendario_deja_la_repeticion_por_aprobar_aunque_sea_autonomo(store, photo, monkeypatch):
    fotos = [photo("a"), photo("b")]
    usar_fotos(store, "cuenta1", fotos, hace_dias=20)
    revisar_anuncios(store, "cuenta1")
    monkeypatch.setattr(scheduler, "build_family_jobs", lambda *a, **k: [{
        "name": "Bolsos", "family_key": "bolsos", "image_paths": [str(f) for f in fotos],
        "excel": "ArticulosGenerados.xlsx", "images_root": "imagenes_firupost",
    }])
    monkeypatch.setattr(scheduler, "custom_rotation_jobs", lambda *a, **k: [])
    save_safety(store, {"warmup_enabled": False, "family_repeat_days": 0})
    config = scheduler.normalize_config({
        "mode": "autonomous", "accounts": ["cuenta1"], "account": "cuenta1", "active_days": list(range(7)),
        "start_time": "09:00", "end_time": "18:00", "max_per_day": 1, "horizon_days": 1, "cooldown_days": 0,
    })
    scheduler.generate_week(store, config, start_date=date.today() + timedelta(days=1))
    nuevos = [item for item in store.list_queue() if item["status"] == "planned"]
    assert len(nuevos) == 1
    assert nuevos[0]["approved"] is False and nuevos[0]["job"]["reuse"]
    assert "Volver a publicar" in nuevos[0]["detail"]
    # La aprobacion automatica del modo autonomo no la toca; la de la persona si.
    assert scheduler.approve_items(store, include_reuse=False) == 0
    assert scheduler.approve_items(store) == 1


def test_repeticion_aprobada_borra_el_anterior_y_publica(store, photo, monkeypatch):
    import marketplace_listing_worker as lw

    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=20)
    hace = date.today() - timedelta(days=20)
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "Out of Stock", f"{hace.month}/{hace.day}"))
    _, reuse = scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", family_key="bolsos", dedupe_key="repeticion",
                           job={"name": "Bolsos", "family_key": "bolsos", "reuse": reuse,
                                "listing_overrides": {"image_paths": [str(foto)]}})
    comandos = []
    monkeypatch.setattr(lw, "_run", lambda command: comandos.append(command) or (0, "[listings-done]Eliminar: hecho."))
    monkeypatch.setattr(worker, "ensure_chrome", lambda key, account: (True, "ok"))
    monkeypatch.setattr(worker, "check_can_publish", lambda store, account: (True, "", "", ""))
    monkeypatch.setattr(worker, "preflight", lambda item, cfg: (True, "", "ok"))
    monkeypatch.setattr(worker, "campaign_for_item", lambda item, cfg: "campana.json")
    import subprocess
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "[listing-url:x]", ""))
    item = store.claim_due()
    worker.run_item(store, item, {"mode": "autonomous", "retry_delay_minutes": 30})
    assert comandos and comandos[0][comandos[0].index("--action") + 1] == "delete"
    assert store.get_queue_item(queue_id)["status"] == "published"
    # La foto renueva su fecha de uso: los 14 dias vuelven a contar desde hoy.
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))[0] is False


def test_si_no_se_puede_borrar_el_anterior_no_publica(store, photo, monkeypatch):
    import marketplace_listing_worker as lw

    foto = photo()
    usar_fotos(store, "cuenta1", [foto], hace_dias=20)
    hace = date.today() - timedelta(days=20)
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "Out of Stock", f"{hace.month}/{hace.day}"))
    _, reuse = scheduler.reuse_check(store, "cuenta1", "bolsos", [foto], load_safety(store))
    queue_id = enqueue_job(store, scheduled_at="2020-01-01T09:00:00", family_key="bolsos", dedupe_key="repeticion",
                           job={"name": "Bolsos", "family_key": "bolsos", "reuse": reuse,
                                "listing_overrides": {"image_paths": [str(foto)]}})
    monkeypatch.setattr(lw, "_run", lambda command: (1, "[listings-error:INTERNAL] no aparecio la confirmacion"))
    monkeypatch.setattr(worker, "ensure_chrome", lambda key, account: (True, "ok"))
    monkeypatch.setattr(worker, "check_can_publish", lambda store, account: (True, "", "", ""))
    item = store.claim_due()
    worker.run_item(store, item, {"mode": "autonomous", "retry_delay_minutes": 30})
    assert store.get_queue_item(queue_id)["status"] == "blocked"
    assert [alert["code"] for alert in store.list_alerts()] == ["REUSE_DELETE_FAILED"]


def test_calendario_vacio_espera_una_hora_y_avisa_una_vez(store, monkeypatch):
    llamadas = []
    monkeypatch.setattr(worker, "generate_week", lambda store, config: llamadas.append(1) or {"created": 0})
    worker.ensure_future_calendar(store, {})
    worker.ensure_future_calendar(store, {})
    assert len(llamadas) == 1
    assert [alert["code"] for alert in store.list_alerts()] == ["CALENDAR_EMPTY"]


def borrar_desde_el_panel(store, cuenta, titulo, fecha):
    hoy = date.today()
    item = next(i for i in listings.list_listings(store, cuenta) if i["title"] == titulo)
    accion = listings.queue_action(store, item["key"], "delete")
    listings.finish_action(store, accion, "done", "Eliminar: hecho y comprobado en Facebook.")
    # La lectura siguiente ya no lo ve.
    listings.save_scan(store, cuenta, [listings.parse_card({"texto": tarjeta("Straps - Verde", "In stock", "9/1"),
                                                            "hrefs": [], "botones": []}, hoy)], today=hoy)


def test_lo_que_borras_en_anuncios_entra_primero_en_el_calendario(store, photo, monkeypatch):
    fotos = [photo("a")]
    usar_fotos(store, "cuenta1", fotos, hace_dias=3)  # menos de los 14 dias de espera
    hace = date.today() - timedelta(days=3)
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "In stock", f"{hace.month}/{hace.day}"))
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", fotos, load_safety(store))[0] is False

    borrar_desde_el_panel(store, "cuenta1", "Bolso deportivo - Negro", hace)
    ok, reuse = scheduler.reuse_check(store, "cuenta1", "bolsos", fotos, load_safety(store))
    assert ok and reuse["requested"] and reuse["replace_listing"] is None

    otras = [photo(f"otra{n}") for n in range(3)]
    monkeypatch.setattr(scheduler, "build_family_jobs", lambda *a, **k: [
        {"name": "Straps", "family_key": "straps", "image_paths": [str(otras[0])], "excel": "a.xlsx", "images_root": "f"},
        {"name": "Bolsos", "family_key": "bolsos", "image_paths": [str(f) for f in fotos], "excel": "a.xlsx", "images_root": "f"},
    ])
    monkeypatch.setattr(scheduler, "custom_rotation_jobs", lambda *a, **k: [])
    save_safety(store, {"warmup_enabled": False, "family_repeat_days": 0})
    config = scheduler.normalize_config({
        "mode": "autonomous", "accounts": ["cuenta1"], "account": "cuenta1", "active_days": list(range(7)),
        "start_time": "09:00", "end_time": "18:00", "max_per_day": 1, "horizon_days": 2, "cooldown_days": 0,
    })
    scheduler.generate_week(store, config, start_date=date.today() + timedelta(days=1))
    nuevos = sorted((i for i in store.list_queue() if i["status"] in ("queued", "planned")), key=lambda i: i["scheduled_at"])
    # Primero el que borraste, y sin la aprobacion especial de las repeticiones.
    assert nuevos[0]["family_key"] == "bolsos"
    assert nuevos[0]["status"] == "queued" and nuevos[0]["approved"] is True
    assert "lo borraste desde Anuncios" in nuevos[0]["detail"]
    assert all(item["family_key"] != "bolsos" for item in nuevos[1:]), "no se programa dos veces"


def test_tras_volver_a_publicarlo_ya_no_se_salta_la_espera(store, photo):
    fotos = [photo("a")]
    usar_fotos(store, "cuenta1", fotos, hace_dias=3)
    hace = date.today() - timedelta(days=3)
    revisar_anuncios(store, "cuenta1", tarjeta("Bolso deportivo - Negro", "In stock", f"{hace.month}/{hace.day}"))
    borrar_desde_el_panel(store, "cuenta1", "Bolso deportivo - Negro", hace)
    # Se volvio a publicar con esas fotos: la peticion queda cumplida.
    usar_fotos(store, "cuenta1", fotos, hace_dias=0)
    assert scheduler.reuse_check(store, "cuenta1", "bolsos", fotos, load_safety(store))[0] is False

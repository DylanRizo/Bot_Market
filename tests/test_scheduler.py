"""Pruebas del planificador: espacios del calendario y rotacion de productos."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import marketplace_scheduler as scheduler
from marketplace_scheduler import build_slots, generate_week, jitter_span_minutes, normalize_config


def base_config(**overrides):
    config = {
        "active_days": [0, 1, 2, 3, 4, 5, 6],
        "start_time": "09:00",
        "end_time": "18:00",
        "max_per_day": 3,
        "interval_minutes": 180,
        "horizon_days": 3,
        "cooldown_days": 3,
        "accounts": ["cuenta1"],
        "account": "cuenta1",
        "slot_jitter_minutes": 0,
    }
    config.update(overrides)
    return normalize_config(config)


AHORA = datetime(2026, 9, 7, 6, 0, 0)   # lunes


def test_una_seleccion_de_fotos_anterior_al_sgi_no_llega_al_anuncio(tmp_path):
    vieja = tmp_path / "TSBK-M" / "foto_1.jpg"
    nueva = tmp_path / "CMP-NEG-L" / "foto_3.jpg"
    for foto in (vieja, nueva):
        foto.parent.mkdir()
        foto.write_bytes(foto.parent.name.encode())
    job = {
        "family_key": "compression_short",
        "sku_prefixes": ["CMP-"],
        "listing_overrides": {"image_paths": ["por-defecto.jpg"]},
    }

    solo_vieja = {"account_media": {"cuenta1": {"compression_short": [str(vieja)]}}}
    assert scheduler.job_for_account(job, "cuenta1", solo_vieja)["listing_overrides"]["image_paths"] == ["por-defecto.jpg"]

    mezcla = {"account_media": {"cuenta1": {"compression_short": [str(vieja), str(nueva)]}}}
    assert scheduler.job_for_account(job, "cuenta1", mezcla)["listing_overrides"]["image_paths"] == [str(nueva)]


def test_build_slots_respeta_dias_activos():
    config = base_config(active_days=[0], horizon_days=7)
    slots = build_slots(config, start_date=AHORA.date(), now=AHORA)
    assert {slot.weekday() for slot in slots} == {0}


def test_build_slots_respeta_maximo_diario_y_ventana_horaria():
    config = base_config(max_per_day=2, horizon_days=2)
    slots = build_slots(config, start_date=AHORA.date(), now=AHORA)
    assert len(slots) == 4
    for slot in slots:
        assert 9 <= slot.hour <= 18


def test_build_slots_no_programa_en_el_pasado():
    tarde = datetime(2026, 9, 7, 14, 0, 0)
    config = base_config(horizon_days=1)
    slots = build_slots(config, start_date=tarde.date(), now=tarde)
    assert all(slot > tarde for slot in slots)


def test_jitter_mueve_los_horarios_sin_desordenarlos():
    config = base_config(slot_jitter_minutes=7)
    slots = build_slots(config, start_date=AHORA.date(), now=AHORA)
    exactos = build_slots(base_config(slot_jitter_minutes=0), start_date=AHORA.date(), now=AHORA)
    assert slots == sorted(slots), "los espacios deben quedar en orden"
    assert slots != exactos, "con jitter no deben caer todos en la hora clavada"
    for movido, exacto in zip(slots, exactos):
        assert abs((movido - exacto).total_seconds()) <= 7 * 60


def test_jitter_es_estable_entre_regeneraciones():
    config = base_config(slot_jitter_minutes=7)
    primera = build_slots(config, start_date=AHORA.date(), now=AHORA)
    segunda = build_slots(config, start_date=AHORA.date(), now=AHORA)
    assert primera == segunda


def test_jitter_nunca_supera_la_mitad_del_intervalo():
    """Con intervalos cortos el desplazamiento se recorta para no solapar espacios."""
    assert jitter_span_minutes(base_config(interval_minutes=30, slot_jitter_minutes=30)) == 14
    assert jitter_span_minutes(base_config(interval_minutes=180, slot_jitter_minutes=7)) == 7
    assert jitter_span_minutes(base_config(slot_jitter_minutes=0)) == 0


def test_normalize_config_acota_el_jitter():
    assert normalize_config({"slot_jitter_minutes": 999})["slot_jitter_minutes"] == 30
    assert normalize_config({"slot_jitter_minutes": -5})["slot_jitter_minutes"] == 0


def test_normalize_config_rechaza_horarios_invertidos():
    with pytest.raises(ValueError):
        normalize_config({"start_time": "18:00", "end_time": "09:00"})


# --- generate_week -----------------------------------------------------------

@pytest.fixture()
def catalogo(monkeypatch, photo):
    """Sustituye el catalogo real por familias sinteticas con fotos propias."""
    def make(familias: int = 3, fotos_por_familia: int = 1):
        jobs = []
        for indice in range(familias):
            imagenes = [str(photo(f"familia{indice}-foto{n}")) for n in range(fotos_por_familia)]
            jobs.append(
                {
                    "name": f"Familia {indice}",
                    "family_key": f"familia{indice}",
                    "image_paths": imagenes,
                    "excel": "ArticulosGenerados.xlsx",
                    "images_root": "imagenes_firupost",
                }
            )
        monkeypatch.setattr(scheduler, "build_family_jobs", lambda *a, **k: list(jobs))
        monkeypatch.setattr(scheduler, "custom_rotation_jobs", lambda *a, **k: [])
        return jobs

    return make


def test_generate_week_crea_calendario_y_no_repite_fotos(store, catalogo):
    catalogo(familias=3)
    config = base_config(horizon_days=2, max_per_day=2)
    resultado = generate_week(store, config, start_date=date.today() + timedelta(days=1))
    assert resultado["created"] > 0
    cola = store.list_queue()
    usadas = []
    for item in cola:
        usadas.extend(str(path) for path in store.job_image_paths(item["job"]))
    assert len(usadas) == len(set(usadas)), "una misma foto no puede quedar en dos anuncios"


def test_generate_week_respeta_el_cooldown_de_familia(store, catalogo):
    catalogo(familias=1)
    config = base_config(horizon_days=3, max_per_day=3, cooldown_days=3)
    resultado = generate_week(store, config, start_date=date.today() + timedelta(days=1))
    familias = [item["family_key"] for item in store.list_queue()]
    assert len(familias) <= 1, "con una sola familia y cooldown de 3 dias solo cabe un anuncio"
    assert resultado["skipped_slots"] > 0


def test_generate_week_alterna_cuentas_en_round_robin(store, catalogo):
    catalogo(familias=6)
    config = base_config(
        accounts=["cuenta1", "cuenta2"], account_strategy="round_robin",
        horizon_days=2, max_per_day=2, cooldown_days=0,
    )
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    cuentas = {item["account"] for item in store.list_queue()}
    assert cuentas == {"cuenta1", "cuenta2"}


def test_generate_week_publica_en_todas_las_cuentas_y_separa_los_horarios(store, catalogo):
    catalogo(familias=4)
    config = base_config(
        accounts=["cuenta1", "cuenta2"], account_strategy="all_accounts",
        horizon_days=1, max_per_day=1, cooldown_days=0, slot_jitter_minutes=0,
    )
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    cola = sorted(store.list_queue(), key=lambda item: item["scheduled_at"])
    assert {item["account"] for item in cola} == {"cuenta1", "cuenta2"}
    primero = datetime.fromisoformat(cola[0]["scheduled_at"])
    segundo = datetime.fromisoformat(cola[1]["scheduled_at"])
    # Con la proteccion activa las cuentas se reparten dentro del intervalo
    # (180 min, 2 cuentas): 85 min entre una y otra, no 7.
    assert segundo - primero == timedelta(minutes=85)


def test_sin_proteccion_las_cuentas_se_separan_siete_minutos(store, catalogo):
    from marketplace_safety import save_safety

    save_safety(store, {"enabled": False})
    catalogo(familias=4)
    config = base_config(
        accounts=["cuenta1", "cuenta2"], account_strategy="all_accounts",
        horizon_days=1, max_per_day=1, cooldown_days=0, slot_jitter_minutes=0,
    )
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    cola = sorted(store.list_queue(), key=lambda item: item["scheduled_at"])
    primero = datetime.fromisoformat(cola[0]["scheduled_at"])
    segundo = datetime.fromisoformat(cola[1]["scheduled_at"])
    assert segundo - primero == timedelta(minutes=7)


def test_la_proteccion_limita_los_anuncios_por_cuenta_al_dia(store, catalogo):
    from marketplace_safety import save_safety

    save_safety(store, {"max_per_account_day": 2, "warmup_enabled": False})
    catalogo(familias=6)
    config = base_config(
        accounts=["cuenta1"], horizon_days=1, max_per_day=5, interval_minutes=60, cooldown_days=0,
    )
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    assert len(store.list_queue()) == 2


def test_cuenta_en_calentamiento_recibe_un_anuncio_al_dia(store, catalogo):
    catalogo(familias=6)
    config = base_config(
        accounts=["cuenta1"], horizon_days=2, max_per_day=5, interval_minutes=60, cooldown_days=0,
    )
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    dias = [item["scheduled_at"][:10] for item in store.list_queue()]
    assert len(dias) == 2 and len(set(dias)) == 2


def test_generate_week_en_modo_autonomo_deja_todo_aprobado(store, catalogo):
    catalogo(familias=3)
    config = base_config(mode="autonomous", horizon_days=2, max_per_day=1, cooldown_days=0)
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    for item in store.list_queue():
        assert item["status"] == "queued"
        assert item["approved"] is True


def test_generate_week_en_modo_supervisado_no_aprueba_nada(store, catalogo):
    catalogo(familias=3)
    config = base_config(mode="supervised", horizon_days=2, max_per_day=1, cooldown_days=0)
    generate_week(store, config, start_date=date.today() + timedelta(days=1))
    for item in store.list_queue():
        assert item["status"] == "planned"
        assert item["approved"] is False


def test_generate_week_falla_claro_si_no_hay_productos(store, monkeypatch):
    monkeypatch.setattr(scheduler, "build_family_jobs", lambda *a, **k: [])
    monkeypatch.setattr(scheduler, "custom_rotation_jobs", lambda *a, **k: [])
    with pytest.raises(RuntimeError, match="No hay familias"):
        generate_week(store, base_config())

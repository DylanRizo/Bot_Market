"""Pruebas del seguimiento de anuncios publicados: lectura, recomendaciones y acciones."""
from __future__ import annotations

import json
from datetime import date, datetime

import pytest

import marketplace_listing_worker as lw
import marketplace_listings as listings


HOY = date(2026, 9, 25)


def tarjeta(titulo, precio="NIO320", estado="In stock", fecha="9/25", clics="2 clicks on listing",
            grupos=False, hrefs=None, botones=None):
    donde = "Listed on Marketplace and at least 1 group" if grupos else "Listed on Marketplace"
    texto = "\n".join([titulo, precio, estado, "\xa0", f" · Listed on {fecha}", donde, "\xa0", " · ", clics,
                       "Mark out of stock" if estado == "In stock" else "Mark as in stock"])
    return {"texto": texto, "hrefs": hrefs or [], "botones": botones or []}


# --- lectura -----------------------------------------------------------------

def test_lee_una_tarjeta_activa():
    card = listings.parse_card(tarjeta(
        "Bolso deportivo - Negro, Rojo y Rosa", "NIO650", fecha="9/24", clics="11 clicks on listing", grupos=True,
        hrefs=["/ad_center/create/listingad/?entry_point=X&page_id=1&target_id=28409619035399561"],
    ), HOY)
    assert card["title"] == "Bolso deportivo - Negro, Rojo y Rosa"
    assert card["price"] == 650
    assert card["status"] == "in_stock"
    assert card["listed_on"] == "2026-09-24"
    assert card["clicks_14d"] == 11
    assert card["in_groups"] is True
    assert card["fb_id"] == "28409619035399561"


def test_lee_precio_rebajado_estado_vendido_y_clic_en_singular():
    card = listings.parse_card(tarjeta(
        "Bolsos para deporte", "NIO650NIO700", estado="Sold", fecha="7/7", clics="1 click on listing",
        botones=["Relist this item Bolsos para deporte"],
    ), HOY)
    assert card["price"] == 650
    assert card["status"] == "sold"
    assert card["clicks_14d"] == 1
    assert card["can_relist"] is True


def test_fecha_sin_ano_del_futuro_es_del_ano_anterior():
    assert listings.parse_listed_on("Listed on 12/20", HOY) == date(2025, 12, 20)
    assert listings.parse_listed_on("Listed on 3/4", HOY) == date(2026, 3, 4)
    assert listings.parse_listed_on("Publicado el 25/9", HOY) == date(2026, 9, 25)


@pytest.mark.parametrize("titulo, familia", [
    ("Camisa de compresion manga corta en Blanco - Tallas S, M", "compression_short"),
    ("Camiseta sin mangas de compresión", "compression_sleeveless"),
    ("Camisa de compresión manga larga", "compression_long"),
    ("Leggins de campana - Negro y Gris", "leggins"),
    ("Short deportivo YoungLA - Beige", "shorts_youngla"),
    ("Short deportivo en Gris y Negro", "shorts"),
    ("Par de Muñequeras Profesionales", "munequeras"),
    ("Straps para gimnasio - Verde", "straps"),
    ("Enterizo largo deportivo - Cafe", "enterizos_largos"),
    ("Calcetas de compresion 15-20 mmHg - Negro", "calcetas"),
    ("Calcetas - Multicolor", "calcetas_multicolor"),
    ("Cosa rara sin familia", ""),
])
def test_reconoce_la_familia_por_el_titulo(titulo, familia):
    assert listings.family_for_title(titulo) == familia


# --- recomendaciones ---------------------------------------------------------

def anuncio(estado="in_stock", dias=10, clics=2):
    fecha = date.fromordinal(HOY.toordinal() - dias).isoformat()
    return {"status": estado, "listed_on": fecha, "clicks_14d": clics}


def rec(listing, **kw):
    kw.setdefault("has_stock", True)
    return listings.recommend(listing, HOY, **kw)[0]


def test_recien_publicado_se_espera():
    assert rec(anuncio(dias=1, clics=0)) == "wait"


def test_poco_interes_despues_de_una_semana_se_renueva():
    assert rec(anuncio(dias=8, clics=2)) == "renew"


def test_renovado_hace_poco_se_espera():
    assert rec(anuncio(dias=12, clics=1), last_renew="2026-09-22T10:00:00", renew_count=1) == "wait"


def test_renovado_varias_veces_sin_interes_pide_cambiar_foto():
    assert rec(anuncio(dias=25, clics=1), last_renew="2026-09-10T10:00:00", renew_count=2) == "edit"


def test_mucho_interes_sin_venta_pide_revisar_precio():
    assert rec(anuncio(dias=9, clics=30)) == "review"
    assert rec(anuncio(dias=4, clics=30)) == "keep"


def test_viejo_y_sin_interes_se_elimina():
    assert rec(anuncio(dias=50, clics=1)) == "delete"


def test_sin_stock_en_el_sgi_se_marca_agotado():
    assert rec(anuncio(dias=2, clics=9), has_stock=False) == "mark_out_of_stock"


def test_sin_sgi_no_se_decide_por_stock():
    assert rec(anuncio(dias=5, clics=9), has_stock=None) == "keep"


def test_duplicado_mas_viejo_se_elimina():
    assert rec(anuncio(dias=9, clics=6), newer_active_duplicate=True) == "delete"


def test_agotado_con_stock_se_republica_solo_si_no_lo_cubre_el_bot():
    agotado = anuncio(estado="out_of_stock", dias=30, clics=0)
    assert rec(agotado) == "relist"
    assert rec(agotado, in_rotation=True) == "none"
    assert rec(agotado, active_same_family=True) == "none"
    assert rec(agotado, has_stock=False) == "none"


def test_vendido_no_se_toca():
    assert rec(anuncio(estado="sold", dias=80, clics=0)) == "none"


# --- base de datos y cola ------------------------------------------------------

def cards_para(store, cuenta, *cards):
    return listings.save_scan(store, cuenta, [listings.parse_card(c, HOY) for c in cards], today=HOY)


def test_guardar_marca_ausentes_y_detecta_los_del_bot(store):
    store.record_publish_event("cuenta1", "Bolso deportivo - Negro", "publicador")
    cards_para(store, "cuenta1", tarjeta("Bolso deportivo - Negro", fecha="9/20"), tarjeta("Straps - Verde", fecha="9/10"))
    cards_para(store, "cuenta1", tarjeta("Bolso deportivo - Negro", fecha="9/20", clics="7 clicks on listing"))
    presentes = listings.list_listings(store, "cuenta1")
    assert [item["title"] for item in presentes] == ["Bolso deportivo - Negro"]
    assert presentes[0]["clicks_14d"] == 7
    assert presentes[0]["by_bot"] == 1
    assert presentes[0]["family_key"] == "bolsos"
    todos = listings.list_listings(store, "cuenta1", present_only=False)
    assert len(todos) == 2


def test_recomendaciones_usan_el_stock_del_sgi_y_los_duplicados(store):
    cards_para(store, "cuenta1",
               tarjeta("Bolso deportivo viejo", fecha="9/10", clics="3 clicks on listing"),
               tarjeta("Bolso deportivo nuevo", fecha="9/22"),
               tarjeta("Straps para gimnasio", fecha="9/01"))
    reporte = {"publishable": ["BOL-NEG-U"], "without_photos": [], "price_issues": []}
    listings.refresh_recommendations(store, reporte, today=HOY)
    por_titulo = {item["title"]: item for item in listings.list_listings(store)}
    assert por_titulo["Bolso deportivo viejo"]["recommendation"] == "delete"
    assert por_titulo["Bolso deportivo nuevo"]["recommendation"] == "keep"
    assert por_titulo["Straps para gimnasio"]["recommendation"] == "mark_out_of_stock"


def test_la_cola_no_duplica_y_rechaza_acciones_manuales(store):
    cards_para(store, "cuenta1", tarjeta("Bolso deportivo", fecha="9/10"))
    key = listings.list_listings(store)[0]["key"]
    primero = listings.queue_action(store, key, "renew")
    assert listings.queue_action(store, key, "renew") == primero
    with pytest.raises(ValueError):
        listings.queue_action(store, key, "edit")


def test_limite_de_renovaciones_por_dia(store):
    cards_para(store, "cuenta1", *[tarjeta(f"Anuncio {i}", fecha="9/10") for i in range(4)])
    for item in listings.list_listings(store)[:3]:
        action_id = listings.queue_action(store, item["key"], "renew")
        listings.finish_action(store, action_id, "done", "ok")
    ok, detalle = listings.action_allowed(store, "cuenta1", "renew")
    assert not ok and "renovaron" in detalle


# --- trabajador --------------------------------------------------------------

CONFIG = {
    "enabled": True, "mode": "semiautomatic", "accounts": ["cuenta1"], "active_days": [0, 1, 2, 3, 4, 5, 6],
    "start_time": "00:00", "end_time": "23:59", "selected_families": [],
}


@pytest.fixture()
def cuentas(monkeypatch):
    monkeypatch.setattr(lw, "_accounts", lambda: {"cuenta1": {"debugger_address": "127.0.0.1:9999"}})
    monkeypatch.setattr(lw, "load_report", lambda: {"publishable": [], "without_photos": [], "price_issues": []})
    monkeypatch.setattr(lw, "ACTION_GAP_SECONDS", 0)


def chrome_ok(key, account):
    return True, "ok"


def test_el_trabajador_lee_los_anuncios_y_encola_los_agotados(store, cuentas):
    llamadas = []

    def run(command):
        llamadas.append(command)
        raw = [tarjeta("Straps para gimnasio", fecha="9/20")]
        return 0, "[listings-json]" + json.dumps([listings.parse_card(c, date.today()) for c in raw])

    assert lw.run_listing_work(store, CONFIG, chrome_ok, log=lambda m: None, run=run)
    assert llamadas[0][2] == "scan"
    pendientes = listings.pending_actions(store)
    assert [(p["action"], p["origin"]) for p in pendientes] == [("mark_out_of_stock", "auto")]
    # La lectura queda hecha: la siguiente vuelta ejecuta la accion, no vuelve a leer.
    assert not lw.scan_due(store, "cuenta1")


def test_el_trabajador_ejecuta_la_accion_aprobada(store, cuentas):
    cards_para(store, "cuenta1", tarjeta("Bolso deportivo", fecha="9/10"))
    key = listings.list_listings(store)[0]["key"]
    listings.queue_action(store, key, "renew")
    store.heartbeat(lw.scan_state_key("cuenta1"), {"status": "ok", "attempted_at_epoch": 9e12})
    comandos = []

    def run(command):
        comandos.append(command)
        return 0, "[listings-done]Renovar: hecho."

    assert lw.run_listing_work(store, CONFIG, chrome_ok, log=lambda m: None, run=run)
    assert "--action" in comandos[0] and comandos[0][comandos[0].index("--action") + 1] == "renew"
    assert not listings.pending_actions(store)
    assert listings.action_history(store, key, "renew")[1] == 1
    assert lw.scan_due(store, "cuenta1"), "tras actuar se vuelve a leer para ver el cambio"


def test_una_restriccion_al_actuar_pausa_la_cuenta(store, cuentas):
    from marketplace_safety import active_pauses

    cards_para(store, "cuenta1", tarjeta("Bolso deportivo", fecha="9/10"))
    key = listings.list_listings(store)[0]["key"]
    listings.queue_action(store, key, "delete")
    lw.run_listing_work(store, CONFIG, chrome_ok, log=lambda m: None,
                        run=lambda c: (1, "[listings-error:ACCOUNT_RESTRICTED] aviso"))
    assert "cuenta1" in active_pauses(store)


def test_fuera_de_horario_no_toca_facebook(store, cuentas):
    config = {**CONFIG, "start_time": "09:00", "end_time": "18:00"}
    llamado = []
    hecho = lw.run_listing_work(store, config, chrome_ok, log=lambda m: None,
                                now=datetime(2026, 9, 25, 22, 0), run=lambda c: llamado.append(c) or (0, ""))
    assert not hecho and not llamado


def test_solo_se_recomienda_republicar_un_anuncio_por_producto(store):
    cards_para(store, "cuenta1",
               tarjeta("Medias altas de descanso", estado="Out of Stock", fecha="8/09", clics="2 clicks on listing"),
               tarjeta("Medias con presion 15-20", estado="Out of Stock", fecha="8/04", clics="0 clicks on listing"),
               tarjeta("Calcetas locas y divertidas", estado="Out of Stock", fecha="4/10", clics="8 clicks on listing"))
    # Las de compresion tienen stock; las multicolor ("calcetas locas") son otro producto.
    reporte = {"publishable": ["CAL-15NEG-M"], "without_photos": [], "price_issues": []}
    listings.refresh_recommendations(store, reporte, today=HOY)
    recomendadas = [item["title"] for item in listings.list_listings(store) if item["recommendation"] == "relist"]
    assert recomendadas == ["Medias altas de descanso"]


def test_una_lectura_incompleta_no_marca_anuncios_como_desaparecidos(store):
    cards_para(store, "cuenta1", *[tarjeta(f"Anuncio {i}", fecha="9/10") for i in range(10)])
    cards_para(store, "cuenta1", *[tarjeta(f"Anuncio {i}", fecha="9/10") for i in range(4)])
    assert len(listings.list_listings(store, "cuenta1")) == 10
    cards_para(store, "cuenta1", *[tarjeta(f"Anuncio {i}", fecha="9/10") for i in range(8)])
    assert len(listings.list_listings(store, "cuenta1")) == 8


def test_el_chaleco_sin_mangas_no_se_confunde_con_la_camisa():
    assert listings.family_for_title("Chaleco de lana sin mangas | Tallas S a L | Azul") == "chalecos"
    assert listings.family_for_title("Camisa sin mangas | Tallas S a XL") == "compression_sleeveless"


def test_estado_active_cuenta_como_activo():
    card = listings.parse_card(tarjeta("Medias altas", estado="Active", fecha="9/20"), HOY)
    assert card["status"] == "in_stock"


# --- avisos de Facebook, gemelos y copias entre cuentas ---------------------------

def test_el_consejo_de_facebook_no_es_el_titulo():
    raw = tarjeta("Chaleco de lana sin mangas | Tallas S a L", clics="96 clicks on listing")
    raw["texto"] = "Tip: Renew your listing?\n" + raw["texto"]
    card = listings.parse_card(raw, HOY)
    assert card["title"] == "Chaleco de lana sin mangas | Tallas S a L"
    assert card["clicks_14d"] == 96


def test_dos_anuncios_identicos_se_guardan_aparte_y_no_se_tocan(store):
    cards_para(store, "cuenta1",
               tarjeta("Medias Altas de Descanso", fecha="9/10", clics="0 clicks on listing"),
               tarjeta("Medias Altas de Descanso", fecha="9/10", clics="4 clicks on listing"))
    gemelos = [item for item in listings.list_listings(store) if item["title"] == "Medias Altas de Descanso"]
    assert sorted(item["clicks_14d"] for item in gemelos) == [0, 4]
    listings.refresh_recommendations(store, {"publishable": ["CAL-15NEG-M"], "without_photos": [], "price_issues": []},
                                     today=HOY)
    gemelos = [item for item in listings.list_listings(store) if item["title"] == "Medias Altas de Descanso"]
    pobre = min(gemelos, key=lambda item: item["clicks_14d"])
    assert pobre["recommendation"] == "review" and "a mano" in pobre["reason"]
    # Ninguna accion automatica: los botones se buscan por titulo y tocarian cualquiera.
    with pytest.raises(ValueError, match="dos anuncios iguales"):
        listings.queue_action(store, pobre["key"], "delete")


def test_copia_escondida_frente_a_otra_cuenta_se_propone_borrar(store):
    cards_para(store, "cuenta1", tarjeta("Chaleco de lana sin mangas - Azul", fecha="9/5", clics="0 clicks on listing"))
    cards_para(store, "cuenta2", tarjeta("Chaleco de lana sin mangas - Gris", fecha="9/8", clics="96 clicks on listing"))
    listings.refresh_recommendations(store, {"publishable": ["CHA-AZU-M"], "without_photos": [], "price_issues": []},
                                     today=HOY)
    por_cuenta = {item["account"]: item for item in listings.list_listings(store)}
    assert por_cuenta["cuenta1"]["recommendation"] == "delete"
    assert "cuenta2" in por_cuenta["cuenta1"]["reason"] and "96" in por_cuenta["cuenta1"]["reason"]
    assert por_cuenta["cuenta2"]["recommendation"] != "delete"

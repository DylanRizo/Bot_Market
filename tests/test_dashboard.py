"""Pruebas del panel: autenticacion, CSRF y que los archivos del frontend se sirvan.

El panel maneja el bot entero y hasta ahora no tenia ninguna prueba. Se levanta
un servidor real contra una base temporal, nunca contra la del usuario.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

SCRATCH_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def panel(tmp_path_factory):
    """Arranca el panel real en un puerto libre, con base y datos propios."""
    temporal = tmp_path_factory.mktemp("panel")
    os.environ["MARKETPLACE_BOT_DB"] = str(temporal / "panel.db")
    import marketplace_bot_dashboard as dashboard

    token = dashboard.issue_session_token()
    servidor = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
    hilo.start()
    base = f"http://127.0.0.1:{servidor.server_address[1]}"
    try:
        yield base, token, dashboard
    finally:
        servidor.shutdown()
        servidor.server_close()
        os.environ.pop("MARKETPLACE_BOT_DB", None)


def pedir(base, ruta, metodo="GET", headers=None, datos=None):
    peticion = urllib.request.Request(
        base + ruta, method=metodo, headers=headers or {},
        data=datos if metodo == "POST" else None,
    )
    try:
        with urllib.request.urlopen(peticion, timeout=15) as respuesta:
            return respuesta.status, respuesta.read(), dict(respuesta.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers or {})


# --- autenticacion -----------------------------------------------------------

def test_sin_token_todo_responde_401(panel):
    base, _, _ = panel
    for ruta in ("/", "/api/state", "/api/autonomy", "/static/app.js"):
        estado, _, _ = pedir(base, ruta)
        assert estado == 401, f"{ruta} deberia exigir la llave"


def test_token_invalido_no_sirve(panel):
    base, _, _ = panel
    estado, _, _ = pedir(base, "/api/state", headers={"Cookie": "mb_session=inventado"})
    assert estado == 401


def test_la_pagina_entrega_cookie_httponly(panel):
    base, token, _ = panel
    estado, cuerpo, cabeceras = pedir(base, f"/?token={token}")
    assert estado == 200
    cookie = cabeceras.get("Set-Cookie", "")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie


# --- CSRF --------------------------------------------------------------------

def test_post_con_cookie_pero_sin_cabecera_se_rechaza(panel):
    """Un formulario de otro sitio lleva la cookie, pero no puede poner cabeceras."""
    base, token, _ = panel
    estado, _, _ = pedir(base, "/api/autonomy/worker/start", "POST",
                         {"Cookie": f"mb_session={token}"}, b"{}")
    assert estado == 401


def test_post_con_origin_ajeno_se_rechaza(panel):
    base, token, _ = panel
    estado, _, _ = pedir(base, "/api/autonomy/worker/start", "POST", {
        "Cookie": f"mb_session={token}", "X-Marketplace-Token": token,
        "Origin": "http://sitio-ajeno.example",
    }, b"{}")
    assert estado == 401


# --- frontend en archivos ----------------------------------------------------

def test_la_pagina_enlaza_el_css_y_el_js(panel):
    base, token, _ = panel
    _, cuerpo, _ = pedir(base, f"/?token={token}")
    assert b'href="/static/app.css"' in cuerpo
    assert b'src="/static/app.js"' in cuerpo


def test_el_token_se_inyecta_en_la_pagina_y_no_queda_el_marcador(panel):
    base, token, _ = panel
    _, cuerpo, _ = pedir(base, f"/?token={token}")
    assert token.encode() in cuerpo
    assert b"__MB_TOKEN__" not in cuerpo


@pytest.mark.parametrize("ruta, tipo", [
    ("/static/app.css", "text/css"),
    ("/static/app.js", "application/javascript"),
])
def test_los_estaticos_se_sirven_con_su_tipo(panel, ruta, tipo):
    base, token, _ = panel
    estado, cuerpo, cabeceras = pedir(base, ruta, headers={"Cookie": f"mb_session={token}"})
    assert estado == 200
    assert tipo in cabeceras.get("Content-Type", "")
    assert len(cuerpo) > 1000


def test_los_estaticos_no_llevan_el_token(panel):
    """El token cambia en cada arranque: no puede quedar en un archivo cacheable."""
    base, token, _ = panel
    for ruta in ("/static/app.css", "/static/app.js"):
        _, cuerpo, _ = pedir(base, ruta, headers={"Cookie": f"mb_session={token}"})
        assert token.encode() not in cuerpo


def test_no_se_puede_salir_de_la_carpeta_static(panel):
    base, token, _ = panel
    ck = {"Cookie": f"mb_session={token}"}
    for intento in ("/static/../marketplace_bot.db", "/static/app.js/../../.gitignore"):
        estado, _, _ = pedir(base, intento, headers=ck)
        assert estado != 200, f"{intento} no deberia servirse"


# --- endpoints de lectura ----------------------------------------------------

@pytest.mark.parametrize("ruta", ["/api/state", "/api/autonomy", "/api/activity", "/api/run/status"])
def test_los_endpoints_de_lectura_responden_json(panel, ruta):
    base, token, _ = panel
    estado, cuerpo, _ = pedir(base, ruta, headers={"Cookie": f"mb_session={token}"})
    assert estado == 200
    assert json.loads(cuerpo)["ok"] is True


def test_el_panel_arranca_con_la_base_de_prueba_no_la_real(panel):
    _, _, dashboard = panel
    assert "panel.db" in str(dashboard.STORE.path)


# --- el mapa de rutas concuerda con el frontend ------------------------------

def rutas_que_llama_el_frontend() -> set[str]:
    """Saca del JS todas las rutas /api/... que el panel invoca."""
    import re

    js = (SCRATCH_DIR / "static" / "app.js").read_text(encoding="utf-8")
    return {m.group(1) for m in re.finditer(r'["\'`](/api/[a-zA-Z0-9/_-]+)', js)}


def test_el_frontend_no_llama_a_ninguna_ruta_inexistente(panel):
    """Si alguien renombra un endpoint y olvida el JS, esto lo caza."""
    _, _, dashboard = panel
    post = set(dashboard.DashboardHandler.POST_ROUTES)
    get = {"/api/state", "/api/activity", "/api/autonomy", "/api/run/status",
           "/api/image", "/api/media", "/api/assistant/review"}
    conocidas = post | get
    desconocidas = sorted(r for r in rutas_que_llama_el_frontend() if r not in conocidas)
    assert not desconocidas, f"el frontend llama a rutas que el servidor no tiene: {desconocidas}"


def test_toda_ruta_post_registrada_apunta_a_un_metodo(panel):
    _, _, dashboard = panel
    for ruta, metodo in dashboard.DashboardHandler.POST_ROUTES.items():
        assert callable(metodo), f"{ruta} no apunta a nada invocable"
        assert metodo.__name__.startswith("_post_")


def test_una_ruta_post_inventada_da_404(panel):
    base, token, _ = panel
    estado, cuerpo, _ = pedir(base, "/api/no-existe", "POST", {
        "Cookie": f"mb_session={token}", "X-Marketplace-Token": token,
    }, b"{}")
    assert estado == 404
    assert json.loads(cuerpo)["ok"] is False

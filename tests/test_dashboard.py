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

    # El token se escribe en un archivo: sin esto las pruebas reemplazan el del
    # panel real que la persona tiene abierto y la dejan fuera.
    ruta_token_real = dashboard.DASHBOARD_TOKEN_PATH
    dashboard.DASHBOARD_TOKEN_PATH = temporal / "token.txt"
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
        dashboard.DASHBOARD_TOKEN_PATH = ruta_token_real
        os.environ.pop("MARKETPLACE_BOT_DB", None)


def test_las_pruebas_no_tocan_el_token_del_panel_real(panel):
    _, _, dashboard = panel
    assert dashboard.DASHBOARD_TOKEN_PATH.parent != SCRATCH_DIR


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


def test_el_enlace_vigente_entra_aunque_quede_una_cookie_vieja(panel):
    """Tras reiniciar el panel, el navegador conserva la cookie de la sesion anterior."""
    base, token, _ = panel
    estado, _, cabeceras = pedir(base, f"/?token={token}", headers={"Cookie": "mb_session=de-un-panel-anterior"})
    assert estado == 200
    assert f"mb_session={token}" in cabeceras.get("Set-Cookie", "")


def test_una_cookie_con_caracteres_raros_no_rompe_el_panel(panel):
    base, _, _ = panel
    estado, _, _ = pedir(base, "/api/state", headers={"Cookie": "mb_session=ñandú"})
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


# --- conexion con el SGI -----------------------------------------------------

LLAVE_FALSA = "k" * 43


@pytest.fixture()
def sgi_aislado(panel, monkeypatch, tmp_path):
    """El panel nunca toca la llave, el token de Drive ni el reporte reales."""
    base, token, dashboard = panel
    guardadas = []
    monkeypatch.setattr(dashboard, "SGI_REPORT_PATH", tmp_path / "reporte.json")
    monkeypatch.setattr(dashboard, "SGI_DRIVE_TOKEN_PATH", tmp_path / "token.json")
    monkeypatch.setattr(dashboard, "load_integration_settings", lambda: {"sgi": {"base_url": "https://sgi.example"}})
    monkeypatch.setattr(dashboard, "integration_key_configured", lambda: bool(guardadas))
    monkeypatch.setattr(dashboard, "save_integration_key", guardadas.append)
    cabeceras = {"Cookie": f"mb_session={token}", "X-Marketplace-Token": token}
    return base, cabeceras, dashboard, guardadas


def test_guardar_la_llave_no_la_devuelve(sgi_aislado):
    base, cabeceras, _, guardadas = sgi_aislado
    estado, cuerpo, _ = pedir(base, "/api/sgi/key", "POST", cabeceras, json.dumps({"key": LLAVE_FALSA}).encode())
    assert estado == 200
    assert guardadas == [LLAVE_FALSA]
    assert LLAVE_FALSA.encode() not in cuerpo
    assert json.loads(cuerpo)["sgi"]["key_configured"] is True


def test_una_llave_mal_formada_se_rechaza_sin_eco(panel, monkeypatch, tmp_path):
    base, token, dashboard = panel
    monkeypatch.setattr(dashboard, "save_integration_key", lambda key: dashboard_real_save(key, tmp_path))
    cabeceras = {"Cookie": f"mb_session={token}", "X-Marketplace-Token": token}
    estado, cuerpo, _ = pedir(base, "/api/sgi/key", "POST", cabeceras, json.dumps({"key": "corta-secreta"}).encode())
    assert estado >= 400
    assert b"corta-secreta" not in cuerpo
    assert not (tmp_path / "llave.bin").exists()


def dashboard_real_save(key, tmp_path):
    from sgi_client import save_integration_key

    save_integration_key(key, tmp_path / "llave.bin")


def test_el_estado_de_automatizacion_incluye_el_sgi_sin_secretos(sgi_aislado):
    base, cabeceras, _, _ = sgi_aislado
    estado, cuerpo, _ = pedir(base, "/api/autonomy", headers=cabeceras)
    assert estado == 200
    sgi = json.loads(cuerpo)["autonomy"]["sgi"]
    assert sgi["key_configured"] is False and sgi["drive_authorized"] is False
    assert "key" not in sgi


def test_sincronizar_con_la_llave_revocada_informa_el_codigo(sgi_aislado, monkeypatch):
    base, cabeceras, _, _ = sgi_aislado
    import marketplace_scheduler_worker as worker
    from sgi_client import SgiError

    def rechazada(store):
        raise SgiError("KEY_REJECTED", "El SGI rechazo la llave: revocada o caducada.")

    monkeypatch.setattr(worker, "refresh_from_sgi", rechazada)
    estado, cuerpo, _ = pedir(base, "/api/sgi/sync", "POST", cabeceras, b"{}")
    assert estado == 502
    respuesta = json.loads(cuerpo)
    assert respuesta["ok"] is False and respuesta["code"] == "SGI_KEY_REJECTED"


@pytest.fixture()
def drive_simulado(sgi_aislado, monkeypatch):
    """Sustituye el consentimiento de Google: nada abre navegadores ni toca la red."""
    import drive_photos

    base, cabeceras, dashboard, _ = sgi_aislado
    monkeypatch.setattr(dashboard, "_drive_authorization", {"error": "", "running": False, "url": ""})
    liberar = threading.Event()

    def conectar(comportamiento):
        def connect(*args, open_url=None, timeout_seconds=None, **kwargs):
            comportamiento(open_url, timeout_seconds)
            liberar.wait(5)

        monkeypatch.setattr(drive_photos.DriveLibrary, "connect", staticmethod(connect))

    yield base, cabeceras, dashboard, conectar
    liberar.set()


def test_autorizar_drive_devuelve_el_enlace_de_google(drive_simulado):
    base, cabeceras, dashboard, conectar = drive_simulado
    recibido = {}

    def entregar_enlace(open_url, timeout_seconds):
        recibido["timeout"] = timeout_seconds
        open_url("https://accounts.google.com/o/oauth2/auth?state=prueba")

    conectar(entregar_enlace)
    estado, cuerpo, _ = pedir(base, "/api/sgi/drive/authorize", "POST", cabeceras, b"{}")
    assert estado == 200
    assert json.loads(cuerpo)["url"].startswith("https://accounts.google.com/")
    assert recibido["timeout"] == dashboard.DRIVE_AUTHORIZATION_TIMEOUT_SECONDS

    # Pulsar otra vez mientras espera a Google devuelve el mismo enlace, sin otro flujo.
    estado, cuerpo, _ = pedir(base, "/api/sgi/drive/authorize", "POST", cabeceras, b"{}")
    assert estado == 200
    assert json.loads(cuerpo)["url"].startswith("https://accounts.google.com/")


def test_autorizar_drive_informa_si_no_se_pudo_preparar(drive_simulado):
    base, cabeceras, _, conectar = drive_simulado

    def fallar(open_url, timeout_seconds):
        raise FileNotFoundError("Falta credenciales_google.json")

    conectar(fallar)
    estado, cuerpo, _ = pedir(base, "/api/sgi/drive/authorize", "POST", cabeceras, b"{}")
    assert estado == 502
    respuesta = json.loads(cuerpo)
    assert respuesta["ok"] is False and "credenciales_google.json" in respuesta["error"]


def test_sincronizar_devuelve_el_reporte(sgi_aislado, monkeypatch):
    base, cabeceras, dashboard, _ = sgi_aislado
    import marketplace_scheduler_worker as worker

    reporte = {"generated_at": "2026-09-12T10:00:00", "publishable": ["CMP-NEG-M"], "price_issues": [],
               "without_photos": [{"code": "BOL-NEG-U", "old_code": ""}], "local_photos": []}

    def sincroniza(store):
        dashboard.SGI_REPORT_PATH.write_text(json.dumps(reporte), encoding="utf-8")
        return reporte

    monkeypatch.setattr(worker, "refresh_from_sgi", sincroniza)
    estado, cuerpo, _ = pedir(base, "/api/sgi/sync", "POST", cabeceras, b"{}")
    assert estado == 200
    sgi = json.loads(cuerpo)["sgi"]["report"]
    assert sgi["publishable"] == ["CMP-NEG-M"]
    assert sgi["without_photos"][0]["code"] == "BOL-NEG-U"

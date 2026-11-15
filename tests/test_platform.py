"""Pruebas de lo que cambia entre Windows y Mac. Corren en las dos: simulan la Mac."""
from __future__ import annotations

import plistlib
import re
import signal
import sys
import types
from pathlib import Path

import pytest

import instalar_trabajador_mac
import marketplace_platform as plataforma
import marketplace_runtime
import marketplace_scheduler_worker as worker
import sgi_client
from marketplace_runtime import images_root, local_path, parse_elapsed, relocate_images_path

RUTA_WINDOWS = r"C:\Users\Raul Gonzalez\.gemini\antigravity\scratch\imagenes_productos\CMP-BLA\foto_1.jpg"


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """Simula la Mac con la carpeta de usuario en tmp_path."""
    monkeypatch.setattr(plataforma, "IS_WINDOWS", False)
    monkeypatch.setattr(plataforma, "IS_MAC", True)
    monkeypatch.setattr(plataforma.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


# --- Rutas de fotos guardadas en Windows


def test_una_foto_guardada_en_windows_se_busca_en_la_carpeta_local(tmp_path):
    destino = relocate_images_path(RUTA_WINDOWS, base=tmp_path)
    assert destino == images_root(tmp_path) / "CMP-BLA" / "foto_1.jpg"


def test_tambien_traduce_la_carpeta_con_el_nombre_anterior(tmp_path):
    (tmp_path / "imagenes_firupost").mkdir()
    ruta = r"C:\bot\imagenes_firupost\TOP-NEG\foto_2.jpg"
    assert relocate_images_path(ruta, base=tmp_path) == tmp_path / "imagenes_firupost" / "TOP-NEG" / "foto_2.jpg"


def test_una_ruta_que_no_es_de_fotos_no_se_traduce():
    assert relocate_images_path(r"C:\Users\x\Downloads\foto.jpg") is None


def test_en_la_mac_la_ruta_de_windows_apunta_a_la_foto_local(monkeypatch):
    monkeypatch.setattr(marketplace_runtime, "TRANSLATE_WINDOWS_PATHS", True)
    assert local_path(RUTA_WINDOWS) == images_root() / "CMP-BLA" / "foto_1.jpg"
    # Una ruta propia de la Mac queda igual.
    assert local_path("/Users/x/bot/otra.jpg") == Path("/Users/x/bot/otra.jpg")


def test_en_windows_las_rutas_no_cambian(monkeypatch):
    monkeypatch.setattr(marketplace_runtime, "TRANSLATE_WINDOWS_PATHS", False)
    assert local_path(RUTA_WINDOWS) == Path(RUTA_WINDOWS)


def test_la_seleccion_de_fotos_por_cuenta_sigue_valiendo_en_la_mac(monkeypatch, tmp_path):
    """La base guarda la seleccion de cada cuenta con rutas de Windows."""
    import marketplace_scheduler as scheduler

    foto = tmp_path / "imagenes_productos" / "CMP-BLA" / "foto_1.jpg"
    foto.parent.mkdir(parents=True)
    foto.write_bytes(b"x")
    monkeypatch.setattr(marketplace_runtime, "TRANSLATE_WINDOWS_PATHS", True)
    monkeypatch.setattr(marketplace_runtime, "SCRATCH_DIR", tmp_path)
    config = {"account_media": {"Dylan": {"compression_short": [RUTA_WINDOWS]}}}
    trabajo = {"family_key": "compression_short", "sku_prefixes": ["CMP"]}

    resultado = scheduler.job_for_account(trabajo, "Dylan", config)

    assert resultado["listing_overrides"]["image_paths"] == [RUTA_WINDOWS]
    # Y al publicar, la ruta guardada lleva a la foto de la Mac.
    assert marketplace_runtime.alias_images_path(Path(RUTA_WINDOWS)) == foto


# --- Procesos


@pytest.mark.parametrize(
    "texto, segundos",
    [("   00:07", 7), ("12:34", 754), ("01:00:00", 3600), ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5)],
)
def test_lee_el_tiempo_de_ps(texto, segundos):
    assert parse_elapsed(texto) == segundos


def test_un_tiempo_ilegible_de_ps_no_se_inventa():
    assert parse_elapsed("") is None
    assert parse_elapsed("abc") is None


def test_detener_en_la_mac_cierra_el_grupo_del_trabajador(monkeypatch, mac):
    senales = []
    monkeypatch.setattr(plataforma.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(plataforma.os, "killpg", lambda pid, sig: senales.append(("grupo", pid, sig)), raising=False)
    monkeypatch.setattr(plataforma.os, "kill", lambda pid, sig: senales.append(("proceso", pid, sig)))
    plataforma.stop_process_tree(4321)
    assert senales == [("grupo", 4321, signal.SIGTERM)]


def test_detener_en_la_mac_un_proceso_sin_grupo_propio(monkeypatch, mac):
    senales = []
    monkeypatch.setattr(plataforma.os, "getpgid", lambda pid: 1, raising=False)
    monkeypatch.setattr(plataforma.os, "killpg", lambda pid, sig: senales.append(("grupo", pid)), raising=False)
    monkeypatch.setattr(plataforma.os, "kill", lambda pid, sig: senales.append(("proceso", pid)))
    plataforma.stop_process_tree(4321)
    assert senales == [("proceso", 4321)]


# --- Chrome


def test_el_perfil_de_windows_se_usa_con_el_mismo_nombre_en_la_mac(mac):
    soporte = mac / "Library" / "Application Support"
    assert plataforma.profile_dir(r"%LOCALAPPDATA%\BotInvisibleChrome", "sebastian") == soporte / "BotInvisibleChrome"
    assert plataforma.profile_dir(r"C:\Users\x\AppData\Local\MarketplaceBot_cuenta2", "Dylan") == (
        soporte / "MarketplaceBot_cuenta2"
    )
    assert plataforma.profile_dir("", "Dylan Steven") == soporte / "MarketplaceBot_Dylan_Steven"


def test_un_perfil_propio_de_la_mac_se_respeta(mac):
    assert plataforma.profile_dir("/Volumes/datos/perfil", "Dylan") == Path("/Volumes/datos/perfil")


def test_chrome_en_la_mac_lleva_las_mismas_opciones_que_en_windows():
    """Si se cambia abrir_sesion_marketplace_cuenta.ps1 hay que cambiar tambien la Mac."""
    script = (Path(plataforma.__file__).parent / "abrir_sesion_marketplace_cuenta.ps1").read_text(encoding="utf-8")
    en_windows = set(re.findall(r'"(--[a-z-]+(?:=[A-Za-z-]+)?)"', script))
    assert en_windows == set(plataforma.CHROME_FLAGS)


def test_abre_chrome_en_la_mac_con_su_perfil_y_su_puerto(monkeypatch, mac):
    lanzados = []
    chrome = mac / "Google Chrome"
    monkeypatch.setattr(plataforma, "chrome_executable", lambda: chrome)
    monkeypatch.setattr(plataforma.subprocess, "Popen", lambda comando, **opciones: lanzados.append((comando, opciones)))

    plataforma.open_chrome("Dylan Steven", 9224, r"%LOCALAPPDATA%\MarketplaceBot_cuenta3")

    perfil = mac / "Library" / "Application Support" / "MarketplaceBot_cuenta3"
    assert perfil.is_dir()
    comando, opciones = lanzados[0]
    assert comando[0] == str(chrome)
    assert f"--user-data-dir={perfil}" in comando
    assert "--remote-debugging-port=9224" in comando
    assert comando[-1] == plataforma.MARKETPLACE_URL
    # Chrome sobrevive si se detiene el trabajador o se cierra la Terminal.
    assert opciones["start_new_session"] is True


def test_sin_chrome_instalado_el_trabajador_lo_dice_y_no_se_cae(monkeypatch, mac):
    monkeypatch.setattr(plataforma, "chrome_executable", lambda: None)
    monkeypatch.setattr(worker, "chrome_status", lambda address: (False, "no responde"))
    ok, detalle = worker.ensure_chrome("Dylan", {"debugger_address": "127.0.0.1:9223"})
    assert ok is False
    assert "No encontre Google Chrome" in detalle


# --- Llaves en el Llavero


@pytest.fixture
def llavero(monkeypatch, mac):
    guardadas = {}
    falso = types.SimpleNamespace(
        get_password=lambda servicio, nombre: guardadas.get((servicio, nombre)),
        set_password=lambda servicio, nombre, valor: guardadas.__setitem__((servicio, nombre), valor),
    )
    monkeypatch.setitem(sys.modules, "keyring", falso)
    return guardadas


def test_la_llave_del_sgi_va_al_llavero_y_no_a_un_archivo(monkeypatch, llavero, tmp_path):
    monkeypatch.delenv("SGI_INTEGRATION_KEY", raising=False)
    llave = "a" * 43
    sgi_client.save_integration_key(llave, tmp_path / "llave.bin")
    assert not (tmp_path / "llave.bin").exists()
    assert llavero == {(plataforma.KEYCHAIN_SERVICE, "sgi_integration_key"): llave}
    assert sgi_client.load_integration_key(tmp_path / "llave.bin") == llave


def test_sin_llavero_pide_la_variable_de_entorno(monkeypatch, mac, tmp_path):
    monkeypatch.setitem(sys.modules, "keyring", None)
    with pytest.raises(RuntimeError, match="SGI_INTEGRATION_KEY"):
        sgi_client.save_integration_key("a" * 43, tmp_path / "llave.bin")
    assert plataforma.load_secret("sgi_integration_key", tmp_path / "llave.bin") == ""


def test_la_llave_de_openai_tambien_va_al_llavero(monkeypatch, llavero):
    import marketplace_ai_descriptions as ai

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    ai.save_api_key("sk-" + "x" * 40)
    assert ai.load_api_key() == "sk-" + "x" * 40
    assert (plataforma.KEYCHAIN_SERVICE, "openai_api_key") in llavero


# --- Trabajador automatico (launchd)


def test_el_trabajador_de_la_mac_arranca_al_iniciar_y_se_revisa_cada_10_minutos(tmp_path):
    datos = plistlib.loads(plistlib.dumps(instalar_trabajador_mac.build_plist(tmp_path)))
    assert datos["Label"] == instalar_trabajador_mac.LABEL
    assert datos["ProgramArguments"] == ["/bin/bash", str(tmp_path / "ejecutar_trabajador_marketplace.sh")]
    assert datos["RunAtLoad"] is True
    assert datos["StartInterval"] == 600
    assert datos["StandardOutPath"] == datos["StandardErrorPath"] == str(tmp_path / "marketplace_scheduler_worker.log")


def test_no_instala_en_carpetas_que_macos_protege(tmp_path):
    assert instalar_trabajador_mac.protected_folder(tmp_path / "Documents" / "bot", tmp_path) == "Documents"
    assert instalar_trabajador_mac.protected_folder(tmp_path / "Library/Mobile Documents/x", tmp_path)
    assert instalar_trabajador_mac.protected_folder(tmp_path / "BotMarketplace", tmp_path) == ""


def test_los_scripts_de_la_mac_tienen_finales_de_linea_lf():
    base = Path(plataforma.__file__).parent
    for nombre in ("ejecutar_trabajador_marketplace.sh", "instalar_mac.command", "iniciar_panel_marketplace.command"):
        contenido = (base / nombre).read_bytes()
        assert contenido.startswith(b"#!/bin/bash\n"), nombre
        assert b"\r\n" not in contenido, nombre

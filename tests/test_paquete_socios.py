"""El paquete para socios debe llevar todo lo que el bot importa o lanza."""
from __future__ import annotations

import ast
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
ENTRADAS = (
    "marketplace_bot_dashboard",
    "marketplace_scheduler_worker",
    "marketplace_campaign_runner",
    "facebook_marketplace_browser_automator",
    "instalar_trabajador_mac",
)


def _locales() -> set[str]:
    return {path.stem for path in BASE.glob("*.py")}


def _dependencias(modulo: str, vistos: set[str], locales: set[str]) -> None:
    if modulo in vistos or modulo not in locales:
        return
    vistos.add(modulo)
    texto = (BASE / f"{modulo}.py").read_text(encoding="utf-8")
    for nodo in ast.walk(ast.parse(texto)):
        if isinstance(nodo, ast.Import):
            for alias in nodo.names:
                _dependencias(alias.name.split(".")[0], vistos, locales)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            _dependencias(nodo.module.split(".")[0], vistos, locales)
    # Scripts que se lanzan como proceso aparte ("marketplace_listing_builder.py").
    for lanzado in re.findall(r'"(\w+)\.py"', texto):
        _dependencias(lanzado, vistos, locales)


def _en_el_paquete() -> set[str]:
    script = (BASE / "preparar_paquete_socios.ps1").read_text(encoding="utf-8")
    lista = script.split("$files = @(", 1)[1].split(")", 1)[0]
    return set(re.findall(r'"([^"]+)"', lista))


def test_el_paquete_lleva_todos_los_modulos_del_bot():
    locales = _locales()
    necesarios: set[str] = set()
    for entrada in ENTRADAS:
        _dependencias(entrada, necesarios, locales)
    faltan = sorted(f"{modulo}.py" for modulo in necesarios if f"{modulo}.py" not in _en_el_paquete())
    assert not faltan, f"Agrega a $files de preparar_paquete_socios.ps1: {faltan}"


def test_el_paquete_lleva_los_iniciadores_de_windows_y_de_mac():
    paquete = _en_el_paquete()
    for nombre in (
        "iniciar_panel_marketplace.ps1",
        "instalar_trabajador_marketplace.ps1",
        "iniciar_panel_marketplace.command",
        "instalar_mac.command",
        "ejecutar_trabajador_marketplace.sh",
        "GUIA_MAC.md",
    ):
        assert nombre in paquete, nombre
    faltan = sorted(nombre for nombre in paquete if not (BASE / nombre).exists())
    assert not faltan, f"El paquete pide archivos que no existen: {faltan}"

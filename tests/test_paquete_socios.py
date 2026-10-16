"""El paquete para socios debe incluir todo modulo que el bot importa."""
from __future__ import annotations

import ast
import re
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# Herramientas de mantenimiento que no viajan en el paquete.
FUERA_DEL_PAQUETE = {"migrar_nombres_antiguos.py"}


def modulos_del_paquete() -> set[str]:
    script = (RAIZ / "preparar_paquete_socios.ps1").read_text(encoding="utf-8")
    return set(re.findall(r'"([A-Za-z0-9_]+\.py)"', script))


def importa(archivo: str) -> set[str]:
    arbol = ast.parse((RAIZ / archivo).read_text(encoding="utf-8"))
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            nombres.add(nodo.module.split(".")[0])
        elif isinstance(nodo, ast.Import):
            nombres.update(alias.name.split(".")[0] for alias in nodo.names)
    return {f"{nombre}.py" for nombre in nombres if (RAIZ / f"{nombre}.py").is_file()}


def test_cada_modulo_local_que_importa_el_paquete_viaja_en_el_paquete() -> None:
    incluidos = modulos_del_paquete()
    faltantes = {
        f"{archivo} importa {modulo}"
        for archivo in sorted(incluidos)
        for modulo in importa(archivo)
        if modulo not in incluidos and modulo not in FUERA_DEL_PAQUETE
    }
    assert not faltantes, f"Faltan en preparar_paquete_socios.ps1: {sorted(faltantes)}"

"""Instala el trabajador del bot en la Mac, como instalar_trabajador_marketplace.ps1 en Windows.

En la Mac la tarea programada de Windows la hace launchd: arranca el trabajador al
iniciar sesion y lo vuelve a lanzar cada 10 minutos si se cerro. Si ya esta
corriendo, launchd no lanza otro.

    .venv-mac/bin/python instalar_trabajador_mac.py              # instalar o reinstalar
    .venv-mac/bin/python instalar_trabajador_mac.py --desinstalar
"""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path


SCRATCH_DIR = Path(__file__).resolve().parent
LABEL = "com.lacomarca.marketplacebot"
WATCHDOG_SECONDS = 10 * 60
WORKER_SCRIPT = "ejecutar_trabajador_marketplace.sh"
WORKER_LOG = "marketplace_scheduler_worker.log"
# macOS no deja que un proceso de fondo lea estas carpetas sin "Acceso total al
# disco": el trabajador fallaria con "Operation not permitted" al abrir la base.
PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents")


def plist_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def build_plist(scratch_dir: Path = SCRATCH_DIR) -> dict:
    log = str(scratch_dir / WORKER_LOG)
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(scratch_dir / WORKER_SCRIPT)],
        "WorkingDirectory": str(scratch_dir),
        "RunAtLoad": True,
        "StartInterval": WATCHDOG_SECONDS,
        "StandardOutPath": log,
        "StandardErrorPath": log,
    }


def protected_folder(folder: Path, home: Path | None = None) -> str:
    """La carpeta protegida de macOS que contiene ``folder``, o "" si no esta en ninguna."""
    home = home or Path.home()
    for name in PROTECTED_FOLDERS:
        try:
            folder.relative_to(home / name)
        except ValueError:
            continue
        return name
    return ""


def launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def uninstall(target: Path) -> None:
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    if target.exists():
        # A la Papelera, no borrado: se puede recuperar.
        trash = Path.home() / ".Trash"
        trash.mkdir(exist_ok=True)
        destination = trash / f"{target.stem}-{time.strftime('%Y%m%d-%H%M%S')}{target.suffix}"
        shutil.move(str(target), str(destination))
    print("Trabajador desinstalado. El bot ya no arranca solo en esta Mac.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--desinstalar", action="store_true")
    args = parser.parse_args()
    if sys.platform != "darwin":
        print("Este instalador es para Mac. En Windows usa instalar_trabajador_marketplace.ps1.")
        return 1
    target = plist_path()
    if args.desinstalar:
        uninstall(target)
        return 0
    folder = protected_folder(SCRATCH_DIR)
    if folder:
        print(f"La carpeta del bot esta dentro de {folder}: {SCRATCH_DIR}")
        print("macOS no deja que el trabajador automatico lea esa carpeta.")
        print(f"Muevela a tu carpeta de usuario (por ejemplo {Path.home() / 'BotMarketplace'}) e instala de nuevo.")
        return 1
    if not (SCRATCH_DIR / ".venv-mac" / "bin" / "python").exists():
        print("Falta el entorno de Python del bot. Abre primero instalar_mac.command.")
        return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    # Si ya estaba instalado se descarga primero; launchd no acepta cargarlo dos veces.
    launchctl("bootout", f"gui/{os.getuid()}/{LABEL}")
    target.write_bytes(plistlib.dumps(build_plist()))
    result = launchctl("bootstrap", f"gui/{os.getuid()}", str(target))
    if result.returncode != 0:
        print(f"launchd no acepto el trabajador: {(result.stderr or result.stdout).strip()}")
        return result.returncode or 1
    print(f"Trabajador instalado ({LABEL}).")
    print("Arranca al iniciar sesion y se revisa cada 10 minutos.")
    print("El bot permanece inactivo hasta activar Bot activo en el panel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Lo que cambia entre Windows y Mac: abrir Chrome, sus perfiles, detener procesos
y guardar llaves.

En Windows todo sigue como antes: Chrome se abre con
abrir_sesion_marketplace_cuenta.ps1 y las llaves se cifran con la cuenta de Windows
(DPAPI). En la Mac Chrome se abre desde aqui y las llaves van al Llavero.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path


SCRATCH_DIR = Path(__file__).resolve().parent
OPEN_SESSION_SCRIPT = SCRATCH_DIR / "abrir_sesion_marketplace_cuenta.ps1"
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
MARKETPLACE_URL = "https://www.facebook.com/marketplace"
# Nombre con el que las llaves aparecen en la app Acceso a Llaveros de la Mac.
KEYCHAIN_SERVICE = "Bot Marketplace La Comarca"

# Los mismos que pasa abrir_sesion_marketplace_cuenta.ps1. Chrome congela las
# paginas de ventanas minimizadas o tapadas y los menus de Marketplace dejan de
# registrar la opcion elegida; el bot publica sin nadie delante, asi que la pagina
# debe seguir viva en segundo plano.
CHROME_FLAGS = (
    "--profile-directory=Default",
    "--lang=en-US",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--new-window",
)
MAC_CHROME_APP = Path("Applications") / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]|\\|%[A-Za-z_]+%")


class SecretStoreMissing(RuntimeError):
    """Este equipo no tiene donde guardar llaves cifradas."""


def panel_launcher() -> str:
    return "iniciar_panel_marketplace.ps1" if IS_WINDOWS else "iniciar_panel_marketplace.command"


def profiles_base() -> Path:
    """Carpeta donde viven los perfiles de Chrome del bot (uno por cuenta)."""
    if IS_WINDOWS:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if IS_MAC:
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def default_profile_dir(account: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(account or "")) or "cuenta1"
    return profiles_base() / f"MarketplaceBot_{safe}"


def profile_dir(value: str, account: str) -> Path:
    """Perfil de Chrome de la cuenta segun marketplace_accounts.json.

    El archivo viene de Windows (``%LOCALAPPDATA%\\MarketplaceBot_cuenta2``). En la
    Mac esa variable no existe: se usa la misma carpeta dentro de la carpeta de
    perfiles de la Mac, asi el archivo sirve igual en las dos computadoras.
    """
    text = os.path.expandvars(str(value or "").strip())
    if not text:
        return default_profile_dir(account)
    if IS_WINDOWS:
        return Path(text)
    if WINDOWS_PATH.search(text):
        parts = [part for part in re.split(r"[\\/]+", text) if part and "%" not in part and not part.endswith(":")]
        return profiles_base() / parts[-1] if parts else default_profile_dir(account)
    return Path(text).expanduser()


def chrome_executable() -> Path | None:
    if IS_WINDOWS:
        roots = [os.environ.get(name, "") for name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
        candidates = [Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots if root]
    elif IS_MAC:
        candidates = [Path("/") / MAC_CHROME_APP, Path.home() / MAC_CHROME_APP]
    else:
        found = [shutil.which(name) for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
        candidates = [Path(path) for path in found if path]
    return next((path for path in candidates if path.is_file()), None)


def chrome_command(chrome: Path, profile: Path, port: int) -> list[str]:
    return [str(chrome), f"--user-data-dir={profile}", f"--remote-debugging-port={int(port)}", *CHROME_FLAGS, MARKETPLACE_URL]


def open_chrome(account: str, port: int, profile: str = "", hidden: bool = True) -> None:
    """Abre el Chrome de la cuenta con su perfil y su puerto de automatizacion.

    No espera a que Chrome responda; quien llama comprueba el puerto.
    """
    if IS_WINDOWS:
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(OPEN_SESSION_SCRIPT)]
        command += ["-Account", str(account), "-Port", str(int(port))]
        expanded = os.path.expandvars(str(profile or ""))
        if expanded:
            command += ["-ProfileDir", expanded]
        flags = subprocess.CREATE_NO_WINDOW if hidden else 0
        subprocess.Popen(command, cwd=str(SCRATCH_DIR), creationflags=flags)
        return
    chrome = chrome_executable()
    if chrome is None:
        where = "en la carpeta Aplicaciones" if IS_MAC else "en este equipo"
        raise RuntimeError(f"No encontre Google Chrome instalado {where}.")
    folder = profile_dir(profile, account)
    folder.mkdir(parents=True, exist_ok=True)
    # Sesion propia: si se detiene el trabajador o se cierra la Terminal, la
    # ventana de Chrome (con la sesion de Facebook) sigue abierta.
    subprocess.Popen(
        chrome_command(chrome, folder, port),
        cwd=str(SCRATCH_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def stop_process_tree(pid: int) -> None:
    """Detiene un proceso y los que lanzo (el trabajador y su publicacion en curso)."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        return
    try:
        # El panel y launchd arrancan el trabajador en su propio grupo de procesos:
        # cerrar el grupo cierra tambien la publicacion que este haciendo.
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def load_secret(name: str, path: Path) -> str:
    """Llave guardada con save_secret, o "" si no hay o no se puede leer."""
    if IS_WINDOWS:
        if not path.is_file():
            return ""
        try:
            import win32crypt

            return win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1].decode("utf-8").strip()
        except Exception:
            return ""
    try:
        import keyring

        return str(keyring.get_password(KEYCHAIN_SERVICE, name) or "").strip()
    except Exception:
        return ""


def save_secret(name: str, path: Path, value: str, description: str) -> None:
    """Guarda una llave cifrada: en Windows con la cuenta del usuario, en la Mac en el Llavero."""
    if IS_WINDOWS:
        try:
            import win32crypt
        except ImportError as exc:
            raise SecretStoreMissing(description) from exc
        path.write_bytes(win32crypt.CryptProtectData(value.encode("utf-8"), description, None, None, None, 0))
        return
    try:
        import keyring

        keyring.set_password(KEYCHAIN_SERVICE, name, value)
    except Exception as exc:  # noqa: BLE001 - sin keyring o sin Llavero disponible
        raise SecretStoreMissing(description) from exc

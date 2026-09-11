from __future__ import annotations

import argparse
import getpass
import os
import re
import subprocess
import time
from pathlib import Path

import pandas as pd
import psutil
import pyautogui
import pygetwindow as gw
from PIL import Image
from pywinauto import Application, Desktop
from pywinauto.timings import TimeoutError as UIATimeoutError

from firupost_make_test_batch import make_batch


FIRUPOST_EXE = Path(r"C:\FiruPost\FiruPost.exe")
SCRATCH_DIR = Path(__file__).resolve().parent
ARTICLES_EXCEL = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"
ACCOUNTS_EXCEL = Path(r"C:\FiruPost\Cuentas_FB.xlsx")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}

pyautogui.PAUSE = 0.15


def log(message: str) -> None:
    print(f"[firupost-ui] {message}", flush=True)


def truthy_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "si", "sí", "on"}


def latest_firupost_pid() -> int | None:
    processes = []
    for proc in psutil.process_iter(["pid", "name", "exe", "create_time"]):
        try:
            if proc.info["name"] == "FiruPost.exe":
                processes.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if not processes:
        return None
    processes.sort(key=lambda p: p.info.get("create_time") or 0, reverse=True)
    return int(processes[0].info["pid"])


def app_for_pid(pid: int) -> Application:
    return Application(backend="uia").connect(process=pid)


def window_titles(pid: int) -> list[str]:
    try:
        app = app_for_pid(pid)
        return [w.window_text() for w in app.windows()]
    except Exception:
        return []


def launch_or_attach() -> int:
    pid = latest_firupost_pid()
    if pid:
        log(f"Usando FiruPost ya abierto: PID {pid}")
        return pid

    if not FIRUPOST_EXE.exists():
        raise FileNotFoundError(f"No encuentro FiruPost en {FIRUPOST_EXE}")

    proc = subprocess.Popen([str(FIRUPOST_EXE)], cwd=str(FIRUPOST_EXE.parent))
    log(f"FiruPost lanzado: PID {proc.pid}")
    return proc.pid


def wait_for_update_and_relaunch(pid: int, timeout_seconds: int = 1200) -> int:
    start = time.time()
    last_progress = None
    while time.time() - start < timeout_seconds:
        current_pid = latest_firupost_pid()
        if current_pid and current_pid != pid:
            log(f"FiruPost se relanzo: PID {current_pid}")
            pid = current_pid

        titles = window_titles(pid)
        if any("Actualizando FiruPost" in title for title in titles):
            try:
                app = app_for_pid(pid)
                updater = app.window(title="Actualizando FiruPost...")
                progress = updater.child_window(control_type="ProgressBar").legacy_properties().get("Value")
                if progress != last_progress:
                    log(f"Actualizando FiruPost: {progress}%")
                    last_progress = progress
            except Exception:
                log("Actualizacion en curso...")
            time.sleep(10)
            continue

        if titles:
            return pid
        time.sleep(2)

    raise TimeoutError("FiruPost no termino de actualizar dentro del tiempo esperado.")


def get_main_app(pid: int) -> Application:
    return app_for_pid(pid)


def set_text_ui_or_click(edit, text: str) -> bool:
    try:
        if edit.exists() and edit.is_enabled():
            edit.set_edit_text(text)
            return True
    except Exception:
        return False
    return False


def activate_desktop_window(title_pattern: str, timeout: int = 10) -> bool:
    compiled = re.compile(title_pattern)
    deadline = time.time() + timeout
    while time.time() < deadline:
        for window in gw.getAllWindows():
            if compiled.search(window.title or ""):
                try:
                    if window.isMinimized:
                        window.restore()
                    window.activate()
                    time.sleep(0.8)
                    return True
                except Exception:
                    pass
        time.sleep(0.5)
    return False


def complete_license_if_needed(pid: int) -> int:
    pid = wait_for_update_and_relaunch(pid)
    app = get_main_app(pid)
    titles = window_titles(pid)
    if not any(title == "Licencia" for title in titles):
        return pid

    log("Completando pantalla de licencia.")
    win = app.window(title="Licencia")
    win.set_focus()
    time.sleep(1)

    used_uia = False
    try:
        edits = win.descendants(control_type="Edit")
        for edit in edits:
            if set_text_ui_or_click(edit, "1"):
                used_uia = True
                break
    except Exception:
        used_uia = False

    if not used_uia:
        rect = win.rectangle()
        pyautogui.click(rect.left + 185, rect.top + 150)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.write("1", interval=0.01)

    try:
        checkbox = win.child_window(title="Acepto los términos y condiciones", control_type="CheckBox")
        if checkbox.get_toggle_state() == 0:
            checkbox.toggle()
    except Exception:
        rect = win.rectangle()
        pyautogui.click(rect.left + 48, rect.top + 196)

    try:
        win.child_window(title=" Iniciar", control_type="Button").invoke()
    except Exception:
        rect = win.rectangle()
        pyautogui.click(rect.left + 515, rect.top + 536)

    deadline = time.time() + 60
    while time.time() < deadline:
        new_pid = latest_firupost_pid() or pid
        pid = new_pid
        titles = window_titles(pid)
        if any("Principal" in title for title in titles):
            log("Licencia aceptada; menu principal abierto.")
            return pid
        time.sleep(2)
    raise TimeoutError("No se abrio el menu Principal despues de licencia.")


def click_button(app: Application, window_title_re: str, button_title: str) -> None:
    win = app.window(title_re=window_title_re)
    win.set_focus()
    title = win.window_text()
    if title:
        activated = activate_desktop_window(re.escape(title), timeout=3)
        log(f"Activar ventana '{title}': {activated}")
    button = win.child_window(title=button_title, control_type="Button")
    rect = button.rectangle()
    x = rect.left + rect.width() // 2
    y = rect.top + rect.height() // 2
    try:
        log(f"Invocando '{button_title}' por UIA.")
        button.invoke()
        time.sleep(0.8)
        return
    except Exception:
        pass
    try:
        log(f"Click_input '{button_title}' en ({x}, {y})")
        button.click_input()
        time.sleep(0.8)
        return
    except Exception:
        pass
    log(f"Click '{button_title}' en ({x}, {y})")
    pyautogui.click(x, y)
    time.sleep(0.8)


def set_checkbox_state(win, label: str, enabled: bool) -> None:
    if not enabled:
        return
    try:
        checkbox = win.child_window(title=label, control_type="CheckBox")
        if checkbox.exists(timeout=2) and checkbox.get_toggle_state() == 0:
            log(f"Marcando opcion: {label}")
            checkbox.toggle()
            time.sleep(0.3)
    except Exception as exc:  # noqa: BLE001 - UI fallback is best-effort.
        log(f"No pude marcar opcion '{label}': {exc}")


def open_publish_article(pid: int) -> None:
    app = get_main_app(pid)
    titles = window_titles(pid)
    if any(title.startswith("Publicar") for title in titles):
        return
    if not any("Principal" in title for title in titles):
        raise RuntimeError(f"FiruPost no esta en menu principal. Ventanas: {titles}")
    log("Entrando a Publicar > Publicar En Articulo.")
    click_button(app, ".*Principal.*", "Publicar")
    time.sleep(2)
    click_button(app, "Opcion de Publicacion", "Publicar En Artículo")
    time.sleep(3)


def choose_mode(pid: int, mode: str) -> None:
    app = get_main_app(pid)
    win = app.window(title="Publicar")
    if not win.exists(timeout=2):
        if app.window(title_re="Publicar - Modo.*").exists(timeout=2):
            return
        raise RuntimeError("No encuentro ventana Publicar.")

    if mode == "individual":
        log("Seleccionando modo cuenta individual.")
        win.child_window(title_re="Modo Cuenta Individual.*", control_type="Button").invoke()
    else:
        log("Seleccionando modo cuentas multiples.")
        win.child_window(title="Modo Cuentas Multiples", control_type="Button").invoke()
    time.sleep(2)


def dismiss_ok_dialogs() -> None:
    desktop = Desktop(backend="uia")
    for win in desktop.windows():
        try:
            button = win.child_window(title="OK", control_type="Button")
            if button.exists() and button.is_enabled():
                button.invoke()
                time.sleep(0.5)
        except Exception:
            continue
    for win in gw.getAllWindows():
        title = win.title or ""
        if "Seleccionad" in title:
            try:
                if win.isMinimized:
                    win.restore()
                win.activate()
                time.sleep(0.2)
                pyautogui.click(win.left + win.width - 45, win.top + win.height - 35)
                time.sleep(0.8)
            except Exception:
                continue


def window_text_dump(win) -> str:
    parts = []
    try:
        title = win.window_text()
        if title:
            parts.append(title)
    except Exception:
        pass
    try:
        for child in win.descendants():
            text = child.window_text()
            if text:
                parts.append(text)
    except Exception:
        pass
    return " | ".join(dict.fromkeys(parts))


def handle_selection_confirmation(win, *, is_folder: bool) -> None:
    text = window_text_dump(win)
    log(f"Confirmacion detectada: {text or win.window_text()}")
    dismiss_ok_dialogs()
    if is_folder and re.search(r"Total de im[aá]genes encontradas:\s*0\b", text, flags=re.IGNORECASE):
        raise RuntimeError(
            "FiruPost confirmo 0 imagenes al cargar la carpeta. "
            "Revisa que Carpetas Imagenes sea la raiz correcta para CarpetaImg/NombreImg."
        )


def retry_connection_dialogs() -> None:
    try:
        desktop = Desktop(backend="uia")
        err = desktop.window(title="Error de conexión")
        if err.exists(timeout=0.5):
            log("Error de conexion detectado; pulsando Reintentar.")
            err.child_window(title="Reintentar", control_type="Button").invoke()
            time.sleep(1)
    except Exception:
        return


def select_path_in_dialog(title_re: str, path: Path, is_folder: bool) -> None:
    desktop = Desktop(backend="uia")
    compiled = re.compile(title_re)
    dialog = None
    gw_dialog = None
    deadline = time.time() + 20
    while time.time() < deadline:
        retry_connection_dialogs()
        for win in desktop.windows():
            title = win.window_text() or ""
            if "Seleccionad" in title:
                handle_selection_confirmation(win, is_folder=is_folder)
                return
            if compiled.search(title):
                dialog = win
                break
        if dialog is None:
            for win in gw.getAllWindows():
                title = win.title or ""
                if "Seleccionad" in title:
                    log(f"Confirmacion detectada: {title}")
                    dismiss_ok_dialogs()
                    return
                if compiled.search(title):
                    gw_dialog = win
                    break
        if dialog is not None:
            break
        if gw_dialog is not None:
            break
        time.sleep(0.5)

    if dialog is None and gw_dialog is None:
        dismiss_ok_dialogs()
        raise UIATimeoutError(f"No aparecio dialogo para {path}")

    if dialog is not None:
        dialog.set_focus()
        rect = dialog.rectangle()
        left, right, bottom = rect.left, rect.right, rect.bottom
    else:
        if gw_dialog.isMinimized:
            gw_dialog.restore()
        gw_dialog.activate()
        left = gw_dialog.left
        right = gw_dialog.left + gw_dialog.width
        bottom = gw_dialog.top + gw_dialog.height
    time.sleep(0.5)
    # Windows/Qt file dialogs expose the path field near the bottom. Coordinates are
    # relative to the dialog, which keeps this stable across window placement.
    pyautogui.click(left + 300, bottom - 55)
    pyautogui.hotkey("ctrl", "a")
    pyautogui.write(str(path), interval=0.001)
    time.sleep(0.2)
    if is_folder:
        pyautogui.click(right - 170, bottom - 25)
    else:
        pyautogui.click(right - 160, bottom - 25)
    time.sleep(2)
    retry_connection_dialogs()
    dismiss_ok_dialogs()


def fill_publish_form(
    pid: int,
    mode: str,
    email: str,
    password: str,
    account_name: str,
    location: str,
    articles_excel: Path,
    images_root: Path,
    accounts_excel: Path,
    simultaneous_accounts: str,
    account_interval: str,
    reuse_session: bool,
    meet_public: bool,
    door_pickup: bool,
    door_delivery: bool,
    publish_groups: bool,
    publish_without_description: bool,
) -> None:
    app = get_main_app(pid)
    title = "Publicar - Modo Individual" if mode == "individual" else "Publicar - Modo Cuentas Multiples"
    win = app.window(title=title)
    win.wait("visible", timeout=20)
    win.set_focus()

    if mode == "individual":
        edits = win.descendants(control_type="Edit")
        if email:
            edits[0].set_edit_text(email)
        elif reuse_session:
            edits[0].set_edit_text("")
        if reuse_session:
            edits[1].set_edit_text("")
        elif password:
            edits[1].set_edit_text(password)
        if account_name:
            edits[2].set_edit_text(account_name)
    else:
        log("Cargando archivo de cuentas.")
        click_button(app, title, "Cargar Cuentas")
        time.sleep(1)
        select_path_in_dialog(".*(Cuentas|Excel|Abrir|Open).*", accounts_excel, is_folder=False)
        edits = win.descendants(control_type="Edit")
        enabled_edits = [edit for edit in edits if edit.is_enabled()]
        if len(enabled_edits) >= 2:
            enabled_edits[-2].set_edit_text(simultaneous_accounts)
            enabled_edits[-1].set_edit_text(account_interval)

    log("Cargando carpeta de imagenes.")
    click_button(app, title, "Carpetas Imagenes")
    time.sleep(1)
    select_path_in_dialog(".*Carpetas Imagenes.*", images_root, is_folder=True)

    log("Cargando Excel de articulos.")
    click_button(app, title, "Archivo Articulo")
    time.sleep(1)
    select_path_in_dialog(".*Archivo Excel.*", articles_excel, is_folder=False)

    log("Configurando Precio Original y ubicacion.")
    checkbox = win.child_window(title="Precio Original", control_type="CheckBox")
    if checkbox.get_toggle_state() == 0:
        checkbox.toggle()

    edits = win.descendants(control_type="Edit")
    # En individual: email, password, account name, location.
    # En multiple: location is the first enabled edit before account fields in this section.
    location_candidates = [edit for edit in edits if edit.is_enabled()]
    if mode == "individual" and len(location_candidates) >= 4:
        location_candidates[3].set_edit_text(location)
    elif location_candidates:
        # Pick the widest enabled edit near the "Ubicacion" row.
        location_candidates[0].set_edit_text(location)

    set_checkbox_state(win, "Encuentro en un Lugar Publico", meet_public)
    set_checkbox_state(win, "Retiro en la Puerta", door_pickup)
    set_checkbox_state(win, "Entrega en la puerta", door_delivery)
    set_checkbox_state(win, "Publicar en Grupos de Facebook", publish_groups)
    set_checkbox_state(win, "Publicar sin Descripción", publish_without_description)


def run_prepare_if_requested() -> None:
    automator = SCRATCH_DIR.parent / "firupost_automator.py"
    if not automator.exists():
        raise FileNotFoundError(f"No encuentro {automator}")
    subprocess.run(["python", str(automator), "--source", "sync"], cwd=str(SCRATCH_DIR), check=True)


def find_image_candidates(images_root: Path, folder: str, name: str) -> list[Path]:
    candidates: list[Path] = []
    search_dirs = []
    if folder:
        search_dirs.append(images_root / folder)
    search_dirs.append(images_root)
    for search_dir in search_dirs:
        exact = search_dir / name
        if exact.exists() and exact.is_file():
            candidates.append(exact)
        if Path(name).suffix.lower() not in IMAGE_EXTENSIONS:
            for extension in IMAGE_EXTENSIONS:
                candidate = search_dir / f"{name}{extension}"
                if candidate.exists() and candidate.is_file():
                    candidates.append(candidate)
        if search_dir.exists():
            candidates.extend(
                path
                for path in search_dir.glob(f"{name}.*")
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
            )
    return list(dict.fromkeys(candidates))


def validate_image_file(path: Path) -> str | None:
    try:
        if path.stat().st_size < 1024:
            return f"archivo demasiado pequeno ({path.stat().st_size} bytes)"
        with Image.open(path) as img:
            img.verify()
    except Exception as exc:  # noqa: BLE001 - this is a validation report.
        return str(exc)
    return None


def batch_summary(excel_path: Path, images_root: Path) -> str:
    try:
        df = pd.read_excel(excel_path)
    except Exception as exc:  # noqa: BLE001 - preflight reports the real error elsewhere.
        return f"No pude leer resumen del Excel: {exc}"
    if df.empty:
        return "Lote vacio."
    first = df.iloc[0]
    folder = "" if pd.isna(first["CarpetaImg"]) else str(first["CarpetaImg"]).strip()
    name = str(first["NombreImg"]).strip()
    image_path = find_image_candidates(images_root, folder, name)
    image_note = str(image_path[0]) if image_path else "imagen no encontrada"
    return (
        f"Lote: {len(df)} producto(s). Primero: SKU={first['Sku']} | "
        f"Titulo={first['Titulo']} | Imagen={image_note}"
    )


def validate_excel_for_firupost(excel_path: Path, images_root: Path) -> list[str]:
    problems: list[str] = []
    if not excel_path.exists():
        return [f"No existe el Excel: {excel_path}"]
    if not images_root.exists():
        problems.append(f"No existe la carpeta de imagenes: {images_root}")
        return problems

    required = ["Titulo", "Precio", "Categoria", "Condicion", "Descripcion", "Etiqueta", "Sku", "Ubicacion", "CarpetaImg", "NombreImg"]
    df = pd.read_excel(excel_path)
    missing = [col for col in required if col not in df.columns]
    if missing:
        problems.append(f"Faltan columnas FiruPost: {missing}")
        return problems
    if df.empty:
        problems.append("El Excel no tiene productos.")
    if df["Sku"].duplicated().any():
        problems.append("Hay SKUs duplicados en el Excel.")
    for col in ["Titulo", "Precio", "Descripcion", "NombreImg"]:
        if df[col].isna().any() or (df[col].astype(str).str.strip() == "").any():
            problems.append(f"Hay valores vacios en columna {col}.")

    for _, row in df.iterrows():
        folder = "" if pd.isna(row["CarpetaImg"]) else str(row["CarpetaImg"]).strip()
        name = str(row["NombreImg"]).strip()
        candidates = find_image_candidates(images_root, folder, name)
        if not candidates:
            problems.append(f"No encontre imagen para SKU {row['Sku']} con NombreImg={name} y CarpetaImg={folder}")
            if len(problems) > 20:
                problems.append("Hay mas problemas; se detuvo el listado.")
                break
            continue
        image_problem = validate_image_file(candidates[0])
        if image_problem:
            problems.append(f"Imagen invalida para SKU {row['Sku']}: {candidates[0]} ({image_problem})")
            if len(problems) > 20:
                problems.append("Hay mas problemas; se detuvo el listado.")
                break
    return problems


def accounts_file_has_rows(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        df = pd.read_excel(path)
    except Exception:
        return False
    required = {"email", "password", "Nombre_Cuentas"}
    return required.issubset(set(df.columns)) and len(df.dropna(how="all")) > 0


def preflight(args: argparse.Namespace) -> list[str]:
    problems = validate_excel_for_firupost(args.articles_excel, args.images_root)
    if args.mode == "individual":
        if args.start and args.reuse_session and not args.account_name:
            problems.append("--start con --reuse-session requiere account-name.")
        elif args.start and not args.reuse_session and (not args.email or not args.password or not args.account_name):
            problems.append("--start en modo individual requiere email, password y account-name.")
    else:
        if not accounts_file_has_rows(args.accounts_excel):
            problems.append(f"Modo multiple requiere Cuentas_FB.xlsx con filas validas: {args.accounts_excel}")
    if args.start and not args.test_batch and not args.allow_full_start:
        problems.append("--start sobre inventario completo requiere --allow-full-start. Primero usa lote de prueba.")
    return problems


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Automatiza la configuracion de FiruPost hasta dejar Publicar listo.")
    parser.add_argument("--mode", choices=["individual", "multiple"], default="individual")
    parser.add_argument("--email", default=os.getenv("FIRUPOST_FB_EMAIL", ""))
    parser.add_argument("--password", default=os.getenv("FIRUPOST_FB_PASSWORD", ""))
    parser.add_argument("--account-name", default=os.getenv("FIRUPOST_FB_ACCOUNT_NAME", ""))
    parser.add_argument("--reuse-session", action="store_true", help="Usa una sesion de Facebook ya abierta en el perfil Chrome de FiruPost; no requiere password.")
    parser.add_argument("--ask-credentials", action="store_true", help="Pide email, nombre de cuenta y contrasena por consola sin guardarlos.")
    parser.add_argument("--ask-password", action="store_true", help="Pide la contrasena por consola sin guardarla.")
    parser.add_argument("--location", default=os.getenv("FIRUPOST_LOCATION", "Managua, Nicaragua"))
    parser.add_argument("--articles-excel", type=Path, default=ARTICLES_EXCEL)
    parser.add_argument("--images-root", type=Path, default=IMAGES_ROOT)
    parser.add_argument("--accounts-excel", type=Path, default=ACCOUNTS_EXCEL)
    parser.add_argument("--simultaneous-accounts", default="1")
    parser.add_argument("--account-interval", default="5")
    parser.add_argument("--prepare", action="store_true", help="Regenera ArticulosGenerados.xlsx antes de configurar UI.")
    parser.add_argument("--test-batch", action="store_true", help="Genera y carga un lote de prueba de pocos productos.")
    parser.add_argument("--test-sku", default="", help="SKU especifico para el lote de prueba.")
    parser.add_argument("--test-limit", type=int, default=1, help="Cantidad de productos en el lote de prueba.")
    parser.add_argument("--test-layout", choices=["flat", "nested", "direct"], default="direct")
    parser.add_argument("--test-category", default=os.getenv("FIRUPOST_TEST_CATEGORY", "Men's clothing & shoes"), help="Categoria de Facebook en ingles para el lote de prueba.")
    parser.add_argument("--test-condition", default=os.getenv("FIRUPOST_TEST_CONDITION", "New"), help="Condicion de Facebook en ingles para el lote de prueba.")
    parser.add_argument("--preflight-only", action="store_true", help="Valida Excel/imagenes/credenciales y no toca FiruPost.")
    parser.add_argument("--allow-full-start", action="store_true", help="Permite --start con inventario completo.")
    parser.add_argument("--meet-public", action="store_true", default=truthy_env("FIRUPOST_MEET_PUBLIC"), help="Marca Encuentro en un Lugar Publico.")
    parser.add_argument("--door-pickup", action="store_true", default=truthy_env("FIRUPOST_DOOR_PICKUP"), help="Marca Retiro en la Puerta.")
    parser.add_argument("--door-delivery", action="store_true", default=truthy_env("FIRUPOST_DOOR_DELIVERY"), help="Marca Entrega en la puerta.")
    parser.add_argument("--publish-groups", action="store_true", default=truthy_env("FIRUPOST_PUBLISH_GROUPS"), help="Marca Publicar en Grupos de Facebook.")
    parser.add_argument("--publish-without-description", action="store_true", default=truthy_env("FIRUPOST_WITHOUT_DESCRIPTION"), help="Marca Publicar sin Descripcion.")
    parser.add_argument("--start", action="store_true", help="Presiona Iniciar al final. Puede publicar en Facebook.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.ask_credentials:
        if not args.email:
            args.email = input("Correo o numero Facebook: ").strip()
        if not args.account_name:
            args.account_name = input("Nombre de cuenta en FiruPost: ").strip()
        if not args.reuse_session and not args.password:
            args.password = getpass.getpass("Contrasena Facebook: ")
    elif args.ask_password and not args.password:
        args.password = getpass.getpass("Contrasena Facebook: ")

    if args.prepare:
        run_prepare_if_requested()

    if args.test_batch:
        batch_dir = SCRATCH_DIR / "firupost_test_batch"
        test_excel, test_images, count = make_batch(
            source_excel=ARTICLES_EXCEL,
            source_images=IMAGES_ROOT,
            output_dir=batch_dir,
            sku=args.test_sku or None,
            limit=args.test_limit,
            layout=args.test_layout,
            category=args.test_category,
            condition=args.test_condition,
        )
        args.articles_excel = test_excel
        if args.test_layout == "flat":
            # Excel has CarpetaImg=imagenes, so FiruPost should receive the parent.
            args.images_root = batch_dir
        else:
            # nested and direct are loaded from the actual images folder.
            args.images_root = test_images
        log(f"Lote de prueba generado: {count} producto(s), layout={args.test_layout}")
        log(f"Excel prueba: {args.articles_excel}")
        log(f"Imagenes prueba: {args.images_root}")

    log(batch_summary(args.articles_excel, args.images_root))
    problems = preflight(args)
    if args.preflight_only:
        if problems:
            log("Preflight encontro problemas:")
            for problem in problems:
                log(f"- {problem}")
            raise SystemExit(1)
        log("Preflight OK.")
        return
    if problems:
        log("No continuo porque el preflight encontro problemas:")
        for problem in problems:
            log(f"- {problem}")
        raise SystemExit(1)

    pid = launch_or_attach()
    pid = complete_license_if_needed(pid)
    open_publish_article(pid)
    choose_mode(pid, args.mode)
    fill_publish_form(
        pid=pid,
        mode=args.mode,
        email=args.email,
        password=args.password,
        account_name=args.account_name,
        location=args.location,
        articles_excel=args.articles_excel,
        images_root=args.images_root,
        accounts_excel=args.accounts_excel,
        simultaneous_accounts=args.simultaneous_accounts,
        account_interval=args.account_interval,
        reuse_session=args.reuse_session,
        meet_public=args.meet_public,
        door_pickup=args.door_pickup,
        door_delivery=args.door_delivery,
        publish_groups=args.publish_groups,
        publish_without_description=args.publish_without_description,
    )

    if args.start:
        log("Presionando Iniciar por solicitud explicita (--start).")
        title = "Publicar - Modo Individual" if args.mode == "individual" else "Publicar - Modo Cuentas Multiples"
        click_button(get_main_app(latest_firupost_pid() or pid), title, "Iniciar")
    else:
        log("Configuracion lista. No presione Iniciar porque no se paso --start.")


if __name__ == "__main__":
    main()

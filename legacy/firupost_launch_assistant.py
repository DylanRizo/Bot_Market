from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


SCRATCH_DIR = Path(__file__).resolve().parent
FIRUPOST_EXE = Path(r"C:\FiruPost\FiruPost.exe")
AUTOMATOR = SCRATCH_DIR.parent / "firupost_automator.py"
ARTICLES_EXCEL = SCRATCH_DIR / "ArticulosGenerados.xlsx"
IMAGES_ROOT = SCRATCH_DIR / "imagenes_firupost"
REPORT_EXCEL = SCRATCH_DIR / "firupost_validation_report.xlsx"
PROMPT_FILE = SCRATCH_DIR / "prompt_firupost_ia.txt"
SESSION_CONFIG = SCRATCH_DIR / "firupost_session_config.json"


def copy_to_clipboard(text: str) -> bool:
    try:
        import tkinter as tk

        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def run_prepare(args: argparse.Namespace) -> None:
    cmd = [
        sys.executable,
        str(AUTOMATOR),
        "--source",
        args.source,
        "--location",
        args.location,
        "--currency",
        args.currency,
    ]
    if args.whatsapp:
        cmd.extend(["--whatsapp", args.whatsapp])
    if args.allow_duplicates:
        cmd.append("--allow-duplicates")
    if args.include_invalid:
        cmd.append("--include-invalid")

    print("Preparando paquete FiruPost...")
    subprocess.run(cmd, cwd=str(SCRATCH_DIR), check=True)


def write_session_config(args: argparse.Namespace) -> None:
    config = {
        "firupost_exe": str(FIRUPOST_EXE),
        "articles_excel": str(ARTICLES_EXCEL),
        "images_root": str(IMAGES_ROOT),
        "validation_report": str(REPORT_EXCEL),
        "ai_prompt": str(PROMPT_FILE),
        "location": args.location,
        "currency": args.currency,
        "whatsapp": args.whatsapp,
        "recommended_firupost_publish_options": {
            "module": "Publicar",
            "publication_type": "Publicar En Articulo",
            "price_option": "Precio Original",
            "image_folder_button": "Carpetas Imagenes",
            "article_file_button": "Archivo Articulo",
        },
    }
    SESSION_CONFIG.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Config de sesion: {SESSION_CONFIG}")


def launch_firupost() -> None:
    if not FIRUPOST_EXE.exists():
        print(f"No encuentro FiruPost en: {FIRUPOST_EXE}")
        return

    print(f"Abriendo FiruPost: {FIRUPOST_EXE}")
    try:
        subprocess.Popen([str(FIRUPOST_EXE)], cwd=str(FIRUPOST_EXE.parent))
    except Exception as exc:
        print(f"No se pudo abrir FiruPost automaticamente: {exc}")
        print("Abre FiruPost manualmente y usa los paths mostrados abajo.")


def print_next_steps() -> None:
    copied = copy_to_clipboard(str(ARTICLES_EXCEL))
    clipboard_note = "Copie el path del Excel al portapapeles." if copied else "No pude copiar al portapapeles."
    print()
    print("Paths para FiruPost")
    print(f"- Archivo Articulo: {ARTICLES_EXCEL}")
    print(f"- Carpetas Imagenes: {IMAGES_ROOT}")
    print(f"- Reporte validacion: {REPORT_EXCEL}")
    print(f"- Prompt IA: {PROMPT_FILE}")
    print()
    print(clipboard_note)
    print()
    print("Flujo recomendado en FiruPost")
    print("1. Entra a Publicar > Publicar En Articulo.")
    print("2. Selecciona modo Individual o Cuentas Multiples.")
    print("3. En Carpetas Imagenes, carga la carpeta imagenes_firupost.")
    print("4. En Archivo Articulo, carga ArticulosGenerados.xlsx.")
    print("5. Usa Precio Original y Ubicacion Managua, Nicaragua.")
    print("6. Revisa una publicacion de prueba antes de lanzar todo el lote.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepara y abre una sesion de publicacion con FiruPost.")
    parser.add_argument("--source", choices=["sync", "existing"], default="sync")
    parser.add_argument("--location", default="Managua, Nicaragua")
    parser.add_argument("--currency", default="C$")
    parser.add_argument("--whatsapp", default="")
    parser.add_argument("--allow-duplicates", action="store_true")
    parser.add_argument("--include-invalid", action="store_true")
    parser.add_argument("--skip-prepare", action="store_true")
    parser.add_argument("--skip-launch", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.skip_prepare:
        run_prepare(args)
    write_session_config(args)
    if not args.skip_launch:
        launch_firupost()
    print_next_steps()


if __name__ == "__main__":
    main()

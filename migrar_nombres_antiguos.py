"""Migra una instalacion anterior a los nombres actuales.

Antes la carpeta de fotos se llamaba ``imagenes_firupost`` y la clave de IA se
guardaba en ``.firupost_openai_key.bin``. Este script, que se ejecuta una vez:

- renombra la carpeta a ``imagenes_productos``;
- renombra el archivo de la clave de IA a ``.openai_key.bin``;
- reescribe en la base de datos (y en los JSON de actividad y campana) las rutas
  de fotos que mencionan el nombre anterior.

Detener el panel y el trabajador antes de ejecutarlo. Guarda una copia de cada
archivo que modifica junto al original (``.bak-AAAAMMDD-HHMMSS``).

    python migrar_nombres_antiguos.py [--dry-run]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from marketplace_runtime import IMAGES_DIR_NAME, LEGACY_IMAGES_DIR_NAME, SCRATCH_DIR
from marketplace_storage import default_db_path

KEY_NAME = ".openai_key.bin"
LEGACY_KEY_NAME = ".firupost_openai_key.bin"
JSON_FILES = ("marketplace_activity.json", "marketplace_campaign_example.json")


def _backup(path: Path, stamp: str) -> Path:
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, backup)
    return backup


def _rewrite_database(db_path: Path, stamp: str, dry_run: bool) -> int:
    """Reemplaza el nombre anterior en todas las columnas de texto. Devuelve las filas tocadas."""
    pattern = f"%{LEGACY_IMAGES_DIR_NAME}%"
    connection = sqlite3.connect(db_path)
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        targets: list[tuple[str, str, int]] = []
        for table in tables:
            for _cid, column, kind, *_rest in connection.execute(f'PRAGMA table_info("{table}")'):
                if kind.upper() not in ("TEXT", ""):
                    continue
                count = connection.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}" LIKE ?', (pattern,)
                ).fetchone()[0]
                if count:
                    targets.append((table, column, count))
        total = sum(count for _table, _column, count in targets)
        if dry_run or not total:
            return total
    finally:
        connection.close()
    _backup(db_path, stamp)
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            for table, column, _count in targets:
                connection.execute(
                    f'UPDATE "{table}" SET "{column}" = REPLACE("{column}", ?, ?) WHERE "{column}" LIKE ?',
                    (LEGACY_IMAGES_DIR_NAME, IMAGES_DIR_NAME, pattern),
                )
    finally:
        connection.close()
    return total


def migrate(base: Path, db_path: Path, dry_run: bool = False) -> list[str]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    report: list[str] = []
    prefix = "[prueba] " if dry_run else ""

    old_dir, new_dir = base / LEGACY_IMAGES_DIR_NAME, base / IMAGES_DIR_NAME
    folder_ready = new_dir.is_dir()
    if old_dir.is_dir() and new_dir.exists():
        report.append(
            f"AVISO: existen {LEGACY_IMAGES_DIR_NAME} y {IMAGES_DIR_NAME}. Une el contenido a mano; "
            "no se reescribieron rutas de la base de datos."
        )
        folder_ready = False
    elif old_dir.is_dir():
        if not dry_run:
            old_dir.rename(new_dir)
        report.append(f"{prefix}Carpeta {LEGACY_IMAGES_DIR_NAME} -> {IMAGES_DIR_NAME}")
        folder_ready = True

    old_key, new_key = base / LEGACY_KEY_NAME, base / KEY_NAME
    if old_key.is_file() and not new_key.exists():
        if not dry_run:
            old_key.rename(new_key)
        report.append(f"{prefix}Clave de IA {LEGACY_KEY_NAME} -> {KEY_NAME}")

    if not folder_ready:
        return report or ["Nada que migrar."]

    if db_path.is_file():
        rows = _rewrite_database(db_path, stamp, dry_run)
        if rows:
            report.append(f"{prefix}Base de datos: {rows} campos con rutas actualizados (copia .bak-{stamp})")
    for name in JSON_FILES:
        path = base / name
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            if LEGACY_IMAGES_DIR_NAME in text:
                if not dry_run:
                    _backup(path, stamp)
                    path.write_text(text.replace(LEGACY_IMAGES_DIR_NAME, IMAGES_DIR_NAME), encoding="utf-8")
                report.append(f"{prefix}{name}: rutas actualizadas")
    return report or ["Nada que migrar."]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true", help="Muestra lo que haria sin cambiar nada.")
    args = parser.parse_args()
    for line in migrate(SCRATCH_DIR, default_db_path(), args.dry_run):
        print(line)


if __name__ == "__main__":
    main()

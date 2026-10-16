"""Cambio de nombre de la carpeta de fotos y de la clave de IA."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import migrar_nombres_antiguos as migracion
from marketplace_runtime import alias_images_path, images_root
from marketplace_storage import MarketplaceStore


def test_images_root_usa_la_carpeta_nueva_si_existe(tmp_path: Path) -> None:
    (tmp_path / "imagenes_productos").mkdir()
    (tmp_path / "imagenes_firupost").mkdir()
    assert images_root(tmp_path) == tmp_path / "imagenes_productos"


def test_images_root_conserva_la_anterior_si_es_la_unica(tmp_path: Path) -> None:
    (tmp_path / "imagenes_firupost").mkdir()
    assert images_root(tmp_path) == tmp_path / "imagenes_firupost"


def test_images_root_sin_carpetas_apunta_a_la_nueva(tmp_path: Path) -> None:
    assert images_root(tmp_path) == tmp_path / "imagenes_productos"


def test_alias_traduce_en_ambas_direcciones(tmp_path: Path) -> None:
    nueva = tmp_path / "imagenes_productos" / "A" / "foto.jpg"
    nueva.parent.mkdir(parents=True)
    nueva.write_bytes(b"x")
    assert alias_images_path(tmp_path / "imagenes_firupost" / "A" / "foto.jpg") == nueva

    (tmp_path / "otra").mkdir()
    (tmp_path / "otra" / "imagenes_firupost").mkdir()
    vieja = tmp_path / "otra" / "imagenes_firupost"
    assert alias_images_path(tmp_path / "otra" / "imagenes_productos") == vieja
    inexistente = tmp_path / "nada" / "imagenes_firupost"
    assert alias_images_path(inexistente) == inexistente


def _instalacion_anterior(base: Path) -> Path:
    (base / "imagenes_firupost" / "CMP-NEG").mkdir(parents=True)
    (base / "imagenes_firupost" / "CMP-NEG" / "foto_1.jpg").write_bytes(b"x")
    (base / ".firupost_openai_key.bin").write_bytes(b"secreto")
    (base / "marketplace_activity.json").write_text(json.dumps({"p": "imagenes_firupost/CMP-NEG"}), encoding="utf-8")
    db = base / "marketplace_bot.db"
    almacen = MarketplaceStore(db)
    almacen.set_setting("autonomy", {"account_media": {"c1": {"f": [str(base / "imagenes_firupost" / "CMP-NEG" / "foto_1.jpg")]}}})
    return db


def test_migracion_renombra_y_reescribe_las_rutas(tmp_path: Path) -> None:
    db = _instalacion_anterior(tmp_path)

    informe = migracion.migrate(tmp_path, db)

    assert (tmp_path / "imagenes_productos" / "CMP-NEG" / "foto_1.jpg").is_file()
    assert not (tmp_path / "imagenes_firupost").exists()
    assert (tmp_path / ".openai_key.bin").read_bytes() == b"secreto"
    assert not (tmp_path / ".firupost_openai_key.bin").exists()
    assert "imagenes_productos" in (tmp_path / "marketplace_activity.json").read_text(encoding="utf-8")
    with sqlite3.connect(db) as conexion:
        valores = [fila[0] for fila in conexion.execute("SELECT value_json FROM settings")]
    assert valores and all("imagenes_firupost" not in valor for valor in valores)
    assert any("imagenes_productos" in valor for valor in valores)
    assert list(tmp_path.glob("marketplace_bot.db.bak-*"))
    assert any("Base de datos" in linea for linea in informe)


def test_migracion_en_prueba_no_cambia_nada(tmp_path: Path) -> None:
    db = _instalacion_anterior(tmp_path)

    informe = migracion.migrate(tmp_path, db, dry_run=True)

    assert (tmp_path / "imagenes_firupost").is_dir()
    assert (tmp_path / ".firupost_openai_key.bin").exists()
    assert not list(tmp_path.glob("*.bak-*"))
    assert informe


def test_migracion_no_toca_la_base_si_existen_las_dos_carpetas(tmp_path: Path) -> None:
    db = _instalacion_anterior(tmp_path)
    (tmp_path / "imagenes_productos").mkdir()

    informe = migracion.migrate(tmp_path, db)

    assert (tmp_path / "imagenes_firupost").is_dir()
    with sqlite3.connect(db) as conexion:
        valores = [fila[0] for fila in conexion.execute("SELECT value_json FROM settings")]
    assert any("imagenes_firupost" in valor for valor in valores)
    assert any("AVISO" in linea for linea in informe)


def test_migracion_sin_nada_que_hacer(tmp_path: Path) -> None:
    assert migracion.migrate(tmp_path, tmp_path / "no_existe.db") == ["Nada que migrar."]

"""Varias publicaciones del mismo producto con fotos distintas."""
from __future__ import annotations

import subprocess
from pathlib import Path

import marketplace_campaign_runner as runner
from marketplace_photo_rounds import MAX_LISTING_PHOTOS, next_round_size, plan_round


def sizes(total: int, per_listing: int) -> list[int]:
    result = []
    while total > 0:
        size = next_round_size(total, per_listing)
        result.append(size)
        total -= size
    return result


def test_el_reparto_es_parejo_y_nunca_deja_una_foto_sola() -> None:
    assert sizes(10, 3) == [3, 3, 2, 2]
    assert sizes(7, 3) == [3, 2, 2]
    assert sizes(6, 3) == [3, 3]
    assert sizes(4, 10) == [4]
    assert sizes(1, 3) == [1]
    assert next_round_size(0, 3) == 0
    assert next_round_size(5, 0) == 0


def banco(cantidad: int) -> list[str]:
    return [f"foto{n}" for n in range(cantidad)]


def plan(pool, **kwargs):
    kwargs.setdefault("per_listing", 3)
    kwargs.setdefault("blocked", set())
    return plan_round(pool, hash_of=lambda path: f"h-{path}", **kwargs)


def test_cada_ronda_toma_las_siguientes_fotos_sin_usar() -> None:
    usados: set[str] = set()
    rondas = []
    for numero in range(4):
        chunk = plan(banco(10), blocked=set(usados), round_number=numero, rotate_cover=False)
        rondas.append(chunk)
        usados.update(f"h-{foto}" for foto in chunk)
    assert [len(r) for r in rondas] == [3, 3, 2, 2]
    assert sum(rondas, []) == banco(10)
    assert plan(banco(10), blocked=usados) == [], "sin fotos nuevas no hay otra ronda"


def test_la_portada_rota_en_cada_ronda() -> None:
    assert plan(banco(3), round_number=0) == ["foto0", "foto1", "foto2"]
    assert plan(banco(3), round_number=1) == ["foto1", "foto2", "foto0"]
    assert plan(banco(3), round_number=2) == ["foto2", "foto0", "foto1"]
    assert plan(banco(3), round_number=1, rotate_cover=False) == ["foto0", "foto1", "foto2"]


def test_cada_cuenta_empieza_en_un_bloque_distinto() -> None:
    primera = plan(banco(10), account_index=0, rotate_cover=False)
    segunda = plan(banco(10), account_index=1, rotate_cover=False)
    tercera = plan(banco(10), account_index=2, rotate_cover=False)
    assert primera == ["foto0", "foto1", "foto2"]
    assert segunda == ["foto3", "foto4", "foto5"]
    assert tercera == ["foto6", "foto7", "foto8"]
    assert plan(banco(10), account_index=1, stagger=False, rotate_cover=False) == primera


def test_el_desplazamiento_por_cuenta_no_cambia_al_usar_fotos() -> None:
    """Si el banco restante cambiara el desplazamiento, la cuenta saltaria fotos."""
    usados = {"h-foto3", "h-foto4", "h-foto5"}
    chunk = plan(banco(10), account_index=1, blocked=usados, rotate_cover=False)
    assert chunk == ["foto6", "foto7", "foto8"]


def test_ignora_fotos_repetidas_por_contenido_y_las_que_no_se_pueden_leer() -> None:
    def hash_of(path: str) -> str:
        if path == "rota":
            raise OSError
        return "misma" if path in {"a", "b"} else f"h-{path}"

    chunk = plan_round(["a", "b", "rota", "c"], per_listing=5, hash_of=hash_of, blocked=set(), rotate_cover=False)
    assert chunk == ["a", "c"]


def test_un_anuncio_nunca_pasa_del_maximo_de_marketplace() -> None:
    assert len(plan(banco(30), per_listing=25, rotate_cover=False)) <= MAX_LISTING_PHOTOS


def test_la_redaccion_cambia_con_cada_ronda(monkeypatch) -> None:
    comandos: list[list[str]] = []

    def falso_run(command, **kwargs):
        comandos.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='{"jobs": []}', stderr="")

    monkeypatch.setattr(runner.subprocess, "run", falso_run)
    base = {"name": "Camisa", "listing_mode": "individual", "source_excel": "x.xlsx", "images_root": "imgs", "account": "c1"}

    def semilla(job: dict) -> str:
        comandos.clear()
        runner.build_dynamic_jobs(job, {}, Path("."), "c1", vary_copy=True)
        command = comandos[0]
        return command[command.index("--variant-seed") + 1]

    sin_ronda = semilla(base)
    ronda_dos = semilla({**base, "copy_variant": 2})
    ronda_tres = semilla({**base, "copy_variant": 3})
    assert "|r" not in sin_ronda and ronda_dos.endswith("|r2") and ronda_tres.endswith("|r3")
    assert len({sin_ronda, ronda_dos, ronda_tres}) == 3

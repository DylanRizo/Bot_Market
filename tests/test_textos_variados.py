"""Titulos, descripciones y etiquetas distintos en cada publicacion, sin cambiar los datos."""
from __future__ import annotations

import itertools
import re

import pytest

from marketplace_copy import PROFILES, build_copy, similarity
from marketplace_listing_builder import price_text

CUENTAS = ["sebastian", "Dylan", "Dylan Steven"]


def texto(base, colores, tallas, precio=450, familia="", semilla="", recientes=None):
    return build_copy(base, colores, tallas, [precio], "Managua, Nicaragua", price_text=price_text,
                      family=familia, seed=semilla, recent=recientes)


def test_tres_cuentas_publican_textos_distintos_del_mismo_producto():
    copias = [texto("Camisa de compresion manga corta", ["Blanco", "Gris", "Negro"], ["S", "M", "L", "XL"],
                    familia="compression_short", semilla=f"{cuenta}|compression_short|2026-10-19|q1") for cuenta in CUENTAS]
    assert len({copia.title for copia in copias}) == 3
    assert len({copia.description for copia in copias}) == 3
    for una, otra in itertools.combinations(copias, 2):
        assert similarity(una, {"title": otra.title, "description": otra.description}) < 0.8


def test_el_mismo_anuncio_otro_dia_cambia_de_redaccion():
    dias = [texto("Bolso deportivo", ["Negro"], [], familia="bolsos", semilla=f"Dylan|bolsos|2026-10-{dia}|q")
            for dia in (19, 26)]
    assert dias[0].title != dias[1].title or dias[0].description != dias[1].description


def test_productos_sin_tallas_tambien_varian_el_titulo():
    """Antes bolsos y munequeras salian con el mismo titulo en las tres cuentas."""
    titulos = {texto("Munequeras deportivas", ["Azul", "Gris", "Rojo"], [], familia="munequeras",
                     semilla=f"{cuenta}|munequeras|2026-10-19|q").title for cuenta in CUENTAS}
    assert len(titulos) == 3


def test_evita_parecerse_a_lo_ya_publicado():
    primero = texto("Short deportivo", ["Gris", "Negro"], ["M", "L"], familia="shorts", semilla="sebastian|shorts|d|1")
    publicado = [{"title": primero.title, "description": primero.description}]
    segundo = texto("Short deportivo", ["Gris", "Negro"], ["M", "L"], familia="shorts", semilla="sebastian|shorts|d|1",
                    recientes=publicado)
    assert segundo.title != primero.title
    assert similarity(segundo, publicado[0]) < similarity(primero, publicado[0])


@pytest.mark.parametrize("cuenta", CUENTAS)
def test_los_datos_no_cambian(cuenta):
    copia = texto("Calcetas de compresion 15-20 mmHg", ["Beige", "Negro"], ["S", "M", "L", "XL"], precio=400,
                  familia="calcetas", semilla=f"{cuenta}|calcetas|2026-10-19|q")
    descripcion = copia.description.lower()
    assert "c$400" in descripcion and "managua" in descripcion
    assert "beige" in descripcion and "negro" in descripcion
    for talla in ("S", "M", "L", "XL"):
        assert re.search(rf"\b{talla}\b", copia.description)
    assert "15-20 mmHg" in copia.title
    assert len(copia.title) <= 99


def test_sin_semilla_el_texto_es_siempre_el_mismo():
    assert texto("Durag", ["Negro", "Rojo"], [], familia="durags") == texto("Durag", ["Negro", "Rojo"], [], familia="durags")


def test_etiquetas_sueltas_y_sin_repetir():
    copia = texto("Chaleco de lana sin mangas", ["Azul", "Beige", "Gris"], ["S", "M", "L"], familia="chalecos",
                  semilla="Dylan|chalecos|d|1")
    assert 4 <= len(copia.tags) <= 8
    assert len(copia.tags) == len({tag.lower() for tag in copia.tags})
    assert not any(" y " in tag for tag in copia.tags if tag in ("azul", "beige", "gris")), copia.tags
    assert all("," not in tag for tag in copia.tags)


BASES = {
    "compression_short": "Camisa de compresion manga corta", "compression_sleeveless": "Camisa sin mangas",
    "compression_long": "Camisa de compresion manga larga", "bershka": "Camisa Bersek",
    "chalecos": "Chaleco de lana sin mangas", "leggins": "Leggins deportivos", "enterizos": "Enterizo deportivo",
    "enterizos_largos": "Enterizo largo deportivo", "shorts": "Short deportivo", "shorts_youngla": "Short deportivo YoungLA",
    "faldas": "Falda deportiva", "tops": "Top deportivo", "bolsos": "Bolso deportivo", "mochilas": "Mochila antirrobo",
    "durags": "Durag", "munequeras": "Munequeras deportivas", "straps": "Straps para gimnasio",
    "cinturones": "Cinturon de gimnasio", "mangas_brazo": "Manga para brazo",
    "calcetas": "Calcetas de compresion 20-30 mmHg", "calcetas_multicolor": "Calcetas", "joggers": "Pantalon jogger",
    "rodilleras": "Rodilleras deportivas",
}


def test_cada_familia_tiene_su_vocabulario():
    assert set(BASES) == set(PROFILES)


@pytest.mark.parametrize("familia", sorted(BASES))
def test_redaccion_limpia_en_todas_las_familias(familia):
    for indice, (colores, tallas) in enumerate([(["Negro"], []), (["Negro", "Cafe"], ["S", "M"]), (["Azul", "Gris", "Rojo"], ["M"])]):
        for semilla in [""] + [f"cuenta{n}|{familia}|d|{indice}" for n in range(6)]:
            copia = texto(BASES[familia], colores, tallas, familia=familia, semilla=semilla)
            todo = f"{copia.title}\n{copia.description}"
            assert "{" not in todo and "}" not in todo, todo
            assert not re.search(r"\b(\w+) \1\b", todo.lower()), todo  # "nueva nueva"
            assert "por unidad" not in todo and "Precio especial" not in todo
            assert "disponibilidad que" not in todo and "disponibilidad de disponibilidad" not in todo
            assert copia.description.count("tenemos") <= 1, copia.description
            assert copia.title[0].isupper() and len(copia.title) <= 99
            if "Cafe" in colores:
                assert "Café" in copia.title or "café" in copia.description


def test_el_texto_publicado_queda_guardado_para_compararlo(store):
    store.record_publish_event("Dylan", "Bolso para gym | Negro", "publicador",
                               description="Bolso deportivo nuevo.", tags=["bolso", "gym"])
    copias = store.recent_copies("2000-01-01")
    assert copias[0]["title"] == "Bolso para gym | Negro"
    assert copias[0]["description"] == "Bolso deportivo nuevo."
    assert copias[0]["tags"] == "bolso;gym"


def test_la_ia_viene_apagada_y_solo_la_enciende_la_persona():
    from marketplace_scheduler import normalize_config

    assert normalize_config({})["ai_descriptions"] is False
    assert normalize_config({"ai_descriptions": "si"})["ai_descriptions"] is False
    assert normalize_config({"ai_descriptions": True})["ai_descriptions"] is True


@pytest.mark.parametrize("encendida", [True, False])
def test_el_interruptor_llega_hasta_el_redactor(tmp_path, monkeypatch, encendida):
    import json
    import subprocess

    import marketplace_campaign_runner as runner
    import marketplace_scheduler_worker as worker

    monkeypatch.setattr(worker, "WORKER_JOBS_DIR", tmp_path)
    monkeypatch.setattr(worker, "CAMPAIGN_PATH", tmp_path / "base.json")
    item = {"id": "q1", "account": "Dylan", "job": {"name": "Bolsos", "family_key": "bolsos"}}
    campana = json.loads(worker.campaign_for_item(item, {"ai_descriptions": encendida}).read_text(encoding="utf-8"))
    assert campana["defaults"]["ai_descriptions"] is encendida

    comandos = []
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **k: comandos.append(command)
                        or subprocess.CompletedProcess(command, 0, '{"jobs": []}', ""))
    job = {**campana["jobs"][0], "listing_mode": "grouped", "source_excel": "a.xlsx", "images_root": "fotos"}
    runner.build_dynamic_jobs(job, campana["defaults"], tmp_path, "Dylan", vary_copy=True)
    assert ("--ai-descriptions" in comandos[0]) is encendida
    assert comandos[0][comandos[0].index("--family-key") + 1] == "bolsos"

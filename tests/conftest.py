"""Utilidades comunes de las pruebas.

Todo corre sin red, sin Chrome y sin tocar la base real: cada prueba recibe un
almacen nuevo en un directorio temporal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRATCH_DIR = Path(__file__).resolve().parent.parent
if str(SCRATCH_DIR) not in sys.path:
    sys.path.insert(0, str(SCRATCH_DIR))

from marketplace_storage import MarketplaceStore  # noqa: E402


@pytest.fixture(autouse=True)
def sin_sgi_real(monkeypatch):
    """Ninguna prueba llega al SGI real, aunque este equipo tenga direccion y llave
    configuradas. Las pruebas de la puerta del SGI lo activan explicitamente."""
    import marketplace_scheduler_worker as worker

    monkeypatch.setattr(worker, "sgi_configured", lambda: False)


@pytest.fixture()
def store(tmp_path: Path) -> MarketplaceStore:
    return MarketplaceStore(tmp_path / "prueba.db")


@pytest.fixture()
def photo(tmp_path: Path):
    """Crea fotos de mentira con contenido distinto para probar los hashes."""
    counter = {"n": 0}

    def make(content: str = "") -> Path:
        counter["n"] += 1
        path = tmp_path / f"foto_{counter['n']}.jpg"
        path.write_bytes((content or f"imagen-{counter['n']}").encode("utf-8"))
        return path

    return make


def enqueue_job(store: MarketplaceStore, **overrides) -> str:
    item = {
        "week_key": "2026-W37",
        "scheduled_at": "2026-09-07T09:00:00",
        "account": "cuenta1",
        "family_key": "durags",
        "name": "Durags",
        "job": {"name": "Durags", "family_key": "durags"},
        "fingerprint": "huella-1",
        "dedupe_key": "2026-09-07T09:00:00|cuenta1|durags",
        "status": "queued",
        "approved": True,
        "max_attempts": 3,
    }
    item.update(overrides)
    queue_id, _ = store.enqueue(item)
    return queue_id

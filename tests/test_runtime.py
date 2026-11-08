"""Pruebas de los utilitarios de procesos y candados de cuenta."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import marketplace_runtime
from marketplace_runtime import AccountLock, process_exists


def test_consultar_un_proceso_vivo_no_lo_termina():
    """En Windows os.kill(pid, 0) mata el proceso: la consulta no debe hacerlo."""
    proceso = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_exists(proceso.pid) is True
        assert process_exists(proceso.pid) is True
        assert proceso.poll() is None, "consultar el proceso lo termino"
    finally:
        proceso.kill()
        proceso.wait()


def test_un_proceso_terminado_no_existe():
    proceso = subprocess.Popen([sys.executable, "-c", "pass"])
    proceso.wait()
    assert process_exists(proceso.pid) is False


def test_el_proceso_actual_existe():
    assert process_exists(os.getpid()) is True


def test_un_pid_invalido_no_existe():
    assert process_exists(0) is False
    assert process_exists(-5) is False


def _candado(carpeta, pid, escrito):
    (carpeta / "scheduler_worker.lock").write_text(
        json.dumps({"pid": pid, "account": "scheduler_worker", "created_at": escrito}), encoding="utf-8"
    )


@pytest.fixture
def otro_proceso():
    proceso = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield proceso
    proceso.kill()
    proceso.wait()


def test_un_candado_cuyo_numero_paso_a_otro_proceso_se_libera(monkeypatch, tmp_path, otro_proceso):
    """El proceso arranco despues de escrito el candado: no es el que lo dejo."""
    monkeypatch.setattr(marketplace_runtime, "LOCKS_DIR", tmp_path)
    _candado(tmp_path, otro_proceso.pid, time.time() - 3600)

    candado = AccountLock("scheduler_worker")
    candado.acquire()

    assert candado.acquired
    candado.release()


def test_un_candado_cuyo_numero_paso_a_un_proceso_del_sistema_se_libera(monkeypatch, tmp_path):
    """Asi quedo el 5 de noviembre: el proceso existe pero Windows no deja abrirlo."""
    monkeypatch.setattr(marketplace_runtime, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(marketplace_runtime, "process_exists", lambda pid: True)
    monkeypatch.setattr(marketplace_runtime, "_windows_process_started_at", lambda pid: None)
    _candado(tmp_path, 2924, time.time() - 3600)

    candado = AccountLock("scheduler_worker")
    candado.acquire()

    assert candado.acquired
    candado.release()


def test_un_candado_de_un_proceso_que_sigue_vivo_se_respeta(monkeypatch, tmp_path, otro_proceso):
    monkeypatch.setattr(marketplace_runtime, "LOCKS_DIR", tmp_path)
    _candado(tmp_path, otro_proceso.pid, time.time())

    with pytest.raises(RuntimeError):
        AccountLock("scheduler_worker").acquire()


def test_un_numero_escrito_antes_de_que_arrancara_el_proceso_no_es_suyo(otro_proceso):
    assert marketplace_runtime.process_wrote(otro_proceso.pid, time.time() - 3600) is False
    assert marketplace_runtime.process_wrote(otro_proceso.pid, time.time()) is True

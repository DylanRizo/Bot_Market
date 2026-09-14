"""Pruebas de los utilitarios de procesos y candados de cuenta."""
from __future__ import annotations

import os
import subprocess
import sys

from marketplace_runtime import process_exists


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

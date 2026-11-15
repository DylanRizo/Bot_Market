#!/bin/bash
# Lo lanza launchd en la Mac (ver instalar_trabajador_mac.py), igual que la tarea de
# Windows lanza ejecutar_trabajador_marketplace.ps1. launchd guarda la salida en
# marketplace_scheduler_worker.log.
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONUNBUFFERED=1
exec ./.venv-mac/bin/python marketplace_scheduler_worker.py

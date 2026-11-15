#!/bin/bash
# Prepara el bot en esta Mac: Python, librerias y el trabajador que arranca solo.
# Se puede abrir con doble clic o desde la Terminal:  bash instalar_mac.command
set -e
cd "$(dirname "$0")"
echo "== Bot Marketplace La Comarca: instalacion en Mac"

case "$PWD/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*|"$HOME/Library/Mobile Documents/"*)
    echo "La carpeta del bot esta en: $PWD"
    echo "macOS no deja que el trabajador automatico lea Escritorio, Documentos, Descargas ni iCloud."
    echo "Mueve la carpeta a tu carpeta de usuario (por ejemplo $HOME/BotMarketplace) y vuelve a abrir este archivo."
    exit 1
    ;;
esac

PY=""
for candidate in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 \
    && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Falta Python 3.10 o mas nuevo."
  echo "Instalalo desde https://www.python.org/downloads/macos/ y vuelve a abrir este archivo."
  exit 1
fi
echo "Python: $PY"

if [ ! -d "/Applications/Google Chrome.app" ] && [ ! -d "$HOME/Applications/Google Chrome.app" ]; then
  echo "Aviso: no encontre Google Chrome en Aplicaciones. Instalalo antes de abrir las sesiones."
fi

if [ ! -x .venv-mac/bin/python ]; then
  "$PY" -m venv .venv-mac
fi
.venv-mac/bin/python -m pip install --upgrade pip
.venv-mac/bin/python -m pip install -r requirements_marketplace_bot.txt

chmod +x iniciar_panel_marketplace.command ejecutar_trabajador_marketplace.sh instalar_mac.command 2>/dev/null || true
.venv-mac/bin/python instalar_trabajador_mac.py

echo
echo "Listo. Abre el panel con iniciar_panel_marketplace.command"

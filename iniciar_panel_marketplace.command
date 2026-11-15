#!/bin/bash
# Abre el panel del bot en el navegador (en Windows: iniciar_panel_marketplace.ps1).
# Si el panel no esta corriendo, lo arranca en segundo plano; cerrar esta ventana
# no lo detiene.
cd "$(dirname "$0")" || exit 1
PORT="${1:-8794}"
PY=./.venv-mac/bin/python
TOKEN_FILE=marketplace_dashboard_token.txt

if [ ! -x "$PY" ]; then
  echo "Primero abre instalar_mac.command."
  exit 1
fi

listening() {
  "$PY" -c 'import socket, sys
s = socket.socket()
s.settimeout(1)
sys.exit(s.connect_ex(("127.0.0.1", int(sys.argv[1]))))' "$PORT"
}

if ! listening; then
  # El panel escribe su token al arrancar. Se quita el anterior para no abrir el
  # navegador con una llave vieja si el arranque falla.
  rm -f "$TOKEN_FILE"
  # La salida normal lleva el enlace con el token: no se guarda. Los errores si.
  PYTHONIOENCODING=utf-8 nohup "$PY" marketplace_bot_dashboard.py --port "$PORT" --no-open \
    > /dev/null 2>> marketplace_dashboard.log &
  # Al arrancar, el panel carga las librerias de Google y revisa la base; en frio
  # puede tardar mas de 15 segundos.
  for _ in $(seq 1 120); do
    sleep 0.5
    listening && break
  done
fi

if ! listening; then
  echo "El panel no pudo iniciar en el puerto $PORT. Revisa marketplace_dashboard.log."
  exit 1
fi

for _ in $(seq 1 20); do
  [ -s "$TOKEN_FILE" ] && break
  sleep 0.5
done
if [ ! -s "$TOKEN_FILE" ]; then
  echo "El panel arranco pero no dejo su token en $TOKEN_FILE."
  exit 1
fi

TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
open "http://127.0.0.1:$PORT/?token=$TOKEN"
echo "Panel abierto en el navegador. Puedes cerrar esta ventana: el panel sigue corriendo."

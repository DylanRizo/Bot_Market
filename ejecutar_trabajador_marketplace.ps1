$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$worker = Join-Path $scratchDir "marketplace_scheduler_worker.py"
$log = Join-Path $scratchDir "marketplace_scheduler_worker.log"

Set-Location -LiteralPath $scratchDir
# Python escribe avisos en stderr (por ejemplo el FutureWarning de las librerias
# de Google al sincronizar con el SGI). En PowerShell 5.1, con "Stop" y *>>, el
# primer aviso detenia este script y el trabajador moria sin dejar error. La
# redireccion la hace cmd.exe: el log queda en UTF-8 y un aviso ya no corta nada.
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
& cmd.exe /d /c "python.exe `"$worker`" >> `"$log`" 2>&1"
exit $LASTEXITCODE

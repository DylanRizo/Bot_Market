param(
    [int]$Port = 8794
)

$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dashboard = Join-Path $scratchDir "marketplace_bot_dashboard.py"
$tokenFile = Join-Path $scratchDir "marketplace_dashboard_token.txt"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $listener) {
    # El panel escribe su token al arrancar. Se borra el anterior para no abrir
    # el navegador con una llave vieja si el arranque falla.
    if (Test-Path -LiteralPath $tokenFile) { Remove-Item -LiteralPath $tokenFile -Force }
    $arguments = "`"$dashboard`" --port $Port --no-open"
    Start-Process -FilePath python.exe -ArgumentList $arguments -WorkingDirectory $scratchDir -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(15)
    do {
        Start-Sleep -Milliseconds 500
        $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    } while (-not $listener -and (Get-Date) -lt $deadline)
}

if (-not $listener) {
    throw "El panel no pudo iniciar en el puerto $Port."
}

$deadline = (Get-Date).AddSeconds(10)
while (-not (Test-Path -LiteralPath $tokenFile) -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 300
}
if (-not (Test-Path -LiteralPath $tokenFile)) {
    throw "El panel arranco pero no dejo su token en $tokenFile."
}

$token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
$url = "http://127.0.0.1:$Port/?token=$token"

Start-Process $url
Write-Output "Panel abierto: $url"
Write-Output "El enlace lleva la llave de esta sesion. Sin ella el panel responde 'No autorizado'."

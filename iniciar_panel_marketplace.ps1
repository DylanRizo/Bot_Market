param(
    [int]$Port = 8794
)

$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dashboard = Join-Path $scratchDir "marketplace_bot_dashboard.py"
$url = "http://127.0.0.1:$Port/"
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue

if (-not $listener) {
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

Start-Process $url
Write-Output "Panel abierto: $url"

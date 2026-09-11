$ErrorActionPreference = "Stop"

$chrome = Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
if (-not (Test-Path -LiteralPath $chrome)) {
  $chrome = Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"
}
if (-not (Test-Path -LiteralPath $chrome)) {
  throw "No encontre Google Chrome instalado."
}

$profileDir = Join-Path $env:LOCALAPPDATA "BotInvisibleChrome"
New-Item -ItemType Directory -Force -Path $profileDir | Out-Null

$argsList = @(
  "--user-data-dir=`"$profileDir`"",
  "--profile-directory=Default",
  "--remote-debugging-port=9222",
  "--lang=en-US",
  "--no-first-run",
  "--no-default-browser-check",
  "--new-window",
  "https://www.facebook.com/marketplace"
)

Start-Process -FilePath $chrome -ArgumentList ($argsList -join " ")
Write-Host "Chrome abierto con el perfil que usa FiruPost: $profileDir"
Write-Host "Puerto de automatizacion local: 127.0.0.1:9222"
Write-Host "Inicia sesion en Facebook/Marketplace y deja esa sesion abierta."

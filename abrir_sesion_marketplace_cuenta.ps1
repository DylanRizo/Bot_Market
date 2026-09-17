param(
  [string]$Account = "cuenta1",
  [int]$Port = 9222,
  [string]$ProfileDir = ""
)

$ErrorActionPreference = "Stop"

$chrome = Join-Path $env:ProgramFiles "Google\Chrome\Application\chrome.exe"
if (-not (Test-Path -LiteralPath $chrome)) {
  $chrome = Join-Path ${env:ProgramFiles(x86)} "Google\Chrome\Application\chrome.exe"
}
if (-not (Test-Path -LiteralPath $chrome)) {
  throw "No encontre Google Chrome instalado."
}

if ([string]::IsNullOrWhiteSpace($ProfileDir)) {
  $safeAccount = ($Account -replace '[^A-Za-z0-9_-]', '_')
  $ProfileDir = Join-Path $env:LOCALAPPDATA "MarketplaceBot_$safeAccount"
}

New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

$argsList = @(
  "--user-data-dir=`"$ProfileDir`"",
  "--profile-directory=Default",
  "--remote-debugging-port=$Port",
  "--lang=en-US",
  "--no-first-run",
  "--no-default-browser-check",
  # Chrome congela las paginas de ventanas minimizadas o tapadas y los menus de
  # Marketplace dejan de registrar la opcion elegida. El bot publica sin nadie
  # delante, asi que la pagina debe seguir viva en segundo plano.
  "--disable-backgrounding-occluded-windows",
  "--disable-renderer-backgrounding",
  "--disable-background-timer-throttling",
  "--new-window",
  "https://www.facebook.com/marketplace"
)

Start-Process -FilePath $chrome -ArgumentList ($argsList -join " ")
Write-Host "Chrome abierto para cuenta: $Account"
Write-Host "Perfil local: $ProfileDir"
Write-Host "Puerto de automatizacion local: 127.0.0.1:$Port"
Write-Host "Inicia sesion en Facebook/Marketplace y deja esa ventana abierta."

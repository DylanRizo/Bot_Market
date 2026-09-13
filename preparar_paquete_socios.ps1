param(
    [string]$OutputPath = (Join-Path $env:USERPROFILE "Desktop\MarketplaceBot_Instalacion_Individual"),
    [switch]$IncludeCatalog
)

$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$package = [System.IO.Path]::GetFullPath($OutputPath)
$zip = "$package.zip"

if (Test-Path -LiteralPath $package) {
    throw "La carpeta de destino ya existe: $package"
}
if (Test-Path -LiteralPath $zip) {
    throw "El archivo ZIP ya existe: $zip"
}

New-Item -ItemType Directory -Path $package | Out-Null

$files = @(
    "facebook_marketplace_browser_automator.py",
    "marketplace_ai_descriptions.py",
    "marketplace_bot_dashboard.py",
    "marketplace_campaign_runner.py",
    "marketplace_catalog.py",
    "marketplace_custom_products.py",
    "marketplace_listing_builder.py",
    "marketplace_runtime.py",
    "marketplace_scheduler.py",
    "marketplace_scheduler_worker.py",
    "marketplace_storage.py",
    "sgi_client.py",
    "drive_photos.py",
    "sgi_sync.py",
    "marketplace_integrations.example.json",
    "abrir_sesion_marketplace_cuenta.ps1",
    "ejecutar_trabajador_marketplace.ps1",
    "iniciar_panel_marketplace.ps1",
    "instalar_trabajador_marketplace.ps1",
    "requirements_marketplace_bot.txt",
    "GUIA_SOCIOS_MARKETPLACE_BOT.md",
    "README_firupost_automation.md"
)

foreach ($name in $files) {
    $path = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Falta un archivo requerido: $name"
    }
    Copy-Item -LiteralPath $path -Destination $package
}

# El panel carga su interfaz desde static/: sin esta carpeta no abre.
$static = Join-Path $source "static"
if (-not (Test-Path -LiteralPath $static)) {
    throw "Falta la carpeta static con la interfaz del panel."
}
Copy-Item -LiteralPath $static -Destination $package -Recurse

$accounts = @'
{
  "accounts": {
    "cuenta1": {
      "display_name": "Cuenta principal",
      "debugger_address": "127.0.0.1:9222",
      "chrome_profile": "%LOCALAPPDATA%\\MarketplaceBot_cuenta1"
    }
  }
}
'@
Set-Content -LiteralPath (Join-Path $package "marketplace_accounts.json") -Value $accounts -Encoding UTF8

if ($IncludeCatalog) {
    Copy-Item -LiteralPath (Join-Path $source "ArticulosGenerados.xlsx") -Destination $package
    Copy-Item -LiteralPath (Join-Path $source "imagenes_firupost") -Destination $package -Recurse
}

Compress-Archive -LiteralPath $package -DestinationPath $zip -CompressionLevel Optimal

Write-Output "Paquete creado: $zip"
Write-Output "Incluye catalogo: $([bool]$IncludeCatalog)"
Write-Output "No incluye sesiones de Facebook, claves de IA, tokens, base de datos, logs ni historial."

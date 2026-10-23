param(
    [string]$TaskName = "MarketplaceBotScheduler"
)

$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scratchDir "ejecutar_trabajador_marketplace.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
# Al iniciar sesion, y ademas un vigilante cada 10 minutos: si el inicio de sesion
# no lo lanzo (paso el 22 de octubre) o el trabajador se cerro, se vuelve a
# levantar solo. Si ya esta corriendo, Windows ignora el disparo (IgnoreNew).
$atLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10)
# Sin limite de tiempo: por defecto Windows detiene la tarea a las 72 horas.
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atLogon, $watchdog) -Settings $settings -Description "Ejecuta el calendario autonomo de Facebook Marketplace." -Force | Out-Null

Write-Output "Tarea instalada: $TaskName"
Write-Output "El bot permanece inactivo hasta activar Bot activo en el panel."

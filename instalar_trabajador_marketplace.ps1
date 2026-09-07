param(
    [string]$TaskName = "MarketplaceBotScheduler"
)

$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$launcher = Join-Path $scratchDir "ejecutar_trabajador_marketplace.ps1"

$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$launcher`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Description "Ejecuta el calendario autonomo de Facebook Marketplace." -Force | Out-Null

Write-Output "Tarea instalada: $TaskName"
Write-Output "El bot permanece inactivo hasta activar Bot activo en el panel."

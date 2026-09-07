$ErrorActionPreference = "Stop"
$scratchDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$worker = Join-Path $scratchDir "marketplace_scheduler_worker.py"
$log = Join-Path $scratchDir "marketplace_scheduler_worker.log"

Set-Location -LiteralPath $scratchDir
& python.exe $worker *>> $log

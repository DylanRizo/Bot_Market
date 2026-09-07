param(
  [string]$Category = "Men's clothing & shoes",
  [string]$Condition = "New"
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$automator = Join-Path $scriptDir "facebook_marketplace_browser_automator.py"

python $automator `
  --debugger-address "127.0.0.1:9222" `
  --category $Category `
  --condition $Condition `
  --inspect-categories

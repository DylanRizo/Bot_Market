param(
  [string]$AccountName = "Cuenta principal",
  [string]$Email = ""
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$automator = Join-Path $scriptDir "firupost_ui_automator.py"

$argsList = @(
  $automator,
  "--mode", "individual",
  "--reuse-session",
  "--account-name", $AccountName,
  "--test-batch",
  "--test-layout", "direct",
  "--test-limit", "1",
  "--test-condition", "New",
  "--meet-public",
  "--door-pickup",
  "--door-delivery",
  "--start"
)

if ($Email.Trim()) {
  $argsList += @("--email", $Email.Trim())
}

python @argsList

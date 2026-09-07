param(
  [string]$Category = "Men's clothing & shoes",
  [string]$Condition = "New",
  [bool]$MeetPublic = $true,
  [bool]$DoorPickup = $true,
  [bool]$DoorDropoff = $true,
  [switch]$ManualCategory,
  [switch]$Publish,
  [switch]$ConfirmPublish
)

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$automator = Join-Path $scriptDir "facebook_marketplace_browser_automator.py"

$argsList = @(
  $automator,
  "--debugger-address", "127.0.0.1:9222",
  "--category", $Category,
  "--condition", $Condition
)

if ($ManualCategory) {
  $argsList += "--manual-category"
}
if ($MeetPublic) {
  $argsList += "--meet-public"
}
if ($DoorPickup) {
  $argsList += "--door-pickup"
}
if ($DoorDropoff) {
  $argsList += "--door-dropoff"
}
if ($Publish) {
  $argsList += "--publish"
}
if ($ConfirmPublish) {
  $argsList += "--confirm-publish"
}

python @argsList

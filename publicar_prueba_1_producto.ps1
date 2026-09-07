$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$automator = Join-Path $scriptDir "firupost_ui_automator.py"

python $automator `
  --mode individual `
  --test-batch `
  --test-layout direct `
  --test-limit 1 `
  --test-category Apparel `
  --test-condition New `
  --ask-credentials `
  --start

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$automator = Join-Path $scriptDir "firupost_automator.py"
$aiWrapper = Join-Path $scriptDir "firupost_ai_description_openai_compatible.py"

if (-not $env:FIRUPOST_AI_MODEL) {
  throw "Configura FIRUPOST_AI_MODEL antes de ejecutar este script."
}
if (-not ($env:FIRUPOST_AI_API_KEY -or $env:OPENAI_API_KEY)) {
  throw "Configura FIRUPOST_AI_API_KEY u OPENAI_API_KEY antes de ejecutar este script."
}

$env:FIRUPOST_AI_DESCRIPTION_COMMAND = "python `"$aiWrapper`""

python $automator --source sync --category Apparel

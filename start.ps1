$env:PYTHONUTF8 = '1'
$host.UI.RawUI.WindowTitle = 'FreePalp - AI Orchestrator'
Set-Location $PSScriptRoot

if (-not (Test-Path '.env')) {
    python first_run.py
    if ($LASTEXITCODE -ne 0) { Read-Host 'Setup failed. Press Enter'; exit 1 }
}

python -m freepalp.app $args

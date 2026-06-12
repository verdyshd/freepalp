$env:PYTHONUTF8 = '1'
$host.UI.RawUI.WindowTitle = 'OCTO - Octopus AI Orchestrator'
Set-Location $PSScriptRoot

if (-not (Test-Path '.env')) {
    python setup.py
    if ($LASTEXITCODE -ne 0) { Read-Host 'Setup failed. Press Enter'; exit 1 }
}

python octo\app.py $args

$env:PYTHONUTF8 = '1'
$host.UI.RawUI.WindowTitle = 'FreePalp - AI Orchestrator'
Set-Location $PSScriptRoot

if ((-not (Test-Path '.env')) -and (Test-Path '.env.example')) {
    Copy-Item '.env.example' '.env'
    Write-Host '[INFO] .env created from .env.example - add API keys in the Providers tab (optional, works on local Ollama without keys).'
}

python -m freepalp.app $args

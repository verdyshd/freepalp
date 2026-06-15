#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ] && [ -f .env.example ]; then
    cp .env.example .env
    echo "[INFO] .env created from .env.example — add API keys in the Providers tab (optional, works on local Ollama without keys)."
fi

export PYTHONUTF8=1
python3 -m freepalp.app "$@"

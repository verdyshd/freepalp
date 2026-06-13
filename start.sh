#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    python3 first_run.py
fi

export PYTHONUTF8=1
python3 -m freepalp.app "$@"

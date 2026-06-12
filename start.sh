#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -f .env ]; then
    python3 setup.py
fi

export PYTHONUTF8=1
python3 octo/app.py "$@"

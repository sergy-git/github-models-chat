#!/usr/bin/env bash
# Run the model selector from the project venv. Usage: ./select_model.sh [agents.json]
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ ! -x .venv/bin/python ]]; then
    echo "missing .venv — create it with: python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
exec .venv/bin/python select_model.py "$@"

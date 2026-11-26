#!/bin/bash
# Double-click to start MyTV. Can be added to Login Items; waits for an external drive to be ready.
cd "$(dirname "$0")" || exit 1
[[ -x .venv/bin/python ]] && .venv/bin/python -c "" 2>/dev/null || scripts/setup.sh
scripts/start.sh

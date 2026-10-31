#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
cd "$ROOT_DIR"
./scripts/test.sh
PYTHONPATH="$ROOT_DIR/src/flowcausal/src/controller${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m unittest discover -s src/flowcausal/src/controller -p 'test_*.py'
python3 tests/verify_results.py

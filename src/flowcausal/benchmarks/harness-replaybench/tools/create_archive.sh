#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
NAME="$(basename "$ROOT")"
VERSION="$(tr -d '\n' < "$ROOT/VERSION")"
OUT="${1:-$PARENT/HarnessReplayBench-v${VERSION}.tar.gz}"

python3 "$ROOT/tools/validate_package.py"
tar -C "$PARENT" --exclude='*.pyc' --exclude='__pycache__' -czf "$OUT" "$NAME"
echo "$OUT"

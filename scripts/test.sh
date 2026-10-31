#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$ROOT_DIR/src/agent${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONDONTWRITEBYTECODE=1

tests=()
while IFS= read -r -d '' test_file; do
  tests+=("$test_file")
done < <(find "$ROOT_DIR/src/agent/tests" -maxdepth 1 -type f -name 'test_*.py' -print0 | sort -z)

for test_file in "${tests[@]}"; do
  printf 'TEST %s\n' "${test_file#"$ROOT_DIR/"}"
  python3 "$test_file"
done

printf 'All %d smoke tests passed.\n' "${#tests[@]}"

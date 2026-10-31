#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ROOT_ARGS=()
if [[ -n "${ROOT:-}" ]]; then
  ROOT_ARGS=(--root "$ROOT")
fi

python3 test8-futex-mutex/scripts/run_mutex_monitor.py \
  "${ROOT_ARGS[@]}" \
  --phase run \
  --schedulers "default scx_simple scx_lcas scx_cfl scx_lockboost scx_lockcoop scx_lockcoop scx_eevdf" \
  --scenarios "mutex_1lock_cs0 mutex_1lock_cs1us mutex_8lock_cs1us" \
  --threads "1 4 16 32 64 128 256" \
  --runs 3 \
  --duration 10 \
  --start-delay-ms 1000

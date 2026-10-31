#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

ROOT_ARGS=()
if [[ -n "${ROOT:-}" ]]; then
  ROOT_ARGS=(--root "$ROOT")
fi

python3 test15-owner-progress-microbench/scripts/run_owner_progress_microbench.py \
  "${ROOT_ARGS[@]}" \
  --phase run \
  --schedulers "default scx_simple scx_lcas scx_cfl scx_lockboost scx_lockcoop scx_eevdf" \
  --threads "64 128 256" \
  --runs 5 \
  --mode convoy \
  --duration 20 \
  --critical-us 100 \
  --outside-us 0 \
  --owner-yields 0 \
  --locks 8 \
  --groups 8 \
  --hog-threads 0 \
  --pin-cpus 0

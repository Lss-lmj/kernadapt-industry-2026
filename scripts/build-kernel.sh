#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${KERNEL_SRC:?Set KERNEL_SRC to a dedicated compatible Linux source checkout}"
SCX_DIR="$KERNEL_SRC/tools/sched_ext"
[[ -f "$SCX_DIR/Makefile" ]] || { echo 'Missing tools/sched_ext/Makefile' >&2; exit 2; }
[[ -d "$KERNEL_SRC/tools/lib/bpf" ]] || { echo 'A full Linux source tree is required' >&2; exit 2; }
for name in scx_lockboost scx_locknest scx_lockcoop; do
  cp "$ROOT_DIR/src/lockcoop/src/sched_ext/$name.c" "$SCX_DIR/"
  cp "$ROOT_DIR/src/lockcoop/src/sched_ext/$name.bpf.c" "$SCX_DIR/"
done
mkdir -p "$SCX_DIR/include/scx"
cp "$ROOT_DIR/src/lockcoop/src/sched_ext/include/scx/lock_observe.bpf.h" "$SCX_DIR/include/scx/"
cp "$ROOT_DIR/src/lockcoop/src/sched_ext/include/scx/lockcoop_user_sync.h" "$SCX_DIR/include/scx/"
cp "$ROOT_DIR/src/lockcoop/src/sched_ext/lockcoop_user_sync.c" "$SCX_DIR/"
cp "$ROOT_DIR/src/flowcausal/src/bpf/scx_flowcausal.bpf.c" "$SCX_DIR/"
cp "$ROOT_DIR/src/flowcausal/src/user/scx_flowcausal.c" "$SCX_DIR/"
cp "$ROOT_DIR/src/flowcausal/src/include/flowcausal.h" "$SCX_DIR/include/"
make -C "$SCX_DIR" -j "${JOBS:-2}" \
  'c-sched-targets=scx_simple scx_lockboost scx_locknest scx_lockcoop scx_flowcausal' \
  scx_simple scx_lockboost scx_locknest scx_lockcoop scx_flowcausal
"${HOSTCC:-cc}" -O2 -g -fPIC -shared -Wall -Werror -I"$SCX_DIR/include" \
  "$SCX_DIR/lockcoop_user_sync.c" -o "$SCX_DIR/build/bin/liblockcoop_sync.so"
make -C "$ROOT_DIR/src/flowcausal/src" KERNEL_SRC="$KERNEL_SRC" \
  TMPDIR="$SCX_DIR/build/bin" check-flowcausal-v2
printf 'Built schedulers under %s/build/bin; no scheduler was loaded.\n' "$SCX_DIR"

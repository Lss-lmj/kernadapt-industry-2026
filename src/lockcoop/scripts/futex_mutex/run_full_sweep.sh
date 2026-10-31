#!/usr/bin/env bash
set -euo pipefail

# test8: futex / pthread_mutex handoff microbenchmark.
# - No CPU/NUMA pinning: let the system freely migrate tasks.
# - Optional auto attach/detach scx schedulers via sudo.
# - Restart scheduler for every benchmark run to reduce state carry-over.
#
# Example:
#   TAG=retest_futex RUNS=3 DURATION=10 THREADS_LIST='32 64 128 256' \
#   SCHEDS='default scx_simple scx_lockboost scx_lockcoop scx_hybrid scx_lcas scx_cfl' \
#   ./test8-futex-mutex/scripts/run_full_sweep.sh

log() { echo "[$(date +%H:%M:%S)] $*"; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEST_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd -- "${TEST_DIR}/.." && pwd)"

TAG="${TAG:-retest_futex}"
RUNS="${RUNS:-3}"
DURATION="${DURATION:-10}"
THREADS_LIST="${THREADS_LIST:-32 64 128 256}"
SCENARIOS="${SCENARIOS:-mutex_1lock_cs0 mutex_1lock_cs1us mutex_8lock_cs1us futex_ring_cs0 futex_ring_cs1us}"
SCHEDS="${SCHEDS:-default scx_simple scx_lockboost scx_lockcoop scx_hybrid scx_lcas scx_cfl}"
NO_BUILD="${NO_BUILD:-0}"

SCX_BIN_DIR="${REPO_ROOT}/lock-sched/tools/sched_ext/build/bin"
SUDO_KEEPALIVE_PID=""

scx_state() {
  if [[ -r /sys/kernel/sched_ext/state ]]; then
    cat /sys/kernel/sched_ext/state
  else
    echo "unknown"
  fi
}

wait_scx_disabled() {
  local i
  for i in $(seq 1 50); do
    if [[ "$(scx_state)" == "disabled" ]]; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

wait_scx_enabled() {
  local i
  for i in $(seq 1 50); do
    if [[ "$(scx_state)" == "enabled" ]]; then
      return 0
    fi
    sleep 0.1
  done
  return 1
}

needs_sudo() {
  local sched
  for sched in $SCHEDS; do
    if [[ "$sched" != "default" ]]; then
      return 0
    fi
  done
  return 1
}

start_sudo_keepalive() {
  if ! needs_sudo; then
    return 0
  fi
  log "Requesting sudo once for sched_ext attach/stop ..."
  sudo -n true
  (
    while true; do
      sudo -n true || exit 0
      sleep 30
    done
  ) &
  SUDO_KEEPALIVE_PID="$!"
}

stop_sudo_keepalive() {
  if [[ -n "${SUDO_KEEPALIVE_PID:-}" ]]; then
    kill "$SUDO_KEEPALIVE_PID" >/dev/null 2>&1 || true
    wait "$SUDO_KEEPALIVE_PID" 2>/dev/null || true
    SUDO_KEEPALIVE_PID=""
  fi
}

stop_scx() {
  local pidfile="$1"
  if [[ -z "$pidfile" || ! -f "$pidfile" ]]; then
    return 0
  fi

  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -n "$pid" ]]; then
    sudo kill "$pid" 2>/dev/null || true
    local i
    for i in $(seq 1 50); do
      if ! ps -p "$pid" >/dev/null 2>&1; then
        break
      fi
      sleep 0.1
    done
    sudo kill -9 "$pid" 2>/dev/null || true
  fi
  wait_scx_disabled || return 1
  sudo pkill -TERM -f "${SCX_BIN_DIR}/scx_locknest_v8" 2>/dev/null || true
  sudo pkill -TERM -f "${SCX_BIN_DIR}/scx_locknest_v9" 2>/dev/null || true
  sudo pkill -TERM -f "${SCX_BIN_DIR}/scx_lockboost2" 2>/dev/null || true
  wait_scx_disabled
}

start_scx() {
  local sched="$1"
  local logfile="$2"
  local pidfile="$3"

  if [[ "$sched" == "default" ]]; then
    return 0
  fi

  local bin="${SCX_BIN_DIR}/${sched}"
  local args=()
  case "$sched" in
    scx_locknest_v8_weak)
      bin="${SCX_BIN_DIR}/scx_locknest_v8"
      args=(-F weak)
      ;;
    scx_locknest_v8_current)
      bin="${SCX_BIN_DIR}/scx_locknest_v8"
      args=(-F current)
      ;;
    scx_locknest_v8_mutexonly)
      bin="${SCX_BIN_DIR}/scx_locknest_v8"
      args=(-F mutexonly)
      ;;
    scx_locknest_v8_futexonly)
      bin="${SCX_BIN_DIR}/scx_locknest_v8"
      args=(-F futexonly)
      ;;
    scx_locknest_v9_weak)
      bin="${SCX_BIN_DIR}/scx_locknest_v9"
      args=(-F weak)
      ;;
    scx_locknest_v9_current)
      bin="${SCX_BIN_DIR}/scx_locknest_v9"
      args=(-F current)
      ;;
    scx_locknest_v9_mutexonly)
      bin="${SCX_BIN_DIR}/scx_locknest_v9"
      args=(-F mutexonly)
      ;;
	    scx_locknest_v9_futexonly)
	      bin="${SCX_BIN_DIR}/scx_locknest_v9"
	      args=(-F futexonly)
	      ;;
	    scx_lockboost2_off)
	      bin="${SCX_BIN_DIR}/scx_lockboost2"
	      args=(-F off)
	      ;;
	    scx_lockboost2_weak)
	      bin="${SCX_BIN_DIR}/scx_lockboost2"
	      args=(-F weak)
	      ;;
	    scx_lockboost2_equal)
	      bin="${SCX_BIN_DIR}/scx_lockboost2"
	      args=(-F equal)
	      ;;
	    scx_lockboost5_*|scx_lockboost6_*)
	      if [[ "$sched" == scx_lockboost6_* ]]; then
	        bin="${SCX_BIN_DIR}/scx_lockboost6"
	      else
	        bin="${SCX_BIN_DIR}/scx_lockboost5"
	      fi
	      local policy="ownerweak"
	      local owner_slice="4"
	      local owner_inf="8"
	      local tail_ms="5"
	      if [[ "$sched" == scx_lockboost6_* ]]; then
	        tail_ms="0"
	      fi
	      local age_cap="50"
	      local slice_score="16"
	      local inf_score="64"
	      local hard_ms="0"
	      local mutex_tail_ms="0"
	      local mutex_age_cap="50"
	      local mutex_slice_score="16"
	      local mutex_inf_score="64"
	      local mutex_hard_ms="0"
	      local token
	      IFS="_" read -r -a sched_tokens <<< "$sched"
	      for token in "${sched_tokens[@]:2}"; do
	        case "$token" in
	          off|weak|equal|owner|ownerweak)
	            policy="$token"
	            ;;
	          countonly|count_only)
	            tail_ms="0"
	            ;;
	          timeonly|time_only)
	            owner_slice="1024"
	            owner_inf="0"
	            ;;
	          T[0-9]*)
	            tail_ms="${token#T}"
	            ;;
	          B[0-9]*)
	            age_cap="${token#B}"
	            ;;
	          s[0-9]*)
	            slice_score="${token#s}"
	            ;;
	          I[0-9]*)
	            inf_score="${token#I}"
	            ;;
	          H[0-9]*)
	            hard_ms="${token#H}"
	            ;;
	          m[0-9]*)
	            mutex_tail_ms="${token#m}"
	            ;;
	          R[0-9]*)
	            mutex_age_cap="${token#R}"
	            ;;
	          S[0-9]*)
	            mutex_slice_score="${token#S}"
	            ;;
	          P[0-9]*)
	            mutex_inf_score="${token#P}"
	            ;;
	          G[0-9]*)
	            mutex_hard_ms="${token#G}"
	            ;;
	          a[0-9]*)
	            owner_slice="${token#a}"
	            ;;
	          i[0-9]*)
	            owner_inf="${token#i}"
	            ;;
	          esac
	      done
	      args=(-F "$policy" -a "$owner_slice" -A "$owner_inf" -T "$tail_ms" -B "$age_cap" -s "$slice_score" -I "$inf_score" -H "$hard_ms")
	      if [[ "$sched" == scx_lockboost6_* ]]; then
	        args+=(-m "$mutex_tail_ms" -R "$mutex_age_cap" -S "$mutex_slice_score" -P "$mutex_inf_score" -G "$mutex_hard_ms")
	      fi
	      ;;
	  esac
  if [[ ! -x "$bin" ]]; then
    echo "error: scx binary not found/executable: $bin" >&2
    echo "hint: build sched_ext first: (cd lock-sched/tools/sched_ext && make)" >&2
    exit 1
  fi

  sudo bash -c 'log="$1"; pid="$2"; shift 2; nohup "$@" > "$log" 2>&1 & echo $! > "$pid"' _ "$logfile" "$pidfile" "$bin" "${args[@]}"

  local pid
  pid="$(cat "$pidfile" 2>/dev/null || true)"
  if [[ -z "$pid" ]] || ! wait_scx_enabled || ! ps -p "$pid" >/dev/null 2>&1; then
    echo "error: ${sched} failed to reach enabled state with a live loader" >&2
    tail -n 30 "$logfile" >&2 || true
    stop_scx "$pidfile" || true
    return 1
  fi
}

scenario_args() {
  local scenario="$1"
  case "$scenario" in
    mutex_1lock_cs0)
      echo "--mode mutex --locks 1 --critical-ns 0 --outside-ns 0"
      ;;
    mutex_1lock_cs1us)
      echo "--mode mutex --locks 1 --critical-ns 1000 --outside-ns 0"
      ;;
    mutex_8lock_cs1us)
      echo "--mode mutex --locks 8 --critical-ns 1000 --outside-ns 0"
      ;;
    mutex_1lock_cs10us)
      echo "--mode mutex --locks 1 --critical-ns 10000 --outside-ns 0"
      ;;
    futex_ring_cs0)
      echo "--mode futex_ring --critical-ns 0 --outside-ns 0"
      ;;
    futex_ring_cs1us)
      echo "--mode futex_ring --critical-ns 1000 --outside-ns 0"
      ;;
    futex_ring_cs10us)
      echo "--mode futex_ring --critical-ns 10000 --outside-ns 0"
      ;;
    *)
      echo "error: unknown scenario: $scenario" >&2
      exit 1
      ;;
  esac
}

main() {
  if [[ "$NO_BUILD" != "1" ]]; then
    log "Building futex/mutex benchmark: make -C ${TEST_DIR}"
    make -C "$TEST_DIR"
  fi

  local result_root="${TEST_DIR}/result/${TAG}"
  mkdir -p "$result_root"

  log "Start test8 futex/mutex sweep. TAG=${TAG} RUNS=${RUNS} DURATION=${DURATION}"
  log "THREADS_LIST='${THREADS_LIST}'"
  log "SCENARIOS='${SCENARIOS}'"
  log "SCHEDS='${SCHEDS}'"
  log "Result root: ${result_root}"

  local scx_pidfile=""
  local threads scenario sched run_idx

  cleanup_main() {
    stop_scx "${scx_pidfile:-}" || true
    stop_sudo_keepalive
  }

  start_sudo_keepalive
  trap cleanup_main EXIT

  if ! wait_scx_disabled; then
    echo "error: sched_ext is not disabled before the sweep" >&2
    exit 1
  fi

  for threads in $THREADS_LIST; do
    for scenario in $SCENARIOS; do
      local args
      args="$(scenario_args "$scenario")"

      for run_idx in $(seq 1 "$RUNS"); do
        for sched in $SCHEDS; do
          local outdir="${result_root}/${sched}_t${threads}"
          mkdir -p "$outdir"

          log "Run ${run_idx}/${RUNS} (${TAG}_${sched}_t${threads}_${scenario})"

          stop_scx "$scx_pidfile"
          scx_pidfile=""

          if [[ "$sched" != "default" ]]; then
            log "Attach ${sched}"
            scx_pidfile="${outdir}/scx.${scenario}.r${run_idx}.pid"
            start_scx "$sched" "${outdir}/scx.${scenario}.r${run_idx}.log" "$scx_pidfile"
            sleep 1
          else
            if ! wait_scx_disabled; then
              echo "error: default trial started while sched_ext state=$(scx_state)" >&2
              exit 1
            fi
          fi

          {
            echo "# date: $(date -Is)"
            echo "# tag: ${TAG}"
            echo "# scheduler: ${sched}"
            echo "# scenario: ${scenario}"
            echo "# threads: ${threads}"
            echo "# duration_s: ${DURATION}"
            echo "# run: ${run_idx}/${RUNS}"
            echo "# kernel: $(uname -r)"
            echo "# scx_bin_dir: ${SCX_BIN_DIR}"
            echo "# sched_ext_state: $(scx_state)"
            echo "# sched_ext_nr_rejected: $(cat /sys/kernel/sched_ext/nr_rejected 2>/dev/null || echo unknown)"
            if [[ "$sched" != "default" ]]; then
              echo "# scheduler_sha256: $(sha256sum "${SCX_BIN_DIR}/${sched}" | awk '{print $1}')"
            fi
            echo "# cmd: ${TEST_DIR}/futex_mutex_bench --threads ${threads} --duration ${DURATION} ${args}"
            echo
            # shellcheck disable=SC2086
            "${TEST_DIR}/futex_mutex_bench" --threads "${threads}" --duration "${DURATION}" ${args}
          } >"${outdir}/${scenario}.r${run_idx}.txt" 2>&1

          if [[ "$sched" != "default" && "$(scx_state)" != "enabled" ]]; then
            echo "error: ${sched} left enabled state during ${scenario} t${threads} r${run_idx}" >&2
            tail -n 30 "${outdir}/scx.${scenario}.r${run_idx}.log" >&2 || true
            exit 1
          fi

          stop_scx "$scx_pidfile"
          scx_pidfile=""
        done
      done
    done
  done

  log "Done. Results under: ${TEST_DIR}/result/"
  log "Tip: summarize & plot:"
  log "  python3 ${TEST_DIR}/scripts/summarize_results.py --tag ${TAG} --expected-runs ${RUNS}"
  log "  python3 ${TEST_DIR}/scripts/plot_results_png.py --tag ${TAG} --mode both"
}

main "$@"

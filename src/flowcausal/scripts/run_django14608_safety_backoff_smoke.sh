#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
cd "$ROOT"

PY=${FLOWCAUSAL_REAL_AGENT_PY:-"$ROOT/experiments/results/real-agent-harness/.venv/bin/python"}
TRIALS=${FLOWCAUSAL_TRIALS:-1}
LABEL_SUFFIX=${FLOWCAUSAL_LABEL_SUFFIX:-"valid"}
OUT=${FLOWCAUSAL_OUT_DIR:-"$ROOT/experiments/results/real-agent-captures/smoke-django14608-safety-backoff-${LABEL_SUFFIX}-${TRIALS}trial"}

FRONTIER_DISPATCH_QUOTA=${FLOWCAUSAL_FRONTIER_DISPATCH_QUOTA:-2}
GUARD_COOLDOWN_DISPATCHES=${FLOWCAUSAL_GUARD_COOLDOWN_DISPATCHES:-2}
SHARED_WAIT_PRESSURE_US=${FLOWCAUSAL_SHARED_WAIT_PRESSURE_US:-300}
PRESSURE_FALLBACK_ENQUEUES=${FLOWCAUSAL_PRESSURE_FALLBACK_ENQUEUES:-8}
PRESSURE_FALLBACK_MS=${FLOWCAUSAL_PRESSURE_FALLBACK_MS:-25}
PRESSURE_DISPATCH_MS=${FLOWCAUSAL_PRESSURE_DISPATCH_MS:-25}
FRONTIER_DISPATCH_WINDOW_MS=${FLOWCAUSAL_FRONTIER_DISPATCH_WINDOW_MS:-10}
FRONTIER_DISPATCH_WINDOW_BUDGET=${FLOWCAUSAL_FRONTIER_DISPATCH_WINDOW_BUDGET:-1}
SHARED_SLICE_US=${FLOWCAUSAL_SHARED_SLICE_US:-0}
SHARED_WAKEUP_PREEMPT=${FLOWCAUSAL_SHARED_WAKEUP_PREEMPT:-0}
SHARED_WAKEUP_SLICE_US=${FLOWCAUSAL_SHARED_WAKEUP_SLICE_US:-0}
SHARED_WAKEUP_MIN_BLOCK_US=${FLOWCAUSAL_SHARED_WAKEUP_MIN_BLOCK_US:-0}
INCLUDE_STATIC_BASELINES=${FLOWCAUSAL_INCLUDE_STATIC_BASELINES:-0}
INCLUDE_ORACLE_BASELINE=${FLOWCAUSAL_INCLUDE_ORACLE_BASELINE:-0}
STATIC_CGROUP_SUDO=${FLOWCAUSAL_STATIC_CGROUP_SUDO:-0}
FRONTIER_LABEL_DESCENDANTS=${FLOWCAUSAL_FRONTIER_LABEL_DESCENDANTS:-$INCLUDE_ORACLE_BASELINE}
FRONTIER_LABEL_FOLLOW_MS=${FLOWCAUSAL_FRONTIER_LABEL_FOLLOW_MS:-300}
FRONTIER_LABEL_FOLLOW_INTERVAL_MS=${FLOWCAUSAL_FRONTIER_LABEL_FOLLOW_INTERVAL_MS:-10}

EXTRA_ARGS=()
if [[ "$SHARED_WAKEUP_PREEMPT" == "1" || "$SHARED_WAKEUP_PREEMPT" == "true" ]]; then
  EXTRA_ARGS+=(--shared-wakeup-preempt)
fi
if [[ "$INCLUDE_STATIC_BASELINES" == "1" || "$INCLUDE_STATIC_BASELINES" == "true" ]]; then
  EXTRA_ARGS+=(--include-static-baselines)
fi
if [[ "$INCLUDE_ORACLE_BASELINE" == "1" || "$INCLUDE_ORACLE_BASELINE" == "true" ]]; then
  EXTRA_ARGS+=(--include-oracle-baseline)
fi
if [[ "$STATIC_CGROUP_SUDO" == "1" || "$STATIC_CGROUP_SUDO" == "true" ]]; then
  EXTRA_ARGS+=(--static-cgroup-sudo)
fi

WORKLOAD_EXTRA_ARGS=()
if [[ "$FRONTIER_LABEL_DESCENDANTS" == "1" || "$FRONTIER_LABEL_DESCENDANTS" == "true" ]]; then
  WORKLOAD_EXTRA_ARGS+=(
    --frontier-label-descendants
    --frontier-label-follow-ms "$FRONTIER_LABEL_FOLLOW_MS"
    --frontier-label-follow-interval-ms "$FRONTIER_LABEL_FOLLOW_INTERVAL_MS"
  )
fi

CMD="$PY $ROOT/experiments/workloads/repo_stage_workflow.py \
--repo-dir $ROOT/benchmarks/harness-replaybench/repos/django__django-14608 \
--work-dir /tmp/flowcausal-django-safety-valid-\$FLOWCAUSAL_VARIANT-\$FLOWCAUSAL_TRIAL \
--overwrite \
--env PYTHONPATH=. \
--inspect-cmd \"$PY -c 'from pathlib import Path; [print(p, Path(p).exists(), Path(p).stat().st_size if Path(p).exists() else 0) for p in [\\\"django/forms/formsets.py\\\", \\\"tests/forms_tests/tests/test_formsets.py\\\"]]'\" \
--build-cmd \"$PY -m compileall -q django/forms tests/forms_tests\" \
--test-cmd \"$PY tests/runtests.py forms_tests.tests.test_formsets.FormsFormsetTestCase.test_non_form_errors --verbosity 1\" \
--critical-after TEST ${WORKLOAD_EXTRA_ARGS[*]}"

python3 experiments/run_repo_ci_matrix.py \
  --out-dir "$OUT" \
  --overwrite \
  --label "django14608-safety-backoff-${LABEL_SUFFIX}-${TRIALS}trial" \
  --command "$CMD" \
  --trials "$TRIALS" \
  --taskset 0-1 \
  --stage-kind AUTO \
  --frontier-width 2 \
  --max-frontier-width 1 \
  --min-frontier-score 50 \
  --frontier-propagation \
  --frontier-propagation-depth 2 \
  --stage-aware-frontier-propagation-depth \
  --frontier-action advance \
  --build-frontier-admission tail-only \
  --confidence 75 \
  --ttl-ms 1000 \
  --policy-window-ms 100 \
  --boost-budget-ms 100 \
  --max-vtime-boost-us 500 \
  --interval-ms 50 \
  --characterization-interval-ms 50 \
  --frontier-dispatch-quota "$FRONTIER_DISPATCH_QUOTA" \
  --guard-cooldown-dispatches "$GUARD_COOLDOWN_DISPATCHES" \
  --shared-wait-pressure-us "$SHARED_WAIT_PRESSURE_US" \
  --pressure-fallback-enqueues "$PRESSURE_FALLBACK_ENQUEUES" \
  --pressure-fallback-ms "$PRESSURE_FALLBACK_MS" \
  --pressure-dispatch-ms "$PRESSURE_DISPATCH_MS" \
  --frontier-dispatch-window-ms "$FRONTIER_DISPATCH_WINDOW_MS" \
  --frontier-dispatch-window-budget "$FRONTIER_DISPATCH_WINDOW_BUDGET" \
  --shared-slice-us "$SHARED_SLICE_US" \
  --shared-wakeup-slice-us "$SHARED_WAKEUP_SLICE_US" \
  --shared-wakeup-min-block-us "$SHARED_WAKEUP_MIN_BLOCK_US" \
  --victim-command "python3 $ROOT/experiments/workloads/cpu_victim.py --duration-sec 5 --period-ms 10 --work-us 500" \
  --victim-taskset 0-1 \
  --victim-warmup-sec 0.2 \
  --no-event-flowmon \
  --skip-no-width-variant \
  "${EXTRA_ARGS[@]}"

python3 - <<'PY' "$OUT/matrix-summary.json"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
print(f"summary={path}")
print(f"level={data['claim_gate_audit']['highest_supported_level']}")
for variant in data["variants"]:
    aggregate = variant["aggregate"]
    victim = aggregate.get("victim_mean", {})
    flowmon = aggregate.get("flowmon_mean", {})
    status = aggregate.get("trial_status", {})
    oracle_rcs = [
        trial.get("manifest", {}).get("oracle_frontier_driver_returncode")
        for trial in variant.get("trials", [])
        if trial.get("manifest", {}).get("oracle_frontier_driver_returncode") is not None
    ]
    print(
        variant["name"],
        f"ok/fail={int(status.get('successful', 0))}/{int(status.get('failed', 0))}",
        f"critical={aggregate.get('critical_mean_sec', 0):.3f}s",
        f"workload_elapsed={aggregate.get('workload_elapsed_mean_sec', 0):.3f}s",
        f"harness_total={aggregate.get('total_mean_sec', 0):.3f}s",
        f"labels={aggregate.get('workload_frontier_label_count_mean', 0):.1f}",
        f"victim_p99/max={victim.get('lateness_p99_ms', 0):.3f}/{victim.get('lateness_max_ms', 0):.3f}ms",
    )
    if oracle_rcs:
        print(" ", f"oracle_driver_rcs={oracle_rcs}")
    if flowmon:
        print(
            " ",
            f"frontier_enqueues={flowmon.get('frontier_enqueues', 0)}",
            f"fallback_enqueues={flowmon.get('fallback_enqueues', 0)}",
            f"pressure_dispatches={flowmon.get('global_pressure_dispatches', 0)}",
            f"frontier_budget_trips={flowmon.get('global_frontier_budget_trips', 0)}",
        )
for gate in data["claim_gate_audit"]["gates"]:
    blockers = "; ".join(gate.get("blockers", [])[:2])
    print(f"gate {gate['name']}: {gate['status']} {blockers}")
PY

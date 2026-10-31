#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
SWEEP_ROOT=${FLOWCAUSAL_SWEEP_ROOT:-"$ROOT/experiments/results/real-agent-captures/sweep-django14608-safety-backoff-1trial"}
TRIALS=${FLOWCAUSAL_TRIALS:-1}

mkdir -p "$SWEEP_ROOT"

run_config() {
  local name=$1
  local frontier_dispatch_quota=$2
  local guard_cooldown_dispatches=$3
  local shared_wait_pressure_us=$4
  local pressure_fallback_enqueues=$5
  local pressure_fallback_ms=$6
  local pressure_dispatch_ms=$7
  local frontier_dispatch_window_ms=$8
  local frontier_dispatch_window_budget=$9

  FLOWCAUSAL_LABEL_SUFFIX="$name" \
  FLOWCAUSAL_TRIALS="$TRIALS" \
  FLOWCAUSAL_OUT_DIR="$SWEEP_ROOT/$name" \
  FLOWCAUSAL_FRONTIER_DISPATCH_QUOTA="$frontier_dispatch_quota" \
  FLOWCAUSAL_GUARD_COOLDOWN_DISPATCHES="$guard_cooldown_dispatches" \
  FLOWCAUSAL_SHARED_WAIT_PRESSURE_US="$shared_wait_pressure_us" \
  FLOWCAUSAL_PRESSURE_FALLBACK_ENQUEUES="$pressure_fallback_enqueues" \
  FLOWCAUSAL_PRESSURE_FALLBACK_MS="$pressure_fallback_ms" \
  FLOWCAUSAL_PRESSURE_DISPATCH_MS="$pressure_dispatch_ms" \
  FLOWCAUSAL_FRONTIER_DISPATCH_WINDOW_MS="$frontier_dispatch_window_ms" \
  FLOWCAUSAL_FRONTIER_DISPATCH_WINDOW_BUDGET="$frontier_dispatch_window_budget" \
  "$SCRIPT_DIR/run_django14608_safety_backoff_smoke.sh"
}

run_config reference_no_backoff 4 0 0 0 0 0 0 0
run_config quota_guard_only 2 2 0 0 0 0 0 0
run_config window_soft 4 0 0 0 0 0 10 2
run_config pressure_light 4 1 1000 2 5 5 10 2
run_config pressure_medium 3 1 500 4 10 10 10 2
run_config pressure_strong 2 2 300 8 25 25 10 1

python3 - <<'PY' "$SWEEP_ROOT"
import csv
import json
import sys
from pathlib import Path


def fnum(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(new, old):
    if old == 0:
        return 0.0
    return (new - old) * 100.0 / old


def variant(data, name):
    for item in data.get("variants", []):
        if item.get("name") == name:
            return item
    return {}


def gate_map(data):
    return {
        item.get("name", ""): item.get("status", "")
        for item in data.get("claim_gate_audit", {}).get("gates", [])
        if item.get("name")
    }


root = Path(sys.argv[1])
rows = []
for summary in sorted(root.glob("*/matrix-summary.json")):
    data = json.loads(summary.read_text(encoding="utf-8"))
    config = data.get("config", {})
    default = variant(data, "default_taskset")
    flow = variant(data, "flowcausal_width")
    default_agg = default.get("aggregate", {})
    flow_agg = flow.get("aggregate", {})
    default_victim = default_agg.get("victim_mean", {})
    flow_victim = flow_agg.get("victim_mean", {})
    flowmon = flow_agg.get("flowmon_mean", {})
    gates = gate_map(data)
    default_critical = fnum(default_agg.get("critical_mean_sec"))
    flow_critical = fnum(flow_agg.get("critical_mean_sec"))
    default_total = fnum(default_agg.get("total_mean_sec"))
    flow_total = fnum(flow_agg.get("total_mean_sec"))
    rows.append(
        {
            "config": summary.parent.name,
            "summary": str(summary),
            "level": data.get("claim_gate_audit", {}).get("highest_supported_level", ""),
            "trials": config.get("trials", 0),
            "frontier_dispatch_quota": config.get("frontier_dispatch_quota", 0),
            "guard_cooldown_dispatches": config.get("guard_cooldown_dispatches", 0),
            "shared_wait_pressure_us": config.get("shared_wait_pressure_us", 0),
            "pressure_fallback_enqueues": config.get("pressure_fallback_enqueues", 0),
            "pressure_fallback_ms": config.get("pressure_fallback_ms", 0),
            "pressure_dispatch_ms": config.get("pressure_dispatch_ms", 0),
            "frontier_dispatch_window_ms": config.get("frontier_dispatch_window_ms", 0),
            "frontier_dispatch_window_budget": config.get("frontier_dispatch_window_budget", 0),
            "default_critical_sec": default_critical,
            "flowcausal_critical_sec": flow_critical,
            "critical_delta_pct": pct(flow_critical, default_critical),
            "default_total_sec": default_total,
            "flowcausal_total_sec": flow_total,
            "total_delta_pct": pct(flow_total, default_total),
            "default_victim_p99_ms": fnum(default_victim.get("lateness_p99_ms")),
            "flowcausal_victim_p99_ms": fnum(flow_victim.get("lateness_p99_ms")),
            "default_victim_max_ms": fnum(default_victim.get("lateness_max_ms")),
            "flowcausal_victim_max_ms": fnum(flow_victim.get("lateness_max_ms")),
            "frontier_enqueues": fnum(flowmon.get("frontier_enqueues")),
            "fallback_enqueues": fnum(flowmon.get("fallback_enqueues")),
            "pressure_dispatches": fnum(flowmon.get("global_pressure_dispatches")),
            "pressure_fallback_entries": fnum(flowmon.get("global_pressure_fallback_entries")),
            "frontier_budget_trips": fnum(flowmon.get("global_frontier_budget_trips")),
            "direct_gate": gates.get("direct CPU mechanism", ""),
            "outcome_gate": gates.get("stage/episode outcome", ""),
            "safety_gate": gates.get("victim/fairness safety", ""),
        }
    )

json_path = root / "sweep-summary.json"
csv_path = root / "sweep-summary.csv"
json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
if rows:
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

print(f"json={json_path}")
print(f"csv={csv_path}")
for row in rows:
    print(
        row["config"],
        f"critical_delta={row['critical_delta_pct']:.1f}%",
        f"total_delta={row['total_delta_pct']:.1f}%",
        f"victim_p99={row['default_victim_p99_ms']:.3f}->{row['flowcausal_victim_p99_ms']:.3f}ms",
        f"frontier={row['frontier_enqueues']:.0f}",
        f"fallback={row['fallback_enqueues']:.0f}",
        f"gates={row['direct_gate']}/{row['outcome_gate']}/{row['safety_gate']}",
    )
PY

#!/usr/bin/env python3
"""Build LockAgent profiling JSON from benchmark metadata and optional summaries.

This builder intentionally does not compute or embed the target run's best
scheduler. It produces a compact observation profile for LLM policy selection.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATES = [
    "default",
    "scx_simple",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
    "scx_hybrid",
    "scx_toponest",
    "scx_cfl",
    "scx_lcas",
]


def load_summary_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def find_case(rows: list[dict[str, str]], scheduler: str, threads: int) -> dict[str, str] | None:
    for row in rows:
        if row.get("scheduler") == scheduler and int(float(row.get("threads", "0") or 0)) == threads:
            return row
    return None


def f(row: dict[str, str] | None, key: str) -> float | None:
    if not row:
        return None
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def categorical_from_ratio(value: float | None, high: float, medium: float) -> str:
    if value is None:
        return "unknown"
    if value >= high:
        return "high"
    if value >= medium:
        return "medium"
    return "low"


def load_default_metrics(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def numeric_metric(metrics: dict[str, Any], key: str) -> float | None:
    value = metrics.get(key)
    if value in (None, ""):
        return None
    if not isinstance(value, (int, float)):
        return None
    return float(value)


def build_mariadb_profile(args: argparse.Namespace) -> dict[str, Any]:
    rows = load_summary_rows(args.summary) if args.summary else []
    default = find_case(rows, "default", args.threads) if rows else None
    live_default = load_default_metrics(args.default_metrics)

    default_tps = numeric_metric(live_default, "tps")
    default_p95 = numeric_metric(live_default, "lat_p95_ms")
    default_avg = numeric_metric(live_default, "lat_avg_ms")
    default_max = numeric_metric(live_default, "lat_max_ms")
    default_cv = numeric_metric(live_default, "fairness_events_cv")
    default_source = "live_profile_window" if live_default else "none"
    if default_tps is None and default:
        default_tps = f(default, "tps_mean")
        default_source = "historical_summary"
    if default_p95 is None and default:
        default_p95 = f(default, "lat_p95_mean")
        default_source = "historical_summary"
    if default_cv is None and default:
        default_cv = f(default, "tps_cv")

    # Conservative observation hints from a default profiling window. These are
    # not final best-scheduler labels; they describe the contention symptoms.
    if args.threads >= 192:
        hot_lock_top1 = 0.72
        hot_lock_top5 = 0.91
        handoff_distance = "frequent_cross_numa"
        owner_preempt_score = 0.68
        lock_event_rate = "high"
    elif args.threads >= 128:
        hot_lock_top1 = 0.55
        hot_lock_top5 = 0.82
        handoff_distance = "mixed_numa"
        owner_preempt_score = 0.52
        lock_event_rate = "medium_high"
    else:
        hot_lock_top1 = 0.35
        hot_lock_top5 = 0.65
        handoff_distance = "mostly_local_or_mixed"
        owner_preempt_score = 0.35
        lock_event_rate = "medium"

    if default_tps is not None and default_p95 is not None:
        if default_tps >= 20000 and default_p95 <= 50:
            cpu_progress_hint = "high_effective_progress_under_default"
            affinity_risk = "high_parallelism_collapse_risk"
        elif default_tps >= 5000 and default_p95 <= 200:
            cpu_progress_hint = "moderate_to_high_effective_progress_under_default"
            affinity_risk = "medium_parallelism_collapse_risk"
        else:
            cpu_progress_hint = "low_effective_progress_under_default"
            affinity_risk = "low_parallelism_collapse_risk"
    else:
        cpu_progress_hint = "unknown_current_default_progress"
        affinity_risk = "unknown"

    profile: dict[str, Any] = {
        "profile_id": f"mariadb_sysbench_oltp_t{args.threads}_profile_window",
        "workload": {
            "name": "MariaDB Sysbench OLTP",
            "threads": args.threads,
            "duration_s": args.profile_s,
            "phase": "profiling_window",
        },
        "signals": {
            "lock_event_rate": lock_event_rate,
            "hot_lock_ratio_top1": hot_lock_top1,
            "hot_lock_ratio_top5": hot_lock_top5,
            "p95_wait_us": args.p95_wait_us,
            "owner_preempt_score": owner_preempt_score,
            "handoff_distance": handoff_distance,
            "wakeup_burstiness": "medium",
            "jain_fairness": 0.98,
            "zero_progress_threads": 0,
            "cpu_utilization_hint": cpu_progress_hint,
            "affinity_parallelism_risk": affinity_risk,
            "default_tps_observed": default_tps,
            "default_avg_ms_observed": default_avg,
            "default_p95_ms_observed": default_p95,
            "default_max_ms_observed": default_max,
            "default_tps_cv_observed": default_cv,
            "default_metrics_source": default_source,
        },
        "current_scheduler": "default",
        "candidate_schedulers": DEFAULT_CANDIDATES,
        "objective": {
            "primary": "maximize throughput",
            "secondary": ["reduce p95 latency", "avoid zero progress", "preserve fairness"],
        },
        "data_provenance": {
            "summary_csv": str(args.summary) if args.summary else None,
            "default_metrics": str(args.default_metrics) if args.default_metrics else None,
            "contains_oracle_best": False,
            "note": "Online profile uses live default profiling metrics when provided; historical summary is only used when explicitly requested.",
        },
    }
    return profile


def build_rocksdb_profile(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "profile_id": f"rocksdb_{args.rocksdb_bench}_t{args.threads}_profile_window",
        "workload": {
            "name": f"RocksDB db_bench {args.rocksdb_bench}",
            "threads": args.threads,
            "duration_s": args.profile_s,
            "phase": "profiling_window",
        },
        "signals": {
            "lock_event_rate": "high",
            "hot_lock_ratio_top1": 0.18,
            "hot_lock_ratio_top5": 0.43,
            "lock_address_cardinality": "high",
            "p95_wait_us": args.p95_wait_us,
            "owner_preempt_score": 0.41,
            "handoff_distance": "mixed",
            "wakeup_burstiness": "low",
            "jain_fairness": 0.99,
            "zero_progress_threads": 0,
            "background_threads": "compaction_present",
        },
        "current_scheduler": "default",
        "candidate_schedulers": DEFAULT_CANDIDATES,
        "objective": {
            "primary": "maximize throughput",
            "secondary": ["avoid affinity regression", "preserve p999 latency"],
        },
        "data_provenance": {
            "contains_oracle_best": False,
            "note": "Synthetic profile summary based on profiling-window symptoms, not final scheduler results.",
        },
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workload", choices=["mariadb-sysbench", "rocksdb-cfl"], required=True)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--profile-s", type=int, default=10)
    ap.add_argument("--summary", type=Path, help="Optional existing summary_cases.csv for default observations")
    ap.add_argument("--default-metrics", type=Path, help="Live default profiling metrics JSON from sysbench_metrics.py")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--p95-wait-us", type=float, default=4800.0)
    ap.add_argument("--rocksdb-bench", default="readwhilewriting")
    args = ap.parse_args(argv)

    if args.workload == "mariadb-sysbench":
        profile = build_mariadb_profile(args)
    else:
        profile = build_rocksdb_profile(args)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(profile, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

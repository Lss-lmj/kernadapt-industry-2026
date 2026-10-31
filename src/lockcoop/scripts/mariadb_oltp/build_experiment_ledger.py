#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent.parent
DEFAULT_RESULT_ROOT = TEST_DIR / "result"
DEFAULT_OUT_DIR = DEFAULT_RESULT_ROOT / "ledger"

COUNTER_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=\s*([0-9]+)")


RUN_FIELDS = [
    "experiment",
    "experiment_path",
    "phase",
    "created_at",
    "workload",
    "run_time",
    "warmup_time",
    "tables",
    "table_size",
    "cpu_pinning",
    "scheduler",
    "threads",
    "run_id",
    "valid",
    "failure_type",
    "failure_detail",
    "tps",
    "qps",
    "lat_avg",
    "lat_p95",
    "lat_p99",
    "lat_max",
    "ignored_errors",
    "sysbench_returncode",
    "start_timestamp",
    "end_timestamp",
    "kernel_version",
    "mariadb_version",
    "innodb_buffer_pool_size",
    "sched_ext_initial_state",
    "sched_ext_after_start_state",
    "sched_ext_final_state",
    "raw_dir",
    "has_scheduler_stall",
    "has_scheduler_error",
    "counter_hit",
    "counter_anchor_idle",
    "counter_anchor_busy",
    "counter_prev_idle_escape",
    "counter_stage_prev",
    "counter_stage_default",
    "counter_sticky_hit",
    "counter_sticky_create",
    "counter_pending_refresh",
    "counter_pending_expired",
    "counter_deferred_migrate",
    "counter_forced_anchor",
    "counter_escape_budget_blocked",
    "counter_escape_topology_blocked",
    "counter_stage_topology_blocked",
]

CASE_FIELDS = [
    "experiment",
    "phase",
    "created_at",
    "workload",
    "run_time",
    "warmup_time",
    "tables",
    "table_size",
    "cpu_pinning",
    "scheduler",
    "threads",
    "n",
    "tps_mean",
    "tps_median",
    "tps_stdev",
    "tps_cv",
    "lat_avg_mean",
    "lat_p95_mean",
    "lat_p99_mean",
    "qps_mean",
    "failures",
    "valid_runs",
    "excluded_runs",
    "notes",
]

PROTOCOL_FIELDS = [
    "protocol_id",
    "workload",
    "run_time",
    "warmup_time",
    "tables",
    "table_size",
    "cpu_pinning",
    "scheduler",
    "threads",
    "n",
    "experiments",
    "tps_mean",
    "tps_median",
    "tps_stdev",
    "tps_cv",
    "lat_avg_mean",
    "lat_p95_mean",
    "lat_p99_mean",
    "qps_mean",
    "excluded_runs",
]


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})


def to_float(row: dict[str, Any], key: str) -> float | None:
    try:
        v = row.get(key, "")
        if v == "":
            return None
        return float(v)
    except Exception:
        return None


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def stdev(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) >= 2 else 0.0


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def cv(values: list[float]) -> float:
    mu = mean(values)
    return stdev(values) / mu if len(values) >= 2 and mu else 0.0


def last_counter_line(log_path: Path) -> dict[str, int]:
    if not log_path.exists():
        return {}
    try:
        text = log_path.read_text(errors="ignore").replace("\033", "\n")
    except Exception:
        return {}
    lines = [ln for ln in text.splitlines() if "local=" in ln or "hit=" in ln]
    if not lines:
        return {}
    return {k: int(v) for k, v in COUNTER_RE.findall(lines[-1])}


def scheduler_flags(log_path: Path) -> tuple[int, int]:
    if not log_path.exists():
        return 0, 0
    try:
        text = log_path.read_text(errors="ignore")
    except Exception:
        return 0, 0
    stall = int("runnable task stall" in text or "watchdog" in text)
    error = int(any(x in text for x in ["ERROR", "Error", "Failed", "failed", "libbpf:"]))
    return stall, error


def phase_from_raw_dir(raw_dir: str) -> str:
    parts = Path(raw_dir).parts
    if "performance" in parts:
        return "performance"
    if "mechanism" in parts:
        return "mechanism"
    return ""


def enrich_run(row: dict[str, str], exp: Path, config: dict[str, Any]) -> dict[str, Any]:
    raw_dir = Path(row.get("raw_dir", ""))
    counters = last_counter_line(raw_dir / "scheduler.log")
    stall, error = scheduler_flags(raw_dir / "scheduler.log")
    out: dict[str, Any] = {
        "experiment": exp.name,
        "experiment_path": str(exp),
        "phase": phase_from_raw_dir(row.get("raw_dir", "")),
        "created_at": config.get("created_at", ""),
        "workload": config.get("workload", ""),
        "run_time": config.get("run_time", ""),
        "warmup_time": config.get("warmup_time", ""),
        "tables": config.get("tables", ""),
        "table_size": config.get("table_size", ""),
        "cpu_pinning": config.get("cpu_pinning", ""),
        "has_scheduler_stall": stall,
        "has_scheduler_error": error,
    }
    out.update(row)
    for name in [
        "hit",
        "anchor_idle",
        "anchor_busy",
        "prev_idle_escape",
        "stage_prev",
        "stage_default",
        "sticky_hit",
        "sticky_create",
        "pending_refresh",
        "pending_expired",
        "deferred_migrate",
        "forced_anchor",
        "escape_budget_blocked",
        "escape_topology_blocked",
        "stage_topology_blocked",
    ]:
        out[f"counter_{name}"] = counters.get(name, "")
    return out


def valid_for_rollup(row: dict[str, Any], include_debug: bool) -> bool:
    if str(row.get("valid", "")) != "1":
        return False
    if row.get("has_scheduler_stall") == 1:
        return False
    if not include_debug and str(row.get("experiment", "")).startswith("debug_"):
        return False
    return to_float(row, "tps") is not None


def summarize_group(rows: list[dict[str, Any]]) -> dict[str, Any]:
    tps = [to_float(r, "tps") for r in rows]
    qps = [to_float(r, "qps") for r in rows]
    lat_avg = [to_float(r, "lat_avg") for r in rows]
    lat_p95 = [to_float(r, "lat_p95") for r in rows]
    lat_p99 = [to_float(r, "lat_p99") for r in rows]
    tps_v = [x for x in tps if x is not None]
    qps_v = [x for x in qps if x is not None]
    lat_avg_v = [x for x in lat_avg if x is not None]
    lat_p95_v = [x for x in lat_p95 if x is not None]
    lat_p99_v = [x for x in lat_p99 if x is not None]
    return {
        "n": len(rows),
        "tps_mean": f"{mean(tps_v):.6f}",
        "tps_median": f"{median(tps_v):.6f}",
        "tps_stdev": f"{stdev(tps_v):.6f}",
        "tps_cv": f"{cv(tps_v):.6f}",
        "lat_avg_mean": f"{mean(lat_avg_v):.6f}",
        "lat_p95_mean": f"{mean(lat_p95_v):.6f}",
        "lat_p99_mean": f"{mean(lat_p99_v):.6f}",
        "qps_mean": f"{mean(qps_v):.6f}",
    }


def build_case_rows(run_rows: list[dict[str, Any]], include_debug: bool) -> list[dict[str, Any]]:
    all_groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    excluded: dict[tuple[Any, ...], int] = defaultdict(int)
    for row in run_rows:
        key = (
            row.get("experiment", ""),
            row.get("phase", ""),
            row.get("scheduler", ""),
            row.get("threads", ""),
        )
        if valid_for_rollup(row, include_debug):
            all_groups[key].append(row)
        else:
            excluded[key] += 1

    cases: list[dict[str, Any]] = []
    for key, rows in sorted(all_groups.items()):
        first = rows[0]
        out = {
            "experiment": first.get("experiment", ""),
            "phase": first.get("phase", ""),
            "created_at": first.get("created_at", ""),
            "workload": first.get("workload", ""),
            "run_time": first.get("run_time", ""),
            "warmup_time": first.get("warmup_time", ""),
            "tables": first.get("tables", ""),
            "table_size": first.get("table_size", ""),
            "cpu_pinning": first.get("cpu_pinning", ""),
            "scheduler": first.get("scheduler", ""),
            "threads": first.get("threads", ""),
            "failures": excluded[key],
            "valid_runs": ",".join(str(r.get("run_id", "")) for r in rows),
            "excluded_runs": excluded[key],
            "notes": "",
        }
        out.update(summarize_group(rows))
        cases.append(out)
    return cases


def protocol_id(row: dict[str, Any]) -> str:
    return "|".join(
        str(row.get(k, ""))
        for k in ["workload", "run_time", "warmup_time", "tables", "table_size", "cpu_pinning"]
    )


def build_protocol_rows(run_rows: list[dict[str, Any]], include_debug: bool) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    excluded: dict[tuple[Any, ...], int] = defaultdict(int)
    for row in run_rows:
        key = (
            protocol_id(row),
            row.get("scheduler", ""),
            row.get("threads", ""),
        )
        if valid_for_rollup(row, include_debug):
            groups[key].append(row)
        else:
            excluded[key] += 1

    out_rows: list[dict[str, Any]] = []
    for key, rows in sorted(groups.items()):
        first = rows[0]
        out = {
            "protocol_id": key[0],
            "workload": first.get("workload", ""),
            "run_time": first.get("run_time", ""),
            "warmup_time": first.get("warmup_time", ""),
            "tables": first.get("tables", ""),
            "table_size": first.get("table_size", ""),
            "cpu_pinning": first.get("cpu_pinning", ""),
            "scheduler": first.get("scheduler", ""),
            "threads": first.get("threads", ""),
            "experiments": ";".join(sorted({str(r.get("experiment", "")) for r in rows})),
            "excluded_runs": excluded[key],
        }
        out.update(summarize_group(rows))
        out_rows.append(out)
    return out_rows


def find_experiments(result_root: Path) -> list[Path]:
    exps: list[Path] = []
    for path in result_root.iterdir():
        if not path.is_dir():
            continue
        if path.name in {"ledger", "plots", "plots_png"}:
            continue
        if (path / "summary_runs.csv").exists() and (path / "config.json").exists():
            exps.append(path)
    return sorted(exps)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build an accumulated MariaDB/Sysbench experiment ledger.")
    ap.add_argument("--result-root", default=str(DEFAULT_RESULT_ROOT))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    ap.add_argument("--include-debug", action="store_true", help="Include debug_* experiments in rollups.")
    args = ap.parse_args()

    result_root = Path(args.result_root)
    out_dir = Path(args.out_dir)

    run_rows: list[dict[str, Any]] = []
    for exp in find_experiments(result_root):
        config = read_json(exp / "config.json")
        for row in read_csv(exp / "summary_runs.csv"):
            run_rows.append(enrich_run(row, exp, config))

    case_rows = build_case_rows(run_rows, args.include_debug)
    protocol_rows = build_protocol_rows(run_rows, args.include_debug)

    write_csv(out_dir / "runs.csv", RUN_FIELDS, run_rows)
    write_csv(out_dir / "cases.csv", CASE_FIELDS, case_rows)
    write_csv(out_dir / "protocol_cases.csv", PROTOCOL_FIELDS, protocol_rows)
    print(f"wrote {len(run_rows)} runs to {out_dir / 'runs.csv'}")
    print(f"wrote {len(case_rows)} cases to {out_dir / 'cases.csv'}")
    print(f"wrote {len(protocol_rows)} protocol cases to {out_dir / 'protocol_cases.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

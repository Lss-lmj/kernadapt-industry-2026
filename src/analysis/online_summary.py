#!/usr/bin/env python3
"""Summarize KernAdapt-online experiment runs.

This script is deliberately conservative: a run is considered paper-usable
only if the online manifest, executor status, sysbench log, and scx log agree
that the selected scheduler actually ran and cleaned up safely.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any


TPS_RE = re.compile(r"transactions:\s+\d+\s+\(([0-9.]+) per sec\.\)")
QPS_RE = re.compile(r"queries:\s+\d+\s+\(([0-9.]+) per sec\.\)")
LAT_AVG_RE = re.compile(r"avg:\s+([0-9.]+)")
LAT_P95_RE = re.compile(r"95th percentile:\s+([0-9.]+)")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def extract_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if not match:
        return None
    return float(match.group(1))


def parse_sysbench(log_path: Path) -> dict[str, float | None]:
    if not log_path.exists():
        return {"tps": None, "qps": None, "lat_avg_ms": None, "lat_p95_ms": None}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return {
        "tps": extract_float(TPS_RE, text),
        "qps": extract_float(QPS_RE, text),
        "lat_avg_ms": extract_float(LAT_AVG_RE, text),
        "lat_p95_ms": extract_float(LAT_P95_RE, text),
    }


def scx_loaded(scheduler: str | None, scx_log: Path) -> bool:
    if scheduler in (None, "default"):
        return True
    if not scx_log.exists():
        return False
    text = scx_log.read_text(encoding="utf-8", errors="replace")
    if "sudo: a password is required" in text or "sudo: a terminal is required" in text:
        return False
    if "EXIT: unregistered from user space" in text:
        return True
    # Lock-aware schedulers print lock placement diagnostics when successfully
    # attached; use this as a secondary signal for older logs.
    return "Lock Addr:" in text and "Anchor CPU:" in text


def summarize_run(run_dir: Path) -> dict[str, Any]:
    manifest = load_json(run_dir / "online_manifest.json")
    decision = load_json(run_dir / "decision.json")
    status = load_json(run_dir / "execution" / "status.json")
    trajectory = load_json(run_dir / "trajectory_status.json")
    failure = load_json(run_dir / "failure_attribution.json")
    curation = load_json(run_dir / "skill_curation.json")
    bench_manifest = load_json(run_dir / "execution" / "benchmark" / "sysbench_manifest.json")
    metrics = parse_sysbench(run_dir / "execution" / "benchmark" / "sysbench.log")

    scheduler = decision.get("scheduler") if decision else None
    loaded = scx_loaded(scheduler, run_dir / "execution" / "scx.log")
    online_ok = bool(manifest and manifest.get("status") == "ok" and manifest.get("returncode") == 0)
    exec_ok = bool(status and status.get("status") == "ok" and status.get("returncode") == 0)
    cleaned = bool(status and status.get("final_sched_ext_state") == "disabled")
    benchmark_ok = metrics["tps"] is not None and metrics["lat_p95_ms"] is not None
    valid = online_ok and exec_ok and cleaned and benchmark_ok and loaded

    invalid_reasons: list[str] = []
    if not online_ok:
        invalid_reasons.append("online_manifest_not_ok")
    if not exec_ok:
        invalid_reasons.append("executor_not_ok")
    if not cleaned:
        invalid_reasons.append("sched_ext_not_disabled_after_run")
    if not benchmark_ok:
        invalid_reasons.append("missing_sysbench_metrics")
    if not loaded:
        invalid_reasons.append("scx_not_loaded")
    if trajectory and not trajectory.get("valid"):
        invalid_reasons.extend(trajectory.get("invalid_reasons", []))
        valid = False
    invalid_reasons = sorted(set(invalid_reasons))

    return {
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "valid": valid,
        "invalid_reasons": invalid_reasons,
        "trajectory_status": trajectory.get("status") if trajectory else None,
        "failure_categories": failure.get("categories") if failure else [],
        "primary_failure_category": failure.get("primary_category") if failure else None,
        "skill_curation_verdict": curation.get("verdict") if curation else None,
        "skill_curation_recommendation": curation.get("recommendation") if curation else None,
        "skill_name": curation.get("skill") if curation else None,
        "workload": manifest.get("workload") if manifest else None,
        "threads": manifest.get("threads") if manifest else bench_manifest.get("threads") if bench_manifest else None,
        "run_s": manifest.get("run_s") if manifest else bench_manifest.get("time") if bench_manifest else None,
        "scheduler": scheduler,
        "confidence": decision.get("confidence") if decision else None,
        "tps": metrics["tps"],
        "qps": metrics["qps"],
        "lat_avg_ms": metrics["lat_avg_ms"],
        "lat_p95_ms": metrics["lat_p95_ms"],
        "online_status": manifest.get("status") if manifest else None,
        "online_returncode": manifest.get("returncode") if manifest else None,
        "executor_status": status.get("status") if status else None,
        "executor_returncode": status.get("returncode") if status else None,
        "final_sched_ext_state": status.get("final_sched_ext_state") if status else None,
        "scx_loaded": loaded,
        "start_time": manifest.get("start_time") if manifest else None,
        "end_time": manifest.get("end_time") if manifest else None,
    }


def collect_runs(root: Path) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for run_dir in sorted(root.glob("run_*")):
        if run_dir.is_dir():
            runs.append(summarize_run(run_dir))
    return runs


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "run_id",
        "valid",
        "invalid_reasons",
        "workload",
        "threads",
        "run_s",
        "scheduler",
        "confidence",
        "trajectory_status",
        "failure_categories",
        "primary_failure_category",
        "skill_name",
        "skill_curation_verdict",
        "skill_curation_recommendation",
        "tps",
        "qps",
        "lat_avg_ms",
        "lat_p95_ms",
        "online_status",
        "executor_status",
        "final_sched_ext_state",
        "scx_loaded",
        "run_dir",
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            out["invalid_reasons"] = ";".join(row["invalid_reasons"])
            out["failure_categories"] = ";".join(row.get("failure_categories", []))
            writer.writerow(out)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=Path("results/mariadb_online"))
    ap.add_argument("--out-json", type=Path, help="Write summary JSON")
    ap.add_argument("--out-csv", type=Path, help="Write summary CSV")
    ap.add_argument("--valid-only", action="store_true")
    args = ap.parse_args(argv)

    rows = collect_runs(args.root)
    if args.valid_only:
        rows = [row for row in rows if row["valid"]]

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(rows, args.out_csv)

    print(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

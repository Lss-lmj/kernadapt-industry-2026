#!/usr/bin/env python3
"""Summarize same-batch MariaDB comparison experiments."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from online_summary import load_json, parse_sysbench, scx_loaded, summarize_run


def fixed_trial_summary(trial: dict[str, Any]) -> dict[str, Any]:
    trial_dir = Path(str(trial["trial_dir"]))
    decision = load_json(trial_dir / "decision.json")
    status = load_json(trial_dir / "execution" / "status.json")
    bench_manifest = load_json(trial_dir / "execution" / "benchmark" / "sysbench_manifest.json")
    metrics = parse_sysbench(trial_dir / "execution" / "benchmark" / "sysbench.log")

    scheduler = decision.get("scheduler") if decision else trial.get("scheduler")
    loaded = scx_loaded(scheduler, trial_dir / "execution" / "scx.log")
    exec_ok = bool(status and status.get("status") == "ok" and status.get("returncode") == 0)
    cleaned = bool(status and status.get("final_sched_ext_state") == "disabled")
    benchmark_ok = metrics["tps"] is not None and metrics["lat_p95_ms"] is not None
    valid = exec_ok and cleaned and benchmark_ok and loaded

    invalid_reasons: list[str] = []
    if not exec_ok:
        invalid_reasons.append("executor_not_ok")
    if not cleaned:
        invalid_reasons.append("sched_ext_not_disabled_after_run")
    if not benchmark_ok:
        invalid_reasons.append("missing_sysbench_metrics")
    if not loaded:
        invalid_reasons.append("scx_not_loaded")

    return {
        "arm": trial.get("arm"),
        "rep": trial.get("rep"),
        "trial_dir": str(trial_dir),
        "valid": valid,
        "invalid_reasons": invalid_reasons,
        "scheduler": scheduler,
        "confidence": decision.get("confidence") if decision else None,
        "threads": bench_manifest.get("threads") if bench_manifest else None,
        "run_s": bench_manifest.get("time") if bench_manifest else None,
        "tps": metrics["tps"],
        "qps": metrics["qps"],
        "lat_avg_ms": metrics["lat_avg_ms"],
        "lat_p95_ms": metrics["lat_p95_ms"],
        "executor_status": status.get("status") if status else None,
        "executor_returncode": status.get("returncode") if status else None,
        "final_sched_ext_state": status.get("final_sched_ext_state") if status else None,
        "scx_loaded": loaded,
    }


def online_trial_summary(trial: dict[str, Any]) -> dict[str, Any]:
    trial_dir_value = trial.get("trial_dir")
    if not trial_dir_value:
        row = dict(trial)
        row.update({"valid": False, "invalid_reasons": ["missing_online_trial_dir"]})
        return row
    row = summarize_run(Path(str(trial_dir_value)))
    row["arm"] = str(trial.get("arm") or "kernadapt_online")
    row["legacy_arm"] = trial.get("legacy_arm")
    row["rep"] = trial.get("rep")
    row["trial_dir"] = row.pop("run_dir")
    return row


def summarize_trials(batch_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for trial in batch_manifest.get("trials", []):
        if trial.get("arm") in {"kernadapt_online", "lockagent_online"}:
            rows.append(online_trial_summary(trial))
        else:
            rows.append(fixed_trial_summary(trial))
    return rows


def mean(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.fmean(values)


def stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return 0.0 if len(values) == 1 else None
    return statistics.stdev(values)


def aggregate_trials(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("arm"))].append(row)

    default_tps_mean: float | None = None
    default_p95_mean: float | None = None
    output: list[dict[str, Any]] = []
    for arm, items in sorted(groups.items()):
        valid_items = [row for row in items if row.get("valid")]
        tps_values = [float(row["tps"]) for row in valid_items if row.get("tps") is not None]
        p95_values = [float(row["lat_p95_ms"]) for row in valid_items if row.get("lat_p95_ms") is not None]
        lat_avg_values = [float(row["lat_avg_ms"]) for row in valid_items if row.get("lat_avg_ms") is not None]

        tps_mean = mean(tps_values)
        tps_stdev = stdev(tps_values)
        p95_mean = mean(p95_values)
        lat_avg_mean = mean(lat_avg_values)
        row = {
            "arm": arm,
            "n_total": len(items),
            "n_valid": len(valid_items),
            "scheduler_values": ",".join(sorted({str(r.get("scheduler")) for r in valid_items})),
            "tps_mean": tps_mean,
            "tps_stdev": tps_stdev,
            "tps_cv": (tps_stdev / tps_mean) if tps_mean not in (None, 0) and tps_stdev is not None else None,
            "lat_avg_ms_mean": lat_avg_mean,
            "lat_p95_ms_mean": p95_mean,
        }
        output.append(row)
        if arm == "default":
            default_tps_mean = tps_mean
            default_p95_mean = p95_mean

    for row in output:
        if default_tps_mean not in (None, 0) and row["tps_mean"] is not None:
            row["tps_speedup_vs_default"] = row["tps_mean"] / default_tps_mean
        else:
            row["tps_speedup_vs_default"] = None
        if default_p95_mean not in (None, 0) and row["lat_p95_ms_mean"] is not None:
            row["p95_reduction_vs_default"] = 1.0 - (row["lat_p95_ms_mean"] / default_p95_mean)
        else:
            row["p95_reduction_vs_default"] = None
    return output


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            out = dict(row)
            if isinstance(out.get("invalid_reasons"), list):
                out["invalid_reasons"] = ";".join(out["invalid_reasons"])
            writer.writerow(out)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", type=Path, required=True, help="Batch directory or batch_manifest.json")
    ap.add_argument("--out-dir", type=Path, help="Output directory. Defaults to the batch directory")
    args = ap.parse_args(argv)

    manifest_path = args.batch if args.batch.name == "batch_manifest.json" else args.batch / "batch_manifest.json"
    manifest = load_json(manifest_path)
    if manifest is None:
        raise FileNotFoundError(f"batch manifest not found: {manifest_path}")
    out_dir = args.out_dir or manifest_path.parent
    if args.out_dir is None and not os.access(out_dir, os.W_OK):
        batch_id = str(manifest.get("batch_id") or manifest_path.parent.name)
        out_dir = Path("results/mariadb_comparison_summaries") / batch_id
        print(f"warning: batch directory is not writable; writing summary to {out_dir}", file=sys.stderr)

    trials = summarize_trials(manifest)
    aggregate = aggregate_trials(trials)
    output = {
        "batch_manifest": str(manifest_path),
        "batch_id": manifest.get("batch_id"),
        "trials": trials,
        "aggregate": aggregate,
    }

    write_json(out_dir / "batch_summary.json", output)
    write_csv(trials, out_dir / "batch_trials.csv")
    write_csv(aggregate, out_dir / "batch_aggregate.csv")
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

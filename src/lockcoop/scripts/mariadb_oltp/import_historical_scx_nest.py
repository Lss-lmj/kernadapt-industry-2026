#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
import statistics
from pathlib import Path
from typing import Any


TEST_DIR = Path(__file__).resolve().parent.parent
DEFAULT_THREADS = [64, 128, 192, 256]

TPS_RE = re.compile(r"^\s*transactions:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
QPS_RE = re.compile(r"^\s*queries:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
LAT_RE = re.compile(r"^\s*(?P<k>min|avg|max|95th percentile|99th percentile):\s*(?P<v>[0-9]+(?:\.[0-9]+)?)\s*$")


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def fmt(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(v):
        return ""
    return f"{v:.6f}"


def parse_sysbench(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {"raw_file": str(path)}
    for line in path.read_text(errors="replace").splitlines():
        m = TPS_RE.match(line)
        if m:
            out["tps"] = float(m.group("v"))
            continue
        m = QPS_RE.match(line)
        if m:
            out["qps"] = float(m.group("v"))
            continue
        m = LAT_RE.match(line)
        if m:
            key = m.group("k")
            val = float(m.group("v"))
            if key == "avg":
                out["lat_avg"] = val
            elif key == "95th percentile":
                out["lat_p95"] = val
            elif key == "99th percentile":
                out["lat_p99"] = val
            elif key == "max":
                out["lat_max"] = val
            elif key == "min":
                out["lat_min"] = val
    return out


def scx_error_count(case_dir: Path) -> int:
    n = 0
    for log in sorted(case_dir.glob("scx.r*.log")):
        try:
            text = log.read_text(errors="replace")
        except OSError:
            continue
        if "scx_bpf_error" in text or "EXIT:" in text:
            n += 1
    return n


def collect_perf_history(result_dir: Path, tag: str, threads: list[int]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    run_rows: list[dict[str, Any]] = []
    case_rows: list[dict[str, Any]] = []
    for t in threads:
        case_dir = result_dir / f"{tag}_scx_nest_t{t}_d60"
        runs: list[dict[str, Any]] = []
        for f in sorted(case_dir.glob("sysbench.r*.txt")):
            m = re.search(r"\.r([0-9]+)\.txt$", f.name)
            run_id = int(m.group(1)) if m else len(runs) + 1
            metrics = parse_sysbench(f)
            if metrics.get("tps") is None:
                continue
            row = {
                "scheduler": "scx_nest",
                "threads": t,
                "run_id": run_id,
                "valid": 1,
                "failure_type": "",
                "failure_detail": "",
                "data_source": "historical",
                "historical_tag": tag,
                "historical_path": str(case_dir),
                "tps": fmt(metrics.get("tps")),
                "qps": fmt(metrics.get("qps")),
                "lat_avg": fmt(metrics.get("lat_avg")),
                "lat_p95": fmt(metrics.get("lat_p95")),
                "lat_p99": fmt(metrics.get("lat_p99")),
                "lat_max": fmt(metrics.get("lat_max")),
                "p99_source": "historical log" if metrics.get("lat_p99") is not None else "missing: historical sysbench used percentile=95 without histogram",
                "raw_dir": str(case_dir),
            }
            runs.append(row)
            run_rows.append(row)

        def nums(field: str) -> list[float]:
            out: list[float] = []
            for r in runs:
                if r.get(field) in ("", None):
                    continue
                out.append(float(r[field]))
            return out

        tps = nums("tps")
        qps = nums("qps")
        lat_avg = nums("lat_avg")
        lat_p95 = nums("lat_p95")
        lat_p99 = nums("lat_p99")
        err_count = scx_error_count(case_dir)
        mu = statistics.mean(tps) if tps else math.nan
        sd = statistics.pstdev(tps) if len(tps) >= 2 else (0.0 if tps else math.nan)
        case_rows.append({
            "scheduler": "scx_nest",
            "threads": t,
            "n": len(tps),
            "tps_mean": fmt(mu),
            "tps_stdev": fmt(sd),
            "tps_cv": fmt(sd / mu if tps and mu else math.nan),
            "lat_avg_mean": fmt(statistics.mean(lat_avg) if lat_avg else math.nan),
            "lat_p95_mean": fmt(statistics.mean(lat_p95) if lat_p95 else math.nan),
            "lat_p99_mean": fmt(statistics.mean(lat_p99) if lat_p99 else math.nan),
            "qps_mean": fmt(statistics.mean(qps) if qps else math.nan),
            "failures": "",
            "data_source": "historical",
            "historical_tag": tag,
            "historical_path": str(case_dir),
            "historical_scheduler_error_runs": err_count,
            "p99_source": "historical log" if lat_p99 else "missing: historical sysbench used percentile=95 without histogram",
        })
    return run_rows, case_rows


def collect_metric_history(result_dir: Path, tag: str, threads: list[int]) -> list[dict[str, Any]]:
    src = result_dir / "summary_metrics_cases.csv"
    rows = []
    for r in read_csv(src):
        if r.get("tag") != tag or r.get("scheduler") != "scx_nest":
            continue
        try:
            t = int(r.get("threads", "0"))
        except ValueError:
            continue
        if t not in threads:
            continue
        rows.append({
            "scheduler": "scx_nest",
            "threads": t,
            "n": r.get("n", ""),
            "ipc_mean": r.get("ipc_mean", ""),
            "ipc_stdev": r.get("ipc_stdev", ""),
            "cpus_utilized_mean": r.get("cpus_utilized_mean", "") or r.get("task_clock_cpus_mean", ""),
            "busy_cpus_mean": r.get("busy_cpus_mean", ""),
            "mig_per_s_mean": r.get("mig_per_s_mean", ""),
            "cs_per_s_mean": r.get("cs_per_s_mean", ""),
            "cache_miss_rate_mean": r.get("cache_miss_rate_mean", ""),
            "failures": "",
            "data_source": "historical",
            "historical_tag": tag,
            "historical_path": str(result_dir / f"{tag}_scx_nest_t{t}_d60"),
            "coverage_note": "historical runs may be fewer than formal target",
        })
    return rows


def merge_rows(existing: list[dict[str, Any]], historical: list[dict[str, Any]], threads: list[int]) -> list[dict[str, Any]]:
    hist_by_t = {int(r["threads"]): r for r in historical}
    old_failures = {
        int(r["threads"]): r.get("failures", "")
        for r in existing
        if r.get("scheduler") == "scx_nest" and str(r.get("threads", "")).isdigit()
    }
    out: list[dict[str, Any]] = []
    seen_threads: set[int] = set()
    for r in existing:
        if r.get("scheduler") == "scx_nest" and str(r.get("threads", "")).isdigit():
            t = int(r["threads"])
            if t in hist_by_t:
                row = dict(hist_by_t[t])
                if old_failures.get(t, "") not in ("", None):
                    row["failures"] = old_failures[t]
                    row["live_failure_note"] = "formal live attempts failed; historical data used for scx_nest"
                out.append(row)
                seen_threads.add(t)
            else:
                out.append(r)
        else:
            out.append(r)
    for t in threads:
        if t in hist_by_t and t not in seen_threads:
            out.append(hist_by_t[t])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Import historical scx_nest MariaDB data into a formal root summary.")
    ap.add_argument("--root", required=True)
    ap.add_argument("--historical-tag", default="retest_2numa")
    ap.add_argument("--threads", default="64 128 192 256")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    result_dir = TEST_DIR / "result"
    threads = [int(x) for x in args.threads.replace(",", " ").split()]

    hist_dir = root / "historical" / "scx_nest"
    perf_runs, perf_cases = collect_perf_history(result_dir, args.historical_tag, threads)
    metric_cases = collect_metric_history(result_dir, args.historical_tag, threads)

    write_csv(hist_dir / "performance_runs.csv", perf_runs)
    write_csv(hist_dir / "performance_cases.csv", perf_cases)
    write_csv(hist_dir / "summary_metrics_cases.csv", metric_cases)

    for rel in ("summary_cases.csv", "performance/summary_cases.csv"):
        p = root / rel
        merged = merge_rows(read_csv(p), perf_cases, threads)
        write_csv(p, merged)

    for rel in ("summary_metrics_cases.csv", "mechanism/summary_metrics_cases.csv"):
        p = root / rel
        merged = merge_rows(read_csv(p), metric_cases, threads)
        write_csv(p, merged)

    (hist_dir / "README.md").write_text(
        "# Historical scx_nest Import\n\n"
        f"- Source tag: `{args.historical_tag}`\n"
        "- Source directories: `test3-SysbenchOLTP /result/retest_2numa_scx_nest_t*_d60/`\n"
        "- These rows are historical data, not live formal reruns.\n"
        "- Historical sysbench logs used `--percentile=95` without histogram, so p99 is unavailable unless present in the raw log.\n"
        "- Some historical scheduler logs contain `scx_bpf_error`; this is recorded through `historical_scheduler_error_runs` and should be discussed as an instability caveat.\n"
    )
    print(hist_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

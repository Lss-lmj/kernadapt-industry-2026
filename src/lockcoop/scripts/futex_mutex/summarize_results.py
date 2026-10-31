#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Optional


@dataclass(frozen=True)
class RunPoint:
    tag: str
    scheduler: str
    threads: int
    scenario: str
    run: int
    throughput_ops_s: float
    ops_min_per_thread: Optional[float]
    ops_max_per_thread: Optional[float]
    ops_mean_per_thread: Optional[float]
    zero_progress_threads: Optional[float]
    jain_fairness: Optional[float]
    latency_avg_us: float
    latency_p50_us: float
    latency_p95_us: float
    latency_p99_us: float
    latency_p999_us: float
    latency_max_us: float
    path: str


FLOAT_RE = r"([0-9]+(?:\.[0-9]+)?)"
THROUGHPUT_RE = re.compile(rf"^Throughput\s*:\s*{FLOAT_RE}\s+ops/s\s*$")
OPS_MIN_RE = re.compile(rf"^Ops/thread min\s*:\s*{FLOAT_RE}\s*$")
OPS_MAX_RE = re.compile(rf"^Ops/thread max\s*:\s*{FLOAT_RE}\s*$")
OPS_MEAN_RE = re.compile(rf"^Ops/thread mean\s*:\s*{FLOAT_RE}\s*$")
ZERO_PROGRESS_RE = re.compile(rf"^Zero-progress thr\s*:\s*{FLOAT_RE}\s*$")
JAIN_RE = re.compile(rf"^Jain fairness\s*:\s*{FLOAT_RE}\s*$")
LAT_RE = {
    "latency_avg_us": re.compile(rf"^Latency avg\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p50_us": re.compile(rf"^Latency p50\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p95_us": re.compile(rf"^Latency p95\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p99_us": re.compile(rf"^Latency p99\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p999_us": re.compile(rf"^Latency p99\.9\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_max_us": re.compile(rf"^Latency max\s*:\s*{FLOAT_RE}\s+us\s*$"),
}


def parse_metrics(path: Path) -> Optional[dict[str, float]]:
    values: dict[str, float] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    for line in lines:
        stripped = line.strip()
        m = THROUGHPUT_RE.match(stripped)
        if m:
            values["throughput_ops_s"] = float(m.group(1))
            continue
        m = OPS_MIN_RE.match(stripped)
        if m:
            values["ops_min_per_thread"] = float(m.group(1))
            continue
        m = OPS_MAX_RE.match(stripped)
        if m:
            values["ops_max_per_thread"] = float(m.group(1))
            continue
        m = OPS_MEAN_RE.match(stripped)
        if m:
            values["ops_mean_per_thread"] = float(m.group(1))
            continue
        m = ZERO_PROGRESS_RE.match(stripped)
        if m:
            values["zero_progress_threads"] = float(m.group(1))
            continue
        m = JAIN_RE.match(stripped)
        if m:
            values["jain_fairness"] = float(m.group(1))
            continue
        for key, regex in LAT_RE.items():
            m = regex.match(stripped)
            if m:
                values[key] = float(m.group(1))

    required = {
        "throughput_ops_s",
        "latency_avg_us",
        "latency_p50_us",
        "latency_p95_us",
        "latency_p99_us",
        "latency_p999_us",
        "latency_max_us",
    }
    if required.issubset(values):
        return values
    return None


def find_points(result_root: Path, tag_filter: Optional[str]) -> list[RunPoint]:
    points: list[RunPoint] = []

    for f in result_root.rglob("*.txt"):
        rel = f.relative_to(result_root)
        # Expect: <tag>/<scheduler>_t<T>/<scenario>.r<R>.txt
        if len(rel.parts) != 3:
            continue
        tag, case_dir, fname = rel.parts
        if tag_filter and tag != tag_filter:
            continue
        if fname.startswith("meta") or fname.startswith("scx."):
            continue

        if "_t" not in case_dir:
            continue
        sched, threads_s = case_dir.rsplit("_t", 1)
        try:
            threads = int(threads_s)
        except ValueError:
            continue

        m = re.match(r"^(?P<scenario>.+)\.r(?P<run>[0-9]+)\.txt$", fname)
        if not m:
            continue
        scenario = m.group("scenario")
        run = int(m.group("run"))

        metrics = parse_metrics(f)
        if metrics is None:
            continue

        points.append(
            RunPoint(
                tag=tag,
                scheduler=sched,
                threads=threads,
                scenario=scenario,
                run=run,
                throughput_ops_s=metrics["throughput_ops_s"],
                ops_min_per_thread=metrics.get("ops_min_per_thread"),
                ops_max_per_thread=metrics.get("ops_max_per_thread"),
                ops_mean_per_thread=metrics.get("ops_mean_per_thread"),
                zero_progress_threads=metrics.get("zero_progress_threads"),
                jain_fairness=metrics.get("jain_fairness"),
                latency_avg_us=metrics["latency_avg_us"],
                latency_p50_us=metrics["latency_p50_us"],
                latency_p95_us=metrics["latency_p95_us"],
                latency_p99_us=metrics["latency_p99_us"],
                latency_p999_us=metrics["latency_p999_us"],
                latency_max_us=metrics["latency_max_us"],
                path=str(f),
            )
        )

    return points


def fmt_pct(value: float) -> str:
    return f"{value:+.1f}%"


def metric_values(rows: list[RunPoint], attr: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = getattr(row, attr)
        if value is None:
            continue
        out.append(float(value))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize test8 futex/mutex benchmark results")
    ap.add_argument("--tag", default=None, help="Only summarize a specific TAG")
    ap.add_argument("--expected-runs", type=int, default=None, help="Warn if a case has fewer runs")
    ap.add_argument(
        "--output-dir",
        default=None,
        help="Directory for summary CSVs. Default: the shared test8 result directory",
    )
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    if not result_root.exists():
        print(f"Result root not found: {result_root}")
        return 1

    points = find_points(result_root, args.tag)

    tags = sorted({p.tag for p in points})
    scheds = sorted({p.scheduler for p in points})
    threads_list = sorted({p.threads for p in points})
    scenarios = sorted({p.scenario for p in points})

    print(f"Result root: {result_root}")
    print(f"Tags: {', '.join(tags) if tags else '(none)'}")
    print(f"Schedulers: {', '.join(scheds) if scheds else '(none)'}")
    print(f"Threads: {', '.join(map(str, threads_list)) if threads_list else '(none)'}")
    print(f"Scenarios: {', '.join(scenarios) if scenarios else '(none)'}")
    print()

    output_dir = Path(args.output_dir).resolve() if args.output_dir else result_root
    output_dir.mkdir(parents=True, exist_ok=True)
    out_runs = output_dir / "summary_runs.csv"
    out_cases = output_dir / "summary_cases.csv"

    run_fields = [
        "tag",
        "scheduler",
        "threads",
        "scenario",
        "run",
        "throughput_ops_s",
        "ops_min_per_thread",
        "ops_max_per_thread",
        "ops_mean_per_thread",
        "zero_progress_threads",
        "jain_fairness",
        "latency_avg_us",
        "latency_p50_us",
        "latency_p95_us",
        "latency_p99_us",
        "latency_p999_us",
        "latency_max_us",
        "path",
    ]

    with out_runs.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=run_fields)
        writer.writeheader()
        for p in sorted(points, key=lambda x: (x.tag, x.scenario, x.threads, x.scheduler, x.run)):
            writer.writerow(
                {
                    field: (
                        getattr(p, field)
                        if field != "path" and getattr(p, field) is not None
                        else (p.path if field == "path" else "")
                    )
                    for field in run_fields
                }
            )

    cases: dict[tuple[str, str, int, str], list[RunPoint]] = {}
    max_run = 0
    for p in points:
        cases.setdefault((p.tag, p.scheduler, p.threads, p.scenario), []).append(p)
        max_run = max(max_run, p.run)
    if args.expected_runs is not None:
        max_run = max(max_run, args.expected_runs)

    run_cols: list[str] = []
    for idx in range(1, max_run + 1):
        run_cols.extend(
            [
                f"r{idx}_throughput_ops_s",
                f"r{idx}_jain_fairness",
                f"r{idx}_zero_progress_threads",
                f"r{idx}_latency_avg_us",
                f"r{idx}_latency_p99_us",
                f"r{idx}_latency_max_us",
            ]
        )

    case_fields = [
        "tag",
        "scheduler",
        "threads",
        "scenario",
        "n",
        "throughput_mean",
        "throughput_stdev",
        "throughput_cv",
        "throughput_best",
        "throughput_worst",
        "ops_min_per_thread_mean",
        "ops_max_per_thread_mean",
        "ops_mean_per_thread_mean",
        "zero_progress_threads_mean",
        "zero_progress_threads_max",
        "jain_fairness_mean",
        "jain_fairness_best",
        "jain_fairness_worst",
        "latency_avg_mean_us",
        "latency_p50_mean_us",
        "latency_p95_mean_us",
        "latency_p99_mean_us",
        "latency_p999_mean_us",
        "latency_max_mean_us",
        "latency_p99_best_us",
        "latency_max_best_us",
        *run_cols,
    ]

    with out_cases.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=case_fields)
        writer.writeheader()
        for (tag, sched, threads, scenario), rows in sorted(cases.items(), key=lambda item: item[0]):
            throughput = metric_values(rows, "throughput_ops_s")
            tmean = mean(throughput)
            tstdev = pstdev(throughput) if len(throughput) >= 2 else 0.0
            tcv = tstdev / tmean if tmean != 0 else math.nan
            ops_min = metric_values(rows, "ops_min_per_thread")
            ops_max = metric_values(rows, "ops_max_per_thread")
            ops_mean = metric_values(rows, "ops_mean_per_thread")
            zero_progress = metric_values(rows, "zero_progress_threads")
            jain = metric_values(rows, "jain_fairness")
            row = {
                "tag": tag,
                "scheduler": sched,
                "threads": threads,
                "scenario": scenario,
                "n": len(rows),
                "throughput_mean": f"{tmean:.6f}",
                "throughput_stdev": f"{tstdev:.6f}",
                "throughput_cv": f"{tcv:.6f}",
                "throughput_best": f"{max(throughput):.6f}",
                "throughput_worst": f"{min(throughput):.6f}",
                "ops_min_per_thread_mean": f"{mean(ops_min):.6f}" if ops_min else "",
                "ops_max_per_thread_mean": f"{mean(ops_max):.6f}" if ops_max else "",
                "ops_mean_per_thread_mean": f"{mean(ops_mean):.6f}" if ops_mean else "",
                "zero_progress_threads_mean": f"{mean(zero_progress):.6f}" if zero_progress else "",
                "zero_progress_threads_max": f"{max(zero_progress):.6f}" if zero_progress else "",
                "jain_fairness_mean": f"{mean(jain):.6f}" if jain else "",
                "jain_fairness_best": f"{max(jain):.6f}" if jain else "",
                "jain_fairness_worst": f"{min(jain):.6f}" if jain else "",
                "latency_avg_mean_us": f"{mean(metric_values(rows, 'latency_avg_us')):.6f}",
                "latency_p50_mean_us": f"{mean(metric_values(rows, 'latency_p50_us')):.6f}",
                "latency_p95_mean_us": f"{mean(metric_values(rows, 'latency_p95_us')):.6f}",
                "latency_p99_mean_us": f"{mean(metric_values(rows, 'latency_p99_us')):.6f}",
                "latency_p999_mean_us": f"{mean(metric_values(rows, 'latency_p999_us')):.6f}",
                "latency_max_mean_us": f"{mean(metric_values(rows, 'latency_max_us')):.6f}",
                "latency_p99_best_us": f"{min(metric_values(rows, 'latency_p99_us')):.6f}",
                "latency_max_best_us": f"{min(metric_values(rows, 'latency_max_us')):.6f}",
            }

            by_run = {p.run: p for p in rows}
            for idx in range(1, max_run + 1):
                p = by_run.get(idx)
                row[f"r{idx}_throughput_ops_s"] = f"{p.throughput_ops_s:.6f}" if p else ""
                row[f"r{idx}_jain_fairness"] = f"{p.jain_fairness:.6f}" if p and p.jain_fairness is not None else ""
                row[f"r{idx}_zero_progress_threads"] = (
                    f"{p.zero_progress_threads:.6f}" if p and p.zero_progress_threads is not None else ""
                )
                row[f"r{idx}_latency_avg_us"] = f"{p.latency_avg_us:.6f}" if p else ""
                row[f"r{idx}_latency_p99_us"] = f"{p.latency_p99_us:.6f}" if p else ""
                row[f"r{idx}_latency_max_us"] = f"{p.latency_max_us:.6f}" if p else ""
            writer.writerow(row)

    if args.expected_runs is not None:
        for (tag, sched, threads, scenario), rows in cases.items():
            if len(rows) < args.expected_runs:
                print(
                    f"WARNING: {tag} {sched} threads={threads} {scenario}: "
                    f"only {len(rows)} runs (expected {args.expected_runs})"
                )
        print()

    for tag in tags:
        print(f"== Tag: {tag} ==")
        for scenario in sorted({p.scenario for p in points if p.tag == tag}):
            print(f"-- {scenario} --")
            for threads in sorted({p.threads for p in points if p.tag == tag and p.scenario == scenario}):
                candidates = []
                for sched in scheds:
                    key = (tag, sched, threads, scenario)
                    if key in cases:
                        rows = cases[key]
                        candidates.append((mean(metric_values(rows, "throughput_ops_s")), sched, rows))
                if not candidates:
                    continue

                best_tput, best_sched, best_rows = max(candidates, key=lambda item: item[0])
                best_p99_sched = min(
                    candidates,
                    key=lambda item: mean(metric_values(item[2], "latency_p99_us")),
                )
                fairness_candidates = [item for item in candidates if metric_values(item[2], "jain_fairness")]
                best_fair_sched = (
                    max(fairness_candidates, key=lambda item: mean(metric_values(item[2], "jain_fairness")))
                    if fairness_candidates
                    else None
                )
                zero_candidates = [item for item in candidates if metric_values(item[2], "zero_progress_threads")]
                best_zero_sched = (
                    min(zero_candidates, key=lambda item: mean(metric_values(item[2], "zero_progress_threads")))
                    if zero_candidates
                    else None
                )
                base_key = (tag, "default", threads, scenario)
                base_tput = mean(metric_values(cases[base_key], "throughput_ops_s")) if base_key in cases else None

                extra = ""
                if base_tput:
                    extra = f" ({fmt_pct((best_tput / base_tput - 1.0) * 100.0)} vs default)"
                msg = (
                    f"  t={threads}: throughput best {best_sched} {best_tput:.2f} ops/s{extra}; "
                    f"p99 best {best_p99_sched[1]} "
                    f"{mean(metric_values(best_p99_sched[2], 'latency_p99_us')):.2f} us"
                )
                if best_fair_sched is not None:
                    msg += (
                        f"; fairness best {best_fair_sched[1]} "
                        f"{mean(metric_values(best_fair_sched[2], 'jain_fairness')):.4f}"
                    )
                if best_zero_sched is not None:
                    msg += (
                        f"; zero-progress best {best_zero_sched[1]} "
                        f"{mean(metric_values(best_zero_sched[2], 'zero_progress_threads')):.2f}"
                    )
                print(msg)
            print()

    print(f"Wrote: {out_runs}")
    print(f"Wrote: {out_cases}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

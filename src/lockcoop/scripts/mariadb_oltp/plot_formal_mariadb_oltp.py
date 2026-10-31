#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import statistics
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


SCHED_ORDER = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_nest",
    "scx_lockboost",
    "scx_locknest",
    "scx_lockcoop",
]

COLORS = {
    "default": "#4c4c4c",
    "scx_simple": "#1f77b4",
    "scx_lcas": "#9467bd",
    "scx_cfl": "#8c564b",
    "scx_nest": "#7f7f7f",
    "scx_lockboost": "#d62728",
    "scx_locknest": "#2ca02c",
    "scx_lockcoop": "#ff7f0e",
}

MARKERS = {
    "default": "o",
    "scx_simple": "s",
    "scx_lcas": "X",
    "scx_cfl": "8",
    "scx_nest": "<",
    "scx_lockboost": "D",
    "scx_locknest": "^",
    "scx_lockcoop": "P",
}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def fnum(value: str | None) -> float:
    if value in (None, ""):
        return math.nan
    try:
        return float(value)
    except ValueError:
        return math.nan


def scheds_in(rows: list[dict[str, str]]) -> list[str]:
    present = {r["scheduler"] for r in rows if r.get("scheduler")}
    return [s for s in SCHED_ORDER if s in present] + sorted(present - set(SCHED_ORDER))


def threads_in(rows: list[dict[str, str]]) -> list[int]:
    return sorted({int(r["threads"]) for r in rows if r.get("threads")})


def grouped_bar(rows: list[dict[str, str]], metric: str, title: str, ylabel: str, out: Path) -> None:
    scheds = scheds_in(rows)
    threads = threads_in(rows)
    by = {(r["scheduler"], int(r["threads"])): fnum(r.get(metric)) for r in rows}
    fig, ax = plt.subplots(figsize=(12, 5.6))
    width = min(0.82 / max(1, len(scheds)), 0.11)
    xs = list(range(len(threads)))
    for i, sched in enumerate(scheds):
        offset = (i - (len(scheds) - 1) / 2) * width
        vals = [by.get((sched, t), math.nan) for t in threads]
        ax.bar([x + offset for x in xs], vals, width=width, label=sched, color=COLORS.get(sched))
    ax.set_title(title)
    ax.set_xlabel("threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(xs)
    ax.set_xticklabels([str(t) for t in threads])
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=4, fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def line_chart(rows: list[dict[str, str]], metric: str, title: str, ylabel: str, out: Path, *, logy: bool = False) -> None:
    scheds = scheds_in(rows)
    threads = threads_in(rows)
    by = {(r["scheduler"], int(r["threads"])): fnum(r.get(metric)) for r in rows}
    all_vals = [v for v in by.values() if not math.isnan(v)]
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    for sched in scheds:
        vals = [by.get((sched, t), math.nan) for t in threads]
        ax.plot(
            threads,
            vals,
            marker=MARKERS.get(sched, "o"),
            linewidth=2,
            label=sched,
            color=COLORS.get(sched),
        )
    ax.set_title(title)
    ax.set_xlabel("threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(threads)
    if logy and any(v > 0 for v in all_vals):
        ax.set_yscale("log")
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=4, fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def ratio_chart(rows: list[dict[str, str]], out: Path, *, title: str = "MariaDB OLTP TPS ratio vs default") -> None:
    scheds = scheds_in(rows)
    threads = threads_in(rows)
    by = {(r["scheduler"], int(r["threads"])): fnum(r.get("tps_mean")) for r in rows}
    fig, ax = plt.subplots(figsize=(10.5, 5.8))
    for sched in scheds:
        vals = []
        for t in threads:
            base = by.get(("default", t), math.nan)
            cur = by.get((sched, t), math.nan)
            vals.append(cur / base if base and not math.isnan(base) and not math.isnan(cur) else math.nan)
        ax.plot(threads, vals, marker=MARKERS.get(sched, "o"), linewidth=2, label=sched, color=COLORS.get(sched))
    ax.axhline(1.0, color="#222222", linestyle="--", linewidth=1)
    ax.set_title(title)
    ax.set_xlabel("threads")
    ax.set_ylabel("TPS / default TPS")
    ax.set_xticks(threads)
    ax.grid(axis="y", alpha=0.25)
    ax.legend(ncol=4, fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=180)
    plt.close(fig)


def performance_rows_for_run(root: Path, run_id: int) -> list[dict[str, str]]:
    rows = read_csv(root / "performance" / "summary_runs.csv")
    out: list[dict[str, str]] = []
    for row in rows:
        if row.get("valid") != "1" or row.get("run_id") != str(run_id):
            continue
        out.append({
            "scheduler": row.get("scheduler", ""),
            "threads": row.get("threads", ""),
            "n": "1",
            "tps_mean": row.get("tps", ""),
            "tps_stdev": "0",
            "tps_cv": "0",
            "lat_avg_mean": row.get("lat_avg", ""),
            "lat_p95_mean": row.get("lat_p95", ""),
            "lat_p99_mean": row.get("lat_p99", ""),
            "qps_mean": row.get("qps", ""),
            "failures": "0",
        })
    return out


def performance_rows_through_run(root: Path, max_run_id: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    selected = [
        row for row in read_csv(root / "performance" / "summary_runs.csv")
        if row.get("valid") == "1" and int(row.get("run_id", "0") or 0) <= max_run_id
    ]
    groups: dict[tuple[str, int], list[dict[str, str]]] = {}
    for row in selected:
        groups.setdefault((row["scheduler"], int(row["threads"])), []).append(row)

    def values(rows: list[dict[str, str]], field: str) -> list[float]:
        return [fnum(row.get(field)) for row in rows if not math.isnan(fnum(row.get(field)))]

    def mean(rows: list[dict[str, str]], field: str) -> float:
        vals = values(rows, field)
        return statistics.mean(vals) if vals else math.nan

    cases: list[dict[str, str]] = []
    scheduler_rank = {scheduler: index for index, scheduler in enumerate(SCHED_ORDER)}
    for (scheduler, threads), rows in sorted(
        groups.items(), key=lambda item: (scheduler_rank.get(item[0][0], len(SCHED_ORDER)), item[0][1])
    ):
        tps = values(rows, "tps")
        tps_mean = statistics.mean(tps) if tps else math.nan
        tps_stdev = statistics.pstdev(tps) if len(tps) >= 2 else 0.0
        cases.append({
            "scheduler": scheduler,
            "threads": str(threads),
            "n": str(len(rows)),
            "tps_mean": f"{tps_mean:.6f}",
            "tps_stdev": f"{tps_stdev:.6f}",
            "tps_cv": f"{tps_stdev / tps_mean:.6f}" if tps_mean else "",
            "lat_avg_mean": f"{mean(rows, 'lat_avg'):.6f}",
            "lat_p95_mean": f"{mean(rows, 'lat_p95'):.6f}",
            "lat_p99_mean": f"{mean(rows, 'lat_p99'):.6f}",
            "qps_mean": f"{mean(rows, 'qps'):.6f}",
            "failures": "0",
        })
    return cases, selected


def write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot formal MariaDB OLTP summaries.")
    ap.add_argument("--root", required=True)
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--performance-run-id", type=int, default=None, help="Plot one completed performance run only")
    scope.add_argument("--performance-max-run-id", type=int, default=None, help="Aggregate performance runs up to this ID")
    ap.add_argument("--output-dir", default=None, help="Directory for generated plots")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if args.output_dir:
        out = Path(args.output_dir).resolve()
    elif args.performance_run_id is not None:
        out = root / f"plots_round{args.performance_run_id}"
    elif args.performance_max_run_id is not None:
        out = root / f"plots_first{args.performance_max_run_id}"
    else:
        out = root / "plots"
    out.mkdir(parents=True, exist_ok=True)

    if args.performance_run_id is not None:
        perf_rows = performance_rows_for_run(root, args.performance_run_id)
        write_rows(out / f"performance_round{args.performance_run_id}.csv", perf_rows)
        title_suffix = f" (round {args.performance_run_id}, single run)"
    elif args.performance_max_run_id is not None:
        perf_rows, selected_runs = performance_rows_through_run(root, args.performance_max_run_id)
        write_rows(out / f"performance_first{args.performance_max_run_id}_cases.csv", perf_rows)
        write_rows(out / f"performance_first{args.performance_max_run_id}_runs.csv", selected_runs)
        title_suffix = f" (first {args.performance_max_run_id} runs, mean)"
    else:
        perf_rows = read_csv(root / "summary_cases.csv")
        title_suffix = ""
    if perf_rows:
        grouped_bar(perf_rows, "tps_mean", "MariaDB/Sysbench OLTP throughput" + title_suffix, "TPS", out / "performance_tps.png")
        ratio_chart(perf_rows, out / "performance_tps_ratio.png", title="MariaDB OLTP TPS ratio vs default" + title_suffix)
        line_chart(perf_rows, "lat_p95_mean", "MariaDB/Sysbench OLTP p95 latency" + title_suffix, "p95 latency (ms)", out / "performance_p95.png", logy=True)
        line_chart(perf_rows, "lat_p99_mean", "MariaDB/Sysbench OLTP p99 latency" + title_suffix, "p99 latency (ms)", out / "performance_p99.png", logy=True)

    performance_snapshot = args.performance_run_id is not None or args.performance_max_run_id is not None
    metric_rows = [] if performance_snapshot else read_csv(root / "summary_metrics_cases.csv")
    if metric_rows:
        line_chart(metric_rows, "ipc_mean", "MariaDB mechanism: IPC", "IPC", out / "mechanism_ipc.png")
        line_chart(metric_rows, "cpus_utilized_mean", "MariaDB mechanism: CPU utilized", "effective CPUs", out / "mechanism_cpus_utilized.png")
        line_chart(metric_rows, "mig_per_s_mean", "MariaDB mechanism: migrations", "migrations/s", out / "mechanism_migrations.png", logy=True)
        line_chart(metric_rows, "cs_per_s_mean", "MariaDB mechanism: context switches", "context switches/s", out / "mechanism_context_switches.png", logy=True)
        line_chart(metric_rows, "cache_miss_rate_mean", "MariaDB mechanism: cache miss rate", "cache misses / references", out / "mechanism_cache_miss_rate.png")

    readme = "# Formal MariaDB OLTP Plots\n\n"
    if args.performance_run_id is not None:
        readme += f"- Performance data is limited to run {args.performance_run_id}; each point is one trial.\n"
    elif args.performance_max_run_id is not None:
        readme += f"- Performance data aggregates valid runs 1 through {args.performance_max_run_id}.\n"
    readme += (
        "- `performance_tps.png`: TPS grouped bar chart.\n"
        "- `performance_tps_ratio.png`: TPS ratio versus default.\n"
        "- `performance_p95.png`: p95 latency line chart.\n"
        "- `performance_p99.png`: p99 latency line chart.\n"
        "- `mechanism_*.png`: perf/numastat mechanism metrics when mechanism phase is available.\n"
    )
    (out / "README.md").write_text(readme)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

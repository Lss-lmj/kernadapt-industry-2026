#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Callable

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


@dataclass(frozen=True)
class Point:
    tag: str
    scheduler: str
    threads: int
    scenario: str
    run: int
    throughput_ops_s: float
    ops_min_per_thread: float | None
    ops_max_per_thread: float | None
    ops_mean_per_thread: float | None
    zero_progress_threads: float | None
    jain_fairness: float | None
    latency_avg_us: float
    latency_p50_us: float
    latency_p95_us: float
    latency_p99_us: float
    latency_p999_us: float
    latency_max_us: float


COLOR_MAP = {
    "default": "#1f77b4",
    "scx_simple": "#17becf",
    "scx_eevdf": "#000000",
    "scx_lockboost": "#d62728",
    "scx_lockcoop": "#ff7f0e",
    "scx_hybrid": "#8c564b",
    "scx_lcas": "#e377c2",
    "scx_nest": "#7f7f7f",
    "scx_qmap": "#bcbd22",
    "scx_central": "#2ca02c",
    "scx_flatcg": "#9467bd",
    "scx_cfl": "#004488",
}

MARKER_MAP = {
    "default": "o",
    "scx_simple": "h",
    "scx_eevdf": "*",
    "scx_lockboost": "D",
    "scx_lockcoop": "s",
    "scx_hybrid": "P",
    "scx_lcas": "X",
    "scx_nest": "<",
    "scx_qmap": ">",
    "scx_central": "^",
    "scx_flatcg": "v",
    "scx_cfl": "8",
}

METRICS: dict[str, tuple[str, str, bool]] = {
    "throughput": ("throughput_ops_s", "Throughput (ops/s)", True),
    "ops_min": ("ops_min_per_thread", "Min ops per thread", True),
    "ops_max": ("ops_max_per_thread", "Max ops per thread", True),
    "ops_mean": ("ops_mean_per_thread", "Mean ops per thread", True),
    "zero": ("zero_progress_threads", "Zero-progress threads", False),
    "jain": ("jain_fairness", "Jain fairness index", True),
    "avg": ("latency_avg_us", "Average latency (us)", False),
    "p50": ("latency_p50_us", "p50 latency (us)", False),
    "p95": ("latency_p95_us", "p95 latency (us)", False),
    "p99": ("latency_p99_us", "p99 latency (us)", False),
    "p999": ("latency_p999_us", "p99.9 latency (us)", False),
    "max": ("latency_max_us", "Max latency (us)", False),
}


def style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "font.size": 12,
            "axes.titlesize": 14,
            "axes.labelsize": 13,
            "legend.fontsize": 10,
            "xtick.labelsize": 12,
            "ytick.labelsize": 12,
            "lines.linewidth": 2.2,
            "lines.markersize": 7,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linestyle": "-",
            "axes.spines.top": True,
            "axes.spines.right": True,
        }
    )


def load_runs(path: Path) -> list[Point]:
    points: list[Point] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            points.append(
                Point(
                    tag=row["tag"],
                    scheduler=row["scheduler"],
                    threads=int(row["threads"]),
                    scenario=row["scenario"],
                    run=int(row["run"]),
                    throughput_ops_s=float(row["throughput_ops_s"]),
                    ops_min_per_thread=float(row["ops_min_per_thread"]) if row.get("ops_min_per_thread") else None,
                    ops_max_per_thread=float(row["ops_max_per_thread"]) if row.get("ops_max_per_thread") else None,
                    ops_mean_per_thread=float(row["ops_mean_per_thread"]) if row.get("ops_mean_per_thread") else None,
                    zero_progress_threads=float(row["zero_progress_threads"]) if row.get("zero_progress_threads") else None,
                    jain_fairness=float(row["jain_fairness"]) if row.get("jain_fairness") else None,
                    latency_avg_us=float(row["latency_avg_us"]),
                    latency_p50_us=float(row["latency_p50_us"]),
                    latency_p95_us=float(row["latency_p95_us"]),
                    latency_p99_us=float(row["latency_p99_us"]),
                    latency_p999_us=float(row["latency_p999_us"]),
                    latency_max_us=float(row["latency_max_us"]),
                )
            )
    return points


def aggregate(points: list[Point], attr: str, mode: str, higher_is_better: bool) -> dict[tuple[str, str, int], float]:
    grouped: dict[tuple[str, str, int], list[float]] = defaultdict(list)
    for point in points:
        value = getattr(point, attr)
        if value is None:
            continue
        grouped[(point.scenario, point.scheduler, point.threads)].append(float(value))

    out: dict[tuple[str, str, int], float] = {}
    for key, values in grouped.items():
        if mode == "mean":
            out[key] = mean(values)
        elif higher_is_better:
            out[key] = max(values)
        else:
            out[key] = min(values)
    return out


def draw_metric(
    points: list[Point],
    out_dir: Path,
    metric: str,
    mode: str,
    scheduler_order: list[str],
) -> None:
    attr, ylabel, higher_is_better = METRICS[metric]
    scenarios = sorted({p.scenario for p in points})
    threads_list = sorted({p.threads for p in points})
    agg = aggregate(points, attr, mode, higher_is_better)

    for scenario in scenarios:
        if not any((scenario, sched, threads) in agg for sched in scheduler_order for threads in threads_list):
            continue
        fig, ax = plt.subplots(figsize=(9.2, 3.8))
        fig.subplots_adjust(right=0.78)

        for idx, sched in enumerate(scheduler_order):
            xs: list[int] = []
            ys: list[float] = []
            for threads in threads_list:
                key = (scenario, sched, threads)
                if key in agg:
                    xs.append(threads)
                    ys.append(agg[key])
            if not xs:
                continue
            ax.plot(
                xs,
                ys,
                marker=MARKER_MAP.get(sched, "o"),
                label=sched,
                color=COLOR_MAP.get(sched) or plt.get_cmap("tab10")(idx % 10),
            )

        ax.set_title(f"{scenario} ({mode})")
        ax.set_xlabel("Threads")
        ax.set_ylabel(ylabel)
        ax.set_xticks(threads_list)
        ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False)
        fig.tight_layout()

        out_path = out_dir / f"{scenario}.{metric}.{mode}.png"
        fig.savefig(out_path, bbox_inches="tight")
        plt.close(fig)
        print(f"Wrote: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot test8 futex/mutex results to PNG")
    ap.add_argument("--tag", required=True, help="TAG to plot")
    ap.add_argument("--mode", choices=["mean", "best", "both"], default="both")
    ap.add_argument(
        "--metrics",
        default="throughput,avg,p99,p999,max",
        help="Comma-separated metrics: throughput,ops_min,ops_max,ops_mean,zero,jain,avg,p50,p95,p99,p999,max",
    )
    ap.add_argument("--summary-runs", default=None, help="Input summary_runs.csv")
    ap.add_argument("--output-dir", default=None, help="Directory for generated PNG files")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    runs_csv = Path(args.summary_runs).resolve() if args.summary_runs else result_root / "summary_runs.csv"
    if not runs_csv.exists():
        print(f"Missing {runs_csv}. Run summarize_results.py first.")
        return 1

    points = [p for p in load_runs(runs_csv) if p.tag == args.tag]
    if not points:
        print(f"No data for tag={args.tag} in {runs_csv}")
        return 1

    selected_metrics = [m.strip() for m in args.metrics.split(",") if m.strip()]
    unknown = [m for m in selected_metrics if m not in METRICS]
    if unknown:
        print(f"Unknown metrics: {', '.join(unknown)}")
        return 1

    style()

    scheds = sorted({p.scheduler for p in points})
    scheduler_order = ["default"] + [s for s in scheds if s != "default"] if "default" in scheds else scheds
    out_dir = Path(args.output_dir).resolve() if args.output_dir else result_root / "plots_png" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    modes = ["mean", "best"] if args.mode == "both" else [args.mode]
    for mode in modes:
        for metric in selected_metrics:
            draw_metric(points, out_dir, metric, mode, scheduler_order)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

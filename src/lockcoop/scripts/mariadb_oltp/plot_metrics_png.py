#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


@dataclass(frozen=True)
class MetricPoint:
    tag: str
    scheduler: str
    threads: int
    duration_s: int
    run: int
    task_clock_cpus: float | None
    cpus_utilized: float | None
    system_busy_ratio: float | None
    busy_cpus: float | None
    cs_per_s: float | None
    mig_per_s: float | None
    ipc: float | None
    cache_miss_rate: float | None
    numastat_node0_ratio: float | None
    numa_pages_n0_ratio: float | None


COLOR_MAP = {
    "default": "#1f77b4",
    "scx_lockcoop": "#ff7f0e",
    "scx_central": "#2ca02c",
    "scx_lockboost": "#d62728",
    "scx_flatcg": "#9467bd",
    "scx_hybrid": "#8c564b",
    "scx_lcas": "#e377c2",
    "scx_nest": "#7f7f7f",
    "scx_qmap": "#bcbd22",
    "scx_simple": "#17becf",
    "scx_eevdf": "#000000",
}

MARKER_MAP = {
    "default": "o",
    "scx_lockcoop": "s",
    "scx_central": "^",
    "scx_lockboost": "D",
    "scx_flatcg": "v",
    "scx_hybrid": "P",
    "scx_lcas": "X",
    "scx_nest": "<",
    "scx_qmap": ">",
    "scx_simple": "h",
    "scx_eevdf": "*",
}

SCHED_ORDER = [
    "default",
    "scx_simple",
    "scx_eevdf",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_hybrid",
    "scx_lcas",
    "scx_qmap",
    "scx_central",
    "scx_flatcg",
    "scx_nest",
]


def style() -> None:
    plt.rcParams.update(
        {
            "figure.dpi": 150,
            "savefig.dpi": 300,
            "font.size": 12,
            "axes.titlesize": 14,
            "axes.labelsize": 13,
            "legend.fontsize": 11,
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


def t_crit_975(dof: int) -> float:
    table = {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
        11: 2.201,
        12: 2.179,
        13: 2.160,
        14: 2.145,
        15: 2.131,
        16: 2.120,
        17: 2.110,
        18: 2.101,
        19: 2.093,
        20: 2.086,
        21: 2.080,
        22: 2.074,
        23: 2.069,
        24: 2.064,
        25: 2.060,
        26: 2.056,
        27: 2.052,
        28: 2.048,
        29: 2.045,
        30: 2.042,
    }
    return table.get(dof, 1.96)


def ordered_schedulers(scheds: set[str]) -> list[str]:
    out: list[str] = []
    for s in SCHED_ORDER:
        if s in scheds:
            out.append(s)
    for s in sorted(scheds):
        if s not in out:
            out.append(s)
    return out


def parse_float(s: str) -> float | None:
    s = s.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def load_points(csv_path: Path, tag: str) -> list[MetricPoint]:
    pts: list[MetricPoint] = []
    with csv_path.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["tag"] != tag:
                continue
            pts.append(
                MetricPoint(
                    tag=row["tag"],
                    scheduler=row["scheduler"],
                    threads=int(row["threads"]),
                    duration_s=int(row["duration_s"]),
                    run=int(row["run"]),
                    task_clock_cpus=parse_float(row.get("task_clock_cpus", "")),
                    cpus_utilized=parse_float(row.get("cpus_utilized", "")),
                    system_busy_ratio=parse_float(row.get("system_busy_ratio", "")),
                    busy_cpus=parse_float(row.get("busy_cpus", "")),
                    cs_per_s=parse_float(row.get("cs_per_s", "")),
                    mig_per_s=parse_float(row.get("mig_per_s", "")),
                    ipc=parse_float(row.get("ipc", "")),
                    cache_miss_rate=parse_float(row.get("cache_miss_rate", "")),
                    numastat_node0_ratio=parse_float(row.get("numastat_node0_ratio", "")),
                    numa_pages_n0_ratio=parse_float(row.get("numa_pages_n0_ratio", "")),
                )
            )
    return pts


def agg_mean_ci(vals: list[float]) -> tuple[float, float]:
    mu = mean(vals)
    if len(vals) <= 1:
        return mu, 0.0
    sd = stdev(vals)
    dof = max(1, len(vals) - 1)
    err = t_crit_975(dof) * sd / math.sqrt(len(vals))
    return mu, err


def plot_metric(
    out_path: Path,
    tag: str,
    pts: list[MetricPoint],
    *,
    metric: str,
    title: str,
    ylabel: str,
    yscale: str = "linear",
) -> None:
    scheds = ordered_schedulers({p.scheduler for p in pts})
    threads = sorted({p.threads for p in pts})

    # (sched, threads) -> values
    data: dict[tuple[str, int], list[float]] = {}
    for p in pts:
        v = getattr(p, metric)
        if v is None:
            continue
        data.setdefault((p.scheduler, p.threads), []).append(float(v))

    fig, ax = plt.subplots(figsize=(9.0, 3.8))
    fig.subplots_adjust(right=0.78)
    plotted = 0

    for idx, sched in enumerate(scheds):
        xs: list[int] = []
        ys: list[float] = []
        yerr: list[float] = []
        for t in threads:
            vals = data.get((sched, t))
            if not vals:
                continue
            mu, err = agg_mean_ci(vals)
            xs.append(t)
            ys.append(mu)
            yerr.append(err)
        if not xs:
            continue
        ax.errorbar(
            xs,
            ys,
            yerr=yerr,
            marker=MARKER_MAP.get(sched, "o"),
            label=sched,
            color=COLOR_MAP.get(sched) or plt.get_cmap("tab10")(idx % 10),
            capsize=3,
        )
        plotted += 1

    ax.set_title(f"{title} (tag={tag}, mean±95%CI)")
    ax.set_xlabel("Threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(threads)
    if yscale == "log":
        ax.set_yscale("log")
    if plotted > 0:
        ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False)
    else:
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center", va="center")

    fig.tight_layout()
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot test3 RCA metrics (perf/numa) to PNG")
    ap.add_argument("--tag", required=True)
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    runs_csv = result_root / "summary_metrics_runs.csv"
    if not runs_csv.exists():
        print(f"Missing {runs_csv}. Run summarize_metrics.py first.")
        return 1

    pts = load_points(runs_csv, args.tag)
    if not pts:
        print(f"No metrics data for tag={args.tag} in {runs_csv}")
        return 1

    style()

    out_dir = result_root / "plots_png" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    plot_metric(
        out_dir / "rca_task_clock_cpus.png",
        args.tag,
        pts,
        metric="task_clock_cpus",
        title="perf task-clock equivalent CPUs (scope-dependent)",
        ylabel="Equivalent CPUs",
    )
    plot_metric(
        out_dir / "rca_cpus_utilized.png",
        args.tag,
        pts,
        metric="cpus_utilized",
        title="PID-scoped CPU time (mariadbd only)",
        ylabel="CPUs utilized",
    )
    plot_metric(
        out_dir / "rca_busy_cpus.png",
        args.tag,
        pts,
        metric="busy_cpus",
        title="System busy CPUs (/proc/stat delta)",
        ylabel="Busy CPUs",
    )
    plot_metric(
        out_dir / "rca_context_switches.png",
        args.tag,
        pts,
        metric="cs_per_s",
        title="Context switches (mariadbd)",
        ylabel="Context switches / s",
        yscale="log",
    )
    plot_metric(
        out_dir / "rca_cpu_migrations.png",
        args.tag,
        pts,
        metric="mig_per_s",
        title="CPU migrations (mariadbd)",
        ylabel="CPU migrations / s",
        yscale="log",
    )
    plot_metric(out_dir / "rca_ipc.png", args.tag, pts, metric="ipc", title="IPC (mariadbd)", ylabel="Instructions / cycle")
    plot_metric(
        out_dir / "rca_cache_miss_rate.png",
        args.tag,
        pts,
        metric="cache_miss_rate",
        title="Cache miss rate (mariadbd)",
        ylabel="Misses / references",
    )
    # NUMA page-placement ratio is best-effort (may be missing if numa_maps wasn't readable).
    plot_metric(
        out_dir / "rca_numastat_node0_ratio.png",
        args.tag,
        pts,
        metric="numastat_node0_ratio",
        title="NUMA memory placement (numastat after)",
        ylabel="Node0 memory ratio",
    )
    plot_metric(
        out_dir / "rca_numa_pages_node0_ratio.png",
        args.tag,
        pts,
        metric="numa_pages_n0_ratio",
        title="NUMA page placement (numa_maps after)",
        ylabel="Node0 pages ratio",
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
class RunPoint:
    tag: str
    scheduler: str
    threads: int
    duration_s: int
    run: int
    tps: float
    qps: float
    lat_avg_ms: float
    lat_p95_ms: float
    lat_max_ms: float


# Keep colors/markers consistent across tests.
COLOR_MAP = {
    "default": "#1f77b4",
    "scx_lockcoop": "#ff7f0e",
    "scx_central": "#2ca02c",
    "scx_cfl": "#c44e52",
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
    "scx_cfl": "8",
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
    "scx_cfl",
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
    # 97.5% quantile for Student's t (two-sided 95% CI), dof=1..30.
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


def load_runs(runs_csv: Path, tag: str) -> list[RunPoint]:
    pts: list[RunPoint] = []
    with runs_csv.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if row["tag"] != tag:
                continue
            pts.append(
                RunPoint(
                    tag=row["tag"],
                    scheduler=row["scheduler"],
                    threads=int(row["threads"]),
                    duration_s=int(row["duration_s"]),
                    run=int(row["run"]),
                    tps=float(row["tps"]),
                    qps=float(row["qps"]),
                    lat_avg_ms=float(row["lat_avg_ms"]),
                    lat_p95_ms=float(row["lat_p95_ms"]),
                    lat_max_ms=float(row["lat_max_ms"]),
                )
            )
    return pts


def ordered_schedulers(scheds: set[str]) -> list[str]:
    out: list[str] = []
    for s in SCHED_ORDER:
        if s in scheds:
            out.append(s)
    for s in sorted(scheds):
        if s not in out:
            out.append(s)
    return out


def agg_y(vals: list[float], agg: str, higher_is_better: bool) -> float:
    if agg == "mean":
        return mean(vals)
    if agg == "best":
        return max(vals) if higher_is_better else min(vals)
    raise ValueError(f"unknown agg: {agg}")


def agg_err(vals: list[float], err: str) -> float:
    if err == "none" or len(vals) <= 1:
        return 0.0
    sd = stdev(vals)
    if err == "stdev":
        return sd
    if err == "ci95":
        dof = max(1, len(vals) - 1)
        return t_crit_975(dof) * sd / math.sqrt(len(vals))
    raise ValueError(f"unknown err: {err}")


def plot_lines(
    out_dir: Path,
    tag: str,
    pts: list[RunPoint],
    *,
    metric: str,
    title: str,
    ylabel: str,
    agg: str,
    err: str,
    higher_is_better: bool,
    yscale: str,
) -> None:
    scheds = ordered_schedulers({p.scheduler for p in pts})
    threads = sorted({p.threads for p in pts})

    # (scheduler, threads) -> values across runs
    series: dict[tuple[str, int], list[float]] = {}
    for p in pts:
        key = (p.scheduler, p.threads)
        v = getattr(p, metric)
        series.setdefault(key, []).append(float(v))

    fig, ax = plt.subplots(figsize=(9.0, 3.8))
    fig.subplots_adjust(right=0.78)

    for idx, sched in enumerate(scheds):
        xs: list[int] = []
        ys: list[float] = []
        yerr: list[float] = []
        for t in threads:
            vals = series.get((sched, t))
            if not vals:
                continue
            xs.append(t)
            ys.append(agg_y(vals, agg, higher_is_better))
            yerr.append(agg_err(vals, err) if agg == "mean" else 0.0)
        if not xs:
            continue

        ax.errorbar(
            xs,
            ys,
            yerr=yerr if err != "none" else None,
            marker=MARKER_MAP.get(sched, "o"),
            label=sched,
            color=COLOR_MAP.get(sched) or plt.get_cmap("tab10")(idx % 10),
            capsize=3 if err != "none" else 0,
        )

    ax.set_title(f"{title} (tag={tag}, agg={agg}{', ci95' if err=='ci95' and agg=='mean' else ''})")
    ax.set_xlabel("Threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(threads)
    if yscale == "log":
        ax.set_yscale("log")

    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False)

    fig.tight_layout()
    out_path = out_dir / f"{metric}.png"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {out_path}")


def plot_box_grid(
    out_dir: Path,
    tag: str,
    pts: list[RunPoint],
    *,
    metric: str,
    title: str,
    ylabel: str,
    yscale: str,
) -> None:
    scheds = ordered_schedulers({p.scheduler for p in pts})
    threads = sorted({p.threads for p in pts})

    # (threads, sched) -> values across runs
    series: dict[tuple[int, str], list[float]] = {}
    for p in pts:
        series.setdefault((p.threads, p.scheduler), []).append(float(getattr(p, metric)))

    cols = 2 if len(threads) > 1 else 1
    rows = int(math.ceil(len(threads) / cols))
    fig, axes = plt.subplots(rows, cols, figsize=(cols * 9.0, rows * 4.5), squeeze=False)
    fig.suptitle(f"{title} distribution across RUNS (tag={tag})", y=1.02)

    for i, t in enumerate(threads):
        ax = axes[i // cols][i % cols]
        data = []
        labels = []
        colors = []
        for sched in scheds:
            vals = series.get((t, sched))
            if not vals:
                continue
            data.append(vals)
            labels.append(sched)
            colors.append(COLOR_MAP.get(sched, "#cccccc"))

        bp = ax.boxplot(
            data,
            labels=labels,
            patch_artist=True,
            showfliers=True,
            medianprops={"color": "black", "linewidth": 1.2},
        )
        for patch, c in zip(bp["boxes"], colors):
            patch.set_facecolor(c)
            patch.set_alpha(0.65)
            patch.set_edgecolor("#333333")
            patch.set_linewidth(1.0)

        ax.set_title(f"threads={t}")
        ax.set_ylabel(ylabel)
        if yscale == "log":
            ax.set_yscale("log")
        ax.tick_params(axis="x", labelrotation=30)
        ax.grid(True, axis="y", alpha=0.25)

    # Turn off unused axes.
    for j in range(len(threads), rows * cols):
        axes[j // cols][j % cols].axis("off")

    fig.tight_layout()
    out_path = out_dir / f"box_{metric}.png"
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {out_path}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot test3 sysbench results to PNG (mean/CI + boxplots)")
    ap.add_argument("--tag", required=True, help="TAG to plot (e.g., retest_2numa)")
    ap.add_argument("--agg", choices=["mean", "best"], default="mean", help="Aggregation across RUNS")
    ap.add_argument("--err", choices=["none", "stdev", "ci95"], default="ci95", help="Errorbar type (mean only)")
    ap.add_argument(
        "--lat-scale",
        choices=["linear", "log"],
        default="log",
        help="Latency plots y-scale (log is usually needed due to tail blowups)",
    )
    ap.add_argument("--no-boxplots", action="store_true", help="Disable boxplots")
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    runs_csv = result_root / "summary_runs.csv"
    if not runs_csv.exists():
        print(f"Missing {runs_csv}. Run summarize_results.py first.")
        return 1

    pts = load_runs(runs_csv, args.tag)
    if not pts:
        print(f"No data for tag={args.tag} in {runs_csv}")
        return 1

    style()

    out_dir = result_root / "plots_png" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    metrics = [
        ("tps", "Sysbench TPS", "Throughput (transactions/s)", True, "linear"),
        ("qps", "Sysbench QPS", "Throughput (queries/s)", True, "linear"),
        ("lat_avg_ms", "Latency (avg)", "Latency (ms)", False, args.lat_scale),
        ("lat_p95_ms", "Latency (p95)", "Latency (ms)", False, args.lat_scale),
        ("lat_max_ms", "Latency (max)", "Latency (ms)", False, args.lat_scale),
    ]

    for metric, title, ylabel, hib, yscale in metrics:
        plot_lines(
            out_dir,
            args.tag,
            pts,
            metric=metric,
            title=title,
            ylabel=ylabel,
            agg=args.agg,
            err=args.err,
            higher_is_better=hib,
            yscale=yscale,
        )

    if not args.no_boxplots:
        for metric, title, ylabel, yscale in [
            ("tps", "Sysbench TPS", "Throughput (transactions/s)", "linear"),
            ("lat_p95_ms", "Latency (p95)", "Latency (ms)", args.lat_scale),
        ]:
            plot_box_grid(out_dir, args.tag, pts, metric=metric, title=title, ylabel=ylabel, yscale=yscale)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

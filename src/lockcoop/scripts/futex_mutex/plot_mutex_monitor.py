#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


SCHED_ORDER = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
]
COLOR_MAP = {
    "default": "#4d4d4d",
    "scx_simple": "#2ca02c",
    "scx_lcas": "#e377c2",
    "scx_cfl": "#8c564b",
    "scx_lockboost": "#d62728",
    "scx_lockcoop": "#1f77b4",
    "scx_lockcoop": "#ff7f0e",
}
MARKER_MAP = {
    "default": "o",
    "scx_simple": "h",
    "scx_lcas": "v",
    "scx_cfl": "P",
    "scx_lockboost": "D",
    "scx_lockcoop": "s",
    "scx_lockcoop": "^",
}
SCENARIO_TITLES = {
    "mutex_1lock_cs0": "(a) 1 mutex, empty critical section",
    "mutex_1lock_cs1us": "(b) 1 mutex, 1us critical section",
    "mutex_8lock_cs1us": "(c) 8 mutexes, 1us critical section",
}
SCENARIOS = ["mutex_1lock_cs0", "mutex_1lock_cs1us", "mutex_8lock_cs1us"]


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            rows.append(row)
    return rows


def to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def style() -> None:
    plt.rcParams.update({
        "figure.dpi": 150,
        "savefig.dpi": 300,
        "font.size": 10,
        "axes.titlesize": 11,
        "axes.labelsize": 10,
        "legend.fontsize": 9,
        "xtick.labelsize": 9,
        "ytick.labelsize": 9,
        "lines.linewidth": 2.0,
        "lines.markersize": 5.0,
        "axes.grid": True,
        "grid.alpha": 0.22,
        "grid.linestyle": "-",
        "axes.spines.top": True,
        "axes.spines.right": True,
    })


def find_value(rows: list[dict[str, Any]], scenario: str, scheduler: str, threads: int, field: str) -> float | None:
    for row in rows:
        if row.get("scenario") != scenario or row.get("scheduler") != scheduler:
            continue
        if int(row.get("threads") or 0) != threads:
            continue
        if int(row.get("n") or 0) <= 0:
            return None
        return to_float(row.get(field))
    return None


def draw_line(
    ax: Any,
    rows: list[dict[str, Any]],
    scenario: str,
    field: str,
    ylabel: str,
    *,
    log_y: bool = False,
    y_scale: float = 1.0,
) -> None:
    threads = sorted({int(r["threads"]) for r in rows if r.get("scenario") == scenario and int(r.get("n") or 0) > 0})
    scheds = [s for s in SCHED_ORDER if any(r.get("scheduler") == s and r.get("scenario") == scenario for r in rows)]
    for sched in scheds:
        xs: list[int] = []
        ys: list[float] = []
        for t in threads:
            value = find_value(rows, scenario, sched, t, field)
            if value is None:
                continue
            xs.append(t)
            ys.append(value / y_scale)
        if xs:
            ax.plot(xs, ys, marker=MARKER_MAP.get(sched, "o"), color=COLOR_MAP.get(sched), label=sched)
    ax.set_title(SCENARIO_TITLES.get(scenario, scenario))
    ax.set_xlabel("Threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(threads)
    if log_y:
        ax.set_yscale("log")


def plot_performance(root: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    fig, axes = plt.subplots(2, 3, figsize=(12.0, 6.3), sharex=False)
    for col, scenario in enumerate(SCENARIOS):
        draw_line(axes[0][col], rows, scenario, "throughput_ops_s_mean", "Throughput (M ops/s)", y_scale=1_000_000.0)
        draw_line(axes[1][col], rows, scenario, "latency_p999_us_mean", "p99.9 wait (us)", log_y=True)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=8, frameon=False)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out_dir = root / "plots_mutex_monitor"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "mutex_monitor_performance.png"
    pdf = out_dir / "mutex_monitor_performance.pdf"
    fig.savefig(png, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return png, pdf


def plot_mechanism(root: Path, rows: list[dict[str, Any]]) -> tuple[Path, Path]:
    fields = [
        ("owner_unlock_p95_us_mean", "Owner contention-to-unlock p95 (us)", True),
        ("owner_offcpu_p95_us_mean", "Owner off-CPU p95 (us)", True),
        ("owner_preempted_events_per_contention_mean", "Runnable owner switch-outs / contention", False),
    ]
    fig, axes = plt.subplots(len(fields), len(SCENARIOS), figsize=(12.0, 8.3), sharex=False)
    for row_idx, (field, ylabel, log_y) in enumerate(fields):
        for col, scenario in enumerate(SCENARIOS):
            draw_line(axes[row_idx][col], rows, scenario, field, ylabel, log_y=log_y)
    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=8, frameon=False)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    out_dir = root / "plots_mutex_monitor"
    out_dir.mkdir(parents=True, exist_ok=True)
    png = out_dir / "mutex_monitor_mechanism.png"
    pdf = out_dir / "mutex_monitor_mechanism.pdf"
    fig.savefig(png, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    return png, pdf


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot test8 mutex monitor result figures.")
    ap.add_argument("--root", required=True, help="Result root containing summary_mutex_monitor_cases.csv")
    args = ap.parse_args()
    root = Path(args.root).resolve()
    cases = root / "summary_mutex_monitor_cases.csv"
    if not cases.exists():
        raise SystemExit(f"missing {cases}")
    rows = load_rows(cases)
    style()
    for path in plot_performance(root, rows):
        print(f"Wrote: {path}")
    for path in plot_mechanism(root, rows):
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

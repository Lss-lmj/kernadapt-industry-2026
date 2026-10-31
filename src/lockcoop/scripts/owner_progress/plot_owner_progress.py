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
    "scx_eevdf",
]
COLOR_MAP = {
    "default": "#4d4d4d",
    "scx_simple": "#2ca02c",
    "scx_lcas": "#9467bd",
    "scx_cfl": "#8c564b",
    "scx_lockboost": "#d62728",
    "scx_lockcoop": "#1f77b4",
    "scx_eevdf": "#7f7f7f",
}
MARKER_MAP = {
    "default": "o",
    "scx_simple": "h",
    "scx_lcas": "v",
    "scx_cfl": "P",
    "scx_lockboost": "D",
    "scx_lockcoop": "s",
    "scx_eevdf": "X",
}


def load_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
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
        "lines.markersize": 5.5,
        "axes.grid": True,
        "grid.alpha": 0.25,
        "grid.linestyle": "-",
        "axes.spines.top": True,
        "axes.spines.right": True,
    })


def draw_panel(ax: Any, rows: list[dict[str, Any]], field: str, title: str, ylabel: str, log_y: bool = False) -> None:
    threads = sorted({int(r["threads"]) for r in rows if r.get("threads")})
    scheds = [s for s in SCHED_ORDER if any(r.get("scheduler") == s for r in rows)]
    for sched in scheds:
        xs: list[int] = []
        ys: list[float] = []
        for t in threads:
            match = next((r for r in rows if r.get("scheduler") == sched and int(r["threads"]) == t), None)
            if not match:
                continue
            value = to_float(match.get(field))
            n = to_float(match.get("n"))
            if value is None or not n:
                continue
            xs.append(t)
            ys.append(value)
        if xs:
            ax.plot(
                xs,
                ys,
                marker=MARKER_MAP.get(sched, "o"),
                color=COLOR_MAP.get(sched),
                label=sched,
            )
    ax.set_title(title)
    ax.set_xlabel("Threads")
    ax.set_ylabel(ylabel)
    ax.set_xticks(threads)
    if log_y:
        ax.set_yscale("log")


def main() -> int:
    ap = argparse.ArgumentParser(description="Plot owner-progress microbenchmark results.")
    ap.add_argument("--root", required=True, help="Result root containing summary_owner_cases.csv")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    cases_csv = root / "summary_owner_cases.csv"
    if not cases_csv.exists():
        raise SystemExit(f"missing {cases_csv}")
    rows = load_cases(cases_csv)
    if not rows:
        raise SystemExit(f"no rows in {cases_csv}")

    style()
    out_dir = root / "plots_owner_progress"
    out_dir.mkdir(parents=True, exist_ok=True)

    fig, axes = plt.subplots(2, 2, figsize=(10.2, 6.2))
    draw_panel(
        axes[0][0],
        rows,
        "throughput_ops_s_mean",
        "Benchmark throughput",
        "ops/s",
        log_y=False,
    )
    draw_panel(
        axes[0][1],
        rows,
        "owner_runnable_offcpu_p95_us_mean",
        "Runnable owner off-CPU delay",
        "p95 us",
        log_y=True,
    )
    draw_panel(
        axes[1][0],
        rows,
        "owner_preempted_events_per_contention_mean",
        "Runnable owner switch-outs",
        "events / contention",
        log_y=False,
    )
    draw_panel(
        axes[1][1],
        rows,
        "lock_wait_p999_us_mean",
        "Mutex wait latency",
        "p99.9 us",
        log_y=True,
    )

    handles, labels = axes[0][0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncol=min(8, len(labels)), frameon=False)
    fig.tight_layout(rect=[0, 0, 1, 0.93])

    png = out_dir / "owner_progress_microbench.png"
    pdf = out_dir / "owner_progress_microbench.pdf"
    fig.savefig(png, bbox_inches="tight")
    fig.savefig(pdf, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote: {png}")
    print(f"Wrote: {pdf}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


@dataclass(frozen=True)
class RunRow:
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


COLOR_MAP = {
    "default": "#1f77b4",
    "scx_simple": "#17becf",
    "scx_lockcoop": "#ff7f0e",
    "scx_hybrid": "#8c564b",
    "scx_lockboost": "#d62728",
    "scx_lcas": "#e377c2",
    "scx_nest": "#7f7f7f",
    "scx_qmap": "#bcbd22",
    "scx_central": "#2ca02c",
    "scx_flatcg": "#9467bd",
    "scx_eevdf": "#000000",
}


def load_runs(csv_path: Path) -> list[RunRow]:
    rows: list[RunRow] = []
    with csv_path.open(newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            rows.append(
                RunRow(
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
    return rows


def style():
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


def best_reduce(rows: list[RunRow]) -> Dict[Tuple[str, int, int], RunRow]:
    """
    Best-of-runs for each (scheduler_label, threads, duration_s).
    - Throughput metrics: max TPS/QPS
    - Latency metrics: min avg/p95/max
    We store a synthesized row per key.
    """
    out: Dict[Tuple[str, int, int], RunRow] = {}
    for r in rows:
        k = (r.scheduler, r.threads, r.duration_s)
        cur = out.get(k)
        if cur is None:
            out[k] = r
            continue
        # Prefer higher TPS; tie-break with lower avg latency.
        if (r.tps > cur.tps) or (math.isclose(r.tps, cur.tps) and r.lat_avg_ms < cur.lat_avg_ms):
            out[k] = r
    return out


def plot_lines(
    out_path: Path,
    title: str,
    xlabel: str,
    ylabel: str,
    x_vals: List[int],
    series: List[Tuple[str, List[float]]],
    yscale: str = "linear",
):
    fig, ax = plt.subplots(figsize=(9.0, 3.8))
    fig.subplots_adjust(right=0.78)
    markers = ["o", "s", "^", "D", "P", "X", "v", "<", ">", "h", "*"]

    for idx, (name, ys) in enumerate(series):
        ax.plot(
            x_vals,
            ys,
            marker=markers[idx % len(markers)],
            label=name,
            color=COLOR_MAP.get(name.split("(", 1)[0], None) or plt.get_cmap("tab10")(idx % 10),
            linestyle="--" if name.startswith("scx_toponest(") else "-",
        )
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.set_xticks(x_vals)
    ax.set_yscale(yscale)
    ax.legend(loc="center left", bbox_to_anchor=(1.0, 0.5), frameon=False)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Plot scx_toponest ablation into a single set of figures (baselines + variants)."
    )
    ap.add_argument("--tag-prefix", required=True, help="e.g. toponest_try_sysbench")
    ap.add_argument("--variants", default="M1,LLC8,LLC8G,NUMA8", help="Comma-separated variant suffixes")
    ap.add_argument("--base-variant", default="M1", help="Which variant tag to use for baselines")
    ap.add_argument(
        "--baselines",
        default="default,scx_simple,scx_lockcoop,scx_hybrid,scx_lockboost",
        help="Comma-separated baseline schedulers to include",
    )
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    runs_csv = result_root / "summary_runs.csv"
    if not runs_csv.exists():
        print(f"Missing {runs_csv}. Run summarize_results.py first (without --tag to include all).")
        return 1

    style()

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    base_tag = f"{args.tag_prefix}_{args.base_variant}"
    variant_tags = {v: f"{args.tag_prefix}_{v}" for v in variants}

    all_rows = load_runs(runs_csv)

    # Baselines: from base_tag only.
    base_scheds = [s.strip() for s in args.baselines.split(",") if s.strip()]
    base_rows = [r for r in all_rows if r.tag == base_tag and r.scheduler in set(base_scheds)]

    # Variants: only take scx_toponest from each tag, rename as scx_toponest(<variant>).
    var_rows: list[RunRow] = []
    for v, tag in variant_tags.items():
        for r in all_rows:
            if r.tag == tag and r.scheduler == "scx_toponest":
                var_rows.append(
                    RunRow(
                        tag=r.tag,
                        scheduler=f"scx_toponest({v})",
                        threads=r.threads,
                        duration_s=r.duration_s,
                        run=r.run,
                        tps=r.tps,
                        qps=r.qps,
                        lat_avg_ms=r.lat_avg_ms,
                        lat_p95_ms=r.lat_p95_ms,
                        lat_max_ms=r.lat_max_ms,
                    )
                )

    rows = base_rows + var_rows
    if not rows:
        print("No rows matched. Check tags exist and that you ran summarize_results.py without filtering.")
        print(f"Expected base tag: {base_tag}")
        print(f"Expected variant tags: {', '.join(variant_tags.values())}")
        return 1

    threads = sorted({r.threads for r in rows})
    durs = sorted({r.duration_s for r in rows})
    if len(durs) > 1:
        print(f"WARN: multiple durations present: {durs}. Figures will mix them; consider filtering runs.")

    best = best_reduce(rows)
    scheds = sorted({k[0] for k in best.keys()})
    ordered = [s for s in base_scheds if s in scheds]
    ordered += [s for s in scheds if s.startswith("scx_toponest(")]

    out_dir = result_root / "plots_png" / f"{args.tag_prefix}_combined"
    out_dir.mkdir(parents=True, exist_ok=True)

    def series_for(getter):
        series: List[Tuple[str, List[float]]] = []
        for s in ordered:
            ys: List[float] = []
            ok = False
            for t in threads:
                # Prefer the smallest duration if multiple exist (common for ablation runs).
                d = min(durs)
                k = (s, t, d)
                if k in best:
                    ok = True
                    ys.append(getter(best[k]))
                else:
                    ys.append(float("nan"))
            if ok:
                series.append((s, ys))
        return series

    plot_lines(
        out_dir / "tps.png",
        title=f"Sysbench OLTP TPS (best-of-runs) [{args.tag_prefix}]",
        xlabel="Threads",
        ylabel="TPS (transactions/s)",
        x_vals=threads,
        series=series_for(lambda r: r.tps),
        yscale="linear",
    )
    plot_lines(
        out_dir / "qps.png",
        title=f"Sysbench OLTP QPS (best-of-runs) [{args.tag_prefix}]",
        xlabel="Threads",
        ylabel="QPS (queries/s)",
        x_vals=threads,
        series=series_for(lambda r: r.qps),
        yscale="linear",
    )
    plot_lines(
        out_dir / "lat_avg_ms.png",
        title=f"Sysbench OLTP latency avg (best-of-runs) [{args.tag_prefix}]",
        xlabel="Threads",
        ylabel="Latency avg (ms, lower is better)",
        x_vals=threads,
        series=series_for(lambda r: r.lat_avg_ms),
        yscale="log",
    )
    plot_lines(
        out_dir / "lat_p95_ms.png",
        title=f"Sysbench OLTP latency p95 (best-of-runs) [{args.tag_prefix}]",
        xlabel="Threads",
        ylabel="Latency p95 (ms, lower is better)",
        x_vals=threads,
        series=series_for(lambda r: r.lat_p95_ms),
        yscale="log",
    )
    plot_lines(
        out_dir / "lat_max_ms.png",
        title=f"Sysbench OLTP latency max (best-of-runs) [{args.tag_prefix}]",
        xlabel="Threads",
        ylabel="Latency max (ms, lower is better)",
        x_vals=threads,
        series=series_for(lambda r: r.lat_max_ms),
        yscale="log",
    )

    print(f"Wrote PNGs under: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


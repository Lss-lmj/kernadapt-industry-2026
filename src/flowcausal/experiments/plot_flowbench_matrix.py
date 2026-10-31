#!/usr/bin/env python3
"""Plot the FlowCausal flowbench preliminary matrix."""
from __future__ import annotations

import argparse
import json
import pathlib

import matplotlib.pyplot as plt


LABELS = {
    "default": "Default\n(no sync)",
    "default_sync": "Default\n(sync)",
    "flowcausal_oracle": "FlowCausal\noracle",
    "flowcausal_width": "FlowCausal\nwidth",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plot FlowCausal flowbench matrix")
    parser.add_argument("--summary", required=True)
    parser.add_argument("--out-prefix", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary_path = pathlib.Path(args.summary)
    out_prefix = pathlib.Path(args.out_prefix)
    matrix = json.loads(summary_path.read_text(encoding="utf-8"))
    variants = matrix["variants"]

    labels = [LABELS.get(v["name"], v["name"]) for v in variants]
    wait_p95 = [
        float(v["summary"].get("frontier_sched_wait_proxy_ms_p95", 0.0))
        for v in variants
    ]
    shares = [
        float(v["summary"].get("frontier_sched_wait_share", 0.0))
        for v in variants
    ]

    flow_variants = [v for v in variants if v.get("flowmon")]
    flow_labels = [LABELS.get(v["name"], v["name"]) for v in flow_variants]
    admitted = [int(v["flowmon"].get("frontier_enqueues", 0)) for v in flow_variants]
    fallback = [int(v["flowmon"].get("fallback_enqueues", 0)) for v in flow_variants]
    width_held = [int(v["flowmon"].get("width_held_enqueues", 0)) for v in flow_variants]

    plt.rcParams.update(
        {
            "font.size": 7,
            "axes.titlesize": 8,
            "axes.labelsize": 7,
            "xtick.labelsize": 6,
            "ytick.labelsize": 6,
            "legend.fontsize": 6,
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )

    fig, axes = plt.subplots(2, 1, figsize=(3.35, 3.7))

    ax = axes[0]
    xs = range(len(labels))
    bars = ax.bar(xs, wait_p95, color=["#bdbdbd", "#9ecae1", "#fdae6b", "#fdd0a2"])
    ax.set_xticks(list(xs), labels)
    ax.set_ylabel("Frontier p95 wait (ms)")
    ax.set_title("(a) Delay-subtracted wait proxy")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    if wait_p95:
        ax.set_ylim(0, max(wait_p95) * 1.18)
    for bar, share in zip(bars, shares):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"share={share:.2f}",
            ha="center",
            va="bottom",
            fontsize=6,
        )

    ax = axes[1]
    xs2 = range(len(flow_labels))
    ax.bar(xs2, admitted, label="admitted", color="#74c476")
    ax.bar(xs2, fallback, bottom=admitted, label="fallback", color="#fc9272")
    ax.bar(
        xs2,
        width_held,
        bottom=[a + f for a, f in zip(admitted, fallback)],
        label="width-held",
        color="#9ecae1",
    )
    ax.set_xticks(list(xs2), flow_labels)
    ax.set_ylabel("BPF enqueue events")
    ax.set_title("(b) Live admission evidence")
    ax.grid(axis="y", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    stacked_total = [a + f + w for a, f, w in zip(admitted, fallback, width_held)]
    if stacked_total:
        ax.set_ylim(0, max(stacked_total) * 1.22)
    ax.legend(frameon=False, ncol=3, loc="upper center", bbox_to_anchor=(0.5, 1.02))

    fig.text(
        0.5,
        0.01,
        "Mechanism gate only; live runs use oracle hints + sync delay.",
        ha="center",
        fontsize=6,
    )
    fig.tight_layout(rect=(0, 0.06, 1, 1))

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_prefix.with_suffix(".pdf"), bbox_inches="tight")
    fig.savefig(out_prefix.with_suffix(".png"), dpi=220, bbox_inches="tight")
    plt.close(fig)
    print(f"wrote={out_prefix.with_suffix('.pdf')}")
    print(f"wrote={out_prefix.with_suffix('.png')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

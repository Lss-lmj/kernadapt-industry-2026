#!/usr/bin/env python3
"""Rank HarnessReplay variants and recommend the best scheduler/control combo."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


METRICS = (
    ("makespan_mean_sec", True),
    ("p95_critical_mean_sec", True),
    ("mean_critical_mean_sec", True),
    ("failed_stage_commands_mean", True),
    ("elapsed_jain_fairness_mean", False),
    ("critical_jain_fairness_mean", False),
)


WEIGHTS = {
    "balanced": {
        "makespan_mean_sec": 0.50,
        "p95_critical_mean_sec": 0.25,
        "mean_critical_mean_sec": 0.10,
        "failed_stage_commands_mean": 0.05,
        "elapsed_jain_fairness_mean": 0.05,
        "critical_jain_fairness_mean": 0.05,
    },
    "performance": {
        "makespan_mean_sec": 0.70,
        "p95_critical_mean_sec": 0.20,
        "mean_critical_mean_sec": 0.05,
        "failed_stage_commands_mean": 0.05,
    },
    "latency": {
        "makespan_mean_sec": 0.15,
        "p95_critical_mean_sec": 0.55,
        "mean_critical_mean_sec": 0.20,
        "failed_stage_commands_mean": 0.05,
        "elapsed_jain_fairness_mean": 0.025,
        "critical_jain_fairness_mean": 0.025,
    },
}


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def variants(summary: dict[str, Any]) -> list[dict[str, Any]]:
    rows = summary.get("variants", [])
    return [row for row in rows if isinstance(row, dict)]


def metric_value(summary: dict[str, Any], key: str) -> float | None:
    value = summary.get(key)
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalized_score(value: float | None, best: float | None, lower_is_better: bool) -> float:
    if value is None:
        return 2.0
    if best is None:
        return 1.0
    if lower_is_better:
        if best <= 0:
            return 1.0 + max(value, 0.0)
        return value / best
    if best <= 0:
        return 2.0
    return best / max(value, 1e-9)


def score_variant(summary: dict[str, Any], objective: str, bests: dict[str, float | None]) -> float:
    weights = WEIGHTS[objective]
    score = 0.0
    total = 0.0
    for key, lower_is_better in METRICS:
        if key not in weights:
            continue
        value = metric_value(summary, key)
        best = bests.get(key)
        part = normalized_score(value, best, lower_is_better)
        score += weights[key] * part
        total += weights[key]
    return score / total if total else math.inf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary-json", required=True)
    parser.add_argument(
        "--objective",
        choices=tuple(WEIGHTS.keys()),
        default="balanced",
        help="ranking objective",
    )
    args = parser.parse_args()

    payload = read_json(Path(args.summary_json))
    rows = variants(payload)
    if not rows:
        raise SystemExit("no variants found in summary")

    bests: dict[str, float | None] = {}
    for key, lower_is_better in METRICS:
        if lower_is_better:
            bests[key] = min(
                (metric_value(row.get("summary", {}), key) for row in rows),
                default=None,
            )
        else:
            bests[key] = max(
                (metric_value(row.get("summary", {}), key) for row in rows),
                default=None,
            )

    ranked: list[tuple[float, dict[str, Any]]] = []
    for row in rows:
        summary = row.get("summary", {})
        ranked.append((score_variant(summary, args.objective, bests), row))
    ranked.sort(key=lambda item: item[0])

    best_score, best_row = ranked[0]
    print(f"objective={args.objective}")
    print(f"recommended={best_row.get('name', '')}")
    print(f"score={best_score:.4f}")
    print()
    print("| rank | variant | score | makespan | p95 critical | mean critical | failures |")
    print("|---|---|---:|---:|---:|---:|---:|")
    for idx, (score, row) in enumerate(ranked, 1):
        summary = row.get("summary", {})
        print(
            "| {idx} | {name} | {score:.4f} | {makespan} | {p95} | {mean} | {failures} |".format(
                idx=idx,
                name=row.get("name", ""),
                score=score,
                makespan=summary.get("makespan_mean_sec", ""),
                p95=summary.get("p95_critical_mean_sec", ""),
                mean=summary.get("mean_critical_mean_sec", ""),
                failures=summary.get("failed_stage_commands_mean", ""),
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

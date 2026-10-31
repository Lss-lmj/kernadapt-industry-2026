#!/usr/bin/env python3
"""Compute oracle-best schedulers from existing summary CSV files."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def metric_value(row: dict[str, str], metric: str) -> float:
    value = row.get(metric)
    if value in (None, ""):
        return float("nan")
    return float(value)


def case_key(row: dict[str, str]) -> tuple[str, ...]:
    ignored = {
        "scheduler",
        "n",
        "tps_mean",
        "tps_stdev",
        "tps_cv",
        "tps_best",
        "qps_mean",
        "qps_best",
        "lat_avg_mean",
        "lat_avg_best",
        "lat_p95_mean",
        "lat_p95_best",
        "lat_max_mean",
        "lat_max_best",
        "throughput_mean",
        "throughput_stdev",
        "throughput_best",
        "throughput_worst",
        "throughput_ratio_vs_default_mean",
        "lat_p999_ratio_vs_default_mean",
    }
    keys = [k for k in row.keys() if k not in ignored and row.get(k) not in (None, "")]
    return tuple(f"{k}={row[k]}" for k in keys)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", type=Path, action="append", required=True, help="summary_cases.csv")
    ap.add_argument("--metric", default="tps_mean", help="Metric to maximize")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    cases: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for path in args.csv:
        for row in read_rows(path):
            value = metric_value(row, args.metric)
            if math.isnan(value):
                continue
            key = (f"source={path}",) + case_key(row)
            cases[key].append({"scheduler": row.get("scheduler", ""), "value": value, "row": row})

    output = []
    for key, items in sorted(cases.items()):
        if not items:
            continue
        default = next((i for i in items if i["scheduler"] == "default"), None)
        best = max(items, key=lambda i: i["value"])
        default_value = default["value"] if default else None
        speedup = best["value"] / default_value if default_value not in (None, 0) else None
        output.append(
            {
                "case": list(key),
                "oracle_scheduler": best["scheduler"],
                "oracle_value": best["value"],
                "default_value": default_value,
                "oracle_speedup_vs_default": speedup,
            }
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

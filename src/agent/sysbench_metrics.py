#!/usr/bin/env python3
"""Parse Sysbench OLTP output into compact metrics JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


TPS_RE = re.compile(r"transactions:\s+\d+\s+\(([0-9.]+) per sec\.\)")
QPS_RE = re.compile(r"queries:\s+\d+\s+\(([0-9.]+) per sec\.\)")
TOTAL_TIME_RE = re.compile(r"total time:\s+([0-9.]+)s")
EVENTS_RE = re.compile(r"total number of events:\s+([0-9]+)")
LAT_MIN_RE = re.compile(r"min:\s+([0-9.]+)")
LAT_AVG_RE = re.compile(r"avg:\s+([0-9.]+)")
LAT_MAX_RE = re.compile(r"max:\s+([0-9.]+)")
LAT_P95_RE = re.compile(r"95th percentile:\s+([0-9.]+)")
FAIRNESS_EVENTS_RE = re.compile(r"events \(avg/stddev\):\s+([0-9.]+)/([0-9.]+)")


def extract_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if not match:
        return None
    return float(match.group(1))


def extract_int(pattern: re.Pattern[str], text: str) -> int | None:
    match = pattern.search(text)
    if not match:
        return None
    return int(match.group(1))


def parse_sysbench_text(text: str) -> dict[str, Any]:
    fairness_match = FAIRNESS_EVENTS_RE.search(text)
    fairness_avg = float(fairness_match.group(1)) if fairness_match else None
    fairness_stddev = float(fairness_match.group(2)) if fairness_match else None
    fairness_cv = None
    if fairness_avg not in (None, 0) and fairness_stddev is not None:
        fairness_cv = fairness_stddev / fairness_avg

    return {
        "tps": extract_float(TPS_RE, text),
        "qps": extract_float(QPS_RE, text),
        "total_time_s": extract_float(TOTAL_TIME_RE, text),
        "events": extract_int(EVENTS_RE, text),
        "lat_min_ms": extract_float(LAT_MIN_RE, text),
        "lat_avg_ms": extract_float(LAT_AVG_RE, text),
        "lat_max_ms": extract_float(LAT_MAX_RE, text),
        "lat_p95_ms": extract_float(LAT_P95_RE, text),
        "fairness_events_avg": fairness_avg,
        "fairness_events_stddev": fairness_stddev,
        "fairness_events_cv": fairness_cv,
    }


def parse_sysbench_log(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_sysbench_text(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    metrics = parse_sysbench_log(args.log)
    output = json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

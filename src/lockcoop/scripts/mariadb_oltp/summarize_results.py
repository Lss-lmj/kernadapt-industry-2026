#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import math
import re
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, pstdev
from typing import Optional


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
    path: str


LABEL_RE = re.compile(r"^(?P<prefix>.+)_t(?P<t>[0-9]+)_d(?P<d>[0-9]+)$")
PREFIX_RE = re.compile(r"^(?P<tag>.+)_(?P<sched>default|scx_[A-Za-z0-9_]+)$")

TPS_RE = re.compile(r"^\s*transactions:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
QPS_RE = re.compile(r"^\s*queries:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
LAT_KV_RE = re.compile(r"^\s*(?P<k>min|avg|max|95th percentile):\s*(?P<v>[0-9]+(?:\.[0-9]+)?)\s*$")


def parse_label(label: str) -> Optional[tuple[str, str, int, int]]:
    m = LABEL_RE.match(label)
    if not m:
        return None
    threads = int(m.group("t"))
    dur = int(m.group("d"))

    prefix = m.group("prefix")
    mp = PREFIX_RE.match(prefix)
    if not mp:
        return None
    tag = mp.group("tag")
    sched = mp.group("sched")
    return tag, sched, threads, dur


def parse_sysbench_output(path: Path) -> Optional[dict]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    tps = None
    qps = None
    lat = {}

    for line in lines:
        m = TPS_RE.match(line)
        if m:
            tps = float(m.group("v"))
            continue
        m = QPS_RE.match(line)
        if m:
            qps = float(m.group("v"))
            continue
        m = LAT_KV_RE.match(line)
        if m:
            lat[m.group("k")] = float(m.group("v"))

    if tps is None or qps is None:
        return None
    if "avg" not in lat or "95th percentile" not in lat or "max" not in lat:
        return None

    return {
        "tps": tps,
        "qps": qps,
        "lat_avg_ms": lat["avg"],
        "lat_p95_ms": lat["95th percentile"],
        "lat_max_ms": lat["max"],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize sysbench OLTP results (test3)")
    ap.add_argument("--tag", default=None, help="Only summarize a specific TAG")
    ap.add_argument("--expected-runs", type=int, default=None)
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    if not result_root.exists():
        print(f"Result root not found: {result_root}")
        return 1

    rows: list[RunRow] = []

    for d in sorted(p for p in result_root.iterdir() if p.is_dir()):
        parsed = parse_label(d.name)
        if not parsed:
            continue
        tag, sched, threads, dur = parsed
        if args.tag and tag != args.tag:
            continue

        for f in sorted(d.glob("sysbench.r*.txt")):
            m = re.match(r"^sysbench\.r(?P<run>[0-9]+)\.txt$", f.name)
            if not m:
                continue
            run = int(m.group("run"))

            out = parse_sysbench_output(f)
            if not out:
                continue
            rows.append(
                RunRow(
                    tag=tag,
                    scheduler=sched,
                    threads=threads,
                    duration_s=dur,
                    run=run,
                    tps=float(out["tps"]),
                    qps=float(out["qps"]),
                    lat_avg_ms=float(out["lat_avg_ms"]),
                    lat_p95_ms=float(out["lat_p95_ms"]),
                    lat_max_ms=float(out["lat_max_ms"]),
                    path=str(f),
                )
            )

    tags = sorted({r.tag for r in rows})
    scheds = sorted({r.scheduler for r in rows})
    threads = sorted({r.threads for r in rows})
    durs = sorted({r.duration_s for r in rows})

    print(f"Result root: {result_root}")
    print(f"Tags: {', '.join(tags) if tags else '(none)'}")
    print(f"Schedulers: {', '.join(scheds) if scheds else '(none)'}")
    print(f"THREADS: {', '.join(map(str, threads)) if threads else '(none)'}")
    print(f"RUN_TIME: {', '.join(map(str, durs)) if durs else '(none)'}")
    print()

    out_runs = result_root / "summary_runs.csv"
    out_cases = result_root / "summary_cases.csv"
    out_cov = result_root / "summary_coverage.csv"

    with out_runs.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "tag",
                "scheduler",
                "threads",
                "duration_s",
                "run",
                "tps",
                "qps",
                "lat_avg_ms",
                "lat_p95_ms",
                "lat_max_ms",
                "path",
            ],
        )
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x.tag, x.duration_s, x.threads, x.scheduler, x.run)):
            w.writerow(
                {
                    "tag": r.tag,
                    "scheduler": r.scheduler,
                    "threads": r.threads,
                    "duration_s": r.duration_s,
                    "run": r.run,
                    "tps": f"{r.tps:.6f}",
                    "qps": f"{r.qps:.6f}",
                    "lat_avg_ms": f"{r.lat_avg_ms:.6f}",
                    "lat_p95_ms": f"{r.lat_p95_ms:.6f}",
                    "lat_max_ms": f"{r.lat_max_ms:.6f}",
                    "path": r.path,
                }
            )

    cases: dict[tuple[str, str, int, int], list[RunRow]] = {}
    for r in rows:
        cases.setdefault((r.tag, r.scheduler, r.threads, r.duration_s), []).append(r)

    # Coverage file is useful when runs are intentionally uneven (e.g. a scheduler wedges the machine).
    with out_cov.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "tag",
                "scheduler",
                "threads",
                "duration_s",
                "n",
                "runs_present",
            ],
        )
        w.writeheader()
        for (tag, sched, t, d), rs in sorted(cases.items(), key=lambda x: x[0]):
            runs_present = ",".join(str(x.run) for x in sorted(rs, key=lambda x: x.run))
            w.writerow(
                {
                    "tag": tag,
                    "scheduler": sched,
                    "threads": t,
                    "duration_s": d,
                    "n": len(rs),
                    "runs_present": runs_present,
                }
            )

    with out_cases.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "tag",
                "scheduler",
                "threads",
                "duration_s",
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
            ],
        )
        w.writeheader()

        for (tag, sched, t, d), rs in sorted(cases.items(), key=lambda x: x[0]):
            tps_vals = [x.tps for x in rs]
            qps_vals = [x.qps for x in rs]
            avg_vals = [x.lat_avg_ms for x in rs]
            p95_vals = [x.lat_p95_ms for x in rs]
            max_vals = [x.lat_max_ms for x in rs]
            mu = mean(tps_vals)
            sd = pstdev(tps_vals) if len(tps_vals) >= 2 else 0.0
            cv = (sd / mu) if mu != 0 else math.nan
            w.writerow(
                {
                    "tag": tag,
                    "scheduler": sched,
                    "threads": t,
                    "duration_s": d,
                    "n": len(rs),
                    "tps_mean": f"{mu:.6f}",
                    "tps_stdev": f"{sd:.6f}",
                    "tps_cv": f"{cv:.6f}",
                    "tps_best": f"{max(tps_vals):.6f}",
                    "qps_mean": f"{mean(qps_vals):.6f}",
                    "qps_best": f"{max(qps_vals):.6f}",
                    "lat_avg_mean": f"{mean(avg_vals):.6f}",
                    "lat_avg_best": f"{min(avg_vals):.6f}",
                    "lat_p95_mean": f"{mean(p95_vals):.6f}",
                    "lat_p95_best": f"{min(p95_vals):.6f}",
                    "lat_max_mean": f"{mean(max_vals):.6f}",
                    "lat_max_best": f"{min(max_vals):.6f}",
                }
            )

    if args.expected_runs is not None:
        for (tag, sched, t, d), rs in sorted(cases.items(), key=lambda x: x[0]):
            if len(rs) < args.expected_runs:
                print(
                    f"WARNING: {tag} {sched} threads={t} d={d}: only {len(rs)} runs "
                    f"(expected {args.expected_runs})"
                )

    print(f"Wrote: {out_runs}")
    print(f"Wrote: {out_cases}")
    print(f"Wrote: {out_cov}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

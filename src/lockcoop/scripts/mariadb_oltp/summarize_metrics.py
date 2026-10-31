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
class MetricRun:
    tag: str
    scheduler: str
    threads: int
    duration_s: int
    run: int

    # perf stat
    perf_scope: str
    task_clock_ms: Optional[float]
    task_clock_cpus: Optional[float]
    cpus_utilized: Optional[float]
    context_switches: Optional[float]
    cpu_migrations: Optional[float]
    cs_per_s: Optional[float]
    mig_per_s: Optional[float]
    cycles: Optional[float]
    instructions: Optional[float]
    ipc: Optional[float]
    cache_references: Optional[float]
    cache_misses: Optional[float]
    cache_miss_rate: Optional[float]

    # numastat (after)
    numastat_total_mb: Optional[float]
    numastat_node0_mb: Optional[float]
    numastat_node1_mb: Optional[float]
    numastat_node0_ratio: Optional[float]
    numastat_node1_ratio: Optional[float]

    # numa_maps (after) page distribution
    numa_pages_total: Optional[int]
    numa_pages_n0: Optional[int]
    numa_pages_n1: Optional[int]
    numa_pages_n0_ratio: Optional[float]
    numa_pages_n1_ratio: Optional[float]

    # /proc/stat delta (true system-wide busy estimate)
    system_busy_ratio: Optional[float]
    busy_cpus: Optional[float]

    # file paths (debug)
    perf_path: str
    numastat_after_path: str
    numa_maps_after_path: str
    proc_stat_before_path: str
    proc_stat_after_path: str


LABEL_RE = re.compile(r"^(?P<prefix>.+)_t(?P<t>[0-9]+)_d(?P<d>[0-9]+)$")
PREFIX_RE = re.compile(r"^(?P<tag>.+)_(?P<sched>default|scx_[A-Za-z0-9_]+)$")


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


def parse_perf_stat_csv(path: Path, duration_s: int) -> dict[str, float | str]:
    vals: dict[str, float | str] = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return vals

    for line in lines:
        if line.startswith("# scope:"):
            vals["perf_scope"] = line.split(":", 1)[1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        value_s, unit, event = parts[0], parts[1], parts[2]
        try:
            value = float(value_s)
        except ValueError:
            continue
        # Normalize task-clock into ms.
        if event == "task-clock":
            if unit == "msec":
                vals["task_clock_ms"] = value
            elif unit == "sec":
                vals["task_clock_ms"] = value * 1000.0
            else:
                vals["task_clock_ms"] = value
        else:
            vals[event] = value

    tc = vals.get("task_clock_ms")
    if tc is not None and duration_s > 0:
        task_clock_cpus = tc / (duration_s * 1000.0)
        vals["task_clock_cpus"] = task_clock_cpus
        # Keep cpus_utilized as PID-only metric to avoid misreading system scope.
        if str(vals.get("perf_scope", "pid")).lower() != "system":
            vals["cpus_utilized"] = task_clock_cpus

    cs = vals.get("context-switches")
    if cs is not None and duration_s > 0:
        vals["cs_per_s"] = cs / duration_s

    mig = vals.get("cpu-migrations")
    if mig is not None and duration_s > 0:
        vals["mig_per_s"] = mig / duration_s

    cyc = vals.get("cycles")
    ins = vals.get("instructions")
    if cyc and ins and cyc > 0:
        vals["ipc"] = ins / cyc

    cref = vals.get("cache-references")
    cmiss = vals.get("cache-misses")
    if cref and cmiss and cref > 0:
        vals["cache_miss_rate"] = cmiss / cref

    return vals


@dataclass(frozen=True)
class ProcStatSnap:
    total: int
    idle: int
    cpu_count: int


def parse_proc_stat_snapshot(path: Path) -> Optional[ProcStatSnap]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    total = None
    idle = None
    cpu_count = 0

    for line in lines:
        if line.startswith("cpu ") and total is None:
            parts = line.split()
            if len(parts) < 5:
                return None
            nums = []
            for x in parts[1:]:
                try:
                    nums.append(int(x))
                except ValueError:
                    return None
            total = sum(nums)
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            continue
        if re.match(r"^cpu[0-9]+\s", line):
            cpu_count += 1

    if total is None or idle is None or cpu_count <= 0:
        return None
    return ProcStatSnap(total=total, idle=idle, cpu_count=cpu_count)


def calc_busy_from_proc_stat(before: Path, after: Path) -> tuple[Optional[float], Optional[float]]:
    b = parse_proc_stat_snapshot(before)
    a = parse_proc_stat_snapshot(after)
    if not b or not a:
        return None, None

    dt = a.total - b.total
    di = a.idle - b.idle
    if dt <= 0:
        return None, None

    busy_ratio = (dt - di) / dt
    if busy_ratio < 0:
        busy_ratio = 0.0
    if busy_ratio > 1:
        busy_ratio = 1.0
    busy_cpus = busy_ratio * a.cpu_count
    return busy_ratio, busy_cpus


TOTAL_RE = re.compile(r"^Total\s+(?P<n0>[0-9]+(?:\.[0-9]+)?)\s+(?P<n1>[0-9]+(?:\.[0-9]+)?)\s+(?P<tot>[0-9]+(?:\.[0-9]+)?)\s*$")


def parse_numastat_total(path: Path) -> tuple[Optional[float], Optional[float], Optional[float]]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None, None, None
    for line in lines:
        m = TOTAL_RE.match(line.strip())
        if not m:
            continue
        n0 = float(m.group("n0"))
        n1 = float(m.group("n1"))
        tot = float(m.group("tot"))
        return n0, n1, tot
    return None, None, None


N_RE = re.compile(r"N(?P<node>[0-9]+)=(?P<pages>[0-9]+)")


def parse_numa_maps_pages(path: Path) -> tuple[Optional[int], Optional[int], Optional[int]]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None, None, None
    if "Permission denied" in text or text.strip().startswith("cat:"):
        return None, None, None

    n0 = 0
    n1 = 0
    for line in text.splitlines():
        for m in N_RE.finditer(line):
            node = int(m.group("node"))
            pages = int(m.group("pages"))
            if node == 0:
                n0 += pages
            elif node == 1:
                n1 += pages
    tot = n0 + n1
    return n0, n1, tot


def main() -> int:
    ap = argparse.ArgumentParser(description="Summarize test3 RCA metrics (perf/numa) into CSV")
    ap.add_argument("--tag", default=None, help="Only summarize a specific TAG")
    ap.add_argument("--expected-runs", type=int, default=None)
    args = ap.parse_args()

    script_dir = Path(__file__).resolve().parent
    test_dir = script_dir.parent
    result_root = test_dir / "result"
    if not result_root.exists():
        print(f"Result root not found: {result_root}")
        return 1

    rows: list[MetricRun] = []

    for d in sorted(p for p in result_root.iterdir() if p.is_dir()):
        parsed = parse_label(d.name)
        if not parsed:
            continue
        tag, sched, threads, dur = parsed
        if args.tag and tag != args.tag:
            continue

        for perf_path in sorted(d.glob("perf_stat.r*.csv")):
            m = re.match(r"^perf_stat\.r(?P<run>[0-9]+)\.csv$", perf_path.name)
            if not m:
                continue
            run = int(m.group("run"))

            # Optional files (may be missing)
            numastat_after = d / f"numastat.after.r{run}.txt"
            numa_maps_after = d / f"numa_maps.after.r{run}.txt"
            proc_stat_before = d / f"proc_stat.before.r{run}.txt"
            proc_stat_after = d / f"proc_stat.after.r{run}.txt"

            perf = parse_perf_stat_csv(perf_path, dur)

            n0_mb, n1_mb, tot_mb = parse_numastat_total(numastat_after) if numastat_after.exists() else (None, None, None)
            n0_ratio = (n0_mb / tot_mb) if (n0_mb is not None and tot_mb) else None
            n1_ratio = (n1_mb / tot_mb) if (n1_mb is not None and tot_mb) else None

            pages_n0, pages_n1, pages_tot = (
                parse_numa_maps_pages(numa_maps_after) if numa_maps_after.exists() else (None, None, None)
            )
            p0_ratio = (pages_n0 / pages_tot) if (pages_n0 is not None and pages_tot) else None
            p1_ratio = (pages_n1 / pages_tot) if (pages_n1 is not None and pages_tot) else None
            sys_busy_ratio, busy_cpus = (
                calc_busy_from_proc_stat(proc_stat_before, proc_stat_after)
                if proc_stat_before.exists() and proc_stat_after.exists()
                else (None, None)
            )

            rows.append(
                MetricRun(
                    tag=tag,
                    scheduler=sched,
                    threads=threads,
                    duration_s=dur,
                    run=run,
                    perf_scope=str(perf.get("perf_scope", "pid")),
                    task_clock_ms=perf.get("task_clock_ms"),  # type: ignore[arg-type]
                    task_clock_cpus=perf.get("task_clock_cpus"),  # type: ignore[arg-type]
                    cpus_utilized=perf.get("cpus_utilized"),  # type: ignore[arg-type]
                    context_switches=perf.get("context-switches"),  # type: ignore[arg-type]
                    cpu_migrations=perf.get("cpu-migrations"),  # type: ignore[arg-type]
                    cs_per_s=perf.get("cs_per_s"),  # type: ignore[arg-type]
                    mig_per_s=perf.get("mig_per_s"),  # type: ignore[arg-type]
                    cycles=perf.get("cycles"),  # type: ignore[arg-type]
                    instructions=perf.get("instructions"),  # type: ignore[arg-type]
                    ipc=perf.get("ipc"),  # type: ignore[arg-type]
                    cache_references=perf.get("cache-references"),  # type: ignore[arg-type]
                    cache_misses=perf.get("cache-misses"),  # type: ignore[arg-type]
                    cache_miss_rate=perf.get("cache_miss_rate"),  # type: ignore[arg-type]
                    numastat_total_mb=tot_mb,
                    numastat_node0_mb=n0_mb,
                    numastat_node1_mb=n1_mb,
                    numastat_node0_ratio=n0_ratio,
                    numastat_node1_ratio=n1_ratio,
                    numa_pages_total=pages_tot,
                    numa_pages_n0=pages_n0,
                    numa_pages_n1=pages_n1,
                    numa_pages_n0_ratio=p0_ratio,
                    numa_pages_n1_ratio=p1_ratio,
                    system_busy_ratio=sys_busy_ratio,
                    busy_cpus=busy_cpus,
                    perf_path=str(perf_path),
                    numastat_after_path=str(numastat_after) if numastat_after.exists() else "",
                    numa_maps_after_path=str(numa_maps_after) if numa_maps_after.exists() else "",
                    proc_stat_before_path=str(proc_stat_before) if proc_stat_before.exists() else "",
                    proc_stat_after_path=str(proc_stat_after) if proc_stat_after.exists() else "",
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

    out_runs = result_root / "summary_metrics_runs.csv"
    out_cases = result_root / "summary_metrics_cases.csv"
    out_cov = result_root / "summary_metrics_coverage.csv"

    fieldnames = [
        "tag",
        "scheduler",
        "threads",
        "duration_s",
        "run",
        "perf_scope",
        "task_clock_cpus",
        "cpus_utilized",
        "system_busy_ratio",
        "busy_cpus",
        "cs_per_s",
        "mig_per_s",
        "ipc",
        "cache_miss_rate",
        "numastat_node0_ratio",
        "numastat_node1_ratio",
        "numa_pages_n0_ratio",
        "numa_pages_n1_ratio",
        # raw totals for debugging
        "task_clock_ms",
        "context_switches",
        "cpu_migrations",
        "cycles",
        "instructions",
        "cache_references",
        "cache_misses",
        "numastat_node0_mb",
        "numastat_node1_mb",
        "numastat_total_mb",
        "numa_pages_n0",
        "numa_pages_n1",
        "numa_pages_total",
        "perf_path",
        "numastat_after_path",
        "numa_maps_after_path",
        "proc_stat_before_path",
        "proc_stat_after_path",
    ]

    def fmt(x: Optional[float]) -> str:
        if x is None:
            return ""
        return f"{x:.6f}"

    def fmt_i(x: Optional[int]) -> str:
        if x is None:
            return ""
        return str(int(x))

    with out_runs.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x.tag, x.duration_s, x.threads, x.scheduler, x.run)):
            w.writerow(
                {
                    "tag": r.tag,
                    "scheduler": r.scheduler,
                    "threads": r.threads,
                    "duration_s": r.duration_s,
                    "run": r.run,
                    "perf_scope": r.perf_scope,
                    "task_clock_cpus": fmt(r.task_clock_cpus),
                    "cpus_utilized": fmt(r.cpus_utilized),
                    "system_busy_ratio": fmt(r.system_busy_ratio),
                    "busy_cpus": fmt(r.busy_cpus),
                    "cs_per_s": fmt(r.cs_per_s),
                    "mig_per_s": fmt(r.mig_per_s),
                    "ipc": fmt(r.ipc),
                    "cache_miss_rate": fmt(r.cache_miss_rate),
                    "numastat_node0_ratio": fmt(r.numastat_node0_ratio),
                    "numastat_node1_ratio": fmt(r.numastat_node1_ratio),
                    "numa_pages_n0_ratio": fmt(r.numa_pages_n0_ratio),
                    "numa_pages_n1_ratio": fmt(r.numa_pages_n1_ratio),
                    "task_clock_ms": fmt(r.task_clock_ms),
                    "context_switches": fmt(r.context_switches),
                    "cpu_migrations": fmt(r.cpu_migrations),
                    "cycles": fmt(r.cycles),
                    "instructions": fmt(r.instructions),
                    "cache_references": fmt(r.cache_references),
                    "cache_misses": fmt(r.cache_misses),
                    "numastat_node0_mb": fmt(r.numastat_node0_mb),
                    "numastat_node1_mb": fmt(r.numastat_node1_mb),
                    "numastat_total_mb": fmt(r.numastat_total_mb),
                    "numa_pages_n0": fmt_i(r.numa_pages_n0),
                    "numa_pages_n1": fmt_i(r.numa_pages_n1),
                    "numa_pages_total": fmt_i(r.numa_pages_total),
                    "perf_path": r.perf_path,
                    "numastat_after_path": r.numastat_after_path,
                    "numa_maps_after_path": r.numa_maps_after_path,
                    "proc_stat_before_path": r.proc_stat_before_path,
                    "proc_stat_after_path": r.proc_stat_after_path,
                }
            )

    # Coverage is useful when some schedulers only have a subset of runs.
    cases: dict[tuple[str, str, int, int], list[MetricRun]] = {}
    for r in rows:
        cases.setdefault((r.tag, r.scheduler, r.threads, r.duration_s), []).append(r)

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
                "cs_per_s_mean",
                "cs_per_s_stdev",
                "mig_per_s_mean",
                "mig_per_s_stdev",
                "ipc_mean",
                "ipc_stdev",
                "cache_miss_rate_mean",
                "cache_miss_rate_stdev",
                "task_clock_cpus_mean",
                "task_clock_cpus_stdev",
                "cpus_utilized_mean",
                "cpus_utilized_stdev",
                "busy_cpus_mean",
                "busy_cpus_stdev",
                "system_busy_ratio_mean",
                "system_busy_ratio_stdev",
            ],
        )
        w.writeheader()
        for (tag, sched, t, d), rs in sorted(cases.items(), key=lambda x: x[0]):
            def agg_opt(getter):
                vals = [getter(x) for x in rs if getter(x) is not None]
                if not vals:
                    return None, None
                mu = mean(vals)
                sd = pstdev(vals) if len(vals) >= 2 else 0.0
                return mu, sd

            cs_mu, cs_sd = agg_opt(lambda x: x.cs_per_s)
            mig_mu, mig_sd = agg_opt(lambda x: x.mig_per_s)
            ipc_mu, ipc_sd = agg_opt(lambda x: x.ipc)
            mr_mu, mr_sd = agg_opt(lambda x: x.cache_miss_rate)
            tcpu_mu, tcpu_sd = agg_opt(lambda x: x.task_clock_cpus)
            cpu_mu, cpu_sd = agg_opt(lambda x: x.cpus_utilized)
            busy_mu, busy_sd = agg_opt(lambda x: x.busy_cpus)
            sbr_mu, sbr_sd = agg_opt(lambda x: x.system_busy_ratio)

            w.writerow(
                {
                    "tag": tag,
                    "scheduler": sched,
                    "threads": t,
                    "duration_s": d,
                    "n": len(rs),
                    "cs_per_s_mean": fmt(cs_mu),
                    "cs_per_s_stdev": fmt(cs_sd),
                    "mig_per_s_mean": fmt(mig_mu),
                    "mig_per_s_stdev": fmt(mig_sd),
                    "ipc_mean": fmt(ipc_mu),
                    "ipc_stdev": fmt(ipc_sd),
                    "cache_miss_rate_mean": fmt(mr_mu),
                    "cache_miss_rate_stdev": fmt(mr_sd),
                    "task_clock_cpus_mean": fmt(tcpu_mu),
                    "task_clock_cpus_stdev": fmt(tcpu_sd),
                    "cpus_utilized_mean": fmt(cpu_mu),
                    "cpus_utilized_stdev": fmt(cpu_sd),
                    "busy_cpus_mean": fmt(busy_mu),
                    "busy_cpus_stdev": fmt(busy_sd),
                    "system_busy_ratio_mean": fmt(sbr_mu),
                    "system_busy_ratio_stdev": fmt(sbr_sd),
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

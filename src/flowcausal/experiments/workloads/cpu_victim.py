#!/usr/bin/env python3
"""Periodic CPU victim workload for FlowCausal fairness experiments.

The workload models a small latency-sensitive sidecar that should keep making
regular progress while a sandbox/code-agent/CI workflow is being optimized.
It intentionally runs outside the sandbox root observed by flowctl.  Each
worker releases a periodic job, optionally burns CPU for a short budget, and
records release lateness:

    actual_start_time - scheduled_release_time

The output is newline-delimited JSON so experiment harnesses can keep the raw
log and later compute victim slowdown, p95/p99 release lateness, and maximum
delay under default scheduling versus FlowCausal variants.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import queue as queue_mod
import signal
import statistics
import sys
import time
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    frac = rank - lower
    return ordered[lower] * (1.0 - frac) + ordered[upper] * frac


def summarize_ns(values: list[int]) -> dict[str, float]:
    if not values:
        return {
            "count": 0.0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
        }
    as_ms = [v / 1_000_000.0 for v in values]
    return {
        "count": float(len(values)),
        "mean_ms": statistics.mean(as_ms),
        "p50_ms": percentile(as_ms, 50),
        "p95_ms": percentile(as_ms, 95),
        "p99_ms": percentile(as_ms, 99),
        "max_ms": max(as_ms),
    }


def burn_until(deadline_ns: int, stop_event: Any) -> int:
    x = 0
    while time.monotonic_ns() < deadline_ns and not stop_event.is_set():
        x = (x * 1103515245 + 12345) & 0xFFFFFFFF
    return x


def worker_main(
    worker_id: int,
    duration_ns: int,
    period_ns: int,
    work_ns: int,
    emit_samples: bool,
    stop_event: Any,
    queue: mp.Queue,
) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    pid = os.getpid()
    start_ns = time.monotonic_ns()
    end_ns = start_ns + duration_ns
    release_ns = start_ns + period_ns
    lateness: list[int] = []
    runtime: list[int] = []
    iterations = 0
    checksum = 0

    while release_ns < end_ns and not stop_event.is_set():
        now = time.monotonic_ns()
        sleep_ns = release_ns - now
        if sleep_ns > 0:
            time.sleep(sleep_ns / 1_000_000_000.0)
        if stop_event.is_set():
            break
        actual_ns = time.monotonic_ns()
        late_ns = max(0, actual_ns - release_ns)
        work_start_ns = actual_ns
        checksum ^= burn_until(work_start_ns + work_ns, stop_event)
        done_ns = time.monotonic_ns()
        run_ns = done_ns - work_start_ns
        lateness.append(late_ns)
        runtime.append(run_ns)
        iterations += 1
        if emit_samples:
            queue.put(
                {
                    "type": "victim_sample",
                    "worker": worker_id,
                    "pid": pid,
                    "iteration": iterations,
                    "lateness_ns": late_ns,
                    "runtime_ns": run_ns,
                }
            )
        release_ns += period_ns

    queue.put(
        {
            "type": "victim_worker_summary",
            "worker": worker_id,
            "pid": pid,
            "iterations": iterations,
            "checksum": checksum,
            "lateness": summarize_ns(lateness),
            "runtime": summarize_ns(runtime),
            "lateness_ns_values": lateness,
            "runtime_ns_values": runtime,
        }
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a periodic CPU victim sidecar and print JSONL metrics",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--duration-sec", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--period-ms", type=float, default=20.0)
    parser.add_argument("--work-us", type=float, default=500.0)
    parser.add_argument(
        "--emit-samples",
        action="store_true",
        help="emit every periodic job sample; default prints only begin/end summaries",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration_sec <= 0:
        raise SystemExit("--duration-sec must be positive")
    if args.workers < 1:
        raise SystemExit("--workers must be positive")
    if args.period_ms <= 0:
        raise SystemExit("--period-ms must be positive")
    if args.work_us < 0:
        raise SystemExit("--work-us must be non-negative")

    duration_ns = int(args.duration_sec * 1_000_000_000)
    period_ns = int(args.period_ms * 1_000_000)
    work_ns = int(args.work_us * 1_000)
    if work_ns >= period_ns:
        raise SystemExit("--work-us must be smaller than --period-ms")

    begin = {
        "type": "victim_begin",
        "pid": os.getpid(),
        "workers": args.workers,
        "duration_sec": args.duration_sec,
        "period_ms": args.period_ms,
        "work_us": args.work_us,
        "emit_samples": bool(args.emit_samples),
    }
    print(json.dumps(begin, sort_keys=True), flush=True)

    stop_event = mp.Event()
    queue: mp.Queue = mp.Queue()
    procs: list[mp.Process] = []

    def request_stop(signum: int, _frame: Any) -> None:
        stop_event.set()
        print(
            json.dumps({"type": "victim_interrupted", "signal": signum}, sort_keys=True),
            flush=True,
        )

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    for worker_id in range(args.workers):
        proc = mp.Process(
            target=worker_main,
            args=(worker_id, duration_ns, period_ns, work_ns, args.emit_samples, stop_event, queue),
        )
        proc.start()
        procs.append(proc)

    worker_summaries: list[dict[str, Any]] = []
    while len(worker_summaries) < len(procs):
        try:
            item = queue.get(timeout=0.1)
        except queue_mod.Empty:
            if all(proc.exitcode is not None for proc in procs):
                break
            continue
        if not isinstance(item, dict):
            continue
        if item.get("type") == "victim_worker_summary":
            worker_summaries.append(item)
            compact = dict(item)
            compact.pop("lateness_ns_values", None)
            compact.pop("runtime_ns_values", None)
            print(json.dumps(compact, sort_keys=True), flush=True)
        else:
            print(json.dumps(item, sort_keys=True), flush=True)

    rc = 0
    for proc in procs:
        proc.join(timeout=1.0)
        if proc.exitcode not in (0, None):
            rc = proc.exitcode or 1

    lateness_values: list[int] = []
    runtime_values: list[int] = []
    total_iterations = 0
    for summary in worker_summaries:
        total_iterations += int(summary.get("iterations", 0))
        lateness_values.extend(int(v) for v in summary.get("lateness_ns_values", []))
        runtime_values.extend(int(v) for v in summary.get("runtime_ns_values", []))

    end = {
        "type": "victim_summary",
        "pid": os.getpid(),
        "workers": args.workers,
        "iterations": total_iterations,
        "lateness": summarize_ns(lateness_values),
        "runtime": summarize_ns(runtime_values),
        "returncode": rc,
    }
    print(json.dumps(end, sort_keys=True), flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

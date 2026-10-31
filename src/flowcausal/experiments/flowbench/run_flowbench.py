#!/usr/bin/env python3
"""Run FlowCausal's sandbox-frontier microbenchmark.

The runner launches one or more flowbench sessions concurrently, collects
newline-delimited JSON, and emits a compact summary.  It intentionally does not
require the FlowCausal scheduler to be attached: the first goal is workload
characterization under the default scheduler.  Later runs can execute the same
script while scx_flowcausal is active and compare mechanism metrics.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import selectors
import statistics
import subprocess
import sys
import time
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
FLOWCAUSAL_ROOT = HERE.parents[1]
BIN = HERE / "flowbench"
FLOWCTL = FLOWCAUSAL_ROOT / "src" / "controller" / "flowctl.py"


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def ensure_built() -> None:
    if BIN.exists():
        return
    subprocess.run(["make", "-C", str(HERE), "flowbench"], check=True)


def update_oracle_hint(
    args: argparse.Namespace,
    trial: int,
    item: dict[str, Any],
) -> dict[str, Any]:
    stage = int(item["stage"])
    pid = int(item["pid"])
    session = str(item["session"])
    cmd = [
        sys.executable,
        str(args.flowctl),
        "--frontier-pid",
        str(pid),
        "--session",
        session,
        "--stage-kind",
        args.hint_stage_kind,
        "--stage-label",
        f"trial{trial}:stage{stage}",
        "--cohort-label",
        f"stage{stage}:cohort0",
        "--pin-dir",
        args.pin_dir,
        "--confidence",
        str(args.confidence),
        "--ttl-ms",
        str(args.ttl_ms),
        "--policy-window-ms",
        str(args.policy_window_ms),
        "--boost-budget-ms",
        str(args.boost_budget_ms),
        "--max-vtime-boost-us",
        str(args.max_vtime_boost_us),
    ]
    if args.anchor_cpu >= 0:
        cmd.extend(["--anchor-cpu", str(args.anchor_cpu)])
    if args.width_control:
        cmd.extend(["--width-control", "--max-frontier-width", str(args.max_frontier_width)])
    if args.hint_dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError(
            "oracle hint update failed\n"
            f"command: {' '.join(cmd)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

    return {
        "type": "hint_update",
        "trial": trial,
        "session": session,
        "stage": stage,
        "pid": pid,
        "oracle": True,
        "dry_run": bool(args.hint_dry_run),
        "confidence": int(args.confidence),
        "ttl_ms": int(args.ttl_ms),
        "policy_window_ms": int(args.policy_window_ms),
        "boost_budget_ms": int(args.boost_budget_ms),
        "max_vtime_boost_us": int(args.max_vtime_boost_us),
        "width_control": bool(args.width_control),
        "max_frontier_width": int(args.max_frontier_width),
    }


def launch_sessions(args: argparse.Namespace, trial: int) -> list[subprocess.Popen[str]]:
    procs: list[subprocess.Popen[str]] = []

    for sid in range(args.sessions):
        cmd = [
            str(BIN),
            "--session",
            f"trial{trial}:session{sid}",
            "--stages",
            str(args.stages),
            "--cohort",
            str(args.cohort),
            "--frontier-width",
            str(args.frontier_width),
            "--work-ms",
            str(args.work_ms),
            "--tail-work-ms",
            str(args.tail_work_ms),
            "--stage-gap-ms",
            str(args.stage_gap_ms),
            "--start-delay-ms",
            str(args.start_delay_ms),
        ]
        if args.taskset:
            cmd = ["taskset", "-c", args.taskset] + cmd
        procs.append(
            subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        )

    return procs


def run_trial_simple(args: argparse.Namespace, trial: int) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    procs = launch_sessions(args, trial)

    for proc in procs:
        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"flowbench failed with code {proc.returncode}\nSTDERR:\n{stderr}"
            )
        if stderr.strip():
            print(stderr, file=sys.stderr)
        for line in stdout.splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            item["trial"] = trial
            records.append(item)

    return records


def run_trial_with_oracle_hints(args: argparse.Namespace, trial: int) -> list[dict[str, Any]]:
    procs = launch_sessions(args, trial)
    selector = selectors.DefaultSelector()
    records: list[dict[str, Any]] = []

    for proc in procs:
        if proc.stdout is None:
            raise RuntimeError("flowbench stdout pipe missing")
        selector.register(proc.stdout, selectors.EVENT_READ, proc)

    while selector.get_map():
        for key, _ in selector.select(timeout=0.25):
            stream = key.fileobj
            proc = key.data
            line = stream.readline()
            if line == "":
                selector.unregister(stream)
                continue

            if not line.strip():
                continue
            item = json.loads(line)
            item["trial"] = trial
            records.append(item)
            if item.get("type") == "oracle_frontier":
                records.append(update_oracle_hint(args, trial, item))

    for proc in procs:
        stderr = proc.stderr.read() if proc.stderr is not None else ""
        ret = proc.wait()
        if ret != 0:
            raise RuntimeError(
                f"flowbench failed with code {ret}\nSTDERR:\n{stderr}"
            )
        if stderr.strip():
            print(stderr, file=sys.stderr)

    return records


def run_trial(args: argparse.Namespace, trial: int) -> list[dict[str, Any]]:
    if args.oracle_hints:
        return run_trial_with_oracle_hints(args, trial)
    return run_trial_simple(args, trial)


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    child = [r for r in records if r.get("type") == "child"]
    stages = [r for r in records if r.get("type") == "stage"]
    bench_end = [r for r in records if r.get("type") == "bench_end"]

    frontier_child = [r for r in child if r.get("frontier")]
    non_frontier_child = [r for r in child if not r.get("frontier")]

    def ns_to_ms(key: str, rows: list[dict[str, Any]]) -> list[float]:
        return [float(r.get(key, 0)) / 1_000_000.0 for r in rows]

    frontier_wait = ns_to_ms("wait_proxy_ns", frontier_child)
    nonfrontier_wait = ns_to_ms("wait_proxy_ns", non_frontier_child)
    frontier_sched_wait = ns_to_ms("sched_wait_proxy_ns", frontier_child)
    nonfrontier_sched_wait = ns_to_ms("sched_wait_proxy_ns", non_frontier_child)
    stage_ms = ns_to_ms("stage_ns", stages)
    episode_ms = ns_to_ms("wall_ns", bench_end)

    total_wait_ns = sum(int(r.get("total_wait_proxy_ns", 0)) for r in stages)
    frontier_wait_ns = sum(int(r.get("frontier_wait_proxy_ns", 0)) for r in stages)
    total_sched_wait_ns = sum(int(r.get("total_sched_wait_proxy_ns", 0)) for r in stages)
    frontier_sched_wait_ns = sum(
        int(r.get("frontier_sched_wait_proxy_ns", 0)) for r in stages
    )

    return {
        "records": len(records),
        "children": len(child),
        "stages": len(stages),
        "episodes": len(bench_end),
        "stage_ms_mean": mean(stage_ms),
        "stage_ms_p95": percentile(stage_ms, 95),
        "episode_ms_mean": mean(episode_ms),
        "frontier_wait_proxy_ms_mean": mean(frontier_wait),
        "frontier_wait_proxy_ms_p95": percentile(frontier_wait, 95),
        "nonfrontier_wait_proxy_ms_mean": mean(nonfrontier_wait),
        "nonfrontier_wait_proxy_ms_p95": percentile(nonfrontier_wait, 95),
        "frontier_sched_wait_proxy_ms_mean": mean(frontier_sched_wait),
        "frontier_sched_wait_proxy_ms_p95": percentile(frontier_sched_wait, 95),
        "nonfrontier_sched_wait_proxy_ms_mean": mean(nonfrontier_sched_wait),
        "nonfrontier_sched_wait_proxy_ms_p95": percentile(nonfrontier_sched_wait, 95),
        "frontier_wait_share": (
            frontier_wait_ns / total_wait_ns if total_wait_ns else 0.0
        ),
        "frontier_sched_wait_share": (
            frontier_sched_wait_ns / total_sched_wait_ns if total_sched_wait_ns else 0.0
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FlowCausal flowbench")
    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--stages", type=int, default=3)
    parser.add_argument("--cohort", type=int, default=8)
    parser.add_argument("--frontier-width", type=int, default=1)
    parser.add_argument("--work-ms", type=int, default=25)
    parser.add_argument("--tail-work-ms", type=int, default=60)
    parser.add_argument("--stage-gap-ms", type=int, default=0)
    parser.add_argument("--start-delay-ms", type=int, default=0)
    parser.add_argument(
        "--taskset",
        default=None,
        help="optional CPU list passed to taskset -c for each flowbench session",
    )
    parser.add_argument("--out-dir", default=None)
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--oracle-hints", action="store_true", help="write oracle frontier hints as soon as flowbench forks the tail child")
    parser.add_argument("--hint-dry-run", action="store_true", help="validate oracle hint commands without writing BPF maps")
    parser.add_argument("--flowctl", default=str(FLOWCTL), help="path to FlowCausal flowctl.py")
    parser.add_argument("--pin-dir", default="/sys/fs/bpf/flowcausal", help="FlowCausal pinned map directory")
    parser.add_argument("--confidence", type=int, default=100, help="oracle frontier confidence")
    parser.add_argument("--ttl-ms", type=int, default=5000, help="oracle hint TTL")
    parser.add_argument("--policy-window-ms", type=int, default=100, help="policy accounting window in milliseconds")
    parser.add_argument("--boost-budget-ms", type=int, default=10, help="frontier boost budget for oracle hints; 0 disables the budget cap")
    parser.add_argument("--max-vtime-boost-us", type=int, default=500, help="maximum virtual-time boost per oracle frontier enqueue")
    parser.add_argument("--anchor-cpu", type=int, default=-1)
    parser.add_argument("--width-control", action="store_true", help="enable FlowCausal width control for oracle hints")
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument("--hint-stage-kind", default="BUILD", help="FlowCausal stage kind for synthetic flowbench frontiers")
    args = parser.parse_args()

    if not args.no_build:
        ensure_built()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = pathlib.Path(args.out_dir or HERE / "results" / f"flowbench-{stamp}")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_records: list[dict[str, Any]] = []
    for trial in range(args.trials):
        all_records.extend(run_trial(args, trial))

    summary = summarize(all_records)
    summary.update(
        {
            "sessions": args.sessions,
            "trials": args.trials,
            "stages_per_session": args.stages,
            "cohort": args.cohort,
            "frontier_width": args.frontier_width,
            "work_ms": args.work_ms,
            "tail_work_ms": args.tail_work_ms,
            "start_delay_ms": args.start_delay_ms,
            "taskset": args.taskset,
            "oracle_hints": bool(args.oracle_hints),
            "hint_dry_run": bool(args.hint_dry_run),
            "width_control": bool(args.width_control),
            "max_frontier_width": args.max_frontier_width,
        }
    )

    raw_path = out_dir / "raw.ndjson"
    summary_path = out_dir / "summary.json"
    with raw_path.open("w", encoding="utf-8") as f:
        for item in all_records:
            f.write(json.dumps(item, sort_keys=True) + "\n")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"raw={raw_path}")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a small reproducible FlowCausal flowbench matrix.

The matrix is intentionally a *mechanism gate*, not a paper-grade performance
study.  It restricts flowbench sessions to a small CPU set to create runnable
pressure, runs default and FlowCausal oracle variants, and collects enough
metadata to check whether the scheduler actually admitted frontier tasks.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FLOWBENCH = ROOT / "experiments" / "flowbench"
FLOWBENCH_RUNNER = FLOWBENCH / "run_flowbench.py"
LIVE_RUNNER = ROOT / "experiments" / "run_live_flowbench.py"
PLOTTER = ROOT / "experiments" / "plot_flowbench_matrix.py"


def run_checked(cmd: list[str], cwd: pathlib.Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def build_tools() -> None:
    run_checked(["make", "check"], cwd=SRC)
    run_checked(["make", "flowbench"], cwd=FLOWBENCH)


def remove_generated_tree(path: pathlib.Path) -> None:
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        if os.geteuid() == 0:
            raise
        subprocess.run(
            ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(path)],
            check=True,
        )
        shutil.rmtree(path)


def common_flowbench_args(args: argparse.Namespace, *, frontier_width: int, start_delay_ms: int) -> list[str]:
    cmd = [
        "--sessions",
        str(args.sessions),
        "--trials",
        str(args.trials),
        "--stages",
        str(args.stages),
        "--cohort",
        str(args.cohort),
        "--frontier-width",
        str(frontier_width),
        "--work-ms",
        str(args.work_ms),
        "--tail-work-ms",
        str(args.tail_work_ms),
        "--stage-gap-ms",
        str(args.stage_gap_ms),
        "--start-delay-ms",
        str(start_delay_ms),
    ]
    if args.taskset:
        cmd.extend(["--taskset", args.taskset])
    return cmd


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_flowmon(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    stats = {
        "sessions": 0,
        "seen_enqueues": 0,
        "frontier_enqueues": 0,
        "fallback_enqueues": 0,
        "width_held_enqueues": 0,
        "frontier_inflight": 0,
        "frontier_width_releases": 0,
        "frontier_runnable_wait_ns": 0,
        "frontier_runtime_ns": 0,
        "total_runnable_wait_ns": 0,
        "total_runtime_ns": 0,
        "global_frontier_dispatches": 0,
        "global_shared_dispatches": 0,
        "global_shared_first_dispatches": 0,
        "global_frontier_guard_trips": 0,
        "global_frontier_guard_misses": 0,
        "global_shared_local_enqueues": 0,
        "global_shared_percpu_enqueues": 0,
        "global_shared_percpu_dispatches": 0,
        "global_shared_percpu_misses": 0,
        "global_background_enqueues": 0,
        "global_background_dispatches": 0,
        "global_frontier_dsq_enqueues": 0,
        "global_tracked_shared_path_enqueues": 0,
        "global_untracked_shared_path_enqueues": 0,
        "global_shared_global_enqueues": 0,
        "global_shared_global_tracked_enqueues": 0,
        "global_shared_global_untracked_enqueues": 0,
        "global_latency_dsq_enqueues": 0,
        "global_shared_local_tracked_enqueues": 0,
        "global_shared_local_untracked_enqueues": 0,
        "global_shared_percpu_tracked_enqueues": 0,
        "global_shared_percpu_untracked_enqueues": 0,
        "global_shared_local_pick_fallbacks": 0,
        "global_shared_percpu_pick_fallbacks": 0,
        "global_shared_local_frontier_fallback_enqueues": 0,
    }
    stat_re = re.compile(
        r"session=(?P<session>\d+) enq=(?P<enq>\d+) frontier=(?P<frontier>\d+) "
        r"fallback=(?P<fallback>\d+) width_held=(?P<width>\d+) "
        r"inflight=(?P<inflight>\d+) width_releases=(?P<releases>\d+)"
    )
    wait_re = re.compile(
        r"wait_ns total=(?P<wait_total>\d+) frontier=(?P<wait_frontier>\d+) "
        r"runtime_ns total=(?P<runtime_total>\d+) frontier=(?P<runtime_frontier>\d+)"
    )
    global_key_map = {
        "frontier": "global_frontier_dispatches",
        "shared": "global_shared_dispatches",
        "shared_first": "global_shared_first_dispatches",
        "guard_trips": "global_frontier_guard_trips",
        "guard_misses": "global_frontier_guard_misses",
        "shared_local_enqueues": "global_shared_local_enqueues",
        "shared_percpu_enqueues": "global_shared_percpu_enqueues",
        "shared_percpu_dispatches": "global_shared_percpu_dispatches",
        "shared_percpu_misses": "global_shared_percpu_misses",
        "background_enqueues": "global_background_enqueues",
        "background_dispatches": "global_background_dispatches",
        "frontier_dsq_enqueues": "global_frontier_dsq_enqueues",
        "tracked_shared_path_enqueues": "global_tracked_shared_path_enqueues",
        "untracked_shared_path_enqueues": "global_untracked_shared_path_enqueues",
        "shared_global_enqueues": "global_shared_global_enqueues",
        "shared_global_tracked_enqueues": "global_shared_global_tracked_enqueues",
        "shared_global_untracked_enqueues": "global_shared_global_untracked_enqueues",
        "latency_dsq_enqueues": "global_latency_dsq_enqueues",
        "shared_local_tracked_enqueues": "global_shared_local_tracked_enqueues",
        "shared_local_untracked_enqueues": "global_shared_local_untracked_enqueues",
        "shared_percpu_tracked_enqueues": "global_shared_percpu_tracked_enqueues",
        "shared_percpu_untracked_enqueues": "global_shared_percpu_untracked_enqueues",
        "shared_local_pick_fallbacks": "global_shared_local_pick_fallbacks",
        "shared_percpu_pick_fallbacks": "global_shared_percpu_pick_fallbacks",
        "shared_local_frontier_fallback_enqueues": (
            "global_shared_local_frontier_fallback_enqueues"
        ),
    }

    pending_session = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stat_match = stat_re.search(line)
        if stat_match:
            stats["sessions"] += 1
            stats["seen_enqueues"] += int(stat_match.group("enq"))
            stats["frontier_enqueues"] += int(stat_match.group("frontier"))
            stats["fallback_enqueues"] += int(stat_match.group("fallback"))
            stats["width_held_enqueues"] += int(stat_match.group("width"))
            stats["frontier_inflight"] += int(stat_match.group("inflight"))
            stats["frontier_width_releases"] += int(stat_match.group("releases"))
            pending_session = True
            continue
        wait_match = wait_re.search(line)
        if wait_match and pending_session:
            stats["total_runnable_wait_ns"] += int(wait_match.group("wait_total"))
            stats["frontier_runnable_wait_ns"] += int(wait_match.group("wait_frontier"))
            stats["total_runtime_ns"] += int(wait_match.group("runtime_total"))
            stats["frontier_runtime_ns"] += int(wait_match.group("runtime_frontier"))
            pending_session = False
            continue
        if "dispatch frontier=" in line:
            pairs = dict(re.findall(r"([A-Za-z_]+)=(\d+)", line))
            for raw_key, stat_key in global_key_map.items():
                value = pairs.get(raw_key)
                if value is not None:
                    stats[stat_key] += int(value)

    stats["frontier_ratio"] = (
        stats["frontier_enqueues"] / stats["seen_enqueues"]
        if stats["seen_enqueues"]
        else 0.0
    )
    stats["fallback_ratio"] = (
        stats["fallback_enqueues"] / stats["seen_enqueues"]
        if stats["seen_enqueues"]
        else 0.0
    )
    return stats


def run_default_variant(
    args: argparse.Namespace,
    name: str,
    out_dir: pathlib.Path,
    *,
    frontier_width: int,
    start_delay_ms: int,
) -> dict[str, Any]:
    variant_dir = out_dir / name
    cmd = [
        sys.executable,
        str(FLOWBENCH_RUNNER),
        *common_flowbench_args(
            args,
            frontier_width=frontier_width,
            start_delay_ms=start_delay_ms,
        ),
        "--out-dir",
        str(variant_dir),
        "--no-build",
    ]
    run_checked(cmd)
    return {
        "name": name,
        "kind": "default",
        "path": str(variant_dir),
        "command": cmd,
        "summary": load_json(variant_dir / "summary.json"),
        "flowmon": {},
    }


def run_live_variant(
    args: argparse.Namespace,
    name: str,
    out_dir: pathlib.Path,
    *,
    frontier_width: int,
    width_control: bool,
) -> dict[str, Any]:
    variant_dir = out_dir / name
    cmd = [
        sys.executable,
        str(LIVE_RUNNER),
        "--attach-scheduler",
        "--no-build-tools",
        *common_flowbench_args(
            args,
            frontier_width=frontier_width,
            start_delay_ms=args.start_delay_ms,
        ),
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
        "--out-dir",
        str(variant_dir),
    ]
    if width_control:
        cmd.extend(["--width-control", "--max-frontier-width", str(args.max_frontier_width)])

    run_checked(cmd)
    return {
        "name": name,
        "kind": "flowcausal",
        "path": str(variant_dir),
        "command": cmd,
        "summary": load_json(variant_dir / "flowbench" / "summary.json"),
        "flowmon": parse_flowmon(variant_dir / "flowmon-final.log"),
    }


def write_csv(matrix: dict[str, Any], path: pathlib.Path) -> None:
    rows = []
    for variant in matrix["variants"]:
        summary = variant["summary"]
        flowmon = variant.get("flowmon", {})
        rows.append(
            {
                "variant": variant["name"],
                "kind": variant["kind"],
                "stage_ms_mean": summary.get("stage_ms_mean", 0.0),
                "episode_ms_mean": summary.get("episode_ms_mean", 0.0),
                "frontier_sched_wait_p95_ms": summary.get("frontier_sched_wait_proxy_ms_p95", 0.0),
                "frontier_sched_wait_share": summary.get("frontier_sched_wait_share", 0.0),
                "frontier_enqueues": flowmon.get("frontier_enqueues", 0),
                "fallback_enqueues": flowmon.get("fallback_enqueues", 0),
                "width_held_enqueues": flowmon.get("width_held_enqueues", 0),
            }
        )

    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(matrix: dict[str, Any], path: pathlib.Path) -> None:
    lines = [
        "# FlowCausal flowbench preliminary matrix",
        "",
        "This is a synthetic mechanism gate.  Live oracle-hint variants use a",
        "start-delay synchronization window; stage/episode wall time is not a",
        "FlowCausal speedup claim.",
        "",
        "| Variant | Stage mean ms | Frontier sched-wait p95 ms | Sched-wait share | Frontier enq | Fallback enq | Width-held |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in matrix["variants"]:
        summary = variant["summary"]
        flowmon = variant.get("flowmon", {})
        lines.append(
            "| {name} | {stage:.2f} | {wait:.2f} | {share:.3f} | {frontier} | {fallback} | {width} |".format(
                name=variant["name"],
                stage=float(summary.get("stage_ms_mean", 0.0)),
                wait=float(summary.get("frontier_sched_wait_proxy_ms_p95", 0.0)),
                share=float(summary.get("frontier_sched_wait_share", 0.0)),
                frontier=int(flowmon.get("frontier_enqueues", 0)),
                fallback=int(flowmon.get("fallback_enqueues", 0)),
                width=int(flowmon.get("width_held_enqueues", 0)),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run FlowCausal flowbench matrix")
    parser.add_argument("--out-dir", default=str(ROOT / "experiments" / "results" / "flowbench-prelim"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-live", action="store_true", help="run only default variants")
    parser.add_argument("--skip-plot", action="store_true")
    parser.add_argument("--taskset", default="0-3", help="CPU list used for each flowbench session")
    parser.add_argument("--sessions", type=int, default=8)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--stages", type=int, default=3)
    parser.add_argument("--cohort", type=int, default=8)
    parser.add_argument("--work-ms", type=int, default=15)
    parser.add_argument("--tail-work-ms", type=int, default=35)
    parser.add_argument("--stage-gap-ms", type=int, default=0)
    parser.add_argument("--start-delay-ms", type=int, default=500)
    parser.add_argument("--confidence", type=int, default=100)
    parser.add_argument("--ttl-ms", type=int, default=5000)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=10)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--max-frontier-width", type=int, default=1)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists():
        if not args.overwrite:
            raise FileExistsError(f"{out_dir} exists; pass --overwrite to replace generated matrix outputs")
        remove_generated_tree(out_dir)
    out_dir.mkdir(parents=True)

    build_tools()
    variants: list[dict[str, Any]] = []
    variants.append(
        run_default_variant(
            args,
            "default",
            out_dir,
            frontier_width=1,
            start_delay_ms=0,
        )
    )
    variants.append(
        run_default_variant(
            args,
            "default_sync",
            out_dir,
            frontier_width=2,
            start_delay_ms=args.start_delay_ms,
        )
    )
    if not args.skip_live:
        variants.append(
            run_live_variant(
                args,
                "flowcausal_oracle",
                out_dir,
                frontier_width=1,
                width_control=False,
            )
        )
        variants.append(
            run_live_variant(
                args,
                "flowcausal_width",
                out_dir,
                frontier_width=2,
                width_control=True,
            )
        )

    matrix = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "config": {
            "taskset": args.taskset,
            "sessions": args.sessions,
            "trials": args.trials,
            "stages": args.stages,
            "cohort": args.cohort,
            "work_ms": args.work_ms,
            "tail_work_ms": args.tail_work_ms,
            "start_delay_ms": args.start_delay_ms,
            "confidence": args.confidence,
            "ttl_ms": args.ttl_ms,
            "policy_window_ms": args.policy_window_ms,
            "boost_budget_ms": args.boost_budget_ms,
            "max_vtime_boost_us": args.max_vtime_boost_us,
            "max_frontier_width": args.max_frontier_width,
        },
        "warning": (
            "Synthetic mechanism gate only. Live oracle-hint variants use "
            "start-delay synchronization; do not interpret stage wall time as "
            "a FlowCausal performance result."
        ),
        "variants": variants,
    }

    summary_path = out_dir / "matrix-summary.json"
    summary_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(matrix, out_dir / "matrix-summary.csv")
    write_markdown(matrix, out_dir / "README.md")

    if not args.skip_plot:
        run_checked(
            [
                sys.executable,
                str(PLOTTER),
                "--summary",
                str(summary_path),
                "--out-prefix",
                str(ROOT / "paper" / "figures" / "fig-flowbench-prelim"),
            ]
        )

    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run a FlowCausal live flowbench experiment.

This script is deliberately conservative.  It can build the local helper
binaries, optionally attach the FlowCausal sched_ext scheduler, start flowmon,
run flowbench in oracle-hint mode, and collect all logs under /tmp by default.

Attaching a sched_ext scheduler changes the machine-wide CPU scheduler while
the process is alive, so it only happens with --attach-scheduler.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FLOWBENCH = ROOT / "experiments" / "flowbench"
FLOWBENCH_RUNNER = FLOWBENCH / "run_flowbench.py"


def sudo_prefix() -> list[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def run_checked(cmd: list[str], cwd: pathlib.Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def ensure_sudo_available() -> None:
    if os.geteuid() == 0:
        return
    subprocess.run(["sudo", "-n", "true"], check=True)


def build_tools() -> None:
    run_checked(["make", "check"], cwd=SRC)
    run_checked(["make", "flowbench"], cwd=FLOWBENCH)


def map_exists(path: pathlib.Path) -> bool:
    try:
        return path.exists()
    except PermissionError:
        probe = sudo_prefix() + ["test", "-e", str(path)]
        return subprocess.run(
            probe,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0


def wait_for_maps(pin_dir: pathlib.Path, timeout_sec: float, scheduler: subprocess.Popen[str] | None) -> None:
    maps = ["task_ctx", "session_policy", "session_stats", "events"]
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if scheduler is not None and scheduler.poll() is not None:
            raise RuntimeError(f"scheduler exited early with code {scheduler.returncode}")
        if all(map_exists(pin_dir / name) for name in maps):
            return
        time.sleep(0.1)
    missing = [name for name in maps if not map_exists(pin_dir / name)]
    raise TimeoutError(f"FlowCausal maps were not pinned in time: {missing}")


def start_logged(cmd: list[str], log_path: pathlib.Path) -> subprocess.Popen[str]:
    print("+ " + " ".join(cmd), flush=True)
    log = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, text=True)


def stop_process(proc: subprocess.Popen[str] | None, name: str, timeout_sec: float = 5.0) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    proc.send_signal(signal.SIGINT)
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.terminate()
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        ret = proc.wait()
        print(f"{name} required SIGKILL", file=sys.stderr)
        return ret


def restore_output_ownership(out_dir: pathlib.Path) -> None:
    """Make generated logs/results editable by the invoking user.

    Live benchmark runs may execute the nested flowbench runner through sudo so
    it can update pinned BPF maps.  That makes raw result files root-owned
    unless we hand them back at the end.
    """
    if os.geteuid() == 0:
        return
    cmd = ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(out_dir)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def parse_args() -> argparse.Namespace:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(description="Run FlowCausal live flowbench experiment")
    parser.add_argument("--out-dir", default=f"/tmp/flowcausal-live-{stamp}")
    parser.add_argument("--pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--scheduler", default="/tmp/scx_flowcausal")
    parser.add_argument("--flowmon", default="/tmp/flowmon")
    parser.add_argument("--attach-scheduler", action="store_true", help="start scx_flowcausal for this run")
    parser.add_argument("--no-flowmon", action="store_true")
    parser.add_argument("--no-final-snapshot", action="store_true", help="skip flowmon --once after the benchmark")
    parser.add_argument("--no-build-tools", action="store_true")
    parser.add_argument("--hint-dry-run", action="store_true", help="validate hint path without writing BPF maps")

    parser.add_argument("--sessions", type=int, default=2)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--stages", type=int, default=2)
    parser.add_argument("--cohort", type=int, default=4)
    parser.add_argument("--frontier-width", type=int, default=1)
    parser.add_argument("--work-ms", type=int, default=5)
    parser.add_argument("--tail-work-ms", type=int, default=12)
    parser.add_argument("--stage-gap-ms", type=int, default=0)
    parser.add_argument(
        "--start-delay-ms",
        type=int,
        default=500,
        help=(
            "child sleep before CPU work so live oracle hints reach BPF before "
            "wakeup; use 0 for normal characterization without synchronization delay"
        ),
    )

    parser.add_argument("--width-control", action="store_true")
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument(
        "--taskset",
        default=None,
        help="optional CPU list passed to taskset -c for each flowbench session",
    )
    parser.add_argument("--ttl-ms", type=int, default=5000)
    parser.add_argument("--confidence", type=int, default=100)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=10)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--anchor-cpu", type=int, default=-1)
    parser.add_argument("--startup-timeout-sec", type=float, default=10.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pin_dir = pathlib.Path(args.pin_dir)

    scheduler_proc: subprocess.Popen[str] | None = None
    flowmon_proc: subprocess.Popen[str] | None = None
    runner_rc: int | None = None

    manifest: dict[str, Any] = {
        "out_dir": str(out_dir),
        "pin_dir": str(pin_dir),
        "attach_scheduler": bool(args.attach_scheduler),
        "flowmon_enabled": not args.no_flowmon,
        "hint_dry_run": bool(args.hint_dry_run),
        "flowbench": {
            "sessions": args.sessions,
            "trials": args.trials,
            "stages": args.stages,
            "cohort": args.cohort,
            "frontier_width": args.frontier_width,
            "work_ms": args.work_ms,
            "tail_work_ms": args.tail_work_ms,
            "stage_gap_ms": args.stage_gap_ms,
            "start_delay_ms": args.start_delay_ms,
            "taskset": args.taskset,
            "width_control": bool(args.width_control),
            "max_frontier_width": args.max_frontier_width,
            "policy_window_ms": args.policy_window_ms,
            "boost_budget_ms": args.boost_budget_ms,
            "max_vtime_boost_us": args.max_vtime_boost_us,
        },
    }

    try:
        if not args.no_build_tools:
            build_tools()

        if args.attach_scheduler or (not args.hint_dry_run):
            ensure_sudo_available()

        scheduler = pathlib.Path(args.scheduler)
        flowmon = pathlib.Path(args.flowmon)
        if args.attach_scheduler and not scheduler.exists():
            raise FileNotFoundError(f"scheduler binary not found: {scheduler}")
        if not args.no_flowmon and not flowmon.exists():
            raise FileNotFoundError(f"flowmon binary not found: {flowmon}")

        if args.attach_scheduler:
            scheduler_cmd = sudo_prefix() + [str(scheduler), "--pin-dir", str(pin_dir)]
            manifest["scheduler_cmd"] = scheduler_cmd
            scheduler_proc = start_logged(scheduler_cmd, out_dir / "scheduler.log")
            wait_for_maps(pin_dir, args.startup_timeout_sec, scheduler_proc)
        elif not args.hint_dry_run:
            wait_for_maps(pin_dir, args.startup_timeout_sec, None)

        if not args.no_flowmon and (args.attach_scheduler or not args.hint_dry_run):
            flowmon_cmd = sudo_prefix() + [
                str(flowmon),
                "--pin-dir",
                str(pin_dir),
                "--events",
                "--interval",
                "1",
            ]
            manifest["flowmon_cmd"] = flowmon_cmd
            flowmon_proc = start_logged(flowmon_cmd, out_dir / "flowmon.log")

        flowbench_out = out_dir / "flowbench"
        runner_cmd = [
            sys.executable,
            str(FLOWBENCH_RUNNER),
            "--sessions",
            str(args.sessions),
            "--trials",
            str(args.trials),
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
            "--oracle-hints",
            "--pin-dir",
            str(pin_dir),
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
            str(flowbench_out),
            "--no-build",
        ]
        if args.taskset:
            runner_cmd.extend(["--taskset", args.taskset])
        if args.anchor_cpu >= 0:
            runner_cmd.extend(["--anchor-cpu", str(args.anchor_cpu)])
        if args.width_control:
            runner_cmd.extend(["--width-control", "--max-frontier-width", str(args.max_frontier_width)])
        if args.hint_dry_run:
            runner_cmd.append("--hint-dry-run")
        elif sudo_prefix():
            runner_cmd = sudo_prefix() + runner_cmd

        manifest["runner_cmd"] = runner_cmd
        with (out_dir / "runner.log").open("w", encoding="utf-8") as log:
            print("+ " + " ".join(runner_cmd), flush=True)
            runner = subprocess.run(runner_cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
            runner_rc = runner.returncode
            if runner.returncode != 0:
                raise RuntimeError(f"flowbench runner failed with code {runner.returncode}")

        if (
            not args.no_flowmon
            and not args.no_final_snapshot
            and (args.attach_scheduler or not args.hint_dry_run)
        ):
            final_cmd = sudo_prefix() + [str(flowmon), "--pin-dir", str(pin_dir), "--once"]
            manifest["flowmon_final_cmd"] = final_cmd
            with (out_dir / "flowmon-final.log").open("w", encoding="utf-8") as log:
                print("+ " + " ".join(final_cmd), flush=True)
                final = subprocess.run(final_cmd, stdout=log, stderr=subprocess.STDOUT, text=True)
                manifest["flowmon_final_returncode"] = final.returncode
                if final.returncode != 0:
                    raise RuntimeError(f"flowmon final snapshot failed with code {final.returncode}")
    finally:
        manifest["flowmon_returncode"] = stop_process(flowmon_proc, "flowmon")
        manifest["scheduler_returncode"] = stop_process(scheduler_proc, "scheduler")
        manifest["runner_returncode"] = runner_rc
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        restore_output_ownership(out_dir)

    print(f"results={out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

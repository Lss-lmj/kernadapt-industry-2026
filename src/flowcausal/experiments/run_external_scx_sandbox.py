#!/usr/bin/env python3
"""Run a workload under an arbitrary external sched_ext scheduler.

This wrapper is intentionally thinner than ``run_live_sandbox.py``.  It does
not start FlowCausal flowctl, flowmon, or any FlowCausal-specific BPF maps.  It
only owns the lifecycle of an already-built scx scheduler binary and a workload
command, so generic sched_ext baselines can be compared through the same matrix
runner as Linux/default and FlowCausal variants.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import time
from typing import Any

from run_live_sandbox import (
    TerminationSignal,
    install_termination_handlers,
    restore_output_ownership,
    start_logged,
    stop_process_group,
    sysfs_value,
)


SCHEMA = "flowcausal.external_scx_sandbox.v1"


def sudo_prefix(mode: str) -> list[str]:
    if mode == "never" or os.geteuid() == 0:
        return []
    if mode in {"auto", "always"}:
        return ["sudo", "-n"]
    raise ValueError(f"unsupported sudo mode: {mode}")


def parse_workload_command(command: list[str]) -> list[str]:
    out = list(command)
    if out and out[0] == "--":
        out = out[1:]
    if not out:
        raise SystemExit("missing workload command; pass it after --")
    return out


def build_scheduler_cmd(args: argparse.Namespace) -> list[str]:
    scheduler = pathlib.Path(args.scheduler)
    if not scheduler.exists():
        raise FileNotFoundError(f"scheduler binary not found: {scheduler}")
    return sudo_prefix(args.scheduler_sudo) + [str(scheduler), *args.scheduler_arg]


def build_workload_cmd(args: argparse.Namespace, command: list[str]) -> list[str]:
    workload_cmd = parse_workload_command(command)
    if args.taskset:
        workload_cmd = ["taskset", "-c", args.taskset, *workload_cmd]
    return workload_cmd


def build_victim_cmd(args: argparse.Namespace) -> list[str] | None:
    if not args.victim_command:
        return None
    victim_cmd = shlex.split(args.victim_command)
    if not victim_cmd:
        raise SystemExit("--victim-command parsed to an empty command")
    if args.victim_taskset:
        victim_cmd = ["taskset", "-c", args.victim_taskset, *victim_cmd]
    return victim_cmd


def wait_for_scheduler_ready(
    proc: subprocess.Popen[str],
    *,
    timeout_sec: float,
    startup_grace_sec: float,
) -> dict[str, Any]:
    """Wait briefly for sched_ext state to become non-disabled.

    Unit tests and dry plumbing runs can set ``timeout_sec=0`` to skip this
    check.  If the sysfs state file is unavailable, the wrapper falls back to a
    small grace period and only verifies that the scheduler process did not
    exit immediately.
    """

    if startup_grace_sec > 0:
        time.sleep(startup_grace_sec)
    state = sysfs_value("/sys/kernel/sched_ext/state")
    if timeout_sec <= 0:
        if proc.poll() is not None:
            raise RuntimeError(f"scheduler exited early with code {proc.returncode}")
        return {"status": "skipped", "state": state}

    deadline = time.monotonic() + timeout_sec
    last_state = state
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"scheduler exited early with code {proc.returncode}")
        last_state = sysfs_value("/sys/kernel/sched_ext/state")
        if last_state and last_state not in {"disabled", "enabling", "disabling"}:
            return {"status": "enabled", "state": last_state}
        time.sleep(0.05)
    if last_state is None:
        return {"status": "unavailable", "state": last_state}
    raise TimeoutError(f"sched_ext did not become enabled in time; last state={last_state!r}")


def parse_args() -> argparse.Namespace:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description="Run a workload under an arbitrary external sched_ext scheduler",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=f"/tmp/flowcausal-external-scx-{stamp}")
    parser.add_argument("--scheduler", required=True)
    parser.add_argument("--scheduler-arg", action="append", default=[])
    parser.add_argument("--scheduler-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--scheduler-ready-timeout-sec", type=float, default=3.0)
    parser.add_argument("--scheduler-startup-grace-sec", type=float, default=0.1)
    parser.add_argument("--taskset", default="", help="optional CPU list for the workload command")
    parser.add_argument("--victim-command", default="")
    parser.add_argument("--victim-taskset", default="")
    parser.add_argument("--victim-warmup-sec", type=float, default=0.2)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args()


def main() -> int:
    install_termination_handlers()
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    scheduler_cmd = build_scheduler_cmd(args)
    workload_cmd = build_workload_cmd(args, args.command)
    victim_cmd = build_victim_cmd(args)

    scheduler_proc: subprocess.Popen[str] | None = None
    workload_proc: subprocess.Popen[str] | None = None
    victim_proc: subprocess.Popen[str] | None = None
    exit_code = 0
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "out_dir": str(out_dir),
        "scheduler_cmd": scheduler_cmd,
        "scheduler": str(pathlib.Path(args.scheduler).resolve()),
        "scheduler_arg": list(args.scheduler_arg),
        "scheduler_sudo": args.scheduler_sudo,
        "scheduler_ready_timeout_sec": args.scheduler_ready_timeout_sec,
        "scheduler_startup_grace_sec": args.scheduler_startup_grace_sec,
        "workload_cmd": workload_cmd,
        "victim_cmd": victim_cmd,
        "victim_warmup_sec": args.victim_warmup_sec,
        "sched_ext_state_before": sysfs_value("/sys/kernel/sched_ext/state"),
        "sched_ext_nr_rejected_before": sysfs_value("/sys/kernel/sched_ext/nr_rejected"),
    }

    try:
        scheduler_proc = start_logged(scheduler_cmd, out_dir / "scheduler.log", start_new_session=True)
        manifest["scheduler_pid"] = scheduler_proc.pid
        manifest["scheduler_ready"] = wait_for_scheduler_ready(
            scheduler_proc,
            timeout_sec=max(0.0, float(args.scheduler_ready_timeout_sec)),
            startup_grace_sec=max(0.0, float(args.scheduler_startup_grace_sec)),
        )

        if victim_cmd:
            victim_proc = start_logged(victim_cmd, out_dir / "victim.log", start_new_session=True)
            if args.victim_warmup_sec > 0:
                time.sleep(args.victim_warmup_sec)
            if victim_proc.poll() is not None and victim_proc.returncode != 0:
                raise RuntimeError(f"victim exited early with code {victim_proc.returncode}")

        workload_proc = start_logged(workload_cmd, out_dir / "workload.log", start_new_session=True)
        manifest["root_pid"] = workload_proc.pid
        manifest["workload_returncode"] = workload_proc.wait()
        if manifest["workload_returncode"] != 0:
            raise RuntimeError(f"workload failed with code {manifest['workload_returncode']}")

        if victim_proc is not None:
            if victim_proc.poll() is None:
                manifest["victim_stopped_with_workload"] = True
                manifest["victim_returncode"] = stop_process_group(victim_proc, "victim")
            else:
                manifest["victim_stopped_with_workload"] = False
                manifest["victim_returncode"] = victim_proc.returncode
                if victim_proc.returncode not in (0, None):
                    raise RuntimeError(f"victim failed with code {victim_proc.returncode}")
            victim_proc = None
    except TerminationSignal as exc:
        manifest["terminated_by_signal"] = exc.signum
        exit_code = 128 + exc.signum
    except Exception as exc:
        manifest["error"] = repr(exc)
        exit_code = 1
    finally:
        if workload_proc is not None and workload_proc.poll() is None:
            manifest["workload_returncode"] = stop_process_group(workload_proc, "workload")
        if "victim_returncode" not in manifest:
            manifest["victim_returncode"] = stop_process_group(victim_proc, "victim")
        if "scheduler_returncode" not in manifest:
            manifest["scheduler_returncode"] = stop_process_group(scheduler_proc, "scheduler")
        manifest["sched_ext_state_after"] = sysfs_value("/sys/kernel/sched_ext/state")
        manifest["sched_ext_nr_rejected_after"] = sysfs_value("/sys/kernel/sched_ext/nr_rejected")
        (out_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        restore_output_ownership(out_dir)

    print(f"results={out_dir}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

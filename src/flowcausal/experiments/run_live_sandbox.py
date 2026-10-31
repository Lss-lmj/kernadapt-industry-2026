#!/usr/bin/env python3
"""Run an arbitrary sandbox/workflow command under FlowCausal observation.

This is the non-oracle experiment harness.  It starts a workload command,
uses flowctl.py --watch to infer the current frontier from the command's
process subtree, optionally attaches the FlowCausal sched_ext scheduler, and
collects controller, workload, scheduler, and flowmon logs in one directory.

The harness intentionally does not know the true frontier.  Its job is to make
online inference auditable so real build/test/code-agent workflows can be
compared against the oracle flowbench gate.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import shlex
import subprocess
import sys
import time
from typing import Any

from run_characterize_sandbox import (
    PidSample,
    flowctl as characterize_flowctl,
    sample_tree as characterize_sample_tree,
    write_summary as write_characterization_summary,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
FLOWCTL = SRC / "controller" / "flowctl.py"


class TerminationSignal(RuntimeError):
    """Raised when the harness is asked to stop so cleanup still runs."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"received signal {signum}")
        self.signum = signum


def _raise_termination(signum: int, _frame: Any) -> None:
    raise TerminationSignal(signum)


def install_termination_handlers() -> None:
    signal.signal(signal.SIGINT, _raise_termination)
    signal.signal(signal.SIGTERM, _raise_termination)


def sysfs_value(path: str) -> str | None:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None
    except PermissionError:
        probe = sudo_prefix() + ["cat", path]
        proc = subprocess.run(
            probe,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return proc.stdout.strip() if proc.returncode == 0 else None


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
    maps = ["task_ctx", "session_policy", "session_stats", "global_stats", "events"]
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if scheduler is not None and scheduler.poll() is not None:
            raise RuntimeError(f"scheduler exited early with code {scheduler.returncode}")
        if all(map_exists(pin_dir / name) for name in maps):
            return
        time.sleep(0.1)
    missing = [name for name in maps if not map_exists(pin_dir / name)]
    raise TimeoutError(f"FlowCausal maps were not pinned in time: {missing}")


def start_logged(
    cmd: list[str],
    log_path: pathlib.Path,
    *,
    start_new_session: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    print("+ " + " ".join(cmd), flush=True)
    log = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=start_new_session,
        env=env,
    )


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


def stop_process_group(proc: subprocess.Popen[str] | None, name: str, timeout_sec: float = 5.0) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.send_signal(signal.SIGINT)
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.terminate()
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.kill()
    ret = proc.wait()
    print(f"{name} required SIGKILL", file=sys.stderr)
    return ret


def wait_then_stop_process_group(
    proc: subprocess.Popen[str] | None,
    name: str,
    *,
    settle_timeout_sec: float = 1.0,
    stop_timeout_sec: float = 5.0,
) -> int | None:
    """Let a cooperative helper drain and exit before sending SIGINT.

    The oracle frontier driver tails a label file.  Once the workload root PID
    is gone, it can usually consume the final records and return 0 by itself.
    Waiting briefly keeps manifests cleaner while still preserving the existing
    hard-stop path for stuck helpers.
    """

    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    try:
        return proc.wait(timeout=settle_timeout_sec)
    except subprocess.TimeoutExpired:
        return stop_process_group(proc, name, timeout_sec=stop_timeout_sec)


def restore_output_ownership(out_dir: pathlib.Path) -> None:
    if os.geteuid() == 0:
        return
    cmd = ["sudo", "-n", "chown", "-R", f"{os.getuid()}:{os.getgid()}", str(out_dir)]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def prepare_oracle_frontier_label_file(path: pathlib.Path) -> None:
    """Create the oracle-label mirror before any sudo helper can touch it.

    ``run_oracle_frontier_driver.py`` is often launched through ``sudo -n`` so
    it can write BPF maps.  If that root process creates the JSONL file first,
    the unprivileged workload cannot append its ``frontier_label`` records.
    Pre-creating/truncating the file from the live harness keeps ownership with
    the workload user while still allowing the sudo oracle driver to read it.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8"):
        pass


def parse_args() -> argparse.Namespace:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description="Run a real sandbox/workflow command with FlowCausal online frontier inference",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=f"/tmp/flowcausal-sandbox-{stamp}")
    parser.add_argument("--pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--scheduler", default="/tmp/scx_flowcausal")
    parser.add_argument(
        "--frontier-dispatch-quota",
        type=int,
        default=4,
        help="try shared DSQ first after this many consecutive frontier dispatches; 0 disables the guard",
    )
    parser.add_argument(
        "--guard-cooldown-dispatches",
        type=int,
        default=0,
        help="after a successful shared-first guard dispatch, keep trying shared first for up to this many dispatch opportunities",
    )
    parser.add_argument(
        "--pressure-fallback-enqueues",
        type=int,
        default=0,
        help="after shared pressure is observed by a safety probe, send the next N admitted frontier enqueues to the shared DSQ",
    )
    parser.add_argument(
        "--pressure-fallback-ms",
        type=int,
        default=0,
        help="after shared pressure is observed, keep pressure fallback open for up to this many milliseconds; 0 disables",
    )
    parser.add_argument(
        "--shared-wait-pressure-us",
        type=int,
        default=0,
        help="open pressure fallback when an untracked shared task waits at least this many microseconds before running; 0 disables",
    )
    parser.add_argument(
        "--pressure-dispatch-ms",
        type=int,
        default=0,
        help="after shared pressure is observed, try the shared DSQ before the frontier DSQ for up to this many milliseconds; 0 disables",
    )
    parser.add_argument(
        "--pressure-local-cpu",
        action="store_true",
        help="confine pressure fallback/dispatch to the CPU that observed shared wait pressure",
    )
    parser.add_argument(
        "--locality-frontier-only",
        action="store_true",
        help="apply FlowCausal locality anchors only to frontier tasks",
    )
    parser.add_argument(
        "--locality-remembered-first",
        action="store_true",
        help="prefer each task's remembered CPU before falling back to its session anchor",
    )
    parser.add_argument(
        "--frontier-dispatch-window-ms",
        type=int,
        default=0,
        help="wall-clock window for the soft frontier dispatch budget; 0 disables",
    )
    parser.add_argument(
        "--frontier-dispatch-window-budget",
        type=int,
        default=0,
        help="after N frontier dispatches in the window, try shared first; 0 disables",
    )
    parser.add_argument(
        "--shared-slice-us",
        type=int,
        default=0,
        help="slice for unhinted/shared/fallback tasks; 0 keeps the scheduler default",
    )
    parser.add_argument(
        "--shared-wakeup-preempt",
        action="store_true",
        help="preempt the previous CPU when a shared/fallback task wakes after blocking",
    )
    parser.add_argument(
        "--shared-wakeup-slice-us",
        type=int,
        default=0,
        help="slice only for untracked blocked-wakeup latency-DSQ tasks; 0 follows --shared-slice-us/default",
    )
    parser.add_argument(
        "--shared-wakeup-min-block-us",
        type=int,
        default=0,
        help="minimum blocked time before an untracked shared wakeup can preempt; 0 preserves legacy behavior",
    )
    parser.add_argument(
        "--frontier-wakeup-preempt",
        action="store_true",
        help="preempt the previous CPU when an admitted frontier task wakes after blocking",
    )
    parser.add_argument(
        "--shared-local-dsq",
        action="store_true",
        help="enqueue ordinary shared/fallback tasks to target CPU local DSQs while keeping frontier/latency DSQs dedicated",
    )
    parser.add_argument(
        "--tracked-local-dsq",
        action="store_true",
        help="enqueue only FlowCausal-tracked fallback workflow tasks to target CPU local DSQs; untracked co-runners stay on latency/global shared paths",
    )
    parser.add_argument(
        "--frontier-fallback-local-dsq",
        action="store_true",
        help="enqueue only frontier fallback workflow tasks to target CPU local DSQs; this is narrower than --tracked-local-dsq",
    )
    parser.add_argument(
        "--shared-percpu-dsq",
        action="store_true",
        help="enqueue ordinary shared/fallback tasks to FlowCausal-owned per-CPU shared DSQs",
    )
    parser.add_argument(
        "--background-dsq",
        action="store_true",
        help="enqueue controller-marked sandbox background tasks to a low-priority background DSQ",
    )
    parser.add_argument(
        "--background-dispatch-quota",
        type=int,
        default=0,
        help=(
            "when --background-dsq is enabled, try one background dispatch after "
            "this many foreground dispatches; 0 drains background only last"
        ),
    )
    parser.add_argument("--flowmon", default="/tmp/flowmon")
    parser.add_argument("--root-flowd", action="store_true", help="register the workload supervisor root with C flowd --once")
    parser.add_argument("--root-flowd-bin", default="/tmp/flowd")
    parser.add_argument("--root-flowd-session", default="harness:supervisor")
    parser.add_argument("--root-flowd-anchor-cpu", type=int, default=-1)
    parser.add_argument("--root-flowd-propagation-depth", type=int, default=0)
    parser.add_argument("--flowctl", default=str(FLOWCTL))
    parser.add_argument("--attach-scheduler", action="store_true", help="start scx_flowcausal for this run")
    parser.add_argument(
        "--no-controller",
        action="store_true",
        help="do not start the online flowctl watcher; useful for sched_ext attach overhead baselines",
    )
    parser.add_argument("--no-flowmon", action="store_true")
    parser.add_argument(
        "--no-event-flowmon",
        action="store_true",
        help="skip the continuous flowmon --events sampler but keep the final flowmon --once snapshot",
    )
    parser.add_argument("--no-final-snapshot", action="store_true", help="skip flowmon --once after the workload")
    parser.add_argument("--no-build-tools", action="store_true")
    parser.add_argument("--hint-dry-run", action="store_true", help="run flowctl watcher without writing BPF maps")
    parser.add_argument("--session", default=None, help="stable session name; default uses workload pid")
    parser.add_argument("--stage-kind", default="AUTO", help="AUTO or a forced FlowCausal stage kind")
    parser.add_argument(
        "--stage-state-file",
        default=None,
        help=(
            "optional live stage JSON file; defaults to <out-dir>/stage-state.json "
            "and is exported to the workload as FLOWCAUSAL_STAGE_STATE_FILE"
        ),
    )
    parser.add_argument(
        "--oracle-frontier-label-file",
        default=None,
        help=(
            "optional JSONL file written by the workload and tailed by an oracle "
            "frontier driver to replay frontier_label records as flowctl hints"
        ),
    )
    parser.add_argument(
        "--oracle-frontier-driver",
        default=str(ROOT / "experiments" / "run_oracle_frontier_driver.py"),
        help="helper script that tails oracle frontier labels and writes flowctl hints",
    )
    parser.add_argument("--frontier-width", type=int, default=1)
    parser.add_argument("--adaptive-frontier-width", action="store_true")
    parser.add_argument("--adaptive-max-frontier-width", type=int, default=8)
    parser.add_argument("--min-frontier-score", type=int, default=50)
    parser.add_argument("--frontier-propagation", action="store_true")
    parser.add_argument("--frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--adaptive-frontier-propagation-depth", action="store_true")
    parser.add_argument("--stage-aware-frontier-propagation-depth", action="store_true")
    parser.add_argument("--frontier-stability-samples", type=int, default=1)
    parser.add_argument("--frontier-hold-ms", type=int, default=0)
    parser.add_argument("--frontier-bypass-score", type=int, default=140)
    parser.add_argument(
        "--frontier-selected-stability",
        action="store_true",
        help="count only currently selected frontier pids toward stability admission",
    )
    parser.add_argument(
        "--frontier-stability-action",
        choices=("suppress", "observe"),
        default="suppress",
        help="suppress unstable selected frontiers or observe them through fallback until stable",
    )
    parser.add_argument(
        "--frontier-action",
        default="advance",
        choices=("advance", "auto", "observe", "stage-aware"),
        help="advance selected frontiers, observe them through fallback, or auto-refuse ambiguous snapshots",
    )
    parser.add_argument(
        "--build-frontier-admission",
        default="advance",
        choices=("advance", "tail-only", "observe"),
        help=(
            "BUILD-stage admission override: advance as before, observe all "
            "BUILD frontiers, or advance only link/archive and wider barrier-tail "
            "BUILD frontiers"
        ),
    )
    parser.add_argument(
        "--policy-fallback",
        action="store_true",
        help="write frontier hints but set the session fallback flag so BPF keeps them on the shared DSQ",
    )
    parser.add_argument("--width-control", action="store_true")
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument("--confidence", type=int, default=75)
    parser.add_argument("--ttl-ms", type=int, default=1000)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=10)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--interval-ms", type=int, default=100)
    parser.add_argument("--anchor-cpu", type=int, default=-1)
    parser.add_argument("--taskset", default=None, help="optional CPU list for the workload command")
    parser.add_argument(
        "--collect-characterization",
        action="store_true",
        help=(
            "sample the workload process tree and /proc/<pid>/schedstat while the "
            "live harness is running; writes characterization/summary.json"
        ),
    )
    parser.add_argument(
        "--characterization-interval-ms",
        type=int,
        default=50,
        help="sampling interval for --collect-characterization",
    )
    parser.add_argument(
        "--characterization-top-candidates",
        type=int,
        default=6,
        help="number of ranked frontier candidates to keep per characterization sample",
    )
    parser.add_argument(
        "--victim-command",
        default=None,
        help="optional shell-style sidecar command run outside the observed sandbox root",
    )
    parser.add_argument("--victim-taskset", default=None, help="optional CPU list for the victim command")
    parser.add_argument(
        "--victim-warmup-sec",
        type=float,
        default=0.2,
        help="delay after starting the victim sidecar before starting the sandbox workload",
    )
    parser.add_argument("--startup-timeout-sec", type=float, default=10.0)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="workload command after --")
    return parser.parse_args()


def main() -> int:
    install_termination_handlers()
    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing workload command; pass it after --")

    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    pin_dir = pathlib.Path(args.pin_dir)
    task_map = pin_dir / "task_ctx"
    policy_map = pin_dir / "session_policy"
    stage_state_file = (
        pathlib.Path(args.stage_state_file).resolve()
        if args.stage_state_file
        else out_dir / "stage-state.json"
    )
    workload_env = os.environ.copy()
    workload_env["FLOWCAUSAL_STAGE_STATE_FILE"] = str(stage_state_file)
    oracle_frontier_label_file = (
        pathlib.Path(args.oracle_frontier_label_file).resolve()
        if args.oracle_frontier_label_file
        else None
    )
    if oracle_frontier_label_file is not None:
        prepare_oracle_frontier_label_file(oracle_frontier_label_file)
        workload_env["FLOWCAUSAL_FRONTIER_LABEL_FILE"] = str(oracle_frontier_label_file)

    scheduler_proc: subprocess.Popen[str] | None = None
    flowmon_proc: subprocess.Popen[str] | None = None
    controller_proc: subprocess.Popen[str] | None = None
    oracle_proc: subprocess.Popen[str] | None = None
    workload_proc: subprocess.Popen[str] | None = None
    victim_proc: subprocess.Popen[str] | None = None
    characterization_records: list[dict[str, Any]] = []
    characterization_previous: dict[int, PidSample] = {}
    characterization_log: Any = None
    characterization_dir = out_dir / "characterization"
    characterization_start_ns = 0
    characterization_sample = 0

    workload_cmd = command
    if args.taskset:
        workload_cmd = ["taskset", "-c", args.taskset, *workload_cmd]
    victim_cmd: list[str] | None = None
    if args.victim_command:
        victim_cmd = shlex.split(args.victim_command)
        if not victim_cmd:
            raise SystemExit("--victim-command parsed to an empty command")
        if args.victim_taskset:
            victim_cmd = ["taskset", "-c", args.victim_taskset, *victim_cmd]

    manifest: dict[str, Any] = {
        "out_dir": str(out_dir),
        "pin_dir": str(pin_dir),
        "attach_scheduler": bool(args.attach_scheduler),
        "sched_ext_state_before": sysfs_value("/sys/kernel/sched_ext/state"),
        "sched_ext_nr_rejected_before": sysfs_value("/sys/kernel/sched_ext/nr_rejected"),
        "frontier_dispatch_quota": args.frontier_dispatch_quota,
        "guard_cooldown_dispatches": args.guard_cooldown_dispatches,
        "pressure_fallback_enqueues": args.pressure_fallback_enqueues,
        "pressure_fallback_ms": args.pressure_fallback_ms,
        "shared_wait_pressure_us": args.shared_wait_pressure_us,
        "pressure_dispatch_ms": args.pressure_dispatch_ms,
        "pressure_local_cpu": bool(args.pressure_local_cpu),
        "locality_frontier_only": bool(args.locality_frontier_only),
        "locality_remembered_first": bool(args.locality_remembered_first),
        "frontier_dispatch_window_ms": args.frontier_dispatch_window_ms,
        "frontier_dispatch_window_budget": args.frontier_dispatch_window_budget,
        "shared_slice_us": args.shared_slice_us,
        "shared_wakeup_preempt": bool(args.shared_wakeup_preempt),
        "shared_wakeup_slice_us": args.shared_wakeup_slice_us,
        "shared_wakeup_min_block_us": args.shared_wakeup_min_block_us,
        "frontier_wakeup_preempt": bool(args.frontier_wakeup_preempt),
        "shared_local_dsq": bool(args.shared_local_dsq),
        "tracked_local_dsq": bool(args.tracked_local_dsq),
        "frontier_fallback_local_dsq": bool(getattr(args, "frontier_fallback_local_dsq", False)),
        "shared_percpu_dsq": bool(args.shared_percpu_dsq),
        "background_dsq": bool(args.background_dsq),
        "background_dispatch_quota": args.background_dispatch_quota,
        "flowmon_enabled": not args.no_flowmon,
        "event_flowmon_enabled": not args.no_flowmon and not args.no_event_flowmon,
        "root_flowd_enabled": bool(args.root_flowd),
        "controller_enabled": not args.no_controller,
        "hint_dry_run": bool(args.hint_dry_run),
        "workload_cmd": workload_cmd,
        "victim_cmd": victim_cmd,
        "victim_warmup_sec": args.victim_warmup_sec,
        "collect_characterization": bool(args.collect_characterization),
        "characterization": {
            "interval_ms": args.characterization_interval_ms,
            "frontier_width": args.frontier_width,
            "adaptive_frontier_width": bool(args.adaptive_frontier_width),
            "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
            "top_candidates": args.characterization_top_candidates,
            "min_frontier_score": args.min_frontier_score,
        },
        "controller": {
            "enabled": not args.no_controller,
            "stage_kind": args.stage_kind,
            "stage_state_file": str(stage_state_file),
            "frontier_width": args.frontier_width,
            "adaptive_frontier_width": bool(args.adaptive_frontier_width),
            "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
            "min_frontier_score": args.min_frontier_score,
            "frontier_propagation": bool(args.frontier_propagation),
            "frontier_propagation_depth": args.frontier_propagation_depth,
            "adaptive_frontier_propagation_depth": bool(args.adaptive_frontier_propagation_depth),
            "stage_aware_frontier_propagation_depth": bool(args.stage_aware_frontier_propagation_depth),
            "frontier_stability_samples": args.frontier_stability_samples,
            "frontier_hold_ms": args.frontier_hold_ms,
            "frontier_bypass_score": args.frontier_bypass_score,
            "frontier_selected_stability": bool(args.frontier_selected_stability),
            "frontier_stability_action": args.frontier_stability_action,
            "frontier_action": args.frontier_action,
            "build_frontier_admission": args.build_frontier_admission,
            "policy_fallback": bool(args.policy_fallback),
            "width_control": bool(args.width_control),
            "max_frontier_width": args.max_frontier_width,
            "confidence": args.confidence,
            "ttl_ms": args.ttl_ms,
            "policy_window_ms": args.policy_window_ms,
            "boost_budget_ms": args.boost_budget_ms,
            "max_vtime_boost_us": args.max_vtime_boost_us,
            "interval_ms": args.interval_ms,
            "anchor_cpu": args.anchor_cpu,
            "frontier_dispatch_window_ms": args.frontier_dispatch_window_ms,
            "frontier_dispatch_window_budget": args.frontier_dispatch_window_budget,
            "shared_slice_us": args.shared_slice_us,
            "shared_wakeup_preempt": bool(args.shared_wakeup_preempt),
            "shared_wakeup_slice_us": args.shared_wakeup_slice_us,
            "shared_wakeup_min_block_us": args.shared_wakeup_min_block_us,
            "frontier_wakeup_preempt": bool(args.frontier_wakeup_preempt),
            "shared_local_dsq": bool(args.shared_local_dsq),
            "tracked_local_dsq": bool(args.tracked_local_dsq),
            "frontier_fallback_local_dsq": bool(getattr(args, "frontier_fallback_local_dsq", False)),
            "shared_percpu_dsq": bool(args.shared_percpu_dsq),
            "background_dsq": bool(args.background_dsq),
            "background_dispatch_quota": args.background_dispatch_quota,
            "pressure_fallback_ms": args.pressure_fallback_ms,
            "shared_wait_pressure_us": args.shared_wait_pressure_us,
            "pressure_dispatch_ms": args.pressure_dispatch_ms,
            "pressure_local_cpu": bool(args.pressure_local_cpu),
            "locality_frontier_only": bool(args.locality_frontier_only),
            "locality_remembered_first": bool(args.locality_remembered_first),
        },
    }
    if oracle_frontier_label_file is not None:
        manifest["oracle_frontier_label_file"] = str(oracle_frontier_label_file)
        manifest["oracle_frontier_driver"] = str(pathlib.Path(args.oracle_frontier_driver).resolve())

    exit_code = 0
    try:
        if not args.no_build_tools:
            build_tools()

        if args.attach_scheduler or (not args.hint_dry_run) or oracle_frontier_label_file is not None:
            ensure_sudo_available()

        scheduler = pathlib.Path(args.scheduler)
        flowmon = pathlib.Path(args.flowmon)
        flowctl = pathlib.Path(args.flowctl)
        controller_needed = not args.no_controller
        oracle_needed = oracle_frontier_label_file is not None
        if args.attach_scheduler and not scheduler.exists():
            raise FileNotFoundError(f"scheduler binary not found: {scheduler}")
        if not args.no_flowmon and not flowmon.exists() and (args.attach_scheduler or not args.hint_dry_run):
            raise FileNotFoundError(f"flowmon binary not found: {flowmon}")
        root_flowd = pathlib.Path(args.root_flowd_bin)
        if args.root_flowd and not root_flowd.exists():
            raise FileNotFoundError(f"root flowd binary not found: {root_flowd}")
        if (controller_needed or oracle_needed) and not flowctl.exists():
            raise FileNotFoundError(f"flowctl not found: {flowctl}")

        if args.attach_scheduler:
            scheduler_cmd = sudo_prefix() + [
                str(scheduler),
                "--pin-dir",
                str(pin_dir),
                "--frontier-dispatch-quota",
                str(max(0, args.frontier_dispatch_quota)),
                "--guard-cooldown-dispatches",
                str(max(0, args.guard_cooldown_dispatches)),
                "--pressure-fallback-enqueues",
                str(max(0, args.pressure_fallback_enqueues)),
                "--pressure-fallback-ms",
                str(max(0, args.pressure_fallback_ms)),
                "--shared-wait-pressure-us",
                str(max(0, args.shared_wait_pressure_us)),
                "--pressure-dispatch-ms",
                str(max(0, args.pressure_dispatch_ms)),
                "--frontier-dispatch-window-ms",
                str(max(0, args.frontier_dispatch_window_ms)),
                "--frontier-dispatch-window-budget",
                str(max(0, args.frontier_dispatch_window_budget)),
                "--shared-slice-us",
                str(max(0, args.shared_slice_us)),
                "--shared-wakeup-slice-us",
                str(max(0, args.shared_wakeup_slice_us)),
                "--shared-wakeup-min-block-us",
                str(max(0, args.shared_wakeup_min_block_us)),
            ]
            if args.pressure_local_cpu:
                scheduler_cmd.append("--pressure-local-cpu")
            if args.locality_frontier_only:
                scheduler_cmd.append("--locality-frontier-only")
            if args.locality_remembered_first:
                scheduler_cmd.append("--locality-remembered-first")
            if args.shared_wakeup_preempt:
                scheduler_cmd.append("--shared-wakeup-preempt")
            if args.frontier_wakeup_preempt:
                scheduler_cmd.append("--frontier-wakeup-preempt")
            if args.shared_local_dsq:
                scheduler_cmd.append("--shared-local-dsq")
            if args.tracked_local_dsq:
                scheduler_cmd.append("--tracked-local-dsq")
            if getattr(args, "frontier_fallback_local_dsq", False):
                scheduler_cmd.append("--frontier-fallback-local-dsq")
            if args.shared_percpu_dsq:
                scheduler_cmd.append("--shared-percpu-dsq")
            if args.background_dsq:
                scheduler_cmd.append("--background-dsq")
            if args.background_dispatch_quota:
                scheduler_cmd += [
                    "--background-dispatch-quota",
                    str(max(0, args.background_dispatch_quota)),
                ]
            manifest["scheduler_cmd"] = scheduler_cmd
            scheduler_proc = start_logged(scheduler_cmd, out_dir / "scheduler.log", start_new_session=True)
            wait_for_maps(pin_dir, args.startup_timeout_sec, scheduler_proc)
        elif not args.hint_dry_run:
            wait_for_maps(pin_dir, args.startup_timeout_sec, None)

        if (
            not args.no_flowmon
            and not args.no_event_flowmon
            and (args.attach_scheduler or not args.hint_dry_run)
        ):
            flowmon_cmd = sudo_prefix() + [
                str(flowmon),
                "--pin-dir",
                str(pin_dir),
                "--events",
                "--interval",
                "1",
            ]
            manifest["flowmon_cmd"] = flowmon_cmd
            flowmon_proc = start_logged(flowmon_cmd, out_dir / "flowmon.log", start_new_session=True)

        if victim_cmd:
            victim_proc = start_logged(victim_cmd, out_dir / "victim.log", start_new_session=True)
            if args.victim_warmup_sec > 0:
                time.sleep(args.victim_warmup_sec)
            if victim_proc.poll() is not None and victim_proc.returncode != 0:
                raise RuntimeError(f"victim exited early with code {victim_proc.returncode}")

        workload_proc = start_logged(
            workload_cmd,
            out_dir / "workload.log",
            start_new_session=True,
            env=workload_env,
        )
        manifest["root_pid"] = workload_proc.pid
        session = args.session or f"pid:{workload_proc.pid}"

        if args.root_flowd:
            root_flowd_cmd = sudo_prefix() + [
                str(root_flowd),
                "--root-pid",
                str(workload_proc.pid),
                "--session",
                args.root_flowd_session,
                "--pin-dir",
                str(pin_dir),
                "--confidence",
                str(args.confidence),
                "--max-frontier-width",
                str(args.max_frontier_width),
                "--max-vtime-boost-us",
                str(args.max_vtime_boost_us),
                "--policy-window-ms",
                str(args.policy_window_ms),
                "--boost-budget-ms",
                str(args.boost_budget_ms),
                "--ttl-ms",
                str(args.ttl_ms),
                "--frontier",
                "--once",
            ]
            if args.width_control:
                root_flowd_cmd.append("--width-control")
            if args.root_flowd_anchor_cpu >= 0:
                root_flowd_cmd.extend(["--anchor-cpu", str(args.root_flowd_anchor_cpu)])
            if args.root_flowd_propagation_depth > 0:
                root_flowd_cmd.extend(["--propagation-depth", str(args.root_flowd_propagation_depth)])
            manifest["root_flowd_cmd"] = root_flowd_cmd
            with (out_dir / "root-flowd.log").open("w", encoding="utf-8") as log:
                root_flowd_result = subprocess.run(
                    root_flowd_cmd,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
            manifest["root_flowd_returncode"] = root_flowd_result.returncode
            if root_flowd_result.returncode != 0:
                raise RuntimeError(f"root flowd failed with code {root_flowd_result.returncode}")
        else:
            manifest["root_flowd_cmd"] = []
            manifest["root_flowd_returncode"] = None

        if oracle_frontier_label_file is not None:
            oracle_driver = pathlib.Path(args.oracle_frontier_driver).resolve()
            if not oracle_driver.exists():
                raise FileNotFoundError(f"oracle frontier driver not found: {oracle_driver}")
            oracle_cmd = sudo_prefix() + [
                sys.executable,
                str(oracle_driver),
                "--label-file",
                str(oracle_frontier_label_file),
                "--root-pid",
                str(workload_proc.pid),
                "--session",
                session,
                "--flowctl",
                str(flowctl),
                "--pin-dir",
                str(pin_dir),
                "--task-map",
                str(task_map),
                "--policy-map",
                str(policy_map),
                "--confidence",
                str(args.confidence),
                "--anchor-cpu",
                str(args.anchor_cpu),
                "--ttl-ms",
                str(args.ttl_ms),
                "--policy-window-ms",
                str(args.policy_window_ms),
                "--boost-budget-ms",
                str(args.boost_budget_ms),
                "--max-vtime-boost-us",
                str(args.max_vtime_boost_us),
                "--max-frontier-width",
                str(args.max_frontier_width),
                "--frontier-action",
                args.frontier_action,
                "--poll-interval-ms",
                str(args.interval_ms),
            ]
            if args.width_control:
                oracle_cmd.append("--width-control")
            manifest["oracle_frontier_driver_cmd"] = oracle_cmd
            oracle_proc = start_logged(oracle_cmd, out_dir / "oracle-frontier.log", start_new_session=True)

        controller_json = out_dir / "controller.ndjson"
        controller_json.write_text("", encoding="utf-8")
        if args.no_controller:
            manifest["controller_cmd"] = []
            manifest["controller_returncode"] = None
            (out_dir / "controller.log").write_text("controller disabled by --no-controller\n", encoding="utf-8")
        else:
            controller_cmd = [
                sys.executable,
                "-u",
                str(flowctl),
                "--root-pid",
                str(workload_proc.pid),
                "--session",
                session,
                "--watch",
                "--stage-kind",
                args.stage_kind,
                "--stage-state-file",
                str(stage_state_file),
                "--frontier-width",
                str(args.frontier_width),
                *(["--adaptive-frontier-width", "--adaptive-max-frontier-width", str(args.adaptive_max_frontier_width)] if args.adaptive_frontier_width else []),
                "--min-frontier-score",
                str(args.min_frontier_score),
                *(["--frontier-propagation", "--frontier-propagation-depth", str(args.frontier_propagation_depth)] if args.frontier_propagation else []),
                *(["--adaptive-frontier-propagation-depth"] if args.adaptive_frontier_propagation_depth else []),
                *(["--stage-aware-frontier-propagation-depth"] if args.stage_aware_frontier_propagation_depth else []),
                "--frontier-stability-samples",
                str(args.frontier_stability_samples),
                "--frontier-hold-ms",
                str(args.frontier_hold_ms),
                "--frontier-bypass-score",
                str(args.frontier_bypass_score),
                *(["--frontier-selected-stability"] if args.frontier_selected_stability else []),
                "--frontier-stability-action",
                args.frontier_stability_action,
                "--frontier-action",
                args.frontier_action,
                "--build-frontier-admission",
                args.build_frontier_admission,
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
                "--interval-ms",
                str(args.interval_ms),
                "--json-log",
                str(controller_json),
            ]
            if args.anchor_cpu >= 0:
                controller_cmd.extend(["--anchor-cpu", str(args.anchor_cpu)])
            if args.width_control:
                controller_cmd.extend(["--width-control", "--max-frontier-width", str(args.max_frontier_width)])
            if args.policy_fallback:
                controller_cmd.append("--policy-fallback")
            if not args.background_dsq:
                controller_cmd.append("--no-background-hints")
            if args.hint_dry_run:
                controller_cmd.append("--dry-run")
            elif sudo_prefix():
                controller_cmd = sudo_prefix() + controller_cmd

            manifest["controller_cmd"] = controller_cmd
            controller_proc = start_logged(controller_cmd, out_dir / "controller.log", start_new_session=True)
        if args.collect_characterization:
            characterization_dir.mkdir(parents=True, exist_ok=True)
            characterization_log = (characterization_dir / "characterization.ndjson").open(
                "w",
                encoding="utf-8",
            )
            characterization_start_ns = time.time_ns()
            manifest["characterization_dir"] = str(characterization_dir)

        forced_stage = characterize_flowctl.parse_optional_stage_kind(args.stage_kind)
        characterization_interval_sec = max(1, args.characterization_interval_ms) / 1000.0
        next_characterization_at = time.monotonic()
        while workload_proc.poll() is None:
            now = time.monotonic()
            if (
                args.collect_characterization
                and characterization_log is not None
                and now >= next_characterization_at
            ):
                try:
                    record, characterization_previous = characterize_sample_tree(
                        workload_proc.pid,
                        forced_stage=forced_stage,
                        stage_state_file=stage_state_file,
                        previous=characterization_previous,
                        sample_index=characterization_sample,
                        start_ns=characterization_start_ns,
                        width=args.frontier_width,
                        adaptive_width=args.adaptive_frontier_width,
                        adaptive_max_width=args.adaptive_max_frontier_width,
                        min_score=args.min_frontier_score,
                        top_candidates=args.characterization_top_candidates,
                    )
                except Exception as exc:  # keep the live workload cleanup reliable
                    record = {
                        "type": "characterization_error",
                        "sample": characterization_sample,
                        "time_ns": time.time_ns(),
                        "error": repr(exc),
                    }
                characterization_log.write(json.dumps(record, sort_keys=True) + "\n")
                characterization_log.flush()
                if record.get("type") == "characterization_sample":
                    characterization_records.append(record)
                characterization_sample += 1
                next_characterization_at = now + characterization_interval_sec
            sleep_for = 0.05
            if args.collect_characterization:
                sleep_for = min(sleep_for, max(0.001, next_characterization_at - time.monotonic()))
            time.sleep(sleep_for)

        workload_rc = workload_proc.returncode
        manifest["workload_returncode"] = workload_rc

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

        controller_rc = stop_process_group(controller_proc, "flowctl")
        controller_proc = None
        manifest["controller_returncode"] = controller_rc

        if flowmon_proc is not None:
            manifest["flowmon_returncode"] = stop_process_group(flowmon_proc, "flowmon")
            flowmon_proc = None

        if oracle_proc is not None:
            manifest["oracle_frontier_driver_returncode"] = wait_then_stop_process_group(
                oracle_proc,
                "oracle-frontier",
            )
            oracle_proc = None

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

        if workload_rc != 0:
            raise RuntimeError(f"workload failed with code {workload_rc}")
    except TerminationSignal as exc:
        manifest["terminated_by_signal"] = exc.signum
        exit_code = 128 + exc.signum
    finally:
        if characterization_log is not None:
            characterization_log.close()
            characterization_manifest = {
                "command": workload_cmd,
                "out_dir": str(characterization_dir),
                "live_harness_out_dir": str(out_dir),
                "interval_ms": args.characterization_interval_ms,
                "frontier_width": args.frontier_width,
                "adaptive_frontier_width": bool(args.adaptive_frontier_width),
                "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
                "top_candidates": args.characterization_top_candidates,
                "min_frontier_score": args.min_frontier_score,
                "stage_kind": args.stage_kind,
                "stage_state_file": str(stage_state_file),
                "taskset": args.taskset,
                "root_pid": manifest.get("root_pid"),
                "workload_returncode": manifest.get("workload_returncode"),
                "sched_ext_state_before": manifest.get("sched_ext_state_before"),
                "sched_ext_state_after": sysfs_value("/sys/kernel/sched_ext/state"),
            }
            (characterization_dir / "manifest.json").write_text(
                json.dumps(characterization_manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            characterization_summary = write_characterization_summary(
                characterization_dir,
                manifest=characterization_manifest,
                records=characterization_records,
            )
            manifest["characterization_summary"] = characterization_summary
        if "controller_returncode" not in manifest:
            manifest["controller_returncode"] = stop_process_group(controller_proc, "flowctl")
        if "victim_returncode" not in manifest:
            manifest["victim_returncode"] = stop_process_group(victim_proc, "victim")
        if "flowmon_returncode" not in manifest:
            manifest["flowmon_returncode"] = stop_process_group(flowmon_proc, "flowmon")
        if "oracle_frontier_driver_returncode" not in manifest:
            manifest["oracle_frontier_driver_returncode"] = wait_then_stop_process_group(
                oracle_proc,
                "oracle-frontier",
            )
        if "scheduler_returncode" not in manifest:
            manifest["scheduler_returncode"] = stop_process_group(scheduler_proc, "scheduler")
        if workload_proc is not None and workload_proc.poll() is None:
            manifest["workload_returncode"] = stop_process_group(workload_proc, "workload")
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

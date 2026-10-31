#!/usr/bin/env python3
"""Run a reproducible build/test sandbox matrix for FlowCausal.

This matrix is a pilot evaluation bridge between synthetic flowbench and large
real repositories.  It compares a taskset-only default run against FlowCausal
live non-oracle variants and summarizes:

  * stage durations from buildtest_sandbox.py JSON markers;
  * online frontier inference from controller.ndjson;
  * BPF admission and width-control counters from flowmon-final.log.

The script deliberately does not compute or print speedup claims.  A small
single-trial run is useful for mechanism connectivity; paper-grade performance
claims require repeated trials, larger repositories, and baseline controls.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import time
from typing import Any

from audit_claim_gate import audit_matrix as audit_claim_gate_matrix


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
LIVE_SANDBOX = ROOT / "experiments" / "run_live_sandbox.py"
CHARACTERIZE_SANDBOX = ROOT / "experiments" / "run_characterize_sandbox.py"
WORKLOAD = ROOT / "experiments" / "workloads" / "buildtest_sandbox.py"


def run_checked(cmd: list[str], cwd: pathlib.Path | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def start_logged(cmd: list[str], log_path: pathlib.Path, *, start_new_session: bool = False) -> subprocess.Popen[str]:
    print("+ " + " ".join(cmd), flush=True)
    log = log_path.open("w", encoding="utf-8")
    return subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=start_new_session,
    )


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


def build_tools() -> None:
    run_checked(["make", "check"], cwd=SRC)


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


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def sysfs_value(path: str) -> str | None:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def parse_int_csv(value: str, *, name: str) -> list[int]:
    parsed: list[int] = []
    seen: set[int] = set()
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        try:
            number = int(item, 0)
        except ValueError as exc:
            raise SystemExit(f"{name} contains a non-integer value: {item!r}") from exc
        number = max(0, number)
        if number not in seen:
            parsed.append(number)
            seen.add(number)
    if not parsed:
        raise SystemExit(f"{name} must contain at least one integer")
    return parsed


def workload_args(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(WORKLOAD),
        "--files",
        str(args.files),
        "--statements",
        str(args.statements),
        "--jobs",
        str(args.jobs),
        "--runtime-spin",
        str(args.runtime_spin),
        "--test-workers",
        str(args.test_workers),
        "--test-iters",
        str(args.test_iters),
        "--pre-sleep-ms",
        str(args.pre_sleep_ms),
        "--post-sleep-ms",
        str(args.post_sleep_ms),
    ]
    if args.package:
        cmd.append("--package")
    return cmd


def victim_args(args: argparse.Namespace) -> list[str] | None:
    if not args.victim_command:
        return None
    cmd = shlex.split(args.victim_command)
    if not cmd:
        raise SystemExit("--victim-command parsed to an empty command")
    if args.victim_taskset:
        cmd = ["taskset", "-c", args.victim_taskset, *cmd]
    return cmd


def parse_json_lines(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in read_text(path).splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def parse_workload_log(path: pathlib.Path) -> dict[str, Any]:
    records = parse_json_lines(path)
    stage_seconds: dict[str, list[float]] = collections.defaultdict(list)
    workload_end: dict[str, Any] = {}
    for record in records:
        if record.get("type") == "stage_end":
            stage = str(record.get("stage", "UNKNOWN"))
            stage_seconds[stage].append(float(record.get("elapsed_sec", 0.0)))
        elif record.get("type") == "workload_end":
            workload_end = record
    timings = workload_end.get("timings") if isinstance(workload_end.get("timings"), dict) else {}
    for stage, value in timings.items():
        stage_name = str(stage).upper()
        if not stage_seconds.get(stage_name):
            stage_seconds[stage_name].append(float(value))
    total_sec = sum(values[0] for values in stage_seconds.values() if values)
    return {
        "stage_seconds": {stage: values for stage, values in sorted(stage_seconds.items())},
        "total_sec": total_sec,
        "records": len(records),
    }


def parse_victim_log(path: pathlib.Path) -> dict[str, Any]:
    summaries = [record for record in parse_json_lines(path) if record.get("type") == "victim_summary"]
    if not summaries:
        return {}
    summary = summaries[-1]
    lateness = summary.get("lateness") if isinstance(summary.get("lateness"), dict) else {}
    runtime = summary.get("runtime") if isinstance(summary.get("runtime"), dict) else {}
    return {
        "workers": int(summary.get("workers", 0)),
        "iterations": int(summary.get("iterations", 0)),
        "returncode": int(summary.get("returncode", 0)),
        "lateness_mean_ms": float(lateness.get("mean_ms", 0.0)),
        "lateness_p95_ms": float(lateness.get("p95_ms", 0.0)),
        "lateness_p99_ms": float(lateness.get("p99_ms", 0.0)),
        "lateness_max_ms": float(lateness.get("max_ms", 0.0)),
        "runtime_mean_ms": float(runtime.get("mean_ms", 0.0)),
        "runtime_p95_ms": float(runtime.get("p95_ms", 0.0)),
        "runtime_p99_ms": float(runtime.get("p99_ms", 0.0)),
        "runtime_max_ms": float(runtime.get("max_ms", 0.0)),
    }


def parse_controller(path: pathlib.Path) -> dict[str, Any]:
    records = parse_json_lines(path)
    stage_counts: collections.Counter[str] = collections.Counter()
    stage_source_counts: collections.Counter[str] = collections.Counter()
    comm_counts: collections.Counter[str] = collections.Counter()
    reason_counts: collections.Counter[str] = collections.Counter()
    adaptive_width_counts: collections.Counter[str] = collections.Counter()
    adaptive_reason_counts: collections.Counter[str] = collections.Counter()
    adaptive_propagation_depth_counts: collections.Counter[str] = collections.Counter()
    adaptive_propagation_reason_counts: collections.Counter[str] = collections.Counter()
    adaptive_propagation_policy_counts: collections.Counter[str] = collections.Counter()
    adaptive_propagation_stage_counts: collections.Counter[str] = collections.Counter()
    stability_mode_counts: collections.Counter[str] = collections.Counter()
    stability_action_counts: collections.Counter[str] = collections.Counter()
    frontier_action_counts: collections.Counter[str] = collections.Counter()
    frontier_action_reason_counts: collections.Counter[str] = collections.Counter()
    build_admission_mode_counts: collections.Counter[str] = collections.Counter()
    build_admission_reason_counts: collections.Counter[str] = collections.Counter()
    frontier_records = 0
    selected_records = 0
    propagation_records = 0
    propagation_anchors = 0
    stability_records = 0
    stability_selected = 0
    stability_held = 0
    stability_suppressed = 0
    stability_observed_unstable = 0
    adaptive_records = 0
    adaptive_enabled_records = 0
    adaptive_widened_records = 0
    adaptive_max_effective_width = 0
    adaptive_propagation_records = 0
    adaptive_propagation_enabled_records = 0
    frontier_action_records = 0
    frontier_action_observe_records = 0
    frontier_action_policy_fallback_records = 0
    build_admission_triggered_records = 0
    examples: list[dict[str, Any]] = []
    adaptive_examples: list[dict[str, Any]] = []
    adaptive_propagation_examples: list[dict[str, Any]] = []
    frontier_action_examples: list[dict[str, Any]] = []
    reason_tags = [
        "parent-wait",
        "barrier-tail",
        "compiler worker",
        "test worker",
        "test driver",
        "build driver",
        "link/archive tail",
    ]

    for record in records:
        stage_counts[str(record.get("stage", "UNKNOWN"))] += 1
        stage_source_counts[str(record.get("stage_source", "unknown"))] += 1
        adaptive_width = record.get("adaptive_width")
        if isinstance(adaptive_width, dict):
            adaptive_records += 1
            enabled = bool(adaptive_width.get("enabled", False))
            if enabled:
                adaptive_enabled_records += 1
            try:
                effective_width = int(adaptive_width.get("frontier_width", 0) or 0)
            except (TypeError, ValueError):
                effective_width = 0
            try:
                requested_width = int(adaptive_width.get("requested_frontier_width", 0) or 0)
            except (TypeError, ValueError):
                requested_width = 0
            if effective_width > 0:
                adaptive_width_counts[str(effective_width)] += 1
                adaptive_max_effective_width = max(adaptive_max_effective_width, effective_width)
            if enabled and effective_width > requested_width:
                adaptive_widened_records += 1
            adaptive_reason = str(adaptive_width.get("reason", "unknown"))
            adaptive_reason_counts[adaptive_reason] += 1
            if len(adaptive_examples) < 8:
                adaptive_examples.append(
                    {
                        "tick": record.get("tick"),
                        "stage": record.get("stage"),
                        "enabled": enabled,
                        "frontier_width": effective_width,
                        "requested_frontier_width": requested_width,
                        "max_frontier_width": adaptive_width.get("max_frontier_width"),
                        "reason": adaptive_reason,
                        "serviceable_leaves": adaptive_width.get("serviceable_leaves"),
                        "eligible_candidates": adaptive_width.get("eligible_candidates"),
                        "parent_wait_candidates": adaptive_width.get("parent_wait_candidates"),
                        "barrier_tail_width": adaptive_width.get("barrier_tail_width"),
                    }
                )
        adaptive_propagation = record.get("adaptive_propagation")
        if isinstance(adaptive_propagation, dict):
            adaptive_propagation_records += 1
            prop_enabled = bool(adaptive_propagation.get("enabled", False))
            if prop_enabled:
                adaptive_propagation_enabled_records += 1
            try:
                prop_depth = int(adaptive_propagation.get("propagation_depth", 0) or 0)
            except (TypeError, ValueError):
                prop_depth = 0
            adaptive_propagation_depth_counts[str(prop_depth)] += 1
            prop_reason = str(adaptive_propagation.get("reason", "unknown"))
            adaptive_propagation_reason_counts[prop_reason] += 1
            adaptive_propagation_policy_counts[str(adaptive_propagation.get("policy", "unknown"))] += 1
            adaptive_propagation_stage_counts[str(adaptive_propagation.get("stage", record.get("stage", "UNKNOWN")))] += 1
            if len(adaptive_propagation_examples) < 8:
                adaptive_propagation_examples.append(
                    {
                        "tick": record.get("tick"),
                        "stage": record.get("stage"),
                        "enabled": prop_enabled,
                        "requested_depth": adaptive_propagation.get("requested_depth"),
                        "propagation_depth": prop_depth,
                        "frontier_count": adaptive_propagation.get("frontier_count"),
                        "anchor_count": adaptive_propagation.get("anchor_count"),
                        "policy": adaptive_propagation.get("policy"),
                        "eligible_candidates": adaptive_propagation.get("eligible_candidate_count"),
                        "parent_wait_count": adaptive_propagation.get("selected_parent_wait_count"),
                        "max_barrier_tail": adaptive_propagation.get("selected_max_barrier_tail"),
                        "reason": prop_reason,
                    }
                )
        anchors = record.get("propagation_anchors")
        if isinstance(anchors, list):
            propagation_records += 1
            propagation_anchors += len(anchors)
        stability = record.get("stability")
        if isinstance(stability, dict):
            stability_records += 1
            stability_mode_counts[str(stability.get("mode", "candidate"))] += 1
            stability_action_counts[str(stability.get("action", "suppress"))] += 1
            try:
                stability_suppressed += int(stability.get("suppressed_count", 0))
            except (TypeError, ValueError):
                pass
            selected = stability.get("selected")
            if isinstance(selected, list):
                stability_selected += len(selected)
            held = stability.get("held")
            if isinstance(held, list):
                stability_held += len(held)
            observed = stability.get("observed_unstable")
            if isinstance(observed, list):
                stability_observed_unstable += len(observed)
        frontier_action = record.get("frontier_action")
        if isinstance(frontier_action, dict):
            frontier_action_records += 1
            action = str(frontier_action.get("action", "unknown"))
            frontier_action_counts[action] += 1
            if action == "observe":
                frontier_action_observe_records += 1
            if bool(frontier_action.get("policy_fallback", False)):
                frontier_action_policy_fallback_records += 1
            action_reason = str(frontier_action.get("reason", "unknown"))
            frontier_action_reason_counts[action_reason] += 1
            build_mode = str(frontier_action.get("build_frontier_admission", "advance"))
            build_admission_mode_counts[build_mode] += 1
            if bool(frontier_action.get("build_admission_triggered", False)):
                build_admission_triggered_records += 1
                build_reason = str(frontier_action.get("build_admission_reason", "unknown"))
                build_admission_reason_counts[build_reason] += 1
            if len(frontier_action_examples) < 8 and (
                bool(frontier_action.get("build_admission_triggered", False))
                or action == "observe"
                or build_mode != "advance"
            ):
                frontier_action_examples.append(
                    {
                        "tick": record.get("tick"),
                        "stage": record.get("stage"),
                        "action": action,
                        "policy_fallback": bool(frontier_action.get("policy_fallback", False)),
                        "reason": action_reason,
                        "build_frontier_admission": build_mode,
                        "build_admission_triggered": bool(
                            frontier_action.get("build_admission_triggered", False)
                        ),
                        "build_admission_reason": frontier_action.get("build_admission_reason"),
                        "build_selected_max_barrier_tail": frontier_action.get(
                            "build_selected_max_barrier_tail"
                        ),
                    }
                )
        frontiers = record.get("frontiers")
        if not isinstance(frontiers, list) or not frontiers:
            continue
        selected_records += 1
        for frontier in frontiers:
            if not isinstance(frontier, dict):
                continue
            frontier_records += 1
            comm_counts[str(frontier.get("comm", ""))] += 1
            reason = str(frontier.get("reason", ""))
            for tag in reason_tags:
                if tag in reason:
                    reason_counts[tag] += 1
            if len(examples) < 8:
                examples.append(
                    {
                        "tick": record.get("tick"),
                        "stage": record.get("stage"),
                        "pid": frontier.get("pid"),
                        "comm": frontier.get("comm"),
                        "score": frontier.get("score"),
                        "reason": reason,
                    }
                )

    return {
        "records": len(records),
        "stage_counts": dict(stage_counts),
        "stage_source_counts": dict(stage_source_counts),
        "selected_samples": selected_records,
        "frontier_records": frontier_records,
        "propagation_records": propagation_records,
        "propagation_anchors": propagation_anchors,
        "stability_records": stability_records,
        "stability_selected": stability_selected,
        "stability_held": stability_held,
        "stability_suppressed": stability_suppressed,
        "stability_observed_unstable": stability_observed_unstable,
        "stability_mode_counts": dict(stability_mode_counts),
        "stability_action_counts": dict(stability_action_counts),
        "adaptive_records": adaptive_records,
        "adaptive_enabled_records": adaptive_enabled_records,
        "adaptive_widened_records": adaptive_widened_records,
        "adaptive_max_effective_width": adaptive_max_effective_width,
        "adaptive_width_counts": dict(adaptive_width_counts),
        "adaptive_reason_counts": dict(adaptive_reason_counts),
        "adaptive_propagation_records": adaptive_propagation_records,
        "adaptive_propagation_enabled_records": adaptive_propagation_enabled_records,
        "frontier_action_records": frontier_action_records,
        "frontier_action_observe_records": frontier_action_observe_records,
        "frontier_action_policy_fallback_records": frontier_action_policy_fallback_records,
        "build_admission_triggered_records": build_admission_triggered_records,
        "frontier_action_counts": dict(frontier_action_counts),
        "frontier_action_reason_counts": dict(frontier_action_reason_counts),
        "build_admission_mode_counts": dict(build_admission_mode_counts),
        "build_admission_reason_counts": dict(build_admission_reason_counts),
        "adaptive_propagation_depth_counts": dict(adaptive_propagation_depth_counts),
        "adaptive_propagation_reason_counts": dict(adaptive_propagation_reason_counts),
        "adaptive_propagation_policy_counts": dict(adaptive_propagation_policy_counts),
        "adaptive_propagation_stage_counts": dict(adaptive_propagation_stage_counts),
        "frontier_comm_counts": dict(comm_counts),
        "reason_counts": dict(reason_counts),
        "examples": examples,
        "adaptive_examples": adaptive_examples,
        "adaptive_propagation_examples": adaptive_propagation_examples,
        "frontier_action_examples": frontier_action_examples,
    }


def parse_flowmon(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}

    stats = {
        "policy_sessions": 0,
        "budget_used_ns": 0,
        "budget_total_ns": 0,
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
        "global_shared_cooldown_entries": 0,
        "global_shared_cooldown_dispatches": 0,
        "global_shared_cooldown_misses": 0,
        "global_pressure_fallback_entries": 0,
        "global_pressure_fallback_enqueues": 0,
        "global_shared_wait_pressure_events": 0,
        "global_shared_wait_pressure_wait_ns": 0,
        "global_pressure_dispatch_entries": 0,
        "global_pressure_dispatches": 0,
        "global_pressure_dispatch_misses": 0,
        "global_shared_wakeup_preempt_entries": 0,
        "global_shared_wakeup_preempt_dispatches": 0,
        "global_shared_wakeup_preempt_misses": 0,
        "global_shared_local_enqueues": 0,
        "global_shared_percpu_enqueues": 0,
        "global_shared_percpu_dispatches": 0,
        "global_shared_percpu_misses": 0,
        "global_frontier_wakeup_preempt_entries": 0,
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
        "global_frontier_budget_windows": 0,
        "global_frontier_budget_trips": 0,
        "global_frontier_budget_shared": 0,
        "global_frontier_budget_misses": 0,
        "global_converge_dsq_enqueues": 0,
        "global_converge_dispatches": 0,
        "global_session_frontier_dsq_enqueues": 0,
        "global_session_frontier_dispatches": 0,
        "global_adaptive_width_tail_admissions": 0,
        "global_adaptive_width_wide_admissions": 0,
        "global_adaptive_width_held_enqueues": 0,
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
    policy_re = re.compile(r"session=(?P<session>\d+).* budget=(?P<used>\d+)/(?P<total>\d+)")
    global_re = re.compile(
        r"dispatch frontier=(?P<frontier>\d+) shared=(?P<shared>\d+) "
        r"shared_first=(?P<shared_first>\d+) guard_trips=(?P<trips>\d+) "
        r"guard_misses=(?P<misses>\d+)"
        r"(?: cooldown_entries=(?P<cooldown_entries>\d+) "
        r"cooldown_dispatches=(?P<cooldown_dispatches>\d+) "
        r"cooldown_misses=(?P<cooldown_misses>\d+))?"
        r"(?: pressure_fallback_entries=(?P<pressure_entries>\d+) "
        r"pressure_fallback_enqueues=(?P<pressure_enqueues>\d+))?"
        r"(?: shared_wait_pressure_events=(?P<shared_wait_events>\d+) "
        r"shared_wait_pressure_wait_ns=(?P<shared_wait_ns>\d+))?"
        r"(?: pressure_dispatch_entries=(?P<pressure_dispatch_entries>\d+) "
        r"pressure_dispatches=(?P<pressure_dispatches>\d+) "
        r"pressure_dispatch_misses=(?P<pressure_dispatch_misses>\d+))?"
        r"(?: shared_wakeup_preempt_entries=(?P<shared_wakeup_entries>\d+) "
        r"shared_wakeup_preempt_dispatches=(?P<shared_wakeup_dispatches>\d+) "
        r"shared_wakeup_preempt_misses=(?P<shared_wakeup_misses>\d+))?"
        r"(?: shared_local_enqueues=(?P<shared_local_enqueues>\d+))?"
        r"(?: shared_percpu_enqueues=(?P<shared_percpu_enqueues>\d+) "
        r"shared_percpu_dispatches=(?P<shared_percpu_dispatches>\d+) "
        r"shared_percpu_misses=(?P<shared_percpu_misses>\d+))?"
        r"(?: frontier_wakeup_preempt_entries=(?P<frontier_wakeup_entries>\d+))?"
        r"(?: background_enqueues=(?P<background_enqueues>\d+) "
        r"background_dispatches=(?P<background_dispatches>\d+))?"
        r"(?: frontier_budget_windows=(?P<budget_windows>\d+) "
        r"frontier_budget_trips=(?P<budget_trips>\d+) "
        r"frontier_budget_shared=(?P<budget_shared>\d+) "
        r"frontier_budget_misses=(?P<budget_misses>\d+))?"
    )
    global_key_map = {
        "frontier": "global_frontier_dispatches",
        "shared": "global_shared_dispatches",
        "shared_first": "global_shared_first_dispatches",
        "guard_trips": "global_frontier_guard_trips",
        "guard_misses": "global_frontier_guard_misses",
        "cooldown_entries": "global_shared_cooldown_entries",
        "cooldown_dispatches": "global_shared_cooldown_dispatches",
        "cooldown_misses": "global_shared_cooldown_misses",
        "pressure_fallback_entries": "global_pressure_fallback_entries",
        "pressure_fallback_enqueues": "global_pressure_fallback_enqueues",
        "shared_wait_pressure_events": "global_shared_wait_pressure_events",
        "shared_wait_pressure_wait_ns": "global_shared_wait_pressure_wait_ns",
        "pressure_dispatch_entries": "global_pressure_dispatch_entries",
        "pressure_dispatches": "global_pressure_dispatches",
        "pressure_dispatch_misses": "global_pressure_dispatch_misses",
        "shared_wakeup_preempt_entries": "global_shared_wakeup_preempt_entries",
        "shared_wakeup_preempt_dispatches": "global_shared_wakeup_preempt_dispatches",
        "shared_wakeup_preempt_misses": "global_shared_wakeup_preempt_misses",
        "shared_local_enqueues": "global_shared_local_enqueues",
        "shared_percpu_enqueues": "global_shared_percpu_enqueues",
        "shared_percpu_dispatches": "global_shared_percpu_dispatches",
        "shared_percpu_misses": "global_shared_percpu_misses",
        "frontier_wakeup_preempt_entries": "global_frontier_wakeup_preempt_entries",
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
        "frontier_budget_windows": "global_frontier_budget_windows",
        "frontier_budget_trips": "global_frontier_budget_trips",
        "frontier_budget_shared": "global_frontier_budget_shared",
        "frontier_budget_misses": "global_frontier_budget_misses",
        "converge_dsq_enqueues": "global_converge_dsq_enqueues",
        "converge_dispatches": "global_converge_dispatches",
        "session_frontier_dsq_enqueues": (
            "global_session_frontier_dsq_enqueues"
        ),
        "session_frontier_dispatches": "global_session_frontier_dispatches",
        "adaptive_width_tail_admissions": (
            "global_adaptive_width_tail_admissions"
        ),
        "adaptive_width_wide_admissions": (
            "global_adaptive_width_wide_admissions"
        ),
        "adaptive_width_held_enqueues": (
            "global_adaptive_width_held_enqueues"
        ),
    }

    pending_session = False
    for line in read_text(path).splitlines():
        policy_match = policy_re.search(line)
        if policy_match:
            stats["policy_sessions"] += 1
            stats["budget_used_ns"] += int(policy_match.group("used"))
            stats["budget_total_ns"] += int(policy_match.group("total"))
            continue
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
            continue
        global_match = global_re.search(line)
        if global_match:
            stats["global_frontier_dispatches"] += int(global_match.group("frontier"))
            stats["global_shared_dispatches"] += int(global_match.group("shared"))
            stats["global_shared_first_dispatches"] += int(global_match.group("shared_first"))
            stats["global_frontier_guard_trips"] += int(global_match.group("trips"))
            stats["global_frontier_guard_misses"] += int(global_match.group("misses"))
            if global_match.group("cooldown_entries") is not None:
                stats["global_shared_cooldown_entries"] += int(global_match.group("cooldown_entries"))
                stats["global_shared_cooldown_dispatches"] += int(global_match.group("cooldown_dispatches"))
                stats["global_shared_cooldown_misses"] += int(global_match.group("cooldown_misses"))
            if global_match.group("pressure_entries") is not None:
                stats["global_pressure_fallback_entries"] += int(global_match.group("pressure_entries"))
                stats["global_pressure_fallback_enqueues"] += int(global_match.group("pressure_enqueues"))
            if global_match.group("shared_wait_events") is not None:
                stats["global_shared_wait_pressure_events"] += int(global_match.group("shared_wait_events"))
                stats["global_shared_wait_pressure_wait_ns"] += int(global_match.group("shared_wait_ns"))
            if global_match.group("pressure_dispatch_entries") is not None:
                stats["global_pressure_dispatch_entries"] += int(global_match.group("pressure_dispatch_entries"))
                stats["global_pressure_dispatches"] += int(global_match.group("pressure_dispatches"))
                stats["global_pressure_dispatch_misses"] += int(global_match.group("pressure_dispatch_misses"))
            if global_match.group("shared_wakeup_entries") is not None:
                stats["global_shared_wakeup_preempt_entries"] += int(global_match.group("shared_wakeup_entries"))
                stats["global_shared_wakeup_preempt_dispatches"] += int(global_match.group("shared_wakeup_dispatches"))
                stats["global_shared_wakeup_preempt_misses"] += int(global_match.group("shared_wakeup_misses"))
            if global_match.group("shared_local_enqueues") is not None:
                stats["global_shared_local_enqueues"] += int(global_match.group("shared_local_enqueues"))
            if global_match.group("shared_percpu_enqueues") is not None:
                stats["global_shared_percpu_enqueues"] += int(global_match.group("shared_percpu_enqueues"))
                stats["global_shared_percpu_dispatches"] += int(global_match.group("shared_percpu_dispatches"))
                stats["global_shared_percpu_misses"] += int(global_match.group("shared_percpu_misses"))
            if global_match.group("frontier_wakeup_entries") is not None:
                stats["global_frontier_wakeup_preempt_entries"] += int(global_match.group("frontier_wakeup_entries"))
            if global_match.group("background_enqueues") is not None:
                stats["global_background_enqueues"] += int(global_match.group("background_enqueues"))
                stats["global_background_dispatches"] += int(global_match.group("background_dispatches"))
            if global_match.group("budget_windows") is not None:
                stats["global_frontier_budget_windows"] += int(global_match.group("budget_windows"))
                stats["global_frontier_budget_trips"] += int(global_match.group("budget_trips"))
                stats["global_frontier_budget_shared"] += int(global_match.group("budget_shared"))
                stats["global_frontier_budget_misses"] += int(global_match.group("budget_misses"))

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
    stats["budget_used_ratio"] = (
        stats["budget_used_ns"] / stats["budget_total_ns"]
        if stats["budget_total_ns"]
        else 0.0
    )
    return stats


def parse_characterization_summary(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(read_text(path) or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(raw, dict):
        return {}
    delta = raw.get("aggregate_delta") if isinstance(raw.get("aggregate_delta"), dict) else {}

    def number(key: str) -> float:
        try:
            return float(delta.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    def top_number(key: str) -> float:
        try:
            return float(raw.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    def optional_top_number(key: str) -> float:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    sample_count = top_number("sample_count")
    samples_with_frontier = top_number("samples_with_frontier")
    samples_with_schedstat = top_number("samples_with_schedstat")
    sampled_elapsed_sec = optional_top_number("sampled_elapsed_sec")
    if sampled_elapsed_sec <= 0.0:
        manifest = raw.get("manifest") if isinstance(raw.get("manifest"), dict) else {}
        try:
            sampled_elapsed_sec = float(manifest.get("elapsed_sec", 0.0))
        except (TypeError, ValueError):
            sampled_elapsed_sec = 0.0
    frontier_wait = number("frontier_sched_wait_delta_ns")
    frontier_runtime = number("frontier_sched_runtime_delta_ns")
    stage_delta_raw = raw.get("stage_delta_summary")
    stage_delta_summary: dict[str, dict[str, Any]] = {}
    if isinstance(stage_delta_raw, dict):
        for stage, value in stage_delta_raw.items():
            if not isinstance(value, dict):
                continue
            stage_delta = value.get("aggregate_delta") if isinstance(value.get("aggregate_delta"), dict) else {}

            def stage_number(key: str, *, _delta: dict[str, Any] = stage_delta) -> float:
                try:
                    return float(_delta.get(key, 0.0))
                except (TypeError, ValueError):
                    return 0.0

            def stage_top_number(key: str, *, _value: dict[str, Any] = value) -> float:
                try:
                    return float(_value.get(key, 0.0))
                except (TypeError, ValueError):
                    return 0.0

            stage_frontier_samples = stage_top_number("samples_with_frontier")
            stage_schedstat_samples = stage_top_number("samples_with_schedstat")
            stage_frontier_wait = stage_number("frontier_sched_wait_delta_ns")
            stage_frontier_runtime = stage_number("frontier_sched_runtime_delta_ns")
            stage_delta_summary[str(stage)] = {
                "sample_count": stage_top_number("sample_count"),
                "samples_with_frontier": stage_frontier_samples,
                "samples_with_parent_wait": stage_top_number("samples_with_parent_wait"),
                "samples_with_barrier_tail": stage_top_number("samples_with_barrier_tail"),
                "samples_with_schedstat": stage_schedstat_samples,
                "cpu_ticks_delta": stage_number("cpu_ticks_delta"),
                "frontier_cpu_ticks_delta": stage_number("frontier_cpu_ticks_delta"),
                "sched_runtime_delta_ns": stage_number("sched_runtime_delta_ns"),
                "frontier_sched_runtime_delta_ns": stage_frontier_runtime,
                "sched_wait_delta_ns": stage_number("sched_wait_delta_ns"),
                "frontier_sched_wait_delta_ns": stage_frontier_wait,
                "sched_slices_delta": stage_number("sched_slices_delta"),
                "frontier_sched_slices_delta": stage_number("frontier_sched_slices_delta"),
                "frontier_wait_share": (
                    float(value["frontier_wait_share"])
                    if isinstance(value.get("frontier_wait_share"), (int, float))
                    else 0.0
                ),
                "frontier_runtime_share": (
                    float(value["frontier_runtime_share"])
                    if isinstance(value.get("frontier_runtime_share"), (int, float))
                    else 0.0
                ),
                "frontier_wait_per_frontier_sample_ns": (
                    optional_value
                    if isinstance((optional_value := value.get("frontier_wait_per_frontier_sample_ns")), (int, float))
                    else (stage_frontier_wait / stage_frontier_samples if stage_frontier_samples > 0.0 else 0.0)
                ),
                "frontier_wait_per_schedstat_sample_ns": (
                    optional_value
                    if isinstance((optional_value := value.get("frontier_wait_per_schedstat_sample_ns")), (int, float))
                    else (stage_frontier_wait / stage_schedstat_samples if stage_schedstat_samples > 0.0 else 0.0)
                ),
                "frontier_runtime_per_frontier_sample_ns": (
                    optional_value
                    if isinstance((optional_value := value.get("frontier_runtime_per_frontier_sample_ns")), (int, float))
                    else (stage_frontier_runtime / stage_frontier_samples if stage_frontier_samples > 0.0 else 0.0)
                ),
            }

    return {
        "characterization_level": str(raw.get("characterization_level", "")),
        "sample_count": sample_count,
        "samples_with_frontier": samples_with_frontier,
        "samples_with_parent_wait": top_number("samples_with_parent_wait"),
        "samples_with_barrier_tail": top_number("samples_with_barrier_tail"),
        "samples_with_schedstat": samples_with_schedstat,
        "max_process_count": top_number("max_process_count"),
        "max_leaf_count": top_number("max_leaf_count"),
        "cpu_ticks_delta": number("cpu_ticks_delta"),
        "frontier_cpu_ticks_delta": number("frontier_cpu_ticks_delta"),
        "sched_runtime_delta_ns": number("sched_runtime_delta_ns"),
        "frontier_sched_runtime_delta_ns": frontier_runtime,
        "sched_wait_delta_ns": number("sched_wait_delta_ns"),
        "frontier_sched_wait_delta_ns": frontier_wait,
        "sched_slices_delta": number("sched_slices_delta"),
        "frontier_sched_slices_delta": number("frontier_sched_slices_delta"),
        "frontier_wait_share": (
            float(raw["frontier_wait_share"])
            if isinstance(raw.get("frontier_wait_share"), (int, float))
            else 0.0
        ),
        "frontier_runtime_share": (
            float(raw["frontier_runtime_share"])
            if isinstance(raw.get("frontier_runtime_share"), (int, float))
            else 0.0
        ),
        "sampled_elapsed_sec": sampled_elapsed_sec,
        "frontier_wait_per_frontier_sample_ns": optional_top_number(
            "frontier_wait_per_frontier_sample_ns"
        )
        or (frontier_wait / samples_with_frontier if samples_with_frontier > 0.0 else 0.0),
        "frontier_wait_per_schedstat_sample_ns": optional_top_number(
            "frontier_wait_per_schedstat_sample_ns"
        )
        or (frontier_wait / samples_with_schedstat if samples_with_schedstat > 0.0 else 0.0),
        "frontier_wait_per_elapsed_sec": optional_top_number("frontier_wait_per_elapsed_sec")
        or (frontier_wait / sampled_elapsed_sec if sampled_elapsed_sec > 0.0 else 0.0),
        "frontier_runtime_per_frontier_sample_ns": optional_top_number(
            "frontier_runtime_per_frontier_sample_ns"
        )
        or (frontier_runtime / samples_with_frontier if samples_with_frontier > 0.0 else 0.0),
        "frontier_runtime_per_elapsed_sec": optional_top_number(
            "frontier_runtime_per_elapsed_sec"
        )
        or (frontier_runtime / sampled_elapsed_sec if sampled_elapsed_sec > 0.0 else 0.0),
        "stage_delta_summary": stage_delta_summary,
    }


def summarize_values(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "n": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "p95": 0.0,
            "min": 0.0,
            "max": 0.0,
        }
    return {
        "n": float(len(values)),
        "mean": statistics.mean(values),
        "std": statistics.stdev(values) if len(values) > 1 else 0.0,
        "p95": percentile(values, 95),
        "min": min(values),
        "max": max(values),
    }


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


def aggregate_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    stages = sorted(
        {
            stage
            for trial in trials
            for stage in trial.get("workload", {}).get("stage_seconds", {})
        }
    )
    stage_summary: dict[str, dict[str, float]] = {}
    for stage in stages:
        values = [
            float(trial["workload"]["stage_seconds"][stage][0])
            for trial in trials
            if trial.get("workload", {}).get("stage_seconds", {}).get(stage)
        ]
        summary = summarize_values(values)
        stage_summary[stage] = {
            "n": summary["n"],
            "mean_sec": summary["mean"],
            "std_sec": summary["std"],
            "p95_sec": summary["p95"],
            "min_sec": summary["min"],
            "max_sec": summary["max"],
        }

    def mean_field(section: str, key: str) -> float:
        values = [
            float(trial.get(section, {}).get(key, 0.0))
            for trial in trials
            if trial.get(section)
        ]
        return statistics.mean(values) if values else 0.0

    def summarize_field(section: str, key: str) -> dict[str, float]:
        values = [
            float(trial.get(section, {}).get(key, 0.0))
            for trial in trials
            if trial.get(section)
        ]
        return summarize_values(values)

    controller_stage_counts: collections.Counter[str] = collections.Counter()
    controller_reasons: collections.Counter[str] = collections.Counter()
    controller_comms: collections.Counter[str] = collections.Counter()
    controller_adaptive_widths: collections.Counter[str] = collections.Counter()
    controller_adaptive_reasons: collections.Counter[str] = collections.Counter()
    controller_adaptive_propagation_depths: collections.Counter[str] = collections.Counter()
    controller_adaptive_propagation_reasons: collections.Counter[str] = collections.Counter()
    for trial in trials:
        ctl = trial.get("controller", {})
        controller_stage_counts.update(ctl.get("stage_counts", {}))
        controller_reasons.update(ctl.get("reason_counts", {}))
        controller_comms.update(ctl.get("frontier_comm_counts", {}))
        controller_adaptive_widths.update(ctl.get("adaptive_width_counts", {}))
        controller_adaptive_reasons.update(ctl.get("adaptive_reason_counts", {}))
        controller_adaptive_propagation_depths.update(ctl.get("adaptive_propagation_depth_counts", {}))
        controller_adaptive_propagation_reasons.update(ctl.get("adaptive_propagation_reason_counts", {}))

    total_values = [float(t["workload"].get("total_sec", 0.0)) for t in trials]
    total_summary = summarize_values(total_values)

    flowmon_summary = {
        key: summarize_field("flowmon", key)
        for key in (
            "seen_enqueues",
            "frontier_enqueues",
            "fallback_enqueues",
            "width_held_enqueues",
            "frontier_width_releases",
            "global_frontier_dispatches",
            "global_shared_dispatches",
            "global_shared_first_dispatches",
            "global_frontier_guard_trips",
            "global_frontier_guard_misses",
            "global_shared_cooldown_entries",
            "global_shared_cooldown_dispatches",
            "global_shared_cooldown_misses",
            "global_pressure_fallback_entries",
            "global_pressure_fallback_enqueues",
            "global_shared_wait_pressure_events",
            "global_shared_wait_pressure_wait_ns",
            "global_pressure_dispatch_entries",
            "global_pressure_dispatches",
            "global_pressure_dispatch_misses",
            "global_frontier_budget_windows",
            "global_frontier_budget_trips",
            "global_frontier_budget_shared",
            "global_frontier_budget_misses",
            "frontier_ratio",
            "fallback_ratio",
            "budget_used_ns",
            "budget_total_ns",
            "budget_used_ratio",
            "frontier_runnable_wait_ns",
            "frontier_runtime_ns",
        )
    }
    victim_summary = {
        key: summarize_field("victim", key)
        for key in (
            "iterations",
            "lateness_mean_ms",
            "lateness_p95_ms",
            "lateness_p99_ms",
            "lateness_max_ms",
            "runtime_mean_ms",
            "runtime_p95_ms",
            "runtime_p99_ms",
            "runtime_max_ms",
        )
    }
    characterization_keys = (
        "sample_count",
        "samples_with_frontier",
        "samples_with_parent_wait",
        "samples_with_barrier_tail",
        "samples_with_schedstat",
        "max_process_count",
        "max_leaf_count",
        "cpu_ticks_delta",
        "frontier_cpu_ticks_delta",
        "sched_runtime_delta_ns",
        "frontier_sched_runtime_delta_ns",
        "sched_wait_delta_ns",
        "frontier_sched_wait_delta_ns",
        "sched_slices_delta",
        "frontier_sched_slices_delta",
        "frontier_wait_share",
        "frontier_runtime_share",
        "sampled_elapsed_sec",
        "frontier_wait_per_frontier_sample_ns",
        "frontier_wait_per_schedstat_sample_ns",
        "frontier_wait_per_elapsed_sec",
        "frontier_runtime_per_frontier_sample_ns",
        "frontier_runtime_per_elapsed_sec",
    )
    characterization_summary = {
        key: summarize_field("characterization", key) for key in characterization_keys
    }

    return {
        "stage_summary": stage_summary,
        "total_summary": {
            "n": total_summary["n"],
            "mean_sec": total_summary["mean"],
            "std_sec": total_summary["std"],
            "p95_sec": total_summary["p95"],
            "min_sec": total_summary["min"],
            "max_sec": total_summary["max"],
        },
        "total_mean_sec": total_summary["mean"],
        "controller": {
            "records": sum(int(t.get("controller", {}).get("records", 0)) for t in trials),
            "selected_samples": sum(int(t.get("controller", {}).get("selected_samples", 0)) for t in trials),
            "frontier_records": sum(int(t.get("controller", {}).get("frontier_records", 0)) for t in trials),
            "propagation_records": sum(int(t.get("controller", {}).get("propagation_records", 0)) for t in trials),
            "propagation_anchors": sum(int(t.get("controller", {}).get("propagation_anchors", 0)) for t in trials),
            "stability_records": sum(int(t.get("controller", {}).get("stability_records", 0)) for t in trials),
            "stability_selected": sum(int(t.get("controller", {}).get("stability_selected", 0)) for t in trials),
            "stability_held": sum(int(t.get("controller", {}).get("stability_held", 0)) for t in trials),
            "stability_suppressed": sum(int(t.get("controller", {}).get("stability_suppressed", 0)) for t in trials),
            "stability_observed_unstable": sum(int(t.get("controller", {}).get("stability_observed_unstable", 0)) for t in trials),
            "adaptive_records": sum(int(t.get("controller", {}).get("adaptive_records", 0)) for t in trials),
            "adaptive_enabled_records": sum(int(t.get("controller", {}).get("adaptive_enabled_records", 0)) for t in trials),
            "adaptive_widened_records": sum(int(t.get("controller", {}).get("adaptive_widened_records", 0)) for t in trials),
            "adaptive_max_effective_width": max(
                [int(t.get("controller", {}).get("adaptive_max_effective_width", 0)) for t in trials]
                or [0]
            ),
            "adaptive_width_counts": dict(controller_adaptive_widths),
            "adaptive_reason_counts": dict(controller_adaptive_reasons),
            "adaptive_propagation_records": sum(
                int(t.get("controller", {}).get("adaptive_propagation_records", 0)) for t in trials
            ),
            "adaptive_propagation_enabled_records": sum(
                int(t.get("controller", {}).get("adaptive_propagation_enabled_records", 0)) for t in trials
            ),
            "adaptive_propagation_depth_counts": dict(controller_adaptive_propagation_depths),
            "adaptive_propagation_reason_counts": dict(controller_adaptive_propagation_reasons),
            "stage_counts": dict(controller_stage_counts),
            "reason_counts": dict(controller_reasons),
            "frontier_comm_counts": dict(controller_comms),
        },
        "flowmon_mean": {
            "seen_enqueues": mean_field("flowmon", "seen_enqueues"),
            "frontier_enqueues": mean_field("flowmon", "frontier_enqueues"),
            "fallback_enqueues": mean_field("flowmon", "fallback_enqueues"),
            "width_held_enqueues": mean_field("flowmon", "width_held_enqueues"),
            "frontier_width_releases": mean_field("flowmon", "frontier_width_releases"),
            "global_frontier_dispatches": mean_field("flowmon", "global_frontier_dispatches"),
            "global_shared_dispatches": mean_field("flowmon", "global_shared_dispatches"),
            "global_shared_first_dispatches": mean_field("flowmon", "global_shared_first_dispatches"),
            "global_frontier_guard_trips": mean_field("flowmon", "global_frontier_guard_trips"),
            "global_frontier_guard_misses": mean_field("flowmon", "global_frontier_guard_misses"),
            "global_shared_cooldown_entries": mean_field("flowmon", "global_shared_cooldown_entries"),
            "global_shared_cooldown_dispatches": mean_field("flowmon", "global_shared_cooldown_dispatches"),
            "global_shared_cooldown_misses": mean_field("flowmon", "global_shared_cooldown_misses"),
            "global_pressure_fallback_entries": mean_field("flowmon", "global_pressure_fallback_entries"),
            "global_pressure_fallback_enqueues": mean_field("flowmon", "global_pressure_fallback_enqueues"),
            "global_shared_wait_pressure_events": mean_field("flowmon", "global_shared_wait_pressure_events"),
            "global_shared_wait_pressure_wait_ns": mean_field("flowmon", "global_shared_wait_pressure_wait_ns"),
            "global_pressure_dispatch_entries": mean_field("flowmon", "global_pressure_dispatch_entries"),
            "global_pressure_dispatches": mean_field("flowmon", "global_pressure_dispatches"),
            "global_pressure_dispatch_misses": mean_field("flowmon", "global_pressure_dispatch_misses"),
            "global_frontier_dsq_enqueues": mean_field("flowmon", "global_frontier_dsq_enqueues"),
            "global_tracked_shared_path_enqueues": mean_field("flowmon", "global_tracked_shared_path_enqueues"),
            "global_untracked_shared_path_enqueues": mean_field("flowmon", "global_untracked_shared_path_enqueues"),
            "global_shared_global_enqueues": mean_field("flowmon", "global_shared_global_enqueues"),
            "global_shared_global_tracked_enqueues": mean_field(
                "flowmon", "global_shared_global_tracked_enqueues"
            ),
            "global_shared_global_untracked_enqueues": mean_field(
                "flowmon", "global_shared_global_untracked_enqueues"
            ),
            "global_latency_dsq_enqueues": mean_field("flowmon", "global_latency_dsq_enqueues"),
            "global_shared_local_tracked_enqueues": mean_field(
                "flowmon", "global_shared_local_tracked_enqueues"
            ),
            "global_shared_local_untracked_enqueues": mean_field(
                "flowmon", "global_shared_local_untracked_enqueues"
            ),
            "global_shared_percpu_tracked_enqueues": mean_field(
                "flowmon", "global_shared_percpu_tracked_enqueues"
            ),
            "global_shared_percpu_untracked_enqueues": mean_field(
                "flowmon", "global_shared_percpu_untracked_enqueues"
            ),
            "global_shared_local_pick_fallbacks": mean_field(
                "flowmon", "global_shared_local_pick_fallbacks"
            ),
            "global_shared_percpu_pick_fallbacks": mean_field(
                "flowmon", "global_shared_percpu_pick_fallbacks"
            ),
            "global_shared_local_frontier_fallback_enqueues": mean_field(
                "flowmon", "global_shared_local_frontier_fallback_enqueues"
            ),
            "global_frontier_budget_windows": mean_field("flowmon", "global_frontier_budget_windows"),
            "global_frontier_budget_trips": mean_field("flowmon", "global_frontier_budget_trips"),
            "global_frontier_budget_shared": mean_field("flowmon", "global_frontier_budget_shared"),
            "global_frontier_budget_misses": mean_field("flowmon", "global_frontier_budget_misses"),
            "frontier_ratio": mean_field("flowmon", "frontier_ratio"),
            "fallback_ratio": mean_field("flowmon", "fallback_ratio"),
            "budget_used_ns": mean_field("flowmon", "budget_used_ns"),
            "budget_total_ns": mean_field("flowmon", "budget_total_ns"),
            "budget_used_ratio": mean_field("flowmon", "budget_used_ratio"),
            "frontier_runnable_wait_ns": mean_field("flowmon", "frontier_runnable_wait_ns"),
            "frontier_runtime_ns": mean_field("flowmon", "frontier_runtime_ns"),
        },
        "flowmon_summary": flowmon_summary,
        "victim_mean": {
            "iterations": mean_field("victim", "iterations"),
            "lateness_mean_ms": mean_field("victim", "lateness_mean_ms"),
            "lateness_p95_ms": mean_field("victim", "lateness_p95_ms"),
            "lateness_p99_ms": mean_field("victim", "lateness_p99_ms"),
            "lateness_max_ms": mean_field("victim", "lateness_max_ms"),
            "runtime_mean_ms": mean_field("victim", "runtime_mean_ms"),
            "runtime_p95_ms": mean_field("victim", "runtime_p95_ms"),
            "runtime_p99_ms": mean_field("victim", "runtime_p99_ms"),
            "runtime_max_ms": mean_field("victim", "runtime_max_ms"),
        },
        "victim_summary": victim_summary,
        "characterization_mean": {
            key: mean_field("characterization", key) for key in characterization_keys
        },
        "characterization_summary": characterization_summary,
    }


def audit_variant_claim(variant: dict[str, Any]) -> dict[str, Any]:
    """Classify what this variant can support.

    This audit is intentionally conservative.  It does not try to infer a
    speedup from wall-clock time.  The goal is to keep each result directory
    self-describing: a reader should see immediately whether the logs support
    only mechanism connectivity or something stronger.
    """

    name = str(variant.get("name", ""))
    kind = str(variant.get("kind", ""))
    trials = variant.get("trials") if isinstance(variant.get("trials"), list) else []
    agg = variant.get("aggregate", {}) if isinstance(variant.get("aggregate"), dict) else {}
    ctl = agg.get("controller", {}) if isinstance(agg.get("controller"), dict) else {}
    flow = agg.get("flowmon_mean", {}) if isinstance(agg.get("flowmon_mean"), dict) else {}
    victim = agg.get("victim_mean", {}) if isinstance(agg.get("victim_mean"), dict) else {}

    blockers: list[str] = []
    evidence: list[str] = []
    forbidden = [
        "end-to-end speedup",
        "frontier inference accuracy",
        "victim slowdown bound",
        "fairness improvement",
    ]

    if kind != "flowcausal":
        return {
            "variant": name,
            "highest_level": "baseline",
            "summary": "Baseline/taskset row; useful for workload timing reference, not a FlowCausal mechanism claim.",
            "evidence": ["workload stage markers"],
            "blockers_for_next_level": ["no controller trace", "no BPF admission counters"],
            "forbidden_claims": forbidden,
        }

    frontier_records = int(ctl.get("frontier_records", 0) or 0)
    selected_samples = int(ctl.get("selected_samples", 0) or 0)
    frontier_enqueues = float(flow.get("frontier_enqueues", 0.0) or 0.0)
    fallback_enqueues = float(flow.get("fallback_enqueues", 0.0) or 0.0)
    seen_enqueues = float(flow.get("seen_enqueues", 0.0) or 0.0)
    frontier_wait = float(flow.get("frontier_runnable_wait_ns", 0.0) or 0.0)
    frontier_runtime = float(flow.get("frontier_runtime_ns", 0.0) or 0.0)
    victim_iterations = float(victim.get("iterations", 0.0) or 0.0)
    policy_fallback = bool(variant.get("policy_fallback", False))
    adaptive_frontier_width = bool(variant.get("adaptive_frontier_width", False))
    if adaptive_frontier_width:
        forbidden.append("adaptive width improves performance or correctly identifies the critical cohort")
    adaptive_records = int(ctl.get("adaptive_records", 0) or 0)
    adaptive_enabled_records = int(ctl.get("adaptive_enabled_records", 0) or 0)
    adaptive_widened_records = int(ctl.get("adaptive_widened_records", 0) or 0)
    adaptive_max_effective_width = int(ctl.get("adaptive_max_effective_width", 0) or 0)
    guard_trips = float(flow.get("global_frontier_guard_trips", 0.0) or 0.0)
    shared_first = float(flow.get("global_shared_first_dispatches", 0.0) or 0.0)
    cooldown_entries = float(flow.get("global_shared_cooldown_entries", 0.0) or 0.0)
    cooldown_dispatches = float(flow.get("global_shared_cooldown_dispatches", 0.0) or 0.0)
    cooldown_misses = float(flow.get("global_shared_cooldown_misses", 0.0) or 0.0)
    pressure_fallback_entries = float(flow.get("global_pressure_fallback_entries", 0.0) or 0.0)
    pressure_fallback_enqueues = float(flow.get("global_pressure_fallback_enqueues", 0.0) or 0.0)
    shared_wait_pressure_events = float(flow.get("global_shared_wait_pressure_events", 0.0) or 0.0)
    shared_wait_pressure_wait_ns = float(flow.get("global_shared_wait_pressure_wait_ns", 0.0) or 0.0)
    pressure_dispatch_entries = float(flow.get("global_pressure_dispatch_entries", 0.0) or 0.0)
    pressure_dispatches = float(flow.get("global_pressure_dispatches", 0.0) or 0.0)
    pressure_dispatch_misses = float(flow.get("global_pressure_dispatch_misses", 0.0) or 0.0)
    budget_trips = float(flow.get("global_frontier_budget_trips", 0.0) or 0.0)
    budget_shared = float(flow.get("global_frontier_budget_shared", 0.0) or 0.0)
    budget_misses = float(flow.get("global_frontier_budget_misses", 0.0) or 0.0)
    pressure_fallback_enabled = (
        int(variant.get("pressure_fallback_enqueues", 0) or 0) > 0
        or int(variant.get("pressure_fallback_ms", 0) or 0) > 0
    )
    pressure_dispatch_enabled = int(variant.get("pressure_dispatch_ms", 0) or 0) > 0
    dispatch_budget_enabled = (
        int(variant.get("frontier_dispatch_window_ms", 0) or 0) > 0
        and int(variant.get("frontier_dispatch_window_budget", 0) or 0) > 0
    )

    if selected_samples > 0 and frontier_records > 0:
        evidence.append(f"controller selected {frontier_records} frontier records across {selected_samples} samples")
    else:
        blockers.append("controller did not select auditable frontiers")

    if adaptive_frontier_width:
        if adaptive_records > 0 and adaptive_enabled_records > 0:
            evidence.append(
                "adaptive width audit present: "
                f"records={adaptive_records}, widened={adaptive_widened_records}, "
                f"max_width={adaptive_max_effective_width}"
            )
        else:
            blockers.append("adaptive-width variant lacks controller adaptive_width audit records")

    if seen_enqueues > 0 and frontier_enqueues > 0:
        evidence.append(f"BPF admitted mean frontier_enqueues={frontier_enqueues:.1f}")
    elif policy_fallback and seen_enqueues > 0 and fallback_enqueues > 0:
        evidence.append(
            "explicit policy fallback routed marked frontiers to shared DSQ "
            f"with mean fallback_enqueues={fallback_enqueues:.1f}"
        )
    else:
        blockers.append("flowmon-final lacks positive frontier admission or fallback routing")

    if frontier_wait > 0 or frontier_runtime > 0:
        evidence.append("frontier runnable-wait/runtime counters are nonzero")
    elif policy_fallback and fallback_enqueues > 0:
        evidence.append("frontier admission counters are intentionally suppressed by policy fallback")
    elif pressure_fallback_enabled and pressure_fallback_enqueues > 0:
        evidence.append("some frontier admission counters are intentionally suppressed by pressure fallback")
    else:
        blockers.append("frontier runnable-wait/runtime accounting is absent or zero")

    if len(trials) >= 3:
        evidence.append(f"{len(trials)} trials recorded")
    else:
        blockers.append("fewer than 3 trials; not enough for a stable performance claim")

    if victim_iterations > 0:
        evidence.append("victim sidecar summary is present")
    else:
        blockers.append("no victim sidecar evidence")

    if guard_trips > 0:
        evidence.append(f"frontier dispatch guard tripped mean={guard_trips:.1f}")
        if shared_first > 0:
            evidence.append(f"shared-first dispatches mean={shared_first:.1f}")
    if cooldown_entries > 0:
        evidence.append(
            "shared-first cooldown mean="
            f"entries={cooldown_entries:.1f}, dispatches={cooldown_dispatches:.1f}, "
            f"misses={cooldown_misses:.1f}"
        )
    if pressure_fallback_entries > 0 or pressure_fallback_enqueues > 0:
        evidence.append(
            "pressure fallback mean="
            f"entries={pressure_fallback_entries:.1f}, "
            f"enqueues={pressure_fallback_enqueues:.1f}"
        )
    if shared_wait_pressure_events > 0:
        evidence.append(
            "shared-wait pressure mean="
            f"events={shared_wait_pressure_events:.1f}, "
            f"wait_ms={shared_wait_pressure_wait_ns / 1_000_000.0:.3f}"
        )
    if pressure_dispatch_entries > 0 or pressure_dispatches > 0 or pressure_dispatch_misses > 0:
        evidence.append(
            "pressure dispatch mean="
            f"entries={pressure_dispatch_entries:.1f}, "
            f"shared={pressure_dispatches:.1f}, "
            f"misses={pressure_dispatch_misses:.1f}"
        )
    if budget_trips > 0 or budget_shared > 0 or budget_misses > 0:
        evidence.append(
            "dispatch-window budget mean="
            f"trips={budget_trips:.1f}, shared={budget_shared:.1f}, "
            f"misses={budget_misses:.1f}"
        )

    if (
        pressure_fallback_enabled
        and dispatch_budget_enabled
        and frontier_records > 0
        and frontier_enqueues > 0
        and pressure_fallback_enqueues > 0
        and budget_trips > 0
    ):
        highest = "L1: mechanism + pressure+dispatch-budget connectivity"
        summary = (
            "Supports online hinting -> BPF frontier admission plus a "
            "dispatch-window shared-first probe that can open pressure "
            "fallback for this generated build/test workload; it does not "
            "prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif (
        pressure_fallback_enabled
        and pressure_dispatch_enabled
        and frontier_records > 0
        and frontier_enqueues > 0
        and pressure_fallback_enqueues > 0
        and pressure_dispatch_entries > 0
    ):
        highest = "L1: mechanism + pressure-fallback+pressure-dispatch connectivity"
        summary = (
            "Supports online hinting -> BPF frontier admission plus "
            "shared-pressure-triggered admission fallback and dispatch-side "
            "shared-first backoff for this generated build/test workload; it "
            "does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif (
        pressure_fallback_enabled
        and frontier_records > 0
        and frontier_enqueues > 0
        and pressure_fallback_enqueues > 0
    ):
        highest = "L1: mechanism + pressure-fallback connectivity"
        summary = (
            "Supports online hinting -> BPF frontier admission plus "
            "shared-pressure-triggered admission fallback for this generated "
            "build/test workload; it does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif dispatch_budget_enabled and frontier_records > 0 and frontier_enqueues > 0 and budget_trips > 0:
        highest = "L1: mechanism + dispatch-budget connectivity"
        summary = (
            "Supports online hinting -> BPF frontier admission plus a "
            "windowed frontier-dispatch budget path for this generated "
            "build/test workload; it does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif frontier_records > 0 and frontier_enqueues > 0:
        highest = "L1: mechanism connectivity"
        summary = "Supports online hinting -> BPF frontier admission connectivity for this generated build/test workload."
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif policy_fallback and frontier_records > 0 and fallback_enqueues > 0:
        highest = "L1: safety-fallback connectivity"
        summary = (
            "Supports online hinting -> explicit policy fallback routing for this "
            "generated build/test workload; it does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif pressure_fallback_enabled and frontier_records > 0 and pressure_fallback_enqueues > 0:
        highest = "L1: pressure-fallback connectivity"
        summary = (
            "Supports shared-pressure-triggered admission fallback for this "
            "generated build/test workload; it does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif pressure_dispatch_enabled and frontier_records > 0 and pressure_dispatch_entries > 0:
        highest = "L1: pressure-dispatch connectivity"
        summary = (
            "Supports shared-pressure-triggered dispatch-side shared-first "
            "backoff for this generated build/test workload; it does not prove "
            "victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    elif dispatch_budget_enabled and frontier_records > 0 and budget_trips > 0:
        highest = "L1: dispatch-budget connectivity"
        summary = (
            "Supports a windowed frontier-dispatch budget path for this "
            "generated build/test workload; it does not prove victim protection."
        )
        blockers.append("no oracle/default direct-mechanism comparison proving L2")
    else:
        highest = "L0: runnable/instrumented only"
        summary = "Does not yet prove that controller-selected frontiers reached BPF admission."

    return {
        "variant": name,
        "highest_level": highest,
        "summary": summary,
        "evidence": evidence,
        "blockers_for_next_level": blockers,
        "forbidden_claims": forbidden,
    }


def audit_matrix_claims(matrix: dict[str, Any]) -> dict[str, Any]:
    config = matrix.get("config", {}) if isinstance(matrix.get("config"), dict) else {}
    variants = matrix.get("variants", []) if isinstance(matrix.get("variants"), list) else []
    variant_claims = [audit_variant_claim(variant) for variant in variants]

    claim_levels = [str(item.get("highest_level", "")) for item in variant_claims]
    has_l1 = any(level.startswith("L1") for level in claim_levels)
    has_mechanism_pressure = any(
        level == "L1: mechanism + pressure-fallback connectivity" for level in claim_levels
    )
    has_mechanism_pressure_dispatch = any(
        level == "L1: mechanism + pressure-fallback+pressure-dispatch connectivity"
        for level in claim_levels
    )
    has_mechanism_pressure_dispatch_budget = any(
        level == "L1: mechanism + pressure+dispatch-budget connectivity"
        for level in claim_levels
    )
    has_mechanism_dispatch_budget = any(
        level == "L1: mechanism + dispatch-budget connectivity" for level in claim_levels
    )
    has_pressure = any(level == "L1: pressure-fallback connectivity" for level in claim_levels)
    has_pressure_dispatch = any(level == "L1: pressure-dispatch connectivity" for level in claim_levels)
    has_dispatch_budget = any(level == "L1: dispatch-budget connectivity" for level in claim_levels)
    has_mechanism = any(level == "L1: mechanism connectivity" for level in claim_levels)
    has_flowcausal = any(str(variant.get("kind", "")) == "flowcausal" for variant in variants)
    has_victim = bool(config.get("victim_command"))
    has_stability = bool(config.get("include_stability_variant"))
    has_propagation = bool(config.get("include_propagation_variant"))
    has_dispatch_guard_variants = bool(config.get("include_dispatch_guard_variants"))
    has_guard_cooldown_variants = bool(config.get("include_guard_cooldown_variants"))
    has_dispatch_budget_variants = bool(config.get("include_dispatch_budget_variants"))
    has_frontier_width_variants = bool(config.get("include_frontier_width_variants"))
    has_adaptive_width_variant = any(
        str(variant.get("kind", "")) == "flowcausal"
        and bool(variant.get("adaptive_frontier_width", False))
        for variant in variants
    )
    has_policy_fallback_variant = any(
        str(variant.get("kind", "")) == "flowcausal"
        and bool(variant.get("policy_fallback", False))
        for variant in variants
    )
    has_pressure_fallback_variants = any(
        str(variant.get("kind", "")) == "flowcausal"
        and (
            int(variant.get("pressure_fallback_enqueues", 0) or 0) > 0
            or int(variant.get("pressure_fallback_ms", 0) or 0) > 0
        )
        for variant in variants
    )
    trials = int(config.get("trials", 0) or 0)

    gaps: list[str] = []
    if not has_flowcausal:
        gaps.append("no live FlowCausal variant")
    if trials < 3:
        gaps.append("fewer than 3 trials per variant")
    if not has_victim:
        gaps.append("no victim sidecar / fairness safety evidence")
    if not has_stability:
        gaps.append("no stability/debounce ablation variant")
    if not has_propagation:
        gaps.append("no fork-time propagation ablation variant")
    if not has_dispatch_guard_variants:
        gaps.append("no dispatch-guard quota ablation variants")
    if not has_guard_cooldown_variants:
        gaps.append("no shared-first cooldown ablation variants")
    if not has_dispatch_budget_variants:
        gaps.append("no frontier dispatch-window budget ablation variants")
    if not has_frontier_width_variants:
        gaps.append("no static frontier-width ablation variants")
    if not has_adaptive_width_variant:
        gaps.append("no adaptive frontier-width variant")
    if not has_policy_fallback_variant:
        gaps.append("no explicit policy-fallback safety-valve variant")
    if not has_pressure_fallback_variants:
        gaps.append("no shared-pressure admission-fallback variants")
    gaps.extend(
        [
            "no oracle frontier upper-bound row",
            "no direct mechanism delta against a comparable default/oracle baseline",
            "no cgroup cpu.weight or static affinity/nice baseline beyond taskset",
            "generated build/test workload only; no real code-agent or CI repository yet",
        ]
    )

    if has_mechanism_pressure_dispatch_budget:
        highest_supported_claim_level = "L1: mechanism + pressure+dispatch-budget connectivity"
    elif has_mechanism_pressure_dispatch:
        highest_supported_claim_level = "L1: mechanism + pressure-fallback+pressure-dispatch connectivity"
    elif has_mechanism_pressure:
        highest_supported_claim_level = "L1: mechanism + pressure-fallback connectivity"
    elif has_mechanism_dispatch_budget:
        highest_supported_claim_level = "L1: mechanism + dispatch-budget connectivity"
    elif has_pressure:
        highest_supported_claim_level = "L1: pressure-fallback connectivity"
    elif has_pressure_dispatch:
        highest_supported_claim_level = "L1: pressure-dispatch connectivity"
    elif has_dispatch_budget:
        highest_supported_claim_level = "L1: dispatch-budget connectivity"
    elif has_mechanism or has_l1:
        highest_supported_claim_level = "L1: mechanism connectivity"
    else:
        highest_supported_claim_level = "L0: runnable/instrumented only"

    return {
        "highest_supported_claim_level": highest_supported_claim_level,
        "summary": (
            "This matrix can be used as mechanism evidence, not as a paper-grade "
            "end-to-end performance result."
        ),
        "variant_claims": variant_claims,
        "paper_claim_gaps": gaps,
        "permitted_claims": [
            "controller-selected frontiers can be written into BPF maps",
            "the FlowCausal BPF path can admit frontier tasks and expose counters",
            "width/stability/propagation/dispatch-guard knobs can be audited when their variants are present",
            "static and adaptive frontier-width choices can be audited through controller adaptive_width records when their variants are present",
            "dispatch guard and shared-first cooldown activity can be audited through global_stats when live FlowCausal runs are present",
            "frontier dispatch-window budget activity can be audited through global_stats when its variants are present",
            "explicit policy fallback can be audited as a safety valve when controller frontiers are routed to fallback_enqueues",
            "count- or time-bounded shared-pressure admission fallback can be audited through pressure_fallback_entries/enqueues when its variants are present",
            "untracked shared-task wait can be audited through shared_wait_pressure_events/wait_ns when --shared-wait-pressure-us is enabled",
            "dispatch-side pressure backoff can be audited through pressure_dispatch_entries/dispatches/misses when --pressure-dispatch-ms is enabled",
        ],
        "forbidden_claims": [
            "FlowCausal accelerates code agents",
            "FlowCausal improves CI/build/test completion",
            "frontier inference is accurate",
            "width-control improves fairness",
            "adaptive width improves performance or correctly identifies the critical cohort",
            "victim slowdown is bounded",
        ],
    }


def run_default_trial(args: argparse.Namespace, trial_dir: pathlib.Path) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    inner_cmd = workload_args(args)
    characterization_dir = trial_dir / "characterization"
    if args.collect_characterization:
        cmd = [
            sys.executable,
            str(CHARACTERIZE_SANDBOX),
            "--out-dir",
            str(characterization_dir),
            "--overwrite",
            "--interval-ms",
            str(args.characterization_interval_ms),
            "--frontier-width",
            str(max(1, args.frontier_width)),
            "--min-frontier-score",
            str(args.min_frontier_score),
            "--stage-kind",
            "AUTO",
            "--top-candidates",
            str(max(1, args.characterization_top_candidates)),
        ]
        if args.adaptive_frontier_width:
            cmd.extend(
                [
                    "--adaptive-frontier-width",
                    "--adaptive-max-frontier-width",
                    str(max(1, args.adaptive_max_frontier_width)),
                ]
            )
        if args.taskset:
            cmd.extend(["--taskset", args.taskset])
        cmd.append("--")
        cmd.extend(inner_cmd)
        workload_log = characterization_dir / "workload.log"
        wrapper_log = trial_dir / "characterizer.log"
    else:
        cmd = inner_cmd
        if args.taskset:
            cmd = ["taskset", "-c", args.taskset, *cmd]
        workload_log = trial_dir / "workload.log"
        wrapper_log = workload_log
    sidecar_cmd = victim_args(args)
    victim_proc: subprocess.Popen[str] | None = None

    manifest = {
        "variant_kind": "default",
        "command": cmd,
        "inner_command": inner_cmd,
        "collect_characterization": bool(args.collect_characterization),
        "characterization_dir": str(characterization_dir) if args.collect_characterization else None,
        "victim_cmd": sidecar_cmd,
        "sched_ext_state_before": sysfs_value("/sys/kernel/sched_ext/state"),
        "sched_ext_nr_rejected_before": sysfs_value("/sys/kernel/sched_ext/nr_rejected"),
    }
    proc: subprocess.CompletedProcess[str] | None = None
    elapsed = 0.0
    try:
        if sidecar_cmd:
            victim_proc = start_logged(sidecar_cmd, trial_dir / "victim.log", start_new_session=True)
            if args.victim_warmup_sec > 0:
                time.sleep(args.victim_warmup_sec)
            if victim_proc.poll() is not None and victim_proc.returncode != 0:
                raise RuntimeError(f"victim exited early with code {victim_proc.returncode}")
        start = time.monotonic()
        with wrapper_log.open("w", encoding="utf-8") as log:
            print("+ " + " ".join(cmd), flush=True)
            proc = subprocess.run(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True)
        elapsed = time.monotonic() - start
    finally:
        if victim_proc is not None:
            if victim_proc.poll() is None:
                manifest["victim_stopped_with_workload"] = True
                manifest["victim_returncode"] = stop_process_group(victim_proc, "victim")
            else:
                manifest["victim_stopped_with_workload"] = False
                manifest["victim_returncode"] = victim_proc.returncode
    if proc is None:
        raise RuntimeError("default workload did not start")
    if (
        manifest.get("victim_stopped_with_workload") is False
        and manifest.get("victim_returncode") not in (0, None)
    ):
        raise RuntimeError(f"victim failed with code {manifest.get('victim_returncode')}")
    manifest["returncode"] = proc.returncode
    manifest["elapsed_sec"] = elapsed
    manifest["sched_ext_state_after"] = sysfs_value("/sys/kernel/sched_ext/state")
    manifest["sched_ext_nr_rejected_after"] = sysfs_value("/sys/kernel/sched_ext/nr_rejected")
    (trial_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"default workload failed with code {proc.returncode}")
    return {
        "path": str(trial_dir),
        "manifest": manifest,
        "workload": parse_workload_log(workload_log),
        "controller": {},
        "flowmon": {},
        "victim": parse_victim_log(trial_dir / "victim.log"),
        "characterization": parse_characterization_summary(characterization_dir / "summary.json"),
    }


def run_live_trial(
    args: argparse.Namespace,
    trial_dir: pathlib.Path,
    *,
    width_control: bool,
    frontier_width: int | None = None,
    max_frontier_width: int | None = None,
    adaptive_frontier_width: bool | None = None,
    adaptive_max_frontier_width: int | None = None,
    frontier_propagation: bool | None = None,
    frontier_propagation_depth: int | None = None,
    frontier_stability_samples: int | None = None,
    frontier_hold_ms: int | None = None,
    frontier_bypass_score: int | None = None,
    frontier_selected_stability: bool | None = None,
    frontier_stability_action: str | None = None,
    frontier_dispatch_quota: int | None = None,
    guard_cooldown_dispatches: int | None = None,
    pressure_fallback_enqueues: int | None = None,
    pressure_fallback_ms: int | None = None,
    frontier_dispatch_window_ms: int | None = None,
    frontier_dispatch_window_budget: int | None = None,
    policy_fallback: bool | None = None,
) -> dict[str, Any]:
    stability_samples = (
        args.frontier_stability_samples
        if frontier_stability_samples is None
        else frontier_stability_samples
    )
    hold_ms = args.frontier_hold_ms if frontier_hold_ms is None else frontier_hold_ms
    bypass_score = (
        args.frontier_bypass_score
        if frontier_bypass_score is None
        else frontier_bypass_score
    )
    selected_stability = (
        args.frontier_selected_stability
        if frontier_selected_stability is None
        else frontier_selected_stability
    )
    stability_action = (
        args.frontier_stability_action
        if frontier_stability_action is None
        else frontier_stability_action
    )
    propagation = args.frontier_propagation if frontier_propagation is None else frontier_propagation
    propagation_depth = (
        args.frontier_propagation_depth
        if frontier_propagation_depth is None
        else frontier_propagation_depth
    )
    dispatch_quota = (
        args.frontier_dispatch_quota
        if frontier_dispatch_quota is None
        else frontier_dispatch_quota
    )
    cooldown_dispatches = (
        args.guard_cooldown_dispatches
        if guard_cooldown_dispatches is None
        else guard_cooldown_dispatches
    )
    pressure_fallback = (
        args.pressure_fallback_enqueues
        if pressure_fallback_enqueues is None
        else pressure_fallback_enqueues
    )
    pressure_ms = (
        args.pressure_fallback_ms
        if pressure_fallback_ms is None
        else pressure_fallback_ms
    )
    dispatch_window_ms = (
        args.frontier_dispatch_window_ms
        if frontier_dispatch_window_ms is None
        else frontier_dispatch_window_ms
    )
    dispatch_window_budget = (
        args.frontier_dispatch_window_budget
        if frontier_dispatch_window_budget is None
        else frontier_dispatch_window_budget
    )
    fallback_policy = args.policy_fallback if policy_fallback is None else policy_fallback
    effective_frontier_width = args.frontier_width if frontier_width is None else frontier_width
    effective_max_frontier_width = (
        args.max_frontier_width if max_frontier_width is None else max_frontier_width
    )
    adaptive_width = (
        args.adaptive_frontier_width
        if adaptive_frontier_width is None
        else adaptive_frontier_width
    )
    adaptive_width_cap = (
        args.adaptive_max_frontier_width
        if adaptive_max_frontier_width is None
        else adaptive_max_frontier_width
    )
    cmd = [
        sys.executable,
        str(LIVE_SANDBOX),
        "--attach-scheduler",
        "--no-build-tools",
        "--out-dir",
        str(trial_dir),
        "--stage-kind",
        "AUTO",
        "--frontier-width",
        str(max(1, effective_frontier_width)),
        "--min-frontier-score",
        str(args.min_frontier_score),
    ]
    if adaptive_width:
        cmd.extend(
            [
                "--adaptive-frontier-width",
                "--adaptive-max-frontier-width",
                str(max(1, adaptive_width_cap)),
            ]
        )
    if propagation:
        cmd.append("--frontier-propagation")
        cmd.extend(["--frontier-propagation-depth", str(propagation_depth)])
    cmd.extend(
        [
        "--frontier-stability-samples",
        str(stability_samples),
        "--frontier-hold-ms",
        str(hold_ms),
        "--frontier-bypass-score",
        str(bypass_score),
        *(["--frontier-selected-stability"] if selected_stability else []),
        "--frontier-stability-action",
        stability_action,
        "--build-frontier-admission",
        args.build_frontier_admission,
        "--interval-ms",
        str(args.interval_ms),
        "--ttl-ms",
        str(args.ttl_ms),
        "--confidence",
        str(args.confidence),
        "--policy-window-ms",
        str(args.policy_window_ms),
        "--boost-budget-ms",
        str(args.boost_budget_ms),
        "--max-vtime-boost-us",
        str(args.max_vtime_boost_us),
        "--frontier-dispatch-quota",
        str(max(0, dispatch_quota)),
        "--guard-cooldown-dispatches",
        str(max(0, cooldown_dispatches)),
        "--pressure-fallback-enqueues",
        str(max(0, pressure_fallback)),
        "--pressure-fallback-ms",
        str(max(0, pressure_ms)),
        "--shared-wait-pressure-us",
        str(max(0, args.shared_wait_pressure_us)),
        "--pressure-dispatch-ms",
        str(max(0, args.pressure_dispatch_ms)),
        "--frontier-dispatch-window-ms",
        str(max(0, dispatch_window_ms)),
        "--frontier-dispatch-window-budget",
        str(max(0, dispatch_window_budget)),
        ]
    )
    if args.collect_characterization:
        cmd.extend(
            [
                "--collect-characterization",
                "--characterization-interval-ms",
                str(args.characterization_interval_ms),
                "--characterization-top-candidates",
                str(max(1, args.characterization_top_candidates)),
            ]
        )
    if args.taskset:
        cmd.extend(["--taskset", args.taskset])
    if width_control:
        cmd.extend(["--width-control", "--max-frontier-width", str(max(1, effective_max_frontier_width))])
    if fallback_policy:
        cmd.append("--policy-fallback")
    if args.victim_command:
        cmd.extend(["--victim-command", args.victim_command])
        if args.victim_taskset:
            cmd.extend(["--victim-taskset", args.victim_taskset])
        cmd.extend(["--victim-warmup-sec", str(args.victim_warmup_sec)])
    cmd.append("--")
    cmd.extend(workload_args(args))

    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    manifest_path = trial_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    return {
        "path": str(trial_dir),
        "manifest": manifest,
        "workload": parse_workload_log(trial_dir / "workload.log"),
        "controller": parse_controller(trial_dir / "controller.ndjson"),
        "flowmon": parse_flowmon(trial_dir / "flowmon-final.log"),
        "victim": parse_victim_log(trial_dir / "victim.log"),
        "characterization": parse_characterization_summary(
            trial_dir / "characterization" / "summary.json"
        ),
    }


def write_csv(matrix: dict[str, Any], path: pathlib.Path) -> None:
    rows: list[dict[str, Any]] = []
    claim_by_variant = {
        str(item.get("variant", "")): item
        for item in matrix.get("claim_audit", {}).get("variant_claims", [])
        if isinstance(item, dict)
    }
    default_total = 0.0
    for variant in matrix["variants"]:
        if variant["name"] == "default_taskset":
            default_total = float(variant["aggregate"].get("total_mean_sec", 0.0))
            break

    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        claim = claim_by_variant.get(str(variant["name"]), {})
        stages = agg["stage_summary"]
        total = agg.get("total_summary", {})
        flow = agg["flowmon_mean"]
        victim = agg["victim_mean"]
        ctl = agg["controller"]
        char = agg.get("characterization_mean", {}) if isinstance(agg.get("characterization_mean"), dict) else {}
        total_mean = float(total.get("mean_sec", agg.get("total_mean_sec", 0.0)))
        total_delta_pct = ((total_mean / default_total) - 1.0) * 100.0 if default_total else 0.0
        rows.append(
            {
                "variant": variant["name"],
                "kind": variant["kind"],
                "frontier_width": variant.get("frontier_width", ""),
                "max_frontier_width": variant.get("max_frontier_width", ""),
                "adaptive_frontier_width": bool(variant.get("adaptive_frontier_width", False)),
                "adaptive_max_frontier_width": variant.get("adaptive_max_frontier_width", ""),
                "frontier_dispatch_quota": variant.get("frontier_dispatch_quota", ""),
                "guard_cooldown_dispatches": variant.get("guard_cooldown_dispatches", ""),
                "pressure_fallback_enqueues": variant.get("pressure_fallback_enqueues", ""),
                "pressure_fallback_ms": variant.get("pressure_fallback_ms", ""),
                "shared_wait_pressure_us": variant.get("shared_wait_pressure_us", ""),
                "pressure_dispatch_ms": variant.get("pressure_dispatch_ms", ""),
                "frontier_dispatch_window_ms": variant.get("frontier_dispatch_window_ms", ""),
                "frontier_dispatch_window_budget": variant.get("frontier_dispatch_window_budget", ""),
                "policy_fallback": bool(variant.get("policy_fallback", False)),
                "trials": len(variant["trials"]),
                "build_mean_sec": stages.get("BUILD", {}).get("mean_sec", 0.0),
                "build_std_sec": stages.get("BUILD", {}).get("std_sec", 0.0),
                "test_mean_sec": stages.get("TEST", {}).get("mean_sec", 0.0),
                "test_std_sec": stages.get("TEST", {}).get("std_sec", 0.0),
                "package_mean_sec": stages.get("PACKAGE", {}).get("mean_sec", 0.0),
                "total_mean_sec": total_mean,
                "total_std_sec": total.get("std_sec", 0.0),
                "total_min_sec": total.get("min_sec", 0.0),
                "total_max_sec": total.get("max_sec", 0.0),
                "total_delta_vs_default_pct": total_delta_pct,
                "controller_frontier_records": ctl.get("frontier_records", 0),
                "controller_selected_samples": ctl.get("selected_samples", 0),
                "controller_propagation_anchors": ctl.get("propagation_anchors", 0),
                "controller_stability_suppressed": ctl.get("stability_suppressed", 0),
                "controller_stability_observed_unstable": ctl.get("stability_observed_unstable", 0),
                "controller_stability_held": ctl.get("stability_held", 0),
                "controller_adaptive_records": ctl.get("adaptive_records", 0),
                "controller_adaptive_enabled_records": ctl.get("adaptive_enabled_records", 0),
                "controller_adaptive_widened_records": ctl.get("adaptive_widened_records", 0),
                "controller_adaptive_max_effective_width": ctl.get("adaptive_max_effective_width", 0),
                "controller_adaptive_width_counts": json.dumps(
                    ctl.get("adaptive_width_counts", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "controller_adaptive_reason_counts": json.dumps(
                    ctl.get("adaptive_reason_counts", {}),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "frontier_enqueues_mean": flow.get("frontier_enqueues", 0.0),
                "fallback_enqueues_mean": flow.get("fallback_enqueues", 0.0),
                "width_held_enqueues_mean": flow.get("width_held_enqueues", 0.0),
                "global_frontier_dispatches_mean": flow.get("global_frontier_dispatches", 0.0),
                "global_shared_dispatches_mean": flow.get("global_shared_dispatches", 0.0),
                "global_shared_first_dispatches_mean": flow.get("global_shared_first_dispatches", 0.0),
                "global_frontier_guard_trips_mean": flow.get("global_frontier_guard_trips", 0.0),
                "global_frontier_guard_misses_mean": flow.get("global_frontier_guard_misses", 0.0),
                "global_shared_cooldown_entries_mean": flow.get("global_shared_cooldown_entries", 0.0),
                "global_shared_cooldown_dispatches_mean": flow.get("global_shared_cooldown_dispatches", 0.0),
                "global_shared_cooldown_misses_mean": flow.get("global_shared_cooldown_misses", 0.0),
                "global_pressure_fallback_entries_mean": flow.get("global_pressure_fallback_entries", 0.0),
                "global_pressure_fallback_enqueues_mean": flow.get("global_pressure_fallback_enqueues", 0.0),
                "global_shared_wait_pressure_events_mean": flow.get("global_shared_wait_pressure_events", 0.0),
                "global_shared_wait_pressure_wait_ms_mean": flow.get("global_shared_wait_pressure_wait_ns", 0.0) / 1_000_000.0,
                "global_pressure_dispatch_entries_mean": flow.get("global_pressure_dispatch_entries", 0.0),
                "global_pressure_dispatches_mean": flow.get("global_pressure_dispatches", 0.0),
                "global_pressure_dispatch_misses_mean": flow.get("global_pressure_dispatch_misses", 0.0),
                "global_frontier_budget_windows_mean": flow.get("global_frontier_budget_windows", 0.0),
                "global_frontier_budget_trips_mean": flow.get("global_frontier_budget_trips", 0.0),
                "global_frontier_budget_shared_mean": flow.get("global_frontier_budget_shared", 0.0),
                "global_frontier_budget_misses_mean": flow.get("global_frontier_budget_misses", 0.0),
                "frontier_ratio_mean": flow.get("frontier_ratio", 0.0),
                "fallback_ratio_mean": flow.get("fallback_ratio", 0.0),
                "final_window_budget_used_ms_mean": flow.get("budget_used_ns", 0.0) / 1_000_000.0,
                "final_window_budget_total_ms_mean": flow.get("budget_total_ns", 0.0) / 1_000_000.0,
                "final_window_budget_used_ratio_mean": flow.get("budget_used_ratio", 0.0),
                "char_samples_mean": char.get("sample_count", 0.0),
                "char_frontier_samples_mean": char.get("samples_with_frontier", 0.0),
                "char_schedstat_samples_mean": char.get("samples_with_schedstat", 0.0),
                "char_frontier_wait_ms_mean": char.get("frontier_sched_wait_delta_ns", 0.0) / 1_000_000.0,
                "char_total_wait_ms_mean": char.get("sched_wait_delta_ns", 0.0) / 1_000_000.0,
                "char_frontier_runtime_ms_mean": char.get("frontier_sched_runtime_delta_ns", 0.0) / 1_000_000.0,
                "char_total_runtime_ms_mean": char.get("sched_runtime_delta_ns", 0.0) / 1_000_000.0,
                "char_frontier_wait_share_mean": char.get("frontier_wait_share", 0.0),
                "char_frontier_wait_per_frontier_sample_ms_mean": char.get(
                    "frontier_wait_per_frontier_sample_ns",
                    0.0,
                )
                / 1_000_000.0,
                "char_frontier_wait_per_schedstat_sample_ms_mean": char.get(
                    "frontier_wait_per_schedstat_sample_ns",
                    0.0,
                )
                / 1_000_000.0,
                "char_frontier_wait_per_elapsed_sec_ms_per_s_mean": char.get(
                    "frontier_wait_per_elapsed_sec",
                    0.0,
                )
                / 1_000_000.0,
                "victim_lateness_p95_ms_mean": victim.get("lateness_p95_ms", 0.0),
                "victim_lateness_p99_ms_mean": victim.get("lateness_p99_ms", 0.0),
                "victim_lateness_max_ms_mean": victim.get("lateness_max_ms", 0.0),
                "claim_level": claim.get("highest_level", ""),
                "claim_blockers": "; ".join(claim.get("blockers_for_next_level", []))
                if isinstance(claim.get("blockers_for_next_level"), list)
                else "",
            }
        )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(matrix: dict[str, Any], path: pathlib.Path) -> None:
    config = matrix.get("config", {})
    claim_audit = matrix.get("claim_audit", {}) if isinstance(matrix.get("claim_audit"), dict) else {}
    claim_gate_audit = (
        matrix.get("claim_gate_audit", {}) if isinstance(matrix.get("claim_gate_audit"), dict) else {}
    )
    default_total = 0.0
    for variant in matrix["variants"]:
        if variant["name"] == "default_taskset":
            default_total = float(variant["aggregate"].get("total_mean_sec", 0.0))
            break

    lines = [
        "# FlowCausal build/test sandbox pilot matrix",
        "",
        "This is a pilot mechanism matrix on a generated build/test process tree.",
        "It is not a paper-grade performance result and must not be cited as a",
        "speedup claim without repeated trials and stronger baselines.",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(config, indent=2, sort_keys=True),
        "```",
        "",
        "| Variant | Frontier width | Adaptive width | Dispatch quota | Cooldown | Pressure fb | Dispatch win/budget | Policy fallback | Trials | BUILD mean±std s | TEST mean±std s | Total mean±std s | Δtotal vs default | Frontier records | Propagation anchors | Stability suppressed | Stability observed | Frontier enq mean | Fallback enq mean | Width-held mean | Guard trips/shared-first | Cooldown disp/miss | Pressure fb ent/enq | Shared wait pressure ev/wait ms | Pressure dispatch ent/shared/miss | Dispatch budget trip/shared/miss | Boost budget used/total | Victim p95/p99/max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        stages = agg["stage_summary"]
        total = agg.get("total_summary", {})
        flow = agg["flowmon_mean"]
        victim = agg["victim_mean"]
        ctl = agg["controller"]
        total_mean = float(total.get("mean_sec", agg.get("total_mean_sec", 0.0)))
        total_delta_pct = ((total_mean / default_total) - 1.0) * 100.0 if default_total else 0.0
        budget_used_ms = float(flow.get("budget_used_ns", 0.0)) / 1_000_000.0
        budget_total_ms = float(flow.get("budget_total_ns", 0.0)) / 1_000_000.0
        budget_cell = "-" if budget_total_ms <= 0.0 else f"{budget_used_ms:.1f}/{budget_total_ms:.1f} ms"
        guard_cell = "{trips:.1f}/{shared_first:.1f}".format(
            trips=float(flow.get("global_frontier_guard_trips", 0.0)),
            shared_first=float(flow.get("global_shared_first_dispatches", 0.0)),
        )
        cooldown_cell = "{dispatches:.1f}/{misses:.1f}".format(
            dispatches=float(flow.get("global_shared_cooldown_dispatches", 0.0)),
            misses=float(flow.get("global_shared_cooldown_misses", 0.0)),
        )
        pressure_cell = "{entries:.1f}/{enqueues:.1f}".format(
            entries=float(flow.get("global_pressure_fallback_entries", 0.0)),
            enqueues=float(flow.get("global_pressure_fallback_enqueues", 0.0)),
        )
        shared_wait_cell = "{events:.1f}/{wait_ms:.3f}".format(
            events=float(flow.get("global_shared_wait_pressure_events", 0.0)),
            wait_ms=float(flow.get("global_shared_wait_pressure_wait_ns", 0.0)) / 1_000_000.0,
        )
        pressure_dispatch_cell = "{entries:.1f}/{dispatches:.1f}/{misses:.1f}".format(
            entries=float(flow.get("global_pressure_dispatch_entries", 0.0)),
            dispatches=float(flow.get("global_pressure_dispatches", 0.0)),
            misses=float(flow.get("global_pressure_dispatch_misses", 0.0)),
        )
        pressure_fb_config = "-"
        pressure_fb_enqueues = variant.get("pressure_fallback_enqueues")
        pressure_fb_ms = variant.get("pressure_fallback_ms")
        if pressure_fb_enqueues is not None or pressure_fb_ms is not None:
            pressure_fb_enqueues_i = int(pressure_fb_enqueues or 0)
            pressure_fb_ms_i = int(pressure_fb_ms or 0)
            if pressure_fb_ms_i > 0:
                pressure_fb_config = f"{pressure_fb_enqueues_i}/{pressure_fb_ms_i} ms"
            else:
                pressure_fb_config = str(pressure_fb_enqueues_i)
        dispatch_window_ms = variant.get("frontier_dispatch_window_ms")
        dispatch_window_budget = variant.get("frontier_dispatch_window_budget")
        dispatch_budget_config = "-"
        if dispatch_window_ms is not None and dispatch_window_budget is not None:
            dispatch_window_ms_f = float(dispatch_window_ms or 0)
            dispatch_window_budget_i = int(dispatch_window_budget or 0)
            if dispatch_window_ms_f > 0 and dispatch_window_budget_i > 0:
                dispatch_budget_config = f"{dispatch_window_ms_f:g} ms/{dispatch_window_budget_i}"
        dispatch_budget_cell = "{trips:.1f}/{shared:.1f}/{misses:.1f}".format(
            trips=float(flow.get("global_frontier_budget_trips", 0.0)),
            shared=float(flow.get("global_frontier_budget_shared", 0.0)),
            misses=float(flow.get("global_frontier_budget_misses", 0.0)),
        )
        frontier_width_cell = "-"
        if variant.get("kind") == "flowcausal":
            frontier_width_cell = "{width}/{max_width}".format(
                width=variant.get("frontier_width", "-"),
                max_width=variant.get("max_frontier_width", "-"),
            )
        adaptive_cell = "-"
        if bool(variant.get("adaptive_frontier_width", False)):
            adaptive_cell = "on/{records}/{widened}/{maxw}".format(
                records=int(ctl.get("adaptive_records", 0)),
                widened=int(ctl.get("adaptive_widened_records", 0)),
                maxw=int(ctl.get("adaptive_max_effective_width", 0)),
            )
        victim_cell = "-"
        if float(victim.get("iterations", 0.0)) > 0:
            victim_cell = "{p95:.3f}/{p99:.3f}/{maxv:.3f}".format(
                p95=float(victim.get("lateness_p95_ms", 0.0)),
                p99=float(victim.get("lateness_p99_ms", 0.0)),
                maxv=float(victim.get("lateness_max_ms", 0.0)),
            )
        lines.append(
            "| {name} | {frontier_width_cell} | {adaptive_cell} | {quota} | {cooldown} | {pressure_fb} | {dispatch_budget_config} | {policy_fallback} | {trials} | {build:.3f}±{build_std:.3f} | {test:.3f}±{test_std:.3f} | {total:.3f}±{total_std:.3f} | {delta:+.1f}% | {frontier_records} | {propagation_anchors} | {stability_suppressed} | {stability_observed} | {frontier:.1f} | {fallback:.1f} | {width:.1f} | {guard} | {cooldown_cell} | {pressure_cell} | {shared_wait_cell} | {pressure_dispatch_cell} | {dispatch_budget_cell} | {budget} | {victim} |".format(
                name=variant["name"],
                frontier_width_cell=frontier_width_cell,
                adaptive_cell=adaptive_cell,
                quota="-" if variant.get("frontier_dispatch_quota") is None else variant.get("frontier_dispatch_quota"),
                cooldown="-" if variant.get("guard_cooldown_dispatches") is None else variant.get("guard_cooldown_dispatches"),
                pressure_fb=pressure_fb_config,
                dispatch_budget_config=dispatch_budget_config,
                policy_fallback="yes" if bool(variant.get("policy_fallback", False)) else "no",
                trials=len(variant["trials"]),
                build=float(stages.get("BUILD", {}).get("mean_sec", 0.0)),
                build_std=float(stages.get("BUILD", {}).get("std_sec", 0.0)),
                test=float(stages.get("TEST", {}).get("mean_sec", 0.0)),
                test_std=float(stages.get("TEST", {}).get("std_sec", 0.0)),
                total=total_mean,
                total_std=float(total.get("std_sec", 0.0)),
                delta=total_delta_pct,
                frontier_records=int(ctl.get("frontier_records", 0)),
                propagation_anchors=int(ctl.get("propagation_anchors", 0)),
                stability_suppressed=int(ctl.get("stability_suppressed", 0)),
                stability_observed=int(ctl.get("stability_observed_unstable", 0)),
                frontier=float(flow.get("frontier_enqueues", 0.0)),
                fallback=float(flow.get("fallback_enqueues", 0.0)),
                width=float(flow.get("width_held_enqueues", 0.0)),
                guard=guard_cell,
                cooldown_cell=cooldown_cell,
                pressure_cell=pressure_cell,
                shared_wait_cell=shared_wait_cell,
                pressure_dispatch_cell=pressure_dispatch_cell,
                dispatch_budget_cell=dispatch_budget_cell,
                budget=budget_cell,
                victim=victim_cell,
            )
        )
    lines.extend(
        [
            "",
            "## Direct schedstat characterization",
            "",
            "These counters come from flowctl-ranked process-tree sampling and `/proc/<pid>/schedstat`.",
            "They are lower-bound mechanism evidence, not by themselves a speedup claim.",
            "",
            "| Variant | Samples | Frontier samples | Schedstat samples | Frontier wait ms | Wait/frontier sample ms | Wait rate ms/s | Total wait ms | Frontier runtime ms | Total runtime ms | Frontier wait share |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        char = agg.get("characterization_mean", {}) if isinstance(agg.get("characterization_mean"), dict) else {}
        lines.append(
            "| {name} | {samples:.1f} | {frontier_samples:.1f} | {schedstat_samples:.1f} | {frontier_wait_ms:.3f} | {frontier_wait_per_sample_ms:.3f} | {frontier_wait_rate_ms:.3f} | {total_wait_ms:.3f} | {frontier_runtime_ms:.3f} | {total_runtime_ms:.3f} | {frontier_wait_share:.3f} |".format(
                name=variant["name"],
                samples=float(char.get("sample_count", 0.0)),
                frontier_samples=float(char.get("samples_with_frontier", 0.0)),
                schedstat_samples=float(char.get("samples_with_schedstat", 0.0)),
                frontier_wait_ms=float(char.get("frontier_sched_wait_delta_ns", 0.0)) / 1_000_000.0,
                frontier_wait_per_sample_ms=float(
                    char.get("frontier_wait_per_frontier_sample_ns", 0.0)
                )
                / 1_000_000.0,
                frontier_wait_rate_ms=float(char.get("frontier_wait_per_elapsed_sec", 0.0)) / 1_000_000.0,
                total_wait_ms=float(char.get("sched_wait_delta_ns", 0.0)) / 1_000_000.0,
                frontier_runtime_ms=float(char.get("frontier_sched_runtime_delta_ns", 0.0)) / 1_000_000.0,
                total_runtime_ms=float(char.get("sched_runtime_delta_ns", 0.0)) / 1_000_000.0,
                frontier_wait_share=float(char.get("frontier_wait_share", 0.0)),
            )
        )
    lines.extend(
        [
            "",
            "## Raw outputs",
            "",
        ]
    )
    for variant in matrix["variants"]:
        lines.append(f"- `{variant['name']}/`: raw logs and per-trial manifests.")
    lines.extend(
        [
            "",
            "## Claim audit",
            "",
            f"- Highest supported claim level: `{claim_audit.get('highest_supported_claim_level', 'unknown')}`.",
            f"- Summary: {claim_audit.get('summary', 'No audit summary available.')}",
            "",
            "Permitted claims:",
            "",
        ]
    )
    for claim in claim_audit.get("permitted_claims", []):
        lines.append(f"- {claim}")
    lines.extend(["", "Forbidden claims:", ""])
    for claim in claim_audit.get("forbidden_claims", []):
        lines.append(f"- {claim}")
    lines.extend(["", "Paper-claim gaps:", ""])
    for gap in claim_audit.get("paper_claim_gaps", []):
        lines.append(f"- {gap}")
    lines.extend(
        [
            "",
            "Per-variant audit:",
            "",
            "| Variant | Highest level | Evidence | Blockers for next level |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in claim_audit.get("variant_claims", []):
        if not isinstance(item, dict):
            continue
        evidence = "; ".join(str(x) for x in item.get("evidence", [])) or "-"
        blockers = "; ".join(str(x) for x in item.get("blockers_for_next_level", [])) or "-"
        lines.append(
            f"| {item.get('variant', '')} | {item.get('highest_level', '')} | {evidence} | {blockers} |"
        )

    lines.extend(
        [
            "",
            "## Claim-gate audit",
            "",
            f"- Gate highest supported level: `{claim_gate_audit.get('highest_supported_level', 'unknown')}`.",
            "",
            "| Gate | Status | Evidence | Blockers |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in claim_gate_audit.get("gates", []):
        if not isinstance(item, dict):
            continue
        evidence = "<br>".join(str(x) for x in item.get("evidence", [])) or "-"
        blockers = "<br>".join(str(x) for x in item.get("blockers", [])) or "-"
        lines.append(f"| {item.get('name', '')} | {item.get('status', '')} | {evidence} | {blockers} |")

    lines.extend(
        [
            "",
            "## Interpretation discipline",
            "",
            "- Default/taskset-only rows provide stage-time reference points but no BPF mechanism counters.",
            "- FlowCausal rows are meaningful only if `controller.ndjson` shows plausible frontier selection and `flowmon-final.log` shows BPF admission.",
            "- `Adaptive width` is reported as `on/records/widened/max_width`; it proves only that controller-side width selection was audited.",
            "- `Propagation anchors` counts wait-chain ancestors marked for fork-time hint inheritance; it is evidence of mechanism activation, not a speedup claim.",
            "- `Stability suppressed` counts online candidates delayed by the optional debounce/hold gate; `Stability observed` counts delayed selected frontiers kept as observe/fallback hints.",
            "- If a victim sidecar is enabled, `victim.log` reports periodic release lateness; compare p95/p99/max across all variants before making safety claims.",
            "- A performance claim requires repeated runs, direct mechanism improvement, and victim/fairness checks.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run FlowCausal build/test sandbox pilot matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=str(ROOT / "experiments" / "results" / "buildtest-matrix-smoke"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-live", action="store_true", help="run only the default/taskset-only variant")
    parser.add_argument("--no-build-tools", action="store_true", help="skip rebuilding scx_flowcausal and flowmon")
    parser.add_argument("--taskset", default="0-1", help="CPU list for workload runs")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--files", type=int, default=32)
    parser.add_argument("--statements", type=int, default=240)
    parser.add_argument("--jobs", type=int, default=4)
    parser.add_argument("--runtime-spin", type=int, default=1024)
    parser.add_argument("--test-workers", type=int, default=4)
    parser.add_argument("--test-iters", type=int, default=20000)
    parser.add_argument("--pre-sleep-ms", type=int, default=200)
    parser.add_argument("--post-sleep-ms", type=int, default=200)
    parser.add_argument("--package", action="store_true")
    parser.add_argument("--frontier-width", type=int, default=2)
    parser.add_argument("--adaptive-frontier-width", action="store_true")
    parser.add_argument("--adaptive-max-frontier-width", type=int, default=8)
    parser.add_argument("--min-frontier-score", type=int, default=50)
    parser.add_argument("--frontier-propagation", action="store_true")
    parser.add_argument("--frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--frontier-stability-samples", type=int, default=1)
    parser.add_argument("--frontier-hold-ms", type=int, default=0)
    parser.add_argument("--frontier-bypass-score", type=int, default=140)
    parser.add_argument("--frontier-selected-stability", action="store_true")
    parser.add_argument("--frontier-stability-action", choices=("suppress", "observe"), default="suppress")
    parser.add_argument("--build-frontier-admission", choices=("advance", "tail-only", "observe"), default="advance")
    parser.add_argument(
        "--policy-fallback",
        action="store_true",
        help="apply explicit policy fallback to every live FlowCausal variant",
    )
    parser.add_argument(
        "--include-stability-variant",
        action="store_true",
        help="add a separate flowcausal_width_stability variant for online-inference debounce/hold ablation",
    )
    parser.add_argument("--stability-variant-samples", type=int, default=2)
    parser.add_argument("--stability-variant-hold-ms", type=int, default=150)
    parser.add_argument("--stability-variant-bypass-score", type=int, default=140)
    parser.add_argument("--stability-variant-action", choices=("suppress", "observe"), default="suppress")
    parser.add_argument(
        "--include-propagation-variant",
        action="store_true",
        help="add a flowcausal_width_stability_propagation variant for fork-time hint propagation",
    )
    parser.add_argument("--propagation-variant-depth", type=int, default=2)
    parser.add_argument(
        "--include-policy-fallback-variant",
        action="store_true",
        help="add flowcausal_width_policy_fallback as an explicit safety-valve connectivity variant",
    )
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument(
        "--include-frontier-width-variants",
        action="store_true",
        help="add explicit flowcausal_width<N> variants with paired --frontier-width/--max-frontier-width values",
    )
    parser.add_argument(
        "--frontier-width-values",
        default="2,8",
        help="comma-separated static frontier widths used with --include-frontier-width-variants",
    )
    parser.add_argument(
        "--include-adaptive-width-variant",
        action="store_true",
        help="add flowcausal_width_adaptive<CAP> to compare controller-side adaptive cohort width against static widths",
    )
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--ttl-ms", type=int, default=1000)
    parser.add_argument("--confidence", type=int, default=75)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=10)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument(
        "--collect-characterization",
        action="store_true",
        help=(
            "collect flowctl-ranked process-tree and /proc/<pid>/schedstat "
            "deltas for both default and live FlowCausal rows"
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
        help="ranked frontier candidates retained in characterization samples",
    )
    parser.add_argument(
        "--frontier-dispatch-quota",
        type=int,
        default=4,
        help="scheduler shared-first guard quota; 0 disables",
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
        help="after shared pressure is observed, try shared before frontier dispatch for up to this many milliseconds; 0 disables",
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
        "--include-dispatch-guard-variants",
        action="store_true",
        help="add explicit flowcausal_width_guard<N> variants for dispatch-guard quota ablation",
    )
    parser.add_argument(
        "--dispatch-guard-quotas",
        default="0,1,4",
        help="comma-separated quotas used with --include-dispatch-guard-variants",
    )
    parser.add_argument(
        "--include-guard-cooldown-variants",
        action="store_true",
        help="add explicit flowcausal_width_guard<Q>_cooldown<N> variants for shared-first cooldown ablation",
    )
    parser.add_argument(
        "--guard-cooldown-values",
        default="0,2,4",
        help="comma-separated cooldown values used with --include-guard-cooldown-variants",
    )
    parser.add_argument(
        "--include-pressure-fallback-variants",
        action="store_true",
        help="add flowcausal_width_guard<Q>_pressure<N> variants for shared-pressure admission-fallback ablation",
    )
    parser.add_argument(
        "--pressure-fallback-values",
        default="0,2,4",
        help="comma-separated pressure fallback enqueue budgets used with --include-pressure-fallback-variants",
    )
    parser.add_argument(
        "--include-dispatch-budget-variants",
        action="store_true",
        help="add flowcausal_width_dbudget<N> variants for frontier dispatch-window budget ablation",
    )
    parser.add_argument(
        "--dispatch-budget-values",
        default="1,2,4",
        help="comma-separated frontier dispatch budgets used with --include-dispatch-budget-variants",
    )
    parser.add_argument("--victim-command", default=None, help="optional shell-style victim sidecar command")
    parser.add_argument("--victim-taskset", default=None, help="optional CPU list for the victim command")
    parser.add_argument("--victim-warmup-sec", type=float, default=0.2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.trials < 1:
        raise SystemExit("--trials must be positive")
    dispatch_guard_quotas = parse_int_csv(
        args.dispatch_guard_quotas,
        name="--dispatch-guard-quotas",
    )
    guard_cooldown_values = parse_int_csv(
        args.guard_cooldown_values,
        name="--guard-cooldown-values",
    )
    pressure_fallback_values = parse_int_csv(
        args.pressure_fallback_values,
        name="--pressure-fallback-values",
    )
    dispatch_budget_values = parse_int_csv(
        args.dispatch_budget_values,
        name="--dispatch-budget-values",
    )
    frontier_width_values = parse_int_csv(
        args.frontier_width_values,
        name="--frontier-width-values",
    )

    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists():
        if not args.overwrite:
            raise FileExistsError(f"{out_dir} exists; pass --overwrite to replace generated matrix outputs")
        remove_generated_tree(out_dir)
    out_dir.mkdir(parents=True)

    if not args.no_build_tools and not args.skip_live:
        build_tools()

    variants: list[dict[str, Any]] = []
    variant_specs: list[dict[str, Any]] = [
        {"name": "default_taskset", "kind": "default", "width_control": False}
    ]
    if not args.skip_live:
        variant_specs.extend(
            [
                {"name": "flowcausal_no_width", "kind": "flowcausal", "width_control": False},
                {"name": "flowcausal_width", "kind": "flowcausal", "width_control": True},
            ]
        )
        if args.include_frontier_width_variants:
            for width in frontier_width_values:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width{width}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_width": max(1, width),
                        "max_frontier_width": max(1, width),
                    }
                )
        if args.include_adaptive_width_variant:
            adaptive_cap = max(1, args.adaptive_max_frontier_width)
            variant_specs.append(
                {
                    "name": f"flowcausal_width_adaptive{adaptive_cap}",
                    "kind": "flowcausal",
                    "width_control": True,
                    "frontier_width": max(1, args.frontier_width),
                    "max_frontier_width": max(1, args.frontier_width),
                    "adaptive_frontier_width": True,
                    "adaptive_max_frontier_width": adaptive_cap,
                }
            )
        if args.include_stability_variant:
            variant_specs.append(
                {
                    "name": "flowcausal_width_stability",
                    "kind": "flowcausal",
                    "width_control": True,
                    "stability": {
                        "frontier_stability_samples": max(1, args.stability_variant_samples),
                        "frontier_hold_ms": max(0, args.stability_variant_hold_ms),
                        "frontier_bypass_score": max(0, args.stability_variant_bypass_score),
                        "frontier_stability_action": args.stability_variant_action,
                    },
                }
            )
        if args.include_propagation_variant:
            variant_specs.append(
                {
                    "name": "flowcausal_width_stability_propagation",
                    "kind": "flowcausal",
                    "width_control": True,
                    "frontier_propagation": True,
                    "frontier_propagation_depth": max(0, args.propagation_variant_depth),
                    "stability": {
                        "frontier_stability_samples": max(1, args.stability_variant_samples),
                        "frontier_hold_ms": max(0, args.stability_variant_hold_ms),
                        "frontier_bypass_score": max(0, args.stability_variant_bypass_score),
                        "frontier_stability_action": args.stability_variant_action,
                    },
                }
            )
        if args.include_policy_fallback_variant:
            variant_specs.append(
                {
                    "name": "flowcausal_width_policy_fallback",
                    "kind": "flowcausal",
                    "width_control": True,
                    "policy_fallback": True,
                }
            )
        if args.include_dispatch_guard_variants:
            for quota in dispatch_guard_quotas:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width_guard{quota}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_dispatch_quota": quota,
                    }
                )
        if args.include_guard_cooldown_variants:
            base_quota = max(0, args.frontier_dispatch_quota)
            for cooldown in guard_cooldown_values:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width_guard{base_quota}_cooldown{cooldown}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_dispatch_quota": base_quota,
                        "guard_cooldown_dispatches": cooldown,
                    }
                )
        if args.include_pressure_fallback_variants:
            base_quota = max(1, args.frontier_dispatch_quota)
            for pressure in pressure_fallback_values:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width_guard{base_quota}_pressure{pressure}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_dispatch_quota": base_quota,
                        "pressure_fallback_enqueues": pressure,
                        "pressure_fallback_ms": args.pressure_fallback_ms,
                    }
                )
        if args.include_dispatch_budget_variants:
            window_ms = max(1, args.frontier_dispatch_window_ms)
            for budget in dispatch_budget_values:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width_dbudget{budget}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_dispatch_window_ms": window_ms,
                        "frontier_dispatch_window_budget": budget,
                    }
                )

    for spec in variant_specs:
        name = str(spec["name"])
        kind = str(spec["kind"])
        width_control = bool(spec["width_control"])
        variant_frontier_width = int(spec.get("frontier_width", args.frontier_width))
        variant_max_frontier_width = int(spec.get("max_frontier_width", args.max_frontier_width))
        adaptive_width = bool(spec.get("adaptive_frontier_width", args.adaptive_frontier_width))
        adaptive_width_cap = int(
            spec.get("adaptive_max_frontier_width", args.adaptive_max_frontier_width)
        )
        propagation = bool(spec.get("frontier_propagation", args.frontier_propagation))
        propagation_depth = int(
            spec.get("frontier_propagation_depth", args.frontier_propagation_depth)
        )
        stability = spec.get("stability") if isinstance(spec.get("stability"), dict) else None
        dispatch_quota = int(spec.get("frontier_dispatch_quota", args.frontier_dispatch_quota))
        cooldown_dispatches = int(
            spec.get("guard_cooldown_dispatches", args.guard_cooldown_dispatches)
        )
        pressure_fallback = int(
            spec.get("pressure_fallback_enqueues", args.pressure_fallback_enqueues)
        )
        pressure_ms = int(spec.get("pressure_fallback_ms", args.pressure_fallback_ms))
        dispatch_window_ms = int(
            spec.get("frontier_dispatch_window_ms", args.frontier_dispatch_window_ms)
        )
        dispatch_window_budget = int(
            spec.get("frontier_dispatch_window_budget", args.frontier_dispatch_window_budget)
        )
        policy_fallback = bool(spec.get("policy_fallback", args.policy_fallback))
        trials: list[dict[str, Any]] = []
        for trial_idx in range(args.trials):
            trial_dir = out_dir / name / f"trial-{trial_idx:02d}"
            if kind == "default":
                trial = run_default_trial(args, trial_dir)
            else:
                trial = run_live_trial(
                    args,
                    trial_dir,
                    width_control=width_control,
                    frontier_width=variant_frontier_width,
                    max_frontier_width=variant_max_frontier_width,
                    adaptive_frontier_width=adaptive_width,
                    adaptive_max_frontier_width=adaptive_width_cap,
                    frontier_propagation=propagation,
                    frontier_propagation_depth=propagation_depth,
                    frontier_stability_samples=(
                        int(stability["frontier_stability_samples"]) if stability else None
                    ),
                    frontier_hold_ms=int(stability["frontier_hold_ms"]) if stability else None,
                    frontier_bypass_score=(
                        int(stability["frontier_bypass_score"]) if stability else None
                    ),
                    frontier_selected_stability=(
                        bool(stability.get("frontier_selected_stability", False))
                        if stability
                        else None
                    ),
                    frontier_stability_action=(
                        str(stability.get("frontier_stability_action", args.frontier_stability_action))
                        if stability
                        else None
                    ),
                    frontier_dispatch_quota=dispatch_quota,
                    guard_cooldown_dispatches=cooldown_dispatches,
                    pressure_fallback_enqueues=pressure_fallback,
                    pressure_fallback_ms=pressure_ms,
                    frontier_dispatch_window_ms=dispatch_window_ms,
                    frontier_dispatch_window_budget=dispatch_window_budget,
                    policy_fallback=policy_fallback,
                )
            trial["trial"] = trial_idx
            trials.append(trial)
        variants.append(
            {
                "name": name,
                "kind": kind,
                "width_control": width_control,
                "frontier_width": variant_frontier_width if kind == "flowcausal" else None,
                "max_frontier_width": variant_max_frontier_width if kind == "flowcausal" else None,
                "adaptive_frontier_width": adaptive_width if kind == "flowcausal" else False,
                "adaptive_max_frontier_width": adaptive_width_cap if kind == "flowcausal" and adaptive_width else None,
                "frontier_propagation": propagation,
                "frontier_propagation_depth": propagation_depth,
                "frontier_dispatch_quota": dispatch_quota if kind == "flowcausal" else None,
                "guard_cooldown_dispatches": cooldown_dispatches if kind == "flowcausal" else None,
                "pressure_fallback_enqueues": pressure_fallback if kind == "flowcausal" else None,
                "pressure_fallback_ms": pressure_ms if kind == "flowcausal" else None,
                "shared_wait_pressure_us": args.shared_wait_pressure_us if kind == "flowcausal" else None,
                "pressure_dispatch_ms": args.pressure_dispatch_ms if kind == "flowcausal" else None,
                "frontier_dispatch_window_ms": dispatch_window_ms if kind == "flowcausal" else None,
                "frontier_dispatch_window_budget": dispatch_window_budget if kind == "flowcausal" else None,
                "policy_fallback": policy_fallback if kind == "flowcausal" else False,
                "stability": stability or {
                    "frontier_stability_samples": args.frontier_stability_samples,
                    "frontier_hold_ms": args.frontier_hold_ms,
                    "frontier_bypass_score": args.frontier_bypass_score,
                    "frontier_selected_stability": bool(args.frontier_selected_stability),
                    "frontier_stability_action": args.frontier_stability_action,
                },
                "trials": trials,
                "aggregate": aggregate_trials(trials),
            }
        )

    matrix = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "config": {
            "taskset": args.taskset,
            "trials": args.trials,
            "files": args.files,
            "statements": args.statements,
            "jobs": args.jobs,
            "runtime_spin": args.runtime_spin,
            "test_workers": args.test_workers,
            "test_iters": args.test_iters,
            "package": bool(args.package),
            "frontier_width": args.frontier_width,
            "adaptive_frontier_width": bool(args.adaptive_frontier_width),
            "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
            "min_frontier_score": args.min_frontier_score,
            "frontier_propagation": bool(args.frontier_propagation),
            "frontier_propagation_depth": args.frontier_propagation_depth,
            "frontier_stability_samples": args.frontier_stability_samples,
            "frontier_hold_ms": args.frontier_hold_ms,
            "frontier_bypass_score": args.frontier_bypass_score,
            "frontier_selected_stability": bool(args.frontier_selected_stability),
            "frontier_stability_action": args.frontier_stability_action,
            "policy_fallback": bool(args.policy_fallback),
            "include_stability_variant": bool(args.include_stability_variant),
            "include_propagation_variant": bool(args.include_propagation_variant),
            "include_policy_fallback_variant": bool(args.include_policy_fallback_variant),
            "include_dispatch_guard_variants": bool(args.include_dispatch_guard_variants),
            "include_guard_cooldown_variants": bool(args.include_guard_cooldown_variants),
            "include_pressure_fallback_variants": bool(args.include_pressure_fallback_variants),
            "include_dispatch_budget_variants": bool(args.include_dispatch_budget_variants),
            "include_frontier_width_variants": bool(args.include_frontier_width_variants),
            "include_adaptive_width_variant": bool(args.include_adaptive_width_variant),
            "frontier_width_values": frontier_width_values,
            "dispatch_guard_quotas": dispatch_guard_quotas,
            "guard_cooldown_dispatches": args.guard_cooldown_dispatches,
            "guard_cooldown_values": guard_cooldown_values,
            "pressure_fallback_enqueues": args.pressure_fallback_enqueues,
            "pressure_fallback_ms": args.pressure_fallback_ms,
            "shared_wait_pressure_us": args.shared_wait_pressure_us,
            "pressure_dispatch_ms": args.pressure_dispatch_ms,
            "pressure_fallback_values": pressure_fallback_values,
            "frontier_dispatch_window_ms": args.frontier_dispatch_window_ms,
            "frontier_dispatch_window_budget": args.frontier_dispatch_window_budget,
            "dispatch_budget_values": dispatch_budget_values,
            "propagation_variant_depth": args.propagation_variant_depth,
            "stability_variant_samples": args.stability_variant_samples,
            "stability_variant_hold_ms": args.stability_variant_hold_ms,
            "stability_variant_bypass_score": args.stability_variant_bypass_score,
            "stability_variant_action": args.stability_variant_action,
            "max_frontier_width": args.max_frontier_width,
            "interval_ms": args.interval_ms,
            "ttl_ms": args.ttl_ms,
            "confidence": args.confidence,
            "policy_window_ms": args.policy_window_ms,
            "boost_budget_ms": args.boost_budget_ms,
            "max_vtime_boost_us": args.max_vtime_boost_us,
            "collect_characterization": bool(args.collect_characterization),
            "characterization_interval_ms": args.characterization_interval_ms,
            "characterization_top_candidates": args.characterization_top_candidates,
            "frontier_dispatch_quota": args.frontier_dispatch_quota,
            "victim_command": args.victim_command,
            "victim_taskset": args.victim_taskset,
            "victim_warmup_sec": args.victim_warmup_sec,
        },
        "warning": (
            "Pilot mechanism matrix only. Do not interpret stage-time differences "
            "as FlowCausal speedups without repeated larger runs, mechanism "
            "improvement, and fairness/victim baselines."
        ),
        "variants": variants,
    }
    matrix["claim_audit"] = audit_matrix_claims(matrix)
    matrix["claim_gate_audit"] = audit_claim_gate_matrix(matrix)

    summary_path = out_dir / "matrix-summary.json"
    summary_path.write_text(json.dumps(matrix, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(matrix, out_dir / "matrix-summary.csv")
    write_markdown(matrix, out_dir / "README.md")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

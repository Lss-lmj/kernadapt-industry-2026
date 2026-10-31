#!/usr/bin/env python3
"""Run Test16 agent-like sandbox workloads under FlowCausal live observation.

This matrix is the first bridge from generated build/test workflows to the
local Test16 code-agent-style sandbox harness.  It wraps Test16's own runner in
the same FlowCausal live harness used by buildtest:

  * default/taskset rows run Test16 directly;
  * FlowCausal rows run Test16 under run_live_sandbox.py;
  * every trial keeps Test16 summaries, controller.ndjson, flowmon logs,
    manifests, and optional victim logs in one FlowCausal result directory.

The script intentionally keeps its claim audit conservative.  A single Test16
row can prove live wiring and BPF admission, not speedup, frontier accuracy, or
victim safety.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import shlex
import statistics
import subprocess
import sys
import time
from typing import Any

from audit_claim_gate import audit_matrix as audit_claim_gate_matrix
from run_buildtest_matrix import (
    CHARACTERIZE_SANDBOX,
    parse_controller,
    parse_characterization_summary,
    parse_flowmon,
    parse_int_csv,
    parse_victim_log,
    remove_generated_tree,
    run_checked,
    start_logged,
    stop_process_group,
    summarize_values,
    sysfs_value,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
REPO_ROOT = ROOT.parent
LIVE_SANDBOX = ROOT / "experiments" / "run_live_sandbox.py"
TEST16_ROOT = REPO_ROOT / "test16-agent-sandbox-sched"
TEST16_RUNNER = TEST16_ROOT / "scripts" / "run_agent_sandbox_sched.py"

TEST16_WORKLOAD_METRICS = (
    "n",
    "success_rate",
    "timeout_rate",
    "task_completion_ms_mean",
    "task_completion_ms_p95",
    "critical_path_ms_mean",
    "end_to_end_ms_mean",
    "cost_proxy_ms_mean",
    "cpu_total_ms_mean",
)


def build_tools() -> None:
    run_checked(["make", "check"], cwd=SRC)


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes"}


def parse_float(value: str, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_test16_cases(path: pathlib.Path) -> dict[str, Any]:
    """Parse Test16 summary_agent_cases.csv into weighted aggregate metrics."""

    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                parsed: dict[str, Any] = dict(row)
                for key, value in row.items():
                    if key in {"experiment", "scheduler", "workload"}:
                        continue
                    parsed[key] = parse_float(value)
                rows.append(parsed)

    total_n = sum(float(row.get("n", 0.0)) for row in rows)

    def weighted_mean(key: str) -> float:
        if total_n <= 0.0:
            return 0.0
        return sum(float(row.get("n", 0.0)) * float(row.get(key, 0.0)) for row in rows) / total_n

    by_workload: dict[str, dict[str, float]] = {}
    for row in rows:
        workload = str(row.get("workload", "unknown"))
        by_workload[workload] = {
            "n": float(row.get("n", 0.0)),
            "success_rate": float(row.get("success_rate", 0.0)),
            "timeout_rate": float(row.get("timeout_rate", 0.0)),
            "task_completion_ms_mean": float(row.get("task_completion_ms_mean", 0.0)),
            "task_completion_ms_p95": float(row.get("task_completion_ms_p95", 0.0)),
            "critical_path_ms_mean": float(row.get("critical_path_ms_mean", 0.0)),
            "end_to_end_ms_mean": float(row.get("end_to_end_ms_mean", 0.0)),
            "cost_proxy_ms_mean": float(row.get("cost_proxy_ms_mean", 0.0)),
            "cpu_total_ms_mean": float(row.get("cpu_total_ms_mean", 0.0)),
        }

    return {
        "rows": rows,
        "workloads": sorted(by_workload),
        "by_workload": by_workload,
        "total_n": total_n,
        "success_rate": weighted_mean("success_rate"),
        "timeout_rate": weighted_mean("timeout_rate"),
        "task_completion_ms_mean": weighted_mean("task_completion_ms_mean"),
        "task_completion_ms_p95": weighted_mean("task_completion_ms_p95"),
        "critical_path_ms_mean": weighted_mean("critical_path_ms_mean"),
        "end_to_end_ms_mean": weighted_mean("end_to_end_ms_mean"),
        "cost_proxy_ms_mean": weighted_mean("cost_proxy_ms_mean"),
        "cpu_total_ms_mean": weighted_mean("cpu_total_ms_mean"),
    }


def parse_test16_runs(path: pathlib.Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                parsed: dict[str, Any] = dict(row)
                for key, value in row.items():
                    if key in {"experiment", "scheduler", "workload"}:
                        continue
                    if key in {"timeout", "success"}:
                        parsed[key] = parse_bool(value)
                    else:
                        parsed[key] = parse_float(value)
                rows.append(parsed)

    return {
        "rows": rows,
        "agents": len(rows),
        "successes": sum(1 for row in rows if row.get("success")),
        "timeouts": sum(1 for row in rows if row.get("timeout")),
    }


def parse_test16_result(result_dir: pathlib.Path) -> dict[str, Any]:
    return {
        "path": str(result_dir),
        "config": json.loads(read_text(result_dir / "agent_sandbox_config.json") or "{}"),
        "cases": parse_test16_cases(result_dir / "summary_agent_cases.csv"),
        "runs": parse_test16_runs(result_dir / "summary_agent_runs.csv"),
        "errors": read_text(result_dir / "errors.txt").strip(),
    }


def aggregate_trials(trials: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize_trial_field(section: str, key: str) -> dict[str, float]:
        values = [
            float(trial.get("test16", {}).get(section, {}).get(key, 0.0))
            for trial in trials
            if trial.get("test16", {}).get(section)
        ]
        return summarize_values(values)

    def mean_flow(key: str) -> float:
        values = [
            float(trial.get("flowmon", {}).get(key, 0.0))
            for trial in trials
            if trial.get("flowmon")
        ]
        return statistics.mean(values) if values else 0.0

    def mean_victim(key: str) -> float:
        values = [
            float(trial.get("victim", {}).get(key, 0.0))
            for trial in trials
            if trial.get("victim")
        ]
        return statistics.mean(values) if values else 0.0

    def mean_characterization(key: str) -> float:
        values = [
            float(trial.get("characterization", {}).get(key, 0.0))
            for trial in trials
            if trial.get("characterization")
        ]
        return statistics.mean(values) if values else 0.0

    controller_adaptive_widths: dict[str, int] = {}
    controller_adaptive_reasons: dict[str, int] = {}
    workload_values: dict[str, dict[str, list[float]]] = {}
    failed_trials = 0
    failed_returncodes: dict[str, int] = {}
    for trial in trials:
        manifest = trial.get("manifest", {})
        if isinstance(manifest, dict) and (
            manifest.get("live_harness_failed")
            or manifest.get("terminated_by_signal")
            or manifest.get("matrix_collection_failed")
        ):
            failed_trials += 1
            rc = str(manifest.get("live_harness_returncode", manifest.get("returncode", "unknown")))
            failed_returncodes[rc] = failed_returncodes.get(rc, 0) + 1
        ctl = trial.get("controller", {})
        for key, value in ctl.get("adaptive_width_counts", {}).items():
            controller_adaptive_widths[str(key)] = controller_adaptive_widths.get(str(key), 0) + int(value)
        for key, value in ctl.get("adaptive_reason_counts", {}).items():
            controller_adaptive_reasons[str(key)] = controller_adaptive_reasons.get(str(key), 0) + int(value)
        by_workload = trial.get("test16", {}).get("cases", {}).get("by_workload", {})
        if isinstance(by_workload, dict):
            for workload, metrics in by_workload.items():
                if not isinstance(metrics, dict):
                    continue
                bucket = workload_values.setdefault(str(workload), {key: [] for key in TEST16_WORKLOAD_METRICS})
                for key in TEST16_WORKLOAD_METRICS:
                    bucket[key].append(float(metrics.get(key, 0.0) or 0.0))

    workload_summary: dict[str, dict[str, dict[str, float]]] = {}
    for workload, metric_values in sorted(workload_values.items()):
        workload_summary[workload] = {
            key: summarize_values(values)
            for key, values in metric_values.items()
            if values
        }

    return {
        "trial_status": {
            "total": len(trials),
            "successful": max(0, len(trials) - failed_trials),
            "failed": failed_trials,
            "failed_returncodes": failed_returncodes,
        },
        "test16_summary": {
            "total_n": summarize_trial_field("cases", "total_n"),
            "success_rate": summarize_trial_field("cases", "success_rate"),
            "timeout_rate": summarize_trial_field("cases", "timeout_rate"),
            "task_completion_ms_mean": summarize_trial_field("cases", "task_completion_ms_mean"),
            "task_completion_ms_p95": summarize_trial_field("cases", "task_completion_ms_p95"),
            "critical_path_ms_mean": summarize_trial_field("cases", "critical_path_ms_mean"),
            "end_to_end_ms_mean": summarize_trial_field("cases", "end_to_end_ms_mean"),
            "cost_proxy_ms_mean": summarize_trial_field("cases", "cost_proxy_ms_mean"),
            "cpu_total_ms_mean": summarize_trial_field("cases", "cpu_total_ms_mean"),
        },
        "controller": {
            "records": sum(int(t.get("controller", {}).get("records", 0)) for t in trials),
            "selected_samples": sum(int(t.get("controller", {}).get("selected_samples", 0)) for t in trials),
            "frontier_records": sum(int(t.get("controller", {}).get("frontier_records", 0)) for t in trials),
            "propagation_anchors": sum(int(t.get("controller", {}).get("propagation_anchors", 0)) for t in trials),
            "adaptive_records": sum(int(t.get("controller", {}).get("adaptive_records", 0)) for t in trials),
            "adaptive_enabled_records": sum(int(t.get("controller", {}).get("adaptive_enabled_records", 0)) for t in trials),
            "adaptive_widened_records": sum(int(t.get("controller", {}).get("adaptive_widened_records", 0)) for t in trials),
            "adaptive_max_effective_width": max(
                [int(t.get("controller", {}).get("adaptive_max_effective_width", 0)) for t in trials]
                or [0]
            ),
            "adaptive_width_counts": controller_adaptive_widths,
            "adaptive_reason_counts": controller_adaptive_reasons,
        },
        "flowmon_mean": {
            key: mean_flow(key)
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
                "global_frontier_dsq_enqueues",
                "global_tracked_shared_path_enqueues",
                "global_untracked_shared_path_enqueues",
                "global_shared_global_enqueues",
                "global_shared_global_tracked_enqueues",
                "global_shared_global_untracked_enqueues",
                "global_latency_dsq_enqueues",
                "global_shared_local_tracked_enqueues",
                "global_shared_local_untracked_enqueues",
                "global_shared_percpu_tracked_enqueues",
                "global_shared_percpu_untracked_enqueues",
                "global_shared_local_pick_fallbacks",
                "global_shared_percpu_pick_fallbacks",
                "global_shared_local_frontier_fallback_enqueues",
                "global_frontier_budget_windows",
                "global_frontier_budget_trips",
                "global_frontier_budget_shared",
                "global_frontier_budget_misses",
                "frontier_runnable_wait_ns",
                "frontier_runtime_ns",
                "frontier_ratio",
                "fallback_ratio",
            )
        },
        "victim_mean": {
            key: mean_victim(key)
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
        },
        "characterization_mean": {
            key: mean_characterization(key)
            for key in (
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
        },
        "workload_summary": workload_summary,
    }


def test16_command(args: argparse.Namespace, result_dir: pathlib.Path, tag: str) -> list[str]:
    cmd = [
        sys.executable,
        str(TEST16_RUNNER),
        "--experiment",
        "scheduler",
        "--tag",
        tag,
        "--result-root",
        str(result_dir),
        "--schedulers",
        "default",
        "--workloads",
        args.workloads,
        "--concurrency",
        args.concurrency,
        "--runs",
        str(args.test16_runs),
        "--workload-scale",
        str(args.workload_scale),
        "--make-jobs",
        str(args.make_jobs),
        "--task-timeout-s",
        str(args.task_timeout_s),
    ]
    if args.critical_only:
        cmd.append("--critical-only")
    return cmd


def victim_cmd(args: argparse.Namespace) -> list[str] | None:
    if not args.victim_command:
        return None
    cmd = shlex.split(args.victim_command)
    if args.victim_taskset:
        cmd = ["taskset", "-c", args.victim_taskset, *cmd]
    return cmd


def run_default_trial(args: argparse.Namespace, trial_dir: pathlib.Path, tag: str) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    result_dir = trial_dir / "test16-result"
    inner_cmd = test16_command(args, result_dir, tag)
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
        wrapper_log = trial_dir / "characterizer.log"
    else:
        cmd = inner_cmd
        if args.taskset:
            cmd = ["taskset", "-c", args.taskset, *cmd]
        wrapper_log = trial_dir / "workload.log"

    sidecar = victim_cmd(args)
    sidecar_proc: subprocess.Popen[str] | None = None
    manifest: dict[str, Any] = {
        "variant_kind": "default",
        "command": cmd,
        "inner_command": inner_cmd,
        "test16_result_dir": str(result_dir),
        "collect_characterization": bool(args.collect_characterization),
        "characterization_dir": str(characterization_dir) if args.collect_characterization else None,
        "victim_cmd": sidecar,
    }
    try:
        if sidecar:
            sidecar_proc = start_logged(sidecar, trial_dir / "victim.log", start_new_session=True)
            if args.victim_warmup_sec > 0:
                time.sleep(args.victim_warmup_sec)
        with wrapper_log.open("w", encoding="utf-8") as log:
            print("+ " + " ".join(cmd), flush=True)
            proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, text=True, cwd=str(ROOT))
        manifest["returncode"] = proc.returncode
        if proc.returncode != 0:
            raise RuntimeError(f"default Test16 command failed with code {proc.returncode}")
    finally:
        if sidecar_proc is not None:
            manifest["victim_returncode"] = stop_process_group(sidecar_proc, "victim")
        (trial_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return {
        "path": str(trial_dir),
        "manifest": manifest,
        "test16": parse_test16_result(result_dir),
        "controller": {},
        "flowmon": {},
        "victim": parse_victim_log(trial_dir / "victim.log"),
        "characterization": parse_characterization_summary(characterization_dir / "summary.json"),
    }


def run_live_trial(
    args: argparse.Namespace,
    trial_dir: pathlib.Path,
    tag: str,
    spec: dict[str, Any],
) -> dict[str, Any]:
    result_dir = trial_dir / "test16-result"
    frontier_width = int(spec.get("frontier_width", args.frontier_width))
    max_frontier_width = int(spec.get("max_frontier_width", args.max_frontier_width))
    adaptive = bool(spec.get("adaptive_frontier_width", args.adaptive_frontier_width))
    adaptive_cap = int(spec.get("adaptive_max_frontier_width", args.adaptive_max_frontier_width))
    propagation = bool(spec.get("frontier_propagation", args.frontier_propagation))
    propagation_depth = int(spec.get("frontier_propagation_depth", args.frontier_propagation_depth))
    frontier_action = str(spec.get("frontier_action", args.frontier_action))
    dispatch_quota = int(spec.get("frontier_dispatch_quota", args.frontier_dispatch_quota))
    cooldown_dispatches = int(spec.get("guard_cooldown_dispatches", args.guard_cooldown_dispatches))
    pressure_fallback = int(spec.get("pressure_fallback_enqueues", args.pressure_fallback_enqueues))
    pressure_ms = int(spec.get("pressure_fallback_ms", args.pressure_fallback_ms))
    shared_wait_pressure = int(spec.get("shared_wait_pressure_us", args.shared_wait_pressure_us))
    pressure_dispatch_ms = int(spec.get("pressure_dispatch_ms", args.pressure_dispatch_ms))
    dispatch_window_ms = int(spec.get("frontier_dispatch_window_ms", args.frontier_dispatch_window_ms))
    dispatch_window_budget = int(
        spec.get("frontier_dispatch_window_budget", args.frontier_dispatch_window_budget)
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
        str(max(1, frontier_width)),
        "--min-frontier-score",
        str(args.min_frontier_score),
        "--frontier-stability-samples",
        str(args.frontier_stability_samples),
        "--frontier-hold-ms",
        str(args.frontier_hold_ms),
        "--frontier-bypass-score",
        str(args.frontier_bypass_score),
        "--frontier-action",
        frontier_action,
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
        str(max(0, shared_wait_pressure)),
        "--pressure-dispatch-ms",
        str(max(0, pressure_dispatch_ms)),
        "--frontier-dispatch-window-ms",
        str(max(0, dispatch_window_ms)),
        "--frontier-dispatch-window-budget",
        str(max(0, dispatch_window_budget)),
    ]
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
    if bool(spec.get("width_control", True)):
        cmd.extend(["--width-control", "--max-frontier-width", str(max(1, max_frontier_width))])
    if adaptive:
        cmd.extend(["--adaptive-frontier-width", "--adaptive-max-frontier-width", str(max(1, adaptive_cap))])
    if propagation:
        cmd.extend(["--frontier-propagation", "--frontier-propagation-depth", str(max(0, propagation_depth))])
    if args.victim_command:
        cmd.extend(["--victim-command", args.victim_command])
        if args.victim_taskset:
            cmd.extend(["--victim-taskset", args.victim_taskset])
        cmd.extend(["--victim-warmup-sec", str(args.victim_warmup_sec)])
    cmd.append("--")
    cmd.extend(test16_command(args, result_dir, tag))

    print("+ " + " ".join(cmd), flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True)
    manifest_path = trial_dir / "manifest.json"
    manifest = json.loads(read_text(manifest_path) or "{}")
    if not isinstance(manifest, dict):
        manifest = {}
    manifest.setdefault("variant_kind", "flowcausal")
    manifest.setdefault("live_sandbox_cmd", cmd)
    manifest["live_harness_returncode"] = proc.returncode
    manifest["sched_ext_state_after_matrix"] = sysfs_value("/sys/kernel/sched_ext/state")
    if proc.returncode != 0:
        manifest["live_harness_failed"] = True
        manifest.setdefault("live_harness_error", f"run_live_sandbox.py exited with code {proc.returncode}")
    try:
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError:
        manifest["matrix_collection_failed"] = True
    return {
        "path": str(trial_dir),
        "manifest": manifest,
        "test16": parse_test16_result(result_dir),
        "controller": parse_controller(trial_dir / "controller.ndjson"),
        "flowmon": parse_flowmon(trial_dir / "flowmon-final.log"),
        "victim": parse_victim_log(trial_dir / "victim.log"),
        "characterization": parse_characterization_summary(
            trial_dir / "characterization" / "summary.json"
        ),
    }


def audit_variant(variant: dict[str, Any]) -> dict[str, Any]:
    kind = str(variant.get("kind", ""))
    name = str(variant.get("name", ""))
    if kind == "default":
        return {
            "variant": name,
            "highest_level": "baseline",
            "evidence": ["Test16 summaries"],
            "blockers_for_next_level": ["no controller trace", "no BPF counters"],
            "forbidden_claims": ["FlowCausal speedup", "frontier accuracy", "victim slowdown bound"],
        }

    agg = variant.get("aggregate", {})
    ctl = agg.get("controller", {})
    flow = agg.get("flowmon_mean", {})
    victim = agg.get("victim_mean", {})
    test16 = agg.get("test16_summary", {})
    trial_status = agg.get("trial_status", {})
    failed_trials = int(trial_status.get("failed", 0) or 0)
    successful_trials = int(trial_status.get("successful", len(variant.get("trials", []))) or 0)
    frontier_records = int(ctl.get("frontier_records", 0) or 0)
    frontier_enqueues = float(flow.get("frontier_enqueues", 0.0) or 0.0)
    fallback_enqueues = float(flow.get("fallback_enqueues", 0.0) or 0.0)
    guard_trips = float(flow.get("global_frontier_guard_trips", 0.0) or 0.0)
    cooldown_entries = float(flow.get("global_shared_cooldown_entries", 0.0) or 0.0)
    pressure_entries = float(flow.get("global_pressure_fallback_entries", 0.0) or 0.0)
    pressure_enqueues = float(flow.get("global_pressure_fallback_enqueues", 0.0) or 0.0)
    shared_wait_events = float(flow.get("global_shared_wait_pressure_events", 0.0) or 0.0)
    pressure_dispatch_entries = float(flow.get("global_pressure_dispatch_entries", 0.0) or 0.0)
    budget_trips = float(flow.get("global_frontier_budget_trips", 0.0) or 0.0)
    adaptive = bool(variant.get("adaptive_frontier_width", False))
    guard_enabled = int(variant.get("frontier_dispatch_quota", 0) or 0) > 0
    cooldown_enabled = int(variant.get("guard_cooldown_dispatches", 0) or 0) > 0
    pressure_enabled = (
        int(variant.get("pressure_fallback_enqueues", 0) or 0) > 0
        or int(variant.get("pressure_fallback_ms", 0) or 0) > 0
    )
    shared_wait_enabled = int(variant.get("shared_wait_pressure_us", 0) or 0) > 0
    pressure_dispatch_enabled = int(variant.get("pressure_dispatch_ms", 0) or 0) > 0
    dispatch_budget_enabled = (
        int(variant.get("frontier_dispatch_window_ms", 0) or 0) > 0
        and int(variant.get("frontier_dispatch_window_budget", 0) or 0) > 0
    )

    evidence: list[str] = []
    blockers: list[str] = []
    forbidden = [
        "FlowCausal accelerates code agents",
        "FlowCausal improves Test16 agent-like completion",
        "frontier inference is accurate",
        "victim slowdown is bounded",
    ]
    if adaptive:
        forbidden.append("adaptive width improves performance or correctly identifies the critical cohort")

    if float(test16.get("total_n", {}).get("mean", 0.0) or 0.0) > 0:
        evidence.append("Test16 summary rows present")
    else:
        blockers.append("missing Test16 summary rows")
    if failed_trials:
        blockers.append(f"{failed_trials} live trial(s) failed or were interrupted; inspect per-trial manifest before using counters")
    if frontier_records > 0:
        evidence.append(f"controller selected {frontier_records} frontier records")
    else:
        blockers.append("controller did not select auditable frontiers")
    if adaptive:
        evidence.append(
            "adaptive width audit "
            f"records={ctl.get('adaptive_records', 0)}, "
            f"widened={ctl.get('adaptive_widened_records', 0)}, "
            f"max_width={ctl.get('adaptive_max_effective_width', 0)}"
        )
    if frontier_enqueues > 0:
        evidence.append(f"BPF admitted mean frontier_enqueues={frontier_enqueues:.1f}")
    elif fallback_enqueues > 0:
        evidence.append(f"BPF fallback/width-held path mean fallback_enqueues={fallback_enqueues:.1f}")
    else:
        blockers.append("flowmon-final lacks positive frontier admission or fallback routing")
    if guard_trips > 0:
        evidence.append(f"dispatch guard tripped mean={guard_trips:.1f}")
    elif guard_enabled:
        blockers.append("dispatch guard enabled but no guard trips observed")
    if cooldown_entries > 0:
        evidence.append(f"shared-first cooldown entries mean={cooldown_entries:.1f}")
    elif cooldown_enabled:
        blockers.append("cooldown enabled but no cooldown entries observed")
    if pressure_entries > 0 or pressure_enqueues > 0:
        evidence.append(
            f"pressure fallback entries/enqueues mean={pressure_entries:.1f}/{pressure_enqueues:.1f}"
        )
    elif pressure_enabled:
        blockers.append("pressure fallback enabled but no pressure-fallback counters observed")
    if shared_wait_events > 0:
        evidence.append(f"shared-wait pressure events mean={shared_wait_events:.1f}")
    elif shared_wait_enabled:
        blockers.append("shared-wait pressure enabled but no shared-wait events observed")
    if pressure_dispatch_entries > 0:
        evidence.append(f"pressure dispatch backoff entries mean={pressure_dispatch_entries:.1f}")
    elif pressure_dispatch_enabled:
        blockers.append("pressure dispatch enabled but no pressure-dispatch entries observed")
    if budget_trips > 0:
        evidence.append(f"dispatch-window budget trips mean={budget_trips:.1f}")
    elif dispatch_budget_enabled:
        blockers.append("dispatch-window budget enabled but no budget trips observed")
    if float(victim.get("iterations", 0.0) or 0.0) > 0:
        evidence.append("victim sidecar summary is present")
    else:
        blockers.append("no victim sidecar evidence")
    if successful_trials < 3:
        blockers.append("fewer than 3 trials; not enough for performance or safety claims")
    blockers.append("no oracle/default direct-mechanism comparison proving L2")

    highest = (
        "L1: mechanism connectivity"
        if successful_trials > 0 and frontier_records > 0 and frontier_enqueues > 0
        else "L0: runnable/instrumented only"
    )
    return {
        "variant": name,
        "highest_level": highest,
        "evidence": evidence,
        "blockers_for_next_level": blockers,
        "forbidden_claims": forbidden,
    }


def audit_matrix(matrix: dict[str, Any]) -> dict[str, Any]:
    variant_claims = [audit_variant(v) for v in matrix.get("variants", [])]
    highest = (
        "L1: mechanism connectivity"
        if any(str(item.get("highest_level", "")).startswith("L1") for item in variant_claims)
        else "L0: runnable/instrumented only"
    )
    return {
        "highest_supported_claim_level": highest,
        "summary": (
            "Variant-local instrumentation audit only.  It checks whether each "
            "FlowCausal row produced controller/BPF/victim evidence, but it does "
            "not compare against default.  Use claim_gate_audit for outcome and "
            "safety claim levels."
        ),
        "variant_claims": variant_claims,
        "permitted_claims": [
            "Test16 agent-like workloads can be run under FlowCausal observation",
            "controller-selected frontiers can be audited with Test16 summaries",
            "static/adaptive frontier width decisions can be audited when their variants are present",
            "BPF admission/fallback counters can be paired with Test16 and victim summaries",
        ],
        "forbidden_claims": [
            "FlowCausal accelerates code agents",
            "FlowCausal improves Test16 completion",
            "frontier inference is accurate",
            "adaptive width is safe or correct",
            "victim slowdown is bounded",
        ],
    }


def write_csv(matrix: dict[str, Any], path: pathlib.Path) -> None:
    rows: list[dict[str, Any]] = []
    claim_by_variant = {
        str(item.get("variant", "")): item
        for item in matrix.get("claim_audit", {}).get("variant_claims", [])
        if isinstance(item, dict)
    }
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        test16 = agg["test16_summary"]
        ctl = agg["controller"]
        flow = agg["flowmon_mean"]
        victim = agg["victim_mean"]
        char = agg.get("characterization_mean", {}) if isinstance(agg.get("characterization_mean"), dict) else {}
        trial_status = agg.get("trial_status", {})
        claim = claim_by_variant.get(str(variant["name"]), {})
        rows.append(
            {
                "variant": variant["name"],
                "kind": variant["kind"],
                "frontier_width": variant.get("frontier_width", ""),
                "max_frontier_width": variant.get("max_frontier_width", ""),
                "adaptive_frontier_width": bool(variant.get("adaptive_frontier_width", False)),
                "adaptive_max_frontier_width": variant.get("adaptive_max_frontier_width", ""),
                "frontier_action": variant.get("frontier_action", ""),
                "frontier_dispatch_quota": variant.get("frontier_dispatch_quota", ""),
                "guard_cooldown_dispatches": variant.get("guard_cooldown_dispatches", ""),
                "pressure_fallback_enqueues": variant.get("pressure_fallback_enqueues", ""),
                "pressure_fallback_ms": variant.get("pressure_fallback_ms", ""),
                "shared_wait_pressure_us": variant.get("shared_wait_pressure_us", ""),
                "pressure_dispatch_ms": variant.get("pressure_dispatch_ms", ""),
                "frontier_dispatch_window_ms": variant.get("frontier_dispatch_window_ms", ""),
                "frontier_dispatch_window_budget": variant.get("frontier_dispatch_window_budget", ""),
                "trials": len(variant["trials"]),
                "successful_trials": trial_status.get("successful", len(variant["trials"])),
                "failed_trials": trial_status.get("failed", 0),
                "agents_mean": test16.get("total_n", {}).get("mean", 0.0),
                "success_rate_mean": test16.get("success_rate", {}).get("mean", 0.0),
                "timeout_rate_mean": test16.get("timeout_rate", {}).get("mean", 0.0),
                "task_completion_ms_mean": test16.get("task_completion_ms_mean", {}).get("mean", 0.0),
                "critical_path_ms_mean": test16.get("critical_path_ms_mean", {}).get("mean", 0.0),
                "end_to_end_ms_mean": test16.get("end_to_end_ms_mean", {}).get("mean", 0.0),
                "cpu_total_ms_mean": test16.get("cpu_total_ms_mean", {}).get("mean", 0.0),
                "controller_frontier_records": ctl.get("frontier_records", 0),
                "controller_adaptive_records": ctl.get("adaptive_records", 0),
                "controller_adaptive_widened_records": ctl.get("adaptive_widened_records", 0),
                "controller_adaptive_max_effective_width": ctl.get("adaptive_max_effective_width", 0),
                "frontier_enqueues_mean": flow.get("frontier_enqueues", 0.0),
                "fallback_enqueues_mean": flow.get("fallback_enqueues", 0.0),
                "width_held_enqueues_mean": flow.get("width_held_enqueues", 0.0),
                "global_shared_first_dispatches_mean": flow.get("global_shared_first_dispatches", 0.0),
                "global_frontier_guard_trips_mean": flow.get("global_frontier_guard_trips", 0.0),
                "global_frontier_guard_misses_mean": flow.get("global_frontier_guard_misses", 0.0),
                "global_shared_cooldown_entries_mean": flow.get("global_shared_cooldown_entries", 0.0),
                "global_shared_cooldown_dispatches_mean": flow.get("global_shared_cooldown_dispatches", 0.0),
                "global_shared_cooldown_misses_mean": flow.get("global_shared_cooldown_misses", 0.0),
                "global_pressure_fallback_entries_mean": flow.get("global_pressure_fallback_entries", 0.0),
                "global_pressure_fallback_enqueues_mean": flow.get("global_pressure_fallback_enqueues", 0.0),
                "global_shared_wait_pressure_events_mean": flow.get("global_shared_wait_pressure_events", 0.0),
                "global_pressure_dispatch_entries_mean": flow.get("global_pressure_dispatch_entries", 0.0),
                "global_pressure_dispatches_mean": flow.get("global_pressure_dispatches", 0.0),
                "global_pressure_dispatch_misses_mean": flow.get("global_pressure_dispatch_misses", 0.0),
                "global_frontier_budget_trips_mean": flow.get("global_frontier_budget_trips", 0.0),
                "global_frontier_budget_shared_mean": flow.get("global_frontier_budget_shared", 0.0),
                "global_frontier_budget_misses_mean": flow.get("global_frontier_budget_misses", 0.0),
                "frontier_runnable_wait_ms_mean": flow.get("frontier_runnable_wait_ns", 0.0) / 1_000_000.0,
                "frontier_runtime_ms_mean": flow.get("frontier_runtime_ns", 0.0) / 1_000_000.0,
                "frontier_ratio_mean": flow.get("frontier_ratio", 0.0),
                "fallback_ratio_mean": flow.get("fallback_ratio", 0.0),
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


def write_workload_csv(matrix: dict[str, Any], path: pathlib.Path) -> None:
    rows: list[dict[str, Any]] = []
    for variant in matrix["variants"]:
        workload_summary = variant.get("aggregate", {}).get("workload_summary", {})
        if not isinstance(workload_summary, dict):
            continue
        for workload, metrics in workload_summary.items():
            if not isinstance(metrics, dict):
                continue
            rows.append(
                {
                    "variant": variant["name"],
                    "kind": variant["kind"],
                    "workload": workload,
                    "trials_observed": metrics.get("n", {}).get("n", 0.0),
                    "agents_mean": metrics.get("n", {}).get("mean", 0.0),
                    "success_rate_mean": metrics.get("success_rate", {}).get("mean", 0.0),
                    "timeout_rate_mean": metrics.get("timeout_rate", {}).get("mean", 0.0),
                    "task_completion_ms_mean": metrics.get("task_completion_ms_mean", {}).get("mean", 0.0),
                    "task_completion_ms_p95_mean": metrics.get("task_completion_ms_p95", {}).get("mean", 0.0),
                    "critical_path_ms_mean": metrics.get("critical_path_ms_mean", {}).get("mean", 0.0),
                    "end_to_end_ms_mean": metrics.get("end_to_end_ms_mean", {}).get("mean", 0.0),
                    "cost_proxy_ms_mean": metrics.get("cost_proxy_ms_mean", {}).get("mean", 0.0),
                    "cpu_total_ms_mean": metrics.get("cpu_total_ms_mean", {}).get("mean", 0.0),
                }
            )
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(matrix: dict[str, Any], path: pathlib.Path) -> None:
    lines = [
        "# FlowCausal Test16 agent-like live matrix",
        "",
        "This matrix wraps the local Test16 sandbox scheduling harness.  It is a",
        "mechanism/evidence bridge.  The claim-gate audit at the end is the",
        "authoritative outcome/safety claim level; the per-variant audit below",
        "only checks local instrumentation connectivity.",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(matrix.get("config", {}), indent=2, sort_keys=True),
        "```",
        "",
        "| Variant | Width | Action | Adaptive | Safety counters | Trials ok/fail | Agents | Success | Task ms | Frontier rec/enq | Fallback/width-held | Frontier wait/runtime ms | Victim p95/p99/max ms | Variant-local audit |",
        "| --- | ---: | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    claim_by_variant = {
        str(item.get("variant", "")): item
        for item in matrix.get("claim_audit", {}).get("variant_claims", [])
        if isinstance(item, dict)
    }
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        test16 = agg["test16_summary"]
        ctl = agg["controller"]
        flow = agg["flowmon_mean"]
        victim = agg["victim_mean"]
        trial_status = agg.get("trial_status", {})
        adaptive = "-"
        if bool(variant.get("adaptive_frontier_width", False)):
            adaptive = f"on/{ctl.get('adaptive_records', 0)}/{ctl.get('adaptive_widened_records', 0)}/{ctl.get('adaptive_max_effective_width', 0)}"
        width = "-"
        if variant.get("kind") == "flowcausal":
            width = f"{variant.get('frontier_width')}/{variant.get('max_frontier_width')}"
        victim_cell = "-"
        if float(victim.get("iterations", 0.0) or 0.0) > 0:
            victim_cell = f"{victim.get('lateness_p95_ms', 0.0):.3f}/{victim.get('lateness_p99_ms', 0.0):.3f}/{victim.get('lateness_max_ms', 0.0):.3f}"
        guard_cell = "g={trips:.0f}/{shared:.0f}".format(
            trips=float(flow.get("global_frontier_guard_trips", 0.0)),
            shared=float(flow.get("global_shared_first_dispatches", 0.0)),
        )
        pressure_cell = "p={entries:.0f}/{enq:.0f}; sw={sw:.0f}; pd={pd:.0f}".format(
            entries=float(flow.get("global_pressure_fallback_entries", 0.0)),
            enq=float(flow.get("global_pressure_fallback_enqueues", 0.0)),
            sw=float(flow.get("global_shared_wait_pressure_events", 0.0)),
            pd=float(flow.get("global_pressure_dispatch_entries", 0.0)),
        )
        budget_cell = "b={trips:.0f}/{shared:.0f}/{miss:.0f}".format(
            trips=float(flow.get("global_frontier_budget_trips", 0.0)),
            shared=float(flow.get("global_frontier_budget_shared", 0.0)),
            miss=float(flow.get("global_frontier_budget_misses", 0.0)),
        )
        safety_cell = f"{guard_cell}; {pressure_cell}; {budget_cell}"
        claim = claim_by_variant.get(str(variant["name"]), {})
        lines.append(
            "| {name} | {width} | {action} | {adaptive} | {safety} | {ok}/{fail} | {agents:.1f} | {success:.3f} | {task:.1f} | {frontier_records}/{frontier:.1f} | {fallback:.1f}/{widthheld:.1f} | {fwait:.3f}/{fruntime:.3f} | {victim} | {claim} |".format(
                name=variant["name"],
                width=width,
                action=variant.get("frontier_action") or "-",
                adaptive=adaptive,
                safety=safety_cell,
                ok=int(trial_status.get("successful", len(variant["trials"]))),
                fail=int(trial_status.get("failed", 0)),
                agents=float(test16.get("total_n", {}).get("mean", 0.0)),
                success=float(test16.get("success_rate", {}).get("mean", 0.0)),
                task=float(test16.get("task_completion_ms_mean", {}).get("mean", 0.0)),
                frontier_records=int(ctl.get("frontier_records", 0)),
                frontier=float(flow.get("frontier_enqueues", 0.0)),
                fallback=float(flow.get("fallback_enqueues", 0.0)),
                widthheld=float(flow.get("width_held_enqueues", 0.0)),
                fwait=float(flow.get("frontier_runnable_wait_ns", 0.0)) / 1_000_000.0,
                fruntime=float(flow.get("frontier_runtime_ns", 0.0)) / 1_000_000.0,
                victim=victim_cell,
                claim=claim.get("highest_level", ""),
            )
        )
    lines.extend(
        [
            "",
            "## Per-workload Test16 summary",
            "",
            "These rows come from Test16's own case summary and are kept separate",
            "from mechanism counters.  They are useful for locating workload",
            "classes, but they are not standalone speedup claims.",
            "",
            "| Variant | Workload | Agents | Success | Task ms | Critical path ms | CPU total ms |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for variant in matrix["variants"]:
        workload_summary = variant.get("aggregate", {}).get("workload_summary", {})
        if not isinstance(workload_summary, dict):
            continue
        for workload, metrics in workload_summary.items():
            if not isinstance(metrics, dict):
                continue
            lines.append(
                "| {variant} | {workload} | {agents:.1f} | {success:.3f} | {task:.1f} | {crit:.1f} | {cpu:.1f} |".format(
                    variant=variant["name"],
                    workload=workload,
                    agents=float(metrics.get("n", {}).get("mean", 0.0)),
                    success=float(metrics.get("success_rate", {}).get("mean", 0.0)),
                    task=float(metrics.get("task_completion_ms_mean", {}).get("mean", 0.0)),
                    crit=float(metrics.get("critical_path_ms_mean", {}).get("mean", 0.0)),
                    cpu=float(metrics.get("cpu_total_ms_mean", {}).get("mean", 0.0)),
                )
            )
    lines.extend(
        [
            "",
            "## Direct schedstat characterization",
            "",
            "These counters come from flowctl-ranked process-tree sampling and `/proc/<pid>/schedstat`.",
            "They are lower-bound CPU mechanism evidence for the selected frontier, not a standalone speedup claim.",
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
            "## Variant-local instrumentation audit",
            "",
            f"- Highest supported variant-local level: `{matrix.get('claim_audit', {}).get('highest_supported_claim_level', 'unknown')}`.",
            "- This section does not compare against default rows and therefore does not override the claim-gate audit below.",
            "",
            "Not authorized by this section alone:",
            "",
        ]
    )
    for claim in matrix.get("claim_audit", {}).get("forbidden_claims", []):
        lines.append(f"- {claim}")
    lines.extend(
        [
            "",
            "Per-variant instrumentation audit:",
            "",
            "| Variant | Highest level | Evidence | Blockers |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in matrix.get("claim_audit", {}).get("variant_claims", []):
        evidence = "; ".join(str(x) for x in item.get("evidence", [])) or "-"
        blockers = "; ".join(str(x) for x in item.get("blockers_for_next_level", [])) or "-"
        lines.append(f"| {item.get('variant', '')} | {item.get('highest_level', '')} | {evidence} | {blockers} |")
    claim_gate_audit = (
        matrix.get("claim_gate_audit", {}) if isinstance(matrix.get("claim_gate_audit"), dict) else {}
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
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run FlowCausal live matrix on local Test16 agent-like workloads",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=str(ROOT / "experiments" / "results" / "test16-agent-matrix-smoke"))
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--no-build-tools", action="store_true")
    parser.add_argument("--taskset", default="0-1")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--workloads", default="shell_mix python_test build_like retrieval_like")
    parser.add_argument("--concurrency", default="8")
    parser.add_argument("--test16-runs", type=int, default=1)
    parser.add_argument("--workload-scale", type=int, default=3)
    parser.add_argument("--make-jobs", type=int, default=4)
    parser.add_argument("--task-timeout-s", type=int, default=180)
    parser.add_argument("--critical-only", action="store_true")
    parser.add_argument("--frontier-width", type=int, default=2)
    parser.add_argument("--max-frontier-width", type=int, default=2)
    parser.add_argument("--adaptive-frontier-width", action="store_true")
    parser.add_argument("--adaptive-max-frontier-width", type=int, default=8)
    parser.add_argument("--include-frontier-width-variants", action="store_true")
    parser.add_argument("--frontier-width-values", default="2,8")
    parser.add_argument("--include-adaptive-width-variant", action="store_true")
    parser.add_argument("--min-frontier-score", type=int, default=50)
    parser.add_argument("--frontier-propagation", action="store_true")
    parser.add_argument("--frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--frontier-stability-samples", type=int, default=1)
    parser.add_argument("--frontier-hold-ms", type=int, default=0)
    parser.add_argument("--frontier-bypass-score", type=int, default=140)
    parser.add_argument("--frontier-action", choices=("advance", "auto", "observe", "stage-aware"), default="advance")
    parser.add_argument(
        "--include-auto-action-variant",
        action="store_true",
        help="add flowcausal_width_auto to compare automatic observe/advance decisions against the default advance policy",
    )
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--ttl-ms", type=int, default=1000)
    parser.add_argument("--confidence", type=int, default=75)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=100)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument(
        "--collect-characterization",
        action="store_true",
        help=(
            "collect flowctl-ranked process-tree and /proc/<pid>/schedstat "
            "deltas for default and live Test16 rows"
        ),
    )
    parser.add_argument("--characterization-interval-ms", type=int, default=50)
    parser.add_argument("--characterization-top-candidates", type=int, default=6)
    parser.add_argument("--frontier-dispatch-quota", type=int, default=4)
    parser.add_argument("--guard-cooldown-dispatches", type=int, default=0)
    parser.add_argument(
        "--include-dispatch-guard-variants",
        action="store_true",
        help="add flowcausal_width_guard<N> variants for dispatch-guard quota ablation",
    )
    parser.add_argument("--dispatch-guard-quotas", default="1,4")
    parser.add_argument(
        "--include-guard-cooldown-variants",
        action="store_true",
        help="add flowcausal_width_guard<Q>_cooldown<N> variants for shared-first cooldown ablation",
    )
    parser.add_argument("--guard-cooldown-values", default="1,2")
    parser.add_argument("--pressure-fallback-enqueues", type=int, default=0)
    parser.add_argument("--pressure-fallback-ms", type=int, default=0)
    parser.add_argument(
        "--include-pressure-fallback-variants",
        action="store_true",
        help="add flowcausal_width_guard<Q>_pressure<N> variants for shared-pressure admission-fallback ablation",
    )
    parser.add_argument("--pressure-fallback-values", default="1,4")
    parser.add_argument("--shared-wait-pressure-us", type=int, default=0)
    parser.add_argument("--pressure-dispatch-ms", type=int, default=0)
    parser.add_argument("--frontier-dispatch-window-ms", type=int, default=0)
    parser.add_argument("--frontier-dispatch-window-budget", type=int, default=0)
    parser.add_argument(
        "--include-dispatch-budget-variants",
        action="store_true",
        help="add flowcausal_width_dbudget<N> variants for dispatch-window budget ablation",
    )
    parser.add_argument("--dispatch-budget-values", default="1,2")
    parser.add_argument("--victim-command", default=None)
    parser.add_argument("--victim-taskset", default=None)
    parser.add_argument("--victim-warmup-sec", type=float, default=0.2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not TEST16_RUNNER.exists():
        raise FileNotFoundError(f"Test16 runner not found: {TEST16_RUNNER}")
    if args.trials < 1:
        raise SystemExit("--trials must be positive")
    width_values = parse_int_csv(args.frontier_width_values, name="--frontier-width-values")
    dispatch_guard_quotas = parse_int_csv(args.dispatch_guard_quotas, name="--dispatch-guard-quotas")
    guard_cooldown_values = parse_int_csv(args.guard_cooldown_values, name="--guard-cooldown-values")
    pressure_fallback_values = parse_int_csv(
        args.pressure_fallback_values,
        name="--pressure-fallback-values",
    )
    dispatch_budget_values = parse_int_csv(args.dispatch_budget_values, name="--dispatch-budget-values")

    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists():
        if not args.overwrite:
            raise FileExistsError(f"{out_dir} exists; pass --overwrite")
        remove_generated_tree(out_dir)
    out_dir.mkdir(parents=True)

    if not args.no_build_tools and not args.skip_live:
        build_tools()

    variant_specs: list[dict[str, Any]] = [
        {"name": "default_taskset", "kind": "default", "width_control": False}
    ]
    if not args.skip_live:
        variant_specs.extend(
            [
                {"name": "flowcausal_no_width", "kind": "flowcausal", "width_control": False},
                {
                    "name": "flowcausal_width",
                    "kind": "flowcausal",
                    "width_control": True,
                    "frontier_width": args.frontier_width,
                    "max_frontier_width": args.max_frontier_width,
                },
            ]
        )
        if args.include_frontier_width_variants:
            for width in width_values:
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
            cap = max(1, args.adaptive_max_frontier_width)
            variant_specs.append(
                {
                    "name": f"flowcausal_width_adaptive{cap}",
                    "kind": "flowcausal",
                    "width_control": True,
                    "frontier_width": args.frontier_width,
                    "max_frontier_width": args.max_frontier_width,
                    "adaptive_frontier_width": True,
                    "adaptive_max_frontier_width": cap,
                }
            )
        if args.include_auto_action_variant:
            variant_specs.append(
                {
                    "name": "flowcausal_width_auto",
                    "kind": "flowcausal",
                    "width_control": True,
                    "frontier_width": args.frontier_width,
                    "max_frontier_width": args.max_frontier_width,
                    "frontier_action": "auto",
                }
            )
        if args.include_dispatch_guard_variants:
            for quota in dispatch_guard_quotas:
                variant_specs.append(
                    {
                        "name": f"flowcausal_width_guard{quota}",
                        "kind": "flowcausal",
                        "width_control": True,
                        "frontier_width": args.frontier_width,
                        "max_frontier_width": args.max_frontier_width,
                        "frontier_dispatch_quota": max(0, quota),
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
                        "frontier_width": args.frontier_width,
                        "max_frontier_width": args.max_frontier_width,
                        "frontier_dispatch_quota": base_quota,
                        "guard_cooldown_dispatches": max(0, cooldown),
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
                        "frontier_width": args.frontier_width,
                        "max_frontier_width": args.max_frontier_width,
                        "frontier_dispatch_quota": base_quota,
                        "pressure_fallback_enqueues": max(0, pressure),
                        "pressure_fallback_ms": max(0, args.pressure_fallback_ms),
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
                        "frontier_width": args.frontier_width,
                        "max_frontier_width": args.max_frontier_width,
                        "frontier_dispatch_window_ms": window_ms,
                        "frontier_dispatch_window_budget": max(1, budget),
                    }
                )

    variants: list[dict[str, Any]] = []
    for spec in variant_specs:
        name = str(spec["name"])
        trials: list[dict[str, Any]] = []
        for trial_idx in range(args.trials):
            trial_dir = out_dir / name / f"trial-{trial_idx:02d}"
            tag = f"{out_dir.name}_{name}_t{trial_idx:02d}"
            if spec["kind"] == "default":
                trial = run_default_trial(args, trial_dir, tag)
            else:
                trial = run_live_trial(args, trial_dir, tag, spec)
            trial["trial"] = trial_idx
            trials.append(trial)
        variants.append(
            {
                "name": name,
                "kind": spec["kind"],
                "width_control": bool(spec.get("width_control", False)),
                "frontier_width": int(spec.get("frontier_width", args.frontier_width)) if spec["kind"] == "flowcausal" else None,
                "max_frontier_width": int(spec.get("max_frontier_width", args.max_frontier_width)) if spec["kind"] == "flowcausal" else None,
                "adaptive_frontier_width": bool(spec.get("adaptive_frontier_width", False)) if spec["kind"] == "flowcausal" else False,
                "adaptive_max_frontier_width": int(spec.get("adaptive_max_frontier_width", args.adaptive_max_frontier_width))
                if spec["kind"] == "flowcausal" and bool(spec.get("adaptive_frontier_width", False))
                else None,
                "frontier_action": str(spec.get("frontier_action", args.frontier_action))
                if spec["kind"] == "flowcausal"
                else None,
                "frontier_dispatch_quota": int(spec.get("frontier_dispatch_quota", args.frontier_dispatch_quota))
                if spec["kind"] == "flowcausal"
                else None,
                "guard_cooldown_dispatches": int(spec.get("guard_cooldown_dispatches", args.guard_cooldown_dispatches))
                if spec["kind"] == "flowcausal"
                else None,
                "pressure_fallback_enqueues": int(spec.get("pressure_fallback_enqueues", args.pressure_fallback_enqueues))
                if spec["kind"] == "flowcausal"
                else None,
                "pressure_fallback_ms": int(spec.get("pressure_fallback_ms", args.pressure_fallback_ms))
                if spec["kind"] == "flowcausal"
                else None,
                "shared_wait_pressure_us": int(spec.get("shared_wait_pressure_us", args.shared_wait_pressure_us))
                if spec["kind"] == "flowcausal"
                else None,
                "pressure_dispatch_ms": int(spec.get("pressure_dispatch_ms", args.pressure_dispatch_ms))
                if spec["kind"] == "flowcausal"
                else None,
                "frontier_dispatch_window_ms": int(spec.get("frontier_dispatch_window_ms", args.frontier_dispatch_window_ms))
                if spec["kind"] == "flowcausal"
                else None,
                "frontier_dispatch_window_budget": int(
                    spec.get("frontier_dispatch_window_budget", args.frontier_dispatch_window_budget)
                )
                if spec["kind"] == "flowcausal"
                else None,
                "trials": trials,
                "aggregate": aggregate_trials(trials),
            }
        )

    matrix = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "config": {
            "taskset": args.taskset,
            "trials": args.trials,
            "workloads": args.workloads,
            "concurrency": args.concurrency,
            "test16_runs": args.test16_runs,
            "workload_scale": args.workload_scale,
            "make_jobs": args.make_jobs,
            "task_timeout_s": args.task_timeout_s,
            "frontier_width": args.frontier_width,
            "max_frontier_width": args.max_frontier_width,
            "collect_characterization": bool(args.collect_characterization),
            "characterization_interval_ms": args.characterization_interval_ms,
            "characterization_top_candidates": args.characterization_top_candidates,
            "include_frontier_width_variants": bool(args.include_frontier_width_variants),
            "frontier_width_values": width_values,
            "include_adaptive_width_variant": bool(args.include_adaptive_width_variant),
            "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
            "frontier_action": args.frontier_action,
            "include_auto_action_variant": bool(args.include_auto_action_variant),
            "frontier_dispatch_quota": args.frontier_dispatch_quota,
            "guard_cooldown_dispatches": args.guard_cooldown_dispatches,
            "include_dispatch_guard_variants": bool(args.include_dispatch_guard_variants),
            "dispatch_guard_quotas": dispatch_guard_quotas,
            "include_guard_cooldown_variants": bool(args.include_guard_cooldown_variants),
            "guard_cooldown_values": guard_cooldown_values,
            "pressure_fallback_enqueues": args.pressure_fallback_enqueues,
            "pressure_fallback_ms": args.pressure_fallback_ms,
            "include_pressure_fallback_variants": bool(args.include_pressure_fallback_variants),
            "pressure_fallback_values": pressure_fallback_values,
            "shared_wait_pressure_us": args.shared_wait_pressure_us,
            "pressure_dispatch_ms": args.pressure_dispatch_ms,
            "frontier_dispatch_window_ms": args.frontier_dispatch_window_ms,
            "frontier_dispatch_window_budget": args.frontier_dispatch_window_budget,
            "include_dispatch_budget_variants": bool(args.include_dispatch_budget_variants),
            "dispatch_budget_values": dispatch_budget_values,
            "frontier_propagation": bool(args.frontier_propagation),
            "frontier_propagation_depth": args.frontier_propagation_depth,
            "victim_command": args.victim_command,
            "victim_taskset": args.victim_taskset,
        },
        "warning": (
            "Test16 agent-like matrix only. Do not interpret timing differences "
            "as FlowCausal speedups without repeated trials, direct mechanism "
            "improvement, and victim/fairness baselines."
        ),
        "variants": variants,
    }
    matrix["claim_audit"] = audit_matrix(matrix)
    matrix["claim_gate_audit"] = audit_claim_gate_matrix(matrix)
    (out_dir / "matrix-summary.json").write_text(
        json.dumps(matrix, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_csv(matrix, out_dir / "matrix-summary.csv")
    write_workload_csv(matrix, out_dir / "matrix-workloads.csv")
    write_markdown(matrix, out_dir / "README.md")
    print(f"summary={out_dir / 'matrix-summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

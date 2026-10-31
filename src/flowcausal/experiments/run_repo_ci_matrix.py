#!/usr/bin/env python3
"""Run an arbitrary repo/CI shell command under the FlowCausal matrix format.

This is the bridge from synthetic/Test16 harnesses to real repositories.  The
script runs the same bounded shell command in three rows:

* ``default_taskset``: no sched_ext attachment; ``flowctl`` is dry-run only so
  we can still collect the same process-tree characterization and optional
  victim sidecar.
* ``flowcausal_no_width``: FlowCausal scheduler attached, online frontier
  hints enabled, but BPF width-control disabled.
* ``flowcausal_width``: same as above with width-control enabled.

The command is executed through ``bash -lc`` with a few environment variables
set for each trial:

* ``FLOWCAUSAL_VARIANT``
* ``FLOWCAUSAL_TRIAL``
* ``FLOWCAUSAL_TRIAL_DIR``
* ``FLOWCAUSAL_BUILD_OUT``

Use those variables to create isolated build directories under ``/tmp``.  The
script deliberately does not infer true frontiers; it only packages raw logs,
controller/BPF counters, direct schedstat characterization, outcome time, and
victim sidecar metrics into ``matrix-summary.json`` so the standard claim gate
can audit the result.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import shlex
import statistics
import subprocess
import sys
import time
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

from audit_claim_gate import audit_matrix as audit_claim_gate_matrix  # noqa: E402
from run_buildtest_matrix import (  # noqa: E402
    parse_characterization_summary,
    parse_controller,
    parse_flowmon,
    parse_victim_log,
)


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def summarize_values(values: list[float]) -> dict[str, float]:
    if not values:
        return {
            "n": 0.0,
            "mean": 0.0,
            "std": 0.0,
            "min": 0.0,
            "max": 0.0,
            "p95": 0.0,
        }
    ordered = sorted(values)
    idx95 = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
    return {
        "n": float(len(values)),
        "mean": statistics.mean(values),
        "std": statistics.pstdev(values) if len(values) > 1 else 0.0,
        "min": ordered[0],
        "max": ordered[-1],
        "p95": ordered[idx95],
    }


def parse_json_lines(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def elapsed_from_ns(start_ns: Any, end_ns: Any) -> float:
    start = fnum(start_ns, default=-1.0)
    end = fnum(end_ns, default=-1.0)
    if start < 0.0 or end < 0.0 or end < start:
        return 0.0
    return (end - start) / 1_000_000_000.0


def normalize_stage_name(value: Any) -> str:
    text = str(value or "UNKNOWN").strip()
    return text.upper() if text else "UNKNOWN"


def stage_from_timing_key(key: str) -> str:
    text = key.strip().lower()
    if text.startswith("turn_"):
        parts = text.split("_")
        if len(parts) >= 3:
            return "_".join(parts[2:]).upper()
    if text == "package_after_critical":
        return "PACKAGE_BACKGROUND"
    return normalize_stage_name(key)


def summarize_stage_seconds(stage_seconds: dict[str, list[float]]) -> dict[str, dict[str, float]]:
    summary: dict[str, dict[str, float]] = {}
    for stage, values in sorted(stage_seconds.items()):
        clean = [float(value) for value in values if float(value) >= 0.0]
        if not clean:
            continue
        occurrence = summarize_values(clean)
        summary[stage] = {
            "occurrences": float(len(clean)),
            "total_sec": sum(clean),
            "mean_sec": occurrence["mean"],
            "p95_sec": occurrence["p95"],
            "min_sec": occurrence["min"],
            "max_sec": occurrence["max"],
        }
    return summary


def aggregate_stage_summary(trials: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    stages = sorted(
        {
            stage
            for trial in trials
            for stage in trial.get("workload", {}).get("stage_seconds", {})
        }
    )
    out: dict[str, dict[str, float]] = {}
    for stage in stages:
        totals: list[float] = []
        occurrences: list[float] = []
        for trial in trials:
            values = trial.get("workload", {}).get("stage_seconds", {}).get(stage)
            if not isinstance(values, list) or not values:
                continue
            clean = [float(value) for value in values if float(value) >= 0.0]
            if not clean:
                continue
            totals.append(sum(clean))
            occurrences.extend(clean)
        total_summary = summarize_values(totals)
        occurrence_summary = summarize_values(occurrences)
        out[stage] = {
            "trials": total_summary["n"],
            "occurrences": occurrence_summary["n"],
            "total_mean_sec": total_summary["mean"],
            "total_p95_sec": total_summary["p95"],
            "total_min_sec": total_summary["min"],
            "total_max_sec": total_summary["max"],
            "occurrence_mean_sec": occurrence_summary["mean"],
            "occurrence_p95_sec": occurrence_summary["p95"],
            "occurrence_min_sec": occurrence_summary["min"],
            "occurrence_max_sec": occurrence_summary["max"],
        }
    return out


CHARACTERIZATION_STAGE_KEYS = (
    "sample_count",
    "samples_with_frontier",
    "samples_with_schedstat",
    "frontier_sched_wait_delta_ns",
    "sched_wait_delta_ns",
    "frontier_sched_runtime_delta_ns",
    "sched_runtime_delta_ns",
    "frontier_wait_per_frontier_sample_ns",
    "frontier_wait_per_schedstat_sample_ns",
    "frontier_runtime_per_frontier_sample_ns",
    "frontier_wait_share",
    "frontier_runtime_share",
)


def aggregate_characterization_stage_summary(trials: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    stages = sorted(
        {
            stage
            for trial in trials
            for stage in (
                trial.get("characterization", {}).get("stage_delta_summary", {})
                if isinstance(trial.get("characterization", {}).get("stage_delta_summary"), dict)
                else {}
            )
        }
    )
    out: dict[str, dict[str, float]] = {}
    for stage in stages:
        rows = [
            trial.get("characterization", {}).get("stage_delta_summary", {}).get(stage, {})
            for trial in trials
            if isinstance(trial.get("characterization", {}).get("stage_delta_summary"), dict)
            and isinstance(trial.get("characterization", {}).get("stage_delta_summary", {}).get(stage), dict)
        ]
        stage_out: dict[str, float] = {"trials": float(len(rows))}
        for key in CHARACTERIZATION_STAGE_KEYS:
            values = [fnum(row.get(key)) for row in rows]
            summary = summarize_values(values)
            stage_out[f"{key}_mean"] = summary["mean"]
            stage_out[f"{key}_std"] = summary["std"]
            stage_out[f"{key}_min"] = summary["min"]
            stage_out[f"{key}_max"] = summary["max"]
        out[stage] = stage_out
    return out


def mean_dict(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = sorted({key for row in rows for key, value in row.items() if isinstance(value, (int, float))})
    return {
        key: statistics.mean([float(row.get(key, 0.0) or 0.0) for row in rows])
        for key in keys
    }


def sum_counter_dict(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    merged: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        if not isinstance(value, dict):
            continue
        for item, count in value.items():
            merged[str(item)] = merged.get(str(item), 0) + int(count or 0)
    return dict(sorted(merged.items(), key=lambda item: (-item[1], item[0]))[:20])


def aggregate_controller(rows: list[dict[str, Any]]) -> dict[str, Any]:
    numeric_sum_keys = {
        "records",
        "selected_samples",
        "frontier_records",
        "propagation_records",
        "propagation_anchors",
        "stability_records",
        "stability_selected",
        "stability_held",
        "stability_suppressed",
        "stability_observed_unstable",
        "adaptive_records",
        "adaptive_enabled_records",
        "adaptive_widened_records",
        "adaptive_propagation_records",
        "adaptive_propagation_enabled_records",
        "frontier_action_records",
        "frontier_action_observe_records",
        "frontier_action_policy_fallback_records",
        "build_admission_triggered_records",
    }
    out: dict[str, Any] = {
        key: int(sum(int(row.get(key, 0) or 0) for row in rows))
        for key in numeric_sum_keys
    }
    out["adaptive_max_effective_width"] = int(
        max((int(row.get("adaptive_max_effective_width", 0) or 0) for row in rows), default=0)
    )
    for key in (
        "stage_counts",
        "stage_source_counts",
        "frontier_comm_counts",
        "reason_counts",
        "adaptive_width_counts",
        "adaptive_reason_counts",
        "stability_mode_counts",
        "stability_action_counts",
        "frontier_action_counts",
        "frontier_action_reason_counts",
        "build_admission_mode_counts",
        "build_admission_reason_counts",
        "adaptive_propagation_depth_counts",
        "adaptive_propagation_reason_counts",
        "adaptive_propagation_policy_counts",
        "adaptive_propagation_stage_counts",
    ):
        out[key] = sum_counter_dict(rows, key)
    examples: list[dict[str, Any]] = []
    for row in rows:
        for item in row.get("examples", []) if isinstance(row.get("examples"), list) else []:
            if isinstance(item, dict) and len(examples) < 8:
                examples.append(item)
    out["examples"] = examples
    action_examples: list[dict[str, Any]] = []
    for row in rows:
        source = row.get("frontier_action_examples", [])
        for item in source if isinstance(source, list) else []:
            if isinstance(item, dict) and len(action_examples) < 8:
                action_examples.append(item)
    out["frontier_action_examples"] = action_examples
    return out


def parse_manifest(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_workload_log(path: pathlib.Path) -> dict[str, Any]:
    """Parse optional JSON metric and stage markers emitted by the workload.

    A generic repo/CI command can print lines such as

        {"type":"flowcausal_metric","name":"critical_sec","value":1.23}

    to distinguish user-visible critical-path time from full sandbox cleanup
    time.  Workloads may also emit ``stage_end`` or ``agent_stage_end``
    records.  Those records are not claim-gate inputs by themselves, but they
    let the matrix explain whether a result moved BUILD, TEST, retry-probe, or
    post-critical cleanup time.  The parser is intentionally permissive and
    ignores unrelated log lines so arbitrary build output remains usable.
    """

    metrics: dict[str, float] = {}
    stage_seconds: dict[str, list[float]] = {}
    stage_records: list[dict[str, Any]] = []
    frontier_label_records: list[dict[str, Any]] = []
    workload_begin: dict[str, Any] = {}
    workload_end: dict[str, Any] = {}
    records = parse_json_lines(path)
    for record in records:
        rtype = str(record.get("type", ""))
        if rtype == "workload_begin":
            workload_begin = {
                key: value
                for key, value in record.items()
                if key
                in {
                    "workload",
                    "trace_file",
                    "trace_source",
                    "instance_id",
                    "repo_dir",
                    "work_dir",
                    "stage_out_dir",
                    "stage_state_file",
                    "copy_root",
                    "copy_dst",
                    "in_place",
                    "command_count",
                    "stages",
                    "critical_command_index",
                    "critical_after_stage",
                    "time_ns",
                }
            }
        elif rtype in {"flowcausal_metric", "flowcausal_workload_metric"}:
            name = str(record.get("name", "")).strip()
            if not name:
                continue
            value = fnum(record.get("value"), default=-1.0)
            if value >= 0.0:
                metrics[name] = value
        elif rtype == "flowcausal_critical_path":
            value = fnum(record.get("elapsed_sec", record.get("value")), default=-1.0)
            if value >= 0.0:
                metrics["critical_sec"] = value
        elif rtype in {"stage_end", "agent_stage_end"}:
            stage = normalize_stage_name(record.get("stage"))
            elapsed = fnum(record.get("elapsed_sec"), default=-1.0)
            if elapsed < 0.0:
                continue
            stage_seconds.setdefault(stage, []).append(elapsed)
            stage_record = {
                "stage": stage,
                "elapsed_sec": elapsed,
            }
            if record.get("turn") is not None:
                stage_record["turn"] = record.get("turn")
            if record.get("pid") is not None:
                stage_record["pid"] = record.get("pid")
            if record.get("cwd") is not None:
                stage_record["cwd"] = record.get("cwd")
            if record.get("cmd") is not None:
                stage_record["cmd"] = record.get("cmd")
            if record.get("returncode") is not None:
                stage_record["returncode"] = record.get("returncode")
            if record.get("command_index") is not None:
                stage_record["command_index"] = record.get("command_index")
            if record.get("source_id") is not None:
                stage_record["source_id"] = record.get("source_id")
            stage_records.append(stage_record)
        elif rtype == "frontier_label":
            frontier_record = {
                key: record.get(key)
                for key in ("stage", "turn", "pid", "command_index", "source_id", "cwd", "label")
                if record.get(key) is not None
            }
            frontier_label_records.append(frontier_record)
        elif rtype == "workload_end":
            workload_end = record

    # Older or ad-hoc workloads may only report a final timings map.  Use it as
    # a fallback when explicit stage_end markers are absent to avoid double
    # counting agent_workflow_replay, which emits both stage_end and timings.
    timings = workload_end.get("timings") if isinstance(workload_end.get("timings"), dict) else {}
    if not stage_seconds:
        for key, value in timings.items():
            elapsed = fnum(value, default=-1.0)
            if elapsed < 0.0:
                continue
            stage = stage_from_timing_key(str(key))
            stage_seconds.setdefault(stage, []).append(elapsed)

    workload_elapsed_sec = elapsed_from_ns(workload_begin.get("time_ns"), workload_end.get("time_ns"))

    return {
        "metrics": metrics,
        "workload_begin": workload_begin,
        "workload_end": workload_end,
        "workload_elapsed_sec": workload_elapsed_sec,
        "stage_seconds": {stage: values for stage, values in sorted(stage_seconds.items())},
        "stage_summary": summarize_stage_seconds(stage_seconds),
        "stage_records": stage_records,
        "frontier_label_records": frontier_label_records,
        "frontier_label_count": len(frontier_label_records),
        "records": len(records),
    }


def parse_workload_metrics(path: pathlib.Path) -> dict[str, float]:
    """Backward-compatible wrapper for callers that only need scalar metrics."""

    return dict(parse_workload_log(path).get("metrics", {}))


def shell_command_args(command: str, *, variant: str, trial: int, trial_dir: pathlib.Path) -> list[str]:
    build_out = pathlib.Path("/tmp") / f"flowcausal-repo-ci-{variant}-t{trial}-{int(time.time_ns())}"
    return [
        "env",
        f"FLOWCAUSAL_VARIANT={variant}",
        f"FLOWCAUSAL_TRIAL={trial}",
        f"FLOWCAUSAL_TRIAL_DIR={trial_dir}",
        f"FLOWCAUSAL_BUILD_OUT={build_out}",
        "bash",
        "-lc",
        command,
    ]


def first_cpu(cpu_list: str | None) -> str:
    text = str(cpu_list or "").strip()
    if not text:
        return ""
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start = part.split("-", 1)[0].strip()
            if start.isdigit():
                return start
        elif part.isdigit():
            return part
    return ""


def baseline_command(command: str, variant: dict[str, Any], args: argparse.Namespace) -> str:
    baseline_type = str(variant.get("baseline_type", ""))
    if baseline_type == "nice_priority":
        return " ".join(
            [
                "nice",
                "-n",
                shlex.quote(str(args.nice_value)),
                "bash",
                "-lc",
                shlex.quote(command),
            ]
        )
    if baseline_type == "static_cgroup":
        wrapper = pathlib.Path(args.static_cgroup_wrapper).resolve()
        cmd = []
        if getattr(args, "static_cgroup_sudo", False):
            cmd += [
                "sudo",
                "-n",
                "--preserve-env=FLOWCAUSAL_VARIANT,FLOWCAUSAL_TRIAL,FLOWCAUSAL_TRIAL_DIR,FLOWCAUSAL_BUILD_OUT",
            ]
        cmd += [
            sys.executable,
            str(wrapper),
            "--name-prefix",
            f"flowcausal-{variant_name_safe(variant)}",
        ]
        if args.static_cgroup_cpu_weight:
            cmd += ["--cpu-weight", str(args.static_cgroup_cpu_weight)]
        if args.static_cgroup_cpu_max:
            cmd += ["--cpu-max", args.static_cgroup_cpu_max]
        cmd += ["--", "bash", "-lc", command]
        return shlex.join(cmd)
    return command


def variant_name_safe(variant: dict[str, Any]) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in str(variant.get("name", "variant")))


def variant_taskset(args: argparse.Namespace, variant: dict[str, Any]) -> str | None:
    if variant.get("baseline_type") == "static_affinity":
        configured = str(args.static_affinity_taskset or "").strip()
        if configured:
            return configured
        selected = first_cpu(args.taskset)
        return selected or args.taskset
    return args.taskset


def build_variants(args: argparse.Namespace) -> list[dict[str, Any]]:
    variants: list[dict[str, Any]] = [
        {"name": "default_taskset", "kind": "default"},
    ]
    if args.include_static_baselines:
        if not args.static_affinity_taskset and not args.taskset:
            raise SystemExit(
                "--include-static-baselines requires --taskset or --static-affinity-taskset "
                "so the static_affinity baseline is a real affinity baseline"
            )
        variants.extend(
            [
                {
                    "name": "nice_priority",
                    "kind": "baseline",
                    "baseline_type": "nice_priority",
                    "nice_value": args.nice_value,
                },
                {
                    "name": "static_affinity",
                    "kind": "baseline",
                    "baseline_type": "static_affinity",
                    "taskset": variant_taskset(args, {"baseline_type": "static_affinity"}),
                },
                {
                    "name": "static_cgroup",
                    "kind": "baseline",
                    "baseline_type": "static_cgroup",
                    "cpu_weight": args.static_cgroup_cpu_weight,
                    "cpu_max": args.static_cgroup_cpu_max,
                },
            ]
        )
    if args.include_oracle_baseline:
        variants.append(
            {
                "name": "oracle_frontier",
                "kind": "baseline",
                "baseline_type": "oracle_frontier",
                "frontier_width": args.frontier_width,
                "max_frontier_width": args.max_frontier_width,
                "width_control": True,
            }
        )
    if args.include_shared_only_variant:
        variants.append(
            {
                "name": "flowcausal_shared_only",
                "kind": "flowcausal",
                "frontier_width": args.frontier_width,
                "max_frontier_width": args.max_frontier_width,
                "width_control": False,
                "hint_dry_run": True,
            }
        )
    if not args.skip_no_width_variant:
        variants.append(
            {
                "name": "flowcausal_no_width",
                "kind": "flowcausal",
                "frontier_width": args.frontier_width,
                "max_frontier_width": args.max_frontier_width,
                "width_control": False,
            },
        )
    variants.extend(
        [
            {
                "name": "flowcausal_width",
                "kind": "flowcausal",
                "frontier_width": args.frontier_width,
                "max_frontier_width": args.max_frontier_width,
                "width_control": True,
            },
        ]
    )
    return variants


def run_command(cmd: list[str], *, cwd: pathlib.Path) -> tuple[int, float]:
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd))
    elapsed = time.monotonic() - start
    return proc.returncode, elapsed


def run_default_trial(args: argparse.Namespace, variant: str, trial: int, trial_dir: pathlib.Path) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(HERE / "run_live_sandbox.py"),
        "--out-dir",
        str(trial_dir),
        "--no-build-tools",
        "--no-flowmon",
        "--no-final-snapshot",
        "--hint-dry-run",
        "--stage-kind",
        args.stage_kind,
        "--frontier-width",
        str(args.frontier_width),
        "--min-frontier-score",
        str(args.min_frontier_score),
        "--frontier-action",
        "observe",
        "--interval-ms",
        str(args.interval_ms),
        "--collect-characterization",
        "--characterization-interval-ms",
        str(args.characterization_interval_ms),
        "--characterization-top-candidates",
        str(args.characterization_top_candidates),
    ]
    if args.taskset:
        cmd += ["--taskset", args.taskset]
    if args.victim_command:
        cmd += ["--victim-command", args.victim_command]
    if args.victim_taskset:
        cmd += ["--victim-taskset", args.victim_taskset]
    if args.victim_warmup_sec is not None:
        cmd += ["--victim-warmup-sec", str(args.victim_warmup_sec)]
    cmd += ["--"] + shell_command_args(args.command, variant=variant, trial=trial, trial_dir=trial_dir)
    rc, elapsed = run_command(cmd, cwd=ROOT.parent)
    return parse_trial(trial_dir, rc=rc, elapsed_sec=elapsed, kind="default")


def run_baseline_trial(
    args: argparse.Namespace,
    variant: dict[str, Any],
    trial: int,
    trial_dir: pathlib.Path,
) -> dict[str, Any]:
    if variant.get("baseline_type") == "oracle_frontier":
        return run_oracle_baseline_trial(args, variant, trial, trial_dir)

    trial_dir.mkdir(parents=True, exist_ok=True)
    name = str(variant.get("name", "baseline"))
    cmd = [
        sys.executable,
        str(HERE / "run_live_sandbox.py"),
        "--out-dir",
        str(trial_dir),
        "--no-build-tools",
        "--no-flowmon",
        "--no-final-snapshot",
        "--hint-dry-run",
        "--stage-kind",
        args.stage_kind,
        "--frontier-width",
        str(args.frontier_width),
        "--min-frontier-score",
        str(args.min_frontier_score),
        "--frontier-action",
        "observe",
        "--interval-ms",
        str(args.interval_ms),
        "--collect-characterization",
        "--characterization-interval-ms",
        str(args.characterization_interval_ms),
        "--characterization-top-candidates",
        str(args.characterization_top_candidates),
    ]
    taskset = variant_taskset(args, variant)
    if taskset:
        cmd += ["--taskset", taskset]
    if args.victim_command:
        cmd += ["--victim-command", args.victim_command]
    if args.victim_taskset:
        cmd += ["--victim-taskset", args.victim_taskset]
    if args.victim_warmup_sec is not None:
        cmd += ["--victim-warmup-sec", str(args.victim_warmup_sec)]
    command = baseline_command(args.command, variant, args)
    cmd += ["--"] + shell_command_args(command, variant=name, trial=trial, trial_dir=trial_dir)
    rc, elapsed = run_command(cmd, cwd=ROOT.parent)
    row = parse_trial(trial_dir, rc=rc, elapsed_sec=elapsed, kind="baseline")
    row["baseline_type"] = variant.get("baseline_type", "")
    row["baseline_config"] = {
        key: value
        for key, value in variant.items()
        if key not in {"trials", "aggregate"}
    }
    return row


def run_oracle_baseline_trial(
    args: argparse.Namespace,
    variant: dict[str, Any],
    trial: int,
    trial_dir: pathlib.Path,
) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    name = str(variant.get("name", "oracle_frontier"))
    label_file = trial_dir / "oracle-frontier-labels.jsonl"
    cmd = [
        sys.executable,
        str(HERE / "run_live_sandbox.py"),
        "--out-dir",
        str(trial_dir),
        "--attach-scheduler",
        "--no-build-tools",
        "--hint-dry-run",
        "--oracle-frontier-label-file",
        str(label_file),
        "--oracle-frontier-driver",
        str(HERE / "run_oracle_frontier_driver.py"),
        "--stage-kind",
        args.stage_kind,
        "--frontier-width",
        str(args.frontier_width),
        "--min-frontier-score",
        str(args.min_frontier_score),
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
        "--frontier-dispatch-quota",
        str(args.frontier_dispatch_quota),
        "--guard-cooldown-dispatches",
        str(args.guard_cooldown_dispatches),
        "--pressure-fallback-enqueues",
        str(args.pressure_fallback_enqueues),
        "--pressure-fallback-ms",
        str(args.pressure_fallback_ms),
        "--shared-wait-pressure-us",
        str(args.shared_wait_pressure_us),
        "--pressure-dispatch-ms",
        str(args.pressure_dispatch_ms),
        "--frontier-dispatch-window-ms",
        str(args.frontier_dispatch_window_ms),
        "--frontier-dispatch-window-budget",
        str(args.frontier_dispatch_window_budget),
        "--shared-slice-us",
        str(args.shared_slice_us),
        "--shared-wakeup-slice-us",
        str(args.shared_wakeup_slice_us),
        "--shared-wakeup-min-block-us",
        str(args.shared_wakeup_min_block_us),
        "--collect-characterization",
        "--characterization-interval-ms",
        str(args.characterization_interval_ms),
        "--characterization-top-candidates",
        str(args.characterization_top_candidates),
    ]
    if args.shared_wakeup_preempt:
        cmd += ["--shared-wakeup-preempt"]
    if args.frontier_wakeup_preempt:
        cmd += ["--frontier-wakeup-preempt"]
    if args.no_event_flowmon:
        cmd += ["--no-event-flowmon"]
    if args.shared_local_dsq:
        cmd += ["--shared-local-dsq"]
    if args.tracked_local_dsq:
        cmd += ["--tracked-local-dsq"]
    if args.shared_percpu_dsq:
        cmd += ["--shared-percpu-dsq"]
    if args.background_dsq:
        cmd += ["--background-dsq"]
    if args.background_dispatch_quota:
        cmd += ["--background-dispatch-quota", str(args.background_dispatch_quota)]
    if args.taskset:
        cmd += ["--taskset", args.taskset]
    if args.frontier_propagation:
        cmd += ["--frontier-propagation", "--frontier-propagation-depth", str(args.frontier_propagation_depth)]
    if args.adaptive_frontier_propagation_depth:
        cmd += ["--adaptive-frontier-propagation-depth"]
    if args.stage_aware_frontier_propagation_depth:
        cmd += ["--stage-aware-frontier-propagation-depth"]
    cmd += ["--width-control", "--max-frontier-width", str(args.max_frontier_width)]
    if args.victim_command:
        cmd += ["--victim-command", args.victim_command]
    if args.victim_taskset:
        cmd += ["--victim-taskset", args.victim_taskset]
    if args.victim_warmup_sec is not None:
        cmd += ["--victim-warmup-sec", str(args.victim_warmup_sec)]
    cmd += ["--"] + shell_command_args(args.command, variant=name, trial=trial, trial_dir=trial_dir)
    rc, elapsed = run_command(cmd, cwd=ROOT.parent)
    row = parse_trial(trial_dir, rc=rc, elapsed_sec=elapsed, kind="baseline")
    row["baseline_type"] = "oracle_frontier"
    row["baseline_config"] = {
        "name": name,
        "baseline_type": "oracle_frontier",
        "label_file": str(label_file),
        "width_control": True,
        "max_frontier_width": args.max_frontier_width,
    }
    if int(row.get("workload", {}).get("frontier_label_count", 0) or 0) <= 0:
        row["oracle_frontier_error"] = "no frontier_label records emitted by workload"
        row["returncode"] = 1
        row["workload"]["returncode"] = 1
    oracle_rc = int(row.get("manifest", {}).get("oracle_frontier_driver_returncode", 0) or 0)
    if oracle_rc not in (0, 130, -2, -15):
        row["oracle_frontier_error"] = f"oracle frontier driver failed with returncode {oracle_rc}"
        row["returncode"] = oracle_rc
        row["workload"]["returncode"] = oracle_rc
    return row


def run_flowcausal_trial(
    args: argparse.Namespace,
    variant: str,
    trial: int,
    trial_dir: pathlib.Path,
    *,
    width_control: bool,
    hint_dry_run: bool = False,
) -> dict[str, Any]:
    trial_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(HERE / "run_live_sandbox.py"),
        "--out-dir",
        str(trial_dir),
        "--attach-scheduler",
        "--no-build-tools",
        "--stage-kind",
        args.stage_kind,
        "--frontier-width",
        str(args.frontier_width),
        "--min-frontier-score",
        str(args.min_frontier_score),
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
        "observe" if hint_dry_run else args.frontier_action,
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
        "--frontier-dispatch-quota",
        str(args.frontier_dispatch_quota),
        "--guard-cooldown-dispatches",
        str(args.guard_cooldown_dispatches),
        "--pressure-fallback-enqueues",
        str(args.pressure_fallback_enqueues),
        "--pressure-fallback-ms",
        str(args.pressure_fallback_ms),
        "--shared-wait-pressure-us",
        str(args.shared_wait_pressure_us),
        "--pressure-dispatch-ms",
        str(args.pressure_dispatch_ms),
        "--frontier-dispatch-window-ms",
        str(args.frontier_dispatch_window_ms),
        "--frontier-dispatch-window-budget",
        str(args.frontier_dispatch_window_budget),
        "--shared-slice-us",
        str(args.shared_slice_us),
        "--shared-wakeup-slice-us",
        str(args.shared_wakeup_slice_us),
        "--shared-wakeup-min-block-us",
        str(args.shared_wakeup_min_block_us),
        "--collect-characterization",
        "--characterization-interval-ms",
        str(args.characterization_interval_ms),
        "--characterization-top-candidates",
        str(args.characterization_top_candidates),
    ]
    if args.shared_wakeup_preempt:
        cmd += ["--shared-wakeup-preempt"]
    if args.frontier_wakeup_preempt:
        cmd += ["--frontier-wakeup-preempt"]
    if args.no_event_flowmon:
        cmd += ["--no-event-flowmon"]
    if args.shared_local_dsq:
        cmd += ["--shared-local-dsq"]
    if args.tracked_local_dsq:
        cmd += ["--tracked-local-dsq"]
    if args.shared_percpu_dsq:
        cmd += ["--shared-percpu-dsq"]
    if args.background_dsq:
        cmd += ["--background-dsq"]
    if args.background_dispatch_quota:
        cmd += ["--background-dispatch-quota", str(args.background_dispatch_quota)]
    if hint_dry_run:
        cmd += ["--hint-dry-run"]
    taskset = variant_taskset(args, {"name": variant})
    if taskset:
        cmd += ["--taskset", taskset]
    if args.frontier_propagation:
        cmd += ["--frontier-propagation", "--frontier-propagation-depth", str(args.frontier_propagation_depth)]
    if args.adaptive_frontier_propagation_depth:
        cmd += ["--adaptive-frontier-propagation-depth"]
    if args.stage_aware_frontier_propagation_depth:
        cmd += ["--stage-aware-frontier-propagation-depth"]
    if width_control:
        cmd += ["--width-control", "--max-frontier-width", str(args.max_frontier_width)]
    if args.victim_command:
        cmd += ["--victim-command", args.victim_command]
    if args.victim_taskset:
        cmd += ["--victim-taskset", args.victim_taskset]
    if args.victim_warmup_sec is not None:
        cmd += ["--victim-warmup-sec", str(args.victim_warmup_sec)]
    cmd += ["--"] + shell_command_args(args.command, variant=variant, trial=trial, trial_dir=trial_dir)
    rc, elapsed = run_command(cmd, cwd=ROOT.parent)
    return parse_trial(trial_dir, rc=rc, elapsed_sec=elapsed, kind="flowcausal")


def parse_trial(trial_dir: pathlib.Path, *, rc: int, elapsed_sec: float, kind: str) -> dict[str, Any]:
    manifest = parse_manifest(trial_dir / "manifest.json")
    workload_log = parse_workload_log(trial_dir / "workload.log")
    workload_metrics = workload_log.get("metrics", {})
    workload = {
        "total_sec": elapsed_sec,
        "workload_elapsed_sec": workload_log.get("workload_elapsed_sec", 0.0),
        "returncode": int(manifest.get("workload_returncode", rc) or rc),
        "live_harness_returncode": rc,
        "metrics": workload_metrics,
        "workload_begin": workload_log.get("workload_begin", {}),
        "workload_end": workload_log.get("workload_end", {}),
        "stage_seconds": workload_log.get("stage_seconds", {}),
        "stage_summary": workload_log.get("stage_summary", {}),
        "stage_records": workload_log.get("stage_records", []),
        "frontier_label_records": workload_log.get("frontier_label_records", []),
        "frontier_label_count": workload_log.get("frontier_label_count", 0),
        "records": workload_log.get("records", 0),
    }
    if fnum(workload_metrics.get("critical_sec")) > 0:
        workload["critical_sec"] = fnum(workload_metrics.get("critical_sec"))
    return {
        "path": str(trial_dir),
        "returncode": rc,
        "elapsed_sec": elapsed_sec,
        "kind": kind,
        "manifest": manifest,
        "workload": workload,
        "controller": parse_controller(trial_dir / "controller.ndjson"),
        "flowmon": parse_flowmon(trial_dir / "flowmon-final.log"),
        "victim": parse_victim_log(trial_dir / "victim.log"),
        "internal_noise": parse_victim_log(trial_dir / "internal-noise.log"),
        "characterization": parse_characterization_summary(trial_dir / "characterization" / "summary.json"),
    }


def aggregate_variant(variant: dict[str, Any]) -> dict[str, Any]:
    trials = variant.get("trials", [])
    total_values = [float(t.get("workload", {}).get("total_sec", 0.0) or 0.0) for t in trials]
    workload_elapsed_values = [
        fnum(t.get("workload", {}).get("workload_elapsed_sec"))
        for t in trials
        if fnum(t.get("workload", {}).get("workload_elapsed_sec")) > 0.0
    ]
    critical_values = [
        float(t.get("workload", {}).get("critical_sec", 0.0) or 0.0)
        for t in trials
        if fnum(t.get("workload", {}).get("critical_sec")) > 0
    ]
    frontier_label_counts = [
        float(t.get("workload", {}).get("frontier_label_count", 0.0) or 0.0)
        for t in trials
    ]
    failed_codes: dict[str, int] = {}
    successful = 0
    for trial in trials:
        rc = int(trial.get("returncode", 1) or 0)
        workload_rc = int(trial.get("workload", {}).get("returncode", rc) or rc)
        if rc == 0 and workload_rc == 0:
            successful += 1
        else:
            failed_codes[str(rc or workload_rc)] = failed_codes.get(str(rc or workload_rc), 0) + 1
    total = len(trials)
    return {
        "total_mean_sec": summarize_values(total_values)["mean"],
        "total_summary": summarize_values(total_values),
        "workload_elapsed_mean_sec": summarize_values(workload_elapsed_values)["mean"]
        if workload_elapsed_values
        else 0.0,
        "workload_elapsed_summary": summarize_values(workload_elapsed_values),
        "critical_mean_sec": summarize_values(critical_values)["mean"] if critical_values else 0.0,
        "critical_summary": summarize_values(critical_values),
        "trial_status": {
            "total": total,
            "successful": successful,
            "failed": total - successful,
            "failed_returncodes": failed_codes,
        },
        "workload_frontier_label_count_mean": summarize_values(frontier_label_counts)["mean"],
        "workload_frontier_label_count_min": min(frontier_label_counts) if frontier_label_counts else 0.0,
        "controller": aggregate_controller([t.get("controller", {}) for t in trials]),
        "flowmon_mean": mean_dict([t.get("flowmon", {}) for t in trials]),
        "victim_mean": mean_dict([t.get("victim", {}) for t in trials]),
        "internal_noise_mean": mean_dict([t.get("internal_noise", {}) for t in trials]),
        "characterization_mean": mean_dict([t.get("characterization", {}) for t in trials]),
        "characterization_stage_summary": aggregate_characterization_stage_summary(trials),
        "stage_summary": aggregate_stage_summary(trials),
    }


def write_csv(matrix: dict[str, Any], path: pathlib.Path) -> None:
    rows: list[dict[str, Any]] = []
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        ctl = agg.get("controller", {})
        flow = agg.get("flowmon_mean", {})
        victim = agg.get("victim_mean", {})
        internal_noise = agg.get("internal_noise_mean", {})
        char = agg.get("characterization_mean", {})
        char_stage = agg.get("characterization_stage_summary", {})
        stages = agg.get("stage_summary", {})
        status = agg.get("trial_status", {})
        rows.append(
            {
                "variant": variant["name"],
                "kind": variant["kind"],
                "baseline_type": variant.get("baseline_type", ""),
                "baseline_taskset": variant.get("taskset", ""),
                "baseline_nice_value": variant.get("nice_value", ""),
                "baseline_cgroup_cpu_weight": variant.get("cpu_weight", ""),
                "baseline_cgroup_cpu_max": variant.get("cpu_max", ""),
                "trials": status.get("total", 0),
                "successful_trials": status.get("successful", 0),
                "failed_trials": status.get("failed", 0),
                "total_mean_sec": agg.get("total_mean_sec", 0.0),
                "workload_elapsed_mean_sec": agg.get("workload_elapsed_mean_sec", 0.0),
                "critical_mean_sec": agg.get("critical_mean_sec", 0.0),
                "controller_frontier_records": ctl.get("frontier_records", 0),
                "workload_frontier_label_count_mean": agg.get("workload_frontier_label_count_mean", 0.0),
                "workload_frontier_label_count_min": agg.get("workload_frontier_label_count_min", 0.0),
                "controller_selected_samples": ctl.get("selected_samples", 0),
                "controller_adaptive_propagation_records": ctl.get("adaptive_propagation_records", 0),
                "controller_adaptive_propagation_enabled_records": ctl.get(
                    "adaptive_propagation_enabled_records", 0
                ),
                "controller_adaptive_propagation_depth_counts": json.dumps(
                    ctl.get("adaptive_propagation_depth_counts", {}), sort_keys=True
                ),
                "controller_adaptive_propagation_reason_counts": json.dumps(
                    ctl.get("adaptive_propagation_reason_counts", {}), sort_keys=True
                ),
                "controller_adaptive_propagation_policy_counts": json.dumps(
                    ctl.get("adaptive_propagation_policy_counts", {}), sort_keys=True
                ),
                "controller_adaptive_propagation_stage_counts": json.dumps(
                    ctl.get("adaptive_propagation_stage_counts", {}), sort_keys=True
                ),
                "controller_stage_source_counts": json.dumps(
                    ctl.get("stage_source_counts", {}), sort_keys=True
                ),
                "controller_frontier_action_records": ctl.get("frontier_action_records", 0),
                "controller_frontier_action_observe_records": ctl.get(
                    "frontier_action_observe_records", 0
                ),
                "controller_build_admission_triggered_records": ctl.get(
                    "build_admission_triggered_records", 0
                ),
                "controller_frontier_action_counts": json.dumps(
                    ctl.get("frontier_action_counts", {}), sort_keys=True
                ),
                "controller_build_admission_mode_counts": json.dumps(
                    ctl.get("build_admission_mode_counts", {}), sort_keys=True
                ),
                "controller_build_admission_reason_counts": json.dumps(
                    ctl.get("build_admission_reason_counts", {}), sort_keys=True
                ),
                "frontier_enqueues_mean": flow.get("frontier_enqueues", 0.0),
                "fallback_enqueues_mean": flow.get("fallback_enqueues", 0.0),
                "width_held_enqueues_mean": flow.get("width_held_enqueues", 0.0),
                "frontier_wakeup_preempt_entries_mean": flow.get(
                    "global_frontier_wakeup_preempt_entries", 0.0
                ),
                "shared_wakeup_preempt_entries_mean": flow.get(
                    "global_shared_wakeup_preempt_entries", 0.0
                ),
                "background_enqueues_mean": flow.get("global_background_enqueues", 0.0),
                "background_dispatches_mean": flow.get("global_background_dispatches", 0.0),
                "frontier_runnable_wait_ms_mean": flow.get("frontier_runnable_wait_ns", 0.0) / 1_000_000.0,
                "frontier_runtime_ms_mean": flow.get("frontier_runtime_ns", 0.0) / 1_000_000.0,
                "char_samples_mean": char.get("sample_count", 0.0),
                "char_frontier_samples_mean": char.get("samples_with_frontier", 0.0),
                "char_frontier_wait_ms_mean": char.get("frontier_sched_wait_delta_ns", 0.0) / 1_000_000.0,
                "char_frontier_wait_per_frontier_sample_ms_mean": char.get(
                    "frontier_wait_per_frontier_sample_ns", 0.0
                )
                / 1_000_000.0,
                "char_stage_build_wait_per_frontier_sample_ms_mean": fnum(
                    char_stage.get("BUILD", {}).get("frontier_wait_per_frontier_sample_ns_mean")
                    if isinstance(char_stage.get("BUILD"), dict)
                    else 0.0
                )
                / 1_000_000.0,
                "char_stage_test_wait_per_frontier_sample_ms_mean": fnum(
                    char_stage.get("TEST", {}).get("frontier_wait_per_frontier_sample_ns_mean")
                    if isinstance(char_stage.get("TEST"), dict)
                    else 0.0
                )
                / 1_000_000.0,
                "stage_inspect_total_mean_sec": fnum(
                    stages.get("INSPECT", {}).get("total_mean_sec") if isinstance(stages.get("INSPECT"), dict) else 0.0
                ),
                "stage_edit_total_mean_sec": fnum(
                    stages.get("EDIT", {}).get("total_mean_sec") if isinstance(stages.get("EDIT"), dict) else 0.0
                ),
                "stage_build_total_mean_sec": fnum(
                    stages.get("BUILD", {}).get("total_mean_sec") if isinstance(stages.get("BUILD"), dict) else 0.0
                ),
                "stage_test_total_mean_sec": fnum(
                    stages.get("TEST", {}).get("total_mean_sec") if isinstance(stages.get("TEST"), dict) else 0.0
                ),
                "stage_test_occurrence_mean_sec": fnum(
                    stages.get("TEST", {}).get("occurrence_mean_sec") if isinstance(stages.get("TEST"), dict) else 0.0
                ),
                "stage_retry_probe_total_mean_sec": fnum(
                    stages.get("RETRY_PROBE", {}).get("total_mean_sec")
                    if isinstance(stages.get("RETRY_PROBE"), dict)
                    else 0.0
                ),
                "stage_package_background_total_mean_sec": fnum(
                    stages.get("PACKAGE_BACKGROUND", {}).get("total_mean_sec")
                    if isinstance(stages.get("PACKAGE_BACKGROUND"), dict)
                    else 0.0
                ),
                "victim_lateness_p95_ms_mean": victim.get("lateness_p95_ms", 0.0),
                "victim_lateness_p99_ms_mean": victim.get("lateness_p99_ms", 0.0),
                "victim_lateness_max_ms_mean": victim.get("lateness_max_ms", 0.0),
                "internal_noise_iterations_mean": internal_noise.get("iterations", 0.0),
                "internal_noise_lateness_p95_ms_mean": internal_noise.get("lateness_p95_ms", 0.0),
                "internal_noise_lateness_p99_ms_mean": internal_noise.get("lateness_p99_ms", 0.0),
                "internal_noise_lateness_max_ms_mean": internal_noise.get("lateness_max_ms", 0.0),
            }
        )
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(matrix: dict[str, Any], path: pathlib.Path) -> None:
    lines = [
        "# FlowCausal repo/CI live matrix",
        "",
        "This matrix wraps an arbitrary repository/CI shell command.  It is intended",
        "to move FlowCausal from local synthetic/Test16 evidence toward real repo",
        "workloads.  The claim-gate audit is authoritative for outcome/safety level.",
        "",
        "## Configuration",
        "",
        "```json",
        json.dumps(matrix.get("config", {}), indent=2, sort_keys=True),
        "```",
        "",
        "| Variant | Trials ok/fail | Critical sec | Workload elapsed | Harness total | Labels | Frontier rec/enq | Fallback/width-held | Wakeup preempt F/S | Background enq/disp | Char wait/sample ms | Victim p95/p99/max ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for variant in matrix["variants"]:
        agg = variant["aggregate"]
        status = agg.get("trial_status", {})
        ctl = agg.get("controller", {})
        flow = agg.get("flowmon_mean", {})
        char = agg.get("characterization_mean", {})
        victim = agg.get("victim_mean", {})
        victim_cell = "-"
        if fnum(victim.get("iterations")) > 0:
            victim_cell = "{:.3f}/{:.3f}/{:.3f}".format(
                fnum(victim.get("lateness_p95_ms")),
                fnum(victim.get("lateness_p99_ms")),
                fnum(victim.get("lateness_max_ms")),
            )
        lines.append(
            "| {name} | {ok}/{fail} | {critical} | {workload_elapsed:.3f} | {total:.3f} | {labels:.1f} | {rec}/{enq:.1f} | {fallback:.1f}/{width:.1f} | {fwake:.1f}/{swake:.1f} | {bgenq:.1f}/{bgdisp:.1f} | {wait:.3f} | {victim} |".format(
                name=variant["name"],
                ok=int(status.get("successful", 0)),
                fail=int(status.get("failed", 0)),
                critical=(
                    "{:.3f}".format(fnum(agg.get("critical_mean_sec")))
                    if fnum(agg.get("critical_mean_sec")) > 0
                    else "-"
                ),
                total=fnum(agg.get("total_mean_sec")),
                workload_elapsed=fnum(agg.get("workload_elapsed_mean_sec")),
                labels=fnum(agg.get("workload_frontier_label_count_mean")),
                rec=int(ctl.get("frontier_records", 0) or 0),
                enq=fnum(flow.get("frontier_enqueues")),
                fallback=fnum(flow.get("fallback_enqueues")),
                width=fnum(flow.get("width_held_enqueues")),
                fwake=fnum(flow.get("global_frontier_wakeup_preempt_entries")),
                swake=fnum(flow.get("global_shared_wakeup_preempt_entries")),
                bgenq=fnum(flow.get("global_background_enqueues")),
                bgdisp=fnum(flow.get("global_background_dispatches")),
                wait=fnum(char.get("frontier_wait_per_frontier_sample_ns")) / 1_000_000.0,
                victim=victim_cell,
            )
        )
    has_baseline_variants = any(variant.get("kind") == "baseline" for variant in matrix["variants"])
    if has_baseline_variants:
        lines.extend(
            [
                "",
                "## Static baseline configuration",
                "",
                "| Variant | Baseline type | Taskset | Nice value | cgroup cpu.weight | cgroup cpu.max |",
                "| --- | --- | --- | ---: | ---: | --- |",
            ]
        )
        for variant in matrix["variants"]:
            if variant.get("kind") != "baseline":
                continue
            lines.append(
                "| {name} | {kind} | {taskset} | {nice} | {weight} | {cpu_max} |".format(
                    name=variant.get("name", ""),
                    kind=variant.get("baseline_type", ""),
                    taskset=variant.get("taskset", "") or "-",
                    nice=variant.get("nice_value", "") if variant.get("nice_value", "") != "" else "-",
                    weight=variant.get("cpu_weight", "") if variant.get("cpu_weight", "") != "" else "-",
                    cpu_max=variant.get("cpu_max", "") or "-",
                )
            )
    has_characterization_stage_summary = any(
        bool(variant.get("aggregate", {}).get("characterization_stage_summary"))
        for variant in matrix["variants"]
    )
    if has_characterization_stage_summary:
        lines.extend(
            [
                "",
                "## Direct schedstat by inferred stage",
                "",
                "This table splits the characterization sampler's `/proc/<pid>/schedstat`",
                "deltas by the stage inferred by `flowctl` for each process-tree sample.",
                "It is diagnostic evidence and does not by itself promote the claim gate.",
                "",
                "| Variant | Stage | Samples | Frontier samples | Schedstat samples | Frontier wait ms | Wait/frontier sample ms | Total wait ms | Frontier wait share |",
                "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )
        for variant in matrix["variants"]:
            stage_summary = variant.get("aggregate", {}).get("characterization_stage_summary", {})
            if not isinstance(stage_summary, dict):
                continue
            for stage, data in sorted(stage_summary.items()):
                if not isinstance(data, dict):
                    continue
                samples = fnum(data.get("sample_count_mean"))
                frontier_samples = fnum(data.get("samples_with_frontier_mean"))
                schedstat_samples = fnum(data.get("samples_with_schedstat_mean"))
                if samples <= 0.0:
                    continue
                lines.append(
                    "| {name} | {stage} | {samples:.1f} | {frontier_samples:.1f} | {schedstat_samples:.1f} | {frontier_wait:.3f} | {wait_per_frontier:.3f} | {total_wait:.3f} | {share:.3f} |".format(
                        name=variant["name"],
                        stage=stage,
                        samples=samples,
                        frontier_samples=frontier_samples,
                        schedstat_samples=schedstat_samples,
                        frontier_wait=fnum(data.get("frontier_sched_wait_delta_ns_mean")) / 1_000_000.0,
                        wait_per_frontier=fnum(data.get("frontier_wait_per_frontier_sample_ns_mean")) / 1_000_000.0,
                        total_wait=fnum(data.get("sched_wait_delta_ns_mean")) / 1_000_000.0,
                        share=fnum(data.get("frontier_wait_share_mean")),
                    )
                )
    has_stage_source = any(
        bool(variant.get("aggregate", {}).get("controller", {}).get("stage_source_counts"))
        for variant in matrix["variants"]
    )
    if has_stage_source:
        lines.extend(
            [
                "",
                "## Controller stage source",
                "",
                "This table audits whether live controller stages came from a workload",
                "state bridge, a forced CLI stage, or process-command inference.",
                "",
                "| Variant | Stage counts | Stage source counts |",
                "| --- | --- | --- |",
            ]
        )
        for variant in matrix["variants"]:
            ctl = variant.get("aggregate", {}).get("controller", {})
            lines.append(
                "| {name} | `{stages}` | `{sources}` |".format(
                    name=variant["name"],
                    stages=json.dumps(ctl.get("stage_counts", {}), sort_keys=True),
                    sources=json.dumps(ctl.get("stage_source_counts", {}), sort_keys=True),
                )
            )
    has_adaptive_propagation = any(
        int(variant.get("aggregate", {}).get("controller", {}).get("adaptive_propagation_records", 0) or 0) > 0
        for variant in matrix["variants"]
    )
    if has_adaptive_propagation:
        lines.extend(
            [
                "",
                "## Adaptive/stage-aware propagation depth",
                "",
                "This table summarizes controller-emitted `adaptive_propagation` records.",
                "It is mechanism evidence for the policy bridge, not an endpoint claim by itself.",
                "",
                "| Variant | Records/enabled | Policy counts | Stage counts | Depth counts | Reason counts |",
                "| --- | ---: | --- | --- | --- | --- |",
            ]
        )
        for variant in matrix["variants"]:
            ctl = variant.get("aggregate", {}).get("controller", {})
            records = int(ctl.get("adaptive_propagation_records", 0) or 0)
            if records <= 0:
                continue
            lines.append(
                "| {name} | {records}/{enabled} | `{policies}` | `{stages}` | `{depths}` | `{reasons}` |".format(
                    name=variant["name"],
                    records=records,
                    enabled=int(ctl.get("adaptive_propagation_enabled_records", 0) or 0),
                    policies=json.dumps(ctl.get("adaptive_propagation_policy_counts", {}), sort_keys=True),
                    stages=json.dumps(ctl.get("adaptive_propagation_stage_counts", {}), sort_keys=True),
                    depths=json.dumps(ctl.get("adaptive_propagation_depth_counts", {}), sort_keys=True),
                    reasons=json.dumps(ctl.get("adaptive_propagation_reason_counts", {}), sort_keys=True),
                )
            )
    has_frontier_action = any(
        int(variant.get("aggregate", {}).get("controller", {}).get("frontier_action_records", 0) or 0) > 0
        for variant in matrix["variants"]
    )
    if has_frontier_action:
        lines.extend(
            [
                "",
                "## Frontier action/admission",
                "",
                "| Variant | Action records | Observe records | BUILD admission triggers | Action counts | BUILD modes | BUILD reasons |",
                "| --- | ---: | ---: | ---: | --- | --- | --- |",
            ]
        )
        for variant in matrix["variants"]:
            ctl = variant.get("aggregate", {}).get("controller", {})
            records = int(ctl.get("frontier_action_records", 0) or 0)
            if records <= 0:
                continue
            lines.append(
                "| {name} | {records} | {observe} | {build_triggers} | `{actions}` | `{modes}` | `{reasons}` |".format(
                    name=variant["name"],
                    records=records,
                    observe=int(ctl.get("frontier_action_observe_records", 0) or 0),
                    build_triggers=int(ctl.get("build_admission_triggered_records", 0) or 0),
                    actions=json.dumps(ctl.get("frontier_action_counts", {}), sort_keys=True),
                    modes=json.dumps(ctl.get("build_admission_mode_counts", {}), sort_keys=True),
                    reasons=json.dumps(ctl.get("build_admission_reason_counts", {}), sort_keys=True),
                )
            )
    has_internal_noise = any(
        fnum(variant.get("aggregate", {}).get("internal_noise_mean", {}).get("iterations")) > 0
        for variant in matrix["variants"]
    )
    if has_internal_noise:
        lines.extend(
            [
                "",
                "## Sandbox-internal background task",
                "",
                "If the workload command writes `internal-noise.log`, this table summarizes that sandbox-internal",
                "background task separately from the external victim sidecar.  High lateness here is evidence of",
                "intentional background demotion, not victim-safety evidence.",
                "",
                "| Variant | Iterations | Internal p95/p99/max lateness ms |",
                "| --- | ---: | ---: |",
            ]
        )
        for variant in matrix["variants"]:
            internal_noise = variant.get("aggregate", {}).get("internal_noise_mean", {})
            if fnum(internal_noise.get("iterations")) <= 0:
                continue
            lines.append(
                "| {name} | {iters:.1f} | {p95:.3f}/{p99:.3f}/{maxv:.3f} |".format(
                    name=variant["name"],
                    iters=fnum(internal_noise.get("iterations")),
                    p95=fnum(internal_noise.get("lateness_p95_ms")),
                    p99=fnum(internal_noise.get("lateness_p99_ms")),
                    maxv=fnum(internal_noise.get("lateness_max_ms")),
                )
            )
    has_stage_summary = any(
        bool(variant.get("aggregate", {}).get("stage_summary"))
        for variant in matrix["variants"]
    )
    if has_stage_summary:
        lines.extend(
            [
                "",
                "## Workload stage timing",
                "",
                "These stage metrics are parsed from workload-emitted JSON markers.  They explain where",
                "the endpoint changed, but the claim-gate still decides whether the scheduler mechanism",
                "and safety evidence are strong enough for a performance claim.",
                "",
                "| Variant | INSPECT total | EDIT total | BUILD total | TEST total | TEST mean/run | RETRY total | PACKAGE_BG total |",
                "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
        )

        def stage_total(variant: dict[str, Any], stage: str) -> float:
            data = variant.get("aggregate", {}).get("stage_summary", {}).get(stage, {})
            return fnum(data.get("total_mean_sec")) if isinstance(data, dict) else 0.0

        def stage_occurrence(variant: dict[str, Any], stage: str) -> float:
            data = variant.get("aggregate", {}).get("stage_summary", {}).get(stage, {})
            return fnum(data.get("occurrence_mean_sec")) if isinstance(data, dict) else 0.0

        for variant in matrix["variants"]:
            lines.append(
                "| {name} | {inspect:.3f} | {edit:.3f} | {build:.3f} | {test:.3f} | {test_run:.3f} | {retry:.3f} | {package:.3f} |".format(
                    name=variant["name"],
                    inspect=stage_total(variant, "INSPECT"),
                    edit=stage_total(variant, "EDIT"),
                    build=stage_total(variant, "BUILD"),
                    test=stage_total(variant, "TEST"),
                    test_run=stage_occurrence(variant, "TEST"),
                    retry=stage_total(variant, "RETRY_PROBE"),
                    package=stage_total(variant, "PACKAGE_BACKGROUND"),
                )
            )
    audit = matrix.get("claim_gate_audit", {}) if isinstance(matrix.get("claim_gate_audit"), dict) else {}
    lines.extend(
        [
            "",
            "## Claim-gate audit",
            "",
            f"- Gate highest supported level: `{audit.get('highest_supported_level', 'unknown')}`.",
            "",
            "| Gate | Status | Evidence | Blockers |",
            "| --- | --- | --- | --- |",
        ]
    )
    for item in audit.get("gates", []):
        if not isinstance(item, dict):
            continue
        evidence = "<br>".join(str(x) for x in item.get("evidence", [])) or "-"
        blockers = "<br>".join(str(x) for x in item.get("blockers", [])) or "-"
        lines.append(f"| {item.get('name', '')} | {item.get('status', '')} | {evidence} | {blockers} |")
    lines.extend(["", "Forbidden claims:", ""])
    for claim in audit.get("forbidden_claims", []):
        lines.append(f"- {claim}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a generic repo/CI command under default and FlowCausal rows",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--label", default="repo-ci")
    parser.add_argument("--command", required=True, help="shell command run by bash -lc")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--taskset", default=None)
    parser.add_argument("--stage-kind", default="AUTO")
    parser.add_argument("--frontier-width", type=int, default=2)
    parser.add_argument("--max-frontier-width", type=int, default=2)
    parser.add_argument("--min-frontier-score", type=int, default=50)
    parser.add_argument("--frontier-propagation", action="store_true")
    parser.add_argument("--frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--adaptive-frontier-propagation-depth", action="store_true")
    parser.add_argument("--stage-aware-frontier-propagation-depth", action="store_true")
    parser.add_argument("--frontier-stability-samples", type=int, default=1)
    parser.add_argument("--frontier-hold-ms", type=int, default=0)
    parser.add_argument("--frontier-bypass-score", type=int, default=140)
    parser.add_argument("--frontier-selected-stability", action="store_true")
    parser.add_argument("--frontier-stability-action", choices=("suppress", "observe"), default="suppress")
    parser.add_argument("--frontier-action", choices=("advance", "auto", "observe", "stage-aware"), default="advance")
    parser.add_argument("--build-frontier-admission", choices=("advance", "tail-only", "observe"), default="advance")
    parser.add_argument("--confidence", type=int, default=75)
    parser.add_argument("--ttl-ms", type=int, default=1000)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=100)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--characterization-interval-ms", type=int, default=50)
    parser.add_argument("--characterization-top-candidates", type=int, default=8)
    parser.add_argument("--frontier-dispatch-quota", type=int, default=4)
    parser.add_argument("--guard-cooldown-dispatches", type=int, default=0)
    parser.add_argument("--pressure-fallback-enqueues", type=int, default=0)
    parser.add_argument("--pressure-fallback-ms", type=int, default=0)
    parser.add_argument("--shared-wait-pressure-us", type=int, default=0)
    parser.add_argument("--pressure-dispatch-ms", type=int, default=0)
    parser.add_argument("--frontier-dispatch-window-ms", type=int, default=0)
    parser.add_argument("--frontier-dispatch-window-budget", type=int, default=0)
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
        "--no-event-flowmon",
        action="store_true",
        help="for FlowCausal variants, skip continuous flowmon --events and keep only final flowmon --once",
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
            "this many foreground dispatches; 0 drains background only after "
            "latency/frontier/shared are empty"
        ),
    )
    parser.add_argument("--victim-command", default=None)
    parser.add_argument("--victim-taskset", default=None)
    parser.add_argument("--victim-warmup-sec", type=float, default=0.2)
    parser.add_argument(
        "--include-shared-only-variant",
        action="store_true",
        help="add a FlowCausal-attached row where flowctl is dry-run and writes no task hints",
    )
    parser.add_argument(
        "--include-static-baselines",
        action="store_true",
        help="add nice_priority, static_affinity, and static_cgroup baseline rows",
    )
    parser.add_argument(
        "--include-oracle-baseline",
        action="store_true",
        help=(
            "add an oracle_frontier baseline row that replays workload-emitted "
            "frontier_label records as flowctl --frontier-pid hints"
        ),
    )
    parser.add_argument(
        "--nice-value",
        type=int,
        default=-5,
        help="nice adjustment for the nice_priority baseline; negative values may require privilege",
    )
    parser.add_argument(
        "--static-affinity-taskset",
        default=None,
        help=(
            "CPU list for the static_affinity baseline. If omitted, the first CPU "
            "from --taskset is used; if --taskset is also absent, no extra pinning is applied."
        ),
    )
    parser.add_argument(
        "--static-cgroup-cpu-weight",
        type=int,
        default=10000,
        help="cpu.weight for the static_cgroup baseline; 0 leaves the kernel default",
    )
    parser.add_argument(
        "--static-cgroup-cpu-max",
        default="",
        help="optional cpu.max for the static_cgroup baseline, e.g. '50000 100000'",
    )
    parser.add_argument(
        "--static-cgroup-wrapper",
        default=str(HERE / "run_static_cgroup.py"),
        help="wrapper script used by the static_cgroup baseline",
    )
    parser.add_argument(
        "--static-cgroup-sudo",
        action="store_true",
        help=(
            "run the static_cgroup wrapper through sudo -n so it can create "
            "cgroups; the wrapper drops the benchmark child back to SUDO_UID "
            "when sudo provides that identity"
        ),
    )
    parser.add_argument(
        "--skip-no-width-variant",
        action="store_true",
        help=(
            "omit the flowcausal_no_width ablation row; useful for reproducing "
            "a specific width-control candidate without letting an intentionally "
            "unsafe ablation dominate the matrix-level safety gate"
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir)
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"{out_dir} exists; pass --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    config = {
        key: value
        for key, value in vars(args).items()
        if key not in {"overwrite"}
    }
    variants = build_variants(args)
    for variant in variants:
        trial_rows: list[dict[str, Any]] = []
        for trial in range(args.trials):
            trial_dir = out_dir / variant["name"] / f"trial-{trial:02d}"
            print(f"[repo-ci] variant={variant['name']} trial={trial}", flush=True)
            if variant["kind"] == "default":
                row = run_default_trial(args, variant["name"], trial, trial_dir)
            elif variant["kind"] == "baseline":
                row = run_baseline_trial(args, variant, trial, trial_dir)
            else:
                row = run_flowcausal_trial(
                    args,
                    variant["name"],
                    trial,
                    trial_dir,
                    width_control=bool(variant.get("width_control", False)),
                    hint_dry_run=bool(variant.get("hint_dry_run", False)),
                )
            trial_rows.append(row)
        variant["trials"] = trial_rows
        variant["aggregate"] = aggregate_variant(variant)

    matrix = {
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "warning": (
            "Generic repo/CI matrix. Do not claim FlowCausal speedup without "
            "claim-gate admission, direct mechanism, outcome, and safety gates."
        ),
        "config": config,
        "variants": variants,
    }
    matrix["claim_gate_audit"] = audit_claim_gate_matrix(matrix)
    (out_dir / "matrix-summary.json").write_text(
        json.dumps(matrix, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_csv(matrix, out_dir / "matrix-summary.csv")
    write_markdown(matrix, out_dir / "README.md")
    print(f"summary={out_dir / 'matrix-summary.json'}")
    print(f"readme={out_dir / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

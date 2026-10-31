#!/usr/bin/env python3
"""Audit HarnessReplayBench matrix mechanism evidence.

This audit sits above the structural replay fidelity audit.  It answers a
different question: after a matrix run, did a FlowCausal variant actually close
the controller -> BPF datapath -> endpoint evidence chain?
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import statistics
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_buildtest_matrix import parse_controller, parse_flowmon  # noqa: E402


SCHEMA = "flowcausal.harness_replay_mechanism_audit.v1"


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def mean(values: list[float]) -> float:
    return statistics.mean(values) if values else 0.0


def sum_numeric(rows: list[dict[str, Any]], key: str) -> float:
    return sum(fnum(row.get(key)) for row in rows)


def mean_numeric(rows: list[dict[str, Any]], key: str) -> float:
    return mean([fnum(row.get(key)) for row in rows])


def merge_counter(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for row in rows:
        value = row.get(key)
        if not isinstance(value, dict):
            continue
        for item, count in value.items():
            counter[str(item)] += int(count or 0)
    return dict(counter)


def group_action_reason(reason: str) -> str:
    if reason.startswith("weak-test-worker-frontier"):
        return "weak-test-worker-frontier"
    return reason


def merge_grouped_counter(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter: collections.Counter[str] = collections.Counter()
    for row in rows:
        value = row.get(key)
        if not isinstance(value, dict):
            continue
        for item, count in value.items():
            counter[group_action_reason(str(item))] += int(count or 0)
    return dict(counter)


def ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def format_counts(counts: dict[str, Any], limit: int = 4) -> str:
    if not counts:
        return ""
    items = sorted(
        ((str(key), int(value or 0)) for key, value in counts.items()),
        key=lambda item: (-item[1], item[0]),
    )
    rendered = ", ".join(f"{key}={value}" for key, value in items[:limit])
    if len(items) > limit:
        rendered += ", ..."
    return rendered


def read_json(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def resolve_trial_path(trial: dict[str, Any], suffix: str) -> pathlib.Path:
    run_dir = pathlib.Path(str(trial.get("run_dir") or trial.get("out_dir") or "."))
    return run_dir / suffix


def flowmon_path(trial: dict[str, Any]) -> pathlib.Path:
    live = trial.get("flowcausal_live") if isinstance(trial.get("flowcausal_live"), dict) else {}
    text = live.get("flowmon_final_log") if isinstance(live, dict) else None
    if text:
        return pathlib.Path(str(text))
    return resolve_trial_path(trial, "flowcausal-live/flowmon-final.log")


def controller_path_for_case(trial: dict[str, Any], case: dict[str, Any]) -> pathlib.Path:
    controller = case.get("controller") if isinstance(case.get("controller"), dict) else {}
    text = controller.get("json_log") if isinstance(controller, dict) else None
    if text:
        return pathlib.Path(str(text))
    run_dir = case.get("run_dir")
    if run_dir:
        return pathlib.Path(str(run_dir)) / "controller.ndjson"
    return resolve_trial_path(trial, f"runs/{case.get('instance_id', 'unknown')}/controller.ndjson")


def is_observe_variant(variant: dict[str, Any]) -> bool:
    opts = variant.get("variant_options") if isinstance(variant.get("variant_options"), dict) else {}
    return (
        str(opts.get("hint_dry_run", "")).lower() in {"1", "true", "yes", "on"}
        or str(opts.get("frontier_action", "")).lower() == "observe"
    )


def is_no_controller_variant(variant: dict[str, Any]) -> bool:
    opts = variant.get("variant_options") if isinstance(variant.get("variant_options"), dict) else {}
    return str(opts.get("no_controller", opts.get("disable_controller", ""))).lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def is_width_variant(variant: dict[str, Any]) -> bool:
    opts = variant.get("variant_options") if isinstance(variant.get("variant_options"), dict) else {}
    return str(opts.get("width_control", "")).lower() in {"1", "true", "yes", "on"}


def audit_trial(trial: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    kind = str(variant.get("kind") or trial.get("kind") or "baseline")
    fidelity_status = str(trial.get("fidelity", {}).get("status", "missing"))
    endpoint = {
        "status": trial.get("status"),
        "makespan_sec": trial.get("makespan_sec"),
        "mean_critical_sec": trial.get("aggregate", {}).get("mean_critical_sec"),
        "p95_critical_sec": trial.get("aggregate", {}).get("p95_critical_sec"),
        "failed_stage_commands": trial.get("aggregate", {}).get("failed_stage_commands"),
    }
    blockers: list[str] = []
    warnings: list[str] = []
    if trial.get("status") != "pass":
        blockers.append("trial status is not pass")
    if fidelity_status != "pass":
        blockers.append("structural fidelity is not pass")

    controller_rows: list[dict[str, Any]] = []
    controller_cases: list[dict[str, Any]] = []
    for case in trial.get("cases", []):
        if not isinstance(case, dict):
            continue
        controller_meta = case.get("controller") if isinstance(case.get("controller"), dict) else {}
        controller_path = controller_path_for_case(trial, case)
        parsed = parse_controller(controller_path)
        controller_rows.append(parsed)
        root_pid = controller_meta.get("root_pid") if isinstance(controller_meta, dict) else None
        status = controller_meta.get("status", "") if isinstance(controller_meta, dict) else ""
        controller_cases.append(
            {
                "instance_id": case.get("instance_id"),
                "status": status,
                "returncode": controller_meta.get("returncode") if isinstance(controller_meta, dict) else None,
                "root_pid": root_pid,
                "json_log": str(controller_path),
                "records": parsed.get("records", 0),
                "frontier_records": parsed.get("frontier_records", 0),
                "selected_samples": parsed.get("selected_samples", 0),
            }
        )
    action_records = sum_numeric(controller_rows, "frontier_action_records")
    action_observe_records = sum_numeric(controller_rows, "frontier_action_observe_records")
    action_policy_fallback_records = sum_numeric(
        controller_rows, "frontier_action_policy_fallback_records"
    )
    action_counts = merge_counter(controller_rows, "frontier_action_counts")
    action_reason_counts = merge_counter(controller_rows, "frontier_action_reason_counts")
    action_reason_group_counts = merge_grouped_counter(
        controller_rows, "frontier_action_reason_counts"
    )
    controller_summary = {
        "cases": len(controller_cases),
        "records": sum_numeric(controller_rows, "records"),
        "selected_samples": sum_numeric(controller_rows, "selected_samples"),
        "frontier_records": sum_numeric(controller_rows, "frontier_records"),
        "propagation_records": sum_numeric(controller_rows, "propagation_records"),
        "propagation_anchors": sum_numeric(controller_rows, "propagation_anchors"),
        "frontier_action_records": action_records,
        "frontier_action_observe_records": action_observe_records,
        "frontier_action_policy_fallback_records": action_policy_fallback_records,
        "frontier_action_observe_ratio": ratio(action_observe_records, action_records),
        "frontier_action_advance_ratio": ratio(fnum(action_counts.get("advance")), action_records),
        "build_admission_triggered_records": sum_numeric(
            controller_rows, "build_admission_triggered_records"
        ),
        "stage_source_counts": merge_counter(controller_rows, "stage_source_counts"),
        "frontier_action_counts": action_counts,
        "frontier_action_reason_counts": action_reason_counts,
        "frontier_action_reason_group_counts": action_reason_group_counts,
        "build_admission_mode_counts": merge_counter(controller_rows, "build_admission_mode_counts"),
        "build_admission_reason_counts": merge_counter(controller_rows, "build_admission_reason_counts"),
        "weak_test_worker_frontier_records": fnum(
            action_reason_group_counts.get("weak-test-worker-frontier")
        ),
        "frontier_comm_counts": merge_counter(controller_rows, "frontier_comm_counts"),
        "cases_detail": controller_cases,
    }

    flow = parse_flowmon(flowmon_path(trial)) if kind == "flowcausal" else {}
    flow_summary = {
        key: flow.get(key, 0)
        for key in [
            "policy_sessions",
            "seen_enqueues",
            "frontier_enqueues",
            "fallback_enqueues",
            "width_held_enqueues",
            "frontier_width_releases",
            "global_frontier_dispatches",
            "global_shared_dispatches",
            "global_shared_local_enqueues",
            "global_shared_percpu_enqueues",
            "global_shared_percpu_dispatches",
            "global_shared_percpu_misses",
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
            "frontier_runnable_wait_ns",
            "frontier_runtime_ns",
            "budget_used_ns",
            "budget_total_ns",
            "frontier_ratio",
            "fallback_ratio",
        ]
    }

    live_manifest = {}
    live = trial.get("flowcausal_live") if isinstance(trial.get("flowcausal_live"), dict) else {}
    if isinstance(live.get("manifest"), dict):
        live_manifest = live["manifest"]
    no_controller_variant = is_no_controller_variant(variant) or live_manifest.get("controller_enabled") is False

    if kind == "flowcausal":
        if live_manifest.get("scheduler_returncode") not in (0, None):
            blockers.append("scheduler returncode is nonzero")
        if live_manifest.get("flowmon_final_returncode") not in (0, None):
            blockers.append("flowmon final snapshot failed")
        if live_manifest.get("sched_ext_state_after") not in ("disabled", None):
            blockers.append("sched_ext did not return to disabled")
        if bool(trial.get("per_case_flowctl")) and not no_controller_variant:
            missing_roots = [
                case.get("instance_id")
                for case in controller_cases
                if not int(case.get("root_pid") or 0) > 0
            ]
            if missing_roots:
                blockers.append(f"per-case controller missing root pid for {missing_roots}")
            if controller_summary["records"] <= 0:
                blockers.append("per-case controller produced no records")
            if controller_summary["frontier_records"] <= 0:
                blockers.append("per-case controller selected no frontier records")
        if not no_controller_variant and not is_observe_variant(variant):
            if fnum(flow_summary.get("frontier_enqueues")) <= 0:
                blockers.append("BPF frontier_enqueues is zero")
            if fnum(flow_summary.get("global_frontier_dispatches")) <= 0:
                blockers.append("BPF global_frontier_dispatches is zero")
            if fnum(flow_summary.get("frontier_runtime_ns")) <= 0:
                blockers.append("BPF frontier_runtime_ns is zero")
        if is_width_variant(variant) and fnum(flow_summary.get("width_held_enqueues")) > 0:
            warnings.append("width-control held some frontier enqueues; inspect admission turbulence")

    direct_status = "baseline"
    if kind == "flowcausal":
        direct_status = "pass" if not blockers else "fail"
        if no_controller_variant and not blockers:
            direct_status = "overhead-baseline"
        if is_observe_variant(variant) and not blockers:
            direct_status = "observe-only"

    return {
        "trial": trial.get("trial"),
        "status": "pass" if not blockers else "fail",
        "direct_status": direct_status,
        "blockers": blockers,
        "warnings": warnings,
        "endpoint": endpoint,
        "fidelity_status": fidelity_status,
        "controller": controller_summary,
        "flowmon": flow_summary,
    }


def classify_variant(variant: dict[str, Any], trials: list[dict[str, Any]]) -> str:
    kind = str(variant.get("kind", "baseline"))
    if kind != "flowcausal":
        return "baseline"
    if is_no_controller_variant(variant):
        return "overhead-baseline"
    if is_observe_variant(variant):
        return "observe-only"
    comp = variant.get("comparison_to_baseline") if isinstance(variant.get("comparison_to_baseline"), dict) else {}
    makespan_delta = fnum(comp.get("makespan_delta"), 0.0)
    mean_delta = fnum(comp.get("mean_critical_delta"), 0.0)
    p95_delta = fnum(comp.get("p95_critical_delta"), 0.0)
    all_direct = all(trial.get("direct_status") == "pass" for trial in trials)
    if not all_direct:
        return "mechanism-incomplete"
    if mean_delta < 0 and makespan_delta <= 0 and p95_delta <= 0:
        if is_width_variant(variant) and mean_numeric([t["flowmon"] for t in trials], "width_held_enqueues") > 0:
            return "candidate-with-width-risk"
        return "candidate"
    if mean_delta < 0:
        return "mixed-endpoint"
    return "risk-boundary"


def audit_variant(variant: dict[str, Any]) -> dict[str, Any]:
    trial_audits = [audit_trial(trial, variant) for trial in variant.get("trials", []) if isinstance(trial, dict)]
    flow_rows = [trial.get("flowmon", {}) for trial in trial_audits]
    controller_rows = [trial.get("controller", {}) for trial in trial_audits]
    blockers = [
        blocker
        for trial in trial_audits
        for blocker in trial.get("blockers", [])
    ]
    warnings = [
        warning
        for trial in trial_audits
        for warning in trial.get("warnings", [])
    ]
    summary = {
        "trials": len(trial_audits),
        "pass_trials": sum(1 for trial in trial_audits if trial.get("status") == "pass"),
        "direct_pass_trials": sum(1 for trial in trial_audits if trial.get("direct_status") == "pass"),
        "observe_only_trials": sum(1 for trial in trial_audits if trial.get("direct_status") == "observe-only"),
        "overhead_baseline_trials": sum(
            1 for trial in trial_audits if trial.get("direct_status") == "overhead-baseline"
        ),
        "controller_records_mean": mean_numeric(controller_rows, "records"),
        "controller_frontier_records_mean": mean_numeric(controller_rows, "frontier_records"),
        "controller_propagation_anchors_mean": mean_numeric(controller_rows, "propagation_anchors"),
        "controller_frontier_action_records_mean": mean_numeric(
            controller_rows, "frontier_action_records"
        ),
        "controller_frontier_action_observe_records_mean": mean_numeric(
            controller_rows, "frontier_action_observe_records"
        ),
        "controller_frontier_action_policy_fallback_records_mean": mean_numeric(
            controller_rows, "frontier_action_policy_fallback_records"
        ),
        "controller_frontier_action_observe_ratio_mean": mean_numeric(
            controller_rows, "frontier_action_observe_ratio"
        ),
        "controller_frontier_action_advance_ratio_mean": mean_numeric(
            controller_rows, "frontier_action_advance_ratio"
        ),
        "controller_weak_test_worker_frontier_records_mean": mean_numeric(
            controller_rows, "weak_test_worker_frontier_records"
        ),
        "controller_frontier_action_counts": merge_counter(
            controller_rows, "frontier_action_counts"
        ),
        "controller_frontier_action_reason_counts": merge_counter(
            controller_rows, "frontier_action_reason_counts"
        ),
        "controller_frontier_action_reason_group_counts": merge_counter(
            controller_rows, "frontier_action_reason_group_counts"
        ),
        "controller_build_admission_mode_counts": merge_counter(
            controller_rows, "build_admission_mode_counts"
        ),
        "controller_build_admission_reason_counts": merge_counter(
            controller_rows, "build_admission_reason_counts"
        ),
        "frontier_enqueues_mean": mean_numeric(flow_rows, "frontier_enqueues"),
        "fallback_enqueues_mean": mean_numeric(flow_rows, "fallback_enqueues"),
        "width_held_enqueues_mean": mean_numeric(flow_rows, "width_held_enqueues"),
        "global_frontier_dispatches_mean": mean_numeric(flow_rows, "global_frontier_dispatches"),
        "global_shared_dispatches_mean": mean_numeric(flow_rows, "global_shared_dispatches"),
        "global_shared_local_enqueues_mean": mean_numeric(flow_rows, "global_shared_local_enqueues"),
        "global_shared_percpu_enqueues_mean": mean_numeric(flow_rows, "global_shared_percpu_enqueues"),
        "global_shared_percpu_dispatches_mean": mean_numeric(flow_rows, "global_shared_percpu_dispatches"),
        "global_shared_percpu_misses_mean": mean_numeric(flow_rows, "global_shared_percpu_misses"),
        "global_frontier_dsq_enqueues_mean": mean_numeric(flow_rows, "global_frontier_dsq_enqueues"),
        "global_tracked_shared_path_enqueues_mean": mean_numeric(flow_rows, "global_tracked_shared_path_enqueues"),
        "global_untracked_shared_path_enqueues_mean": mean_numeric(
            flow_rows, "global_untracked_shared_path_enqueues"
        ),
        "global_shared_global_enqueues_mean": mean_numeric(flow_rows, "global_shared_global_enqueues"),
        "global_shared_global_tracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_global_tracked_enqueues"
        ),
        "global_shared_global_untracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_global_untracked_enqueues"
        ),
        "global_latency_dsq_enqueues_mean": mean_numeric(flow_rows, "global_latency_dsq_enqueues"),
        "global_shared_local_tracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_local_tracked_enqueues"
        ),
        "global_shared_local_untracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_local_untracked_enqueues"
        ),
        "global_shared_percpu_tracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_percpu_tracked_enqueues"
        ),
        "global_shared_percpu_untracked_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_percpu_untracked_enqueues"
        ),
        "global_shared_local_pick_fallbacks_mean": mean_numeric(flow_rows, "global_shared_local_pick_fallbacks"),
        "global_shared_percpu_pick_fallbacks_mean": mean_numeric(
            flow_rows, "global_shared_percpu_pick_fallbacks"
        ),
        "global_shared_local_frontier_fallback_enqueues_mean": mean_numeric(
            flow_rows, "global_shared_local_frontier_fallback_enqueues"
        ),
        "frontier_runnable_wait_ns_mean": mean_numeric(flow_rows, "frontier_runnable_wait_ns"),
        "frontier_runtime_ns_mean": mean_numeric(flow_rows, "frontier_runtime_ns"),
    }
    status = "pass" if not blockers else "fail"
    return {
        "name": variant.get("name"),
        "kind": variant.get("kind", "baseline"),
        "variant_options": variant.get("variant_options", {}),
        "status": status,
        "classification": classify_variant(variant, trial_audits),
        "comparison_to_baseline": variant.get("comparison_to_baseline", {}),
        "matrix_summary": variant.get("summary", {}),
        "mechanism_summary": summary,
        "blockers": blockers,
        "warnings": warnings,
        "trials": trial_audits,
    }


def write_markdown(path: pathlib.Path, payload: dict[str, Any]) -> None:
    lines = [
        "# HarnessReplayBench mechanism audit",
        "",
        f"- status: `{payload['status']}`",
        f"- matrix: `{payload['matrix_summary']}`",
        "",
        "| variant | class | status | makespan Δ | mean critical Δ | p95 critical Δ | ctrl frontier mean | ctrl actions | observe ratio | weak TEST reason mean | BPF frontier mean | fallback mean | width-held mean |",
        "|---|---|---|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|",
    ]
    for variant in payload["variants"]:
        comp = variant.get("comparison_to_baseline", {})
        mech = variant.get("mechanism_summary", {})
        lines.append(
            f"| `{variant['name']}` | `{variant['classification']}` | `{variant['status']}` | "
            f"{comp.get('makespan_delta', '')} | {comp.get('mean_critical_delta', '')} | "
            f"{comp.get('p95_critical_delta', '')} | "
            f"{mech.get('controller_frontier_records_mean', '')} | "
            f"{format_counts(mech.get('controller_frontier_action_counts', {}))} | "
            f"{mech.get('controller_frontier_action_observe_ratio_mean', '')} | "
            f"{mech.get('controller_weak_test_worker_frontier_records_mean', '')} | "
            f"{mech.get('frontier_enqueues_mean', '')} | "
            f"{mech.get('fallback_enqueues_mean', '')} | "
            f"{mech.get('width_held_enqueues_mean', '')} |"
        )
    lines.append("")
    for variant in payload["variants"]:
        mech = variant.get("mechanism_summary", {})
        reason_groups = mech.get("controller_frontier_action_reason_group_counts", {})
        build_reasons = mech.get("controller_build_admission_reason_counts", {})
        if reason_groups or build_reasons:
            lines.append(f"## {variant['name']} action evidence")
            lines.append("")
            if reason_groups:
                lines.append(f"- action reasons: {format_counts(reason_groups, limit=8)}")
            if build_reasons:
                lines.append(f"- build admission reasons: {format_counts(build_reasons, limit=8)}")
            lines.append("")
    for variant in payload["variants"]:
        if variant.get("blockers") or variant.get("warnings"):
            lines.append(f"## {variant['name']}")
            lines.append("")
            if variant.get("blockers"):
                lines.append("Blockers:")
                lines.append("")
                for item in variant["blockers"]:
                    lines.append(f"- {item}")
                lines.append("")
            if variant.get("warnings"):
                lines.append("Warnings:")
                lines.append("")
                for item in variant["warnings"]:
                    lines.append(f"- {item}")
                lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit FlowCausal controller/BPF mechanism evidence in a HarnessReplayBench matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--matrix-summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    matrix_path = pathlib.Path(args.matrix_summary).resolve()
    matrix = read_json(matrix_path)
    variants = [audit_variant(variant) for variant in matrix.get("variants", []) if isinstance(variant, dict)]
    status = "pass" if all(variant.get("status") == "pass" for variant in variants) else "fail"
    payload = {
        "schema": SCHEMA,
        "status": status,
        "matrix_summary": str(matrix_path),
        "matrix_status": matrix.get("status"),
        "variants": variants,
    }
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        write_markdown(pathlib.Path(args.markdown_output).resolve(), payload)
    print(json.dumps({"status": status, "output": str(output)}, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

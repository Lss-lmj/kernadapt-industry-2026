#!/usr/bin/env python3
"""Run the post-execution pipeline for real-agent full-harness matrices.

This driver starts only after ``run_real_agent_harness_matrix.py --execute`` or
``import_real_agent_harness_matrix.py`` has produced one or more
``harness-matrix-summary.json`` files.  It turns those endpoint summaries into
paper-gated artifacts:

* per-instance full-harness endpoint audits;
* optional per-instance bundle manifests for the selected harness trial;
* a multi-instance harness-audit summary that the evidence dashboard can read;
* a refreshed evidence dashboard.

It is intentionally strict about the evidence boundary.  If no executed matrix
summaries exist, or if the matrices fail the endpoint gates, the report fails
honestly and does not create fake endpoint evidence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from audit_real_agent_harness_matrix import (  # noqa: E402
    as_dict,
    as_list,
    summarize as summarize_matrix_audit,
    write_markdown as write_matrix_audit_markdown,
)
from export_harness_matrix_bundle_manifest import summarize as summarize_bundle_export  # noqa: E402
from summarize_real_agent_evidence import summarize as summarize_dashboard  # noqa: E402
from summarize_real_agent_evidence import latex_table, markdown as dashboard_markdown  # noqa: E402


SCHEMA = "flowcausal.real_agent_postmatrix_pipeline.v1"
COLLECTION_SCHEMA = "flowcausal.real_agent_harness_matrix_audit_collection.v1"
CHAIN_NAMES = (
    "frontier correctness",
    "endpoint improvement",
    "scheduler mechanism",
    "safety/fairness",
    "baseline comparison",
    "reproducibility/artifact",
)
STATUS_ORDER = {"pass": 3, "partial": 2, "fail": 1, "missing": 0}


def norm_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in STATUS_ORDER else "missing"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON report is not an object: {path}")
    return payload


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def existing_optional_path(path_text: str) -> str:
    if not path_text:
        return ""
    path = pathlib.Path(path_text).expanduser().resolve()
    return str(path) if path.exists() else ""


def discover_matrix_summaries(matrix_root: pathlib.Path, explicit: list[str]) -> list[pathlib.Path]:
    if explicit:
        paths = [pathlib.Path(item).expanduser().resolve() for item in explicit]
    elif matrix_root.exists():
        paths = sorted(matrix_root.glob("*/harness-matrix-summary.json"))
    else:
        paths = []
    return [path for path in paths if path.exists()]


def matrix_dir_for(summary_path: pathlib.Path) -> pathlib.Path:
    return summary_path.expanduser().resolve().parent


def default_repo_dir(summary: dict[str, Any], manifest_path: pathlib.Path, repo_root: pathlib.Path) -> str:
    if manifest_path.exists():
        manifest = read_json(manifest_path)
        repo_dir = str(manifest.get("repo_dir") or "").strip()
        if repo_dir:
            return repo_dir
    instance_id = str(summary.get("instance_id") or "").strip()
    return str((repo_root / instance_id).resolve()) if instance_id else ""


def audit_args(args: argparse.Namespace, summary_path: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(
        summary=str(summary_path),
        baseline_variant=args.baseline_variant,
        flowcausal_variant=args.flowcausal_variant,
        min_trials=args.min_trials,
        min_improvement_pct=args.min_improvement_pct,
        no_path_check=args.no_path_check,
        require_victim_safety=args.require_victim_safety,
        safety_baseline_variant=args.safety_baseline_variant,
        safety_flowcausal_variant=args.safety_flowcausal_variant,
        max_victim_p99_slowdown_pct=args.max_victim_p99_slowdown_pct,
        max_victim_max_slowdown_pct=args.max_victim_max_slowdown_pct,
        max_victim_p99_ms=args.max_victim_p99_ms,
        max_victim_max_ms=args.max_victim_max_ms,
    )


def export_args(
    args: argparse.Namespace,
    *,
    summary_path: pathlib.Path,
    audit_path: pathlib.Path,
    manifest_path: pathlib.Path,
    repo_dir: str,
    output: pathlib.Path,
    report: pathlib.Path,
) -> SimpleNamespace:
    return SimpleNamespace(
        summary=str(summary_path),
        harness_matrix_audit=str(audit_path),
        harness_matrix_manifest=str(manifest_path) if manifest_path.exists() else "",
        repo_dir=repo_dir,
        output=str(output),
        report=str(report),
        source_variant=args.export_source_variant,
        trial_selection=args.trial_selection,
        raw_format=args.raw_format,
        source="real-agent-harness-matrix",
        agent_repo_root=args.agent_repo_root,
        taskset=args.taskset,
        trials=args.trials,
        annotator=args.annotator,
        annotation_source=args.annotation_source,
        frontier_annotations="",
        replay_matrix_summary="",
        bundle_out_dir=str(output.parent / "bundles"),
        allow_partial_audit=args.allow_partial_audit_export,
        allow_unresolved=args.allow_unresolved_export,
        allow_weak_endpoint=args.allow_weak_endpoint_export,
    )


def status_for_counts(counts: dict[str, int], *, min_instances: int) -> str:
    if counts.get("pass", 0) >= min_instances:
        return "pass"
    if counts.get("pass", 0) or counts.get("partial", 0):
        return "partial"
    if counts.get("fail", 0):
        return "fail"
    return "missing"


def collapse_chain_reports(audits: list[dict[str, Any]], *, min_instances: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for name in CHAIN_NAMES:
        counts = {status: 0 for status in STATUS_ORDER}
        evidence: list[str] = []
        blockers: list[str] = []
        next_actions: list[str] = []
        for audit in audits:
            instance_id = str(as_dict(audit.get("summary")).get("instance_id") or "<unknown>")
            row = next((as_dict(item) for item in as_list(audit.get("chains")) if as_dict(item).get("name") == name), {})
            status = norm_status(row.get("status"))
            counts[status] += 1
            if row:
                evidence.append(f"{instance_id}:{status}")
                blockers.extend(f"{instance_id}: {item}" for item in as_list(row.get("blockers"))[:2])
                next_actions.extend(str(item) for item in as_list(row.get("next_actions"))[:2])
            else:
                blockers.append(f"{instance_id}: chain missing in matrix audit")
        status = status_for_counts(counts, min_instances=min_instances)
        if counts["pass"] < min_instances:
            blockers.insert(0, f"{name} pass instances {counts['pass']} < required {min_instances}")
        rows.append(
            {
                "name": name,
                "status": status,
                "counts": counts,
                "evidence": evidence[:20],
                "blockers": list(dict.fromkeys(blockers))[:20],
                "next_actions": list(dict.fromkeys(action for action in next_actions if action))[:8],
            }
        )
    return rows


def collapse_gate_reports(audits: list[dict[str, Any]], *, min_instances: int) -> list[dict[str, Any]]:
    gate_names: list[str] = []
    for audit in audits:
        for row in as_list(audit.get("gates")):
            name = str(as_dict(row).get("name") or "")
            if name and name not in gate_names:
                gate_names.append(name)
    out: list[dict[str, Any]] = []
    for name in gate_names:
        counts = {status: 0 for status in STATUS_ORDER}
        blockers: list[str] = []
        evidence: list[str] = []
        next_actions: list[str] = []
        for audit in audits:
            instance_id = str(as_dict(audit.get("summary")).get("instance_id") or "<unknown>")
            row = next((as_dict(item) for item in as_list(audit.get("gates")) if as_dict(item).get("name") == name), {})
            status = norm_status(row.get("status"))
            counts[status] += 1
            evidence.append(f"{instance_id}:{status}")
            blockers.extend(f"{instance_id}: {item}" for item in as_list(row.get("blockers"))[:2])
            next_actions.extend(str(item) for item in as_list(row.get("next_actions"))[:2])
        status = status_for_counts(counts, min_instances=min_instances)
        if counts["pass"] < min_instances:
            blockers.insert(0, f"{name} pass instances {counts['pass']} < required {min_instances}")
        out.append(
            {
                "name": name,
                "status": status,
                "counts": counts,
                "evidence": evidence[:20],
                "blockers": list(dict.fromkeys(blockers))[:20],
                "next_actions": list(dict.fromkeys(action for action in next_actions if action))[:8],
            }
        )
    return out


def aggregate_audits(
    audits: list[dict[str, Any]],
    *,
    output: pathlib.Path,
    min_instances: int,
    min_trials: int,
) -> dict[str, Any]:
    gates = collapse_gate_reports(audits, min_instances=min_instances)
    chains = collapse_chain_reports(audits, min_instances=min_instances)
    pass_audits = sum(1 for audit in audits if norm_status(audit.get("status")) == "pass")
    partial_audits = sum(1 for audit in audits if norm_status(audit.get("status")) == "partial")
    fail_audits = sum(1 for audit in audits if norm_status(audit.get("status")) == "fail")
    if pass_audits >= min_instances and all(row.get("status") == "pass" for row in chains):
        status = "pass"
        highest = "multi-instance full-harness endpoint evidence"
    elif pass_audits or partial_audits:
        status = "partial"
        highest = "partial multi-instance full-harness endpoint evidence"
    elif fail_audits:
        status = "fail"
        highest = "not endpoint-ready"
    else:
        status = "missing"
        highest = "not endpoint-ready"
    blockers: list[str] = []
    if pass_audits < min_instances:
        blockers.append(f"passing matrix audits {pass_audits} < required {min_instances}")
    for row in [*gates, *chains]:
        if row.get("status") != "pass":
            blockers.extend(str(item) for item in as_list(row.get("blockers"))[:2])
    return {
        "schema": COLLECTION_SCHEMA,
        "status": status,
        "highest_supported_scope": highest,
        "summary": {
            "input_summary": str(output),
            "instance_id": f"{len(audits)} matrix instance(s)",
            "instances": len(audits),
            "pass_audits": pass_audits,
            "partial_audits": partial_audits,
            "fail_audits": fail_audits,
            "baseline_variant": "multi-instance",
            "flowcausal_variant": "multi-instance",
            "min_instances": min_instances,
            "min_trials": min_trials,
        },
        "gates": gates,
        "chains": chains,
        "blockers": list(dict.fromkeys(blockers))[:40],
        "notes": [
            "Generated by run_real_agent_postmatrix_pipeline.py from per-instance full-harness matrix audits.",
            "This aggregate is dashboard input; final EuroSys claims still require L5 bundle collection audits.",
        ],
    }


def empty_aggregate(*, output: pathlib.Path, min_instances: int, min_trials: int) -> dict[str, Any]:
    return aggregate_audits([], output=output, min_instances=min_instances, min_trials=min_trials) | {
        "status": "fail",
        "highest_supported_scope": "not endpoint-ready",
        "blockers": ["no harness-matrix-summary.json files found"],
    }


def dashboard_args(args: argparse.Namespace, *, aggregate_audit: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(
        readiness_report=existing_optional_path(args.readiness_report),
        capture_plan_preflight=existing_optional_path(args.capture_plan_preflight),
        harness_matrix_plan=existing_optional_path(args.harness_matrix_plan),
        harness_matrix_runbook_preflight=existing_optional_path(args.harness_matrix_runbook_preflight),
        bootstrap_report=existing_optional_path(args.bootstrap_report),
        repo_prep_report=existing_optional_path(args.repo_prep_report),
        harness_matrix_audit=str(aggregate_audit) if aggregate_audit.exists() else "",
        repo_stage_input=list(args.repo_stage_input or []),
        repo_stage_recursive=args.repo_stage_recursive,
        mechanism_smoke_input=list(args.mechanism_smoke_input or []),
        mechanism_smoke_recursive=args.mechanism_smoke_recursive,
        intake_report=existing_optional_path(args.intake_report),
        preflight_report=existing_optional_path(args.preflight_report),
        collection_audit=existing_optional_path(args.collection_audit),
        bundle_input=list(args.bundle_input or []),
        recursive=args.recursive,
        min_ready_bundles=args.min_instances,
        output=str(pathlib.Path(args.dashboard_output).expanduser().resolve()),
        markdown_output=str(pathlib.Path(args.dashboard_markdown_output).expanduser().resolve()) if args.dashboard_markdown_output else "",
        latex_output=str(pathlib.Path(args.latex_output).expanduser().resolve()) if args.latex_output else "",
    )


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    matrix_root = pathlib.Path(args.matrix_root).expanduser().resolve()
    repo_root = pathlib.Path(args.repo_root).expanduser().resolve()
    report_output = pathlib.Path(args.report_output or (matrix_root.parent / "postmatrix-pipeline.current.json")).expanduser().resolve()
    aggregate_output = pathlib.Path(args.aggregate_audit_output or (matrix_root.parent / "harness-matrix-audit.collection.current.json")).expanduser().resolve()
    dashboard_output = pathlib.Path(args.dashboard_output or (matrix_root.parent / "evidence-dashboard.current.json")).expanduser().resolve()
    args.dashboard_output = str(dashboard_output)

    summary_paths = discover_matrix_summaries(matrix_root, list(args.summary or []))
    rows: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    export_reports: list[dict[str, Any]] = []

    for summary_path in summary_paths:
        matrix_dir = matrix_dir_for(summary_path)
        audit_path = matrix_dir / "harness-matrix-audit.current.json"
        audit_md = matrix_dir / "harness-matrix-audit.current.md"
        audit = summarize_matrix_audit(audit_args(args, summary_path))
        write_json(audit_path, audit)
        write_matrix_audit_markdown(audit, str(audit_md))
        audits.append(audit)

        summary = read_json(summary_path)
        manifest_path = matrix_dir / "real-agent-harness-matrix-manifest.json"
        export_manifest = matrix_dir / "harness-matrix-bundle-manifest.json"
        export_report_path = matrix_dir / "harness-matrix-bundle-export.current.json"
        repo_dir = default_repo_dir(summary, manifest_path, repo_root)
        export_status = "skipped"
        export_blockers: list[str] = []
        if not args.skip_bundle_export:
            manifest, export_report, export_code = summarize_bundle_export(
                export_args(
                    args,
                    summary_path=summary_path,
                    audit_path=audit_path,
                    manifest_path=manifest_path,
                    repo_dir=repo_dir,
                    output=export_manifest,
                    report=export_report_path,
                )
            )
            write_json(export_manifest, manifest)
            write_json(export_report_path, export_report)
            export_status = str(export_report.get("status") or ("pass" if export_code == 0 else "fail"))
            export_blockers = [str(item) for item in as_list(export_report.get("blockers"))]
            export_reports.append(export_report)

        rows.append(
            {
                "instance_id": str(summary.get("instance_id") or as_dict(audit.get("summary")).get("instance_id") or summary_path.parent.name),
                "status": norm_status(audit.get("status")),
                "summary": str(summary_path),
                "audit": str(audit_path),
                "audit_markdown": str(audit_md),
                "bundle_manifest": str(export_manifest) if not args.skip_bundle_export else "",
                "bundle_export_report": str(export_report_path) if not args.skip_bundle_export else "",
                "bundle_export_status": export_status,
                "bundle_export_blockers": export_blockers[:10],
            }
        )

    if audits:
        aggregate = aggregate_audits(audits, output=aggregate_output, min_instances=args.min_instances, min_trials=args.min_trials)
    else:
        aggregate = empty_aggregate(output=aggregate_output, min_instances=args.min_instances, min_trials=args.min_trials)
    write_json(aggregate_output, aggregate)

    dash_args = dashboard_args(args, aggregate_audit=aggregate_output)
    dashboard = summarize_dashboard(dash_args)
    write_json(dashboard_output, dashboard)
    if dash_args.markdown_output:
        markdown_path = pathlib.Path(dash_args.markdown_output)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(dashboard_markdown(dashboard), encoding="utf-8")
    if dash_args.latex_output:
        latex_path = pathlib.Path(dash_args.latex_output)
        latex_path.parent.mkdir(parents=True, exist_ok=True)
        latex_path.write_text(latex_table(dashboard), encoding="utf-8")

    blockers: list[str] = []
    if not summary_paths:
        blockers.append("no harness-matrix-summary.json files found")
    blockers.extend(str(item) for item in as_list(aggregate.get("blockers"))[:20])
    blockers.extend(str(item) for item in as_list(dashboard.get("blockers"))[:20])
    export_pass = sum(1 for report in export_reports if norm_status(report.get("status")) == "pass")
    status = "pass" if dashboard.get("status") == "pass" else "partial" if audits else "fail"
    report = {
        "schema": SCHEMA,
        "status": status,
        "matrix_root": str(matrix_root),
        "artifacts": {
            "aggregate_harness_matrix_audit": str(aggregate_output),
            "dashboard": str(dashboard_output),
            "dashboard_markdown": str(pathlib.Path(args.dashboard_markdown_output).expanduser().resolve()) if args.dashboard_markdown_output else "",
            "latex": str(pathlib.Path(args.latex_output).expanduser().resolve()) if args.latex_output else "",
            "report": str(report_output),
        },
        "summary": {
            "matrix_summaries": len(summary_paths),
            "matrix_audits": len(audits),
            "pass_audits": int(as_dict(aggregate.get("summary")).get("pass_audits") or 0),
            "partial_audits": int(as_dict(aggregate.get("summary")).get("partial_audits") or 0),
            "fail_audits": int(as_dict(aggregate.get("summary")).get("fail_audits") or 0),
            "bundle_exports": len(export_reports),
            "bundle_export_pass": export_pass,
            "dashboard_status": dashboard.get("status"),
            "dashboard_scope": dashboard.get("highest_supported_scope"),
        },
        "rows": rows,
        "blockers": list(dict.fromkeys(blocker for blocker in blockers if blocker))[:60],
        "notes": [
            "This pipeline consumes already executed full-harness matrices; it does not run SWE-agent/OpenHands itself.",
            "A passing postmatrix pipeline still needs enough L5 bundle audits before the final paper claim is EuroSys-ready.",
        ],
    }
    write_json(report_output, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit executed real-agent full-harness matrices and refresh FlowCausal evidence dashboard",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--matrix-root", default="flowcausal/experiments/results/real-agent-captures/full-harness-matrices")
    parser.add_argument("--repo-root", default="flowcausal/benchmarks/harness-replaybench/repos")
    parser.add_argument("--summary", action="append", default=[], help="explicit harness-matrix-summary.json; repeatable")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--min-trials", type=int, default=3)
    parser.add_argument("--min-improvement-pct", type=float, default=2.0)
    parser.add_argument("--baseline-variant", default="")
    parser.add_argument("--flowcausal-variant", default="")
    parser.add_argument("--no-path-check", action="store_true")
    parser.add_argument("--require-victim-safety", action="store_true")
    parser.add_argument("--safety-baseline-variant", default="")
    parser.add_argument("--safety-flowcausal-variant", default="")
    parser.add_argument("--max-victim-p99-slowdown-pct", type=float, default=25.0)
    parser.add_argument("--max-victim-max-slowdown-pct", type=float, default=50.0)
    parser.add_argument("--max-victim-p99-ms", type=float, default=0.0)
    parser.add_argument("--max-victim-max-ms", type=float, default=0.0)
    parser.add_argument("--skip-bundle-export", action="store_true")
    parser.add_argument("--export-source-variant", default="audited-flowcausal")
    parser.add_argument("--trial-selection", choices=("first-pass", "median-elapsed", "fastest", "slowest"), default="first-pass")
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--agent-repo-root", default="/testbed")
    parser.add_argument("--taskset", default="0-1")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", default="manual", choices=("manual", "oracle"))
    parser.add_argument("--allow-partial-audit-export", action="store_true")
    parser.add_argument("--allow-unresolved-export", action="store_true")
    parser.add_argument("--allow-weak-endpoint-export", action="store_true")
    parser.add_argument("--aggregate-audit-output")
    parser.add_argument("--dashboard-output")
    parser.add_argument("--dashboard-markdown-output")
    parser.add_argument("--latex-output")
    parser.add_argument("--readiness-report", default="flowcausal/experiments/results/real-agent-captures/readiness.current.json")
    parser.add_argument("--capture-plan-preflight", default="flowcausal/experiments/results/real-agent-captures/capture-plan-preflight.current.json")
    parser.add_argument("--harness-matrix-plan", default="flowcausal/experiments/results/real-agent-captures/harness-matrix-plan.current.json")
    parser.add_argument("--harness-matrix-runbook-preflight", default="flowcausal/experiments/results/real-agent-captures/harness-matrix-runbook-preflight.current.json")
    parser.add_argument("--bootstrap-report", default="flowcausal/experiments/results/real-agent-harness-current/bootstrap-report.json")
    parser.add_argument("--repo-prep-report", default="flowcausal/experiments/results/real-agent-captures/repo-prep.json")
    parser.add_argument("--intake-report", default="flowcausal/experiments/results/real-agent-captures/intake.current.json")
    parser.add_argument("--preflight-report", default="flowcausal/experiments/results/real-agent-captures/manifest-preflight.current.json")
    parser.add_argument("--collection-audit", default="")
    parser.add_argument("--bundle-input", action="append", default=[])
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--repo-stage-input", action="append", default=[])
    parser.add_argument("--repo-stage-recursive", action="store_true")
    parser.add_argument("--mechanism-smoke-input", action="append", default=[])
    parser.add_argument("--mechanism-smoke-recursive", action="store_true")
    parser.add_argument("--report-output")
    parser.add_argument("--strict", action="store_true", help="exit non-zero unless dashboard is EuroSys paper-ready")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_instances <= 0:
        raise SystemExit("--min-instances must be positive")
    if args.min_trials <= 0:
        raise SystemExit("--min-trials must be positive")
    report = run_pipeline(args)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if (not args.strict or report.get("status") == "pass") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

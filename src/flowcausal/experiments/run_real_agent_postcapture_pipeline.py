#!/usr/bin/env python3
"""Run the post-capture evidence pipeline for real code-agent workloads.

After ``capture_real_agent_run.py`` archives SWE-bench/SWE-agent/OpenHands
artifacts, this driver performs the boring handoff steps in one reproducible
command:

1. discover raw/repo/endpoint triples and write ``real-agent-manifest.json``;
2. preflight that manifest for bundle preparation;
3. refresh the evidence dashboard with intake/preflight state included.

The driver is intentionally conservative.  It can produce failing reports for
an empty capture directory, but it never upgrades missing captures into endpoint
evidence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from types import SimpleNamespace
from typing import Any

from build_real_agent_manifest import build_manifest
from summarize_real_agent_evidence import summarize as summarize_dashboard
from validate_bundle_collection_manifest import validate_manifest


SCHEMA = "flowcausal.real_agent_postcapture_pipeline.v1"


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def path_or_empty(value: str) -> str:
    return str(pathlib.Path(value).expanduser().resolve()) if value else ""


def existing_optional_path(value: str) -> str:
    if not value:
        return ""
    path = pathlib.Path(value).expanduser().resolve()
    return str(path) if path.exists() else ""


def build_intake_args(args: argparse.Namespace, *, manifest_output: pathlib.Path) -> SimpleNamespace:
    capture_dir = pathlib.Path(args.capture_dir).expanduser().resolve()
    return SimpleNamespace(
        raw_dir=[str(capture_dir / "raw"), str(capture_dir / "runs")],
        repo_root=[str(pathlib.Path(args.repo_root).expanduser().resolve())],
        endpoint_dir=[str(capture_dir / "endpoints"), str(capture_dir / "runs")],
        label_dir=[str(capture_dir / "labels")] if (capture_dir / "labels").exists() else [],
        matrix_dir=[str(capture_dir / "matrices")] if (capture_dir / "matrices").exists() else [],
        output=str(manifest_output),
        report=str(pathlib.Path(args.intake_report).expanduser().resolve()),
        raw_format=args.raw_format,
        source=args.source,
        agent_repo_root=args.agent_repo_root,
        taskset=args.taskset,
        trials=args.trials,
        annotator=args.annotator,
        annotation_source=args.annotation_source,
        scheduler_variant=args.scheduler_variant,
        require_frontier_annotations=args.require_frontier_annotations,
        require_matrix_summary=args.require_matrix_summary,
        include_incomplete=args.include_incomplete,
        allow_empty=True,
    )


def dashboard_args(args: argparse.Namespace, *, intake_report: pathlib.Path, preflight_report: pathlib.Path) -> SimpleNamespace:
    return SimpleNamespace(
        readiness_report=existing_optional_path(args.readiness_report),
        capture_plan_preflight=existing_optional_path(args.capture_plan_preflight),
        harness_matrix_plan=existing_optional_path(args.harness_matrix_plan),
        harness_matrix_runbook_preflight=existing_optional_path(args.harness_matrix_runbook_preflight),
        bootstrap_report=existing_optional_path(args.bootstrap_report),
        repo_prep_report=existing_optional_path(args.repo_prep_report),
        harness_matrix_audit=existing_optional_path(args.harness_matrix_audit),
        repo_stage_input=list(args.repo_stage_input or []),
        repo_stage_recursive=args.repo_stage_recursive,
        mechanism_smoke_input=list(args.mechanism_smoke_input or []),
        mechanism_smoke_recursive=args.mechanism_smoke_recursive,
        intake_report=str(intake_report),
        preflight_report=str(preflight_report),
        collection_audit=existing_optional_path(args.collection_audit),
        bundle_input=list(args.bundle_input or []),
        recursive=args.recursive,
        min_ready_bundles=args.min_ready_bundles,
        output=str(pathlib.Path(args.dashboard_output).expanduser().resolve()),
        markdown_output=str(pathlib.Path(args.dashboard_markdown_output).expanduser().resolve()) if args.dashboard_markdown_output else "",
        latex_output=str(pathlib.Path(args.latex_output).expanduser().resolve()) if args.latex_output else "",
    )


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    capture_dir = pathlib.Path(args.capture_dir).expanduser().resolve()
    manifest_output = pathlib.Path(args.manifest_output or (capture_dir / "real-agent-manifest.json")).expanduser().resolve()
    intake_report = pathlib.Path(args.intake_report or (capture_dir / "intake.current.json")).expanduser().resolve()
    preflight_report = pathlib.Path(args.preflight_report or (capture_dir / "manifest-preflight.current.json")).expanduser().resolve()
    dashboard_output = pathlib.Path(args.dashboard_output or (capture_dir / "evidence-dashboard.current.json")).expanduser().resolve()
    args.manifest_output = str(manifest_output)
    args.intake_report = str(intake_report)
    args.preflight_report = str(preflight_report)
    args.dashboard_output = str(dashboard_output)

    manifest, intake = build_manifest(build_intake_args(args, manifest_output=manifest_output))
    write_json(manifest_output, manifest)
    write_json(intake_report, intake)

    preflight = validate_manifest(
        manifest_output,
        out_dir=capture_dir,
        require_annotations=bool(args.strict_preflight or args.require_frontier_annotations),
        require_matrix_summary=bool(args.strict_preflight or args.require_matrix_summary),
    )
    write_json(preflight_report, preflight)

    dash_args = dashboard_args(args, intake_report=intake_report, preflight_report=preflight_report)
    dashboard = summarize_dashboard(dash_args)
    write_json(dashboard_output, dashboard)
    if dash_args.markdown_output:
        from summarize_real_agent_evidence import markdown

        markdown_path = pathlib.Path(dash_args.markdown_output)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.write_text(markdown(dashboard), encoding="utf-8")
    if dash_args.latex_output:
        from summarize_real_agent_evidence import latex_table

        latex_path = pathlib.Path(dash_args.latex_output)
        latex_path.parent.mkdir(parents=True, exist_ok=True)
        latex_path.write_text(latex_table(dashboard), encoding="utf-8")

    blockers: list[str] = []
    blockers.extend(str(item) for item in as_list(preflight.get("blockers")))
    blockers.extend(str(item) for item in as_list(dashboard.get("blockers"))[:20])
    ready_instances = int(intake.get("summary", {}).get("ready_instances") or 0)
    status = "pass" if dashboard.get("status") == "pass" else "partial" if ready_instances else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "capture_dir": str(capture_dir),
        "artifacts": {
            "manifest": str(manifest_output),
            "intake_report": str(intake_report),
            "preflight_report": str(preflight_report),
            "dashboard": str(dashboard_output),
            "dashboard_markdown": str(pathlib.Path(args.dashboard_markdown_output).expanduser().resolve()) if args.dashboard_markdown_output else "",
            "latex": str(pathlib.Path(args.latex_output).expanduser().resolve()) if args.latex_output else "",
        },
        "summary": {
            "ready_instances": ready_instances,
            "manifest_instances": int(intake.get("summary", {}).get("manifest_instances") or 0),
            "preflight_status": preflight.get("status"),
            "dashboard_status": dashboard.get("status"),
            "dashboard_scope": dashboard.get("highest_supported_scope"),
        },
        "blockers": list(dict.fromkeys(blocker for blocker in blockers if blocker))[:40],
        "notes": [
            "post-capture pipeline reports can fail honestly when captures are absent",
            "dashboard pass still requires audited multi-instance bundle evidence; this driver does not create endpoint results",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run FlowCausal real-agent post-capture intake, preflight, and dashboard refresh",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--capture-dir", default="flowcausal/experiments/results/real-agent-captures")
    parser.add_argument("--repo-root", default="flowcausal/benchmarks/harness-replaybench/repos")
    parser.add_argument("--manifest-output")
    parser.add_argument("--intake-report")
    parser.add_argument("--preflight-report")
    parser.add_argument("--dashboard-output")
    parser.add_argument("--dashboard-markdown-output")
    parser.add_argument("--latex-output")
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="real-agent")
    parser.add_argument("--agent-repo-root", default="/testbed")
    parser.add_argument("--taskset", default="0-1")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", default="")
    parser.add_argument("--scheduler-variant", default="")
    parser.add_argument("--require-frontier-annotations", action="store_true")
    parser.add_argument("--require-matrix-summary", action="store_true")
    parser.add_argument("--include-incomplete", action="store_true")
    parser.add_argument("--strict-preflight", action="store_true", help="require frontier annotations and matrix summaries")
    parser.add_argument("--readiness-report", default="flowcausal/experiments/results/real-agent-captures/readiness.current.json")
    parser.add_argument("--capture-plan-preflight", default="flowcausal/experiments/results/real-agent-captures/capture-plan-preflight.current.json")
    parser.add_argument("--harness-matrix-plan", default="flowcausal/experiments/results/real-agent-captures/harness-matrix-plan.current.json")
    parser.add_argument("--harness-matrix-runbook-preflight", default="flowcausal/experiments/results/real-agent-captures/harness-matrix-runbook-preflight.current.json")
    parser.add_argument("--bootstrap-report", default="flowcausal/experiments/results/real-agent-harness-current/bootstrap-report.json")
    parser.add_argument("--repo-prep-report", default="flowcausal/experiments/results/real-agent-captures/repo-prep.json")
    parser.add_argument("--harness-matrix-audit", default="")
    parser.add_argument("--repo-stage-input", action="append", default=[])
    parser.add_argument("--repo-stage-recursive", action="store_true")
    parser.add_argument("--mechanism-smoke-input", action="append", default=[])
    parser.add_argument("--mechanism-smoke-recursive", action="store_true")
    parser.add_argument("--collection-audit", default="")
    parser.add_argument("--bundle-input", action="append", default=[])
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--min-ready-bundles", type=int, default=3)
    parser.add_argument("--report-output", help="write post-capture pipeline JSON report")
    parser.add_argument("--strict", action="store_true", help="exit non-zero unless the pipeline dashboard is paper-ready")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_pipeline(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report_output:
        output = pathlib.Path(args.report_output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0 if (not args.strict or report.get("status") == "pass") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

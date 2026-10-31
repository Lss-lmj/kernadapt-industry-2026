#!/usr/bin/env python3
"""Preflight generated full-harness matrix runbooks.

``plan_real_agent_harness_matrices.py`` creates one matrix runbook per selected
real SWE-bench/code-agent instance.  This checker verifies that those generated
artifacts are actually ready for endpoint-matrix execution:

* per-instance matrix manifests and run scripts exist;
* required variants/trials are present;
* static Linux baselines are real wrappers, not just variant names;
* FlowCausal variants are routed through ``run_live_sandbox.py`` with the
  expected scheduler flags; and
* run scripts contain an API-key guard before they can spend model budget.

It does not execute the harness and does not create endpoint evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import Any


SCHEMA = "flowcausal.real_agent_harness_matrix_runbook_preflight.v1"
PLAN_SCHEMA = "flowcausal.real_agent_harness_matrix_plan.v1"
MATRIX_SCHEMA = "flowcausal.real_agent_harness_matrix.v1"
DEFAULT_REQUIRED_VARIANTS = (
    "default",
    "nice_priority",
    "static_affinity",
    "static_cgroup",
    "default_observed",
    "flowcausal_no_width",
    "flowcausal_width",
)
DEFAULT_REQUIRED_ENVS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON is not an object: {path}")
    return payload


def path_from(value: str, *, root: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def contains_all(tokens: list[str], required: tuple[str, ...] | list[str]) -> bool:
    return all(item in tokens for item in required)


def wrapped_command_gate(variant: str, tokens: list[str]) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    joined = " ".join(tokens)
    if variant == "nice_priority":
        if "nice" in tokens and "-n" in tokens:
            evidence.append("nice_priority uses nice -n wrapper")
        else:
            blockers.append("nice_priority wrapped_command lacks nice -n")
    elif variant == "static_affinity":
        if "taskset" in tokens and "-c" in tokens:
            evidence.append("static_affinity uses taskset -c wrapper")
        else:
            blockers.append("static_affinity wrapped_command lacks taskset -c")
    elif variant == "static_cgroup":
        if "run_static_cgroup.py" in joined:
            evidence.append("static_cgroup uses run_static_cgroup.py wrapper")
        else:
            blockers.append("static_cgroup wrapped_command lacks run_static_cgroup.py")
    elif variant == "default_observed":
        if "run_live_sandbox.py" in joined and "--hint-dry-run" in tokens:
            evidence.append("default_observed uses run_live_sandbox.py dry-run observation")
        else:
            blockers.append("default_observed lacks run_live_sandbox.py --hint-dry-run")
    elif variant == "flowcausal_no_width":
        if "run_live_sandbox.py" in joined and "--attach-scheduler" in tokens and "--width-control" not in tokens:
            evidence.append("flowcausal_no_width attaches scheduler without width control")
        else:
            blockers.append("flowcausal_no_width wrapper does not match no-width FlowCausal baseline")
    elif variant == "flowcausal_width":
        if "run_live_sandbox.py" in joined and contains_all(tokens, ["--attach-scheduler", "--width-control"]):
            evidence.append("flowcausal_width attaches scheduler with width control")
        else:
            blockers.append("flowcausal_width wrapper lacks attach-scheduler and width-control")
    elif variant == "default":
        if "run_live_sandbox.py" not in joined and "--attach-scheduler" not in tokens:
            evidence.append("default is a non-FlowCausal direct baseline")
        else:
            blockers.append("default baseline unexpectedly uses live scheduler attachment")
    return evidence, blockers


def inspect_script(path: pathlib.Path, required_env: list[str]) -> dict[str, Any]:
    blockers: list[str] = []
    evidence: list[str] = []
    if not path.exists():
        return {"status": "fail", "script": str(path), "evidence": [], "blockers": [f"run script missing: {path}"]}
    if not os.access(path, os.X_OK):
        blockers.append(f"run script is not executable: {path}")
    else:
        evidence.append(f"run script executable: {path}")
    text = path.read_text(encoding="utf-8", errors="replace")
    if "FLOWCAUSAL_HAS_REQUIRED_ENV" not in text:
        blockers.append("run script lacks API-key guard variable FLOWCAUSAL_HAS_REQUIRED_ENV")
    missing = [key for key in required_env if key not in text]
    if missing:
        blockers.append("run script API-key guard missing env var(s): " + ", ".join(missing))
    if not missing and "FLOWCAUSAL_HAS_REQUIRED_ENV" in text:
        evidence.append("run script contains API-key guard for: " + ", ".join(required_env))
    return {"status": "pass" if not blockers else "fail", "script": str(path), "evidence": evidence, "blockers": blockers}


def inspect_manifest(
    manifest_path: pathlib.Path,
    *,
    required_variants: list[str],
    min_trials: int,
) -> dict[str, Any]:
    blockers: list[str] = []
    evidence: list[str] = []
    if not manifest_path.exists():
        return {
            "status": "fail",
            "manifest": str(manifest_path),
            "evidence": [],
            "blockers": [f"matrix manifest missing: {manifest_path}"],
            "variant_counts": {},
        }
    manifest = read_json(manifest_path)
    if manifest.get("schema") != MATRIX_SCHEMA:
        blockers.append(f"unexpected matrix manifest schema: {manifest.get('schema')}")
    runs = [row for row in as_list(manifest.get("runs")) if isinstance(row, dict)]
    variants = [str(item) for item in as_list(manifest.get("variants")) if str(item)]
    missing_variants = [variant for variant in required_variants if variant not in variants]
    if missing_variants:
        blockers.append("matrix manifest missing required variant(s): " + ", ".join(missing_variants))
    else:
        evidence.append("all required variants are listed")
    variant_counts: dict[str, int] = {}
    wrapper_evidence: list[str] = []
    for variant in required_variants:
        rows = [row for row in runs if str(row.get("variant") or "") == variant]
        variant_counts[variant] = len(rows)
        if len(rows) < min_trials:
            blockers.append(f"{variant} trial count {len(rows)} < required {min_trials}")
        for row in rows:
            tokens = [str(item) for item in as_list(row.get("wrapped_command"))]
            row_evidence, row_blockers = wrapped_command_gate(variant, tokens)
            wrapper_evidence.extend(row_evidence[:1])
            trial = row.get("trial")
            blockers.extend(f"{variant} trial {trial}: {item}" for item in row_blockers)
    if not blockers:
        evidence.append(f"required variant/trial coverage pass: {len(required_variants)} variants × {min_trials} trials")
    evidence.extend(sorted(set(wrapper_evidence)))
    return {
        "status": "pass" if not blockers else "fail",
        "manifest": str(manifest_path),
        "evidence": evidence,
        "blockers": blockers,
        "variant_counts": variant_counts,
        "runs": len(runs),
    }


def inspect_row(row: dict[str, Any], *, root: pathlib.Path, required_variants: list[str], min_trials: int, required_env: list[str]) -> dict[str, Any]:
    instance_id = str(row.get("instance_id") or "")
    matrix_out_dir = str(row.get("matrix_out_dir") or "")
    blockers: list[str] = []
    evidence: list[str] = []
    if not instance_id:
        blockers.append("missing instance_id")
    if not matrix_out_dir:
        blockers.append("missing matrix_out_dir")
        matrix_dir = root
    else:
        matrix_dir = path_from(matrix_out_dir, root=root)
        if matrix_dir.is_dir():
            evidence.append(f"matrix_out_dir exists: {matrix_dir}")
        else:
            blockers.append(f"matrix_out_dir missing: {matrix_dir}")
    manifest_report = inspect_manifest(matrix_dir / "real-agent-harness-matrix-manifest.json", required_variants=required_variants, min_trials=min_trials)
    script_report = inspect_script(matrix_dir / "run-harness-matrix.sh", required_env)
    blockers.extend(str(item) for item in as_list(manifest_report.get("blockers")))
    blockers.extend(str(item) for item in as_list(script_report.get("blockers")))
    evidence.extend(str(item) for item in as_list(manifest_report.get("evidence")))
    evidence.extend(str(item) for item in as_list(script_report.get("evidence")))
    return {
        "instance_id": instance_id,
        "status": "pass" if not blockers else "fail",
        "matrix_out_dir": str(matrix_dir),
        "evidence": evidence,
        "blockers": blockers,
        "manifest_check": manifest_report,
        "script_check": script_report,
    }


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    root = pathlib.Path(args.root).expanduser().resolve()
    plan_path = path_from(args.plan, root=root)
    plan = read_json(plan_path)
    required_variants = list(args.required_variant or DEFAULT_REQUIRED_VARIANTS)
    required_env = list(args.require_any_env or DEFAULT_REQUIRED_ENVS)
    blockers: list[str] = []
    evidence: list[str] = [f"plan={plan_path}"]
    if plan.get("schema") != PLAN_SCHEMA:
        blockers.append(f"unexpected plan schema: {plan.get('schema')}")
    rows = [row for row in as_list(plan.get("rows")) if isinstance(row, dict)]
    if len(rows) < args.min_instances:
        blockers.append(f"matrix plan rows {len(rows)} < required {args.min_instances}")
    row_reports = [
        inspect_row(
            row,
            root=root,
            required_variants=required_variants,
            min_trials=args.min_trials,
            required_env=required_env,
        )
        for row in rows
    ]
    for row in row_reports:
        blockers.extend(f"{row.get('instance_id')}: {item}" for item in as_list(row.get("blockers")))
    pass_rows = sum(1 for row in row_reports if row.get("status") == "pass")
    fail_rows = sum(1 for row in row_reports if row.get("status") == "fail")
    if pass_rows:
        evidence.append(f"pass_rows={pass_rows}")
    status = "pass" if not blockers else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "plan": str(plan_path),
        "summary": {
            "rows": len(rows),
            "row_pass": pass_rows,
            "row_fail": fail_rows,
            "min_instances": args.min_instances,
            "min_trials": args.min_trials,
            "required_variants": required_variants,
        },
        "evidence": evidence,
        "rows": row_reports,
        "blockers": blockers,
        "notes": [
            "runbook preflight validates generated full-harness matrix artifacts only",
            "it does not execute the matrix and cannot support endpoint-improvement claims",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal full-harness matrix runbook preflight",
        "",
        f"Status: `{report.get('status')}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in as_dict(report.get("summary")).items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Rows", "", "| Instance | Status | Evidence |", "| --- | --- | --- |"])
    for row in as_list(report.get("rows")):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| `{row.get('instance_id', '')}` | `{row.get('status', '')}` | {'; '.join(str(item) for item in as_list(row.get('evidence'))[:5])} |"
        )
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in as_list(report.get("blockers")):
            lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight generated FlowCausal full-harness matrix runbooks",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--plan", required=True, help="harness-matrix-plan JSON from plan_real_agent_harness_matrices.py")
    parser.add_argument("--root", default=".")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--min-trials", type=int, default=3)
    parser.add_argument("--required-variant", action="append", default=[])
    parser.add_argument("--require-any-env", action="append", default=[])
    parser.add_argument("--output")
    parser.add_argument("--markdown-output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = summarize(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        output = pathlib.Path(args.markdown_output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(report), encoding="utf-8")
    print(text, end="")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

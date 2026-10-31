#!/usr/bin/env python3
"""Audit a collection of FlowCausal real-agent evidence bundles.

``audit_agent_trace_bundle.py`` answers whether one benchmark instance has a
complete six-gate evidence bundle.  A EuroSys paper claim needs a collection:
multiple real instances, each with frontier correctness, endpoint improvement,
scheduler mechanism, safety/fairness, baseline comparison, and reproducibility.

This helper aggregates bundle-audit JSON files and reports how many instances
are L5-ready, which gates are missing across the set, and which blockers remain
most common.  It deliberately treats a generated scaffold as insufficient unless
the underlying per-instance audit has actually passed.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
from typing import Any


BUNDLE_SCHEMA = "flowcausal.agent_trace_bundle_audit.v1"
COLLECTION_SCHEMA = "flowcausal.bundle_collection_audit.v1"
GATE_NAMES = (
    "frontier correctness",
    "endpoint improvement",
    "scheduler mechanism",
    "safety/fairness",
    "baseline comparison",
    "reproducibility/artifact",
)
STATUSES = ("pass", "partial", "fail", "missing")
SELECTED_TRIAL_BINDING_EVIDENCE = {
    "raw_agent_output": "harness selected trial raw_agent_output matches bundle raw",
    "full_harness_result": "harness selected trial full_harness_result matches normalized endpoint provenance",
    "victim_log": "harness selected trial victim_log matches bundle victim log",
    "live_dir": "harness selected trial live_dir matches bundle live dir",
}


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"audit report is not a JSON object: {path}")
    return data


def discover_audit_paths(inputs: list[str], *, recursive: bool) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    for text in inputs:
        path = pathlib.Path(text).resolve()
        if path.is_file():
            paths.append(path)
        elif path.is_dir():
            direct = path / "agent-trace-bundle-audit.json"
            if direct.exists():
                paths.append(direct)
            if recursive:
                paths.extend(sorted(path.rglob("agent-trace-bundle-audit.json")))
        else:
            raise SystemExit(f"input path does not exist: {path}")
    unique: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(resolved)
    return unique


def gate_map(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for gate in as_list(report.get("gates")):
        if isinstance(gate, dict):
            out[str(gate.get("name", ""))] = gate
    return out


def sibling_markdown_report(path: pathlib.Path) -> str:
    md_path = path.with_suffix(".md")
    return str(md_path) if md_path.exists() else ""


def selected_trial_binding(report: dict[str, Any]) -> dict[str, Any]:
    inputs = as_dict(report.get("inputs"))
    uses_harness_matrix = bool(inputs.get("harness_matrix_summary") or inputs.get("harness_matrix_audit"))
    if not uses_harness_matrix:
        return {
            "status": "not_applicable",
            "required": [],
            "matched": [],
            "missing": [],
        }
    gates = gate_map(report)
    repro = gates.get("reproducibility/artifact", {})
    evidence_text = "\n".join(str(item) for item in as_list(repro.get("evidence")))
    required = ["raw_agent_output", "full_harness_result"]
    if inputs.get("victim_log"):
        required.append("victim_log")
    if inputs.get("live_dir"):
        required.append("live_dir")
    matched = [
        key
        for key in required
        if SELECTED_TRIAL_BINDING_EVIDENCE[key] in evidence_text
    ]
    missing = [key for key in required if key not in matched]
    return {
        "status": "pass" if not missing else "missing",
        "required": required,
        "matched": matched,
        "missing": missing,
    }


def instance_id(report: dict[str, Any], path: pathlib.Path) -> str:
    normalized = as_dict(report.get("normalized_trace"))
    if normalized.get("instance_id"):
        return str(normalized.get("instance_id"))
    endpoint = as_dict(report.get("full_harness_endpoint"))
    if endpoint.get("instance_id"):
        return str(endpoint.get("instance_id"))
    return path.parent.name or path.stem


def is_l5_ready(report: dict[str, Any]) -> bool:
    return (
        report.get("schema") == BUNDLE_SCHEMA
        and report.get("status") == "pass"
        and report.get("highest_supported_scope") == "L5-ready evidence bundle"
    )


def summarize_reports(
    reports: list[tuple[pathlib.Path, dict[str, Any]]],
    *,
    min_ready_bundles: int,
) -> dict[str, Any]:
    bundle_rows: list[dict[str, Any]] = []
    gate_counts: dict[str, dict[str, int]] = {
        name: {status: 0 for status in STATUSES}
        for name in GATE_NAMES
    }
    blocker_counts: collections.Counter[str] = collections.Counter()
    schema_errors: list[str] = []
    duplicate_instances: dict[str, int] = {}
    seen_instances: collections.Counter[str] = collections.Counter()
    selected_binding_counts: collections.Counter[str] = collections.Counter()
    selected_binding_blockers: list[str] = []

    for path, report in reports:
        if report.get("schema") != BUNDLE_SCHEMA:
            schema_errors.append(f"{path}: schema={report.get('schema')!r}")
        iid = instance_id(report, path)
        seen_instances[iid] += 1
        gates = gate_map(report)
        gate_statuses: dict[str, str] = {}
        binding = selected_trial_binding(report)
        selected_binding_counts[str(binding["status"])] += 1
        if binding["status"] == "missing":
            selected_binding_blockers.append(
                f"{iid}: full-harness selected-trial binding missing for {', '.join(binding['missing'])}"
            )
        for name in GATE_NAMES:
            gate = gates.get(name, {})
            status = str(gate.get("status") or "missing")
            if status not in STATUSES:
                status = "missing"
            gate_counts[name][status] += 1
            gate_statuses[name] = status
            for blocker in as_list(gate.get("blockers")):
                blocker_counts[str(blocker)] += 1
        bundle_rows.append(
            {
                "instance_id": iid,
                "path": str(path),
                "markdown_report": sibling_markdown_report(path),
                "status": report.get("status", ""),
                "highest_supported_scope": report.get("highest_supported_scope", ""),
                "l5_ready": is_l5_ready(report),
                "gates": gate_statuses,
                "selected_trial_binding": binding,
            }
        )

    duplicate_instances = {
        iid: count
        for iid, count in seen_instances.items()
        if iid and count > 1
    }
    ready_count = sum(1 for row in bundle_rows if row["l5_ready"])
    partial_count = sum(1 for row in bundle_rows if row["status"] == "partial")
    fail_count = sum(1 for row in bundle_rows if row["status"] == "fail")

    blockers: list[str] = []
    if not reports:
        blockers.append("no bundle audit reports were found")
    if schema_errors:
        blockers.append("some inputs are not flowcausal.agent_trace_bundle_audit.v1")
    if duplicate_instances:
        blockers.append("duplicate instance_id values in collection")
    if ready_count < min_ready_bundles:
        blockers.append(f"L5-ready bundle count {ready_count} < required {min_ready_bundles}")
    for name, counts in gate_counts.items():
        if counts["pass"] < min_ready_bundles:
            blockers.append(f"gate {name!r} has only {counts['pass']} pass bundle(s)")
    blockers.extend(selected_binding_blockers)

    if not blockers:
        status = "pass"
        highest = "collection-ready evidence set"
    elif ready_count > 0 or partial_count > 0:
        status = "partial"
        highest = "partial collection evidence; not paper-ready"
    else:
        status = "fail"
        highest = "insufficient collection evidence"

    return {
        "schema": COLLECTION_SCHEMA,
        "status": status,
        "highest_supported_scope": highest,
        "requirements": {
            "min_ready_bundles": min_ready_bundles,
            "required_gates": list(GATE_NAMES),
        },
        "summary": {
            "bundle_count": len(reports),
            "l5_ready_count": ready_count,
            "partial_count": partial_count,
            "fail_count": fail_count,
            "duplicate_instances": duplicate_instances,
            "schema_errors": schema_errors,
            "selected_trial_binding": dict(selected_binding_counts),
        },
        "gate_counts": gate_counts,
        "top_blockers": [
            {"blocker": blocker, "count": count}
            for blocker, count in blocker_counts.most_common(20)
        ],
        "blockers": blockers,
        "bundles": bundle_rows,
        "notes": [
            "collection pass means the evidence set is structurally ready; it does not prove statistical significance",
            "single-instance L5-ready reports are necessary but not sufficient for a EuroSys paper claim",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal bundle collection audit",
        "",
        f"Status: `{report.get('status')}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    summary = as_dict(report.get("summary"))
    for key in ("bundle_count", "l5_ready_count", "partial_count", "fail_count"):
        lines.append(f"| {key} | {summary.get(key, 0)} |")
    binding_counts = as_dict(summary.get("selected_trial_binding"))
    if binding_counts:
        lines.extend(["", "## Full-harness Selected-trial binding", "", "| Status | Count |", "| --- | ---: |"])
        for status in ("pass", "missing", "not_applicable"):
            if status in binding_counts:
                lines.append(f"| {status} | {binding_counts.get(status, 0)} |")
    lines.extend(["", "## Gate counts", "", "| Gate | Pass | Partial | Fail | Missing |", "| --- | ---: | ---: | ---: | ---: |"])
    for name, counts in as_dict(report.get("gate_counts")).items():
        data = as_dict(counts)
        lines.append(
            f"| {name} | {data.get('pass', 0)} | {data.get('partial', 0)} | {data.get('fail', 0)} | {data.get('missing', 0)} |"
        )
    lines.extend(
        [
            "",
            "## Bundles",
            "",
            "| Instance | Status | L5-ready | Selected-trial binding | Path | Markdown |",
            "| --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for row in as_list(report.get("bundles")):
        if not isinstance(row, dict):
            continue
        binding = as_dict(row.get("selected_trial_binding"))
        lines.append(
            f"| {row.get('instance_id', '')} | {row.get('status', '')} | "
            f"{str(row.get('l5_ready', False)).lower()} | "
            f"{binding.get('status', '')} | "
            f"`{row.get('path', '')}` | "
            f"`{row.get('markdown_report', '')}` |"
        )
    blockers = as_list(report.get("blockers"))
    if blockers:
        lines.extend(["", "## Collection blockers", ""])
        for blocker in blockers:
            lines.append(f"- {blocker}")
    top_blockers = as_list(report.get("top_blockers"))
    if top_blockers:
        lines.extend(["", "## Top per-bundle blockers", ""])
        for row in top_blockers:
            if isinstance(row, dict):
                lines.append(f"- {row.get('count', 0)}× {row.get('blocker', '')}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate FlowCausal per-instance bundle audits into a collection-level report",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("inputs", nargs="+", help="bundle audit JSON files or bundle/result directories")
    parser.add_argument("--recursive", action="store_true", help="recursively discover agent-trace-bundle-audit.json under directories")
    parser.add_argument("--min-ready-bundles", type=int, default=3)
    parser.add_argument("--output", help="write collection audit JSON here")
    parser.add_argument("--markdown-output", help="write markdown summary here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    paths = discover_audit_paths(args.inputs, recursive=args.recursive)
    reports = [(path, read_json(path)) for path in paths]
    report = summarize_reports(reports, min_ready_bundles=args.min_ready_bundles)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        output = pathlib.Path(args.markdown_output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(report), encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

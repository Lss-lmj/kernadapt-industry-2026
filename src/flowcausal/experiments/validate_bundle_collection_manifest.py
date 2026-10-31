#!/usr/bin/env python3
"""Preflight-check a FlowCausal real-agent bundle collection manifest.

The batch runner can generate missing artifacts, but before spending scheduler
time we should catch the cheap mistakes: missing raw outputs, stale endpoint
paths, duplicate instance ids, absent frontier labels, or an existing
``matrix-summary.json`` that points at a different trace than the bundle would
generate.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
from typing import Any


SCHEMA = "flowcausal.bundle_collection_manifest_preflight.v1"
REQUIRED_PATH_FIELDS = ("raw_agent_output", "repo_dir", "full_harness_result")
OPTIONAL_PATH_FIELDS = (
    "frontier_annotations",
    "matrix_summary",
    "victim_log",
    "live_dir",
    "harness_matrix_summary",
    "harness_matrix_audit",
)
REQUIRED_FIELDS = ("instance_id", *REQUIRED_PATH_FIELDS)


def safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return out.strip("-") or "instance"


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
        raise SystemExit(f"manifest is not a JSON object: {path}")
    return data


def resolve_path(value: Any, base_dir: pathlib.Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = pathlib.Path(text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def path_exists(path_text: str, *, expect_dir: bool = False) -> bool:
    if not path_text:
        return False
    path = pathlib.Path(path_text)
    return path.is_dir() if expect_dir else path.exists()


def merged_instance(defaults: dict[str, Any], instance: dict[str, Any], base_dir: pathlib.Path) -> dict[str, Any]:
    row = dict(defaults)
    row.update(instance)
    for key in (*REQUIRED_PATH_FIELDS, *OPTIONAL_PATH_FIELDS):
        if row.get(key):
            row[key] = resolve_path(row.get(key), base_dir)
    return row


def payload_tokens(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, str):
        try:
            return shlex.split(payload)
        except ValueError:
            return payload.split()
    return []


def trace_file_args(payload: Any) -> list[str]:
    tokens = payload_tokens(payload)
    out: list[str] = []
    for idx, token in enumerate(tokens):
        if token == "--trace-file" and idx + 1 < len(tokens):
            out.append(tokens[idx + 1])
        elif token.startswith("--trace-file="):
            out.append(token.split("=", 1)[1])
    return out


def collect_matrix_trace_refs(matrix: dict[str, Any]) -> tuple[list[str], list[str]]:
    begin_refs: list[str] = []
    cli_refs: list[str] = []
    for variant in as_list(matrix.get("variants")):
        if not isinstance(variant, dict):
            continue
        for trial in as_list(variant.get("trials")):
            trial_dict = as_dict(trial)
            workload = as_dict(trial_dict.get("workload"))
            begin = as_dict(workload.get("workload_begin"))
            trace_file = begin.get("trace_file")
            if isinstance(trace_file, str) and trace_file:
                begin_refs.append(trace_file)
            manifest = as_dict(trial_dict.get("manifest"))
            for key in ("command", "inner_command", "workload_cmd", "controller_cmd"):
                if manifest.get(key):
                    cli_refs.extend(trace_file_args(manifest.get(key)))
    config = as_dict(matrix.get("config"))
    if config.get("command"):
        cli_refs.extend(trace_file_args(config.get("command")))
    return begin_refs, cli_refs


def ref_matches(path_text: str, expected: pathlib.Path, *, base_dirs: list[pathlib.Path]) -> bool:
    if not path_text:
        return False
    raw = pathlib.Path(path_text).expanduser()
    candidates = [raw] if raw.is_absolute() else [pathlib.Path.cwd() / raw, *[base / raw for base in base_dirs]]
    for candidate in candidates:
        try:
            if candidate.resolve() == expected.resolve():
                return True
        except OSError:
            continue
    return False


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def selected_harness_trial(summary: dict[str, Any], *, variant: str, trial: int) -> dict[str, Any]:
    for row in as_list(summary.get("trials")):
        row_dict = as_dict(row)
        if str(row_dict.get("variant") or "") != variant:
            continue
        if int_or_none(row_dict.get("trial")) == trial:
            return row_dict
    return {}


def validate_harness_matrix_source(row: dict[str, Any]) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    has_summary = bool(row.get("harness_matrix_summary"))
    has_audit = bool(row.get("harness_matrix_audit"))
    if not (has_summary or has_audit):
        return evidence, blockers

    variant = str(row.get("harness_matrix_source_variant") or "").strip()
    trial = int_or_none(row.get("harness_matrix_source_trial"))
    if not variant:
        blockers.append("missing harness_matrix_source_variant for full-harness matrix provenance")
    if trial is None:
        blockers.append("missing harness_matrix_source_trial for full-harness matrix provenance")
    if not has_summary:
        blockers.append("missing harness_matrix_summary for full-harness selected-trial verification")
        return evidence, blockers
    if not path_exists(str(row.get("harness_matrix_summary"))):
        return evidence, blockers
    if not variant or trial is None:
        return evidence, blockers

    summary_path = pathlib.Path(str(row.get("harness_matrix_summary")))
    try:
        summary = read_json(summary_path)
    except SystemExit as exc:
        blockers.append(str(exc))
        return evidence, blockers
    selected = selected_harness_trial(summary, variant=variant, trial=trial)
    if not selected:
        blockers.append(f"harness matrix selected trial not found: variant={variant} trial={trial}")
        return evidence, blockers
    evidence.append(f"harness matrix selected trial found: variant={variant} trial={trial}")

    base_dirs = [parent for parent in summary_path.resolve().parents]
    path_pairs = (
        ("raw_agent_output", "raw_agent_output"),
        ("full_harness_result", "full_harness_result"),
        ("victim_log", "victim_log"),
        ("live_dir", "live_dir"),
    )
    for manifest_key, selected_key in path_pairs:
        manifest_value = str(row.get(manifest_key) or "")
        selected_value = str(selected.get(selected_key) or "")
        if not manifest_value and not selected_value:
            continue
        if manifest_value and selected_value and ref_matches(selected_value, pathlib.Path(manifest_value), base_dirs=base_dirs):
            evidence.append(f"harness selected trial {selected_key} matches manifest {manifest_key}")
        else:
            blockers.append(f"harness selected trial {selected_key} does not match manifest {manifest_key}")
    return evidence, blockers


def validate_matrix_summary(path_text: str, *, expected_trace: pathlib.Path, expected_instance: str) -> tuple[list[str], list[str], list[str]]:
    evidence: list[str] = []
    warnings: list[str] = []
    blockers: list[str] = []
    path = pathlib.Path(path_text)
    try:
        matrix = read_json(path)
    except SystemExit as exc:
        return evidence, warnings, [str(exc)]

    base_dirs = [parent for parent in path.resolve().parents]
    begin_refs, cli_refs = collect_matrix_trace_refs(matrix)
    refs = [*begin_refs, *cli_refs]
    if refs:
        if all(ref_matches(ref, expected_trace, base_dirs=base_dirs) for ref in refs):
            evidence.append("matrix trace references match expected bundle trace")
        else:
            blockers.append("matrix trace_file/--trace-file does not match expected bundle trace")
    else:
        warnings.append("matrix summary lacks trace_file/--trace-file provenance")

    begin_instances: list[str] = []
    for variant in as_list(matrix.get("variants")):
        if not isinstance(variant, dict):
            continue
        for trial in as_list(variant.get("trials")):
            begin = as_dict(as_dict(as_dict(trial).get("workload")).get("workload_begin"))
            iid = begin.get("instance_id")
            if isinstance(iid, str) and iid:
                begin_instances.append(iid)
    if begin_instances:
        if all(iid == expected_instance for iid in begin_instances):
            evidence.append("matrix workload_begin instance_id matches manifest")
        else:
            blockers.append("matrix workload_begin instance_id does not match manifest")
    else:
        warnings.append("matrix summary lacks workload_begin.instance_id provenance")
    return evidence, warnings, blockers


def expected_trace_path(out_dir: pathlib.Path, instance_id: str, *, has_annotations: bool) -> pathlib.Path:
    name = safe_name(instance_id)
    suffix = ".annotated.flowcausal-trace.json" if has_annotations else ".flowcausal-trace.json"
    return out_dir / "bundles" / name / "traces" / f"{name}{suffix}"


def validate_manifest(
    manifest_path: pathlib.Path,
    *,
    out_dir: pathlib.Path,
    require_annotations: bool,
    require_matrix_summary: bool,
) -> dict[str, Any]:
    manifest = read_json(manifest_path)
    base_dir = manifest_path.parent
    defaults = as_dict(manifest.get("defaults"))
    rows = as_list(manifest.get("instances"))
    blockers: list[str] = []
    warnings: list[str] = []
    instances: list[dict[str, Any]] = []
    seen: dict[str, int] = {}

    if not rows:
        blockers.append("manifest has no instances")

    for idx, item in enumerate(rows):
        if not isinstance(item, dict):
            blockers.append(f"instances[{idx}] is not an object")
            continue
        row = merged_instance(defaults, item, base_dir)
        instance_id = str(row.get("instance_id") or "").strip()
        row_blockers: list[str] = []
        row_warnings: list[str] = []
        row_evidence: list[str] = []

        for key in REQUIRED_FIELDS:
            if not str(row.get(key) or "").strip():
                row_blockers.append(f"missing required field {key}")
        if instance_id:
            seen[instance_id] = seen.get(instance_id, 0) + 1

        for key in REQUIRED_PATH_FIELDS:
            value = str(row.get(key) or "")
            expect_dir = key == "repo_dir"
            if value and path_exists(value, expect_dir=expect_dir):
                row_evidence.append(f"{key} exists")
            elif value:
                row_blockers.append(f"{key} path does not exist: {value}")

        if row.get("victim_log"):
            if path_exists(str(row.get("victim_log"))):
                row_evidence.append("victim_log exists")
            else:
                row_blockers.append(f"victim_log path does not exist: {row.get('victim_log')}")

        if row.get("live_dir"):
            if path_exists(str(row.get("live_dir")), expect_dir=True):
                row_evidence.append("live_dir exists")
            else:
                row_blockers.append(f"live_dir path does not exist or is not a directory: {row.get('live_dir')}")

        if row.get("harness_matrix_summary"):
            if path_exists(str(row.get("harness_matrix_summary"))):
                row_evidence.append("harness_matrix_summary exists")
            else:
                row_blockers.append(f"harness_matrix_summary path does not exist: {row.get('harness_matrix_summary')}")

        if row.get("harness_matrix_audit"):
            if path_exists(str(row.get("harness_matrix_audit"))):
                row_evidence.append("harness_matrix_audit exists")
            else:
                row_blockers.append(f"harness_matrix_audit path does not exist: {row.get('harness_matrix_audit')}")

        harness_evidence, harness_blockers = validate_harness_matrix_source(row)
        row_evidence.extend(harness_evidence)
        row_blockers.extend(harness_blockers)

        has_annotations = bool(row.get("frontier_annotations"))
        if has_annotations:
            if path_exists(str(row.get("frontier_annotations"))):
                row_evidence.append("frontier_annotations exists")
            else:
                row_blockers.append(f"frontier_annotations path does not exist: {row.get('frontier_annotations')}")
        elif require_annotations:
            row_blockers.append("missing frontier_annotations")
        else:
            row_warnings.append("frontier_annotations missing; prepare step will generate disabled template")

        if row.get("matrix_summary"):
            matrix_path = str(row.get("matrix_summary"))
            if not path_exists(matrix_path):
                row_blockers.append(f"matrix_summary path does not exist: {matrix_path}")
            elif instance_id:
                matrix_evidence, matrix_warnings, matrix_blockers = validate_matrix_summary(
                    matrix_path,
                    expected_trace=expected_trace_path(out_dir, instance_id, has_annotations=has_annotations),
                    expected_instance=instance_id,
                )
                row_evidence.extend(matrix_evidence)
                row_warnings.extend(matrix_warnings)
                row_blockers.extend(matrix_blockers)
        elif require_matrix_summary:
            row_blockers.append("missing matrix_summary")
        else:
            row_warnings.append("matrix_summary missing; collection cannot be L5-ready until matrix runs")

        instances.append(
            {
                "index": idx,
                "instance_id": instance_id,
                "expected_trace": str(expected_trace_path(out_dir, instance_id, has_annotations=has_annotations)) if instance_id else "",
                "evidence": row_evidence,
                "warnings": row_warnings,
                "blockers": row_blockers,
            }
        )
        warnings.extend(f"{instance_id or f'instances[{idx}]'}: {item}" for item in row_warnings)
        blockers.extend(f"{instance_id or f'instances[{idx}]'}: {item}" for item in row_blockers)

    duplicates = {iid: count for iid, count in seen.items() if count > 1}
    for iid, count in duplicates.items():
        blockers.append(f"duplicate instance_id {iid!r} appears {count} times")

    if blockers:
        status = "fail"
    elif warnings:
        status = "partial"
    else:
        status = "pass"
    return {
        "schema": SCHEMA,
        "status": status,
        "manifest": str(manifest_path),
        "out_dir": str(out_dir),
        "summary": {
            "instances": len(instances),
            "warnings": len(warnings),
            "blockers": len(blockers),
            "duplicates": duplicates,
        },
        "warnings": warnings,
        "blockers": blockers,
        "instances": instances,
        "notes": [
            "pass means the manifest is internally consistent for bundle preparation; it does not prove experiments have run",
            "missing matrix summaries are warnings by default because prepare_bundle_collection.py can generate run scripts first",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight-check a FlowCausal real-agent collection manifest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--out-dir", required=True, help="same output directory intended for prepare_bundle_collection.py")
    parser.add_argument("--require-frontier-annotations", action="store_true")
    parser.add_argument("--require-matrix-summary", action="store_true")
    parser.add_argument("--output", help="write preflight JSON report here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = validate_manifest(
        pathlib.Path(args.manifest).resolve(),
        out_dir=pathlib.Path(args.out_dir).resolve(),
        require_annotations=bool(args.require_frontier_annotations),
        require_matrix_summary=bool(args.require_matrix_summary),
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Build a FlowCausal real-agent bundle manifest from raw artifact directories.

The EuroSys evidence path needs a boring-but-critical intake step: given a pile
of SWE-agent/OpenHands trajectories, cloned benchmark repos, full-harness
endpoint results, optional frontier labels, and optional replay matrix summaries,
produce the JSON manifest consumed by ``prepare_bundle_collection.py``.

This script is intentionally conservative.  It does not copy artifacts and it
does not run experiments.  It only discovers candidate instances, matches them
by ``instance_id``, writes a runnable manifest for instances with the required
raw/repo/endpoint triple, and writes a report explaining every missing edge.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from normalize_full_harness_result import collect_candidates, merge_candidates, read_json_or_jsonl
from validate_bundle_collection_manifest import safe_name


SCHEMA = "flowcausal.real_agent_manifest_intake.v1"
RAW_SUFFIXES = {".traj", ".json", ".jsonl"}
LABEL_SUFFIXES = {".json", ".jsonl"}
MATRIX_FILENAMES = {"matrix-summary.json", "matrix.json"}
INSTANCE_KEYS = ("instance_id", "task_id", "problem_id", "sample_id", "id")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json_like(path: pathlib.Path) -> tuple[Any | None, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return None, f"cannot read {path}: {exc}"
    try:
        if path.suffix.lower() == ".jsonl":
            rows: list[Any] = []
            for line_no, line in enumerate(text.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    return None, f"invalid JSONL at {path}:{line_no}: {exc}"
            return rows, ""
        return json.loads(text), ""
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON at {path}: {exc}"


def path_list(values: list[str]) -> list[pathlib.Path]:
    return [pathlib.Path(value).expanduser().resolve() for value in values]


def iter_files(roots: list[pathlib.Path], *, suffixes: set[str]) -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    seen: set[pathlib.Path] = set()
    for root in roots:
        if not root.exists():
            continue
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            if path.suffix.lower() not in suffixes:
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            out.append(resolved)
    return sorted(out)


def cleaned_stem(path: pathlib.Path) -> str:
    stem = path.name
    for suffix in (
        ".frontier-labels.json",
        ".frontier-labels.jsonl",
        ".frontier_labels.json",
        ".frontier_labels.jsonl",
        ".labels.json",
        ".labels.jsonl",
        ".label.json",
        ".label.jsonl",
        ".full-harness.json",
        ".full_harness.json",
        ".endpoint.json",
        ".result.json",
        ".results.json",
        ".traj",
        ".jsonl",
        ".json",
    ):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem


def first_instance_id_from_value(value: Any) -> str:
    if isinstance(value, dict):
        for key in INSTANCE_KEYS:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
        for key in ("info", "metadata", "task", "instance", "run", "args"):
            found = first_instance_id_from_value(value.get(key))
            if found:
                return found
    if isinstance(value, list):
        for item in value[:20]:
            found = first_instance_id_from_value(item)
            if found:
                return found
    return ""


def infer_raw_format(path: pathlib.Path, data: Any) -> str:
    name = path.name.lower()
    if path.suffix.lower() == ".traj" or "swe-agent" in name or "swe_agent" in name:
        return "swe-agent"
    if isinstance(data, dict):
        if data.get("schema") == "flowcausal.agent_trace.v1" or "commands" in data:
            return "flowcausal"
        if isinstance(data.get("trajectory"), list):
            return "swe-agent"
        if any(key in data for key in ("history", "agent_controller", "openhands_version")):
            return "openhands"
    if path.suffix.lower() == ".jsonl" or "openhands" in name:
        return "openhands"
    return ""


def discover_raw_outputs(roots: list[pathlib.Path]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for path in iter_files(roots, suffixes=RAW_SUFFIXES):
        data, error = read_json_like(path)
        if error:
            warnings.append(error)
            continue
        raw_format = infer_raw_format(path, data)
        if not raw_format:
            continue
        instance_id = first_instance_id_from_value(data) or cleaned_stem(path)
        if not instance_id:
            warnings.append(f"raw output has no instance_id and no usable filename stem: {path}")
            continue
        by_instance.setdefault(instance_id, []).append(
            {
                "path": str(path),
                "raw_format": raw_format,
            }
        )
    return by_instance, warnings


def discover_endpoints(roots: list[pathlib.Path]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for path in iter_files(roots, suffixes={".json", ".jsonl"}):
        try:
            data, _kind, _rows = read_json_or_jsonl(path)
        except SystemExit as exc:
            warnings.append(str(exc))
            continue
        candidates = merge_candidates(collect_candidates(data))
        if not candidates:
            fallback = cleaned_stem(path)
            if fallback:
                candidates = [{"instance_id": fallback, "source_paths": ["filename-stem"]}]
        for candidate in candidates:
            instance_id = str(candidate.get("instance_id") or "").strip()
            if not instance_id:
                continue
            by_instance.setdefault(instance_id, []).append(
                {
                    "path": str(path),
                    "candidate": candidate,
                }
            )
    return by_instance, warnings


def discover_labels(roots: list[pathlib.Path]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    for path in iter_files(roots, suffixes=LABEL_SUFFIXES):
        data, error = read_json_like(path)
        if error:
            warnings.append(error)
            continue
        instance_id = first_instance_id_from_value(data) or cleaned_stem(path)
        if not instance_id:
            warnings.append(f"label file has no instance_id and no usable filename stem: {path}")
            continue
        by_instance.setdefault(instance_id, []).append({"path": str(path)})
    return by_instance, warnings


def matrix_instance_ids(matrix: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for variant in as_list(matrix.get("variants")):
        for trial in as_list(as_dict(variant).get("trials")):
            begin = as_dict(as_dict(as_dict(trial).get("workload")).get("workload_begin"))
            value = begin.get("instance_id")
            if isinstance(value, str) and value.strip():
                ids.append(value.strip())
    return sorted(set(ids))


def discover_matrices(roots: list[pathlib.Path]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    files = [
        path
        for path in iter_files(roots, suffixes={".json"})
        if path.name in MATRIX_FILENAMES or path.name.endswith(".matrix-summary.json")
    ]
    for path in files:
        data, error = read_json_like(path)
        if error:
            warnings.append(error)
            continue
        matrix = as_dict(data)
        ids = matrix_instance_ids(matrix)
        if not ids:
            fallback = path.parent.name if path.name in MATRIX_FILENAMES else cleaned_stem(path)
            if fallback:
                ids = [fallback]
        for instance_id in ids:
            by_instance.setdefault(instance_id, []).append({"path": str(path)})
    return by_instance, warnings


def discover_repos(roots: list[pathlib.Path]) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    warnings: list[str] = []
    seen: set[pathlib.Path] = set()
    for root in roots:
        if not root.exists():
            warnings.append(f"repo root does not exist: {root}")
            continue
        if root.is_dir():
            children = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")]
            candidates = [root] if (root / ".git").exists() or not children else []
            candidates.extend(children)
        else:
            candidates = []
        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            instance_id = path.name
            by_instance.setdefault(instance_id, []).append({"path": str(resolved)})
            safe = safe_name(instance_id)
            if safe != instance_id:
                by_instance.setdefault(safe, []).append({"path": str(resolved)})
    return by_instance, warnings


def choose_one(entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    return entries[0] if entries else None


def rel(path_text: str, base_dir: pathlib.Path) -> str:
    path = pathlib.Path(path_text).resolve()
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        try:
            return str(path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return str(path)


def default_block(args: argparse.Namespace) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key in (
        "source",
        "agent_repo_root",
        "taskset",
        "trials",
        "annotator",
        "annotation_source",
        "scheduler_variant",
    ):
        value = getattr(args, key)
        if value is not None and value != "":
            defaults[key] = value
    if args.raw_format != "auto":
        defaults["raw_format"] = args.raw_format
    return defaults


def build_manifest(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    output = pathlib.Path(args.output).expanduser().resolve()
    base_dir = output.parent
    raw, raw_warnings = discover_raw_outputs(path_list(args.raw_dir))
    endpoints, endpoint_warnings = discover_endpoints(path_list(args.endpoint_dir))
    repos, repo_warnings = discover_repos(path_list(args.repo_root))
    labels, label_warnings = discover_labels(path_list(args.label_dir)) if args.label_dir else ({}, [])
    matrices, matrix_warnings = discover_matrices(path_list(args.matrix_dir)) if args.matrix_dir else ({}, [])

    all_ids = sorted(set(raw) | set(endpoints) | set(repos) | set(labels) | set(matrices))
    manifest_instances: list[dict[str, Any]] = []
    report_instances: list[dict[str, Any]] = []
    duplicate_raw = {iid for iid, entries in raw.items() if len(entries) > 1}

    for instance_id in all_ids:
        blockers: list[str] = []
        warnings: list[str] = []
        raw_entry = choose_one(raw.get(instance_id, []))
        endpoint_entry = choose_one(endpoints.get(instance_id, []))
        repo_entry = choose_one(repos.get(instance_id, []))
        label_entry = choose_one(labels.get(instance_id, []))
        matrix_entry = choose_one(matrices.get(instance_id, []))

        if not raw_entry:
            blockers.append("missing raw_agent_output")
        if not endpoint_entry:
            blockers.append("missing full_harness_result")
        if not repo_entry:
            blockers.append("missing repo_dir")
        if instance_id in duplicate_raw:
            blockers.append("multiple raw_agent_output files match this instance")
        if len(endpoints.get(instance_id, [])) > 1:
            warnings.append("multiple full_harness_result files match this instance; using first")
        if len(repos.get(instance_id, [])) > 1:
            warnings.append("multiple repo_dir candidates match this instance; using first")
        if args.require_frontier_annotations and not label_entry:
            blockers.append("missing frontier_annotations")
        elif not label_entry:
            warnings.append("frontier_annotations missing")
        if args.require_matrix_summary and not matrix_entry:
            blockers.append("missing matrix_summary")
        elif not matrix_entry:
            warnings.append("matrix_summary missing")

        row: dict[str, Any] = {"instance_id": instance_id}
        if raw_entry:
            row["raw_agent_output"] = rel(raw_entry["path"], base_dir)
            if args.raw_format == "auto":
                row["raw_format"] = raw_entry.get("raw_format", "")
        if endpoint_entry:
            row["full_harness_result"] = rel(endpoint_entry["path"], base_dir)
        if repo_entry:
            row["repo_dir"] = rel(repo_entry["path"], base_dir)
        if label_entry:
            row["frontier_annotations"] = rel(label_entry["path"], base_dir)
        if matrix_entry:
            row["matrix_summary"] = rel(matrix_entry["path"], base_dir)

        ready = not blockers
        report_instances.append(
            {
                "instance_id": instance_id,
                "ready": ready,
                "manifest_row": row,
                "blockers": blockers,
                "warnings": warnings,
                "matches": {
                    "raw_agent_output": raw.get(instance_id, []),
                    "repo_dir": repos.get(instance_id, []),
                    "full_harness_result": endpoints.get(instance_id, []),
                    "frontier_annotations": labels.get(instance_id, []),
                    "matrix_summary": matrices.get(instance_id, []),
                },
            }
        )
        if ready or args.include_incomplete:
            manifest_instances.append(row)

    manifest = {
        "defaults": default_block(args),
        "instances": manifest_instances,
    }
    ready_instances = sum(1 for item in report_instances if item["ready"])
    report = {
        "schema": SCHEMA,
        "status": "pass" if ready_instances else "fail",
        "output_manifest": str(output),
        "summary": {
            "raw_instances": len(raw),
            "repo_instances": len(repos),
            "endpoint_instances": len(endpoints),
            "label_instances": len(labels),
            "matrix_instances": len(matrices),
            "ready_instances": ready_instances,
            "manifest_instances": len(manifest_instances),
        },
        "warnings": [*raw_warnings, *endpoint_warnings, *repo_warnings, *label_warnings, *matrix_warnings],
        "instances": report_instances,
        "notes": [
            "ready means the intake step found the raw/repo/endpoint triple and any user-required optional artifacts",
            "the generated manifest still must pass validate_bundle_collection_manifest.py before use",
        ],
    }
    return manifest, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Discover real agent artifacts and build a prepare_bundle_collection.py manifest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--raw-dir", action="append", required=True, help="directory/file with SWE-agent/OpenHands traces")
    parser.add_argument("--repo-root", action="append", required=True, help="directory containing per-instance repos")
    parser.add_argument("--endpoint-dir", action="append", required=True, help="directory/file with full harness endpoint JSON/JSONL")
    parser.add_argument("--label-dir", action="append", default=[], help="optional directory/file with frontier label JSON/JSONL")
    parser.add_argument("--matrix-dir", action="append", default=[], help="optional directory/file with replay matrix summaries")
    parser.add_argument("--output", required=True, help="write collection manifest here")
    parser.add_argument("--report", help="write intake report here")
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="real-agent")
    parser.add_argument("--agent-repo-root", default="")
    parser.add_argument("--taskset", default="")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", default="")
    parser.add_argument("--scheduler-variant", default="")
    parser.add_argument("--require-frontier-annotations", action="store_true")
    parser.add_argument("--require-matrix-summary", action="store_true")
    parser.add_argument("--include-incomplete", action="store_true", help="include incomplete rows in manifest for manual editing")
    parser.add_argument("--allow-empty", action="store_true", help="exit 0 even when no ready manifest rows are found")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = pathlib.Path(args.output).expanduser().resolve()
    report_path = pathlib.Path(args.report).expanduser().resolve() if args.report else output.with_suffix(".intake-report.json")
    manifest, report = build_manifest(args)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"manifest": str(output), "report": str(report_path), **report["summary"]}, sort_keys=True))
    if not manifest["instances"] and not args.allow_empty:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

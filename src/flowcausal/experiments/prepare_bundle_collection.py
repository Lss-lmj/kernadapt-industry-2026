#!/usr/bin/env python3
"""Prepare and audit a collection of real-agent FlowCausal bundles.

``prepare_agent_trace_bundle.py`` handles one benchmark instance.  This script
handles a JSON manifest with many instances, prepares each bundle using the same
artifact layout, and then runs ``audit_bundle_collection.py`` over the resulting
per-instance audits.

The default mode is still conservative: it does not run sched_ext matrices
unless ``--run-matrix`` is passed.  If an instance entry provides an existing
``matrix_summary``, the per-instance prepare step can audit that bundle
immediately; otherwise the generated collection audit truthfully reports that
no completed bundle audits are present yet.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any

from validate_bundle_collection_manifest import validate_manifest


HERE = pathlib.Path(__file__).resolve().parent
WORKSPACE = HERE.parent.parent


BOOLEAN_KEYS = {
    "replace_heuristic",
    "emit_heuristic_frontier_labels",
}
PATH_KEYS = {
    "raw_agent_output",
    "repo_dir",
    "frontier_annotations",
    "full_harness_result",
    "matrix_summary",
    "victim_log",
    "live_dir",
    "harness_matrix_summary",
    "harness_matrix_audit",
}
OPTION_MAP = {
    "raw_format": "--raw-format",
    "source": "--source",
    "agent_repo_root": "--agent-repo-root",
    "annotator": "--annotator",
    "annotation_source": "--annotation-source",
    "critical_policy": "--critical-policy",
    "full_harness_format": "--full-harness-format",
    "scheduler_variant": "--scheduler-variant",
    "trials": "--trials",
    "taskset": "--taskset",
    "frontier_width": "--frontier-width",
    "max_frontier_width": "--max-frontier-width",
    "build_frontier_admission": "--build-frontier-admission",
    "frontier_stability_action": "--frontier-stability-action",
    "victim_command": "--victim-command",
    "victim_taskset": "--victim-taskset",
    "victim_warmup_sec": "--victim-warmup-sec",
    "matrix_extra_args": "--matrix-extra-args",
    "harness_matrix_source_variant": "--harness-matrix-source-variant",
    "harness_matrix_source_trial": "--harness-matrix-source-trial",
    "harness_matrix_trial_selection": "--harness-matrix-trial-selection",
    "created_at": "--created-at",
}


def safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return out.strip("-") or "instance"


def sh(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run(cmd: list[str], *, cwd: pathlib.Path = WORKSPACE) -> None:
    print("+ " + sh(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_manifest(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("collection manifest must be a JSON object")
    if not isinstance(data.get("instances"), list):
        raise SystemExit("collection manifest must contain an instances list")
    return data


def resolve_path(value: Any, base_dir: pathlib.Path) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = pathlib.Path(text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def merged_instance(defaults: dict[str, Any], instance: dict[str, Any], base_dir: pathlib.Path) -> dict[str, Any]:
    merged = dict(defaults)
    merged.update(instance)
    for key in PATH_KEYS:
        if merged.get(key):
            merged[key] = resolve_path(merged.get(key), base_dir)
    return merged


def require_field(row: dict[str, Any], key: str) -> str:
    value = str(row.get(key) or "").strip()
    if not value:
        raise SystemExit(f"instance entry missing required field: {key}")
    return value


def build_prepare_cmd(
    row: dict[str, Any],
    *,
    bundle_dir: pathlib.Path,
    overwrite: bool,
    run_matrix: bool,
    cli_created_at: str,
) -> list[str]:
    instance_id = require_field(row, "instance_id")
    cmd = [
        sys.executable,
        str(HERE / "prepare_agent_trace_bundle.py"),
        "--out-dir",
        str(bundle_dir),
        "--raw-agent-output",
        require_field(row, "raw_agent_output"),
        "--instance-id",
        instance_id,
        "--repo-dir",
        require_field(row, "repo_dir"),
        "--full-harness-result",
        require_field(row, "full_harness_result"),
    ]
    if overwrite:
        cmd.append("--overwrite")
    if run_matrix:
        cmd.append("--run-matrix")

    path_options = {
        "frontier_annotations": "--frontier-annotations",
        "matrix_summary": "--matrix-summary",
        "harness_matrix_summary": "--harness-matrix-summary",
        "harness_matrix_audit": "--harness-matrix-audit",
        "victim_log": "--victim-log",
        "live_dir": "--live-dir",
    }
    for key, option in path_options.items():
        if row.get(key):
            cmd.extend([option, str(row[key])])
    for key, option in OPTION_MAP.items():
        value = cli_created_at if key == "created_at" and cli_created_at else row.get(key)
        if value is None or value == "":
            continue
        cmd.extend([option, str(value)])
    for key in BOOLEAN_KEYS:
        if bool(row.get(key)):
            cmd.append("--" + key.replace("_", "-"))
    return cmd


def source_artifacts(row: dict[str, Any]) -> dict[str, str]:
    keys = [
        "raw_agent_output",
        "repo_dir",
        "frontier_annotations",
        "full_harness_result",
        "matrix_summary",
        "victim_log",
        "live_dir",
        "harness_matrix_summary",
        "harness_matrix_audit",
    ]
    return {key: str(row.get(key) or "") for key in keys if row.get(key)}


def harness_matrix_provenance(row: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "harness_matrix_source_variant",
        "harness_matrix_source_trial",
        "harness_matrix_trial_selection",
        "scheduler_variant",
    ]
    return {key: row.get(key) for key in keys if row.get(key) not in (None, "")}


def write_script(path: pathlib.Path, commands: list[list[str]], *, cwd: pathlib.Path = WORKSPACE) -> None:
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", f"cd {shlex.quote(str(cwd))}"]
    lines.extend(sh(cmd) for cmd in commands)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def build_collection_audit_cmd(
    *,
    bundles_dir: pathlib.Path,
    min_ready_bundles: int,
    output_json: pathlib.Path,
    output_md: pathlib.Path,
) -> list[str]:
    return [
        sys.executable,
        str(HERE / "audit_bundle_collection.py"),
        str(bundles_dir),
        "--recursive",
        "--min-ready-bundles",
        str(min_ready_bundles),
        "--output",
        str(output_json),
        "--markdown-output",
        str(output_md),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare many FlowCausal real-agent evidence bundles from a JSON manifest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--manifest", required=True, help="JSON file with defaults and instances[]")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--run-matrix", action="store_true")
    parser.add_argument("--min-ready-bundles", type=int, default=3)
    parser.add_argument("--created-at", help="override created_at forwarded to child prepare commands")
    parser.add_argument(
        "--preflight-output",
        help="write validate_bundle_collection_manifest.py-style report before preparing bundles",
    )
    parser.add_argument(
        "--strict-preflight",
        action="store_true",
        help="run preflight in paper-ready mode: require frontier annotations and matrix summaries",
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="write scripts and manifest but do not execute child prepare commands",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = pathlib.Path(args.manifest).resolve()
    manifest = read_manifest(manifest_path)
    base_dir = manifest_path.parent
    out_dir = pathlib.Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    bundles_dir = out_dir / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)

    preflight_output = pathlib.Path(args.preflight_output).resolve() if args.preflight_output else None
    preflight_report: dict[str, Any] | None = None
    if preflight_output or args.strict_preflight:
        preflight_report = validate_manifest(
            manifest_path,
            out_dir=out_dir,
            require_annotations=bool(args.strict_preflight),
            require_matrix_summary=bool(args.strict_preflight),
        )
        preflight_output = preflight_output or (
            out_dir / ("manifest-preflight.strict.json" if args.strict_preflight else "manifest-preflight.json")
        )
        preflight_output.parent.mkdir(parents=True, exist_ok=True)
        preflight_output.write_text(
            json.dumps(preflight_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if preflight_report.get("status") == "fail":
            raise SystemExit(f"manifest preflight failed; see {preflight_output}")

    defaults = as_dict(manifest.get("defaults"))
    prepare_commands: list[list[str]] = []
    instances_out: list[dict[str, Any]] = []
    seen_instances: set[str] = set()
    for raw_instance in as_list(manifest.get("instances")):
        if not isinstance(raw_instance, dict):
            raise SystemExit("instances entries must be JSON objects")
        row = merged_instance(defaults, raw_instance, base_dir)
        instance_id = require_field(row, "instance_id")
        if instance_id in seen_instances:
            raise SystemExit(f"duplicate instance_id in collection manifest: {instance_id}")
        seen_instances.add(instance_id)
        bundle_dir = bundles_dir / safe_name(instance_id)
        cmd = build_prepare_cmd(
            row,
            bundle_dir=bundle_dir,
            overwrite=args.overwrite,
            run_matrix=args.run_matrix,
            cli_created_at=args.created_at or "",
        )
        prepare_commands.append(cmd)
        instances_out.append(
            {
                "instance_id": instance_id,
                "bundle_dir": str(bundle_dir),
                "command": cmd,
                "expected_audit": str(bundle_dir / "agent-trace-bundle-audit.json"),
                "expected_audit_markdown": str(bundle_dir / "agent-trace-bundle-audit.md"),
                "source_artifacts": source_artifacts(row),
                "harness_matrix_provenance": harness_matrix_provenance(row),
            }
        )

    collection_json = out_dir / "collection-audit.json"
    collection_md = out_dir / "collection-audit.md"
    collection_cmd = build_collection_audit_cmd(
        bundles_dir=bundles_dir,
        min_ready_bundles=args.min_ready_bundles,
        output_json=collection_json,
        output_md=collection_md,
    )

    write_script(out_dir / "prepare-all.sh", prepare_commands)
    write_script(out_dir / "audit-collection.sh", [collection_cmd])
    if not args.generate_only:
        for cmd in prepare_commands:
            run(cmd)
        run(collection_cmd)

    output_manifest = {
        "schema": "flowcausal.bundle_collection_manifest.v1",
        "created_at": args.created_at or time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "source_manifest": str(manifest_path),
        "out_dir": str(out_dir),
        "bundles_dir": str(bundles_dir),
        "run_matrix": bool(args.run_matrix),
        "generate_only": bool(args.generate_only),
        "min_ready_bundles": args.min_ready_bundles,
        "instances": instances_out,
        "commands": {
            "prepare_all_script": str(out_dir / "prepare-all.sh"),
            "audit_collection_script": str(out_dir / "audit-collection.sh"),
            "audit_bundle_collection": collection_cmd,
        },
        "artifacts": {
            "collection_audit_json": str(collection_json),
            "collection_audit_markdown": str(collection_md),
            "manifest_preflight_json": str(preflight_output) if preflight_output else "",
        },
        "preflight": preflight_report,
        "notes": [
            "This manifest records batch preparation commands; collection pass still requires per-instance audits.",
            "Use --run-matrix only on a machine ready to attach the FlowCausal scheduler.",
        ],
    }
    (out_dir / "bundle-collection-manifest.json").write_text(
        json.dumps(output_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({"out_dir": str(out_dir), "instances": len(instances_out)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

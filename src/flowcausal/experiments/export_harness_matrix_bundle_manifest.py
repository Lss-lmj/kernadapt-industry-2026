#!/usr/bin/env python3
"""Export a real-agent bundle manifest from a full-harness endpoint matrix.

After ``run_real_agent_harness_matrix.py --execute`` or
``import_real_agent_harness_matrix.py`` we have many raw trajectory and endpoint
artifacts for the same benchmark instance: one per scheduler variant and trial.
``build_real_agent_manifest.py`` can discover artifacts from directories, but it
cannot know which trial should be the canonical trace source for replay/bundle
auditing.

This helper makes that choice explicit and auditable.  It reads
``harness-matrix-summary.json`` plus an optional
``audit_real_agent_harness_matrix.py`` report, selects one pass/resolved trial
from a named variant, and writes the JSON manifest consumed by
``prepare_bundle_collection.py``.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
from typing import Any

from validate_bundle_collection_manifest import safe_name


SCHEMA = "flowcausal.harness_matrix_bundle_manifest_export.v1"
MATRIX_SCHEMA = "flowcausal.real_agent_harness_matrix.v1"
AUDIT_SCHEMA = "flowcausal.real_agent_harness_matrix_audit.v1"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path_text: str) -> dict[str, Any]:
    path = pathlib.Path(path_text).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"JSON file is not an object: {path}")
    data["_path"] = str(path)
    return data


def fnum(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0.0 else None


def rel(path_text: str, base_dir: pathlib.Path) -> str:
    path = pathlib.Path(path_text).expanduser().resolve()
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        try:
            return str(path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return str(path)


def resolve_optional_path(path_text: str, base_dir: pathlib.Path) -> str:
    text = str(path_text or "").strip()
    if not text:
        return ""
    path = pathlib.Path(text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return str(path.resolve())


def matrix_manifest_repo_dir(path_text: str) -> str:
    if not path_text:
        return ""
    manifest = read_json(path_text)
    return str(manifest.get("repo_dir") or "")


def audit_selected_flow(audit: dict[str, Any] | None) -> str:
    if not audit:
        return ""
    return str(as_dict(audit.get("summary")).get("flowcausal_variant") or "")


def trials_for(summary: dict[str, Any], variant: str) -> list[dict[str, Any]]:
    return [
        row
        for row in as_list(summary.get("trials"))
        if isinstance(row, dict) and str(row.get("variant") or "") == variant
    ]


def choose_variant(summary: dict[str, Any], audit: dict[str, Any] | None, requested: str) -> str:
    if requested:
        if requested == "audited-flowcausal":
            selected = audit_selected_flow(audit)
            if selected:
                return selected
            raise SystemExit("--source-variant audited-flowcausal requires --harness-matrix-audit with flowcausal_variant")
        return requested
    baseline = str(summary.get("baseline_variant") or "")
    return baseline or "default"


def pass_resolved_trials(rows: list[dict[str, Any]], *, allow_unresolved: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("status") != "pass":
            continue
        if row.get("run_returncode") not in (0, None):
            continue
        if not allow_unresolved and row.get("resolved") is not True:
            continue
        out.append(row)
    return out


def select_trial(rows: list[dict[str, Any]], strategy: str) -> dict[str, Any] | None:
    if not rows:
        return None
    if strategy == "first-pass":
        return sorted(rows, key=lambda row: int(row.get("trial") or 0))[0]
    with_elapsed = [(row, fnum(row.get("elapsed_sec"))) for row in rows]
    with_elapsed = [(row, elapsed) for row, elapsed in with_elapsed if elapsed is not None]
    if not with_elapsed:
        return sorted(rows, key=lambda row: int(row.get("trial") or 0))[0]
    if strategy == "fastest":
        return min(with_elapsed, key=lambda item: (item[1], int(item[0].get("trial") or 0)))[0]
    if strategy == "slowest":
        return max(with_elapsed, key=lambda item: (item[1], -int(item[0].get("trial") or 0)))[0]
    if strategy == "median-elapsed":
        elapsed_values = [item[1] for item in with_elapsed]
        median = statistics.median(elapsed_values)
        return min(with_elapsed, key=lambda item: (abs(item[1] - median), int(item[0].get("trial") or 0)))[0]
    raise SystemExit(f"unknown trial selection strategy: {strategy}")


def check_existing_file(path_text: str, label: str, blockers: list[str], evidence: list[str]) -> None:
    if not path_text:
        blockers.append(f"missing {label} path")
        return
    path = pathlib.Path(path_text)
    if not path.exists():
        blockers.append(f"{label} path does not exist: {path}")
    else:
        evidence.append(f"{label} exists: {path}")


def check_existing_dir(path_text: str, label: str, blockers: list[str], evidence: list[str]) -> None:
    if not path_text:
        blockers.append(f"missing {label} path")
        return
    path = pathlib.Path(path_text)
    if not path.is_dir():
        blockers.append(f"{label} path does not exist or is not a directory: {path}")
    else:
        evidence.append(f"{label} exists: {path}")


def build_defaults(args: argparse.Namespace, source_variant: str) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "source": args.source,
        "scheduler_variant": source_variant,
    }
    for key in ("agent_repo_root", "taskset", "annotator", "annotation_source"):
        value = getattr(args, key)
        if value:
            defaults[key] = value
    if args.trials is not None:
        defaults["trials"] = args.trials
    if args.raw_format != "auto":
        defaults["raw_format"] = args.raw_format
    return defaults


def make_next_commands(output: pathlib.Path, args: argparse.Namespace) -> dict[str, list[str]]:
    out_dir = pathlib.Path(args.bundle_out_dir).expanduser().resolve() if args.bundle_out_dir else output.parent / "bundles-from-harness-matrix"
    return {
        "preflight": [
            sys.executable,
            str(pathlib.Path(__file__).resolve().parent / "validate_bundle_collection_manifest.py"),
            "--manifest",
            str(output),
            "--out-dir",
            str(out_dir),
            "--output",
            str(out_dir / "manifest-preflight.json"),
        ],
        "prepare": [
            sys.executable,
            str(pathlib.Path(__file__).resolve().parent / "prepare_bundle_collection.py"),
            "--manifest",
            str(output),
            "--out-dir",
            str(out_dir),
            "--preflight-output",
            str(out_dir / "manifest-preflight.json"),
        ],
    }


def summarize(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], int]:
    output = pathlib.Path(args.output).expanduser().resolve()
    base_dir = output.parent
    summary = read_json(args.summary)
    audit = read_json(args.harness_matrix_audit) if args.harness_matrix_audit else None
    manifest_repo = matrix_manifest_repo_dir(args.harness_matrix_manifest)
    repo_dir = resolve_optional_path(args.repo_dir or manifest_repo, pathlib.Path(args.summary).expanduser().resolve().parent)
    source_variant = choose_variant(summary, audit, args.source_variant)
    rows = trials_for(summary, source_variant)
    candidates = pass_resolved_trials(rows, allow_unresolved=args.allow_unresolved)
    selected = select_trial(candidates, args.trial_selection)

    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []

    if summary.get("schema") not in {MATRIX_SCHEMA, None}:
        warnings.append(f"unexpected harness matrix summary schema: {summary.get('schema')}")
    if audit:
        evidence.append(f"harness matrix audit status={audit.get('status')}")
        if audit.get("schema") not in {AUDIT_SCHEMA, None}:
            warnings.append(f"unexpected harness matrix audit schema: {audit.get('schema')}")
        if audit.get("status") != "pass" and not args.allow_partial_audit:
            blockers.append(f"harness matrix audit is not pass: {audit.get('status')}")
    else:
        warnings.append("no harness matrix audit provided; export is usable for plumbing but weak for paper evidence")

    if not rows:
        blockers.append(f"source variant has no trials in summary: {source_variant}")
    if not candidates:
        blockers.append(
            f"source variant {source_variant} has no pass"
            + ("/resolved" if not args.allow_unresolved else "")
            + " trial"
        )
    if selected is None:
        selected = {}
    instance_id = str(summary.get("instance_id") or selected.get("instance_id") or "").strip()
    if not instance_id:
        blockers.append("missing instance_id in harness matrix summary")
    if not repo_dir:
        blockers.append("--repo-dir is required unless --harness-matrix-manifest contains repo_dir")
    elif not pathlib.Path(repo_dir).is_dir():
        blockers.append(f"repo_dir does not exist or is not a directory: {repo_dir}")
    else:
        evidence.append(f"repo_dir exists: {repo_dir}")

    raw_path = str(selected.get("raw_agent_output") or "")
    endpoint_path = str(selected.get("full_harness_result") or "")
    capture_dir = str(selected.get("capture_dir") or "")
    victim_log = str(selected.get("victim_log") or "")
    live_dir = str(selected.get("live_dir") or "")
    check_existing_file(raw_path, "raw_agent_output", blockers, evidence)
    check_existing_file(endpoint_path, "full_harness_result", blockers, evidence)
    if capture_dir:
        capture_path = pathlib.Path(capture_dir)
        if capture_path.is_dir():
            evidence.append(f"capture_dir exists: {capture_path}")
        else:
            warnings.append(f"capture_dir missing or not a directory: {capture_path}")
    else:
        warnings.append("selected trial has no capture_dir")
    if victim_log:
        check_existing_file(victim_log, "victim_log", blockers, evidence)
    if live_dir:
        check_existing_dir(live_dir, "live_dir", blockers, evidence)
    if "captured-endpoint" in endpoint_path and not args.allow_weak_endpoint:
        blockers.append("selected endpoint is captured command metadata, not a framework-produced full-harness endpoint")
    if selected.get("resolved") is not True and not args.allow_unresolved:
        blockers.append(f"selected trial is not resolved=true: {selected.get('resolved')}")

    frontier_annotations = resolve_optional_path(args.frontier_annotations, base_dir)
    if frontier_annotations:
        check_existing_file(frontier_annotations, "frontier_annotations", blockers, evidence)
    replay_matrix_summary = resolve_optional_path(args.replay_matrix_summary, base_dir)
    if replay_matrix_summary:
        check_existing_file(replay_matrix_summary, "replay_matrix_summary", blockers, evidence)

    manifest_instances: list[dict[str, Any]] = []
    if not blockers:
        row = {
            "instance_id": instance_id,
            "raw_agent_output": rel(raw_path, base_dir),
            "repo_dir": rel(repo_dir, base_dir),
            "full_harness_result": rel(endpoint_path, base_dir),
            "raw_format": args.raw_format,
            "scheduler_variant": source_variant,
            "harness_matrix_summary": rel(str(pathlib.Path(args.summary).expanduser().resolve()), base_dir),
            "harness_matrix_source_variant": source_variant,
            "harness_matrix_source_trial": int(selected.get("trial") or 0),
            "harness_matrix_trial_selection": args.trial_selection,
        }
        if args.harness_matrix_audit:
            row["harness_matrix_audit"] = rel(str(pathlib.Path(args.harness_matrix_audit).expanduser().resolve()), base_dir)
        if frontier_annotations:
            row["frontier_annotations"] = rel(frontier_annotations, base_dir)
        if replay_matrix_summary:
            row["matrix_summary"] = rel(replay_matrix_summary, base_dir)
        if victim_log:
            row["victim_log"] = rel(victim_log, base_dir)
        if live_dir:
            row["live_dir"] = rel(live_dir, base_dir)
        manifest_instances.append(row)

    manifest = {
        "defaults": build_defaults(args, source_variant),
        "instances": manifest_instances,
        "notes": [
            "generated by export_harness_matrix_bundle_manifest.py",
            "the selected raw/endpoint trial comes from a full real-agent harness matrix",
            "run validate_bundle_collection_manifest.py before preparing bundles",
        ],
    }

    if blockers:
        status = "fail"
    elif warnings:
        status = "partial"
    else:
        status = "pass"
    report = {
        "schema": SCHEMA,
        "status": status,
        "summary": {
            "instance_id": instance_id,
            "source_variant": source_variant,
            "trial_selection": args.trial_selection,
            "selected_trial": int(selected.get("trial") or 0) if selected else 0,
            "selected_resolved": selected.get("resolved"),
            "selected_elapsed_sec": selected.get("elapsed_sec"),
            "manifest_instances": len(manifest_instances),
        },
        "artifacts": {
            "harness_matrix_summary": str(pathlib.Path(args.summary).expanduser().resolve()),
            "harness_matrix_audit": str(pathlib.Path(args.harness_matrix_audit).expanduser().resolve()) if args.harness_matrix_audit else "",
            "raw_agent_output": raw_path,
            "full_harness_result": endpoint_path,
            "capture_dir": capture_dir,
            "victim_log": victim_log,
            "live_dir": live_dir,
            "repo_dir": repo_dir,
            "frontier_annotations": frontier_annotations,
            "replay_matrix_summary": replay_matrix_summary,
        },
        "evidence": evidence,
        "warnings": warnings,
        "blockers": blockers,
        "next_commands": make_next_commands(output, args),
    }
    return manifest, report, 0 if manifest_instances else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export prepare_bundle_collection.py manifest from a full real-agent harness matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--summary", required=True, help="harness-matrix-summary.json")
    parser.add_argument("--harness-matrix-audit", help="optional audit_real_agent_harness_matrix.py JSON")
    parser.add_argument("--harness-matrix-manifest", help="optional real-agent-harness-matrix-manifest.json for repo_dir")
    parser.add_argument("--repo-dir", help="repo directory used by replay/bundle preparation")
    parser.add_argument("--output", required=True, help="output prepare_bundle_collection.py manifest")
    parser.add_argument("--report", required=True, help="output export report")
    parser.add_argument(
        "--source-variant",
        default="",
        help="variant to export; default uses matrix baseline_variant; use audited-flowcausal to export audit-selected FlowCausal variant",
    )
    parser.add_argument(
        "--trial-selection",
        choices=("first-pass", "median-elapsed", "fastest", "slowest"),
        default="first-pass",
    )
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="real-agent-harness-matrix")
    parser.add_argument("--agent-repo-root", default="")
    parser.add_argument("--taskset", default="")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", default="manual", choices=("manual", "oracle"))
    parser.add_argument("--frontier-annotations", help="optional manual/oracle annotation file to attach to manifest row")
    parser.add_argument("--replay-matrix-summary", help="optional existing replay matrix-summary.json to attach to manifest row")
    parser.add_argument("--bundle-out-dir", help="path used in generated next_commands")
    parser.add_argument("--allow-partial-audit", action="store_true")
    parser.add_argument("--allow-unresolved", action="store_true")
    parser.add_argument("--allow-weak-endpoint", action="store_true", help="allow captured command metadata endpoint")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest, report, return_code = summarize(args)
    output = pathlib.Path(args.output).expanduser().resolve()
    report_path = pathlib.Path(args.report).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "manifest": str(output), "report": str(report_path)}, sort_keys=True))
    return return_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

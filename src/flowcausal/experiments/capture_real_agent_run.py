#!/usr/bin/env python3
"""Run and archive one real code-agent/harness instance for FlowCausal.

This is the first-mile capture wrapper for SWE-agent/OpenHands/SWE-bench style
experiments.  It runs an arbitrary harness command, saves stdout/stderr and
timing metadata, copies the raw trajectory and endpoint files it produced, and
writes a one-instance manifest that can be fed to
``prepare_bundle_collection.py``.

The wrapper does not infer scientific success.  If the harness does not produce
an endpoint file, it writes a conservative command-metadata endpoint with
``resolved=null`` unless the caller explicitly provides ``--resolved``.  The
bundle and collection audits still decide whether the evidence can support a
paper claim.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any

from harness_replay_system_profile import SystemProfileRecorder, disabled_profile
from validate_bundle_collection_manifest import safe_name


SCHEMA = "flowcausal.real_agent_run_capture.v1"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def parse_bool_text(value: str) -> bool | None:
    text = value.strip().lower()
    if text in {"true", "1", "yes", "resolved", "pass", "passed", "success"}:
        return True
    if text in {"false", "0", "no", "unresolved", "fail", "failed", "error"}:
        return False
    return None


def rel(path_text: str, base_dir: pathlib.Path) -> str:
    path = pathlib.Path(path_text).resolve()
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        try:
            return str(path.relative_to(pathlib.Path.cwd()))
        except ValueError:
            return str(path)


def resolve_after_run(path_text: str, work_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(path_text).expanduser()
    if not path.is_absolute():
        path = work_dir / path
    return path.resolve()


def copy_artifact(src: pathlib.Path, dst_dir: pathlib.Path, dst_name: str) -> pathlib.Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / dst_name
    if src.resolve() == dst.resolve():
        return dst
    shutil.copy2(src, dst)
    return dst


def suffix_for(path: pathlib.Path, fallback: str = ".json") -> str:
    if path.name.endswith(".jsonl"):
        return ".jsonl"
    if path.name.endswith(".traj"):
        return ".traj"
    if path.suffix:
        return path.suffix
    return fallback


def command_from_args(args: argparse.Namespace) -> list[str]:
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    return command


def parse_env(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"--env must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"--env key is empty: {item!r}")
        out[key] = value
    return out


def run_harness(
    command: list[str],
    *,
    cwd: pathlib.Path,
    env_extra: dict[str, str],
    stdout_path: pathlib.Path,
    stderr_path: pathlib.Path,
    system_profile: bool,
    system_profile_interval_ms: int,
    system_profile_stage_state_file: pathlib.Path | None,
    profile_dir: pathlib.Path,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    env = os.environ.copy()
    env.update(env_extra)
    started = time.time()
    profile_result: dict[str, Any] = disabled_profile()
    with stdout_path.open("w", encoding="utf-8", errors="replace") as stdout, stderr_path.open("w", encoding="utf-8", errors="replace") as stderr:
        proc = subprocess.Popen(command, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr, text=True)
        recorder: SystemProfileRecorder | None = None
        if system_profile:
            recorder = SystemProfileRecorder(
                root_pid=proc.pid,
                out_dir=profile_dir,
                stage_state_file=system_profile_stage_state_file,
                interval_ms=system_profile_interval_ms,
                provenance={
                    **provenance,
                    "profile_scope": "real_agent_harness_capture",
                    "command": command,
                    "cwd": str(cwd),
                    "stdout": str(stdout_path),
                    "stderr": str(stderr_path),
                    "stage_state_file": str(system_profile_stage_state_file) if system_profile_stage_state_file else "",
                },
            )
            recorder.start()
        try:
            returncode = proc.wait()
        finally:
            if recorder is not None:
                profile_result = recorder.stop()
    ended = time.time()
    return {
        "command": command,
        "cwd": str(cwd),
        "returncode": returncode,
        "elapsed_sec": ended - started,
        "stdout": str(stdout_path),
        "stderr": str(stderr_path),
        "system_profile": profile_result,
    }


def write_captured_endpoint(
    path: pathlib.Path,
    *,
    instance_id: str,
    resolved: bool | None,
    status: str,
    run: dict[str, Any] | None,
) -> pathlib.Path:
    payload = {
        "schema": "flowcausal.captured_harness_endpoint.v1",
        "instance_id": instance_id,
        "resolved": resolved,
        "elapsed_sec": run.get("elapsed_sec") if run else None,
        "status": status,
        "harness_returncode": run.get("returncode") if run else None,
        "provenance": {
            "capture_schema": SCHEMA,
            "command": run.get("command") if run else [],
            "stdout": run.get("stdout") if run else "",
            "stderr": run.get("stderr") if run else "",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def read_existing_manifest(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"existing manifest is invalid JSON: {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def build_manifest(args: argparse.Namespace, *, row: dict[str, Any] | None, manifest_path: pathlib.Path) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key in ("source", "agent_repo_root", "taskset", "annotator", "annotation_source", "scheduler_variant"):
        value = getattr(args, key)
        if value:
            defaults[key] = value
    if args.trials is not None:
        defaults["trials"] = args.trials
    if args.raw_format:
        defaults["raw_format"] = args.raw_format
    existing = read_existing_manifest(manifest_path)
    merged_defaults = dict(as_dict(existing.get("defaults")))
    merged_defaults.update(defaults)
    existing_rows = [item for item in existing.get("instances", []) if isinstance(item, dict)]
    if row is not None:
        existing_rows = [item for item in existing_rows if str(item.get("instance_id") or "") != args.instance_id]
        existing_rows.append(row)
    return {
        "defaults": merged_defaults,
        "instances": existing_rows,
        "notes": [
            "generated by capture_real_agent_run.py",
            "captures in the same --out-dir are merged by instance_id",
            "run validate_bundle_collection_manifest.py before expensive matrix execution",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run/archive one real SWE-agent/OpenHands/SWE-bench instance for FlowCausal",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", required=True, help="capture output directory")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo-dir", required=True, help="repository directory used by the harness/replay")
    parser.add_argument("--raw-agent-output", help="raw trajectory file produced by the harness")
    parser.add_argument("--endpoint-result", help="full-harness endpoint JSON/JSONL produced by the harness")
    parser.add_argument("--frontier-annotations", help="optional manual/oracle frontier labels to archive")
    parser.add_argument("--matrix-summary", help="optional existing matrix-summary.json to archive")
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="real-agent-capture")
    parser.add_argument("--agent-repo-root", default="")
    parser.add_argument("--taskset", default="")
    parser.add_argument("--trials", type=int)
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", default="manual", choices=("manual", "oracle"))
    parser.add_argument("--scheduler-variant", default="")
    parser.add_argument("--resolved", choices=("true", "false", "unknown"), default="unknown")
    parser.add_argument("--status", default="", help="endpoint status when no endpoint-result is provided")
    parser.add_argument("--work-dir", default=".", help="working directory for the harness command and relative output paths")
    parser.add_argument("--env", action="append", default=[], help="extra environment variable KEY=VALUE for harness command")
    parser.add_argument("--no-run", action="store_true", help="archive existing files without running a harness command")
    parser.add_argument("--allow-command-failure", action="store_true", help="keep manifest row even if the harness command exits nonzero")
    parser.add_argument("--system-profile", action="store_true", help="record a live/original harness process-tree system profile while the command runs")
    parser.add_argument("--system-profile-interval-ms", type=int, default=25, help="sampling interval for --system-profile")
    parser.add_argument("--system-profile-stage-state-file", default="", help="optional stage-state JSON file read by --system-profile; relative paths resolve under --work-dir")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="harness command after --")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    safe = safe_name(args.instance_id)
    work_dir = pathlib.Path(args.work_dir).expanduser().resolve()
    repo_dir = resolve_after_run(args.repo_dir, work_dir)
    raw_dir = out_dir / "raw"
    endpoint_dir = out_dir / "endpoints"
    label_dir = out_dir / "labels"
    matrix_dir = out_dir / "matrices"
    log_dir = out_dir / "logs"
    profile_dir = out_dir / "system-profile" / safe
    log_dir.mkdir(parents=True, exist_ok=True)

    command = command_from_args(args)
    system_profile_stage_state_file = (
        resolve_after_run(args.system_profile_stage_state_file, work_dir)
        if args.system_profile_stage_state_file
        else None
    )
    if not args.no_run and not command:
        raise SystemExit("harness command is required unless --no-run is used")
    if not work_dir.is_dir():
        raise SystemExit(f"--work-dir does not exist or is not a directory: {work_dir}")

    stdout_path = log_dir / f"{safe}.stdout.log"
    stderr_path = log_dir / f"{safe}.stderr.log"
    run: dict[str, Any] | None = None
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []

    if args.no_run:
        warnings.append("--no-run used; command execution was not captured")
        if args.system_profile:
            warnings.append("--system-profile requested with --no-run; profile disabled because no harness process was started")
    else:
        run = run_harness(
            command,
            cwd=work_dir,
            env_extra=parse_env(args.env),
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            system_profile=bool(args.system_profile),
            system_profile_interval_ms=args.system_profile_interval_ms,
            system_profile_stage_state_file=system_profile_stage_state_file,
            profile_dir=profile_dir,
            provenance={
                "capture_schema": SCHEMA,
                "instance_id": args.instance_id,
                "repo_dir": str(repo_dir),
                "source": args.source,
                "scheduler_variant": args.scheduler_variant,
            },
        )
        evidence.append(f"harness command exited with returncode={run['returncode']}")
        if run["returncode"] != 0 and not args.allow_command_failure:
            blockers.append(f"harness command failed with returncode={run['returncode']}")

    if repo_dir.is_dir():
        evidence.append(f"repo_dir exists: {repo_dir}")
    else:
        blockers.append(f"repo_dir does not exist: {repo_dir}")

    archived_raw = ""
    if args.raw_agent_output:
        raw_src = resolve_after_run(args.raw_agent_output, work_dir)
        if raw_src.exists():
            archived = copy_artifact(raw_src, raw_dir, f"{safe}{suffix_for(raw_src, '.traj')}")
            archived_raw = str(archived)
            evidence.append(f"raw agent output archived: {archived}")
        else:
            blockers.append(f"raw_agent_output does not exist: {raw_src}")
    else:
        blockers.append("raw_agent_output was not provided")

    endpoint_resolved = parse_bool_text(args.resolved) if args.resolved != "unknown" else None
    endpoint_status = args.status or ("completed" if (run is None or run.get("returncode") == 0) else "command_failed")
    archived_endpoint = ""
    if args.endpoint_result:
        endpoint_src = resolve_after_run(args.endpoint_result, work_dir)
        if endpoint_src.exists():
            archived = copy_artifact(endpoint_src, endpoint_dir, f"{safe}.full-harness.raw{suffix_for(endpoint_src)}")
            archived_endpoint = str(archived)
            evidence.append(f"full harness endpoint archived: {archived}")
        else:
            blockers.append(f"endpoint_result does not exist: {endpoint_src}")
    if not archived_endpoint:
        captured_endpoint = endpoint_dir / f"{safe}.captured-endpoint.json"
        write_captured_endpoint(
            captured_endpoint,
            instance_id=args.instance_id,
            resolved=endpoint_resolved,
            status=endpoint_status,
            run=run,
        )
        archived_endpoint = str(captured_endpoint)
        warnings.append("using captured command metadata as endpoint; provide --endpoint-result for full harness evidence")
        if endpoint_resolved is None:
            warnings.append("captured endpoint has resolved=null; endpoint improvement gate will remain partial")

    archived_labels = ""
    if args.frontier_annotations:
        label_src = resolve_after_run(args.frontier_annotations, work_dir)
        if label_src.exists():
            archived = copy_artifact(label_src, label_dir, f"{safe}.frontier-labels{suffix_for(label_src)}")
            archived_labels = str(archived)
            evidence.append(f"frontier annotations archived: {archived}")
        else:
            blockers.append(f"frontier_annotations does not exist: {label_src}")

    archived_matrix = ""
    if args.matrix_summary:
        matrix_src = resolve_after_run(args.matrix_summary, work_dir)
        if matrix_src.exists():
            archived = copy_artifact(matrix_src, matrix_dir, f"{safe}.matrix-summary.json")
            archived_matrix = str(archived)
            evidence.append(f"matrix summary archived: {archived}")
        else:
            blockers.append(f"matrix_summary does not exist: {matrix_src}")

    row: dict[str, Any] | None = None
    if not blockers:
        row = {
            "instance_id": args.instance_id,
            "raw_agent_output": rel(archived_raw, out_dir),
            "repo_dir": rel(str(repo_dir), out_dir),
            "full_harness_result": rel(archived_endpoint, out_dir),
        }
        if args.raw_format:
            row["raw_format"] = args.raw_format
        if archived_labels:
            row["frontier_annotations"] = rel(archived_labels, out_dir)
        if archived_matrix:
            row["matrix_summary"] = rel(archived_matrix, out_dir)

    manifest_path = out_dir / "real-agent-manifest.json"
    report_path = out_dir / "capture-report.json"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(args, row=row, manifest_path=manifest_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    profile_result = (run or {}).get("system_profile") if isinstance(run, dict) else disabled_profile()
    if not isinstance(profile_result, dict):
        profile_result = disabled_profile()
    status = "pass" if row is not None and not warnings else "partial" if row is not None else "fail"
    report = {
        "schema": SCHEMA,
        "status": status,
        "instance_id": args.instance_id,
        "out_dir": str(out_dir),
        "manifest": str(manifest_path),
        "run": run,
        "artifacts": {
            "raw_agent_output": archived_raw,
            "repo_dir": str(repo_dir),
            "full_harness_result": archived_endpoint,
            "frontier_annotations": archived_labels,
            "matrix_summary": archived_matrix,
            "stdout": str(stdout_path) if stdout_path.exists() else "",
            "stderr": str(stderr_path) if stderr_path.exists() else "",
            "system_profile_summary": profile_result.get("summary", ""),
            "system_profile_samples": profile_result.get("samples_log", ""),
            "system_profile_markdown": profile_result.get("markdown", ""),
        },
        "evidence": evidence,
        "warnings": warnings,
        "blockers": blockers,
        "next_commands": {
            "preflight": [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parent / "validate_bundle_collection_manifest.py"),
                "--manifest",
                str(manifest_path),
                "--out-dir",
                str(out_dir),
                "--output",
                str(out_dir / "manifest-preflight.json"),
            ],
            "prepare": [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parent / "prepare_bundle_collection.py"),
                "--manifest",
                str(manifest_path),
                "--out-dir",
                str(out_dir),
                "--preflight-output",
                str(out_dir / "manifest-preflight.json"),
            ],
        },
        "notes": [
            "capture pass/partial is only artifact readiness; bundle audit decides paper evidence",
            "captured endpoint metadata is weaker than a framework-produced full-harness endpoint",
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "manifest": str(manifest_path), "report": str(report_path)}, sort_keys=True))
    return 0 if row is not None else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

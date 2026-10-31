#!/usr/bin/env python3
"""Replay a recorded code-agent command trace as a staged sandbox workload.

This workload is the bridge from real SWE-agent/OpenHands/Codex-style
trajectories to FlowCausal's repeatable scheduler matrix.  A full agent run can
be noisy because model latency, API jitter, and planner randomness change from
run to run.  This wrapper takes the sandbox-visible part of that run---the
commands executed in the repository runtime---and replays it with the same JSON
stage contract consumed by ``run_repo_ci_matrix.py``:

    agent_stage_begin/end, flowcausal_metric:critical_sec, stage-state bridge

The input trace can be a JSON object with a ``commands`` list, a JSON list of
command records, or JSONL with one command record per line.  Each command record
should contain at least ``cmd`` or ``command``.  Optional fields include
``stage``, ``cwd``, ``turn``, ``allow_failure``, ``critical``, and
``frontier_label``.  The last command marked ``critical`` defines the
user-visible critical path; otherwise the wrapper emits ``critical_sec`` after
the last command in ``--critical-after-stage`` if present, or after the last
non-background command.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from repo_stage_workflow import (
    MAIN_STAGE_ORDER,
    emit,
    emit_metric,
    parse_env,
    prepare_work_dir,
    write_stage_state,
)


ALLOWED_STAGES = (*MAIN_STAGE_ORDER, "PACKAGE_BACKGROUND", "SHELL")
STAGE_ALIASES = {
    "SEARCH": "INSPECT",
    "READ": "INSPECT",
    "EXPLORE": "INSPECT",
    "LOCALIZE": "INSPECT",
    "PATCH": "EDIT",
    "APPLY_PATCH": "EDIT",
    "MODIFY": "EDIT",
    "COMPILE": "BUILD",
    "CHECK": "TEST",
    "VERIFY": "TEST",
    "VALIDATE": "TEST",
    "RETRY": "RETRY_PROBE",
    "RETRY-PROBE": "RETRY_PROBE",
    "PACKAGE": "PACKAGE_BACKGROUND",
    "PACKAGE_BG": "PACKAGE_BACKGROUND",
    "BACKGROUND": "PACKAGE_BACKGROUND",
}


@dataclass(frozen=True)
class TraceCommand:
    index: int
    command: str
    stage: str
    cwd: str
    turn: int
    allow_failure: bool
    critical: bool
    source_id: str
    raw: dict[str, Any]


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_stage(value: Any, command: str = "") -> str:
    text = str(value or "").strip().upper().replace(" ", "_")
    text = STAGE_ALIASES.get(text, text)
    if text in ALLOWED_STAGES:
        return text
    return infer_stage_from_command(command)


def infer_stage_from_command(command: str) -> str:
    text = command.lower()
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if "complete_task_and_submit_final_output" in text:
        return "PACKAGE_BACKGROUND"
    if re.search(r"\bcat\s+patch\.txt\b", text):
        return "PACKAGE_BACKGROUND"
    if re.search(r"\bgit\s+diff\b.*>\s*patch\.txt\b", text):
        return "PACKAGE_BACKGROUND"
    if "pytest" in text or "tox" in text or "nox" in text or "unittest" in text:
        return "TEST"
    if "make test" in text or "cargo test" in text or "npm test" in text:
        return "TEST"
    if re.search(r"\bpython(?:3)?\s+-m\s+django\s+test\b", text):
        return "TEST"
    if re.search(r"\bpython(?:3)?\b", text) and re.search(
        r"\bassert\b|\btest\b|\bcheck\b|\bverify\b|\breproduce\b|failed|success|bug|regression",
        text,
    ):
        return "TEST"
    if re.search(r"\bsed\s+-i\b", text):
        return "EDIT"
    if re.search(r"\bperl\s+-pi\b", text):
        return "EDIT"
    if "git apply" in text or "apply_patch" in text:
        return "EDIT"
    if re.search(r"\bpatch\b\s*(?:<|-p)", text):
        return "EDIT"
    if re.search(r"\bpython(?:3)?\b", text) and re.search(
        r"write_text|open\([^)]*['\"]w|replace\(|\.write\(",
        text,
    ):
        return "EDIT"
    if first in {
        "find",
        "grep",
        "rg",
        "ag",
        "ls",
        "cat",
        "sed",
        "awk",
        "wc",
        "open",
        "goto",
        "scroll_down",
        "scroll_up",
        "search_file",
        "search_dir",
    }:
        return "INSPECT"
    if re.search(r"(^|[;&|()\s])(?:find|grep|rg|ag|ls|cat|awk|wc)(?=$|[;&|()\s])", text):
        return "INSPECT"
    if re.search(r"(^|[;&|()\s])sed\s+-n\b", text):
        return "INSPECT"
    if "git grep" in text or "ripgrep" in text or re.search(r"\bgit\s+diff\b", text):
        return "INSPECT"
    if first in {"patch", "edit", "insert", "create", "write"} or "git apply" in text or "apply_patch" in text:
        return "EDIT"
    if "make" in text or "cmake" in text or "ninja" in text or "cargo build" in text:
        return "BUILD"
    if "py_compile" in text or "compileall" in text:
        return "BUILD"
    if first in {"cc", "gcc", "g++", "clang", "clang++", "ld", "ar"}:
        return "BUILD"
    if "tar " in text or "zip " in text or "gzip " in text:
        return "PACKAGE_BACKGROUND"
    return "SHELL"


def command_from_record(record: dict[str, Any]) -> str:
    for key in ("cmd", "command", "bash", "shell"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    action = record.get("action")
    if isinstance(action, dict):
        return command_from_record(action)
    args = record.get("args")
    if isinstance(args, dict):
        return command_from_record(args)
    return ""


def extract_command_records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        if isinstance(data.get("commands"), list):
            return [item for item in data["commands"] if isinstance(item, dict)]
        if isinstance(data.get("trajectory"), list):
            return [item for item in data["trajectory"] if isinstance(item, dict)]
        if isinstance(data.get("history"), list):
            return [item for item in data["history"] if isinstance(item, dict)]
        return [data]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def load_trace_records(path: pathlib.Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if path.suffix.lower() == ".jsonl":
        records: list[dict[str, Any]] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
            if isinstance(item, dict):
                records.extend(extract_command_records(item))
        return {"schema": "jsonl", "source": path.name}, records

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON trace: {path}") from exc

    meta = data if isinstance(data, dict) else {"schema": "json-list", "source": path.name}
    return dict(meta), extract_command_records(data)


def normalize_trace_commands(records: list[dict[str, Any]], *, allow_failure_stages: set[str]) -> list[TraceCommand]:
    commands: list[TraceCommand] = []
    for raw_index, record in enumerate(records):
        command = command_from_record(record)
        if not command:
            continue
        stage = normalize_stage(record.get("stage"), command)
        turn_value = record.get("turn", record.get("iteration", 0))
        try:
            turn = int(turn_value)
        except (TypeError, ValueError):
            turn = 0
        allow_failure = as_bool(record.get("allow_failure"), default=stage in allow_failure_stages)
        critical = as_bool(record.get("critical"), default=False) or as_bool(
            record.get("critical_after"), default=False
        )
        source_id = str(record.get("id") or record.get("event_id") or raw_index)
        commands.append(
            TraceCommand(
                index=len(commands),
                command=command,
                stage=stage,
                cwd=str(record.get("cwd") or "."),
                turn=turn,
                allow_failure=allow_failure,
                critical=critical,
                source_id=source_id,
                raw=record,
            )
        )
    if not commands:
        raise SystemExit("trace contains no command records with cmd/command/bash/shell")
    return commands


def choose_critical_index(commands: list[TraceCommand], requested_stage: str) -> int:
    critical = [cmd.index for cmd in commands if cmd.critical]
    if critical:
        return critical[-1]
    requested_stage = normalize_stage(requested_stage)
    matching = [cmd.index for cmd in commands if cmd.stage == requested_stage]
    if matching:
        return matching[-1]
    foreground = [cmd.index for cmd in commands if cmd.stage != "PACKAGE_BACKGROUND"]
    return foreground[-1] if foreground else commands[-1].index


def resolve_command_cwd(work_dir: pathlib.Path, command_cwd: str) -> pathlib.Path:
    candidate = pathlib.Path(command_cwd)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = (work_dir / candidate).resolve()
    try:
        resolved.relative_to(work_dir.resolve())
    except ValueError as exc:
        raise SystemExit(f"trace command cwd escapes work_dir: {command_cwd}") from exc
    if not resolved.is_dir():
        raise SystemExit(f"trace command cwd is not a directory: {resolved}")
    return resolved


def create_work_dir_aliases(work_dir: pathlib.Path, aliases: list[str]) -> list[str]:
    """Create optional absolute symlinks that point at the replay work tree.

    Some SWE-rebench Docker images carry editable Python installs that point to
    the original in-container checkout path.  Harness replay intentionally runs
    from a copied work tree under ``--work-dir``; this helper lets a manifest
    declare explicit compatibility aliases such as ``/sdf-xarray -> /out/work``
    without changing the recorded command trace.
    """

    created: list[str] = []
    for alias_text in aliases:
        alias_text = alias_text.strip()
        if not alias_text:
            continue
        alias = pathlib.Path(alias_text)
        if not alias.is_absolute():
            raise SystemExit(f"--work-dir-alias must be absolute: {alias_text}")
        if alias == work_dir:
            continue
        if alias.exists() or alias.is_symlink():
            try:
                if alias.resolve() == work_dir.resolve():
                    continue
            except FileNotFoundError:
                pass
            raise SystemExit(f"--work-dir-alias already exists and does not point to work_dir: {alias}")
        alias.parent.mkdir(parents=True, exist_ok=True)
        alias.symlink_to(work_dir, target_is_directory=True)
        created.append(str(alias))
    return created


def emit_frontier_label(
    command: TraceCommand,
    *,
    pid: int,
    cwd: pathlib.Path,
    frontier_label_file: pathlib.Path | None,
) -> None:
    label = command.raw.get("frontier_label")
    if label is None:
        return
    record = {
        "type": "frontier_label",
        "time_ns": time.time_ns(),
        "command_index": command.index,
        "source_id": command.source_id,
        "stage": command.stage,
        "turn": command.turn,
        "pid": pid,
        "cwd": str(cwd),
        "label": label,
    }
    emit("frontier_label", **{key: value for key, value in record.items() if key != "type"})
    if frontier_label_file is not None:
        frontier_label_file.parent.mkdir(parents=True, exist_ok=True)
        with frontier_label_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()


def run_trace_command(
    command: TraceCommand,
    *,
    work_dir: pathlib.Path,
    stage_out_dir: pathlib.Path,
    stage_state_file: pathlib.Path | None,
    frontier_label_file: pathlib.Path | None,
    trace_source: str,
    instance_id: str,
    extra_env: dict[str, str],
) -> tuple[int, float]:
    cwd = resolve_command_cwd(work_dir, command.cwd)
    env = os.environ.copy()
    env.update(extra_env)
    replay_python = (
        extra_env.get("HRB_REPLAY_PYTHON")
        or extra_env.get("FLOWCAUSAL_REPLAY_PYTHON")
        or env.get("HRB_REPLAY_PYTHON")
        or env.get("FLOWCAUSAL_REPLAY_PYTHON")
    )
    env.update(
        {
            "FLOWCAUSAL_STAGE": command.stage,
            "FLOWCAUSAL_TURN": str(command.turn),
            "FLOWCAUSAL_WORK_DIR": str(work_dir),
            "FLOWCAUSAL_STAGE_OUT": str(stage_out_dir),
            "FLOWCAUSAL_TRACE_SOURCE": trace_source,
            "FLOWCAUSAL_TRACE_INSTANCE_ID": instance_id,
            "FLOWCAUSAL_TRACE_COMMAND_INDEX": str(command.index),
            "FLOWCAUSAL_TRACE_SOURCE_ID": command.source_id,
            "HRB_STAGE": command.stage,
            "HRB_TURN": str(command.turn),
            "HRB_WORK_DIR": str(work_dir),
            "HRB_STAGE_OUT": str(stage_out_dir),
            "HRB_TRACE_SOURCE": trace_source,
            "HRB_TRACE_INSTANCE_ID": instance_id,
            "HRB_TRACE_COMMAND_INDEX": str(command.index),
            "HRB_TRACE_SOURCE_ID": command.source_id,
        }
    )
    if replay_python:
        env.setdefault("HRB_REPLAY_PYTHON", replay_python)
        env.setdefault("FLOWCAUSAL_REPLAY_PYTHON", replay_python)
    if stage_state_file is not None:
        env["FLOWCAUSAL_STAGE_STATE_FILE"] = str(stage_state_file)
        env["HRB_STAGE_STATE_FILE"] = str(stage_state_file)

    start = time.monotonic()
    # Use a non-login shell so replay-provided environment entries remain
    # authoritative.  Several SWE-rebench containers reset PATH from profile
    # files in login shells, which can make a recorded ``python``/``pip`` pair
    # run against different interpreter environments.
    proc = subprocess.Popen(["bash", "-c", command.command], cwd=str(cwd), env=env)
    write_stage_state(
        stage_state_file,
        workload="agent_trace_workflow",
        phase="begin",
        stage=command.stage,
        turn=command.turn,
        pid=proc.pid,
        cmd=command.command,
        cwd=str(cwd),
        command_index=command.index,
        source_id=command.source_id,
    )
    emit(
        "agent_stage_begin",
        stage=command.stage,
        turn=command.turn,
        pid=proc.pid,
        cmd=command.command,
        cwd=str(cwd),
        command_index=command.index,
        source_id=command.source_id,
        allow_failure=command.allow_failure,
    )
    emit_frontier_label(command, pid=proc.pid, cwd=cwd, frontier_label_file=frontier_label_file)
    returncode = proc.wait()
    elapsed = time.monotonic() - start
    write_stage_state(
        stage_state_file,
        workload="agent_trace_workflow",
        phase="end",
        stage=command.stage,
        turn=command.turn,
        pid=proc.pid,
        returncode=returncode,
        elapsed_sec=elapsed,
        cwd=str(cwd),
        command_index=command.index,
        source_id=command.source_id,
    )
    emit(
        "agent_stage_end",
        stage=command.stage,
        turn=command.turn,
        pid=proc.pid,
        returncode=returncode,
        elapsed_sec=elapsed,
        cwd=str(cwd),
        command_index=command.index,
        source_id=command.source_id,
        allow_failure=command.allow_failure,
    )
    if returncode != 0 and not command.allow_failure:
        raise subprocess.CalledProcessError(returncode, command.command)
    return returncode, elapsed


def wait_for_start_gate(path: pathlib.Path, timeout_sec: float) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.02)
    raise SystemExit(f"timed out waiting for start gate: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay recorded code-agent commands with FlowCausal stage markers",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--trace-file", required=True, help="JSON/JSONL trace with command records")
    parser.add_argument("--repo-dir", default=".", help="source repository directory")
    parser.add_argument("--work-dir", help="destination work directory when copying the repository")
    parser.add_argument("--copy-root", help="directory tree to copy before running commands")
    parser.add_argument("--overwrite", action="store_true", help="replace an existing --work-dir")
    parser.add_argument("--keep-work-dir", action="store_true", help="keep auto-created temporary directories")
    parser.add_argument("--in-place", action="store_true", help="run commands directly in --repo-dir")
    parser.add_argument("--copy-exclude", action="append", default=[], help="additional copy ignore pattern")
    parser.add_argument(
        "--copy-keep-git",
        action="store_true",
        help="preserve .git metadata when copying the repository for traces that execute git commands",
    )
    parser.add_argument("--stage-out-dir", help="directory exposed to commands as FLOWCAUSAL_STAGE_OUT")
    parser.add_argument("--stage-state-file", help="JSON stage bridge updated at command begin/end")
    parser.add_argument(
        "--start-gate-file",
        help="optional file that must exist before repository copy and stage replay begin",
    )
    parser.add_argument(
        "--start-gate-timeout-sec",
        type=float,
        default=120.0,
        help="how long to wait for --start-gate-file before failing",
    )
    parser.add_argument(
        "--frontier-label-file",
        help="optional JSONL file that mirrors frontier_label records for oracle/manual hint drivers",
    )
    parser.add_argument("--pre-sleep-ms", type=int, default=200, help="delay before first command")
    parser.add_argument("--post-sleep-ms", type=int, default=200, help="delay after final command")
    parser.add_argument("--env", action="append", default=[], help="extra KEY=VALUE environment entry")
    parser.add_argument(
        "--work-dir-alias",
        action="append",
        default=[],
        help="absolute symlink path that should point at --work-dir, for container editable-install compatibility",
    )
    parser.add_argument(
        "--critical-after-stage",
        default="TEST",
        choices=[*ALLOWED_STAGES],
        help="fallback stage after which critical_sec is emitted",
    )
    parser.add_argument(
        "--allow-failure-stage",
        action="append",
        default=["RETRY_PROBE"],
        choices=[*ALLOWED_STAGES],
        help="stage whose nonzero exit should not fail the replay",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trace_file = pathlib.Path(args.trace_file).resolve()
    trace_meta, raw_records = load_trace_records(trace_file)
    allow_failure_stages = {normalize_stage(stage) for stage in args.allow_failure_stage}
    commands = normalize_trace_commands(raw_records, allow_failure_stages=allow_failure_stages)
    critical_index = choose_critical_index(commands, args.critical_after_stage)
    extra_env = parse_env(args.env)

    if args.start_gate_file:
        wait_for_start_gate(pathlib.Path(args.start_gate_file).resolve(), args.start_gate_timeout_sec)

    work_dir, cleanup_path, copy_root, copy_dst = prepare_work_dir(args)
    work_dir_aliases = create_work_dir_aliases(work_dir, list(args.work_dir_alias or []))
    stage_out_cleanup: pathlib.Path | None = None
    if args.stage_out_dir:
        stage_out_dir = pathlib.Path(args.stage_out_dir).resolve()
    elif os.environ.get("FLOWCAUSAL_TRIAL_DIR"):
        stage_out_dir = pathlib.Path(os.environ["FLOWCAUSAL_TRIAL_DIR"]).resolve() / "agent_trace_outputs"
    else:
        stage_out_dir = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-agent-trace-out-"))
        stage_out_cleanup = stage_out_dir
    stage_out_dir.mkdir(parents=True, exist_ok=True)

    state_file_text = args.stage_state_file or os.environ.get("FLOWCAUSAL_STAGE_STATE_FILE")
    stage_state_file = pathlib.Path(state_file_text).resolve() if state_file_text else None
    frontier_label_text = args.frontier_label_file or os.environ.get("FLOWCAUSAL_FRONTIER_LABEL_FILE")
    frontier_label_file = pathlib.Path(frontier_label_text).resolve() if frontier_label_text else None
    trace_source = str(trace_meta.get("source") or trace_meta.get("agent") or trace_file.name)
    instance_id = str(trace_meta.get("instance_id") or trace_meta.get("task_id") or "")

    start = time.monotonic()
    timings: dict[str, float] = {}
    returncode = 0

    emit(
        "workload_begin",
        workload="agent_trace_workflow",
        trace_file=str(trace_file),
        trace_source=trace_source,
        instance_id=instance_id,
        repo_dir=str(pathlib.Path(args.repo_dir).resolve()),
        work_dir=str(work_dir),
        work_dir_aliases=work_dir_aliases,
        stage_out_dir=str(stage_out_dir),
        stage_state_file=str(stage_state_file) if stage_state_file is not None else "",
        copy_root=str(copy_root),
        copy_dst=str(copy_dst),
        in_place=bool(args.in_place),
        command_count=len(commands),
        stages=[cmd.stage for cmd in commands],
        critical_command_index=critical_index,
        critical_after_stage=normalize_stage(args.critical_after_stage),
    )

    try:
        if args.pre_sleep_ms > 0:
            time.sleep(args.pre_sleep_ms / 1000.0)

        critical_emitted = False
        for command in commands:
            _, elapsed = run_trace_command(
                command,
                work_dir=work_dir,
                stage_out_dir=stage_out_dir,
                stage_state_file=stage_state_file,
                frontier_label_file=frontier_label_file,
                trace_source=trace_source,
                instance_id=instance_id,
                extra_env=extra_env,
            )
            timings[f"command_{command.index}_{command.stage.lower()}"] = elapsed
            if command.index == critical_index:
                critical_sec = time.monotonic() - start
                emit_metric(
                    "critical_sec",
                    critical_sec,
                    meaning=f"trace_command_{command.index}_{command.stage.lower()}_complete",
                )
                emit(
                    "flowcausal_critical_path",
                    elapsed_sec=critical_sec,
                    stage=command.stage,
                    command_index=command.index,
                )
                critical_emitted = True

        if not critical_emitted:
            critical_sec = time.monotonic() - start
            emit_metric("critical_sec", critical_sec, meaning="all_trace_commands_complete")
            emit("flowcausal_critical_path", elapsed_sec=critical_sec, stage=commands[-1].stage)

        if args.post_sleep_ms > 0:
            time.sleep(args.post_sleep_ms / 1000.0)

    except subprocess.CalledProcessError as exc:
        returncode = int(exc.returncode or 1)
        emit("workload_error", workload="agent_trace_workflow", returncode=returncode, cmd=str(exc.cmd))
    except SystemExit as exc:
        returncode = exc.code if isinstance(exc.code, int) else 1
        emit(
            "workload_error",
            workload="agent_trace_workflow",
            returncode=returncode,
            error=str(exc.code),
        )
    finally:
        write_stage_state(
            stage_state_file,
            workload="agent_trace_workflow",
            phase="workload_end",
            stage="SHELL",
            returncode=returncode,
        )
        emit("workload_end", workload="agent_trace_workflow", returncode=returncode, timings=timings)
        if not args.keep_work_dir:
            emit(
                "cleanup_begin",
                workload="agent_trace_workflow",
                stage="SHELL",
                returncode=returncode,
            )
            write_stage_state(
                stage_state_file,
                workload="agent_trace_workflow",
                phase="cleanup_begin",
                stage="SHELL",
                returncode=returncode,
            )
            if cleanup_path is not None:
                shutil.rmtree(cleanup_path, ignore_errors=True)
            if stage_out_cleanup is not None:
                shutil.rmtree(stage_out_cleanup, ignore_errors=True)
            for alias_text in work_dir_aliases:
                try:
                    pathlib.Path(alias_text).unlink()
                except FileNotFoundError:
                    continue
            emit(
                "cleanup_end",
                workload="agent_trace_workflow",
                stage="SHELL",
                returncode=returncode,
            )
            write_stage_state(
                stage_state_file,
                workload="agent_trace_workflow",
                phase="cleanup_end",
                stage="SHELL",
                returncode=returncode,
            )

    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Wrap a real repository workflow with FlowCausal stage markers.

This workload is the bridge between an arbitrary ``bash -lc`` repo command and
the evidence format consumed by ``run_repo_ci_matrix.py``.  It does not infer a
frontier and it does not attach a scheduler.  Instead, it executes ordinary
repository commands as subprocesses while emitting JSON markers for the
user-visible stages that a code-agent or CI turn would expose:

    INSPECT -> EDIT -> BUILD -> RETRY_PROBE -> TEST -> PACKAGE_BACKGROUND

Only stages with a command are executed.  By default the repository is copied to
a temporary working directory before commands run, so experiments do not dirty
the source tree.  Use ``--in-place`` only for deliberately managed scratch
repositories.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any


MAIN_STAGE_ORDER = ("INSPECT", "EDIT", "BUILD", "RETRY_PROBE", "TEST")
DEFAULT_COPY_EXCLUDES = (
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    ".venv-*",
    "build",
    "cmake-build-*",
    "target",
    "node_modules",
    "*.o",
    "*.a",
    "*.so",
    "*.d",
    "*.pyc",
)
BUILD_ARTIFACT_EXCLUDES = frozenset(
    {
        "build",
        "cmake-build-*",
        "target",
        "*.o",
        "*.a",
        "*.so",
        "*.d",
    }
)


def emit(event: str, **fields: Any) -> None:
    print(json.dumps({"type": event, "time_ns": time.time_ns(), **fields}, sort_keys=True), flush=True)


def emit_metric(name: str, value: float, **fields: Any) -> None:
    emit("flowcausal_metric", name=name, value=value, **fields)


def mirror_jsonl(path: pathlib.Path | None, record: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()


def write_stage_state(path: pathlib.Path | None, **fields: Any) -> None:
    """Atomically publish the currently active workflow stage.

    ``flowctl --stage-state-file`` reads this file while the workload runs.  A
    missing file is fine, but a partially written file would create noisy stage
    fallback, so writes go through a temporary sibling and ``replace``.
    """

    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"type": "flowcausal_stage_state", "time_ns": time.time_ns(), **fields}
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def proc_task_children(pid: int) -> set[int]:
    """Return currently visible Linux child PIDs for a process.

    ``/proc/<pid>/task/<tid>/children`` is intentionally a best-effort sampling
    interface.  That is exactly what the repo-stage oracle needs here: it is not
    trying to infer a frontier from the kernel, only to mirror wrapper-known
    foreground workers quickly enough for the oracle driver to attach hints.
    """

    children: set[int] = set()
    task_dir = pathlib.Path("/proc") / str(pid) / "task"
    try:
        task_entries = list(task_dir.iterdir())
    except OSError:
        return children
    for task in task_entries:
        try:
            text = (task / "children").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in text.split():
            try:
                child = int(item)
            except ValueError:
                continue
            if child > 0:
                children.add(child)
    return children


def proc_descendants_with_parent(root_pid: int) -> dict[int, int]:
    """Best-effort recursive descendant map, keyed by child PID."""

    descendants: dict[int, int] = {}
    seen = {root_pid}
    stack = [root_pid]
    while stack:
        parent = stack.pop()
        for child in sorted(proc_task_children(parent)):
            if child in seen:
                continue
            seen.add(child)
            descendants[child] = parent
            stack.append(child)
    return descendants


def emit_frontier_label_record(
    *,
    frontier_label_file: pathlib.Path | None,
    stage: str,
    turn: int,
    pid: int,
    cwd: pathlib.Path,
    command_index: int,
    source_id: str,
    role: str,
    reason: str,
    root_pid: int | None = None,
    parent_pid: int | None = None,
) -> None:
    label = {
        "source": "oracle",
        "role": role,
        "reason": reason,
        "confidence": 100,
        "stage_label": f"repo-stage-{turn}-{stage.lower()}",
        "cohort_label": "repo-stage",
        "frontier_action": "advance",
    }
    record = {
        "type": "frontier_label",
        "time_ns": time.time_ns(),
        "command_index": command_index,
        "source_id": source_id,
        "stage": stage,
        "turn": turn,
        "pid": pid,
        "cwd": str(cwd),
        "label": label,
    }
    if root_pid is not None:
        record["root_pid"] = root_pid
    if parent_pid is not None:
        record["parent_pid"] = parent_pid
    emit("frontier_label", **{key: value for key, value in record.items() if key != "type"})
    mirror_jsonl(frontier_label_file, record)


def follow_and_emit_descendant_frontier_labels(
    *,
    proc: subprocess.Popen[Any],
    frontier_label_file: pathlib.Path | None,
    stage: str,
    turn: int,
    cwd: pathlib.Path,
    command_index: int,
    source_id: str,
    follow_ms: int,
    interval_ms: int,
    emitted_pids: set[int],
) -> None:
    if follow_ms <= 0:
        return
    interval_sec = max(1, interval_ms) / 1000.0
    deadline = time.monotonic() + follow_ms / 1000.0
    while True:
        for pid, parent_pid in sorted(proc_descendants_with_parent(proc.pid).items()):
            if pid in emitted_pids:
                continue
            emitted_pids.add(pid)
            emit_frontier_label_record(
                frontier_label_file=frontier_label_file,
                stage=stage,
                turn=turn,
                pid=pid,
                cwd=cwd,
                command_index=command_index,
                source_id=f"{source_id}:desc:{pid}",
                role="foreground-worker",
                reason="repo-stage-descendant-worker",
                root_pid=proc.pid,
                parent_pid=parent_pid,
            )
        if proc.poll() is not None or time.monotonic() >= deadline:
            break
        time.sleep(interval_sec)


def parse_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--env must be KEY=VALUE, got: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--env has an empty key: {item}")
        env[key] = value
    return env


def load_stage_spec(path: pathlib.Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"failed to read --stage-spec-file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON in --stage-spec-file: {path}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"--stage-spec-file must contain a JSON object: {path}")
    spec: dict[str, str] = {}
    for stage, value in data.items():
        if not isinstance(value, str):
            continue
        spec[str(stage).upper()] = value
    return spec


def stage_command_map(args: argparse.Namespace) -> dict[str, str]:
    commands = {
        "INSPECT": args.inspect_cmd,
        "EDIT": args.edit_cmd,
        "BUILD": args.build_cmd,
        "RETRY_PROBE": args.retry_cmd,
        "TEST": args.test_cmd,
    }
    if args.stage_spec_file:
        commands.update(load_stage_spec(pathlib.Path(args.stage_spec_file).resolve()))
    return commands


def selected_main_stages(commands: dict[str, str]) -> list[str]:
    return [stage for stage in MAIN_STAGE_ORDER if commands.get(stage)]


def resolve_critical_stage(requested: str, stages: list[str]) -> str:
    requested = requested.upper()
    if requested in stages:
        return requested
    if not stages:
        raise SystemExit("no main stage command was provided")
    return stages[-1]


def copy_repo(
    src: pathlib.Path,
    dst: pathlib.Path,
    excludes: list[str],
    *,
    keep_build_artifacts: bool = False,
    keep_git: bool = False,
) -> None:
    default_excludes = list(DEFAULT_COPY_EXCLUDES)
    if keep_build_artifacts:
        default_excludes = [pattern for pattern in default_excludes if pattern not in BUILD_ARTIFACT_EXCLUDES]
    if keep_git:
        default_excludes = [pattern for pattern in default_excludes if pattern != ".git"]
    ignore = make_copy_ignore(src, default_excludes + excludes)
    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


def make_copy_ignore(src: pathlib.Path, patterns: list[str]):
    """Build a copytree ignore function with basename and relative-path globs.

    ``shutil.ignore_patterns`` only matches basenames.  That is fine for
    patterns such as ``__pycache__`` but surprising for experiment controls like
    ``--copy-exclude experiments/results``.  Support both forms so replay
    provenance can express the intended source-tree slice directly.
    """

    src = src.resolve()
    basename_patterns = [pattern for pattern in patterns if "/" not in pattern]
    path_patterns = [pattern.strip("/") for pattern in patterns if "/" in pattern]
    basename_ignore = shutil.ignore_patterns(*basename_patterns)

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = set(basename_ignore(directory, names))
        if not path_patterns:
            return ignored
        try:
            rel_dir = pathlib.Path(directory).resolve().relative_to(src)
        except ValueError:
            rel_dir = pathlib.Path()
        rel_dir_text = "" if str(rel_dir) == "." else rel_dir.as_posix()
        for name in names:
            rel = f"{rel_dir_text}/{name}" if rel_dir_text else name
            for pattern in path_patterns:
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(f"{rel}/", f"{pattern.rstrip('/')}/"):
                    ignored.add(name)
                    break
        return ignored

    return ignore


def auto_copy_excludes(copy_root: pathlib.Path, copy_dst: pathlib.Path, excludes: list[str]) -> list[str]:
    """Add safety excludes for work dirs nested inside the copied tree."""

    out = list(excludes)
    try:
        rel_dst = copy_dst.resolve().relative_to(copy_root.resolve())
    except ValueError:
        return out
    rel_text = rel_dst.as_posix()
    if rel_text:
        out.append(rel_text)
    if len(rel_dst.parts) >= 2 and rel_dst.parts[0] == "experiments" and rel_dst.parts[1] == "results":
        out.append("experiments/results")
    return out


def prepare_work_dir(args: argparse.Namespace) -> tuple[pathlib.Path, pathlib.Path | None, pathlib.Path, pathlib.Path]:
    repo_dir = pathlib.Path(args.repo_dir).resolve()
    if not repo_dir.is_dir():
        raise SystemExit(f"--repo-dir is not a directory: {repo_dir}")

    if args.in_place:
        if args.work_dir:
            raise SystemExit("--work-dir cannot be combined with --in-place")
        if args.copy_root:
            raise SystemExit("--copy-root cannot be combined with --in-place")
        return repo_dir, None, repo_dir, repo_dir

    copy_root = pathlib.Path(args.copy_root).resolve() if args.copy_root else repo_dir
    if not copy_root.is_dir():
        raise SystemExit(f"--copy-root is not a directory: {copy_root}")
    try:
        repo_relative = repo_dir.relative_to(copy_root)
    except ValueError:
        raise SystemExit(f"--repo-dir must be inside --copy-root: repo={repo_dir} copy_root={copy_root}") from None

    auto_temp = args.work_dir is None
    if auto_temp:
        parent = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-repo-stage-"))
        copy_dst = parent / copy_root.name
        cleanup_path: pathlib.Path | None = parent
    else:
        copy_dst = pathlib.Path(args.work_dir).resolve()
        if copy_dst.exists():
            if not args.overwrite:
                raise SystemExit(f"work directory exists; pass --overwrite: {copy_dst}")
            shutil.rmtree(copy_dst)
        copy_dst.parent.mkdir(parents=True, exist_ok=True)
        cleanup_path = None

    copy_repo(
        copy_root,
        copy_dst,
        auto_copy_excludes(copy_root, copy_dst, args.copy_exclude),
        keep_build_artifacts=bool(getattr(args, "copy_keep_build_artifacts", False)),
        keep_git=bool(getattr(args, "copy_keep_git", False)),
    )
    command_cwd = copy_dst / repo_relative
    return command_cwd, cleanup_path, copy_root, copy_dst


def run_stage(
    stage: str,
    command: str,
    *,
    cwd: pathlib.Path,
    stage_out_dir: pathlib.Path,
    stage_state_file: pathlib.Path | None,
    frontier_label_file: pathlib.Path | None,
    turn: int,
    command_index: int,
    source_id: str,
    emit_frontier_labels: bool,
    emit_descendant_frontier_labels: bool,
    frontier_label_follow_ms: int,
    frontier_label_follow_interval_ms: int,
    allow_failure: bool,
    extra_env: dict[str, str],
) -> tuple[int, float]:
    env = os.environ.copy()
    env.update(extra_env)
    env.update(
        {
            "FLOWCAUSAL_STAGE": stage,
            "FLOWCAUSAL_TURN": str(turn),
            "FLOWCAUSAL_WORK_DIR": str(cwd),
            "FLOWCAUSAL_STAGE_OUT": str(stage_out_dir),
            "FLOWCAUSAL_REPO_STAGE_COMMAND_INDEX": str(command_index),
            "FLOWCAUSAL_REPO_STAGE_SOURCE_ID": source_id,
        }
    )
    if stage_state_file is not None:
        env["FLOWCAUSAL_STAGE_STATE_FILE"] = str(stage_state_file)

    start = time.monotonic()
    proc = subprocess.Popen(["bash", "-lc", command], cwd=str(cwd), env=env)
    write_stage_state(
        stage_state_file,
        workload="repo_stage_workflow",
        phase="begin",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        cmd=command,
        cwd=str(cwd),
        command_index=command_index,
        source_id=source_id,
    )
    emit(
        "agent_stage_begin",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        cmd=command,
        cwd=str(cwd),
        command_index=command_index,
        source_id=source_id,
    )
    if emit_frontier_labels:
        emitted_pids = {proc.pid}
        emit_frontier_label_record(
            frontier_label_file=frontier_label_file,
            stage=stage,
            turn=turn,
            pid=proc.pid,
            cwd=cwd,
            command_index=command_index,
            source_id=source_id,
            role="foreground-stage",
            reason="repo-stage-active-command",
        )
        if emit_descendant_frontier_labels:
            follow_and_emit_descendant_frontier_labels(
                proc=proc,
                frontier_label_file=frontier_label_file,
                stage=stage,
                turn=turn,
                cwd=cwd,
                command_index=command_index,
                source_id=source_id,
                follow_ms=frontier_label_follow_ms,
                interval_ms=frontier_label_follow_interval_ms,
                emitted_pids=emitted_pids,
            )
    returncode = proc.wait()
    elapsed = time.monotonic() - start
    write_stage_state(
        stage_state_file,
        workload="repo_stage_workflow",
        phase="end",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        returncode=returncode,
        elapsed_sec=elapsed,
        cwd=str(cwd),
        command_index=command_index,
        source_id=source_id,
    )
    emit(
        "agent_stage_end",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        returncode=returncode,
        elapsed_sec=elapsed,
        cmd=command,
        cwd=str(cwd),
        command_index=command_index,
        source_id=source_id,
    )
    if returncode != 0 and not allow_failure:
        raise subprocess.CalledProcessError(returncode, command)
    return returncode, elapsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run staged real-repository commands with FlowCausal JSON markers",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo-dir", default=".", help="source repository directory")
    parser.add_argument("--work-dir", help="destination work directory when copying the repository")
    parser.add_argument(
        "--copy-root",
        help=(
            "directory tree to copy before running commands; --repo-dir must be inside it. "
            "Use this for repos that depend on adjacent workspace paths"
        ),
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing --work-dir")
    parser.add_argument("--keep-work-dir", action="store_true", help="keep an auto-created temporary repository copy")
    parser.add_argument("--in-place", action="store_true", help="run commands directly in --repo-dir instead of copying")
    parser.add_argument(
        "--copy-exclude",
        action="append",
        default=[],
        help="additional shutil ignore pattern used when copying the repository",
    )
    parser.add_argument(
        "--copy-keep-build-artifacts",
        action="store_true",
        help=(
            "when copying the repository, keep build artifacts such as build/, "
            "*.so, *.a and *.o; useful for pinned compiled repos whose tests "
            "need already-built extension modules"
        ),
    )
    parser.add_argument(
        "--copy-keep-git",
        action="store_true",
        help=(
            "when copying the repository, keep .git metadata; needed for "
            "real agent traces that execute git diff/status commands"
        ),
    )
    parser.add_argument("--turns", type=int, default=1, help="repeat INSPECT/EDIT/BUILD/RETRY/TEST stages N times")
    parser.add_argument("--pre-sleep-ms", type=int, default=200, help="delay before the first command so observers can attach")
    parser.add_argument("--post-sleep-ms", type=int, default=200, help="delay after final work so observers can flush")
    parser.add_argument("--env", action="append", default=[], help="extra KEY=VALUE environment entry for stage commands")
    parser.add_argument(
        "--stage-spec-file",
        help="JSON object mapping stage names to shell commands; overrides per-stage cmd args",
    )
    parser.add_argument(
        "--stage-out-dir",
        help=(
            "directory exposed to commands as FLOWCAUSAL_STAGE_OUT; defaults to "
            "$FLOWCAUSAL_TRIAL_DIR/repo_stage_outputs under matrix runs or a temporary directory"
        ),
    )
    parser.add_argument(
        "--stage-state-file",
        help=(
            "JSON file updated at every stage begin/end; defaults to "
            "$FLOWCAUSAL_STAGE_STATE_FILE when set"
        ),
    )
    parser.add_argument(
        "--frontier-label-file",
        help=(
            "optional JSONL file that mirrors emitted frontier_label records; "
            "defaults to $FLOWCAUSAL_FRONTIER_LABEL_FILE when set"
        ),
    )
    parser.add_argument(
        "--no-frontier-labels",
        action="store_true",
        help="disable repo-stage oracle labels for active foreground stage subprocesses",
    )
    parser.add_argument(
        "--frontier-label-descendants",
        action="store_true",
        help=(
            "after labeling the stage shell, briefly sample /proc descendants "
            "and emit additional oracle labels for foreground worker PIDs"
        ),
    )
    parser.add_argument(
        "--frontier-label-follow-ms",
        type=int,
        default=250,
        help="sampling window for --frontier-label-descendants",
    )
    parser.add_argument(
        "--frontier-label-follow-interval-ms",
        type=int,
        default=10,
        help="sampling interval for --frontier-label-descendants",
    )
    parser.add_argument("--inspect-cmd", default="", help="optional inspect/search command")
    parser.add_argument("--edit-cmd", default="", help="optional edit/preparation command")
    parser.add_argument("--build-cmd", default="", help="optional build command")
    parser.add_argument("--retry-cmd", default="", help="optional expected-failure/retry-probe command")
    parser.add_argument("--test-cmd", default="", help="optional test command")
    parser.add_argument("--package-cmd", default="", help="optional post-critical package/cleanup command")
    parser.add_argument(
        "--critical-after",
        default="TEST",
        choices=[*MAIN_STAGE_ORDER],
        help="emit critical_sec after this final-turn stage; if absent, falls back to the last executed main stage",
    )
    parser.add_argument(
        "--allow-failure-stage",
        action="append",
        default=["RETRY_PROBE"],
        choices=[*MAIN_STAGE_ORDER, "PACKAGE_BACKGROUND"],
        help="stage whose nonzero exit status should not fail the workload",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.turns < 1:
        raise SystemExit("--turns must be positive")

    commands = stage_command_map(args)
    stages = selected_main_stages(commands)
    critical_stage = resolve_critical_stage(args.critical_after, stages)
    extra_env = parse_env(args.env)
    allow_failure = {stage.upper() for stage in args.allow_failure_stage}

    work_dir, cleanup_path, copy_root, copy_dst = prepare_work_dir(args)
    stage_out_cleanup: pathlib.Path | None = None
    if args.stage_out_dir:
        stage_out_dir = pathlib.Path(args.stage_out_dir).resolve()
    elif os.environ.get("FLOWCAUSAL_TRIAL_DIR"):
        stage_out_dir = pathlib.Path(os.environ["FLOWCAUSAL_TRIAL_DIR"]).resolve() / "repo_stage_outputs"
    else:
        stage_out_dir = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-repo-stage-out-"))
        stage_out_cleanup = stage_out_dir
    stage_out_dir.mkdir(parents=True, exist_ok=True)
    stage_state_file: pathlib.Path | None = None
    state_file_text = args.stage_state_file or os.environ.get("FLOWCAUSAL_STAGE_STATE_FILE")
    if state_file_text:
        stage_state_file = pathlib.Path(state_file_text).resolve()
    frontier_label_text = args.frontier_label_file or os.environ.get("FLOWCAUSAL_FRONTIER_LABEL_FILE")
    frontier_label_file = pathlib.Path(frontier_label_text).resolve() if frontier_label_text else None
    start = time.monotonic()
    timings: dict[str, float] = {}
    returncode = 0

    emit(
        "workload_begin",
        workload="repo_stage_workflow",
        repo_dir=str(pathlib.Path(args.repo_dir).resolve()),
        work_dir=str(work_dir),
        stage_out_dir=str(stage_out_dir),
        stage_state_file=str(stage_state_file) if stage_state_file is not None else "",
        copy_root=str(copy_root),
        copy_dst=str(copy_dst),
        in_place=bool(args.in_place),
        copy_keep_build_artifacts=bool(args.copy_keep_build_artifacts),
        turns=args.turns,
        stages=stages,
        critical_after=critical_stage,
        package_after_critical=bool(args.package_cmd),
        frontier_label_source=("disabled" if args.no_frontier_labels else "repo-stage-active-command"),
        frontier_label_descendants=bool(args.frontier_label_descendants),
        frontier_label_follow_ms=args.frontier_label_follow_ms,
        frontier_label_follow_interval_ms=args.frontier_label_follow_interval_ms,
    )

    try:
        if args.pre_sleep_ms > 0:
            time.sleep(args.pre_sleep_ms / 1000.0)

        critical_emitted = False
        for turn in range(args.turns):
            for stage_index, stage in enumerate(stages):
                source_id = f"turn{turn}:{stage.lower()}"
                _, elapsed = run_stage(
                    stage,
                    commands[stage],
                    cwd=work_dir,
                    stage_out_dir=stage_out_dir,
                    stage_state_file=stage_state_file,
                    frontier_label_file=frontier_label_file,
                    turn=turn,
                    command_index=turn * len(MAIN_STAGE_ORDER) + stage_index,
                    source_id=source_id,
                    emit_frontier_labels=not args.no_frontier_labels,
                    emit_descendant_frontier_labels=bool(args.frontier_label_descendants),
                    frontier_label_follow_ms=args.frontier_label_follow_ms,
                    frontier_label_follow_interval_ms=args.frontier_label_follow_interval_ms,
                    allow_failure=stage in allow_failure,
                    extra_env=extra_env,
                )
                timings[f"turn_{turn}_{stage.lower()}"] = elapsed
                if turn == args.turns - 1 and stage == critical_stage:
                    critical_sec = time.monotonic() - start
                    emit_metric(
                        "critical_sec",
                        critical_sec,
                        meaning=f"final_turn_{critical_stage.lower()}_complete",
                    )
                    emit("flowcausal_critical_path", elapsed_sec=critical_sec, stage=critical_stage)
                    critical_emitted = True

        if not critical_emitted:
            # This should only happen if the selected stage list changes after
            # validation.  Keep the workload log parseable rather than silently
            # losing the endpoint metric.
            critical_sec = time.monotonic() - start
            emit_metric("critical_sec", critical_sec, meaning="all_main_stages_complete")
            emit("flowcausal_critical_path", elapsed_sec=critical_sec, stage=critical_stage)

        if args.package_cmd:
            package_source_id = f"turn{args.turns - 1}:package_background"
            _, elapsed = run_stage(
                "PACKAGE_BACKGROUND",
                args.package_cmd,
                cwd=work_dir,
                stage_out_dir=stage_out_dir,
                stage_state_file=stage_state_file,
                frontier_label_file=frontier_label_file,
                turn=args.turns - 1,
                command_index=args.turns * len(MAIN_STAGE_ORDER),
                source_id=package_source_id,
                emit_frontier_labels=not args.no_frontier_labels,
                emit_descendant_frontier_labels=bool(args.frontier_label_descendants),
                frontier_label_follow_ms=args.frontier_label_follow_ms,
                frontier_label_follow_interval_ms=args.frontier_label_follow_interval_ms,
                allow_failure="PACKAGE_BACKGROUND" in allow_failure,
                extra_env=extra_env,
            )
            timings["package_after_critical"] = elapsed

        if args.post_sleep_ms > 0:
            time.sleep(args.post_sleep_ms / 1000.0)

    except subprocess.CalledProcessError as exc:
        returncode = int(exc.returncode or 1)
        emit("workload_error", workload="repo_stage_workflow", returncode=returncode, cmd=str(exc.cmd))
    finally:
        write_stage_state(
            stage_state_file,
            workload="repo_stage_workflow",
            phase="workload_end",
            stage="SHELL",
            returncode=returncode,
        )
        emit("workload_end", workload="repo_stage_workflow", returncode=returncode, timings=timings)
        if cleanup_path is not None and not args.keep_work_dir:
            write_stage_state(
                stage_state_file,
                workload="repo_stage_workflow",
                phase="cleanup_begin",
                stage="CLEANUP",
                path=str(cleanup_path),
                returncode=returncode,
            )
            shutil.rmtree(cleanup_path, ignore_errors=True)
            write_stage_state(
                stage_state_file,
                workload="repo_stage_workflow",
                phase="cleanup_end",
                stage="CLEANUP",
                path=str(cleanup_path),
                returncode=returncode,
            )
        if stage_out_cleanup is not None and not args.keep_work_dir:
            shutil.rmtree(stage_out_cleanup, ignore_errors=True)

    return returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Shared cleanup helpers for HarnessReplayBench outputs.

The benchmark's scientific evidence lives in summaries, logs, fidelity audits,
controller traces, and scheduler counters.  Per-case copied repositories and
large stage-output directories are reproducible execution scratch space and can
be removed for successful runs to keep long matrices from filling the disk.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import Any


DEFAULT_SUCCESS_ARTIFACTS = ("work", "stage-out", "agent_trace_outputs")


def artifact_names_or_default(names: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    return tuple(names or DEFAULT_SUCCESS_ARTIFACTS)


def validate_artifact_name(name: str) -> str:
    text = name.strip()
    if not text or text in {".", ".."}:
        raise ValueError(f"invalid cleanup artifact name: {name!r}")
    candidate = pathlib.PurePosixPath(text)
    if candidate.is_absolute() or len(candidate.parts) != 1:
        raise ValueError(f"cleanup artifact must be a direct child name, got {name!r}")
    return text


def path_size_bytes(path: pathlib.Path) -> int:
    """Return apparent disk usage for a file tree without following symlinks."""

    try:
        stat = path.lstat()
    except FileNotFoundError:
        return 0
    if not path.is_dir() or path.is_symlink():
        return int(stat.st_size)
    total = int(stat.st_size)
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        entry_stat = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    total += int(entry_stat.st_size)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(pathlib.Path(entry.path))
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
    return total


def remove_artifact_tree(path: pathlib.Path, *, use_sudo: bool) -> dict[str, Any]:
    """Remove a cleanup target and report the removal method.

    Docker replay cases can leave root-owned files under ``work/``.  The normal
    path is still a plain Python removal.  If that fails and ``use_sudo`` is
    enabled, retry with ``sudo -n`` so benchmark matrices do not retain
    gigabytes of successful, reproducible work trees.
    """

    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        return {"removed": True, "method": "python"}
    except OSError as exc:
        first_error = str(exc)

    if not use_sudo:
        return {"removed": False, "method": "python", "error": first_error}

    proc = subprocess.run(
        ["sudo", "-n", "rm", "-r", "--", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if proc.returncode == 0 and not path.exists():
        return {"removed": True, "method": "sudo"}
    return {
        "removed": False,
        "method": "sudo",
        "error": (proc.stderr or first_error).strip() or first_error,
        "python_error": first_error,
        "returncode": proc.returncode,
    }


def compact_case_artifacts(
    run_dir: pathlib.Path,
    *,
    enabled: bool,
    success: bool,
    artifact_names: list[str] | tuple[str, ...] | None = None,
    dry_run: bool = False,
    use_sudo: bool = True,
) -> dict[str, Any]:
    """Remove reproducible heavy artifacts from a case run directory.

    Cleanup is deliberately conservative: only direct children with approved
    names are touched, and failed cases are preserved unless the caller passes
    ``success=True``.
    """

    artifacts = artifact_names_or_default(artifact_names)
    record: dict[str, Any] = {
        "enabled": bool(enabled),
        "success": bool(success),
        "dry_run": bool(dry_run),
        "use_sudo": bool(use_sudo),
        "candidate_bytes": 0,
        "removed_bytes": 0,
        "artifacts": [],
    }
    if not enabled:
        record["reason"] = "disabled"
        return record
    if not success:
        record["reason"] = "preserve_failed_case"
        return record

    run_dir = run_dir.resolve()
    for raw_name in artifacts:
        name = validate_artifact_name(raw_name)
        target = run_dir / name
        item: dict[str, Any] = {
            "name": name,
            "path": str(target),
            "existed": target.exists() or target.is_symlink(),
            "removed": False,
            "bytes": 0,
        }
        if not item["existed"]:
            record["artifacts"].append(item)
            continue
        size = path_size_bytes(target)
        item["bytes"] = size
        record["candidate_bytes"] += size
        if dry_run:
            record["removed_bytes"] += size
        if not dry_run:
            removal = remove_artifact_tree(target, use_sudo=use_sudo)
            item.update(removal)
            if removal.get("removed"):
                record["removed_bytes"] += size
        record["artifacts"].append(item)
    return record

#!/usr/bin/env python3
"""Run a command inside a temporary cgroup-v2 CPU control group.

This helper exists for FlowCausal's static-resource baselines.  It does not
attach sched_ext and it does not infer frontiers; it only places the target
workflow in a fixed cgroup so a repo/agent trace matrix can compare FlowCausal
against ordinary Linux CPU control knobs.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import pwd
import subprocess
import sys
import time
from typing import Any


CGROUP_ROOT = pathlib.Path("/sys/fs/cgroup")


def emit(**payload: Any) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def read_text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def write_text(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def sudo_user_identity() -> tuple[int, int, str] | None:
    """Return the original sudo user when the wrapper is running as root.

    The static-cgroup baseline needs privilege to create/write cgroups, but the
    actual benchmark workload should remain comparable with the non-cgroup
    rows.  When invoked through sudo, keep the privileged wrapper and drop only
    the child command back to SUDO_UID/SUDO_GID.
    """

    if os.geteuid() != 0:
        return None
    uid_text = os.environ.get("SUDO_UID", "")
    gid_text = os.environ.get("SUDO_GID", "")
    if not uid_text or not gid_text:
        return None
    try:
        uid = int(uid_text)
        gid = int(gid_text)
    except ValueError:
        return None
    if uid == 0:
        return None
    return uid, gid, os.environ.get("SUDO_USER", "")


def child_env_and_preexec(identity: tuple[int, int, str] | None) -> tuple[dict[str, str] | None, Any]:
    if identity is None:
        return None, None

    uid, gid, user = identity
    env = os.environ.copy()
    try:
        pwent = pwd.getpwuid(uid)
        user = user or pwent.pw_name
        env["HOME"] = pwent.pw_dir
        env["USER"] = pwent.pw_name
        env["LOGNAME"] = pwent.pw_name
    except KeyError:
        env["USER"] = user or str(uid)
        env["LOGNAME"] = user or str(uid)

    def drop_privileges() -> None:
        try:
            if user:
                os.initgroups(user, gid)
            else:
                os.setgroups([])
        except OSError:
            os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    return env, drop_privileges


def ensure_cgroup_v2(root: pathlib.Path) -> None:
    if not root.exists():
        raise SystemExit(f"cgroup root does not exist: {root}")
    if not (root / "cgroup.controllers").exists():
        raise SystemExit(f"not a cgroup-v2 hierarchy: {root}")


def enable_cpu_controller(parent: pathlib.Path) -> None:
    controllers = set(read_text(parent / "cgroup.controllers").split())
    if "cpu" not in controllers:
        raise SystemExit(f"cpu controller is not available under {parent}")
    subtree = set(read_text(parent / "cgroup.subtree_control").split())
    if "cpu" in subtree:
        return
    try:
        write_text(parent / "cgroup.subtree_control", "+cpu")
    except OSError as exc:
        raise SystemExit(
            f"cpu controller is available but not enabled under {parent}; "
            f"enable +cpu or run with sufficient privilege: {exc}"
        ) from exc


def remove_cgroup(path: pathlib.Path) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            path.rmdir()
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a command in a temporary cgroup-v2 CPU baseline group",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--cgroup-root", default=str(CGROUP_ROOT))
    parser.add_argument("--name-prefix", default="flowcausal-static")
    parser.add_argument("--cpu-weight", type=int, default=0, help="write cpu.weight; 0 leaves kernel default")
    parser.add_argument("--cpu-max", default="", help="write cpu.max, e.g. '50000 100000' or 'max 100000'")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command after --")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing command; pass it after --")

    root = pathlib.Path(args.cgroup_root).resolve()
    ensure_cgroup_v2(root)
    enable_cpu_controller(root)

    name = f"{args.name_prefix}-{os.getpid()}-{time.time_ns()}"
    cgroup = root / name
    cgroup.mkdir(mode=0o755)
    try:
        if args.cpu_weight:
            if args.cpu_weight < 1 or args.cpu_weight > 10000:
                raise SystemExit("--cpu-weight must be in cgroup-v2 range [1, 10000]")
            write_text(cgroup / "cpu.weight", f"{args.cpu_weight}\n")
        if args.cpu_max:
            write_text(cgroup / "cpu.max", args.cpu_max.strip() + "\n")

        identity = sudo_user_identity()
        emit(
            type="flowcausal_baseline_begin",
            baseline="static_cgroup",
            cgroup=str(cgroup),
            cpu_weight=args.cpu_weight,
            cpu_max=args.cpu_max,
            command=command,
            dropped_to_sudo_user=bool(identity),
        )
        child_env, preexec_fn = child_env_and_preexec(identity)
        proc = subprocess.Popen(command, env=child_env, preexec_fn=preexec_fn)
        try:
            write_text(cgroup / "cgroup.procs", f"{proc.pid}\n")
        except OSError:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise
        returncode = proc.wait()
        emit(
            type="flowcausal_baseline_end",
            baseline="static_cgroup",
            cgroup=str(cgroup),
            returncode=returncode,
        )
        return int(returncode or 0)
    finally:
        remove_cgroup(cgroup)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

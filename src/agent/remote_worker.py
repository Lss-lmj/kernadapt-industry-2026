#!/usr/bin/env python3
"""Remote worker client for LockAgent runtime_service.py."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


SCHEMA_VERSION = "lockagent.remote_worker.v0.1"


def post_json(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    timeout_s: int = 30,
    allow_error: bool = False,
    auth_token: str | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = Request(base_url.rstrip("/") + path, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if not allow_error:
            raise
        result = json.loads(exc.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"{path} did not return a JSON object")
    return result


def register_worker(
    base_url: str,
    *,
    worker_id: str,
    roles: list[str] | None,
    auth_token: str | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return post_json(
        base_url,
        "/workers/register",
        {
            "worker_id": worker_id,
            "roles": roles or [],
            "metadata": metadata or {},
        },
        auth_token=auth_token,
        allow_error=True,
    )


def heartbeat_worker(base_url: str, *, worker_id: str, roles: list[str] | None, auth_token: str | None) -> dict[str, Any]:
    return post_json(
        base_url,
        "/workers/heartbeat",
        {
            "worker_id": worker_id,
            "roles": roles or [],
            "status": "ready",
        },
        auth_token=auth_token,
        allow_error=True,
    )


def lease_task(
    base_url: str,
    *,
    checkpoint: Path,
    worker_id: str,
    roles: list[str] | None,
    lease_s: int,
    auth_token: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "checkpoint": str(checkpoint),
        "worker_id": worker_id,
        "lease_s": lease_s,
    }
    if roles:
        payload["roles"] = roles
    return post_json(base_url, "/worker/lease", payload, auth_token=auth_token)


def complete_task(
    base_url: str,
    *,
    checkpoint: Path,
    worker_id: str,
    task_id: str,
    status: str,
    returncode: int | None,
    result: dict[str, Any],
    auth_token: str | None,
) -> dict[str, Any]:
    return post_json(
        base_url,
        "/worker/complete",
        {
            "checkpoint": str(checkpoint),
            "worker_id": worker_id,
            "task_id": task_id,
            "status": status,
            "returncode": returncode,
            "result": result,
        },
        allow_error=True,
        auth_token=auth_token,
    )


def run_command(command: list[str], *, cwd: Path | None, dry_run: bool, timeout_s: int | None) -> dict[str, Any]:
    if dry_run:
        return {"status": "ok", "returncode": 0, "stdout_tail": "", "stderr_tail": "", "dry_run": True}
    proc = subprocess.run(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=timeout_s,
    )
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-4000:],
        "dry_run": False,
    }


def run_once(
    *,
    base_url: str,
    checkpoint: Path,
    worker_id: str,
    roles: list[str] | None,
    lease_s: int,
    cwd: Path | None,
    dry_run: bool,
    command_timeout_s: int | None,
    auth_token: str | None,
) -> dict[str, Any]:
    heartbeat_worker(base_url, worker_id=worker_id, roles=roles, auth_token=auth_token)
    leased = lease_task(base_url, checkpoint=checkpoint, worker_id=worker_id, roles=roles, lease_s=lease_s, auth_token=auth_token)
    if leased.get("status") != "leased":
        return {"schema_version": SCHEMA_VERSION, "worker_id": worker_id, "status": leased.get("status"), "lease": leased}
    task = leased["task"]
    command = task.get("payload", {}).get("command")
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        return complete_task(
            base_url,
            checkpoint=checkpoint,
            worker_id=worker_id,
            task_id=str(task["task_id"]),
            status="skipped",
            returncode=0,
            result={"reason": "missing_command_payload"},
            auth_token=auth_token,
        )
    try:
        execution = run_command(command, cwd=cwd, dry_run=dry_run, timeout_s=command_timeout_s)
    except subprocess.TimeoutExpired as exc:
        execution = {
            "status": "failed",
            "returncode": 124,
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
            "error": "command_timeout",
            "dry_run": dry_run,
        }
    return complete_task(
        base_url,
        checkpoint=checkpoint,
        worker_id=worker_id,
        task_id=str(task["task_id"]),
        status=str(execution["status"]),
        returncode=int(execution["returncode"]),
        result={"execution": execution, "remote_worker_schema": SCHEMA_VERSION},
        auth_token=auth_token,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--worker-id", default="remote-worker")
    ap.add_argument("--role", action="append")
    ap.add_argument("--lease-s", type=int, default=300)
    ap.add_argument("--cwd", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--command-timeout-s", type=int)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--poll-s", type=float, default=2.0)
    ap.add_argument("--auth-token")
    ap.add_argument("--no-register", action="store_true")
    args = ap.parse_args(argv)

    if not args.no_register:
        registration = register_worker(
            args.base_url,
            worker_id=args.worker_id,
            roles=args.role,
            auth_token=args.auth_token,
            metadata={"client_schema": SCHEMA_VERSION, "cwd": str(args.cwd) if args.cwd else None},
        )
        if registration.get("status") == "error":
            print(json.dumps(registration, ensure_ascii=False, indent=2, sort_keys=True))
            return 1

    while True:
        result = run_once(
            base_url=args.base_url,
            checkpoint=args.checkpoint,
            worker_id=args.worker_id,
            roles=args.role,
            lease_s=args.lease_s,
            cwd=args.cwd,
            dry_run=args.dry_run,
            command_timeout_s=args.command_timeout_s,
            auth_token=args.auth_token,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if args.once:
            return 0 if result.get("status") in {"ok", "skipped", "no_task"} or result.get("ok") else 1
        if result.get("status") == "no_task":
            time.sleep(args.poll_s)


if __name__ == "__main__":
    raise SystemExit(main())

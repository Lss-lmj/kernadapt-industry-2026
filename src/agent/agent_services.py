#!/usr/bin/env python3
"""Service-style frontends for LockAgent memory, HITL, and worker queues."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from agent_runtime import AgentRun, stable_json
from global_memory import read_jsonl, retrieve_policy_context, summarize


SCHEMA_VERSION = "lockagent.services.v0.1"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(stable_json(data) + "\n", encoding="utf-8")


@contextmanager
def run_lock(checkpoint: Path):
    """Serialize file-backed checkpoint mutations across service processes."""

    run_dir = checkpoint.parent
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / ".lockagent_run.lock"
    with lock_path.open("w", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def memory_query(memory_path: Path, *, namespace: str | None = None, scheduler: str | None = None, failure: str | None = None) -> dict[str, Any]:
    records = read_jsonl(memory_path)
    filtered = records
    if namespace:
        filtered = [record for record in filtered if record.get("namespace") == namespace]
    if scheduler:
        filtered = [record for record in filtered if record.get("scheduler") == scheduler]
    if failure:
        filtered = [record for record in filtered if failure in record.get("failure_categories", [])]
    return {
        "schema_version": SCHEMA_VERSION,
        "memory": str(memory_path),
        "filters": {"namespace": namespace, "scheduler": scheduler, "failure": failure},
        "count": len(filtered),
        "records": filtered,
        "summary": summarize(filtered),
    }


def memory_retrieve(
    memory_path: Path,
    *,
    workload: str | None = None,
    scheduler: str | None = None,
    skill: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    records = read_jsonl(memory_path)
    result = retrieve_policy_context(records, workload=workload, scheduler=scheduler, skill=skill, limit=limit)
    return {
        "schema_version": SCHEMA_VERSION,
        "memory": str(memory_path),
        **result,
    }


def hitl_list(root: Path) -> dict[str, Any]:
    requests = []
    for path in sorted(root.glob("**/human_gate.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            data = {**data, "path": str(path), "checkpoint": str(path.parent / "agent_run.json")}
            requests.append(data)
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(root),
        "waiting": [item for item in requests if item.get("status") == "waiting"],
        "all": requests,
    }


def hitl_approve(checkpoint: Path, *, approved_by: str, note: str = "", auto_resume: bool = False, dry_run: bool = False) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        approval = run.approve_human_gate(approved_by=approved_by, note=note)
        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "approval": approval,
            "auto_resume": auto_resume,
        }
        if auto_resume:
            cmd = [
                "python3",
                str(Path(__file__).resolve().parent / "agent_runtime.py"),
                "auto-resume",
                "--checkpoint",
                str(checkpoint),
            ]
            if dry_run:
                cmd.append("--dry-run")
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
            result["resume_command"] = cmd
            result["resume_returncode"] = proc.returncode
            result["resume_output"] = proc.stdout
        write_json(checkpoint.parent / "hitl_approval_result.json", result)
        return result


def task_list(checkpoint: Path, *, status: str | None = None, role: str | None = None) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        run.scheduler.hydrate_from_durable()
        run.scheduler.flush()
        tasks = [task.to_dict() for task in sorted(run.scheduler._queue)]
        durable_tasks = run.durable.list_tasks(run.run_id)
        if status:
            tasks = [task for task in tasks if task.get("status") == status]
            durable_tasks = [task for task in durable_tasks if task.get("status") == status]
        if role:
            tasks = [task for task in tasks if task.get("role") == role]
            durable_tasks = [task for task in durable_tasks if task.get("role") == role]
        result = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "task_count": len(tasks),
            "durable_task_count": len(durable_tasks),
            "filters": {"status": status, "role": role},
            "tasks": tasks,
            "durable_tasks": durable_tasks,
            "scheduler_history_count": len(run.scheduler.history),
            "scheduler_history_tail": run.scheduler.history[-20:],
        }
        write_json(run.artifact_root / "tasks_view.json", result)
        run.store.register(run.artifact_root / "tasks_view.json", artifact_type="tasks_view", producer="agent_services.task_list")
        run.checkpoint()
        return result


def task_submit(
    checkpoint: Path,
    *,
    task_id: str,
    role: str,
    step_name: str,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    submitted_by: str = "runtime-service",
) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        task = run.submit_task(
            task_id=task_id,
            role=role,
            step_name=step_name,
            payload={**(payload or {}), "submitted_by": submitted_by},
            priority=priority,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "status": "submitted",
            "task": task.to_dict(),
            "submitted_by": submitted_by,
        }
        write_json(run.artifact_root / "task_submission_result.json", result)
        run.store.register(run.artifact_root / "task_submission_result.json", artifact_type="task_submission_result", producer="agent_services.task_submit")
        run.checkpoint()
        return result


def worker_once(
    checkpoint: Path,
    *,
    roles: list[str] | None = None,
    dry_run: bool = False,
    worker_id: str = "local-worker",
    lease_s: int = 300,
) -> dict[str, Any]:
    with run_lock(checkpoint):
        return _worker_once_locked(checkpoint, roles=roles, dry_run=dry_run, worker_id=worker_id, lease_s=lease_s)


def worker_pool(
    checkpoint: Path,
    *,
    roles: list[str] | None = None,
    dry_run: bool = False,
    worker_count: int = 2,
    max_tasks: int | None = None,
    lease_s: int = 300,
    worker_prefix: str = "pool-worker",
) -> dict[str, Any]:
    """Drain queued command tasks with multiple local worker threads.

    The workers share the same file-backed runtime but every lease/complete
    operation passes through `run_lock`, so this exercises the durable
    scheduling path without requiring a separate HTTP service.
    """

    started_at = timestamp()
    worker_count = max(1, int(worker_count))
    completed_budget = max_tasks if max_tasks is not None and max_tasks >= 0 else None
    budget_lock = threading.Lock()
    results_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    worker_iterations: dict[str, int] = {}

    def claim_budget() -> bool:
        nonlocal completed_budget
        if completed_budget is None:
            return True
        with budget_lock:
            if completed_budget <= 0:
                return False
            completed_budget -= 1
            return True

    def refund_budget() -> None:
        nonlocal completed_budget
        if completed_budget is None:
            return
        with budget_lock:
            completed_budget += 1

    def worker_loop(index: int) -> None:
        worker_id = f"{worker_prefix}-{index}"
        while claim_budget():
            result = worker_once(
                checkpoint,
                roles=roles,
                dry_run=dry_run,
                worker_id=worker_id,
                lease_s=lease_s,
            )
            with results_lock:
                results.append(result)
                worker_iterations[worker_id] = worker_iterations.get(worker_id, 0) + 1
            if result.get("status") == "no_task":
                refund_budget()
                break

    threads = [threading.Thread(target=worker_loop, args=(index,), name=f"{worker_prefix}-{index}") for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    completed_statuses = {"ok", "skipped", "failed"}
    completed = [item for item in results if item.get("status") in completed_statuses]
    failed = [item for item in results if item.get("status") == "failed"]
    no_task = [item for item in results if item.get("status") == "no_task"]
    run = AgentRun.load(checkpoint)
    result = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint": str(checkpoint),
        "run_id": run.run_id,
        "status": "failed" if failed else "ok",
        "started_at": started_at,
        "ended_at": timestamp(),
        "worker_count": worker_count,
        "worker_prefix": worker_prefix,
        "roles": roles,
        "dry_run": dry_run,
        "lease_s": lease_s,
        "max_tasks": max_tasks,
        "attempt_count": len(results),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "no_task_count": len(no_task),
        "worker_iterations": worker_iterations,
        "results": results,
    }
    write_json(run.artifact_root / "worker_pool_result.json", result)
    run.store.register(run.artifact_root / "worker_pool_result.json", artifact_type="worker_pool_result", producer="agent_services.worker_pool")
    run.scheduler.record_lifecycle(
        event="worker_pool_completed",
        step_name="worker_pool",
        role="WorkerPool",
        task_id=f"{run.run_id}:worker_pool:{int(time.time() * 1000)}",
        payload={
            "worker_count": worker_count,
            "completed_count": len(completed),
            "failed_count": len(failed),
            "no_task_count": len(no_task),
        },
    )
    run.checkpoint()
    return result


def _worker_once_locked(
    checkpoint: Path,
    *,
    roles: list[str] | None,
    dry_run: bool,
    worker_id: str,
    lease_s: int,
) -> dict[str, Any]:
    run = AgentRun.load(checkpoint)
    task = run.scheduler.pop_ready(roles, worker_id=worker_id, lease_s=lease_s)
    if task is None:
        result = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "status": "no_task",
            "roles": roles,
            "worker_id": worker_id,
        }
        write_json(run.artifact_root / "worker_result.json", result)
        return result

    command = task.payload.get("command") if isinstance(task.payload, dict) else None
    if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
        run.scheduler.complete(task.task_id, "skipped")
        result = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "status": "skipped",
            "task": task.to_dict(),
            "reason": "missing_command_payload",
            "worker_id": worker_id,
        }
        write_json(run.artifact_root / "worker_result.json", result)
        run.store.register(run.artifact_root / "worker_result.json", artifact_type="worker_result", producer="agent_services.worker_once")
        run.checkpoint()
        return result

    leased_task = task.to_dict()
    leased_until = task.lease_until
    step = run.start_step(
        name=task.step_name,
        role=task.role,
        required_capabilities=list(task.payload.get("required_capabilities", [])),
        input_artifacts=list(task.payload.get("input_artifacts", [])),
        output_artifacts=list(task.payload.get("output_artifacts", [])),
        metadata={
            "command": command,
            "worker_id": worker_id,
            "lease_s": lease_s,
            "resumed_from_task": task.task_id,
        },
    )
    if dry_run:
        returncode = 0
    else:
        proc = subprocess.run(command, cwd=Path.cwd(), check=False)
        returncode = proc.returncode
    status = "ok" if returncode == 0 else "failed"
    run.end_step(
        step,
        status=status,
        returncode=returncode,
        error=None if returncode == 0 else f"worker command failed with return code {returncode}",
        output_artifacts=list(task.payload.get("output_artifacts", [])),
    )
    run.scheduler.complete(task.task_id, status)
    result = {
        "schema_version": SCHEMA_VERSION,
        "checkpoint": str(checkpoint),
        "status": status,
        "task": leased_task,
        "step": step.to_dict(),
        "returncode": returncode,
        "dry_run": dry_run,
        "worker_id": worker_id,
        "lease_until": leased_until,
        "timestamp": timestamp(),
    }
    write_json(run.artifact_root / "worker_result.json", result)
    run.store.register(run.artifact_root / "worker_result.json", artifact_type="worker_result", producer="agent_services.worker_once")
    run.checkpoint()
    return result


def worker_heartbeat(checkpoint: Path, *, task_id: str, worker_id: str, lease_s: int = 300) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        result = run.scheduler.heartbeat(task_id, worker_id=worker_id, lease_s=lease_s)
        result = {"schema_version": SCHEMA_VERSION, "checkpoint": str(checkpoint), "worker_id": worker_id, **result}
        write_json(run.artifact_root / "worker_heartbeat.json", result)
        return result


def worker_lease(
    checkpoint: Path,
    *,
    roles: list[str] | None = None,
    worker_id: str = "remote-worker",
    lease_s: int = 300,
) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        task = run.scheduler.pop_ready(roles, worker_id=worker_id, lease_s=lease_s)
        if task is None:
            result = {
                "schema_version": SCHEMA_VERSION,
                "checkpoint": str(checkpoint),
                "status": "no_task",
                "roles": roles,
                "worker_id": worker_id,
            }
        else:
            result = {
                "schema_version": SCHEMA_VERSION,
                "checkpoint": str(checkpoint),
                "status": "leased",
                "task": task.to_dict(),
                "worker_id": worker_id,
                "lease_s": lease_s,
            }
        write_json(run.artifact_root / "worker_lease.json", result)
        run.store.register(run.artifact_root / "worker_lease.json", artifact_type="worker_lease", producer="agent_services.worker_lease")
        run.checkpoint()
        return result


def worker_complete(
    checkpoint: Path,
    *,
    task_id: str,
    worker_id: str,
    status: str,
    returncode: int | None = None,
    result_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        outcome = run.scheduler.complete_leased(
            task_id,
            worker_id=worker_id,
            status=status,
            returncode=returncode,
            result=result_payload or {},
        )
        if outcome.get("ok"):
            task = outcome.get("task", {})
            payload = task.get("payload", {}) if isinstance(task, dict) else {}
            step = run.record_step(
                name=str(task.get("step_name", task_id)),
                role=str(task.get("role", "RemoteWorker")),
                required_capabilities=list(payload.get("required_capabilities", [])) if isinstance(payload, dict) else [],
                input_artifacts=list(payload.get("input_artifacts", [])) if isinstance(payload, dict) else [],
                output_artifacts=list(payload.get("output_artifacts", [])) if isinstance(payload, dict) else [],
                metadata={
                    "remote_worker_id": worker_id,
                    "remote_task_id": task_id,
                    "remote_completion": result_payload or {},
                },
                status=status,
                returncode=returncode,
                error=None if status == "ok" else str((result_payload or {}).get("error", "remote worker reported failure")),
            )
            outcome["step"] = step.to_dict()
        result = {
            "schema_version": SCHEMA_VERSION,
            "checkpoint": str(checkpoint),
            "worker_id": worker_id,
            **outcome,
        }
        write_json(run.artifact_root / "worker_complete.json", result)
        run.store.register(run.artifact_root / "worker_complete.json", artifact_type="worker_complete", producer="agent_services.worker_complete")
        run.checkpoint()
        return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    memory = sub.add_parser("memory-query")
    memory.add_argument("--memory", type=Path, required=True)
    memory.add_argument("--scheduler")
    memory.add_argument("--failure")
    memory.add_argument("--out", type=Path)
    hitl = sub.add_parser("hitl-list")
    hitl.add_argument("--root", type=Path, required=True)
    hitl.add_argument("--out", type=Path)
    approve = sub.add_parser("hitl-approve")
    approve.add_argument("--checkpoint", type=Path, required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--note", default="")
    approve.add_argument("--auto-resume", action="store_true")
    approve.add_argument("--dry-run", action="store_true")
    worker = sub.add_parser("worker-once")
    worker.add_argument("--checkpoint", type=Path, required=True)
    worker.add_argument("--role", action="append")
    worker.add_argument("--dry-run", action="store_true")
    worker.add_argument("--worker-id", default="local-worker")
    worker.add_argument("--lease-s", type=int, default=300)
    worker.add_argument("--out", type=Path)
    pool = sub.add_parser("worker-pool")
    pool.add_argument("--checkpoint", type=Path, required=True)
    pool.add_argument("--role", action="append")
    pool.add_argument("--dry-run", action="store_true")
    pool.add_argument("--worker-count", type=int, default=2)
    pool.add_argument("--max-tasks", type=int)
    pool.add_argument("--worker-prefix", default="pool-worker")
    pool.add_argument("--lease-s", type=int, default=300)
    pool.add_argument("--out", type=Path)
    lease = sub.add_parser("worker-lease")
    lease.add_argument("--checkpoint", type=Path, required=True)
    lease.add_argument("--role", action="append")
    lease.add_argument("--worker-id", default="remote-worker")
    lease.add_argument("--lease-s", type=int, default=300)
    lease.add_argument("--out", type=Path)
    complete = sub.add_parser("worker-complete")
    complete.add_argument("--checkpoint", type=Path, required=True)
    complete.add_argument("--task-id", required=True)
    complete.add_argument("--worker-id", required=True)
    complete.add_argument("--status", required=True)
    complete.add_argument("--returncode", type=int)
    complete.add_argument("--result", default="{}")
    complete.add_argument("--out", type=Path)
    heartbeat = sub.add_parser("worker-heartbeat")
    heartbeat.add_argument("--checkpoint", type=Path, required=True)
    heartbeat.add_argument("--task-id", required=True)
    heartbeat.add_argument("--worker-id", required=True)
    heartbeat.add_argument("--lease-s", type=int, default=300)
    heartbeat.add_argument("--out", type=Path)
    args = ap.parse_args(argv)

    if args.cmd == "memory-query":
        result = memory_query(args.memory, scheduler=args.scheduler, failure=args.failure)
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0
    if args.cmd == "hitl-list":
        result = hitl_list(args.root)
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0
    if args.cmd == "hitl-approve":
        result = hitl_approve(args.checkpoint, approved_by=args.approved_by, note=args.note, auto_resume=args.auto_resume, dry_run=args.dry_run)
        print(stable_json(result))
        return 0 if result.get("resume_returncode", 0) == 0 else 1
    if args.cmd == "worker-once":
        result = worker_once(args.checkpoint, roles=args.role, dry_run=args.dry_run, worker_id=args.worker_id, lease_s=args.lease_s)
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0 if result["status"] in {"ok", "skipped", "no_task"} else 1
    if args.cmd == "worker-pool":
        result = worker_pool(
            args.checkpoint,
            roles=args.role,
            dry_run=args.dry_run,
            worker_count=args.worker_count,
            max_tasks=args.max_tasks,
            worker_prefix=args.worker_prefix,
            lease_s=args.lease_s,
        )
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0 if result["status"] == "ok" else 1
    if args.cmd == "worker-lease":
        result = worker_lease(args.checkpoint, roles=args.role, worker_id=args.worker_id, lease_s=args.lease_s)
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0 if result["status"] in {"leased", "no_task"} else 1
    if args.cmd == "worker-complete":
        result_payload = json.loads(args.result)
        result = worker_complete(
            args.checkpoint,
            task_id=args.task_id,
            worker_id=args.worker_id,
            status=args.status,
            returncode=args.returncode,
            result_payload=result_payload if isinstance(result_payload, dict) else {"value": result_payload},
        )
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0 if result.get("ok") else 1
    if args.cmd == "worker-heartbeat":
        result = worker_heartbeat(args.checkpoint, task_id=args.task_id, worker_id=args.worker_id, lease_s=args.lease_s)
        if args.out:
            write_json(args.out, result)
        print(stable_json(result))
        return 0 if result.get("ok") else 1
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())

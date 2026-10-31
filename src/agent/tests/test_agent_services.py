#!/usr/bin/env python3
"""Local checks for LockAgent service-style frontends."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import threading
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SERVICES = ROOT / "agent_services.py"
RUNTIME = ROOT / "agent_runtime.py"
spec = importlib.util.spec_from_file_location("agent_services", SERVICES)
assert spec and spec.loader
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
rt_spec = importlib.util.spec_from_file_location("agent_runtime", RUNTIME)
assert rt_spec and rt_spec.loader
runtime = importlib.util.module_from_spec(rt_spec)
sys.modules["agent_runtime"] = runtime
rt_spec.loader.exec_module(runtime)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        memory = base / "global_memory.jsonl"
        memory.write_text(
            json.dumps({"workload": "mariadb-sysbench", "scheduler": "scx_lockcoop", "failure_categories": ["skill_misfire"], "run_id": "run_1", "trajectory_valid": False, "skill": "choose_locknest_for_hot_lock_oltp"}) + "\n"
            + json.dumps({"workload": "mariadb-sysbench", "scheduler": "default", "failure_categories": [], "run_id": "run_2", "trajectory_valid": True, "skill": "fallback_default_for_non_lock_bound"}) + "\n",
            encoding="utf-8",
        )
        result = services.memory_query(memory, scheduler="scx_lockcoop")
        assert result["count"] == 1, result
        assert result["records"][0]["run_id"] == "run_1", result
        retrieved = services.memory_retrieve(memory, workload="mariadb-sysbench", scheduler="scx_lockcoop", limit=2)
        assert retrieved["returned_count"] == 2, retrieved
        assert retrieved["negative_examples"][0]["run_id"] == "run_1", retrieved
        assert retrieved["policy_hints"]["avoid_schedulers_with_repeated_failures"]["scx_lockcoop"]["skill_misfire"] == 1, retrieved

        run = runtime.AgentRun(
            run_id="run_services",
            intent="online",
            artifact_root=base / "run_services",
            environment_fingerprint={"kernel": "test"},
        )
        run.pause_for_human(step_name="scx_executor", reason="approve", artifacts=[])
        run.record_step(
            name="human_approval_gate",
            role=runtime.ROLE_GUARD,
            required_capabilities=[runtime.CAP_EVALUATE_POLICY],
            metadata={"requires_human_approval": True},
            status="paused",
            returncode=2,
        )
        run.submit_task(
            task_id="queued-worker",
            role=runtime.ROLE_EXECUTOR,
            step_name="queued_worker",
            payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
            priority=1,
        )
        listed = services.hitl_list(base)
        assert len(listed["waiting"]) == 1, listed
        approval = services.hitl_approve(base / "run_services" / "agent_run.json", approved_by="tester", auto_resume=True, dry_run=True)
        assert approval["approval"]["status"] == "approved", approval
        assert approval["resume_returncode"] == 0, approval

        worker_run = runtime.AgentRun(
            run_id="run_worker",
            intent="online",
            artifact_root=base / "run_worker",
            environment_fingerprint={"kernel": "test"},
        )
        worker_run.submit_task(
            task_id="task-worker",
            role=runtime.ROLE_EXECUTOR,
            step_name="worker_exec",
            payload={
                "command": ["/bin/true"],
                "required_capabilities": [runtime.CAP_RUN_BENCHMARK],
                "output_artifacts": [str(base / "run_worker" / "worker.out")],
            },
            priority=1,
        )
        worker_result = services.worker_once(
            base / "run_worker" / "agent_run.json",
            dry_run=True,
            worker_id="worker-a",
            lease_s=60,
        )
        assert worker_result["status"] == "ok", worker_result
        assert worker_result["task"]["task_id"] == "task-worker", worker_result
        assert worker_result["task"]["lease_owner"] == "worker-a", worker_result
        assert worker_result["task"]["attempts"] == 1, worker_result
        assert worker_result["step"]["name"] == "worker_exec", worker_result
        loaded_worker_run = runtime.AgentRun.load(base / "run_worker" / "agent_run.json")
        assert any(step.name == "worker_exec" and step.status == "ok" for step in loaded_worker_run.steps)
        assert (base / "run_worker" / "worker_result.json").exists()

        lease_run = runtime.AgentRun(
            run_id="run_lease",
            intent="online",
            artifact_root=base / "run_lease",
            environment_fingerprint={"kernel": "test"},
        )
        lease_run.submit_task(
            task_id="task-lease",
            role=runtime.ROLE_EXECUTOR,
            step_name="lease_exec",
            payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
            priority=1,
        )
        leased = lease_run.scheduler.pop_ready([runtime.ROLE_EXECUTOR], worker_id="worker-a", lease_s=60)
        assert leased is not None and leased.lease_owner == "worker-a", leased
        heartbeat = services.worker_heartbeat(
            base / "run_lease" / "agent_run.json",
            task_id="task-lease",
            worker_id="worker-a",
            lease_s=120,
        )
        assert heartbeat["ok"] is True, heartbeat
        wrong_worker = services.worker_heartbeat(
            base / "run_lease" / "agent_run.json",
            task_id="task-lease",
            worker_id="worker-b",
            lease_s=120,
        )
        assert wrong_worker["ok"] is False and wrong_worker["reason"] == "lease_not_owned", wrong_worker

        concurrent_run = runtime.AgentRun(
            run_id="run_concurrent_lease",
            intent="online",
            artifact_root=base / "run_concurrent_lease",
            environment_fingerprint={"kernel": "test"},
        )
        concurrent_run.submit_task(
            task_id="task-concurrent",
            role=runtime.ROLE_EXECUTOR,
            step_name="concurrent_exec",
            payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
            priority=1,
        )
        results = []

        def lease(worker_id: str) -> None:
            results.append(
                services.worker_lease(
                    base / "run_concurrent_lease" / "agent_run.json",
                    roles=[runtime.ROLE_EXECUTOR],
                    worker_id=worker_id,
                    lease_s=60,
                )
            )

        threads = [threading.Thread(target=lease, args=(f"worker-{idx}",)) for idx in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        statuses = sorted(result["status"] for result in results)
        assert statuses == ["leased", "no_task"], results
        leased_result = next(result for result in results if result["status"] == "leased")
        rejected = services.worker_complete(
            base / "run_concurrent_lease" / "agent_run.json",
            task_id="task-concurrent",
            worker_id="not-owner",
            status="ok",
            returncode=0,
            result_payload={},
        )
        assert rejected["ok"] is False and rejected["reason"] == "lease_not_owned", rejected
        completed = services.worker_complete(
            base / "run_concurrent_lease" / "agent_run.json",
            task_id="task-concurrent",
            worker_id=leased_result["worker_id"],
            status="ok",
            returncode=0,
            result_payload={"stdout_tail": ""},
        )
        assert completed["ok"] is True, completed

        pool_run = runtime.AgentRun(
            run_id="run_worker_pool",
            intent="online",
            artifact_root=base / "run_worker_pool",
            environment_fingerprint={"kernel": "test"},
        )
        for index in range(4):
            pool_run.submit_task(
                task_id=f"pool-task-{index}",
                role=runtime.ROLE_EXECUTOR,
                step_name=f"pool_exec_{index}",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=index,
            )
        pool_result = services.worker_pool(
            base / "run_worker_pool" / "agent_run.json",
            roles=[runtime.ROLE_EXECUTOR],
            dry_run=True,
            worker_count=3,
            max_tasks=4,
            worker_prefix="unit-pool",
            lease_s=60,
        )
        assert pool_result["status"] == "ok", pool_result
        assert pool_result["worker_count"] == 3, pool_result
        assert pool_result["completed_count"] == 4, pool_result
        assert pool_result["failed_count"] == 0, pool_result
        assert len(pool_result["worker_iterations"]) >= 1, pool_result
        pool_loaded = runtime.AgentRun.load(base / "run_worker_pool" / "agent_run.json")
        completed_tasks = [task for task in pool_loaded.scheduler._queue if task.task_id.startswith("pool-task-")]
        assert len(completed_tasks) == 4, completed_tasks
        assert all(task.status == "ok" for task in completed_tasks), completed_tasks
        assert len({step.name for step in pool_loaded.steps if step.name.startswith("pool_exec_")}) == 4, pool_loaded.steps
        assert (base / "run_worker_pool" / "worker_pool_result.json").exists()

        cli_pool_run = runtime.AgentRun(
            run_id="run_worker_pool_cli",
            intent="online",
            artifact_root=base / "run_worker_pool_cli",
            environment_fingerprint={"kernel": "test"},
        )
        for index in range(2):
            cli_pool_run.submit_task(
                task_id=f"cli-pool-task-{index}",
                role=runtime.ROLE_EXECUTOR,
                step_name=f"cli_pool_exec_{index}",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=index,
            )
        cli_proc = subprocess.run(
            [
                "python3",
                str(SERVICES),
                "worker-pool",
                "--checkpoint",
                str(base / "run_worker_pool_cli" / "agent_run.json"),
                "--role",
                runtime.ROLE_EXECUTOR,
                "--worker-count",
                "2",
                "--max-tasks",
                "2",
                "--dry-run",
                "--worker-prefix",
                "cli-pool",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert cli_proc.returncode == 0, cli_proc.stdout
        assert (base / "run_worker_pool_cli" / "worker_pool_result.json").exists()
        cli_pool_result = json.loads((base / "run_worker_pool_cli" / "worker_pool_result.json").read_text(encoding="utf-8"))
        assert cli_pool_result["completed_count"] == 2, cli_pool_result

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

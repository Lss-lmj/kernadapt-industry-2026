#!/usr/bin/env python3
"""Local checks for the LockAgent HTTP runtime service."""

from __future__ import annotations

import importlib.util
import json
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME_PATH = ROOT / "agent_runtime.py"
SERVICE_PATH = ROOT / "runtime_service.py"
REMOTE_WORKER_PATH = ROOT / "remote_worker.py"
rt_spec = importlib.util.spec_from_file_location("agent_runtime", RUNTIME_PATH)
assert rt_spec and rt_spec.loader
runtime = importlib.util.module_from_spec(rt_spec)
sys.modules["agent_runtime"] = runtime
rt_spec.loader.exec_module(runtime)


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    try:
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def get_json(url: str, *, auth_token: str | None = None, expect_error: bool = False) -> dict:
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = Request(url, headers=headers, method="GET")
    try:
        resp_ctx = urlopen(req, timeout=5)
    except HTTPError as exc:
        if not expect_error:
            raise
        return json.loads(exc.read().decode("utf-8"))
    with resp_ctx as resp:
        return json.loads(resp.read().decode("utf-8"))


def post_json(url: str, payload: dict, *, expect_error: bool = False, auth_token: str | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if not expect_error:
            raise
        return json.loads(exc.read().decode("utf-8"))


def wait_health(base_url: str) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            health = get_json(base_url + "/health")
            if health.get("status") == "ok":
                return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError("runtime service did not become healthy")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        root = base / "runs"
        memory = base / "memory" / "global_memory.jsonl"
        memory.parent.mkdir(parents=True)
        memory.write_text(
            json.dumps({"run_id": "run_mem", "workload": "mariadb-sysbench", "scheduler": "scx_lockcoop", "failure_categories": ["skill_misfire"], "trajectory_valid": False, "skill": "choose_locknest_for_hot_lock_oltp"}) + "\n"
            + json.dumps({"run_id": "run_mem_ok", "workload": "mariadb-sysbench", "scheduler": "default", "failure_categories": [], "trajectory_valid": True, "skill": "fallback_default_for_non_lock_bound"}) + "\n",
            encoding="utf-8",
        )
        port = free_port()
        proc = subprocess.Popen(
            [
                "python3",
                str(SERVICE_PATH),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--root",
                str(root),
                "--memory",
                str(memory),
                "--auth-token",
                "test-token",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            url = f"http://127.0.0.1:{port}"
            wait_health(url)
            health = get_json(url + "/health")
            assert health["status"] == "ok", health
            assert health["auth_enabled"] is True, health
            unauth = get_json(url + "/memory?scheduler=scx_lockcoop", expect_error=True)
            assert unauth["error"] == "unauthorized", unauth
            mem = get_json(url + "/memory?scheduler=scx_lockcoop", auth_token="test-token")
            assert mem["count"] == 1, mem
            memory_summary = get_json(url + "/memory/summary", auth_token="test-token")
            assert memory_summary["summary"]["record_count"] == 2, memory_summary
            retrieved_memory = get_json(
                url + "/memory/retrieve?workload=mariadb-sysbench&scheduler=scx_lockcoop&limit=2",
                auth_token="test-token",
            )
            assert retrieved_memory["returned_count"] == 2, retrieved_memory
            assert retrieved_memory["negative_examples"][0]["run_id"] == "run_mem", retrieved_memory
            appended = post_json(
                url + "/memory/append",
                {
                    "record": {
                        "run_id": "run_appended",
                        "namespace": "test",
                        "scheduler": "default",
                        "failure_categories": [],
                    }
                },
                auth_token="test-token",
            )
            assert appended["status"] == "appended", appended
            assert appended["count"] == 3, appended
            registered = post_json(
                url + "/workers/register",
                {
                    "worker_id": "worker-reg-a",
                    "roles": [runtime.ROLE_EXECUTOR],
                    "metadata": {"host": "unit-test"},
                },
                auth_token="test-token",
            )
            assert registered["status"] == "registered", registered
            worker_hb = post_json(
                url + "/workers/heartbeat",
                {
                    "worker_id": "worker-reg-a",
                    "roles": [runtime.ROLE_EXECUTOR],
                    "status": "ready",
                },
                auth_token="test-token",
            )
            assert worker_hb["status"] == "heartbeat", worker_hb
            workers = get_json(url + "/workers", auth_token="test-token")
            assert workers["worker_count"] == 1, workers
            assert workers["active_worker_count"] == 1, workers
            assert workers["durable_workers"], workers
            assert any(worker["worker_id"] == "worker-reg-a" for worker in workers["durable_workers"]), workers

            task_api_run = runtime.AgentRun(
                run_id="run_task_api",
                intent="online",
                artifact_root=root / "run_task_api",
                environment_fingerprint={"kernel": "test"},
            )
            submitted = post_json(
                url + "/tasks/submit",
                {
                    "checkpoint": str(root / "run_task_api" / "agent_run.json"),
                    "task_id": "queued-http-task-api",
                    "role": runtime.ROLE_EXECUTOR,
                    "step_name": "http_task_api_exec",
                    "payload": {"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                    "priority": 1,
                    "submitted_by": "unit-test",
                },
                auth_token="test-token",
            )
            assert submitted["status"] == "submitted", submitted
            assert submitted["task"]["task_id"] == "queued-http-task-api", submitted
            listed = get_json(
                url + "/tasks?checkpoint=" + str(root / "run_task_api" / "agent_run.json"),
                auth_token="test-token",
            )
            assert listed["task_count"] == 1, listed
            assert listed["durable_task_count"] == 1, listed
            assert listed["tasks"][0]["status"] == "queued", listed
            task_worker = post_json(
                url + "/worker/once",
                {
                    "checkpoint": str(root / "run_task_api" / "agent_run.json"),
                    "roles": [runtime.ROLE_EXECUTOR],
                    "worker_id": "http-task-worker",
                    "lease_s": 60,
                    "dry_run": True,
                },
                auth_token="test-token",
            )
            assert task_worker["status"] == "ok", task_worker
            listed_done = get_json(
                url + "/tasks?checkpoint=" + str(root / "run_task_api" / "agent_run.json") + "&status=ok",
                auth_token="test-token",
            )
            assert listed_done["task_count"] == 1, listed_done
            assert listed_done["tasks"][0]["status"] == "ok", listed_done
            adapter_invoked = post_json(
                url + "/adapter/invoke",
                {
                    "checkpoint": str(root / "run_task_api" / "agent_run.json"),
                    "framework": "openai_agents",
                    "tool_name": "policy_decision",
                    "payload": {"input_artifacts": ["profile.json"]},
                    "task_id": "http-adapter-task",
                    "priority": 5,
                },
                auth_token="test-token",
            )
            assert adapter_invoked["status"] == "submitted", adapter_invoked
            assert adapter_invoked["framework"] == "openai_agents", adapter_invoked
            assert adapter_invoked["task"]["task_id"] == "http-adapter-task", adapter_invoked
            assert (root / "run_task_api" / "runtime_api_adapter_result.json").exists()

            run = runtime.AgentRun(
                run_id="run_service_http",
                intent="online",
                artifact_root=root / "run_service_http",
                environment_fingerprint={"kernel": "test"},
            )
            run.pause_for_human(step_name="scx_executor", reason="approval required", artifacts=[])
            run.record_step(
                name="human_approval_gate",
                role=runtime.ROLE_GUARD,
                required_capabilities=[runtime.CAP_EVALUATE_POLICY],
                metadata={"requires_human_approval": True},
                status="paused",
                returncode=2,
            )
            run.submit_task(
                task_id="queued-http-worker",
                role=runtime.ROLE_EXECUTOR,
                step_name="http_worker_exec",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=1,
            )
            hitl = get_json(url + "/hitl", auth_token="test-token")
            assert len(hitl["waiting"]) == 1, hitl
            approval = post_json(
                url + "/hitl/approve",
                {
                    "checkpoint": str(root / "run_service_http" / "agent_run.json"),
                    "approved_by": "test",
                    "auto_resume": False,
                },
                auth_token="test-token",
            )
            assert approval["approval"]["status"] == "approved", approval
            worker = post_json(
                url + "/worker/once",
                {
                    "checkpoint": str(root / "run_service_http" / "agent_run.json"),
                    "roles": [runtime.ROLE_EXECUTOR],
                    "worker_id": "http-worker-a",
                    "lease_s": 60,
                    "dry_run": True,
                },
                auth_token="test-token",
            )
            assert worker["status"] == "ok", worker
            assert worker["task"]["lease_owner"] == "http-worker-a", worker

            remote_run = runtime.AgentRun(
                run_id="run_remote_worker",
                intent="online",
                artifact_root=root / "run_remote_worker",
                environment_fingerprint={"kernel": "test"},
            )
            remote_run.submit_task(
                task_id="queued-remote-worker",
                role=runtime.ROLE_EXECUTOR,
                step_name="remote_worker_exec",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=1,
            )
            lease = post_json(
                url + "/worker/lease",
                {
                    "checkpoint": str(root / "run_remote_worker" / "agent_run.json"),
                    "roles": [runtime.ROLE_EXECUTOR],
                    "worker_id": "remote-worker-a",
                    "lease_s": 60,
                },
                auth_token="test-token",
            )
            assert lease["status"] == "leased", lease
            assert lease["task"]["lease_owner"] == "remote-worker-a", lease
            complete = post_json(
                url + "/worker/complete",
                {
                    "checkpoint": str(root / "run_remote_worker" / "agent_run.json"),
                    "task_id": "queued-remote-worker",
                    "worker_id": "remote-worker-a",
                    "status": "ok",
                    "returncode": 0,
                    "result": {"stdout_tail": "", "stderr_tail": ""},
                },
                auth_token="test-token",
            )
            assert complete["ok"] is True, complete
            assert complete["step"]["name"] == "remote_worker_exec", complete

            remote_run2 = runtime.AgentRun(
                run_id="run_remote_worker_client",
                intent="online",
                artifact_root=root / "run_remote_worker_client",
                environment_fingerprint={"kernel": "test"},
            )
            remote_run2.submit_task(
                task_id="queued-remote-client",
                role=runtime.ROLE_EXECUTOR,
                step_name="remote_client_exec",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=1,
            )
            remote_proc = subprocess.run(
                [
                    "python3",
                    str(REMOTE_WORKER_PATH),
                    "--base-url",
                    url,
                    "--checkpoint",
                    str(root / "run_remote_worker_client" / "agent_run.json"),
                    "--worker-id",
                    "remote-client-a",
                "--role",
                runtime.ROLE_EXECUTOR,
                "--once",
                "--dry-run",
                "--auth-token",
                "test-token",
            ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            assert remote_proc.returncode == 0, remote_proc.stdout
            completed_run = runtime.AgentRun.load(root / "run_remote_worker_client" / "agent_run.json")
            assert any(step.name == "remote_client_exec" and step.status == "ok" for step in completed_run.steps)

            concurrent_run = runtime.AgentRun(
                run_id="run_http_concurrent_lease",
                intent="online",
                artifact_root=root / "run_http_concurrent_lease",
                environment_fingerprint={"kernel": "test"},
            )
            concurrent_run.submit_task(
                task_id="queued-http-concurrent",
                role=runtime.ROLE_EXECUTOR,
                step_name="http_concurrent_exec",
                payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
                priority=1,
            )
            lease_results = []

            def lease(worker_id: str) -> None:
                lease_results.append(
                    post_json(
                        url + "/worker/lease",
                        {
                            "checkpoint": str(root / "run_http_concurrent_lease" / "agent_run.json"),
                            "roles": [runtime.ROLE_EXECUTOR],
                            "worker_id": worker_id,
                            "lease_s": 60,
                        },
                        auth_token="test-token",
                    )
                )

            threads = [threading.Thread(target=lease, args=(f"http-worker-{idx}",)) for idx in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert sorted(result["status"] for result in lease_results) == ["leased", "no_task"], lease_results
            assert (root / "runtime_service_events.jsonl").exists()
            assert (root / "run_service_http" / "runtime_service_events.jsonl").exists()
            assert (root / "run_remote_worker" / "runtime_service_events.jsonl").exists()
            assert (root / "run_remote_worker_client" / "runtime_service_events.jsonl").exists()
            html = urlopen(url + "/ui/hitl?token=test-token", timeout=5).read().decode("utf-8")
            assert "LockAgent HITL Queue" in html, html
            task_html = urlopen(url + "/ui/tasks?checkpoint=" + str(root / "run_task_api" / "agent_run.json") + "&token=test-token", timeout=5).read().decode("utf-8")
            assert "LockAgent Task Queue" in task_html, task_html
            assert "queued-http-task-api" in task_html, task_html
            registry = json.loads((root / "worker_registry.json").read_text(encoding="utf-8"))
            assert "worker-reg-a" in registry["workers"], registry
            assert "remote-client-a" in registry["workers"], registry
            durable_workers = runtime.DurableStore(root).list_workers()
            assert any(worker["worker_id"] == "worker-reg-a" for worker in durable_workers), durable_workers
            assert any(worker["worker_id"] == "remote-client-a" for worker in durable_workers), durable_workers
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

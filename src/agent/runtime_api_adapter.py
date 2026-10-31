#!/usr/bin/env python3
"""Runtime API adapter for mainstream agent-framework style calls.

The default path maps common framework invocation shapes onto LockAgent's
durable runtime primitives. LangGraph additionally has an optional native
backend that invokes a real StateGraph when the dependency is installed.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from agent_runtime import (
    CAP_CALL_LLM,
    CAP_CURATE_SKILL,
    CAP_EVALUATE_POLICY,
    CAP_LOAD_SCX,
    CAP_READ_SYSTEM_STATE,
    CAP_RUN_BENCHMARK,
    CAP_VALIDATE_ARTIFACT,
    CAP_WRITE_ARTIFACT,
    ROLE_CRITIC,
    ROLE_CURATOR,
    ROLE_EXECUTOR,
    ROLE_GUARD,
    ROLE_POLICY,
    ROLE_PROFILER,
    AgentRun,
    RuntimeReplayExecutor,
    stable_json,
)
from agent_services import run_lock


SCHEMA_VERSION = "lockagent.runtime_api_adapter.v0.1"
RESULT_NAME = "runtime_api_adapter_result.json"


ROLE_BY_HINT = [
    (ROLE_PROFILER, ("profile", "profiler", "ledger", "state", "observe")),
    (ROLE_POLICY, ("policy", "llm", "decision", "choose", "select")),
    (ROLE_CRITIC, ("critic", "critique", "validate", "attribution", "provenance")),
    (ROLE_GUARD, ("guard", "policy_engine", "preflight", "postflight", "approval")),
    (ROLE_EXECUTOR, ("execute", "executor", "scx", "benchmark", "scheduler", "sysbench")),
    (ROLE_CURATOR, ("curator", "skill", "evolution", "promote", "reject")),
]

DEFAULT_CAPABILITIES = {
    ROLE_PROFILER: [CAP_READ_SYSTEM_STATE, CAP_WRITE_ARTIFACT],
    ROLE_POLICY: [CAP_CALL_LLM, CAP_WRITE_ARTIFACT],
    ROLE_CRITIC: [CAP_VALIDATE_ARTIFACT, CAP_WRITE_ARTIFACT],
    ROLE_GUARD: [CAP_EVALUATE_POLICY, CAP_WRITE_ARTIFACT],
    ROLE_EXECUTOR: [CAP_RUN_BENCHMARK, CAP_WRITE_ARTIFACT],
    ROLE_CURATOR: [CAP_CURATE_SKILL],
}


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_json_arg(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def load_json_file(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def merge_payload(raw: str | None, path: Path | None) -> dict[str, Any]:
    payload = load_json_file(path)
    payload.update(load_json_arg(raw))
    return payload


def infer_role(name: str, explicit_role: str | None = None) -> str:
    if explicit_role:
        return explicit_role
    lowered = name.lower()
    for role, hints in ROLE_BY_HINT:
        if any(hint in lowered for hint in hints):
            return role
    return ROLE_POLICY


def default_capabilities_for(role: str, payload: dict[str, Any], *, name: str = "") -> list[str]:
    explicit = payload.get("required_capabilities")
    if isinstance(explicit, list):
        return [str(item) for item in explicit]
    capabilities = list(DEFAULT_CAPABILITIES.get(role, [CAP_WRITE_ARTIFACT]))
    command = payload.get("command")
    if role == ROLE_EXECUTOR and isinstance(command, list) and CAP_LOAD_SCX not in capabilities:
        if any("scx" in str(item) for item in command) or "scx" in name.lower():
            capabilities.append(CAP_LOAD_SCX)
    return capabilities


def task_payload(
    *,
    framework: str,
    invocation_type: str,
    payload: dict[str, Any],
    required_capabilities: list[str],
    input_artifacts: list[str],
    output_artifacts: list[str],
) -> dict[str, Any]:
    data = dict(payload)
    data.setdefault("required_capabilities", required_capabilities)
    data.setdefault("input_artifacts", input_artifacts)
    data.setdefault("output_artifacts", output_artifacts)
    data["framework_adapter"] = {
        "framework": framework,
        "invocation_type": invocation_type,
        "timestamp": timestamp(),
    }
    return data


def write_result(run: AgentRun, result: dict[str, Any]) -> dict[str, Any]:
    path = run.artifact_root / RESULT_NAME
    path.write_text(stable_json(result) + "\n", encoding="utf-8")
    run.store.register(path, artifact_type="runtime_api_adapter_result", producer="runtime_api_adapter")
    run.checkpoint()
    return result


def emit_adapter_event(
    run: AgentRun,
    *,
    framework: str,
    invocation_type: str,
    source: str,
    target: str,
    payload: dict[str, Any],
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    run.trace.emit(
        "runtime_api_adapter_invoked",
        {
            "run_id": run.run_id,
            "framework": framework,
            "invocation_type": invocation_type,
            "source": source,
            "target": target,
            "payload": payload,
        },
    )
    return run.event_bus.emit(
        run_id=run.run_id,
        event_type="runtime_api_adapter.invoke",
        source=source,
        target=target,
        artifacts=artifacts or [],
        payload={
            "framework": framework,
            "invocation_type": invocation_type,
            "payload": payload,
        },
    )


def submit_adapter_task(
    checkpoint: Path,
    *,
    framework: str,
    invocation_type: str,
    source: str,
    role: str,
    step_name: str,
    payload: dict[str, Any],
    task_id: str | None,
    priority: int,
) -> dict[str, Any]:
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        input_artifacts = [str(item) for item in payload.get("input_artifacts", [])] if isinstance(payload.get("input_artifacts"), list) else []
        output_artifacts = [str(item) for item in payload.get("output_artifacts", [])] if isinstance(payload.get("output_artifacts"), list) else []
        required = default_capabilities_for(role, payload, name=step_name)
        adapter_task_id = task_id or f"{framework}:{invocation_type}:{step_name}:{int(time.time() * 1000)}"
        task = run.submit_task(
            task_id=adapter_task_id,
            role=role,
            step_name=step_name,
            payload=task_payload(
                framework=framework,
                invocation_type=invocation_type,
                payload=payload,
                required_capabilities=required,
                input_artifacts=input_artifacts,
                output_artifacts=output_artifacts,
            ),
            priority=priority,
        )
        event = emit_adapter_event(
            run,
            framework=framework,
            invocation_type=invocation_type,
            source=source,
            target=role,
            payload={"task": task.to_dict(), "step_name": step_name},
            artifacts=input_artifacts,
        )
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "submitted",
            "framework": framework,
            "invocation_type": invocation_type,
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "task": task.to_dict(),
            "typed_event": event,
            "runtime_mapping": {
                "source": source,
                "target_role": role,
                "step_name": step_name,
                "durable_primitive": "AgentRun.submit_task",
            },
        }
        return write_result(run, result)


def openai_invoke(
    checkpoint: Path,
    *,
    tool_name: str,
    payload: dict[str, Any],
    role: str | None = None,
    task_id: str | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    target = infer_role(tool_name, role)
    return submit_adapter_task(
        checkpoint,
        framework="openai_agents",
        invocation_type="tool_invoke",
        source="openai_agents.tool_call",
        role=target,
        step_name=tool_name,
        payload=payload,
        task_id=task_id,
        priority=priority,
    )


def langgraph_resume(
    checkpoint: Path,
    *,
    from_step: str | None = None,
    all_from_step: bool = False,
    drain_queue: bool = False,
    dry_run: bool = False,
    cwd: Path | None = None,
    native_backend: bool = False,
) -> dict[str, Any]:
    if native_backend:
        from native_langgraph_backend import invoke_native_langgraph

        native = invoke_native_langgraph(
            checkpoint,
            from_step=from_step,
            all_from_step=all_from_step,
            drain_queue=drain_queue,
            dry_run_replay=dry_run,
            cwd=cwd,
        )
        run = AgentRun.load(checkpoint)
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": native.get("status"),
            "framework": "langgraph",
            "invocation_type": "native_resume",
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "native_backend": True,
            "native_langgraph": native,
            "resume": native.get("resume"),
            "runtime_mapping": {
                "source": "langgraph.StateGraph",
                "target_role": "RuntimeReplayExecutor",
                "durable_primitive": "native_langgraph_backend.invoke_native_langgraph+RuntimeReplayExecutor.replay",
            },
        }
        return write_result(run, result)

    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        event = emit_adapter_event(
            run,
            framework="langgraph",
            invocation_type="resume",
            source="langgraph.thread",
            target="RuntimeReplayExecutor",
            payload={
                "from_step": from_step,
                "all_from_step": all_from_step,
                "drain_queue": drain_queue,
                "dry_run": dry_run,
            },
        )
        replay = RuntimeReplayExecutor(checkpoint, cwd=cwd, dry_run=dry_run).replay(
            from_step=from_step,
            only_failed=not all_from_step,
            drain_queue=drain_queue,
        )
        # Reload because RuntimeReplayExecutor writes the checkpoint.
        run = AgentRun.load(checkpoint)
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": replay.get("status"),
            "framework": "langgraph",
            "invocation_type": "resume",
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "typed_event": event,
            "resume": replay,
            "runtime_mapping": {
                "source": "langgraph.thread",
                "target_role": "RuntimeReplayExecutor",
                "durable_primitive": "RuntimeReplayExecutor.replay",
            },
        }
        return write_result(run, result)


def autogen_send(
    checkpoint: Path,
    *,
    source: str,
    target: str,
    message: dict[str, Any],
    task_id: str | None = None,
    priority: int = 100,
    enqueue: bool = True,
) -> dict[str, Any]:
    role = infer_role(target, target if target in {ROLE_PROFILER, ROLE_POLICY, ROLE_CRITIC, ROLE_GUARD, ROLE_EXECUTOR, ROLE_CURATOR} else None)
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        event = emit_adapter_event(
            run,
            framework="autogen_core",
            invocation_type="message_send",
            source=source,
            target=role,
            payload={"message": message, "enqueue": enqueue},
            artifacts=[str(item) for item in message.get("artifacts", [])] if isinstance(message.get("artifacts"), list) else [],
        )
        task = None
        if enqueue:
            step_name = str(message.get("step_name") or message.get("type") or "autogen_message")
            required = default_capabilities_for(role, message, name=step_name)
            task = run.submit_task(
                task_id=task_id or f"autogen:{source}:{role}:{int(time.time() * 1000)}",
                role=role,
                step_name=step_name,
                payload=task_payload(
                    framework="autogen_core",
                    invocation_type="message_send",
                    payload=message,
                    required_capabilities=required,
                    input_artifacts=[str(item) for item in message.get("input_artifacts", [])] if isinstance(message.get("input_artifacts"), list) else [],
                    output_artifacts=[str(item) for item in message.get("output_artifacts", [])] if isinstance(message.get("output_artifacts"), list) else [],
                ),
                priority=priority,
            )
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "submitted" if task else "delivered",
            "framework": "autogen_core",
            "invocation_type": "message_send",
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "typed_event": event,
            "task": task.to_dict() if task else None,
            "runtime_mapping": {
                "source": source,
                "target_role": role,
                "durable_primitive": "TypedEventBus.emit+AgentRun.submit_task" if task else "TypedEventBus.emit",
            },
        }
        return write_result(run, result)


def crewai_kickoff(
    checkpoint: Path,
    *,
    task: str,
    payload: dict[str, Any],
    role: str | None = None,
    task_id: str | None = None,
    priority: int = 100,
) -> dict[str, Any]:
    target = infer_role(task, role)
    enriched = {**payload, "crew_task": task}
    return submit_adapter_task(
        checkpoint,
        framework="crewai",
        invocation_type="kickoff",
        source="crewai.crew",
        role=target,
        step_name=task,
        payload=enriched,
        task_id=task_id,
        priority=priority,
    )


def invoke_from_body(body: dict[str, Any]) -> dict[str, Any]:
    checkpoint = Path(str(body["checkpoint"]))
    framework = str(body["framework"])
    payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
    if framework == "openai_agents":
        return openai_invoke(
            checkpoint,
            tool_name=str(body["tool_name"]),
            payload=payload,
            role=str(body["role"]) if body.get("role") else None,
            task_id=str(body["task_id"]) if body.get("task_id") else None,
            priority=int(body.get("priority", 100)),
        )
    if framework == "langgraph":
        return langgraph_resume(
            checkpoint,
            from_step=str(body["from_step"]) if body.get("from_step") else None,
            all_from_step=bool(body.get("all_from_step", False)),
            drain_queue=bool(body.get("drain_queue", False)),
            dry_run=bool(body.get("dry_run", False)),
            cwd=Path(str(body["cwd"])) if body.get("cwd") else None,
            native_backend=bool(body.get("native_backend", False)),
        )
    if framework == "autogen_core":
        message = body.get("message") if isinstance(body.get("message"), dict) else payload
        return autogen_send(
            checkpoint,
            source=str(body.get("source", "autogen.sender")),
            target=str(body.get("target", ROLE_POLICY)),
            message=message,
            task_id=str(body["task_id"]) if body.get("task_id") else None,
            priority=int(body.get("priority", 100)),
            enqueue=bool(body.get("enqueue", True)),
        )
    if framework == "crewai":
        return crewai_kickoff(
            checkpoint,
            task=str(body["task"]),
            payload=payload,
            role=str(body["role"]) if body.get("role") else None,
            task_id=str(body["task_id"]) if body.get("task_id") else None,
            priority=int(body.get("priority", 100)),
        )
    raise ValueError(f"unsupported framework: {framework}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    openai = sub.add_parser("openai-invoke")
    openai.add_argument("--checkpoint", type=Path, required=True)
    openai.add_argument("--tool-name", required=True)
    openai.add_argument("--payload", default="{}")
    openai.add_argument("--payload-file", type=Path)
    openai.add_argument("--role")
    openai.add_argument("--task-id")
    openai.add_argument("--priority", type=int, default=100)

    langgraph = sub.add_parser("langgraph-resume")
    langgraph.add_argument("--checkpoint", type=Path, required=True)
    langgraph.add_argument("--from-step")
    langgraph.add_argument("--all-from-step", action="store_true")
    langgraph.add_argument("--drain-queue", action="store_true")
    langgraph.add_argument("--dry-run", action="store_true")
    langgraph.add_argument("--cwd", type=Path)
    langgraph.add_argument("--native-backend", action="store_true")

    autogen = sub.add_parser("autogen-send")
    autogen.add_argument("--checkpoint", type=Path, required=True)
    autogen.add_argument("--source", required=True)
    autogen.add_argument("--target", required=True)
    autogen.add_argument("--message", default="{}")
    autogen.add_argument("--message-file", type=Path)
    autogen.add_argument("--task-id")
    autogen.add_argument("--priority", type=int, default=100)
    autogen.add_argument("--no-enqueue", action="store_true")

    crewai = sub.add_parser("crewai-kickoff")
    crewai.add_argument("--checkpoint", type=Path, required=True)
    crewai.add_argument("--task", required=True)
    crewai.add_argument("--payload", default="{}")
    crewai.add_argument("--payload-file", type=Path)
    crewai.add_argument("--role")
    crewai.add_argument("--task-id")
    crewai.add_argument("--priority", type=int, default=100)

    body = sub.add_parser("invoke-body")
    body.add_argument("--body", default="{}")
    body.add_argument("--body-file", type=Path)

    args = ap.parse_args(argv)
    if args.cmd == "openai-invoke":
        result = openai_invoke(
            args.checkpoint,
            tool_name=args.tool_name,
            payload=merge_payload(args.payload, args.payload_file),
            role=args.role,
            task_id=args.task_id,
            priority=args.priority,
        )
    elif args.cmd == "langgraph-resume":
        result = langgraph_resume(
            args.checkpoint,
            from_step=args.from_step,
            all_from_step=args.all_from_step,
            drain_queue=args.drain_queue,
            dry_run=args.dry_run,
            cwd=args.cwd,
            native_backend=args.native_backend,
        )
    elif args.cmd == "autogen-send":
        result = autogen_send(
            args.checkpoint,
            source=args.source,
            target=args.target,
            message=merge_payload(args.message, args.message_file),
            task_id=args.task_id,
            priority=args.priority,
            enqueue=not args.no_enqueue,
        )
    elif args.cmd == "crewai-kickoff":
        result = crewai_kickoff(
            args.checkpoint,
            task=args.task,
            payload=merge_payload(args.payload, args.payload_file),
            role=args.role,
            task_id=args.task_id,
            priority=args.priority,
        )
    elif args.cmd == "invoke-body":
        result = invoke_from_body(merge_payload(args.body, args.body_file))
    else:
        raise AssertionError(args.cmd)
    print(stable_json(result))
    return 0 if result.get("status") in {"submitted", "delivered", "ok", "nothing_to_resume"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

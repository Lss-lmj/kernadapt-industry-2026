#!/usr/bin/env python3
"""Local checks for mainstream runtime API adapter calls."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RUNTIME_PATH = ROOT / "agent_runtime.py"
ADAPTER_PATH = ROOT / "runtime_api_adapter.py"

rt_spec = importlib.util.spec_from_file_location("agent_runtime", RUNTIME_PATH)
assert rt_spec and rt_spec.loader
runtime = importlib.util.module_from_spec(rt_spec)
sys.modules["agent_runtime"] = runtime
rt_spec.loader.exec_module(runtime)

adapter_spec = importlib.util.spec_from_file_location("runtime_api_adapter", ADAPTER_PATH)
assert adapter_spec and adapter_spec.loader
adapter = importlib.util.module_from_spec(adapter_spec)
sys.modules["runtime_api_adapter"] = adapter
adapter_spec.loader.exec_module(adapter)


CALLS: list[str] = []


class FakeCompiled:
    def __init__(self, graph: "FakeStateGraph") -> None:
        self.graph = graph

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        CALLS.append("invoke")
        current = dict(state)
        for name, func in self.graph.nodes.items():
            current = func(current)
        return current

    def get_state(self, config: dict | None = None) -> dict:
        CALLS.append("get_state")
        return {"values": {"config": config}}


class FakeStateGraph:
    def __init__(self, state_type: object) -> None:
        CALLS.append("StateGraph")
        self.nodes: dict[str, object] = {}
        self.edges: list[tuple[str, str]] = []

    def add_node(self, name: str, func: object) -> None:
        CALLS.append("add_node")
        self.nodes[name] = func

    def add_edge(self, source: str, target: str) -> None:
        CALLS.append("add_edge")
        self.edges.append((source, target))

    def compile(self, checkpointer: object | None = None) -> FakeCompiled:
        CALLS.append("compile")
        return FakeCompiled(self)


class FakeMemorySaver:
    def __init__(self) -> None:
        CALLS.append("MemorySaver")


def install_fake_langgraph() -> None:
    langgraph_pkg = types.ModuleType("langgraph")
    graph_mod = types.ModuleType("langgraph.graph")
    graph_mod.StateGraph = FakeStateGraph
    graph_mod.START = "__start__"
    graph_mod.END = "__end__"
    checkpoint_pkg = types.ModuleType("langgraph.checkpoint")
    memory_mod = types.ModuleType("langgraph.checkpoint.memory")
    memory_mod.MemorySaver = FakeMemorySaver
    sys.modules["langgraph"] = langgraph_pkg
    sys.modules["langgraph.graph"] = graph_mod
    sys.modules["langgraph.checkpoint"] = checkpoint_pkg
    sys.modules["langgraph.checkpoint.memory"] = memory_mod


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def typed_events(run_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (run_dir / "typed_event_bus.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        run = runtime.AgentRun(
            run_id="run_adapter",
            intent="online",
            artifact_root=base / "run_adapter",
            environment_fingerprint={"kernel": "test"},
        )
        checkpoint = run.checkpoint_path

        openai = adapter.openai_invoke(
            checkpoint,
            tool_name="execute_scx_benchmark",
            payload={
                "command": ["/bin/true"],
                "input_artifacts": ["decision.json"],
                "output_artifacts": ["execution/status.json"],
            },
            task_id="openai-task",
            priority=1,
        )
        assert openai["status"] == "submitted", openai
        assert openai["framework"] == "openai_agents", openai
        assert openai["task"]["role"] == runtime.ROLE_EXECUTOR, openai
        assert openai["task"]["payload"]["framework_adapter"]["framework"] == "openai_agents", openai
        assert "load_scx" in openai["task"]["payload"]["required_capabilities"], openai
        assert (run.artifact_root / "runtime_api_adapter_result.json").exists()

        after_openai = runtime.AgentRun.load(checkpoint)
        assert any(task.task_id == "openai-task" for task in after_openai.scheduler._queue), after_openai.scheduler._queue
        assert any(event["event_type"] == "runtime_api_adapter.invoke" for event in typed_events(run.artifact_root))

        autogen = adapter.autogen_send(
            checkpoint,
            source="ProfilerAgent",
            target="CriticAgent",
            message={
                "type": "critic_validate",
                "artifacts": ["profile.json", "decision.json"],
                "input_artifacts": ["profile.json"],
            },
            task_id="autogen-task",
            priority=2,
        )
        assert autogen["status"] == "submitted", autogen
        assert autogen["framework"] == "autogen_core", autogen
        assert autogen["task"]["role"] == runtime.ROLE_CRITIC, autogen
        assert autogen["runtime_mapping"]["durable_primitive"] == "TypedEventBus.emit+AgentRun.submit_task", autogen

        crewai = adapter.crewai_kickoff(
            checkpoint,
            task="curator_promote_skill",
            payload={"skill": "choose_locknest_for_hot_lock_oltp"},
            task_id="crewai-task",
            priority=3,
        )
        assert crewai["status"] == "submitted", crewai
        assert crewai["framework"] == "crewai", crewai
        assert crewai["task"]["role"] == runtime.ROLE_CURATOR, crewai
        assert crewai["task"]["payload"]["crew_task"] == "curator_promote_skill", crewai

        resume_run = runtime.AgentRun(
            run_id="run_adapter_resume",
            intent="online",
            artifact_root=base / "run_adapter_resume",
            environment_fingerprint={"kernel": "test"},
        )
        failed = resume_run.record_step(
            name="failed_exec",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        assert failed.status == "failed"
        resume = adapter.langgraph_resume(
            resume_run.checkpoint_path,
            from_step="failed_exec",
            all_from_step=True,
            drain_queue=False,
            dry_run=True,
        )
        assert resume["framework"] == "langgraph", resume
        assert resume["invocation_type"] == "resume", resume
        assert resume["status"] == "ok", resume
        assert resume["resume"]["replayed"][0]["step"] == "failed_exec", resume
        assert read_json(resume_run.artifact_root / "runtime_api_adapter_result.json")["runtime_mapping"]["durable_primitive"] == "RuntimeReplayExecutor.replay"

        native_resume_run = runtime.AgentRun(
            run_id="run_adapter_native_resume",
            intent="online",
            artifact_root=base / "run_adapter_native_resume",
            environment_fingerprint={"kernel": "test"},
        )
        native_resume_run.record_step(
            name="failed_native_exec",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        install_fake_langgraph()
        CALLS.clear()
        native_resume = adapter.langgraph_resume(
            native_resume_run.checkpoint_path,
            from_step="failed_native_exec",
            all_from_step=True,
            drain_queue=False,
            dry_run=True,
            native_backend=True,
        )
        assert native_resume["framework"] == "langgraph", native_resume
        assert native_resume["invocation_type"] == "native_resume", native_resume
        assert native_resume["native_backend"] is True, native_resume
        assert native_resume["native_langgraph"]["invoked"] is True, native_resume
        assert native_resume["native_langgraph"]["resumed"] is True, native_resume
        assert "invoke" in CALLS and "compile" in CALLS, CALLS

        body = adapter.invoke_from_body(
            {
                "checkpoint": str(checkpoint),
                "framework": "openai_agents",
                "tool_name": "policy_decision",
                "payload": {"input_artifacts": ["profile.json"]},
                "task_id": "body-task",
            }
        )
        assert body["status"] == "submitted", body
        assert body["task"]["role"] == runtime.ROLE_POLICY, body

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local checks for optional native LangGraph backend."""

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
BACKEND_PATH = ROOT / "native_langgraph_backend.py"

rt_spec = importlib.util.spec_from_file_location("agent_runtime", RUNTIME_PATH)
assert rt_spec and rt_spec.loader
runtime = importlib.util.module_from_spec(rt_spec)
sys.modules["agent_runtime"] = runtime
rt_spec.loader.exec_module(runtime)

backend_spec = importlib.util.spec_from_file_location("native_langgraph_backend", BACKEND_PATH)
assert backend_spec and backend_spec.loader
backend = importlib.util.module_from_spec(backend_spec)
sys.modules["native_langgraph_backend"] = backend
backend_spec.loader.exec_module(backend)


CALLS: list[str] = []
ADDED_NODE_NAMES: list[str] = []


class FakeCompiled:
    def __init__(self, graph: "FakeStateGraph") -> None:
        self.graph = graph

    def invoke(self, state: dict, config: dict | None = None) -> dict:
        CALLS.append("invoke")
        current = dict(state)
        for name, func in self.graph.nodes.items():
            if name in {self.graph.start, self.graph.end}:
                continue
            current = func(current)
        current["fake_config"] = config
        return current

    def get_state(self, config: dict | None = None) -> dict:
        CALLS.append("get_state")
        return {"values": {"config": config}, "next": []}


class FakeStateGraph:
    start = "__start__"
    end = "__end__"

    def __init__(self, state_type: object) -> None:
        CALLS.append("StateGraph")
        self.state_type = state_type
        self.nodes: dict[str, object] = {}
        self.edges: list[tuple[str, str]] = []

    def add_node(self, name: str, func: object) -> None:
        CALLS.append("add_node")
        if name in self.nodes:
            raise ValueError(f"Node `{name}` already present.")
        ADDED_NODE_NAMES.append(name)
        self.nodes[name] = func

    def add_edge(self, source: str, target: str) -> None:
        CALLS.append("add_edge")
        self.edges.append((source, target))

    def compile(self, checkpointer: object | None = None) -> FakeCompiled:
        CALLS.append("compile")
        assert checkpointer is not None
        return FakeCompiled(self)


class FakeMemorySaver:
    def __init__(self) -> None:
        CALLS.append("MemorySaver")


def install_fake_langgraph() -> None:
    langgraph_pkg = types.ModuleType("langgraph")
    graph_mod = types.ModuleType("langgraph.graph")
    graph_mod.StateGraph = FakeStateGraph
    graph_mod.START = FakeStateGraph.start
    graph_mod.END = FakeStateGraph.end
    checkpoint_pkg = types.ModuleType("langgraph.checkpoint")
    memory_mod = types.ModuleType("langgraph.checkpoint.memory")
    memory_mod.MemorySaver = FakeMemorySaver
    sys.modules["langgraph"] = langgraph_pkg
    sys.modules["langgraph.graph"] = graph_mod
    sys.modules["langgraph.checkpoint"] = checkpoint_pkg
    sys.modules["langgraph.checkpoint.memory"] = memory_mod


def remove_fake_langgraph() -> None:
    for name in ["langgraph.checkpoint.memory", "langgraph.checkpoint", "langgraph.graph", "langgraph"]:
        sys.modules.pop(name, None)


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        run = runtime.AgentRun(
            run_id="run_native_lg",
            intent="online",
            artifact_root=base / "run_native_lg",
            environment_fingerprint={"kernel": "test"},
            graph=runtime.ExecutionGraph(
                nodes=[
                    runtime.ROLE_PROFILER,
                    runtime.ROLE_CRITIC,
                    runtime.ROLE_EXECUTOR,
                    runtime.ROLE_CRITIC,
                ],
                edges=[
                    (runtime.ROLE_PROFILER, runtime.ROLE_CRITIC),
                    (runtime.ROLE_CRITIC, runtime.ROLE_EXECUTOR),
                    (runtime.ROLE_EXECUTOR, runtime.ROLE_CRITIC),
                ],
            ),
        )
        failed = run.record_step(
            name="failed_exec",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        assert failed.status == "failed"

        remove_fake_langgraph()
        unavailable = backend.invoke_native_langgraph(run.checkpoint_path, from_step="failed_exec", dry_run_replay=True)
        assert unavailable["status"] == "unavailable", unavailable
        assert unavailable["native_runtime_available"] is False, unavailable
        assert read_json(run.artifact_root / "native_langgraph_result.json")["status"] == "unavailable"

        install_fake_langgraph()
        CALLS.clear()
        ADDED_NODE_NAMES.clear()
        invoked = backend.invoke_native_langgraph(
            run.checkpoint_path,
            from_step="failed_exec",
            all_from_step=True,
            dry_run_replay=True,
        )
        assert invoked["status"] == "ok", invoked
        assert invoked["native_runtime_available"] is True, invoked
        assert invoked["backend_validation_source"] == "fake_tested", invoked
        assert invoked["graph_mode"] == "sequential_calibration", invoked
        assert invoked["invoked"] is True, invoked
        assert invoked["resumed"] is True, invoked
        assert invoked["state_observed"] is True, invoked
        for expected in ["StateGraph", "MemorySaver", "add_node", "add_edge", "compile", "invoke", "get_state"]:
            assert expected in CALLS, (expected, CALLS)
        critic_nodes = [name for name in ADDED_NODE_NAMES if name.endswith(runtime.ROLE_CRITIC)]
        assert critic_nodes == ["01_CriticAgent", "03_CriticAgent"], ADDED_NODE_NAMES
        assert len(ADDED_NODE_NAMES) == len(set(ADDED_NODE_NAMES)), ADDED_NODE_NAMES
        result_file = read_json(run.artifact_root / "native_langgraph_result.json")
        assert result_file["runtime_api_calls"][:5] == ["StateGraph", "add_node", "add_edge", "compile", "invoke"], result_file
        assert invoked["output_event_count"] == 4, invoked
        events = [
            json.loads(line)
            for line in (run.artifact_root / "typed_event_bus.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(event.get("event_type") == "native_langgraph.invoke" for event in events), events

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

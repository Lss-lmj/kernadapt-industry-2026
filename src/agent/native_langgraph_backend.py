#!/usr/bin/env python3
"""Optional native LangGraph backend for LockAgent checkpoints.

The default LockAgent runtime is dependency-free. This module is deliberately
optional: when LangGraph is installed it builds and invokes a real StateGraph;
otherwise it emits an explicit unavailable result instead of pretending that a
schema export is a runtime integration.
"""

from __future__ import annotations

import argparse
import importlib
import json
import time
from pathlib import Path
from typing import Any

from agent_runtime import AgentRun, RuntimeReplayExecutor, stable_json
from agent_services import run_lock


SCHEMA_VERSION = "lockagent.native_langgraph_backend.v0.1"
RESULT_NAME = "native_langgraph_result.json"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _import_langgraph() -> tuple[Any | None, Any | None, str | None]:
    try:
        graph_mod = importlib.import_module("langgraph.graph")
        checkpoint_mod = importlib.import_module("langgraph.checkpoint.memory")
    except Exception as exc:
        return None, None, f"{type(exc).__name__}: {exc}"
    return graph_mod, checkpoint_mod, None


def _backend_source(graph_mod: Any | None) -> str:
    if graph_mod is None:
        return "unavailable"
    module_file = getattr(graph_mod, "__file__", None)
    module_spec = getattr(graph_mod, "__spec__", None)
    if module_file or module_spec:
        return "real_langgraph"
    return "fake_tested"


def _load_state(run: AgentRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "intent": run.intent,
        "status": run.status,
        "checkpoint": str(run.checkpoint_path),
        "artifact_root": str(run.artifact_root),
        "environment_fingerprint": run.environment_fingerprint,
        "steps": [step.to_dict() for step in run.steps],
        "next": run.next_resumable_step_names(),
        "handoffs": run.graph.to_dict(),
        "native_langgraph_events": [],
    }


def _append_event(state: dict[str, Any], event: str, **attrs: Any) -> dict[str, Any]:
    events = list(state.get("native_langgraph_events", []))
    events.append({"timestamp": timestamp(), "event": event, **attrs})
    return {**state, "native_langgraph_events": events}


def _state_graph_class(graph_mod: Any) -> Any:
    return getattr(graph_mod, "StateGraph")


def _start_symbol(graph_mod: Any) -> str:
    return getattr(graph_mod, "START", "__start__")


def _end_symbol(graph_mod: Any) -> str:
    return getattr(graph_mod, "END", "__end__")


def _memory_saver(checkpoint_mod: Any) -> Any:
    saver_cls = getattr(checkpoint_mod, "MemorySaver", None)
    if saver_cls is None:
        saver_cls = getattr(checkpoint_mod, "InMemorySaver", None)
    if saver_cls is None:
        raise RuntimeError("langgraph checkpoint module does not expose MemorySaver/InMemorySaver")
    return saver_cls()


def _graph_edges(run: AgentRun) -> list[tuple[str, str]]:
    edges: list[tuple[str, str]] = []
    for edge in run.graph.edges:
        if isinstance(edge, dict):
            source = edge.get("from")
            target = edge.get("to")
        else:
            try:
                source, target = edge
            except Exception:
                continue
        if source is not None and target is not None:
            edges.append((str(source), str(target)))
    return edges


def _unique_role_nodes(roles: list[str]) -> list[dict[str, Any]]:
    """Return LangGraph-safe node IDs while preserving LockAgent role names."""

    seen: dict[str, int] = {}
    nodes: list[dict[str, Any]] = []
    for index, role in enumerate(roles):
        seen[role] = seen.get(role, 0) + 1
        nodes.append(
            {
                "id": f"{index:02d}_{role}",
                "role": role,
                "role_occurrence": seen[role],
            }
        )
    return nodes


def _build_graph(run: AgentRun, graph_mod: Any) -> Any:
    state_graph = _state_graph_class(graph_mod)(dict)
    roles = run.graph.nodes or ["LockAgent"]
    nodes = _unique_role_nodes(roles)

    def make_node(role: str, node_id: str, role_occurrence: int):
        def node(state: dict[str, Any]) -> dict[str, Any]:
            role_steps = [
                step
                for step in state.get("steps", [])
                if isinstance(step, dict) and step.get("role") == role
            ]
            return _append_event(
                state,
                "role_invoked",
                node_id=node_id,
                role=role,
                role_occurrence=role_occurrence,
                step_count=len(role_steps),
            )

        return node

    for item in nodes:
        state_graph.add_node(item["id"], make_node(item["role"], item["id"], item["role_occurrence"]))

    # LangGraph's default root-state channel rejects multiple concurrent
    # writes. LockAgent's native execution graph can contain branches and a
    # CriticAgent revisit, so this optional backend uses a sequential
    # calibration graph while preserving the native graph in the state payload.
    start = _start_symbol(graph_mod)
    end = _end_symbol(graph_mod)
    node_ids = [item["id"] for item in nodes]
    if node_ids:
        state_graph.add_edge(start, node_ids[0])
    for source, target in zip(node_ids, node_ids[1:]):
        state_graph.add_edge(source, target)
    if node_ids:
        state_graph.add_edge(node_ids[-1], end)
    return state_graph


def invoke_native_langgraph(
    checkpoint: Path,
    *,
    from_step: str | None = None,
    all_from_step: bool = False,
    drain_queue: bool = False,
    dry_run_replay: bool = True,
    cwd: Path | None = None,
) -> dict[str, Any]:
    graph_mod, checkpoint_mod, import_error = _import_langgraph()
    with run_lock(checkpoint):
        run = AgentRun.load(checkpoint)
        result_path = run.artifact_root / RESULT_NAME
        if import_error:
            result = {
                "schema_version": SCHEMA_VERSION,
                "status": "unavailable",
                "backend": "langgraph",
                "backend_validation_source": "unavailable",
                "native_runtime_available": False,
                "import_error": import_error,
                "checkpoint": str(checkpoint),
                "run_id": run.run_id,
                "invoked": False,
                "resumed": False,
                "state_observed": False,
                "message": "Install langgraph to enable native StateGraph execution.",
            }
            result_path.write_text(stable_json(result) + "\n", encoding="utf-8")
            run.store.register(result_path, artifact_type="native_langgraph_result", producer="native_langgraph_backend")
            run.checkpoint()
            return result

        state_graph = _build_graph(run, graph_mod)
        checkpointer = _memory_saver(checkpoint_mod)
        compiled = state_graph.compile(checkpointer=checkpointer)
        config = {"configurable": {"thread_id": run.run_id}}
        input_state = _load_state(run)
        output_state = compiled.invoke(input_state, config=config)
        observed_state: Any = None
        if hasattr(compiled, "get_state"):
            try:
                observed_state = compiled.get_state(config)
            except Exception as exc:
                observed_state = {"error": f"{type(exc).__name__}: {exc}"}

        replay = RuntimeReplayExecutor(checkpoint, cwd=cwd, dry_run=dry_run_replay).replay(
            from_step=from_step,
            only_failed=not all_from_step,
            drain_queue=drain_queue,
        )
        run = AgentRun.load(checkpoint)
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "ok" if replay.get("status") in {"ok", "nothing_to_resume"} else replay.get("status"),
            "backend": "langgraph",
            "backend_validation_source": _backend_source(graph_mod),
            "graph_mode": "sequential_calibration",
            "native_runtime_available": True,
            "checkpoint": str(checkpoint),
            "run_id": run.run_id,
            "invoked": True,
            "resumed": replay.get("status") in {"ok", "nothing_to_resume"},
            "state_observed": observed_state is not None,
            "thread_id": run.run_id,
            "input_next": input_state.get("next", []),
            "output_event_count": len(output_state.get("native_langgraph_events", [])) if isinstance(output_state, dict) else None,
            "state_observation_type": type(observed_state).__name__ if observed_state is not None else None,
            "resume": replay,
            "runtime_api_calls": [
                "StateGraph",
                "add_node",
                "add_edge",
                "compile",
                "invoke",
                "get_state" if observed_state is not None else "get_state_unavailable",
            ],
        }
        result_path.write_text(stable_json(result) + "\n", encoding="utf-8")
        run.store.register(result_path, artifact_type="native_langgraph_result", producer="native_langgraph_backend")
        run.event_bus.emit(
            run_id=run.run_id,
            event_type="native_langgraph.invoke",
            source="native_langgraph_backend",
            target="langgraph.StateGraph",
            artifacts=[str(result_path)],
            payload={
                "status": result["status"],
                "invoked": result["invoked"],
                "resumed": result["resumed"],
                "state_observed": result["state_observed"],
            },
        )
        run.checkpoint()
        return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--from-step")
    ap.add_argument("--all-from-step", action="store_true")
    ap.add_argument("--drain-queue", action="store_true")
    ap.add_argument("--dry-run-replay", action="store_true")
    ap.add_argument("--cwd", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    result = invoke_native_langgraph(
        args.checkpoint,
        from_step=args.from_step,
        all_from_step=args.all_from_step,
        drain_queue=args.drain_queue,
        dry_run_replay=args.dry_run_replay,
        cwd=args.cwd,
    )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(stable_json(result) + "\n", encoding="utf-8")
    print(stable_json(result))
    return 0 if result.get("status") in {"ok", "nothing_to_resume", "unavailable"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export LockAgent runs into mainstream agent-runtime adapter schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent_runtime import AgentRun, SCHEMA_VERSION, stable_json


ADAPTER_SCHEMA = "lockagent.framework_adapters.v0.1"


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def typed_events(root: Path) -> list[dict[str, Any]]:
    data = load_json(root / "typed_event_bus.json")
    events = data.get("events")
    if isinstance(events, list):
        return [event for event in events if isinstance(event, dict)]
    return load_jsonl(root / "typed_event_bus.jsonl")


def step_records(run: AgentRun) -> list[dict[str, Any]]:
    return [step.to_dict() for step in run.steps]


def build_openai_adapter(run: AgentRun, events: list[dict[str, Any]]) -> dict[str, Any]:
    tools = []
    handoffs = []
    spans = []
    for index, step in enumerate(step_records(run)):
        span_id = f"span:{index}:{step['name']}"
        spans.append(
            {
                "trace_id": run.run_id,
                "span_id": span_id,
                "parent_id": run.run_id,
                "type": "agent_step",
                "name": step["name"],
                "metadata": {
                    "agent": step["role"],
                    "status": step["status"],
                    "input_artifacts": step.get("input_artifacts", []),
                    "output_artifacts": step.get("output_artifacts", []),
                    "capabilities": step.get("required_capabilities", []),
                },
            }
        )
        tools.append(
            {
                "name": step["name"],
                "agent": step["role"],
                "capabilities": step.get("required_capabilities", []),
                "inputs": step.get("input_artifacts", []),
                "outputs": step.get("output_artifacts", []),
            }
        )
    for event in events:
        if event.get("event_type") == "handoff":
            handoffs.append(
                {
                    "source_agent": event.get("source"),
                    "target_agent": event.get("target"),
                    "input_items": event.get("artifacts", []),
                    "metadata": {
                        "reason": event.get("payload", {}).get("reason") or event.get("reason"),
                        "correlation_id": event.get("correlation_id"),
                    },
                }
            )
    return {
        "schema": "openai_agents_adapter_v0",
        "contract": {
            "trace_id": "required",
            "span_id": "required_per_span",
            "handoff_source_target": "required_per_handoff",
            "tool_io_artifacts": "required_per_tool",
        },
        "concepts": ["Agent", "Tool", "Handoff", "Trace", "Guardrail"],
        "agent": {"name": "LockAgent", "run_id": run.run_id, "status": run.status},
        "tools": tools,
        "handoffs": handoffs,
        "traces": {"trace_id": run.run_id, "spans": spans},
    }


def build_langgraph_adapter(run: AgentRun) -> dict[str, Any]:
    scheduler_data = load_json(run.artifact_root / "agent_scheduler.json")
    resume_plan = load_json(run.artifact_root / "resume_plan.json")
    observed_graph = load_json(run.artifact_root / "execution_graph_runtime.json")
    return {
        "schema": "langgraph_adapter_v0",
        "contract": {
            "thread_id": "required",
            "checkpoint": "required",
            "graph_nodes_edges": "required",
            "state_values": "required",
        },
        "concepts": ["StateGraph", "thread_id", "checkpoint", "interrupt", "durable_execution"],
        "thread_id": run.run_id,
        "state": {
            "values": {
                "intent": run.intent,
                "status": run.status,
                "environment_fingerprint": run.environment_fingerprint,
                "steps": step_records(run),
                "tasks": scheduler_data.get("tasks", []),
            },
            "next": run.next_resumable_step_names(),
            "checkpoint": str(run.checkpoint_path),
        },
        "graph": run.graph.to_dict(),
        "observed_graph": observed_graph,
        "interrupts": [
            step
            for step in step_records(run)
            if step.get("status") == "paused" or step.get("metadata", {}).get("requires_human_approval")
        ],
        "resume_graph": resume_plan.get("resume_graph", {}),
    }


def build_autogen_adapter(run: AgentRun, events: list[dict[str, Any]]) -> dict[str, Any]:
    messages = []
    subscriptions: dict[str, list[str]] = {node: run.graph.successors(node) for node in run.graph.nodes}
    for event in events:
        if event.get("event_type") == "handoff":
            messages.append(
                {
                    "type": "HandoffMessage",
                    "source": event.get("source"),
                    "target": event.get("target"),
                    "content": {
                        "artifacts": event.get("artifacts", []),
                        "payload": event.get("payload", {}),
                    },
                    "metadata": {"correlation_id": event.get("correlation_id")},
                }
            )
        elif event.get("event_type") in {"step_start", "step_end", "agent_task_submitted"}:
            messages.append(
                {
                    "type": str(event.get("event_type")),
                    "source": event.get("source") or "runtime",
                    "target": event.get("target") or "runtime",
                    "content": event.get("payload", event),
                }
            )
    return {
        "schema": "autogen_core_adapter_v0",
        "contract": {
            "runtime_agent_ids": "required",
            "topic_subscriptions": "required",
            "message_source_target": "required_per_message",
        },
        "concepts": ["AgentRuntime", "RoutedAgent", "Message", "TopicSubscription"],
        "runtime": {"run_id": run.run_id, "agent_ids": run.graph.nodes, "status": run.status},
        "subscriptions": subscriptions,
        "messages": messages,
    }


def build_crewai_adapter(run: AgentRun) -> dict[str, Any]:
    return {
        "schema": "crewai_adapter_v0",
        "contract": {
            "crew_agents": "required",
            "tasks_agent_expected_output": "required_per_task",
            "memory_paths": "required",
        },
        "concepts": ["Crew", "Agent", "Task", "Process", "Memory"],
        "crew": {
            "name": "LockAgent",
            "process": "typed_handoff_resource_control",
            "agents": [{"role": node, "goal": f"LockAgent {node} responsibility"} for node in run.graph.nodes],
        },
        "tasks": [
            {
                "description": step["name"],
                "agent": step["role"],
                "expected_output": step.get("output_artifacts", []),
                "context": step.get("input_artifacts", []),
                "status": step.get("status"),
            }
            for step in step_records(run)
        ],
        "memory": {
            "run_memory": str(run.memory.path),
            "global_memory_record": str(run.artifact_root / "global_memory_record.json"),
            "policy_memory_context": str(run.artifact_root / "memory_context.json"),
        },
    }


def _check(name: str, passed: bool, detail: str | None = None) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def validate_contracts(bundle: dict[str, Any]) -> dict[str, Any]:
    openai = bundle["openai_agents"]
    langgraph = bundle["langgraph"]
    autogen = bundle["autogen_core"]
    crewai = bundle["crewai"]
    graph_nodes = set(langgraph.get("graph", {}).get("nodes", []))
    graph_edges = {
        (edge.get("from"), edge.get("to"))
        for edge in langgraph.get("graph", {}).get("edges", [])
        if isinstance(edge, dict)
    }
    observed_handoff_edges = {
        (edge.get("from"), edge.get("to"))
        for edge in langgraph.get("observed_graph", {}).get("observed_handoff_edges", [])
        if isinstance(edge, dict)
    }
    observed_control_role_edges = {
        (source.get("role"), target.get("role"))
        for source, target in zip(
            langgraph.get("observed_graph", {}).get("observed_nodes", []),
            langgraph.get("observed_graph", {}).get("observed_nodes", [])[1:],
        )
        if isinstance(source, dict) and isinstance(target, dict)
    }
    role_sequence = [
        item
        for item in langgraph.get("graph", {}).get("role_sequence", [])
        if isinstance(item, str)
    ]
    role_sequence_edges = {
        (source, target)
        for source, target in zip(role_sequence, role_sequence[1:])
    }
    openai_spans = openai.get("traces", {}).get("spans", [])
    openai_handoffs = openai.get("handoffs", [])
    openai_tools = openai.get("tools", [])
    autogen_messages = autogen.get("messages", [])
    autogen_subscriptions = autogen.get("subscriptions", {})
    crew_agents = crewai.get("crew", {}).get("agents", [])
    crew_tasks = crewai.get("tasks", [])
    checks: list[dict[str, Any]] = []

    def handoff_matches_graph_or_runtime_control(handoff: dict[str, Any]) -> bool:
        source = handoff.get("source_agent")
        target = handoff.get("target_agent")
        if source == "START" and target in graph_nodes:
            return True
        if source == target and source in graph_nodes:
            return True
        if source in graph_nodes and target == "runtime":
            return True
        return (
            (source, target) in graph_edges
            or (source, target) in role_sequence_edges
            or (source, target) in observed_handoff_edges
            or (source, target) in observed_control_role_edges
        )

    mismatched_handoffs = [
        f"{handoff.get('source_agent')}->{handoff.get('target_agent')}"
        for handoff in openai_handoffs
        if isinstance(handoff, dict) and not handoff_matches_graph_or_runtime_control(handoff)
    ]

    checks.extend(
        [
            _check("openai.trace_id", bool(openai.get("traces", {}).get("trace_id"))),
            _check(
                "openai.spans_have_ids",
                bool(openai_spans)
                and all(span.get("trace_id") and span.get("span_id") and span.get("name") for span in openai_spans),
            ),
            _check(
                "openai.tools_have_io",
                bool(openai_tools)
                and all("inputs" in tool and "outputs" in tool and tool.get("agent") for tool in openai_tools),
            ),
            _check(
                "openai.handoffs_have_source_target",
                bool(openai_handoffs)
                and all(handoff.get("source_agent") and handoff.get("target_agent") for handoff in openai_handoffs),
            ),
            _check(
                "openai.handoffs_match_execution_graph",
                bool(openai_handoffs)
                and not mismatched_handoffs,
                ",".join(mismatched_handoffs) if mismatched_handoffs else None,
            ),
        ]
    )
    checks.extend(
        [
            _check("langgraph.thread_id", bool(langgraph.get("thread_id"))),
            _check("langgraph.checkpoint", bool(langgraph.get("state", {}).get("checkpoint"))),
            _check("langgraph.state_values", isinstance(langgraph.get("state", {}).get("values"), dict)),
            _check("langgraph.graph_nodes", bool(graph_nodes)),
            _check("langgraph.graph_edges", bool(graph_edges)),
            _check(
                "langgraph.observed_handoff_edges",
                isinstance(langgraph.get("observed_graph", {}).get("observed_handoff_edges"), list)
                and bool(langgraph.get("observed_graph", {}).get("observed_handoff_edges")),
            ),
            _check("langgraph.resume_graph_present", isinstance(langgraph.get("resume_graph", {}), dict)),
        ]
    )
    checks.extend(
        [
            _check("autogen.runtime_agent_ids", bool(autogen.get("runtime", {}).get("agent_ids"))),
            _check(
                "autogen.subscriptions_cover_graph_nodes",
                bool(autogen_subscriptions)
                and set(autogen_subscriptions.keys()).issubset(graph_nodes)
                and graph_nodes.issubset(set(autogen_subscriptions.keys())),
            ),
            _check(
                "autogen.messages_have_source_target",
                bool(autogen_messages)
                and all(message.get("source") and message.get("target") for message in autogen_messages),
            ),
        ]
    )
    crew_agent_roles = {agent.get("role") for agent in crew_agents if isinstance(agent, dict)}
    checks.extend(
        [
            _check("crewai.crew_agents", bool(crew_agent_roles) and graph_nodes.issubset(crew_agent_roles)),
            _check(
                "crewai.tasks_have_agent_and_output",
                bool(crew_tasks)
                and all(task.get("agent") and "expected_output" in task for task in crew_tasks),
            ),
            _check(
                "crewai.memory_paths",
                bool(crewai.get("memory", {}).get("run_memory"))
                and bool(crewai.get("memory", {}).get("global_memory_record"))
                and bool(crewai.get("memory", {}).get("policy_memory_context")),
            ),
        ]
    )
    by_framework = {
        "openai_agents": [check for check in checks if check["name"].startswith("openai.")],
        "langgraph": [check for check in checks if check["name"].startswith("langgraph.")],
        "autogen_core": [check for check in checks if check["name"].startswith("autogen.")],
        "crewai": [check for check in checks if check["name"].startswith("crewai.")],
    }
    framework_summary = {
        name: {
            "passed": all(check["passed"] for check in items),
            "passed_count": sum(1 for check in items if check["passed"]),
            "check_count": len(items),
            "failed": [check["name"] for check in items if not check["passed"]],
        }
        for name, items in by_framework.items()
    }
    passed = sum(1 for check in checks if check["passed"])
    return {
        "schema_version": "lockagent.framework_contracts.v0.1",
        "status": "aligned" if passed == len(checks) else "partial",
        "contract_score": round(passed / max(len(checks), 1), 3),
        "check_count": len(checks),
        "passed_count": passed,
        "failed": [check for check in checks if not check["passed"]],
        "checks": checks,
        "frameworks": framework_summary,
    }


def build_coverage(bundle: dict[str, Any]) -> dict[str, Any]:
    openai = bundle["openai_agents"]
    langgraph = bundle["langgraph"]
    autogen = bundle["autogen_core"]
    crewai = bundle["crewai"]
    checks = {
        "openai_agents_handoff": bool(openai.get("handoffs")),
        "openai_agents_spans": bool(openai.get("traces", {}).get("spans")),
        "langgraph_checkpoint": bool(langgraph.get("state", {}).get("checkpoint")),
        "langgraph_graph": bool(langgraph.get("graph", {}).get("nodes")) and bool(langgraph.get("graph", {}).get("edges")),
        "autogen_messages": bool(autogen.get("messages")),
        "autogen_subscriptions": bool(autogen.get("subscriptions")),
        "crewai_tasks": bool(crewai.get("tasks")),
        "crewai_memory": bool(crewai.get("memory")),
    }
    contracts = validate_contracts(bundle)
    passed = sum(1 for value in checks.values() if value)
    contract_passed = contracts.get("status") == "aligned"
    return {
        "schema_version": ADAPTER_SCHEMA,
        "status": "aligned" if passed == len(checks) and contract_passed else "partial",
        "coverage_score": round(passed / max(len(checks), 1), 3),
        "contract_score": contracts.get("contract_score", 0),
        "checks": checks,
        "contract_status": contracts.get("status"),
        "concept_count": {
            "openai_agents": len(openai.get("concepts", [])),
            "langgraph": len(langgraph.get("concepts", [])),
            "autogen_core": len(autogen.get("concepts", [])),
            "crewai": len(crewai.get("concepts", [])),
        },
    }


def build_bundle(checkpoint: Path) -> dict[str, Any]:
    run = AgentRun.load(checkpoint)
    events = typed_events(run.artifact_root)
    bundle = {
        "schema_version": ADAPTER_SCHEMA,
        "run_id": run.run_id,
        "checkpoint": str(checkpoint),
        "openai_agents": build_openai_adapter(run, events),
        "langgraph": build_langgraph_adapter(run),
        "autogen_core": build_autogen_adapter(run, events),
        "crewai": build_crewai_adapter(run),
    }
    bundle["framework_contracts"] = validate_contracts(bundle)
    bundle["coverage"] = build_coverage(bundle)
    return bundle


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    bundle = build_bundle(args.checkpoint)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(stable_json(bundle) + "\n", encoding="utf-8")
    print(stable_json(bundle))
    return 0 if bundle.get("coverage", {}).get("status") == "aligned" else 2


if __name__ == "__main__":
    raise SystemExit(main())

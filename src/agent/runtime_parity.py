#!/usr/bin/env python3
"""Compare native LockAgent runtime capabilities against the LangGraph backend.

This is not a benchmark. It is a capability parity audit: native LockAgent
runtime must cover the durable/stateful graph-runtime features we rely on from
LangGraph, and it should exceed the generic backend on LockAgent-specific OS
resource-control safety, provenance, and skill-evolution requirements.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lockagent.runtime_parity.v0.1"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


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


def native_langgraph_invocation_class(native_langgraph: dict[str, Any]) -> str:
    if not native_langgraph:
        return "missing"
    source = native_langgraph.get("backend_validation_source")
    status = native_langgraph.get("status")
    invoked = bool(native_langgraph.get("invoked"))
    if source == "real_langgraph" and invoked:
        return "real_backend_invoked"
    if source == "real_langgraph":
        return "real_backend_available_not_invoked"
    if source == "fake_tested":
        return "fake_tested"
    if source == "unavailable" or status == "unavailable":
        return "unavailable"
    return "unknown"


def check(name: str, passed: bool, native_evidence: str, langgraph_evidence: str = "", advantage: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "class": "advantage" if advantage else "parity",
        "native_evidence": native_evidence,
        "langgraph_evidence": langgraph_evidence,
    }


def evaluate_run(run_dir: Path) -> dict[str, Any]:
    agent_run = load_json(run_dir / "agent_run.json")
    langgraph_state = load_json(run_dir / "langgraph_state.json")
    native_langgraph = load_json(run_dir / "native_langgraph_result.json")
    framework_bundle = load_json(run_dir / "framework_adapter_bundle.json")
    trace_alignment = load_json(run_dir / "trace_schema_alignment.json")
    resume_plan = load_json(run_dir / "resume_plan.json")
    resume_audit = load_json(run_dir / "resume_audit.json")
    durable_summary = load_json(run_dir / "durable_runtime_summary.json")
    execution_graph_runtime = load_json(run_dir / "execution_graph_runtime.json")
    graph_scheduler_plan = load_json(run_dir / "graph_scheduler_plan.json")
    typed_bus = load_json(run_dir / "typed_event_bus.json")
    typed_events = load_jsonl(run_dir / "typed_event_bus.jsonl") or typed_bus.get("events", [])
    artifact_store = load_json(run_dir / "artifact_store.json")
    provenance = load_json(run_dir / "provenance.json")
    causal = load_json(run_dir / "causal_attribution.json")
    policy = load_json(run_dir / "runtime_policy_preflight.json")
    guard_pre = load_json(run_dir / "execution" / "guard_preflight.json")
    guard_post = load_json(run_dir / "execution" / "guard_postflight.json")
    skill_curation = load_json(run_dir / "skill_curation.json")
    skill_update = load_json(run_dir / "skill_repository_update.json")
    skill_evolution = load_json(run_dir / "skill_evolution_application.json")
    system_evolution = load_json(run_dir / "system_evolution_state.json")
    override_application = load_json(run_dir / "systemic_override_application.json")
    human_gate = load_json(run_dir / "human_gate.json")
    worker_pool = load_json(run_dir / "worker_pool_result.json")

    graph = provenance.get("dependency_graph", {})
    execution_graph = agent_run.get("graph", {}) if isinstance(agent_run.get("graph"), dict) else {}
    observed_graph_summary = execution_graph_runtime.get("summary", {}) if isinstance(execution_graph_runtime.get("summary"), dict) else {}
    graph_scheduler_summary = graph_scheduler_plan.get("summary", {}) if isinstance(graph_scheduler_plan.get("summary"), dict) else {}
    execution_graph_step_nodes = execution_graph.get("step_nodes", []) if isinstance(execution_graph.get("step_nodes"), list) else []
    execution_graph_step_edges = execution_graph.get("step_edges", []) if isinstance(execution_graph.get("step_edges"), list) else []
    declared_step_pairs = {
        (str(node.get("name")), str(node.get("role")))
        for node in execution_graph_step_nodes
        if isinstance(node, dict) and node.get("name") and node.get("role")
    }
    executed_steps = agent_run.get("steps", []) if isinstance(agent_run.get("steps"), list) else []
    mapped_executed_steps = [
        step
        for step in executed_steps
        if isinstance(step, dict)
        and (
            (str(step.get("name")), str(step.get("role"))) in declared_step_pairs
            or step.get("metadata", {}).get("execution_graph_node")
        )
    ]
    runtime_cross_reference = graph.get("runtime_cross_reference", {}) if isinstance(graph.get("runtime_cross_reference"), dict) else {}
    runtime_cross_reference_edges = graph.get("runtime_cross_reference_edges", []) if isinstance(graph.get("runtime_cross_reference_edges"), list) else []
    artifacts = artifact_store.get("artifacts", []) if isinstance(artifact_store.get("artifacts"), list) else []
    typed_artifacts = [
        item
        for item in artifacts
        if isinstance(item, dict)
        and item.get("artifact_role") in {"input", "output"}
        and item.get("step_name")
        and ("required" in item)
        and (item.get("producer") or item.get("consumer"))
    ]
    resume_graph = resume_plan.get("resume_graph", {}) if isinstance(resume_plan.get("resume_graph"), dict) else {}
    resume_dependency_edges = resume_graph.get("dependency_edges", []) if isinstance(resume_graph.get("dependency_edges"), list) else []
    resume_execution_order = resume_plan.get("execution_order", [])
    resume_dependency_order = resume_plan.get("dependency_order", [])
    resume_artifact_records = resume_plan.get("artifact_records", [])
    resumable_steps = [
        step
        for step in executed_steps
        if isinstance(step, dict) and step.get("status") in {"failed", "blocked", "running", "paused"}
    ]
    queued_command_tasks = resume_plan.get("queued_command_tasks", []) if isinstance(resume_plan.get("queued_command_tasks"), list) else []
    resume_required = bool(resumable_steps or queued_command_tasks)
    empty_resume_recorded = (
        bool(resume_plan)
        and not resume_required
        and isinstance(resume_execution_order, list)
        and isinstance(resume_dependency_order, list)
        and isinstance(resume_artifact_records, list)
        and resume_audit.get("status") in {"nothing_to_resume", "ok"}
    )
    causal_summary = causal.get("summary", {})
    framework_contracts = framework_bundle.get("framework_contracts", {})
    alignment_checks = trace_alignment.get("checks", {})
    policy_checks = []
    for doc in (policy, guard_pre, guard_post):
        policy_checks.extend(doc.get("policy_engine", {}).get("checks", []) if isinstance(doc, dict) else [])

    parity_checks = [
        check(
            "state_graph_schema",
            bool(langgraph_state.get("thread_id") and langgraph_state.get("metadata", {}).get("graph")),
            "langgraph_state.json exported from native runtime",
            "LangGraph-compatible state graph shape exists",
        ),
        check(
            "checkpoint_persistence",
            bool((run_dir / "durable_runtime.db").exists() and durable_summary.get("checkpoint_count", 0) >= 1),
            "durable_runtime.db + durable_runtime_summary checkpoint_count",
            str(langgraph_state.get("checkpoint", "")),
        ),
        check(
            "step_level_resume",
            bool(resume_plan.get("resume_graph", {}).get("nodes") and resume_audit.get("dependency_graph"))
            or empty_resume_recorded,
            "resume_plan.json + resume_audit.json dependency graph",
            "LangGraph backend exposes checkpoint/resume target",
        ),
        check(
            "typed_handoff_events",
            any(isinstance(event, dict) and event.get("event_type") == "handoff" for event in typed_events),
            "typed_event_bus contains handoff events",
            "LangGraph edges preserve role topology",
        ),
        check(
            "step_level_execution_graph",
            bool(execution_graph_step_nodes)
            and bool(execution_graph_step_edges)
            and (not executed_steps or len(mapped_executed_steps) == len(executed_steps)),
            "agent_run.graph exposes step_nodes/step_edges and executed steps map to declared graph nodes",
            "Graph runtimes model runnable nodes, not just role labels",
        ),
        check(
            "observed_execution_graph",
            bool(execution_graph_runtime)
            and bool(execution_graph_runtime.get("observed_nodes"))
            and bool(execution_graph_runtime.get("observed_control_edges") or len(executed_steps) <= 1)
            and float(observed_graph_summary.get("declared_step_coverage") or 0) > 0,
            "execution_graph_runtime.json records observed nodes, control edges, artifact dependencies, and declared coverage",
            "Graph runtimes expose observed execution topology",
        ),
        check(
            "graph_scheduler_readiness",
            bool(graph_scheduler_plan)
            and bool(graph_scheduler_plan.get("declared_steps"))
            and isinstance(graph_scheduler_plan.get("next_runnable_steps", []), list)
            and isinstance(graph_scheduler_plan.get("artifact_dependency_edges", []), list)
            and isinstance(graph_scheduler_plan.get("failed_step_replay_candidates", []), list)
            and int(graph_scheduler_summary.get("declared_step_count") or 0) > 0
            and int(graph_scheduler_summary.get("observed_declared_step_count") or 0) <= int(graph_scheduler_summary.get("declared_step_count") or 0),
            "graph_scheduler_plan.json records declared step status, artifact dependency edges, failed-step replay candidates, next runnable steps, and readiness counts",
            "Graph runtimes expose runnable/waiting node state, data dependencies, and replay candidates",
        ),
        check(
            "typed_artifact_handoff_records",
            bool(typed_artifacts),
            "artifact_store.json has input/output records with step/producer/consumer/required metadata",
            "Framework handoffs carry typed payload metadata",
        ),
        check(
            "resume_execution_dependency_order",
            (
                isinstance(resume_execution_order, list)
                and bool(resume_execution_order)
                and isinstance(resume_dependency_order, list)
                and bool(resume_dependency_order)
                and bool(resume_artifact_records)
                and bool(resume_dependency_edges)
            )
            or empty_resume_recorded,
            "resume_plan.json exposes execution_order, dependency_order, and artifact_records",
            "Graph runtimes distinguish execution order from data dependencies",
        ),
        check(
            "provenance_runtime_cross_reference",
            bool(runtime_cross_reference.get("runtime_step_nodes"))
            and bool(runtime_cross_reference.get("artifact_record_nodes"))
            and bool(runtime_cross_reference_edges),
            "provenance.json links runtime_step nodes, artifact_record nodes, and semantic provenance nodes",
            "Graph runtime and trace/provenance nodes can be cross-referenced",
        ),
        check(
            "hitl_pause_resume",
            bool(human_gate) or bool(alignment_checks.get("durable_execution", {}).get("passed")),
            "HumanGate artifact or durable execution alignment",
            "LangGraph supports interrupt/resume semantics",
        ),
        check(
            "framework_trace_contract",
            framework_contracts.get("frameworks", {}).get("langgraph", {}).get("passed") is True,
            "framework_adapter_bundle.framework_contracts.langgraph",
            "LangGraph contract checks",
        ),
        check(
            "native_langgraph_backend_status_recorded",
            bool(native_langgraph) and native_langgraph.get("status") in {"ok", "nothing_to_resume", "unavailable"},
            "native_langgraph_result.json records ok/nothing_to_resume/unavailable",
            "Optional native StateGraph backend status is explicit",
        ),
    ]

    advantage_checks = [
        check(
            "capability_guardrails",
            any(isinstance(item, dict) and item.get("name") for item in policy_checks),
            "PolicyEngine checks in runtime_policy_preflight/guard artifacts",
            "Generic LangGraph does not enforce scx capability policy by itself",
            advantage=True,
        ),
        check(
            "os_side_effect_allowlist",
            any(isinstance(item, dict) and "scheduler" in str(item.get("name", "")).lower() for item in policy_checks)
            or (guard_pre.get("verdict") in {"allow", "block"}),
            "RuntimeGuard scheduler policy verdict",
            "Scheduler/root side effects are LockAgent-specific",
            advantage=True,
        ),
        check(
            "artifact_store_with_hashes",
            bool(artifact_store.get("artifacts")),
            "artifact_store.json records typed artifacts/checksums",
            "LangGraph checkpoints do not by themselves hash experiment artifacts",
            advantage=True,
        ),
        check(
            "provenance_dependency_graph",
            bool(graph.get("dependency_edges") and graph.get("execution_edges")),
            "provenance.json dependency_graph execution/dependency edges",
            "LangGraph state graph is execution topology, not evidence provenance",
            advantage=True,
        ),
        check(
            "regression_blame_graph",
            bool(graph.get("regression_blame_chains"))
            or bool(causal_summary.get("bad_profile_bad_skill_bad_scheduler_regression_path")),
            "regression_blame_chains or causal_attribution summary",
            "GRADE/AgentTrace-style blame graph is LockAgent-specific",
            advantage=True,
        ),
        check(
            "skill_lifecycle",
            bool(skill_curation) and (bool(skill_update) or bool(skill_evolution)),
            "skill_curation plus repository/evolution update artifacts",
            "Scheduler skill promotion/rejection is LockAgent-specific",
            advantage=True,
        ),
        check(
            "systemic_meta_evolution",
            bool(system_evolution) and bool(override_application),
            "system_evolution_state + systemic_override_application",
            "System architecture adaptation is LockAgent-specific",
            advantage=True,
        ),
        check(
            "worker_lease_runtime",
            durable_summary.get("task_count", 0) > 0 or bool(worker_pool),
            "Durable task queue/worker pool artifacts",
            "Native runtime exposes lock-aware worker/service controls",
            advantage=True,
        ),
    ]

    parity_passed = sum(1 for item in parity_checks if item["passed"])
    advantage_passed = sum(1 for item in advantage_checks if item["passed"])
    total_checks = parity_checks + advantage_checks
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp(),
        "run_dir": str(run_dir),
        "native_runtime": {
            "status": agent_run.get("status"),
            "durable_checkpoint_count": durable_summary.get("checkpoint_count", 0),
            "durable_step_count": durable_summary.get("step_count", 0),
            "typed_event_count": len(typed_events) if isinstance(typed_events, list) else 0,
        },
        "langgraph_backend": {
            "state_schema_present": bool(langgraph_state),
            "native_backend_status": native_langgraph.get("status"),
            "backend_validation_source": native_langgraph.get("backend_validation_source"),
            "backend_invocation_class": native_langgraph_invocation_class(native_langgraph),
            "graph_mode": native_langgraph.get("graph_mode"),
            "native_runtime_available": native_langgraph.get("native_runtime_available"),
            "invoked": native_langgraph.get("invoked"),
            "resumed": native_langgraph.get("resumed"),
            "state_observed": native_langgraph.get("state_observed"),
        },
        "parity": {
            "status": "passed" if parity_passed == len(parity_checks) else "partial",
            "passed_count": parity_passed,
            "check_count": len(parity_checks),
            "checks": parity_checks,
        },
        "native_advantage": {
            "status": "passed" if advantage_passed == len(advantage_checks) else "partial",
            "passed_count": advantage_passed,
            "check_count": len(advantage_checks),
            "checks": advantage_checks,
        },
        "summary": {
            "status": "native_not_weaker" if all(item["passed"] for item in parity_checks) else "native_gap_detected",
            "parity_score": round(parity_passed / max(len(parity_checks), 1), 3),
            "advantage_score": round(advantage_passed / max(len(advantage_checks), 1), 3),
            "overall_score": round(sum(1 for item in total_checks if item["passed"]) / max(len(total_checks), 1), 3),
            "failed_parity": [item["name"] for item in parity_checks if not item["passed"]],
            "failed_advantage": [item["name"] for item in advantage_checks if not item["passed"]],
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    result = evaluate_run(args.run_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["summary"]["status"] == "native_not_weaker" else 2


if __name__ == "__main__":
    raise SystemExit(main())

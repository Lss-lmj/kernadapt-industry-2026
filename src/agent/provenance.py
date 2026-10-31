#!/usr/bin/env python3
"""Build execution provenance for a LockAgent online run."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def build_provenance(args: argparse.Namespace) -> dict[str, Any]:
    ledger = load_json(args.ledger)
    profile = load_json(args.profile)
    decision = load_json(args.decision)
    manifest = load_json(args.manifest)
    status = load_json(args.status)
    default_metrics = load_json(args.default_metrics) if args.default_metrics else None
    agent_run = load_json(args.agent_run) if getattr(args, "agent_run", None) else None
    failure = load_json(args.failure) if getattr(args, "failure", None) else None
    artifact_store = load_json(args.artifact_store) if getattr(args, "artifact_store", None) else None
    memory_store = load_json(args.memory_store) if getattr(args, "memory_store", None) else None
    skill_curation = load_json(args.skill_curation) if getattr(args, "skill_curation", None) else None
    guard_preflight = load_json(args.guard_preflight) if getattr(args, "guard_preflight", None) else None
    guard_postflight = load_json(args.guard_postflight) if getattr(args, "guard_postflight", None) else None
    human_gate = load_json(args.human_gate) if getattr(args, "human_gate", None) else None
    durable_summary = load_json(args.durable_summary) if getattr(args, "durable_summary", None) else None
    typed_event_bus = load_json(args.typed_event_bus) if getattr(args, "typed_event_bus", None) else None

    evidence_units: list[dict[str, Any]] = []
    if ledger:
        evidence_units.append({"id": "ledger.environment", "source": str(args.ledger), "type": "state_ledger"})
        evidence_units.append({"id": "ledger.buffer_pool", "source": str(args.ledger), "type": "database_config"})
    if profile:
        evidence_units.append({"id": "profile.signals", "source": str(args.profile), "type": "workload_profile"})
    if default_metrics:
        evidence_units.append({"id": "profile.default_metrics", "source": str(args.default_metrics), "type": "profiling_metrics"})
    if decision:
        for item in decision.get("evidence_used", []):
            evidence_units.append({"id": f"decision.evidence_used.{item}", "source": str(args.decision), "type": "llm_cited_evidence"})

    scheduler = decision.get("scheduler") if decision else None
    graph = build_dependency_graph(
        ledger=ledger,
        profile=profile,
        decision=decision,
        manifest=manifest,
        status=status,
        default_metrics=default_metrics,
        agent_run=agent_run,
        failure=failure,
        artifact_store=artifact_store,
        memory_store=memory_store,
        skill_curation=skill_curation,
        guard_preflight=guard_preflight,
        guard_postflight=guard_postflight,
        human_gate=human_gate,
        durable_summary=durable_summary,
        typed_event_bus=typed_event_bus,
        sources={
            "ledger": str(args.ledger),
            "profile": str(args.profile),
            "decision": str(args.decision),
            "manifest": str(args.manifest),
            "status": str(args.status),
            "benchmark_log": str(args.benchmark_log),
            "default_metrics": str(args.default_metrics) if args.default_metrics else None,
            "agent_run": str(args.agent_run) if getattr(args, "agent_run", None) else None,
            "failure": str(args.failure) if getattr(args, "failure", None) else None,
            "artifact_store": str(args.artifact_store) if getattr(args, "artifact_store", None) else None,
            "memory_store": str(args.memory_store) if getattr(args, "memory_store", None) else None,
            "skill_curation": str(args.skill_curation) if getattr(args, "skill_curation", None) else None,
            "guard_preflight": str(args.guard_preflight) if getattr(args, "guard_preflight", None) else None,
            "guard_postflight": str(args.guard_postflight) if getattr(args, "guard_postflight", None) else None,
            "human_gate": str(args.human_gate) if getattr(args, "human_gate", None) else None,
            "durable_summary": str(args.durable_summary) if getattr(args, "durable_summary", None) else None,
            "typed_event_bus": str(args.typed_event_bus) if getattr(args, "typed_event_bus", None) else None,
        },
    )
    return {
        "run_id": args.run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "evidence_units": evidence_units,
        "claims": [
            {
                "id": "claim.scheduler_selection_supported",
                "supported_by": [u["id"] for u in evidence_units],
                "text": (decision or {}).get("rationale"),
            }
        ],
        "actions": [
            {
                "id": "action.load_scx",
                "scheduler": scheduler,
                "caused_by": "decision.scheduler",
                "source": str(args.manifest),
                "benchmark_cmd": (manifest or {}).get("benchmark_cmd"),
            }
        ],
        "outcomes": [
            {
                "id": "outcome.executor_status",
                "source": str(args.status),
                "returncode": (status or {}).get("returncode"),
                "final_sched_ext_state": (status or {}).get("final_sched_ext_state"),
            },
            {
                "id": "outcome.benchmark_log",
                "source": str(args.benchmark_log),
                "exists": args.benchmark_log.exists(),
            },
        ],
        "relations": [
            {"from": "profile.signals", "to": "decision.scheduler", "type": "supports"},
            {"from": "decision.scheduler", "to": "action.load_scx", "type": "causes"},
            {"from": "action.load_scx", "to": "outcome.executor_status", "type": "produces"},
        ],
        "dependency_graph": graph,
    }


def add_node(nodes: list[dict[str, Any]], node_id: str, node_type: str, **attrs: Any) -> None:
    if any(node["id"] == node_id for node in nodes):
        return
    nodes.append({"id": node_id, "type": node_type, **attrs})


def add_edge(edges: list[dict[str, Any]], source: str, target: str, edge_type: str, **attrs: Any) -> None:
    edges.append({"from": source, "to": target, "type": edge_type, **attrs})


def safe_node_suffix(value: Any) -> str:
    text = str(value).strip() or "unknown"
    return "".join(ch if ch.isalnum() else "_" for ch in text)[:80]


def build_dependency_graph(
    *,
    ledger: dict[str, Any] | None,
    profile: dict[str, Any] | None,
    decision: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    status: dict[str, Any] | None,
    default_metrics: dict[str, Any] | None,
    agent_run: dict[str, Any] | None,
    failure: dict[str, Any] | None,
    artifact_store: dict[str, Any] | None,
    memory_store: dict[str, Any] | None,
    skill_curation: dict[str, Any] | None,
    guard_preflight: dict[str, Any] | None,
    guard_postflight: dict[str, Any] | None,
    human_gate: dict[str, Any] | None,
    durable_summary: dict[str, Any] | None,
    typed_event_bus: dict[str, Any] | None,
    sources: dict[str, str | None],
) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    execution_edges: list[dict[str, Any]] = []
    dependency_edges: list[dict[str, Any]] = []
    handoff_edges: list[dict[str, Any]] = []
    artifact_edges: list[dict[str, Any]] = []
    blame_edges: list[dict[str, Any]] = []
    causal_dependency_edges: list[dict[str, Any]] = []
    diagnostic_edges: list[dict[str, Any]] = []
    runtime_cross_reference_edges: list[dict[str, Any]] = []
    regression_blame_chains: list[dict[str, Any]] = []
    categories = failure.get("categories", []) if failure else []
    category_set = {str(category) for category in categories}

    def edge(bucket: list[dict[str, Any]], source: str, target: str, edge_type: str, **attrs: Any) -> None:
        item = {"from": source, "to": target, "type": edge_type, **attrs}
        bucket.append(item)
        edges.append(item)

    def norm_path(value: Any) -> str | None:
        if value in (None, ""):
            return None
        try:
            return str(Path(str(value)).resolve())
        except Exception:
            return str(value)

    def source_semantic_nodes(path: Any) -> list[str]:
        path_norm = norm_path(path)
        if not path_norm:
            return []
        mapping = {
            "ledger": ["ledger.environment"],
            "profile": ["profile.signals"],
            "decision": ["decision.scheduler"],
            "manifest": ["manifest.execution"],
            "status": ["outcome.executor_status", "outcome.scheduler_startup", "outcome.rollback"],
            "benchmark_log": ["outcome.benchmark_log", "outcome.benchmark"],
            "default_metrics": ["profile.default_metrics"],
            "guard_preflight": ["guard.preflight"],
            "guard_postflight": ["guard.postflight"],
            "human_gate": ["human_gate.approval"],
            "durable_summary": ["durable.runtime"],
            "typed_event_bus": ["typed_event_bus.runtime"],
        }
        result: list[str] = []
        for key, nodes_for_source in mapping.items():
            source = sources.get(key)
            if source and norm_path(source) == path_norm:
                result.extend(nodes_for_source)
        return result

    def semantic_nodes_for_runtime_step(step_name: str) -> list[str]:
        name = step_name.lower()
        refs: list[str] = []
        if "state_ledger" in name:
            refs.append("ledger.environment")
        if "profile" in name:
            refs.extend(["profile.default_metrics", "profile.signals"])
        if "llm_policy" in name:
            refs.append("decision.scheduler")
        if "critic" in name:
            refs.extend(["diagnosis.profile_quality", "diagnosis.scheduler_choice", "diagnosis.performance_regression"])
        if "runtime_policy" in name or "guard" in name:
            refs.append("guard.preflight")
        if "human_approval" in name:
            refs.append("human_gate.approval")
        if "scx_executor" in name:
            refs.extend(
                [
                    "manifest.execution",
                    "action.load_scx",
                    "action.scheduler_startup",
                    "action.benchmark_run",
                    "action.rollback",
                    "outcome.executor_status",
                    "outcome.scheduler_startup",
                    "outcome.benchmark",
                    "outcome.benchmark_log",
                    "outcome.rollback",
                    "guard.preflight",
                    "guard.postflight",
                ]
            )
        if "provenance" in name:
            refs.extend(["durable.runtime", "typed_event_bus.runtime"])
        if "skill_curator" in name and skill_curation:
            skill = skill_curation.get("skill")
            if skill:
                refs.append(f"skill.{skill}")
        return list(dict.fromkeys(refs))

    runtime_steps: list[dict[str, Any]] = []
    runtime_step_by_name: dict[str, str] = {}
    if agent_run:
        for index, step in enumerate(agent_run.get("steps", [])):
            if not isinstance(step, dict):
                continue
            step_name = str(step.get("name", f"step_{index}"))
            runtime_id = f"runtime_step.{index}.{safe_node_suffix(step_name)}"
            runtime_steps.append({"index": index, "id": runtime_id, "step": step, "name": step_name})
            runtime_step_by_name.setdefault(step_name, runtime_id)

    add_node(nodes, "ledger.environment", "environment", source=sources["ledger"], present=ledger is not None)
    add_node(nodes, "profile.default_metrics", "evidence", source=sources["default_metrics"], present=default_metrics is not None)
    add_node(nodes, "profile.signals", "evidence", source=sources["profile"], present=profile is not None)
    add_node(nodes, "decision.scheduler", "decision", source=sources["decision"], scheduler=(decision or {}).get("scheduler"))
    add_node(nodes, "manifest.execution", "manifest", source=sources["manifest"], present=manifest is not None)
    add_node(nodes, "action.load_scx", "action", scheduler=(decision or {}).get("scheduler"))
    add_node(nodes, "outcome.executor_status", "outcome", source=sources["status"], returncode=(status or {}).get("returncode"))
    add_node(nodes, "outcome.benchmark_log", "outcome", source=sources["benchmark_log"], present=Path(sources["benchmark_log"]).exists() if sources["benchmark_log"] else False)
    add_node(nodes, "guard.preflight", "guard", source=sources.get("guard_preflight"), verdict=(guard_preflight or {}).get("verdict"), present=guard_preflight is not None)
    add_node(nodes, "guard.postflight", "guard", source=sources.get("guard_postflight"), verdict=(guard_postflight or {}).get("verdict"), present=guard_postflight is not None)
    add_node(nodes, "action.scheduler_startup", "scheduler_action", scheduler=(decision or {}).get("scheduler"), phase="startup")
    add_node(nodes, "action.benchmark_run", "scheduler_action", scheduler=(decision or {}).get("scheduler"), phase="benchmark")
    add_node(nodes, "action.rollback", "scheduler_action", scheduler=(decision or {}).get("scheduler"), phase="rollback")
    add_node(
        nodes,
        "outcome.scheduler_startup",
        "outcome",
        status=(status or {}).get("status"),
        scheduler=(decision or {}).get("scheduler"),
    )
    add_node(
        nodes,
        "outcome.benchmark",
        "outcome",
        returncode=(status or {}).get("returncode"),
        benchmark_log_present=Path(sources["benchmark_log"]).exists() if sources["benchmark_log"] else False,
    )
    add_node(
        nodes,
        "outcome.rollback",
        "outcome",
        final_sched_ext_state=(status or {}).get("final_sched_ext_state"),
    )
    add_node(
        nodes,
        "durable.runtime",
        "durable_store",
        source=sources.get("durable_summary"),
        present=durable_summary is not None,
        checkpoint_count=(durable_summary or {}).get("checkpoint_count"),
        step_count=(durable_summary or {}).get("step_count"),
        event_count=(durable_summary or {}).get("event_count"),
        handoff_count=(durable_summary or {}).get("handoff_count"),
    )
    add_node(
        nodes,
        "typed_event_bus.runtime",
        "typed_event_bus",
        source=sources.get("typed_event_bus"),
        present=typed_event_bus is not None,
        event_count=len((typed_event_bus or {}).get("events", [])) if isinstance((typed_event_bus or {}).get("events", []), list) else 0,
    )

    scheduler = (decision or {}).get("scheduler")
    skill_name = (skill_curation or {}).get("skill")
    if not skill_name and profile:
        for skill in profile.get("scheduler_skill_cards", []):
            if isinstance(skill, dict) and skill.get("action", {}).get("scheduler") == scheduler:
                skill_name = skill.get("name")
                break
    if skill_name:
        add_node(
            nodes,
            f"skill.{skill_name}",
            "skill",
            source=sources.get("skill_curation") or sources["profile"],
            scheduler=scheduler,
            curation_verdict=(skill_curation or {}).get("verdict"),
            present=True,
        )
        add_node(
            nodes,
            f"skill.{skill_name}.applicability",
            "skill_applicability",
            source=sources.get("skill_curation") or sources["profile"],
            skill=skill_name,
            verdict=(skill_curation or {}).get("verdict"),
        )
    if human_gate:
        add_node(
            nodes,
            "human_gate.approval",
            "human_gate",
            source=sources.get("human_gate"),
            status=human_gate.get("status"),
            step_name=human_gate.get("step_name"),
        )

    signals = profile.get("signals", {}) if isinstance(profile, dict) else {}
    signal_count = len(signals) if isinstance(signals, dict) else 0
    profile_quality = "missing_or_failed" if profile is None or "profile_failure" in category_set else ("weak_or_empty" if signal_count == 0 else "available")
    skill_quality = "not_used"
    if skill_name:
        if "skill_misfire" in category_set:
            skill_quality = "misfired"
        else:
            skill_quality = str((skill_curation or {}).get("verdict") or "candidate_or_unknown")
    scheduler_quality = "regressed" if "scheduler_regression" in category_set else ("startup_failed" if "scheduler_startup_failure" in category_set else "selected")
    regression_status = "suspected" if category_set & {"scheduler_regression", "skill_misfire"} else "not_observed"
    add_node(
        nodes,
        "diagnosis.profile_quality",
        "diagnosis",
        subject="profile",
        quality=profile_quality,
        signal_count=signal_count,
        source=sources["profile"],
    )
    add_node(
        nodes,
        "diagnosis.skill_applicability",
        "diagnosis",
        subject="skill_applicability",
        skill=skill_name,
        quality=skill_quality,
        source=sources.get("skill_curation") or sources["profile"],
    )
    add_node(
        nodes,
        "diagnosis.scheduler_choice",
        "diagnosis",
        subject="scheduler_choice",
        scheduler=scheduler,
        quality=scheduler_quality,
        source=sources["decision"],
    )
    add_node(
        nodes,
        "diagnosis.performance_regression",
        "diagnosis",
        subject="performance_regression",
        status=regression_status,
        benchmark_returncode=(status or {}).get("returncode"),
        source=sources["status"],
    )

    edge(dependency_edges, "ledger.environment", "profile.signals", "context_for")
    edge(dependency_edges, "profile.default_metrics", "profile.signals", "summarized_into")
    edge(dependency_edges, "profile.signals", "decision.scheduler", "supports")
    edge(dependency_edges, "ledger.environment", "decision.scheduler", "constrains")
    edge(diagnostic_edges, "profile.signals", "diagnosis.profile_quality", "profile_quality_assessed")
    if skill_name:
        edge(dependency_edges, "profile.signals", f"skill.{skill_name}", "retrieves_or_conditions")
        edge(dependency_edges, "profile.signals", f"skill.{skill_name}.applicability", "tests_applicability")
        edge(dependency_edges, f"skill.{skill_name}.applicability", f"skill.{skill_name}", "selects_skill")
        edge(dependency_edges, f"skill.{skill_name}", "decision.scheduler", "guides")
        edge(diagnostic_edges, "diagnosis.profile_quality", f"skill.{skill_name}.applicability", "profile_conditions_skill")
        edge(diagnostic_edges, f"skill.{skill_name}.applicability", "diagnosis.skill_applicability", "skill_applicability_assessed")
        edge(diagnostic_edges, "diagnosis.skill_applicability", f"skill.{skill_name}", "skill_conditioned_by_profile")
        edge(diagnostic_edges, f"skill.{skill_name}", "decision.scheduler", "skill_guides_scheduler_choice")
    else:
        edge(diagnostic_edges, "diagnosis.profile_quality", "decision.scheduler", "profile_conditions_scheduler_choice")
    edge(diagnostic_edges, "decision.scheduler", "diagnosis.scheduler_choice", "scheduler_choice_assessed")
    edge(diagnostic_edges, "diagnosis.scheduler_choice", "action.scheduler_startup", "scheduler_choice_materializes_action")
    edge(diagnostic_edges, "action.benchmark_run", "outcome.benchmark", "action_produces_benchmark_outcome")
    edge(diagnostic_edges, "outcome.benchmark", "diagnosis.performance_regression", "outcome_indicates_regression", suspected=regression_status == "suspected")
    if profile:
        signals = profile.get("signals", {})
        if isinstance(signals, dict):
            for key, value in signals.items():
                signal_id = f"profile.signal.{safe_node_suffix(key)}"
                add_node(nodes, signal_id, "profile_signal", key=key, value=value, source=sources["profile"])
                edge(dependency_edges, signal_id, "profile.signals", "part_of_profile")
                if skill_name:
                    edge(causal_dependency_edges, signal_id, f"skill.{skill_name}.applicability", "conditions_skill_applicability")
                else:
                    edge(causal_dependency_edges, signal_id, "decision.scheduler", "conditions_decision")
    if decision:
        for index, evidence in enumerate(decision.get("evidence_used", []) if isinstance(decision.get("evidence_used", []), list) else []):
            evidence_id = f"decision.evidence.{index}.{safe_node_suffix(evidence)}"
            add_node(nodes, evidence_id, "decision_evidence", evidence=evidence, source=sources["decision"])
            edge(dependency_edges, evidence_id, "decision.scheduler", "cited_by_decision")
            edge(causal_dependency_edges, "profile.signals", evidence_id, "supports_evidence_claim")
        for index, risk in enumerate(decision.get("risks", []) if isinstance(decision.get("risks", []), list) else []):
            risk_id = f"decision.risk.{index}.{safe_node_suffix(risk)}"
            add_node(nodes, risk_id, "decision_risk", risk=risk, source=sources["decision"])
            edge(dependency_edges, "decision.scheduler", risk_id, "declares_risk")
        for index, condition in enumerate(decision.get("rollback_conditions", []) if isinstance(decision.get("rollback_conditions", []), list) else []):
            rollback_id = f"decision.rollback_condition.{index}.{safe_node_suffix(condition)}"
            add_node(nodes, rollback_id, "rollback_condition", condition=condition, source=sources["decision"])
            edge(dependency_edges, rollback_id, "action.rollback", "guards_rollback")
    edge(execution_edges, "decision.scheduler", "guard.preflight", "checked_by")
    edge(execution_edges, "guard.preflight", "manifest.execution", "authorizes")
    edge(execution_edges, "decision.scheduler", "manifest.execution", "materialized_as")
    edge(execution_edges, "manifest.execution", "action.load_scx", "authorizes")
    edge(execution_edges, "action.load_scx", "action.scheduler_startup", "starts")
    edge(execution_edges, "action.scheduler_startup", "outcome.scheduler_startup", "produces")
    edge(execution_edges, "outcome.scheduler_startup", "action.benchmark_run", "enables")
    edge(execution_edges, "action.benchmark_run", "outcome.benchmark", "produces")
    edge(execution_edges, "outcome.benchmark", "action.rollback", "triggers_cleanup")
    edge(execution_edges, "action.rollback", "outcome.rollback", "produces")
    edge(causal_dependency_edges, "decision.scheduler", "action.scheduler_startup", "causes_scheduler_startup")
    edge(causal_dependency_edges, "action.scheduler_startup", "outcome.scheduler_startup", "causes_startup_outcome")
    edge(causal_dependency_edges, "outcome.scheduler_startup", "action.benchmark_run", "enables_benchmark")
    edge(causal_dependency_edges, "action.benchmark_run", "outcome.benchmark", "causes_benchmark_outcome")
    edge(causal_dependency_edges, "outcome.benchmark", "action.rollback", "requires_cleanup")
    edge(causal_dependency_edges, "action.rollback", "outcome.rollback", "causes_rollback_outcome")
    if human_gate:
        edge(execution_edges, "guard.preflight", "human_gate.approval", "requires_approval")
        edge(execution_edges, "human_gate.approval", "action.load_scx", "gates")
    edge(execution_edges, "action.load_scx", "outcome.executor_status", "produces")
    edge(execution_edges, "action.load_scx", "outcome.benchmark_log", "produces")
    edge(execution_edges, "outcome.executor_status", "guard.postflight", "checked_by")
    edge(causal_dependency_edges, "outcome.executor_status", "guard.postflight", "checked_by_postflight_guard")
    edge(dependency_edges, "durable.runtime", "typed_event_bus.runtime", "indexes_events")
    edge(dependency_edges, "typed_event_bus.runtime", "decision.scheduler", "records_policy_handoff")
    edge(execution_edges, "typed_event_bus.runtime", "action.load_scx", "records_execution_order")

    if artifact_store:
        for index, artifact in enumerate(artifact_store.get("artifacts", [])):
            if not isinstance(artifact, dict):
                continue
            node_id = f"artifact_record.{index}.{safe_node_suffix(artifact.get('artifact_type'))}"
            add_node(
                nodes,
                node_id,
                "artifact_record",
                path=artifact.get("path"),
                artifact_type=artifact.get("artifact_type"),
                producer=artifact.get("producer"),
                consumer=artifact.get("consumer"),
                step_name=artifact.get("step_name"),
                handoff_id=artifact.get("handoff_id"),
                artifact_role=artifact.get("artifact_role"),
                required=artifact.get("required"),
                exists=artifact.get("exists"),
                sha256=artifact.get("sha256"),
                schema_version=artifact.get("schema_version"),
            )
            producer = artifact.get("producer")
            if producer:
                edge(artifact_edges, f"step.{producer}", node_id, "writes")
            step_name = str(artifact.get("step_name") or "")
            runtime_step_id = runtime_step_by_name.get(step_name)
            role = artifact.get("artifact_role")
            if runtime_step_id and role == "input":
                edge(runtime_cross_reference_edges, node_id, runtime_step_id, "runtime_consumes_artifact", required=artifact.get("required"))
                edge(artifact_edges, node_id, runtime_step_id, "consumed_by_runtime_step", required=artifact.get("required"))
            elif runtime_step_id and role == "output":
                edge(runtime_cross_reference_edges, runtime_step_id, node_id, "runtime_produces_artifact", required=artifact.get("required"))
                edge(artifact_edges, runtime_step_id, node_id, "produced_by_runtime_step", required=artifact.get("required"))
            for semantic_id in source_semantic_nodes(artifact.get("path")):
                edge(runtime_cross_reference_edges, node_id, semantic_id, "artifact_represents_provenance_node")
                if runtime_step_id and role == "output":
                    edge(runtime_cross_reference_edges, runtime_step_id, semantic_id, "runtime_step_produces_provenance_node")
                if runtime_step_id and role == "input":
                    edge(runtime_cross_reference_edges, semantic_id, runtime_step_id, "runtime_step_consumes_provenance_node")

    if memory_store:
        for index, item in enumerate(memory_store.get("items", [])):
            if not isinstance(item, dict):
                continue
            node_id = f"memory.{item.get('namespace', 'unknown')}.{item.get('key', index)}"
            add_node(
                nodes,
                node_id,
                "memory",
                namespace=item.get("namespace"),
                key=item.get("key"),
                source=item.get("source") or sources.get("memory_store"),
            )
            source = item.get("source")
            if source:
                edge(dependency_edges, str(source), node_id, "stored_as_memory")
            if item.get("namespace") == "skill_evidence" and skill_name:
                edge(dependency_edges, node_id, f"skill.{skill_name}", "updates_skill_evidence")
            if item.get("namespace") == "policy_decisions":
                edge(dependency_edges, node_id, "decision.scheduler", "policy_memory")

    if agent_run:
        previous_step: str | None = None
        for runtime_item in runtime_steps:
            step = runtime_item["step"]
            step_id = f"step.{step.get('name')}"
            runtime_step_id = str(runtime_item["id"])
            add_node(
                nodes,
                step_id,
                "agent_step",
                role=step.get("role"),
                status=step.get("status"),
                returncode=step.get("returncode"),
                capability_verdict=step.get("capability_verdict"),
            )
            add_node(
                nodes,
                runtime_step_id,
                "runtime_step",
                step_name=step.get("name"),
                step_index=runtime_item["index"],
                role=step.get("role"),
                status=step.get("status"),
                returncode=step.get("returncode"),
                handoff_id=(step.get("metadata") or {}).get("handoff_id") if isinstance(step.get("metadata"), dict) else None,
                scheduler_task_id=(step.get("metadata") or {}).get("scheduler_task_id") if isinstance(step.get("metadata"), dict) else None,
                capability_verdict=step.get("capability_verdict"),
            )
            edge(runtime_cross_reference_edges, step_id, runtime_step_id, "aliases_runtime_step")
            if previous_step:
                edge(execution_edges, previous_step, runtime_step_id, "precedes_runtime_step")
            previous_step = runtime_step_id
            for artifact in step.get("input_artifacts", []):
                edge(handoff_edges, str(artifact), step_id, "input_to")
                edge(runtime_cross_reference_edges, str(artifact), runtime_step_id, "path_input_to_runtime_step")
                for semantic_id in source_semantic_nodes(artifact):
                    edge(runtime_cross_reference_edges, semantic_id, runtime_step_id, "semantic_input_to_runtime_step")
            for artifact in step.get("output_artifacts", []):
                edge(handoff_edges, step_id, str(artifact), "outputs")
                edge(runtime_cross_reference_edges, runtime_step_id, str(artifact), "runtime_step_outputs_path")
                for semantic_id in source_semantic_nodes(artifact):
                    edge(runtime_cross_reference_edges, runtime_step_id, semantic_id, "runtime_step_outputs_semantic_node")
            if step.get("role"):
                role_id = f"role.{step.get('role')}"
                add_node(nodes, role_id, "agent_role", role=step.get("role"))
                edge(execution_edges, role_id, runtime_step_id, "owns_runtime_step")
                edge(execution_edges, role_id, step_id, "owns_step")
            for semantic_id in semantic_nodes_for_runtime_step(str(step.get("name", ""))):
                edge(runtime_cross_reference_edges, runtime_step_id, semantic_id, "runtime_step_semantic_reference")

    if durable_summary:
        for status_name, count in (durable_summary.get("step_status_counts") or {}).items():
            status_id = f"durable.step_status.{status_name}"
            add_node(nodes, status_id, "durable_step_status", status=status_name, count=count)
            edge(dependency_edges, "durable.runtime", status_id, "summarizes")

    if typed_event_bus:
        previous_event_id: str | None = None
        for index, event in enumerate((typed_event_bus.get("events") or [])[:200]):
            if not isinstance(event, dict):
                continue
            event_id = f"typed_event.{index}.{event.get('event_type')}"
            add_node(
                nodes,
                event_id,
                "typed_event",
                event_type=event.get("event_type"),
                source=event.get("source"),
                target=event.get("target"),
                correlation_id=event.get("correlation_id"),
            )
            edge(handoff_edges, "typed_event_bus.runtime", event_id, "contains")
            if previous_event_id:
                edge(execution_edges, previous_event_id, event_id, "event_precedes")
            previous_event_id = event_id
            if event.get("event_type") == "handoff":
                source_role = f"role.{event.get('source')}"
                target_role = f"role.{event.get('target')}"
                add_node(nodes, source_role, "agent_role", role=event.get("source"))
                add_node(nodes, target_role, "agent_role", role=event.get("target"))
                edge(handoff_edges, source_role, target_role, "typed_handoff", reason=event.get("payload", {}).get("reason"))

    blame_paths: list[dict[str, Any]] = []
    fine_grained_blame_paths: list[dict[str, Any]] = []
    causal_dependency_chains: list[dict[str, Any]] = []

    def canonical_chain_for(category: str) -> list[str]:
        chain = ["ledger.environment", "profile.signals"]
        if skill_name:
            chain.extend([f"skill.{skill_name}.applicability", f"skill.{skill_name}"])
        chain.extend(
            [
                "decision.scheduler",
                "guard.preflight",
                "manifest.execution",
                "action.scheduler_startup",
                "outcome.scheduler_startup",
                "action.benchmark_run",
                "outcome.benchmark",
                "action.rollback",
                "outcome.rollback",
                f"failure.{category}",
            ]
        )
        if category in {"policy_reasoning_failure", "skill_misfire"}:
            return chain[1:]
        if category in {"guard_blocked"}:
            guarded = ["decision.scheduler", "guard.preflight"]
            if human_gate:
                guarded.append("human_gate.approval")
            guarded.append(f"failure.{category}")
            return guarded
        if category in {"scheduler_startup_failure"}:
            return ["decision.scheduler", "manifest.execution", "action.scheduler_startup", "outcome.scheduler_startup", f"failure.{category}"]
        if category in {"benchmark_failure", "scheduler_regression"}:
            return ["decision.scheduler", "action.scheduler_startup", "outcome.scheduler_startup", "action.benchmark_run", "outcome.benchmark", f"failure.{category}"]
        if category in {"rollback_failure"}:
            return ["outcome.benchmark", "action.rollback", "outcome.rollback", f"failure.{category}"]
        if category in {"executor_failure"}:
            return ["manifest.execution", "action.load_scx", "outcome.executor_status", f"failure.{category}"]
        return chain

    for category in categories:
        failure_id = f"failure.{category}"
        add_node(nodes, failure_id, "failure", category=category)
        if category in {"policy_reasoning_failure", "skill_misfire", "scheduler_regression"}:
            edge(diagnostic_edges, "diagnosis.performance_regression", failure_id, "regression_explains_failure", category=category)
        if category in {"profile_failure", "environment_mismatch", "comparison_invalid"}:
            path = ["ledger.environment", "profile.signals"]
            if skill_name:
                path.append(f"skill.{skill_name}")
            path.extend(["decision.scheduler", "action.load_scx", "outcome.executor_status", failure_id])
        elif category in {"policy_reasoning_failure", "skill_misfire"}:
            path = ["profile.signals"]
            if skill_name:
                path.append(f"skill.{skill_name}")
            path.extend(["decision.scheduler", "action.load_scx", "outcome.executor_status", failure_id])
        elif category in {"scheduler_startup_failure", "scheduler_regression"}:
            path = ["decision.scheduler", "manifest.execution", "action.load_scx", "outcome.executor_status", failure_id]
        elif category in {"benchmark_failure", "rollback_failure", "executor_failure"}:
            path = ["manifest.execution", "action.load_scx", "outcome.executor_status", failure_id]
        elif category == "guard_blocked":
            path = ["decision.scheduler", "guard.preflight"]
            if human_gate:
                path.append("human_gate.approval")
            path.append(failure_id)
        else:
            path = ["outcome.executor_status", failure_id]
        for source, target in zip(path, path[1:]):
            edge(blame_edges, source, target, "blame_path", category=category)
        blame_paths.append({"category": category, "path": path})
        fine_path = canonical_chain_for(category)
        for source, target in zip(fine_path, fine_path[1:]):
            edge(blame_edges, source, target, "fine_grained_blame_path", category=category)
        fine_grained_blame_paths.append(
            {
                "category": category,
                "path": fine_path,
                "root": fine_path[0] if fine_path else None,
                "terminal": fine_path[-1] if fine_path else None,
            }
        )
        causal_dependency_chains.append(
            {
                "category": category,
                "chain_type": "profile_skill_decision_action_outcome_failure",
                "path": fine_path,
                "has_skill_node": any(node.startswith("skill.") for node in fine_path),
                "has_guard_node": any(node.startswith("guard.") for node in fine_path),
                "has_benchmark_node": "outcome.benchmark" in fine_path,
                "has_rollback_node": "outcome.rollback" in fine_path,
            }
        )
        if category in {"policy_reasoning_failure", "skill_misfire", "scheduler_regression"}:
            regression_path = ["profile.signals", "diagnosis.profile_quality"]
            if skill_name:
                regression_path.extend(
                    [
                        f"skill.{skill_name}.applicability",
                        "diagnosis.skill_applicability",
                        f"skill.{skill_name}",
                    ]
                )
            regression_path.extend(
                [
                    "decision.scheduler",
                    "diagnosis.scheduler_choice",
                    "action.scheduler_startup",
                    "outcome.scheduler_startup",
                    "action.benchmark_run",
                    "outcome.benchmark",
                    "diagnosis.performance_regression",
                    failure_id,
                ]
            )
            for source, target in zip(regression_path, regression_path[1:]):
                edge(blame_edges, source, target, "regression_blame_path", category=category)
            regression_blame_chains.append(
                {
                    "category": category,
                    "chain_type": "bad_profile_bad_skill_bad_scheduler_regression",
                    "path": regression_path,
                    "profile_quality": profile_quality,
                    "skill_quality": skill_quality,
                    "scheduler_quality": scheduler_quality,
                    "performance_regression": regression_status,
                    "has_bad_profile_node": "diagnosis.profile_quality" in regression_path,
                    "has_bad_skill_node": "diagnosis.skill_applicability" in regression_path,
                    "has_bad_scheduler_node": "diagnosis.scheduler_choice" in regression_path,
                    "has_regression_node": "diagnosis.performance_regression" in regression_path,
                }
            )

    return {
        "schema_version": "lockagent.provenance_graph.v0.4",
        "nodes": nodes,
        "edges": edges,
        "execution_edges": execution_edges,
        "dependency_edges": dependency_edges,
        "handoff_edges": handoff_edges,
        "artifact_edges": artifact_edges,
        "blame_edges": blame_edges,
        "diagnostic_edges": diagnostic_edges,
        "runtime_cross_reference_edges": runtime_cross_reference_edges,
        "blame_paths": blame_paths,
        "causal_dependency_edges": causal_dependency_edges,
        "causal_dependency_chains": causal_dependency_chains,
        "fine_grained_blame_paths": fine_grained_blame_paths,
        "regression_blame_chains": regression_blame_chains,
        "runtime_cross_reference": {
            "runtime_step_nodes": [node for node in nodes if node.get("type") == "runtime_step"],
            "artifact_record_nodes": [node for node in nodes if node.get("type") == "artifact_record"],
            "edges": runtime_cross_reference_edges,
            "semantic_reference_edges": [
                item
                for item in runtime_cross_reference_edges
                if item.get("type") in {
                    "runtime_step_semantic_reference",
                    "runtime_step_produces_provenance_node",
                    "runtime_step_consumes_provenance_node",
                    "artifact_represents_provenance_node",
                    "semantic_input_to_runtime_step",
                    "runtime_step_outputs_semantic_node",
                }
            ],
        },
        "dependency_blame_graph": {
            "nodes": [
                node
                for node in nodes
                if node.get("id", "").startswith(("diagnosis.", "profile.", "skill.", "decision.", "action.", "outcome.", "failure.", "runtime_step."))
            ],
            "edges": diagnostic_edges + blame_edges + runtime_cross_reference_edges,
            "regression_blame_chains": regression_blame_chains,
        },
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--decision", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--benchmark-log", type=Path, required=True)
    ap.add_argument("--default-metrics", type=Path)
    ap.add_argument("--agent-run", type=Path)
    ap.add_argument("--failure", type=Path)
    ap.add_argument("--artifact-store", type=Path)
    ap.add_argument("--memory-store", type=Path)
    ap.add_argument("--skill-curation", type=Path)
    ap.add_argument("--guard-preflight", type=Path)
    ap.add_argument("--guard-postflight", type=Path)
    ap.add_argument("--human-gate", type=Path)
    ap.add_argument("--durable-summary", type=Path)
    ap.add_argument("--typed-event-bus", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    write_json(args.out, build_provenance(args))
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

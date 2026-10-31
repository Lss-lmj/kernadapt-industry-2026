#!/usr/bin/env python3
"""Local checks for LockAgent provenance dependency graph."""

from __future__ import annotations

import argparse
import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "provenance.py"
spec = importlib.util.spec_from_file_location("provenance", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ledger = base / "ledger.json"
        profile = base / "profile.json"
        decision = base / "decision.json"
        manifest = base / "manifest.json"
        status = base / "status.json"
        default_metrics = base / "default_metrics.json"
        failure = base / "failure.json"
        agent_run = base / "agent_run.json"
        artifacts = base / "artifact_store.json"
        memory = base / "memory_store.json"
        skill_curation = base / "skill_curation.json"
        guard_pre = base / "guard_preflight.json"
        guard_post = base / "guard_postflight.json"
        durable_summary = base / "durable_runtime_summary.json"
        typed_event_bus = base / "typed_event_bus.json"
        bench = base / "sysbench.log"
        write(ledger, {"database": {"collection_status": "ok"}})
        write(
            profile,
            {
                "signals": {"hot_lock": True},
                "scheduler_skill_cards": [
                    {"name": "choose_locknest_for_hot_lock_oltp", "action": {"scheduler": "scx_lockcoop"}}
                ],
            },
        )
        write(decision, {"scheduler": "scx_lockcoop", "evidence_used": ["profile.signals"], "rationale": "test"})
        write(manifest, {"benchmark_cmd": "true"})
        write(status, {"returncode": 1, "final_sched_ext_state": "disabled"})
        write(default_metrics, {"tps": 1.0})
        write(failure, {"categories": ["policy_reasoning_failure", "skill_misfire"]})
        write(
            agent_run,
            {
                "steps": [
                    {
                        "name": "llm_policy_agent",
                        "role": "PolicyAgent",
                        "status": "ok",
                        "input_artifacts": [str(profile)],
                        "output_artifacts": [str(decision)],
                        "metadata": {"handoff_id": "handoff-policy", "scheduler_task_id": "task-policy"},
                    },
                    {
                        "name": "scx_executor",
                        "role": "ExecutorAgent",
                        "status": "failed",
                        "input_artifacts": [str(decision), str(profile)],
                        "output_artifacts": [str(status), str(bench)],
                        "metadata": {"handoff_id": "handoff-exec", "scheduler_task_id": "task-exec"},
                    },
                ]
            },
        )
        write(
            artifacts,
            {
                "artifacts": [
                    {
                        "artifact_type": "step_input",
                        "path": str(profile),
                        "producer": "ProfilerAgent",
                        "consumer": "PolicyAgent",
                        "step_name": "llm_policy_agent",
                        "handoff_id": "handoff-policy",
                        "artifact_role": "input",
                        "required": True,
                        "exists": True,
                        "sha256": "profile-hash",
                        "schema_version": "lockagent.runtime.v0.1",
                    },
                    {
                        "artifact_type": "step_output",
                        "path": str(decision),
                        "producer": "PolicyAgent",
                        "consumer": None,
                        "step_name": "llm_policy_agent",
                        "handoff_id": "handoff-policy",
                        "artifact_role": "output",
                        "required": True,
                        "exists": True,
                        "sha256": "decision-hash",
                        "schema_version": "lockagent.runtime.v0.1",
                    },
                    {
                        "artifact_type": "step_input",
                        "path": str(decision),
                        "producer": "PolicyAgent",
                        "consumer": "ExecutorAgent",
                        "step_name": "scx_executor",
                        "handoff_id": "handoff-exec",
                        "artifact_role": "input",
                        "required": True,
                        "exists": True,
                        "sha256": "decision-hash",
                        "schema_version": "lockagent.runtime.v0.1",
                    },
                    {
                        "artifact_type": "step_output",
                        "path": str(status),
                        "producer": "ExecutorAgent",
                        "consumer": None,
                        "step_name": "scx_executor",
                        "handoff_id": "handoff-exec",
                        "artifact_role": "output",
                        "required": True,
                        "exists": True,
                        "sha256": "status-hash",
                        "schema_version": "lockagent.runtime.v0.1",
                    },
                ]
            },
        )
        write(memory, {"items": [{"namespace": "policy_decisions", "key": "run_test", "source": str(decision)}]})
        write(skill_curation, {"skill": "choose_locknest_for_hot_lock_oltp", "verdict": "candidate_rejected_for_this_context"})
        write(guard_pre, {"verdict": "allow"})
        write(guard_post, {"verdict": "allow"})
        write(durable_summary, {"checkpoint_count": 4, "step_count": 3, "event_count": 9, "handoff_count": 2, "step_status_counts": {"ok": 2, "failed": 1}})
        write(
            typed_event_bus,
            {
                "events": [
                    {"event_type": "handoff", "source": "ProfilerAgent", "target": "PolicyAgent", "payload": {"reason": "profile ready"}, "correlation_id": "e1"},
                    {"event_type": "step_start", "source": "PolicyAgent", "target": "runtime", "payload": {}, "correlation_id": "e2"},
                    {"event_type": "step_end", "source": "PolicyAgent", "target": "runtime", "payload": {}, "correlation_id": "e2"},
                ]
            },
        )
        bench.write_text("benchmark", encoding="utf-8")
        args = argparse.Namespace(
            run_id="run_test",
            ledger=ledger,
            profile=profile,
            decision=decision,
            manifest=manifest,
            status=status,
            benchmark_log=bench,
            default_metrics=default_metrics,
            agent_run=agent_run,
            failure=failure,
            artifact_store=artifacts,
            memory_store=memory,
            skill_curation=skill_curation,
            guard_preflight=guard_pre,
            guard_postflight=guard_post,
            human_gate=base / "missing_human_gate.json",
            durable_summary=durable_summary,
            typed_event_bus=typed_event_bus,
        )
        prov = module.build_provenance(args)
        graph = prov["dependency_graph"]
        assert graph["nodes"], graph
        assert graph["edges"], graph
        assert graph["execution_edges"], graph
        assert graph["dependency_edges"], graph
        assert graph["handoff_edges"], graph
        assert graph["artifact_edges"], graph
        assert graph["blame_edges"], graph
        assert graph["blame_paths"], graph
        assert graph["causal_dependency_edges"], graph
        assert graph["causal_dependency_chains"], graph
        assert graph["fine_grained_blame_paths"], graph
        assert graph["diagnostic_edges"], graph
        assert graph["runtime_cross_reference_edges"], graph
        assert graph["runtime_cross_reference"]["runtime_step_nodes"], graph
        assert graph["runtime_cross_reference"]["artifact_record_nodes"], graph
        assert graph["runtime_cross_reference"]["semantic_reference_edges"], graph
        assert graph["regression_blame_chains"], graph
        assert graph["dependency_blame_graph"]["regression_blame_chains"], graph
        assert graph["blame_paths"][0]["category"] == "policy_reasoning_failure", graph
        assert any(node["id"] == "skill.choose_locknest_for_hot_lock_oltp" for node in graph["nodes"]), graph
        assert any(node["id"] == "skill.choose_locknest_for_hot_lock_oltp.applicability" for node in graph["nodes"]), graph
        assert any(node["id"] == "diagnosis.profile_quality" for node in graph["nodes"]), graph
        assert any(node["id"] == "diagnosis.skill_applicability" for node in graph["nodes"]), graph
        assert any(node["id"] == "diagnosis.scheduler_choice" for node in graph["nodes"]), graph
        assert any(node["id"] == "diagnosis.performance_regression" for node in graph["nodes"]), graph
        assert any(node["id"] == "profile.signal.hot_lock" for node in graph["nodes"]), graph
        assert any(node["id"] == "action.scheduler_startup" for node in graph["nodes"]), graph
        assert any(node["id"] == "outcome.benchmark" for node in graph["nodes"]), graph
        assert any(node["id"] == "outcome.rollback" for node in graph["nodes"]), graph
        assert any(node["id"] == "durable.runtime" for node in graph["nodes"]), graph
        assert any(node["id"] == "typed_event_bus.runtime" for node in graph["nodes"]), graph
        assert any(node["type"] == "typed_event" for node in graph["nodes"]), graph
        assert any(node["type"] == "runtime_step" and node["step_name"] == "llm_policy_agent" for node in graph["nodes"]), graph
        assert any(node["type"] == "runtime_step" and node["step_name"] == "scx_executor" for node in graph["nodes"]), graph
        assert any(node["type"] == "artifact_record" and node["artifact_role"] == "input" and node["consumer"] == "ExecutorAgent" for node in graph["nodes"]), graph
        assert any(edge["type"] == "typed_handoff" for edge in graph["handoff_edges"]), graph
        assert any(edge["type"] == "runtime_consumes_artifact" for edge in graph["runtime_cross_reference_edges"]), graph
        assert any(edge["type"] == "runtime_produces_artifact" for edge in graph["runtime_cross_reference_edges"]), graph
        assert any(edge["type"] == "runtime_step_semantic_reference" and edge["to"] == "decision.scheduler" for edge in graph["runtime_cross_reference_edges"]), graph
        assert any(edge["type"] == "runtime_step_outputs_semantic_node" and edge["to"] == "outcome.executor_status" for edge in graph["runtime_cross_reference_edges"]), graph
        assert any(edge["type"] == "conditions_skill_applicability" for edge in graph["causal_dependency_edges"]), graph
        assert any(edge["type"] == "causes_benchmark_outcome" for edge in graph["causal_dependency_edges"]), graph
        diagnostic_edge_types = {edge["type"] for edge in graph["diagnostic_edges"]}
        assert "profile_conditions_skill" in diagnostic_edge_types, graph
        assert "skill_guides_scheduler_choice" in diagnostic_edge_types, graph
        assert "scheduler_choice_materializes_action" in diagnostic_edge_types, graph
        assert "action_produces_benchmark_outcome" in diagnostic_edge_types, graph
        assert "outcome_indicates_regression" in diagnostic_edge_types, graph
        assert "regression_explains_failure" in diagnostic_edge_types, graph
        assert any(
            "skill.choose_locknest_for_hot_lock_oltp" in path["path"]
            for path in graph["blame_paths"]
            if path["category"] in {"policy_reasoning_failure", "skill_misfire"}
        ), graph
        assert any(
            "skill.choose_locknest_for_hot_lock_oltp.applicability" in path["path"]
            and "outcome.benchmark" in path["path"]
            for path in graph["fine_grained_blame_paths"]
        ), graph
        regression_path = graph["regression_blame_chains"][0]["path"]
        assert "profile.signals" in regression_path, regression_path
        assert "diagnosis.profile_quality" in regression_path, regression_path
        assert "skill.choose_locknest_for_hot_lock_oltp.applicability" in regression_path, regression_path
        assert "diagnosis.skill_applicability" in regression_path, regression_path
        assert "skill.choose_locknest_for_hot_lock_oltp" in regression_path, regression_path
        assert "decision.scheduler" in regression_path, regression_path
        assert "diagnosis.scheduler_choice" in regression_path, regression_path
        assert "outcome.benchmark" in regression_path, regression_path
        assert "diagnosis.performance_regression" in regression_path, regression_path
        assert regression_path[-1].startswith("failure."), regression_path
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

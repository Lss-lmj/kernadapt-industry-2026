#!/usr/bin/env python3
"""Local checks for native-vs-LangGraph runtime parity audit."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "runtime_parity.py"
spec = importlib.util.spec_from_file_location("runtime_parity", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run_parity"
        run.mkdir()
        (run / "durable_runtime.db").write_bytes(b"sqlite-placeholder")
        write(
            run / "agent_run.json",
            {
                "status": "ok",
                "graph": {
                    "nodes": ["ProfilerAgent", "ExecutorAgent"],
                    "edges": [{"from": "ProfilerAgent", "to": "ExecutorAgent"}],
                    "step_nodes": [
                        {"id": "step_template.0.profile_builder", "name": "profile_builder", "role": "ProfilerAgent"},
                        {"id": "step_template.1.scx_executor", "name": "scx_executor", "role": "ExecutorAgent"},
                    ],
                    "step_edges": [{"from": "step_template.0.profile_builder", "to": "step_template.1.scx_executor"}],
                },
                "steps": [
                    {
                        "name": "scx_executor",
                        "role": "ExecutorAgent",
                        "metadata": {"execution_graph_node": "step_template.1.scx_executor"},
                    }
                ],
            },
        )
        write(
            run / "langgraph_state.json",
            {
                "thread_id": "run_parity",
                "checkpoint": str(run / "agent_run.json"),
                "metadata": {"graph": {"nodes": ["ProfilerAgent"], "edges": [{"from": "START", "to": "ProfilerAgent"}]}},
            },
        )
        write(
            run / "native_langgraph_result.json",
            {
                "status": "unavailable",
                "backend_validation_source": "unavailable",
                "native_runtime_available": False,
                "invoked": False,
                "resumed": False,
                "state_observed": False,
            },
        )
        write(run / "durable_runtime_summary.json", {"checkpoint_count": 2, "step_count": 2, "task_count": 1})
        write(
            run / "execution_graph_runtime.json",
            {
                "observed_nodes": [{"id": "runtime_step.0.scx_executor", "name": "scx_executor"}],
                "observed_control_edges": [{"from": "runtime_step.0.profile_builder", "to": "runtime_step.1.scx_executor"}],
                "observed_artifact_dependency_edges": [],
                "summary": {
                    "observed_step_count": 1,
                    "declared_step_coverage": 0.5,
                    "observed_control_edge_count": 1,
                    "observed_artifact_dependency_edge_count": 0,
                },
            },
        )
        write(
            run / "graph_scheduler_plan.json",
            {
                "declared_steps": [
                    {
                        "id": "step_template.0.scx_executor",
                        "name": "scx_executor",
                        "role": "ExecutorAgent",
                        "status": "ok",
                        "observed": True,
                    }
                ],
                "artifact_dependency_edges": [
                    {
                        "from": "runtime_step.0.profile_builder",
                        "to": "step_template.0.scx_executor",
                        "type": "artifact_dependency",
                        "artifact": "decision.json",
                    }
                ],
                "failed_step_replay_candidates": [],
                "next_runnable_steps": [],
                "summary": {
                    "declared_step_count": 1,
                    "observed_declared_step_count": 1,
                    "completed_step_count": 1,
                    "waiting_step_count": 0,
                    "blocked_step_count": 0,
                    "runnable_step_count": 0,
                    "next_runnable_count": 0,
                    "artifact_dependency_edge_count": 1,
                    "failed_step_replay_candidate_count": 0,
                },
            },
        )
        write(run / "typed_event_bus.json", {"events": [{"event_type": "handoff"}]})
        (run / "typed_event_bus.jsonl").write_text(json.dumps({"event_type": "handoff"}) + "\n", encoding="utf-8")
        typed_artifact = {
            "path": str(run / "decision.json"),
            "artifact_type": "step_input",
            "producer": "PolicyAgent",
            "consumer": "ExecutorAgent",
            "step_name": "scx_executor",
            "handoff_id": "handoff-1",
            "artifact_role": "input",
            "required": True,
            "schema_version": "lockagent.runtime.v0.1",
            "sha256": "abc",
            "exists": True,
        }
        write(
            run / "resume_plan.json",
            {
                "execution_order": ["resume.step.1.scx_executor"],
                "dependency_order": [{"from": "artifact.input.1.decision.json", "to": "resume.step.1.scx_executor", "type": "artifact_dependency"}],
                "artifact_records": [typed_artifact],
                "resume_graph": {
                    "nodes": [{"id": "step.x"}, {"id": "artifact.input.1.decision.json", "records": [typed_artifact]}],
                    "edges": [{"from": "step.x", "to": "resume.x"}],
                    "dependency_edges": [{"from": "artifact.input.1.decision.json", "to": "resume.step.1.scx_executor", "type": "artifact_dependency"}],
                    "execution_order": ["resume.step.1.scx_executor"],
                    "artifact_records": [typed_artifact],
                },
            },
        )
        write(run / "resume_audit.json", {"dependency_graph": {"resume_dependency_edges": [{"from": "step.x", "to": "resume.x"}]}})
        write(
            run / "framework_adapter_bundle.json",
            {"framework_contracts": {"frameworks": {"langgraph": {"passed": True}}}},
        )
        write(
            run / "trace_schema_alignment.json",
            {"checks": {"durable_execution": {"passed": True}}},
        )
        write(
            run / "runtime_policy_preflight.json",
            {"policy_engine": {"checks": [{"name": "scheduler_allowed", "passed": True}]}},
        )
        write(run / "execution" / "guard_preflight.json", {"verdict": "allow", "policy_engine": {"checks": []}})
        write(run / "execution" / "guard_postflight.json", {"verdict": "allow", "policy_engine": {"checks": []}})
        write(run / "artifact_store.json", {"artifacts": [typed_artifact]})
        write(
            run / "provenance.json",
            {
                "dependency_graph": {
                    "runtime_cross_reference": {
                        "runtime_step_nodes": [{"id": "runtime_step.1.scx_executor", "type": "runtime_step"}],
                        "artifact_record_nodes": [{"id": "artifact_record.1.step_input", "type": "artifact_record"}],
                        "edges": [{"from": "artifact_record.1.step_input", "to": "runtime_step.1.scx_executor", "type": "runtime_consumes_artifact"}],
                    },
                    "runtime_cross_reference_edges": [
                        {"from": "artifact_record.1.step_input", "to": "runtime_step.1.scx_executor", "type": "runtime_consumes_artifact"}
                    ],
                    "execution_edges": [{"from": "decision.scheduler", "to": "action.scheduler_startup"}],
                    "dependency_edges": [{"from": "profile.signals", "to": "decision.scheduler"}],
                    "regression_blame_chains": [{"path": ["profile.signals", "diagnosis.performance_regression"]}],
                }
            },
        )
        write(
            run / "causal_attribution.json",
            {"summary": {"bad_profile_bad_skill_bad_scheduler_regression_path": True}},
        )
        write(run / "skill_curation.json", {"skill": "choose_locknest_for_hot_lock_oltp"})
        write(run / "skill_repository_update.json", {"updated": True})
        write(run / "skill_evolution_application.json", {"applied_count": 1})
        write(run / "system_evolution_state.json", {"history": [{}]})
        write(run / "systemic_override_application.json", {"applied_count": 1})
        write(run / "worker_pool_result.json", {"status": "ok"})

        result = module.evaluate_run(run)
        assert result["summary"]["status"] == "native_not_weaker", result
        assert result["summary"]["parity_score"] == 1.0, result
        assert result["summary"]["advantage_score"] == 1.0, result
        assert result["summary"]["failed_parity"] == [], result
        assert result["summary"]["failed_advantage"] == [], result
        assert result["langgraph_backend"]["backend_invocation_class"] == "unavailable", result

        empty = Path(td) / "run_empty_resume"
        empty.mkdir()
        (empty / "durable_runtime.db").write_bytes(b"sqlite-placeholder")
        write(
            empty / "agent_run.json",
            {
                "status": "ok",
                "graph": {
                    "nodes": ["ProfilerAgent"],
                    "edges": [],
                    "step_nodes": [{"id": "step_template.0.profile_builder", "name": "profile_builder", "role": "ProfilerAgent"}],
                    "step_edges": [],
                },
                "steps": [
                    {
                        "name": "profile_builder",
                        "role": "ProfilerAgent",
                        "status": "ok",
                        "metadata": {"execution_graph_node": "step_template.0.profile_builder"},
                    }
                ],
            },
        )
        write(
            empty / "langgraph_state.json",
            {
                "thread_id": "run_empty_resume",
                "checkpoint": str(empty / "agent_run.json"),
                "metadata": {"graph": {"nodes": ["ProfilerAgent"], "edges": []}},
            },
        )
        write(
            empty / "native_langgraph_result.json",
            {
                "status": "ok",
                "backend_validation_source": "real_langgraph",
                "native_runtime_available": True,
                "invoked": True,
                "resumed": True,
                "state_observed": True,
            },
        )
        write(empty / "durable_runtime_summary.json", {"checkpoint_count": 2, "step_count": 1, "task_count": 0})
        write(
            empty / "execution_graph_runtime.json",
            {
                "observed_nodes": [{"id": "runtime_step.0.profile_builder", "name": "profile_builder"}],
                "observed_control_edges": [],
                "observed_artifact_dependency_edges": [],
                "summary": {"observed_step_count": 1, "declared_step_coverage": 1.0},
            },
        )
        write(
            empty / "graph_scheduler_plan.json",
            {
                "declared_steps": [
                    {
                        "id": "step_template.0.profile_builder",
                        "name": "profile_builder",
                        "role": "ProfilerAgent",
                        "status": "ok",
                        "observed": True,
                    }
                ],
                "artifact_dependency_edges": [],
                "failed_step_replay_candidates": [],
                "next_runnable_steps": [],
                "summary": {
                    "declared_step_count": 1,
                    "observed_declared_step_count": 1,
                    "completed_step_count": 1,
                    "waiting_step_count": 0,
                    "blocked_step_count": 0,
                    "runnable_step_count": 0,
                    "next_runnable_count": 0,
                    "artifact_dependency_edge_count": 0,
                    "failed_step_replay_candidate_count": 0,
                },
            },
        )
        write(empty / "typed_event_bus.json", {"events": [{"event_type": "handoff"}]})
        (empty / "typed_event_bus.jsonl").write_text(json.dumps({"event_type": "handoff"}) + "\n", encoding="utf-8")
        write(
            empty / "resume_plan.json",
            {
                "execution_order": [],
                "dependency_order": [],
                "artifact_records": [],
                "queued_command_tasks": [],
                "resume_graph": {"nodes": [], "edges": [], "dependency_edges": [], "execution_order": [], "artifact_records": []},
            },
        )
        write(empty / "resume_audit.json", {"status": "nothing_to_resume", "dependency_graph": {"nodes": [], "edges": []}})
        write(empty / "framework_adapter_bundle.json", {"framework_contracts": {"frameworks": {"langgraph": {"passed": True}}}})
        write(empty / "trace_schema_alignment.json", {"checks": {"durable_execution": {"passed": True}}})
        write(empty / "runtime_policy_preflight.json", {"policy_engine": {"checks": [{"name": "scheduler_allowed", "passed": True}]}})
        write(empty / "execution" / "guard_preflight.json", {"verdict": "allow", "policy_engine": {"checks": []}})
        write(empty / "execution" / "guard_postflight.json", {"verdict": "allow", "policy_engine": {"checks": []}})
        write(empty / "artifact_store.json", {"artifacts": []})
        write(
            empty / "provenance.json",
            {
                "dependency_graph": {
                    "runtime_cross_reference": {
                        "runtime_step_nodes": [{"id": "runtime_step.0.profile_builder", "type": "runtime_step"}],
                        "artifact_record_nodes": [{"id": "artifact_record.synthetic", "type": "artifact_record"}],
                    },
                    "runtime_cross_reference_edges": [{"from": "runtime_step.0.profile_builder", "to": "artifact_record.synthetic"}],
                    "execution_edges": [{"from": "profile", "to": "decision"}],
                    "dependency_edges": [{"from": "ledger", "to": "profile"}],
                    "regression_blame_chains": [{"path": ["profile", "outcome"]}],
                }
            },
        )
        write(empty / "causal_attribution.json", {"summary": {"bad_profile_bad_skill_bad_scheduler_regression_path": True}})
        write(empty / "skill_curation.json", {"skill": "fallback_default"})
        write(empty / "skill_evolution_application.json", {"applied_count": 0})
        write(empty / "system_evolution_state.json", {"history": []})
        write(empty / "systemic_override_application.json", {"applied_count": 0})
        write(empty / "worker_pool_result.json", {"status": "ok"})

        empty_result = module.evaluate_run(empty)
        empty_checks = {item["name"]: item for item in empty_result["parity"]["checks"]}
        assert empty_checks["step_level_resume"]["passed"] is True, empty_result
        assert empty_checks["resume_execution_dependency_order"]["passed"] is True, empty_result

        weak = Path(td) / "run_weak"
        weak.mkdir()
        write(weak / "agent_run.json", {"status": "ok"})
        weak_result = module.evaluate_run(weak)
        assert weak_result["summary"]["status"] == "native_gap_detected", weak_result
        assert "checkpoint_persistence" in weak_result["summary"]["failed_parity"], weak_result

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local checks for process-level evaluation suite."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
MODULE_PATH = KERNADAPT_ROOT / "analysis" / "process_evaluation.py"
spec = importlib.util.spec_from_file_location("process_evaluation", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def policy_doc(verdict: str = "allow") -> dict:
    checks = [
        {
            "name": "decision_exists",
            "severity": "block",
            "category": "schema",
            "passed": True,
            "evidence_source": "decision.json",
            "remediation": "Generate decision artifact.",
            "failure_category": "policy_reasoning_failure",
            "owner_role": "CriticAgent",
        },
        {
            "name": "evidence_resolves_to_profile_or_skill",
            "severity": "block",
            "category": "profile_evidence",
            "passed": True,
            "evidence_source": "decision.evidence_used",
            "remediation": "Use resolvable evidence refs.",
            "failure_category": "policy_reasoning_failure",
            "owner_role": "CriticAgent",
        },
        {
            "name": "no_oracle_leakage",
            "severity": "block",
            "category": "oracle_leakage",
            "passed": True,
            "evidence_source": "profile.data_provenance",
            "remediation": "Rebuild profile without oracle fields.",
            "failure_category": "comparison_invalid",
            "owner_role": "CriticAgent",
        },
        {
            "name": "lock_signal_required_for_scx",
            "severity": "block",
            "category": "profile_evidence",
            "passed": True,
            "evidence_source": "profile.signals",
            "remediation": "Collect lock signals or choose default.",
            "failure_category": "profile_failure",
            "owner_role": "RuntimeGuard",
        },
        {
            "name": "benchmark_log_preserved",
            "severity": "warn",
            "category": "postflight_cleanup",
            "passed": True,
            "evidence_source": "execution/benchmark.log",
            "remediation": "Preserve benchmark log.",
            "failure_category": "benchmark_failure",
            "owner_role": "RuntimeGuard",
        },
    ]
    return {
        "verdict": verdict,
        "policy_engine": {
            "verdict": verdict,
            "checks": checks,
            "blocking_reasons": [],
            "warnings": [],
        },
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        run = Path(td) / "run_1"
        run.mkdir()
        for name in module.REQUIRED_ARTIFACTS:
            path = run / name
            if name in {"runtime_trace.jsonl", "typed_event_bus.jsonl"}:
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "typed_event_bus.jsonl":
                    path.write_text(
                        json.dumps({"event_type": "handoff"}) + "\n"
                        + json.dumps({"event_type": "step_start"}) + "\n"
                        + json.dumps({"event_type": "step_end"}) + "\n"
                        + json.dumps({"event_type": "runtime_api_adapter.invoke"}) + "\n",
                        encoding="utf-8",
                    )
                else:
                    path.write_text("{}", encoding="utf-8")
            elif name == "durable_runtime.db":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"sqlite-placeholder")
            else:
                write(path, {})
        (run / "runtime_service_events.jsonl").write_text(
            (
                json.dumps({"event": "http_hitl_approve"}) + "\n"
                + json.dumps({"event": "http_worker_once"}) + "\n"
                + json.dumps({"event": "http_worker_lease"}) + "\n"
                + json.dumps({"event": "http_worker_complete"}) + "\n"
                + json.dumps({"event": "http_adapter_invoke", "framework": "openai_agents"}) + "\n"
            ),
            encoding="utf-8",
        )
        (run.parent / "runtime_service_events.jsonl").write_text(
            (
                json.dumps({"event": "http_auth_failure"}) + "\n"
                + json.dumps({"event": "http_memory_query"}) + "\n"
                + json.dumps({"event": "http_memory_summary"}) + "\n"
                + json.dumps({"event": "http_memory_retrieve"}) + "\n"
                + json.dumps({"event": "http_memory_append"}) + "\n"
                + json.dumps({"event": "http_worker_register", "worker_id": "worker-a"}) + "\n"
                + json.dumps({"event": "http_worker_registry_heartbeat", "worker_id": "worker-a"}) + "\n"
            ),
            encoding="utf-8",
        )
        write(
            run.parent / "worker_registry.json",
            {
                "workers": {
                    "worker-a": {"worker_id": "worker-a", "last_seen_epoch": 1},
                    "worker-b": {"worker_id": "worker-b", "last_seen_epoch": 2},
                }
            },
        )
        write(run / "decision.json", {"scheduler": "default", "rollback_conditions": ["rollback"]})
        memory_isolation = {
            "schema_version": "lockagent.memory_isolation.v0.1",
            "enabled": True,
            "mode": "run_local_defaults",
            "dry_run": True,
            "memory_dir": str(run / "memory"),
            "explicit": {
                "global_memory": False,
                "global_memory_summary": False,
                "systemic_overrides": False,
                "system_evolution_state": False,
            },
            "rewritten": {
                "global_memory": {
                    "from": "kernadapt/results/memory/global_memory.jsonl",
                    "to": str(run / "memory" / "global_memory.jsonl"),
                },
                "global_memory_summary": {
                    "from": "kernadapt/results/memory/global_memory_summary.json",
                    "to": str(run / "memory" / "global_memory_summary.json"),
                },
            },
            "paths": {
                "global_memory": str(run / "memory" / "global_memory.jsonl"),
                "global_memory_summary": str(run / "memory" / "global_memory_summary.json"),
                "systemic_overrides": str(run / "memory" / "systemic_overrides.json"),
                "system_evolution_state": str(run / "memory" / "system_evolution_state.json"),
            },
        }
        write(
            run / "online_manifest.json",
            {
                "run_id": "run_1",
                "dry_run": True,
                "global_memory": str(run / "memory" / "global_memory.jsonl"),
                "memory_isolation": memory_isolation,
            },
        )
        write(run / "runtime_policy_preflight.json", policy_doc())
        write(run / "execution" / "guard_preflight.json", policy_doc())
        write(run / "execution" / "guard_postflight.json", policy_doc())
        write(
            run / "execution" / "status.json",
            {
                "status": "ok",
                "returncode": 0,
                "executor_failure_class": None,
                "final_sched_ext_state": "disabled",
            },
        )
        write(run / "trajectory_status.json", {"valid": True})
        write(run / "failure_attribution.json", {"categories": []})
        write(
            run / "fallback_artifacts.json",
            {
                "schema_version": "lockagent.fallback_artifacts.v0.1",
                "run_id": "run_1",
                "created": False,
                "records": [],
                "safe_for_performance_claims": True,
            },
        )
        write(run / "skill_curation.json", {"skill": "fallback_default_for_non_lock_bound", "verdict": "positive_evidence"})
        write(
            run / "provenance.json",
            {
                "dependency_graph": {
                    "nodes": [{"id": "profile.signals"}, {"id": "failure.skill_misfire"}],
                    "edges": [{"from": "profile.signals", "to": "failure.skill_misfire", "type": "blame_path"}],
                    "execution_edges": [{"from": "decision.scheduler", "to": "action.load_scx"}],
                    "dependency_edges": [{"from": "profile.signals", "to": "decision.scheduler"}],
                    "handoff_edges": [{"from": "profile.json", "to": "step.llm_policy_agent"}],
                    "artifact_edges": [{"from": "step.llm_policy_agent", "to": "artifact.decision"}],
                    "blame_edges": [{"from": "profile.signals", "to": "failure.skill_misfire"}],
                    "diagnostic_edges": [
                        {"from": "profile.signals", "to": "diagnosis.profile_quality", "type": "profile_quality_assessed"},
                        {"from": "diagnosis.profile_quality", "to": "skill.x.applicability", "type": "profile_conditions_skill"},
                        {"from": "skill.x.applicability", "to": "diagnosis.skill_applicability", "type": "skill_applicability_assessed"},
                        {"from": "diagnosis.skill_applicability", "to": "skill.x", "type": "skill_conditioned_by_profile"},
                        {"from": "skill.x", "to": "decision.scheduler", "type": "skill_guides_scheduler_choice"},
                        {"from": "decision.scheduler", "to": "diagnosis.scheduler_choice", "type": "scheduler_choice_assessed"},
                        {"from": "action.benchmark_run", "to": "outcome.benchmark", "type": "action_produces_benchmark_outcome"},
                        {"from": "outcome.benchmark", "to": "diagnosis.performance_regression", "type": "outcome_indicates_regression"},
                    ],
                    "blame_paths": [{"category": "skill_misfire", "path": ["profile.signals", "failure.skill_misfire"]}],
                    "fine_grained_blame_paths": [
                        {
                            "category": "skill_misfire",
                            "path": [
                                "profile.signals",
                                "skill.x.applicability",
                                "skill.x",
                                "decision.scheduler",
                                "action.scheduler_startup",
                                "outcome.scheduler_startup",
                                "action.benchmark_run",
                                "outcome.benchmark",
                                "action.rollback",
                                "outcome.rollback",
                                "failure.skill_misfire",
                            ],
                        }
                    ],
                    "regression_blame_chains": [
                        {
                            "category": "skill_misfire",
                            "chain_type": "bad_profile_bad_skill_bad_scheduler_regression",
                            "path": [
                                "profile.signals",
                                "diagnosis.profile_quality",
                                "skill.x.applicability",
                                "diagnosis.skill_applicability",
                                "skill.x",
                                "decision.scheduler",
                                "diagnosis.scheduler_choice",
                                "action.scheduler_startup",
                                "outcome.scheduler_startup",
                                "action.benchmark_run",
                                "outcome.benchmark",
                                "diagnosis.performance_regression",
                                "failure.skill_misfire",
                            ],
                        }
                    ],
                    "dependency_blame_graph": {
                        "nodes": [
                            {"id": "profile.signals"},
                            {"id": "diagnosis.profile_quality"},
                            {"id": "skill.x"},
                            {"id": "decision.scheduler"},
                            {"id": "outcome.benchmark"},
                            {"id": "diagnosis.performance_regression"},
                        ],
                        "edges": [
                            {"from": "profile.signals", "to": "diagnosis.profile_quality"},
                            {"from": "diagnosis.profile_quality", "to": "skill.x.applicability"},
                            {"from": "skill.x.applicability", "to": "diagnosis.skill_applicability"},
                            {"from": "diagnosis.skill_applicability", "to": "skill.x"},
                            {"from": "skill.x", "to": "decision.scheduler"},
                            {"from": "decision.scheduler", "to": "diagnosis.scheduler_choice"},
                            {"from": "outcome.benchmark", "to": "diagnosis.performance_regression"},
                            {"from": "diagnosis.performance_regression", "to": "failure.skill_misfire"},
                        ],
                    },
                    "causal_dependency_edges": [
                        {"from": "profile.signals", "to": "skill.x.applicability", "type": "conditions_skill_applicability"},
                        {"from": "action.benchmark_run", "to": "outcome.benchmark", "type": "causes_benchmark_outcome"},
                    ],
                    "causal_dependency_chains": [
                        {
                            "category": "skill_misfire",
                            "chain_type": "profile_skill_decision_action_outcome_failure",
                            "path": [
                                "profile.signals",
                                "skill.x.applicability",
                                "skill.x",
                                "decision.scheduler",
                                "action.scheduler_startup",
                                "outcome.scheduler_startup",
                                "action.benchmark_run",
                                "outcome.benchmark",
                                "action.rollback",
                                "outcome.rollback",
                                "failure.skill_misfire",
                            ],
                        }
                    ],
                }
            },
        )
        write(
            run / "causal_attribution.json",
            {
                "causal_chains": [
                    {"category": "skill_misfire", "evidence_strength": {"label": "strong_graph_support"}}
                ],
                "causal_blame_graph": {
                    "nodes": [{"id": "profile.signals"}, {"id": "skill.x"}, {"id": "decision.scheduler"}],
                    "edges": [{"from": "profile.signals", "to": "skill.x", "type": "causal_blame"}],
                },
                "summary": {
                    "strong_chain_count": 1,
                    "requires_same_batch_validation": False,
                    "validated_chain_count": 1,
                    "refuted_chain_count": 0,
                    "inconclusive_chain_count": 0,
                    "profile_skill_scheduler_outcome_path": True,
                    "profile_skill_decision_action_outcome_path": True,
                    "fine_grained_chain_count": 1,
                    "causal_dependency_chain_count": 1,
                    "regression_blame_chain_count": 1,
                    "bad_profile_bad_skill_bad_scheduler_regression_path": True,
                    "bad_profile_to_regression_path": True,
                    "skill_to_regression_path": True,
                    "scheduler_to_regression_path": True,
                    "root_cause_candidate_count": 1,
                },
            },
        )
        write(
            run / "agent_run.json",
            {
                "steps": [
                    {"role": "ProfilerAgent", "name": "state_ledger", "capability_verdict": {"denied": []}},
                    {
                        "role": "CuratorAgent",
                        "name": "skill_evolution_application",
                        "status": "skipped",
                        "capability_verdict": {"denied": []},
                        "metadata": {
                            "runtime_template_policy": True,
                            "skip_reason": "meta_template:critic_required_agent:disabled_stage:skill_evolution_application",
                        },
                    },
                ]
            },
        )
        write(run / "global_memory_record.json", {"run_id": "run_1"})
        write(run / "worker_complete.json", {"ok": True})
        write(
            run / "worker_pool_result.json",
            {
                "status": "ok",
                "worker_count": 3,
                "attempt_count": 5,
                "completed_count": 4,
                "failed_count": 0,
                "no_task_count": 1,
                "worker_iterations": {"pool-0": 2, "pool-1": 2, "pool-2": 1},
            },
        )
        write(
            run / "policy_audit.json",
            {
                "schema_version": "lockagent.policy_audit.v0.1",
                "status": "ok",
                "policy_rules": {
                    "required_rules_passed": True,
                    "metadata_complete": True,
                    "missing_required_rules": [],
                    "mismatched_required_rules": [],
                    "missing_metadata_rules": [],
                    "category_counts": {"schema": 5, "scheduler_allowlist": 3},
                    "owner_role_counts": {"CriticAgent": 9, "RuntimeGuard": 23},
                },
                "local_branch_audit": {
                    "branch_count": 29,
                    "unclassified_count": 0,
                    "classification_counts": {
                        "policy_engine_owned": 3,
                        "executor_mechanics": 5,
                        "artifact_fallback": 1,
                    },
                    "unclassified": [],
                },
            },
        )
        write(
            run / "resume_plan.json",
            {
                "planned_steps": [{"name": "failed_step"}],
                "durable_resume_view": {
                    "source": "durable_runtime.db",
                    "resumable_steps": [{"name": "failed_step"}],
                    "resumable_command_tasks": [{"task_id": "task-a"}],
                },
                "resume_graph": {
                    "nodes": [
                        {"id": "step.0.failed_step", "type": "checkpoint_step"},
                        {"id": "resume.step.0.failed_step", "type": "replay_action"},
                        {"id": "task.task-a", "type": "durable_task"},
                        {"id": "resume.task.task-a", "type": "queued_task_replay"},
                    ],
                    "edges": [
                        {"from": "step.0.failed_step", "to": "resume.step.0.failed_step", "type": "resume_dependency"},
                        {"from": "task.task-a", "to": "resume.task.task-a", "type": "resume_dependency"},
                        {"from": "resume.step.0.failed_step", "to": "artifact.output.failed_step", "type": "artifact_generation"},
                        {
                            "from": "selection.artifact_dependency_subgraph",
                            "to": "resume.step.0.failed_step",
                            "type": "replay_selection",
                        },
                    ],
                    "dependency_edges": [
                        {"from": "step.0.failed_step", "to": "resume.step.0.failed_step", "type": "resume_dependency"},
                        {"from": "task.task-a", "to": "resume.task.task-a", "type": "resume_dependency"},
                    ],
                    "execution_edges": [
                        {"from": "resume.step.0.failed_step", "to": "artifact.output.failed_step", "type": "artifact_generation"}
                    ],
                    "replay_selection_edges": [
                        {
                            "from": "selection.artifact_dependency_subgraph",
                            "to": "resume.step.0.failed_step",
                            "type": "replay_selection",
                        }
                    ],
                    "replayable_step_count": 1,
                    "non_replayable_step_count": 0,
                    "queued_task_replay_count": 1,
                    "skipped_unselected_step_count": 1,
                },
            },
        )
        write(run / "resume_result.json", {"status": "ok"})
        write(
            run / "resume_audit.json",
            {
                "status": "ok",
                "summary": {
                    "planned_step_count": 1,
                    "queued_task_count": 1,
                    "replayed_count": 2,
                    "safe_to_replay_count": 2,
                    "unsafe_to_replay_count": 0,
                    "skipped_count": 0,
                    "failed_count": 0,
                    "dependency_node_count": 6,
                    "dependency_edge_count": 5,
                    "replay_selection_edge_count": 1,
                    "skipped_unselected_step_count": 1,
                    "input_hash_changed_count": 0,
                    "input_hash_missing_count": 0,
                    "input_hash_unverified_count": 0,
                    "input_hash_ok_count": 2,
                    "side_effect_safe_count": 2,
                    "side_effect_unsafe_count": 0,
                    "scx_load_replay_count": 0,
                    "service_restart_replay_count": 0,
                    "benchmark_replay_count": 1,
                    "root_control_replay_count": 0,
                },
                "skipped_unselected_steps": [
                    {
                        "index": 2,
                        "name": "unrelated_ok",
                        "reason": "outside_artifact_dependency_subgraph",
                    }
                ],
                "dependency_graph": {
                    "resume_dependency_edges": [
                        {"from": "step.0.failed_step", "to": "resume.step.0.failed_step"},
                        {"from": "task.task-a", "to": "resume.task.task-a"},
                    ],
                    "artifact_dependency_edges": [
                        {"from": "artifact.input.0.profile.json", "to": "resume.step.0.failed_step"}
                    ],
                    "artifact_generation_edges": [
                        {"from": "resume.step.0.failed_step", "to": "artifact.output.0.out.json"},
                        {"from": "resume.task.task-a", "to": "artifact.task_output.task-a.out.json"},
                    ],
                    "replay_selection_edges": [
                        {
                            "from": "selection.artifact_dependency_subgraph",
                            "to": "resume.step.0.failed_step",
                            "type": "replay_selection",
                        }
                    ],
                },
            },
        )
        write(
            run / "same_batch_validation.json",
            {
                "trajectory_valid": True,
                "verdict": "positive_evidence",
                "validation": {"improvement_pct": 10.0},
                "comparison_arms": {
                    "arms": {
                        "default": {"role": "default_control"},
                        "fixed_locknest": {"role": "fixed_scx_treatment"},
                        "lockagent_online": {"role": "lockagent_online"},
                        "oracle_best": {"role": "oracle_best_evaluation_only"},
                    }
                },
            },
        )
        write(
            run / "counterfactual_plan.json",
            {
                "requires_counterfactual": True,
                "task_count": 2,
                "executable_task_count": 1,
                "submission": {"submitted_count": 1},
                "skill_evolution": {
                    "update_repository": True,
                    "repository": "kernadapt/skills/scheduler_skill_cards.json",
                },
                "executable_tasks": [
                    {
                        "payload": {
                            "skill_evolution": {
                                "update_repository": True,
                                "repository": "kernadapt/skills/scheduler_skill_cards.json",
                            }
                        }
                    }
                ],
            },
        )
        write(run / "counterfactual_execution.json", {"status": "ok", "dry_run": True})
        evolution_ledger = run / "skill_evolution_ledger.jsonl"
        evolution_ledger.write_text(json.dumps({"transition": "candidate->validated"}) + "\n", encoding="utf-8")
        write(
            run / "skill_repository_update.json",
            {
                "updated": True,
                "repository_mutated": True,
                "transition": "candidate->validated",
                "validation_gate": {"passed": True},
                "evolution_ledger": str(evolution_ledger),
            },
        )
        write(
            run / "skill_evolution_application.json",
            {
                "dry_run": False,
                "applied_count": 2,
                "skipped_count": 1,
                "transition_count": 1,
                "transitions": [
                    {
                        "skill": "fallback_default_for_non_lock_bound",
                        "transition": "candidate->validated",
                    }
                ],
            },
        )
        write(
            run / "self_evolution_plan.json",
            {
                "systemic_actions": [{"action": "require_strict_critic_preflight_before_guard"}],
                "meta_template": "critic_required_agent",
                "systemic_overrides": {
                    "require_environment_compatibility": True,
                    "require_strict_critic": True,
                    "scheduler_disablelist": ["scx_lockcoop"],
                    "meta_template": "critic_required_agent",
                },
            },
        )
        write(
            run / "system_evolution_state.json",
            {
                "selected_meta_template": "critic_required_agent",
                "history": [{"root": str(run), "run_count": 1}],
                "override_history": [{"root": str(run), "action_count": 1}],
                "template_history": [{"candidate": "critic_required_agent", "selected": "critic_required_agent"}],
                "failure_totals": {"policy_reasoning_failure": 1, "environment_mismatch": 1},
                "template_scores": {"critic_required_agent": 2, "guarded_policy_agent": 1},
            },
        )
        write(
            run / "systemic_override_application.json",
            {
                "ignored": False,
                "memory_isolation": memory_isolation,
                "normalized_overrides": {
                    "require_environment_compatibility": True,
                    "require_strict_critic": True,
                    "scheduler_disablelist": ["scx_lockcoop"],
                    "meta_template": "critic_required_agent",
                },
                "applied": [
                    {"field": "require_environment_compatibility"},
                    {"field": "require_strict_critic"},
                    {"field": "scheduler_disablelist"},
                    {"field": "meta_template"},
                ],
                "applied_count": 4,
                "profile_s": {"original": 2, "final": 4},
                "meta_template": "critic_required_agent",
                "runtime_template_policy": {
                    "template": "critic_required_agent",
                    "requires_critic_preflight": True,
                    "run_skill_curator": False,
                    "run_causal_attribution": True,
                    "run_counterfactual_planner": False,
                    "run_skill_evolution_application": False,
                    "mode": "critic_required_runtime_control",
                },
                "execution_graph": {
                    "nodes": ["ProfilerAgent", "PolicyAgent", "CriticAgent"],
                    "edges": [
                        {"from": "ProfilerAgent", "to": "PolicyAgent"},
                        {"from": "PolicyAgent", "to": "CriticAgent"},
                    ],
                    "step_nodes": [
                        {"id": "step_template.0.state_ledger", "name": "state_ledger", "role": "ProfilerAgent"},
                        {"id": "step_template.1.llm_policy_agent", "name": "llm_policy_agent", "role": "PolicyAgent"},
                        {"id": "step_template.2.critic_preflight", "name": "critic_preflight", "role": "CriticAgent"},
                    ],
                    "step_edges": [
                        {"from": "step_template.0.state_ledger", "to": "step_template.1.llm_policy_agent", "type": "control_dependency"},
                        {"from": "step_template.1.llm_policy_agent", "to": "step_template.2.critic_preflight", "type": "control_dependency"},
                    ],
                },
            },
        )
        write(
            run / "execution_graph_runtime.json",
            {
                "observed_nodes": [{"id": "runtime_step.0.state_ledger"}, {"id": "runtime_step.1.llm_policy_agent"}],
                "observed_control_edges": [{"from": "runtime_step.0.state_ledger", "to": "runtime_step.1.llm_policy_agent"}],
                "observed_artifact_dependency_edges": [{"from": "runtime_step.0.state_ledger", "to": "runtime_step.1.llm_policy_agent"}],
                "summary": {
                    "declared_step_node_count": 3,
                    "observed_declared_step_count": 2,
                    "observed_step_count": 2,
                    "declared_step_coverage": 0.667,
                    "observed_control_edge_count": 1,
                    "observed_artifact_dependency_edge_count": 1,
                },
            },
        )
        write(
            run / "graph_scheduler_plan.json",
            {
                "declared_steps": [
                    {
                        "id": "step_template.0.state_ledger",
                        "name": "state_ledger",
                        "role": "ProfilerAgent",
                        "status": "ok",
                        "observed": True,
                    },
                    {
                        "id": "step_template.1.llm_policy_agent",
                        "name": "llm_policy_agent",
                        "role": "PolicyAgent",
                        "status": "ok",
                        "observed": True,
                    },
                    {
                        "id": "step_template.2.critic_preflight",
                        "name": "critic_preflight",
                        "role": "CriticAgent",
                        "status": "runnable",
                        "observed": False,
                    },
                ],
                "artifact_dependency_edges": [
                    {
                        "from": "step_template.0.state_ledger",
                        "to": "step_template.1.llm_policy_agent",
                        "type": "artifact_dependency",
                        "artifact": "profile.json",
                    }
                ],
                "failed_step_replay_candidates": [
                    {
                        "source_step_id": "step_template.0.state_ledger",
                        "source_name": "state_ledger",
                        "source_status": "failed",
                        "selection_policy": "artifact_dependency_subgraph",
                        "candidate_step_ids": [
                            "step_template.0.state_ledger",
                            "step_template.1.llm_policy_agent",
                        ],
                    }
                ],
                "next_runnable_steps": [
                    {"id": "step_template.2.critic_preflight", "name": "critic_preflight", "role": "CriticAgent"}
                ],
                "summary": {
                    "declared_step_count": 3,
                    "observed_declared_step_count": 2,
                    "completed_step_count": 2,
                    "skipped_step_count": 0,
                    "failed_step_count": 0,
                    "blocked_step_count": 0,
                    "waiting_step_count": 0,
                    "runnable_step_count": 1,
                    "next_runnable_count": 1,
                    "artifact_dependency_edge_count": 1,
                    "failed_step_replay_candidate_count": 1,
                    "missing_required_artifact_count": 0,
                },
            },
        )
        write(
            run / "memory_store.json",
            {
                "items": [
                    {"namespace": "policy_decisions", "key": "run_1", "value": {"scheduler": "default"}},
                    {"namespace": "skill_evidence", "key": "fallback_default_for_non_lock_bound", "value": {}},
                ]
            },
        )
        write(
            run / "durable_runtime_summary.json",
            {
                "checkpoint_count": 7,
                "step_count": 5,
                "event_count": 18,
                "handoff_count": 4,
                "task_count": 3,
                "leased_task_count": 1,
                "worker_count": 2,
                "step_status_counts": {"ok": 5},
                "task_status_counts": {"queued": 1, "running": 1, "ok": 1},
            },
        )
        write(
            run / "typed_event_bus.json",
            {
                "events": [
                    {"event_type": "handoff"},
                    {"event_type": "step_start"},
                    {"event_type": "step_end"},
                    {"event_type": "runtime_api_adapter.invoke"},
                ]
            },
        )
        write(
            run / "runtime_api_adapter_result.json",
            {
                "framework": "openai_agents",
                "invocation_type": "tool_invoke",
                "status": "submitted",
                "task": {"task_id": "adapter-task"},
                "resume": {"status": "ok"},
            },
        )
        write(
            run / "native_langgraph_result.json",
            {
                "status": "ok",
                "backend_validation_source": "real_langgraph",
                "graph_mode": "sequential_calibration",
                "native_runtime_available": True,
                "invoked": True,
                "resumed": True,
                "state_observed": True,
            },
        )
        write(
            run / "runtime_parity.json",
            {
                "summary": {
                    "status": "native_not_weaker",
                    "parity_score": 1.0,
                    "advantage_score": 1.0,
                    "failed_parity": [],
                    "failed_advantage": [],
                }
            },
        )
        write(
            run / "agent_scheduler.json",
            {
                "tasks": [],
                "history": [
                    {"event": "step_scheduled", "step_name": "state_ledger"},
                    {"event": "step_completed", "step_name": "state_ledger"},
                    {"event": "task_leased", "task_id": "worker-task", "worker_id": "worker-a"},
                    {"event": "task_heartbeat", "task_id": "worker-task", "worker_id": "worker-a"},
                    {"event": "task_lease_expired", "task_id": "worker-task"},
                ],
            },
        )
        write(run / "openai_agents_trace.json", {"schema": "openai_agents_trace_compatible_v0", "spans": []})
        write(run / "langgraph_state.json", {"schema": "langgraph_state_compatible_v0", "values": {}})
        write(
            run / "framework_adapter_bundle.json",
            {
                "schema_version": "lockagent.framework_adapters.v0.1",
                "coverage": {
                    "status": "aligned",
                    "coverage_score": 1.0,
                    "contract_status": "aligned",
                    "contract_score": 1.0,
                    "concept_count": {
                        "openai_agents": 5,
                        "langgraph": 5,
                        "autogen_core": 4,
                        "crewai": 5,
                    },
                },
                "framework_contracts": {
                    "status": "aligned",
                    "contract_score": 1.0,
                    "check_count": 4,
                    "passed_count": 4,
                    "checks": [
                        {"name": "openai.handoffs_match_execution_graph", "passed": True},
                        {"name": "langgraph.checkpoint", "passed": True},
                        {"name": "autogen.subscriptions_cover_graph_nodes", "passed": True},
                        {"name": "crewai.memory_paths", "passed": True},
                    ],
                    "frameworks": {
                        "openai_agents": {"passed": True},
                        "langgraph": {"passed": True},
                        "autogen_core": {"passed": True},
                        "crewai": {"passed": True},
                    },
                },
                "openai_agents": {},
                "langgraph": {},
                "autogen_core": {},
                "crewai": {},
            },
        )
        write(
            run / "trace_schema_alignment.json",
            {
                "status": "aligned",
                "alignment_score": 1.0,
                "checks": {
                    "openai_agents": {"passed": True},
                    "langgraph": {"passed": True},
                    "autogen_core": {"passed": True},
                    "crewai": {"passed": True},
                    "typed_handoff": {"passed": True},
                    "durable_execution": {"passed": True},
                    "typed_event_bus": {"passed": True},
                    "resume_graph": {"passed": True},
                    "framework_adapter_bundle": {"passed": True},
                },
            },
        )
        batch = Path(td) / "batch_1"
        batch.mkdir()
        write(
            batch / "batch_readiness.json",
            {
                "schema_version": "lockagent.same_batch_readiness.v0.1",
                "status": "blocked",
                "blocking_reasons": ["sudo_noninteractive_available"],
                "checks": [
                    {
                        "name": "sysbench_available",
                        "passed": True,
                        "severity": "block",
                        "evidence": "/usr/bin/sysbench",
                        "remediation": "install sysbench",
                    },
                    {
                        "name": "sudo_noninteractive_available",
                        "passed": False,
                        "severity": "block",
                        "evidence": {"returncode": 1, "stderr": "sudo: a password is required"},
                        "remediation": "enable passwordless sudo",
                    },
                ],
            },
        )
        write(
            batch / "batch_manifest.json",
            {
                "batch_id": "batch_1",
                "status": "blocked",
                "returncode": 2,
                "check_only": True,
                "dry_run": False,
                "arms": ["default", "fixed_locknest"],
                "trials": [],
            },
        )

        row = module.summarize_run(run)
        core_row = module.summarize_run(run, evaluation_mode="core-online")
        paper_row = module.summarize_run(run, evaluation_mode="paper-evidence")
        batch_row = module.summarize_batch(batch)
        assert batch_row["readiness_status"] == "blocked", batch_row
        assert batch_row["sudo_noninteractive_available"] is False, batch_row
        assert batch_row["readiness_blocking_checks"] == ["sudo_noninteractive_available"], batch_row
        batch_agg = module.aggregate_batches([batch_row])
        assert batch_agg["batch_count"] == 1, batch_agg
        assert batch_agg["readiness_record_rate"] == 1.0, batch_agg
        assert batch_agg["readiness_blocked_rate"] == 1.0, batch_agg
        assert batch_agg["sudo_noninteractive_blocked_count"] == 1, batch_agg
        assert batch_agg["readiness_blocking_reason_distribution"] == {"sudo_noninteractive_available": 1}, batch_agg

        assert row["artifact_complete"] is True, row
        assert row["evaluation_mode"] == "full", row
        assert row["evaluation_artifact_complete"] is True, row
        assert row["evaluation_missing_artifact_count"] == 0, row
        assert row["evaluation_missing_artifacts"] == [], row
        assert row["evaluation_optional_missing_artifacts"] == [], row
        assert core_row["evaluation_mode"] == "core-online", core_row
        assert core_row["evaluation_artifact_complete"] is True, core_row
        assert "native_langgraph_result.json" in core_row["evaluation_optional_missing_artifacts"] or core_row["native_langgraph_result_exists"] is True, core_row
        assert paper_row["evaluation_mode"] == "paper-evidence", paper_row
        assert paper_row["evaluation_artifact_complete"] is False, paper_row
        assert "execution/benchmark/sysbench.log" in paper_row["evaluation_missing_artifacts"], paper_row
        assert row["optional_process_artifact_complete"] is True, row
        assert row["optional_process_missing_artifacts"] == [], row
        assert row["fallback_artifacts_exists"] is True, row
        assert row["fallback_artifacts_created"] is False, row
        assert row["fallback_artifact_count"] == 0, row
        assert row["fallback_safe_for_performance_claims"] is True, row
        assert row["valid_decision"] is True, row
        assert row["rollback_success"] is True, row
        assert row["executor_status"] == "ok", row
        assert row["executor_returncode"] == 0, row
        assert row["executor_failure_class"] is None, row
        assert row["executor_error_recorded"] is False, row
        assert row["policy_engine_check_count"] == 15, row
        assert row["policy_engine_rule_count"] == 5, row
        assert row["policy_engine_rule_categories"] == [
            "oracle_leakage",
            "postflight_cleanup",
            "profile_evidence",
            "schema",
        ], row
        assert row["policy_engine_rule_owner_roles"] == ["CriticAgent", "RuntimeGuard"], row
        assert row["policy_engine_missing_metadata_count"] == 0, row
        assert row["policy_engine_has_oracle_leakage_rule"] is True, row
        assert row["policy_engine_has_evidence_resolution_rule"] is True, row
        assert row["policy_engine_has_lock_signal_rule"] is True, row
        assert row["policy_audit_exists"] is True, row
        assert row["policy_audit_status"] == "ok", row
        assert row["policy_audit_required_rules_passed"] is True, row
        assert row["policy_audit_metadata_complete"] is True, row
        assert row["policy_audit_missing_required_rule_count"] == 0, row
        assert row["policy_audit_mismatched_required_rule_count"] == 0, row
        assert row["policy_audit_missing_metadata_rule_count"] == 0, row
        assert row["policy_audit_local_branch_count"] == 29, row
        assert row["policy_audit_unclassified_branch_count"] == 0, row
        assert row["policy_audit_branch_classifications"] == {
            "policy_engine_owned": 3,
            "executor_mechanics": 5,
            "artifact_fallback": 1,
        }, row
        assert row["memory_isolation_source"] == "online_manifest", row
        assert row["memory_mode"] == "run_local_defaults", row
        assert row["memory_isolation_enabled"] is True, row
        assert row["memory_isolation_dry_run"] is True, row
        assert row["memory_path_scope"] == "run_local", row
        assert row["memory_explicit_path_count"] == 0, row
        assert row["memory_rewritten_path_count"] == 2, row
        assert row["memory_isolated_for_dry_run"] is True, row
        assert row["dry_run_memory_shared_path_risk"] is False, row
        assert row["production_shared_memory"] is False, row
        assert row["memory_item_count"] == 2, row
        assert row["durable_checkpoint_count"] == 7, row
        assert row["durable_step_count"] == 5, row
        assert row["durable_event_count"] == 18, row
        assert row["durable_handoff_count"] == 4, row
        assert row["durable_task_count"] == 3, row
        assert row["durable_leased_task_count"] == 1, row
        assert row["durable_worker_count"] == 2, row
        assert row["typed_event_count"] == 4, row
        assert row["typed_handoff_event_count"] == 1, row
        assert row["typed_step_start_event_count"] == 1, row
        assert row["typed_step_end_event_count"] == 1, row
        assert row["durable_resume_step_count"] == 1, row
        assert row["durable_resume_task_count"] == 1, row
        assert row["durable_resume_source"] == "durable_runtime.db", row
        assert row["resume_graph_node_count"] == 4, row
        assert row["resume_graph_edge_count"] == 4, row
        assert row["resume_graph_dependency_edge_count"] == 2, row
        assert row["resume_graph_execution_edge_count"] == 1, row
        assert row["resume_graph_replayable_step_count"] == 1, row
        assert row["resume_graph_queued_task_replay_count"] == 1, row
        assert row["resume_graph_skipped_unselected_step_count"] == 1, row
        assert row["resume_graph_replay_selection_edge_count"] == 1, row
        assert row["resume_audit_exists"] is True, row
        assert row["resume_audit_status"] == "ok", row
        assert row["resume_audit_planned_step_count"] == 1, row
        assert row["resume_audit_replayed_count"] == 2, row
        assert row["resume_audit_safe_to_replay_count"] == 2, row
        assert row["resume_audit_unsafe_to_replay_count"] == 0, row
        assert row["resume_audit_dependency_node_count"] == 6, row
        assert row["resume_audit_dependency_edge_count"] == 5, row
        assert row["resume_audit_resume_dependency_edge_count"] == 2, row
        assert row["resume_audit_artifact_dependency_edge_count"] == 1, row
        assert row["resume_audit_artifact_generation_edge_count"] == 2, row
        assert row["resume_audit_replay_selection_edge_count"] == 1, row
        assert row["resume_audit_skipped_unselected_step_count"] == 1, row
        assert row["resume_audit_input_hash_changed_count"] == 0, row
        assert row["resume_audit_input_hash_missing_count"] == 0, row
        assert row["resume_audit_input_hash_unverified_count"] == 0, row
        assert row["resume_audit_input_hash_ok_count"] == 2, row
        assert row["resume_audit_side_effect_safe_count"] == 2, row
        assert row["resume_audit_side_effect_unsafe_count"] == 0, row
        assert row["resume_audit_scx_load_replay_count"] == 0, row
        assert row["resume_audit_service_restart_replay_count"] == 0, row
        assert row["resume_audit_benchmark_replay_count"] == 1, row
        assert row["resume_audit_root_control_replay_count"] == 0, row
        assert row["scheduler_history_count"] == 5, row
        assert row["worker_lease_events"] == 1, row
        assert row["worker_heartbeat_events"] == 1, row
        assert row["worker_lease_expired_events"] == 1, row
        assert row["runtime_service_event_count"] == 5, row
        assert row["root_runtime_service_event_count"] == 7, row
        assert row["http_auth_failure_events"] == 1, row
        assert row["http_memory_query_events"] == 1, row
        assert row["http_memory_summary_events"] == 1, row
        assert row["http_memory_retrieve_events"] == 1, row
        assert row["http_memory_append_events"] == 1, row
        assert row["http_hitl_approve_events"] == 1, row
        assert row["http_worker_once_events"] == 1, row
        assert row["http_worker_lease_events"] == 1, row
        assert row["http_worker_complete_events"] == 1, row
        assert row["http_worker_register_events"] == 1, row
        assert row["http_worker_registry_heartbeat_events"] == 1, row
        assert row["http_adapter_invoke_events"] == 1, row
        assert row["runtime_api_adapter_result_exists"] is True, row
        assert row["runtime_api_adapter_framework"] == "openai_agents", row
        assert row["runtime_api_adapter_invocation_type"] == "tool_invoke", row
        assert row["runtime_api_adapter_status"] == "submitted", row
        assert row["runtime_api_adapter_submitted_task_count"] == 1, row
        assert row["runtime_api_adapter_resume_status"] == "ok", row
        assert row["runtime_api_adapter_event_count"] == 1, row
        assert row["native_langgraph_result_exists"] is True, row
        assert row["native_langgraph_status"] == "ok", row
        assert row["native_langgraph_backend_validation_source"] == "real_langgraph", row
        assert row["native_langgraph_backend_invocation_class"] == "real_backend_invoked", row
        assert row["native_langgraph_graph_mode"] == "sequential_calibration", row
        assert row["native_langgraph_available"] is True, row
        assert row["native_langgraph_invoked"] is True, row
        assert row["native_langgraph_resumed"] is True, row
        assert row["native_langgraph_state_observed"] is True, row
        assert row["runtime_parity_exists"] is True, row
        assert row["runtime_parity_status"] == "native_not_weaker", row
        assert row["runtime_parity_score"] == 1.0, row
        assert row["runtime_advantage_score"] == 1.0, row
        assert row["registered_worker_count"] == 2, row
        assert row["active_registered_worker_count"] == 2, row
        assert row["worker_complete_recorded"] is True, row
        assert row["worker_complete_ok"] is True, row
        assert row["worker_pool_result_recorded"] is True, row
        assert row["worker_pool_status"] == "ok", row
        assert row["worker_pool_worker_count"] == 3, row
        assert row["worker_pool_attempt_count"] == 5, row
        assert row["worker_pool_completed_count"] == 4, row
        assert row["worker_pool_failed_count"] == 0, row
        assert row["worker_pool_parallelism_observed"] == 3, row
        assert row["same_batch_validation_exists"] is True, row
        assert row["same_batch_validation_passed"] is True, row
        assert row["same_batch_improvement_pct"] == 10.0, row
        assert row["same_batch_has_default_control"] is True, row
        assert row["same_batch_has_fixed_scx_treatment"] is True, row
        assert row["same_batch_has_lockagent_online"] is True, row
        assert row["same_batch_has_oracle_best_eval_only"] is True, row
        assert row["counterfactual_plan_exists"] is True, row
        assert row["counterfactual_required"] is True, row
        assert row["counterfactual_task_count"] == 2, row
        assert row["counterfactual_executable_task_count"] == 1, row
        assert row["counterfactual_submitted_task_count"] == 1, row
        assert row["counterfactual_skill_update_planned"] is True, row
        assert row["counterfactual_skill_update_task_count"] == 1, row
        assert row["counterfactual_execution_exists"] is True, row
        assert row["counterfactual_execution_status"] == "ok", row
        assert row["causal_chain_count"] == 1, row
        assert row["strong_causal_chain_count"] == 1, row
        assert row["validated_causal_chain_count"] == 1, row
        assert row["refuted_causal_chain_count"] == 0, row
        assert row["causal_requires_same_batch"] is False, row
        assert row["causal_blame_node_count"] == 3, row
        assert row["causal_blame_edge_count"] == 1, row
        assert row["causal_profile_skill_scheduler_path"] is True, row
        assert row["fine_grained_blame_path_count"] == 1, row
        assert row["diagnostic_edge_count"] == 8, row
        assert row["regression_blame_chain_count_graph"] == 1, row
        assert row["dependency_blame_graph_node_count"] == 6, row
        assert row["dependency_blame_graph_edge_count"] == 8, row
        assert row["causal_dependency_edge_count"] == 2, row
        assert row["causal_dependency_chain_count_graph"] == 1, row
        assert row["causal_profile_skill_decision_action_outcome_path"] is True, row
        assert row["fine_grained_causal_chain_count"] == 1, row
        assert row["causal_dependency_chain_count_report"] == 1, row
        assert row["regression_blame_chain_count_report"] == 1, row
        assert row["bad_profile_bad_skill_scheduler_regression_path"] is True, row
        assert row["bad_profile_to_regression_path"] is True, row
        assert row["skill_to_regression_path"] is True, row
        assert row["scheduler_to_regression_path"] is True, row
        assert row["causal_root_candidate_count"] == 1, row
        assert row["systemic_override_application_exists"] is True, row
        assert row["system_evolution_state_exists"] is True, row
        assert row["system_evolution_history_count"] == 1, row
        assert row["system_evolution_override_history_count"] == 1, row
        assert row["system_evolution_template_history_count"] == 1, row
        assert row["system_evolution_selected_template"] == "critic_required_agent", row
        assert row["system_evolution_failure_total_count"] == 2, row
        assert row["systemic_override_applied_count"] == 4, row
        assert row["systemic_override_applied_fields"] == [
            "require_environment_compatibility",
            "require_strict_critic",
            "scheduler_disablelist",
            "meta_template",
        ], row
        assert row["systemic_override_profile_s_original"] == 2, row
        assert row["systemic_override_profile_s_final"] == 4, row
        assert row["systemic_override_disablelist"] == ["scx_lockcoop"], row
        assert row["meta_template_applied"] == "critic_required_agent", row
        assert row["meta_execution_graph_nodes"] == 3, row
        assert row["meta_execution_graph_edges"] == 2, row
        assert row["meta_execution_graph_step_nodes"] == 3, row
        assert row["meta_execution_graph_step_edges"] == 2, row
        assert row["meta_execution_graph_declared_step_count"] == 3, row
        assert row["meta_execution_graph_observed_declared_step_count"] == 2, row
        assert row["meta_execution_graph_observed_declared_gap"] == 1, row
        assert row["meta_template_graph_applied_to_runtime"] is True, row
        assert row["meta_template_runtime_policy_mode"] == "critic_required_runtime_control", row
        assert row["meta_template_runtime_skipped_step_count"] == 1, row
        assert row["meta_template_runtime_skipped_steps"] == ["skill_evolution_application"], row
        assert row["meta_template_runtime_skip_reasons"] == {
            "meta_template:critic_required_agent:disabled_stage:skill_evolution_application": 1
        }, row
        assert row["observed_execution_graph_exists"] is True, row
        assert row["observed_execution_graph_step_count"] == 2, row
        assert row["observed_execution_graph_declared_coverage"] == 0.667, row
        assert row["observed_execution_graph_control_edges"] == 1, row
        assert row["observed_execution_graph_artifact_dependency_edges"] == 1, row
        assert row["graph_scheduler_plan_exists"] is True, row
        assert row["graph_scheduler_declared_step_count"] == 3, row
        assert row["graph_scheduler_observed_declared_step_count"] == 2, row
        assert row["graph_scheduler_completed_step_count"] == 2, row
        assert row["graph_scheduler_runnable_step_count"] == 1, row
        assert row["graph_scheduler_next_runnable_count"] == 1, row
        assert row["graph_scheduler_artifact_dependency_edge_count"] == 1, row
        assert row["graph_scheduler_failed_step_replay_candidate_count"] == 1, row
        assert row["openai_trace_exists"] is True, row
        assert row["langgraph_state_exists"] is True, row
        assert row["trace_alignment_exists"] is True, row
        assert row["trace_alignment_score"] == 1.0, row
        assert row["typed_handoff_alignment_passed"] is True, row
        assert row["durable_execution_alignment_passed"] is True, row
        assert row["typed_event_bus_alignment_passed"] is True, row
        assert row["resume_graph_alignment_passed"] is True, row
        assert row["framework_adapter_alignment_passed"] is True, row
        assert row["framework_adapter_bundle_exists"] is True, row
        assert row["framework_adapter_score"] == 1.0, row
        assert row["framework_contract_status"] == "aligned", row
        assert row["framework_contract_score"] == 1.0, row
        assert row["framework_contract_check_count"] == 4, row
        assert row["framework_contract_passed_count"] == 4, row
        assert row["framework_contract_openai_passed"] is True, row
        assert row["framework_contract_langgraph_passed"] is True, row
        assert row["framework_contract_autogen_passed"] is True, row
        assert row["framework_contract_crewai_passed"] is True, row
        assert row["global_memory_recorded"] is True, row
        assert row["skill_repository_updated"] is True, row
        assert row["skill_repository_mutated"] is True, row
        assert row["skill_update_validation_gate_passed"] is True, row
        assert row["skill_update_evolution_ledger_exists"] is True, row
        assert row["skill_evolution_application_exists"] is True, row
        assert row["skill_evolution_application_applied_count"] == 2, row
        assert row["skill_evolution_application_transition_count"] == 1, row
        assert row["skill_evolution_application_skipped_count"] == 1, row
        assert row["skill_evolution_application_transitions"] == ["candidate->validated"], row
        agg = module.aggregate([row])
        assert agg["valid_decision_rate"] == 1.0, agg
        assert agg["artifact_complete_rate"] == 1.0, agg
        assert agg["evaluation_mode_distribution"] == {"full": 1}, agg
        assert agg["evaluation_artifact_complete_rate"] == 1.0, agg
        assert agg["avg_evaluation_missing_artifacts"] == 0.0, agg
        assert agg["evaluation_missing_artifact_distribution"] == {}, agg
        assert agg["avg_evaluation_optional_missing_artifacts"] == 0.0, agg
        assert agg["optional_process_artifact_complete_rate"] == 1.0, agg
        assert agg["fallback_artifacts_record_rate"] == 1.0, agg
        assert agg["fallback_artifacts_created_rate"] == 0.0, agg
        assert agg["avg_fallback_artifacts"] == 0.0, agg
        assert agg["fallback_performance_safe_rate"] == 1.0, agg
        assert agg["avg_policy_engine_checks"] == 15.0, agg
        assert agg["avg_policy_engine_rules"] == 5.0, agg
        assert agg["policy_engine_category_distribution"] == {
            "oracle_leakage": 1,
            "postflight_cleanup": 1,
            "profile_evidence": 1,
            "schema": 1,
        }, agg
        assert agg["policy_engine_owner_role_distribution"] == {
            "CriticAgent": 1,
            "RuntimeGuard": 1,
        }, agg
        assert agg["avg_policy_engine_missing_metadata"] == 0.0, agg
        assert agg["policy_engine_missing_metadata_distribution"] == {}, agg
        assert agg["policy_engine_oracle_rule_coverage_rate"] == 1.0, agg
        assert agg["policy_engine_evidence_resolution_rule_coverage_rate"] == 1.0, agg
        assert agg["policy_engine_lock_signal_rule_coverage_rate"] == 1.0, agg
        assert agg["policy_audit_record_rate"] == 1.0, agg
        assert agg["policy_audit_ok_rate"] == 1.0, agg
        assert agg["policy_audit_required_rules_pass_rate"] == 1.0, agg
        assert agg["policy_audit_metadata_complete_rate"] == 1.0, agg
        assert agg["avg_policy_audit_unclassified_branches"] == 0.0, agg
        assert agg["policy_audit_branch_classification_distribution"] == {
            "policy_engine_owned": 3,
            "executor_mechanics": 5,
            "artifact_fallback": 1,
        }, agg
        assert agg["executor_failure_class_distribution"] == {}, agg
        assert agg["executor_error_record_rate"] == 0.0, agg
        assert agg["avg_memory_items"] == 2.0, agg
        assert agg["avg_durable_checkpoints"] == 7.0, agg
        assert agg["avg_durable_steps"] == 5.0, agg
        assert agg["avg_durable_events"] == 18.0, agg
        assert agg["avg_durable_handoffs"] == 4.0, agg
        assert agg["avg_durable_tasks"] == 3.0, agg
        assert agg["avg_durable_leased_tasks"] == 1.0, agg
        assert agg["max_durable_workers"] == 2, agg
        assert agg["avg_typed_events"] == 4.0, agg
        assert agg["avg_typed_handoff_events"] == 1.0, agg
        assert agg["typed_handoff_event_count"] == 1, agg
        assert agg["typed_step_start_event_count"] == 1, agg
        assert agg["typed_step_end_event_count"] == 1, agg
        assert agg["durable_resume_plan_rate"] == 1.0, agg
        assert agg["resume_graph_alignment_rate"] == 1.0, agg
        assert agg["avg_resume_graph_nodes"] == 4.0, agg
        assert agg["avg_resume_graph_edges"] == 4.0, agg
        assert agg["avg_resume_graph_dependency_edges"] == 2.0, agg
        assert agg["avg_resume_graph_execution_edges"] == 1.0, agg
        assert agg["avg_resume_graph_replayable_steps"] == 1.0, agg
        assert agg["avg_resume_graph_queued_task_replays"] == 1.0, agg
        assert agg["avg_resume_graph_skipped_unselected_steps"] == 1.0, agg
        assert agg["avg_resume_graph_replay_selection_edges"] == 1.0, agg
        assert agg["resume_audit_rate"] == 1.0, agg
        assert agg["avg_resume_audit_replayed"] == 2.0, agg
        assert agg["avg_resume_audit_safe_to_replay"] == 2.0, agg
        assert agg["avg_resume_audit_unsafe_to_replay"] == 0.0, agg
        assert agg["avg_resume_audit_dependency_nodes"] == 6.0, agg
        assert agg["avg_resume_audit_dependency_edges"] == 5.0, agg
        assert agg["avg_resume_audit_resume_dependency_edges"] == 2.0, agg
        assert agg["avg_resume_audit_artifact_dependency_edges"] == 1.0, agg
        assert agg["avg_resume_audit_artifact_generation_edges"] == 2.0, agg
        assert agg["avg_resume_audit_replay_selection_edges"] == 1.0, agg
        assert agg["avg_resume_audit_skipped_unselected_steps"] == 1.0, agg
        assert agg["avg_resume_audit_input_hash_changed"] == 0.0, agg
        assert agg["avg_resume_audit_input_hash_missing"] == 0.0, agg
        assert agg["avg_resume_audit_input_hash_unverified"] == 0.0, agg
        assert agg["avg_resume_audit_input_hash_ok"] == 2.0, agg
        assert agg["avg_resume_audit_side_effect_safe"] == 2.0, agg
        assert agg["avg_resume_audit_side_effect_unsafe"] == 0.0, agg
        assert agg["avg_resume_audit_scx_load_replays"] == 0.0, agg
        assert agg["avg_resume_audit_service_restart_replays"] == 0.0, agg
        assert agg["avg_resume_audit_benchmark_replays"] == 1.0, agg
        assert agg["avg_resume_audit_root_control_replays"] == 0.0, agg
        assert agg["avg_durable_resume_steps"] == 1.0, agg
        assert agg["avg_durable_resume_tasks"] == 1.0, agg
        assert agg["avg_scheduler_history_events"] == 5.0, agg
        assert agg["worker_lease_event_count"] == 1, agg
        assert agg["worker_heartbeat_event_count"] == 1, agg
        assert agg["worker_lease_expired_event_count"] == 1, agg
        assert agg["runtime_service_event_count"] == 5, agg
        assert agg["root_runtime_service_event_count"] == 7, agg
        assert agg["http_auth_failure_event_count"] == 1, agg
        assert agg["http_memory_query_event_count"] == 1, agg
        assert agg["http_memory_summary_event_count"] == 1, agg
        assert agg["http_memory_retrieve_event_count"] == 1, agg
        assert agg["http_memory_append_event_count"] == 1, agg
        assert agg["http_hitl_approve_event_count"] == 1, agg
        assert agg["http_worker_once_event_count"] == 1, agg
        assert agg["http_worker_lease_event_count"] == 1, agg
        assert agg["http_worker_complete_event_count"] == 1, agg
        assert agg["http_worker_register_event_count"] == 1, agg
        assert agg["http_worker_registry_heartbeat_event_count"] == 1, agg
        assert agg["http_adapter_invoke_event_count"] == 1, agg
        assert agg["runtime_api_adapter_result_rate"] == 1.0, agg
        assert agg["runtime_api_adapter_framework_distribution"] == {"openai_agents": 1}, agg
        assert agg["runtime_api_adapter_invocation_distribution"] == {"tool_invoke": 1}, agg
        assert agg["runtime_api_adapter_submitted_task_count"] == 1, agg
        assert agg["runtime_api_adapter_event_count"] == 1, agg
        assert agg["runtime_api_adapter_resume_ok_rate"] == 1.0, agg
        assert agg["native_langgraph_result_rate"] == 1.0, agg
        assert agg["native_langgraph_backend_validation_source_distribution"] == {"real_langgraph": 1}, agg
        assert agg["native_langgraph_backend_invocation_class_distribution"] == {"real_backend_invoked": 1}, agg
        assert agg["native_langgraph_graph_mode_distribution"] == {"sequential_calibration": 1}, agg
        assert agg["native_langgraph_real_backend_invoked_rate"] == 1.0, agg
        assert agg["native_langgraph_fake_tested_rate"] == 0.0, agg
        assert agg["native_langgraph_unavailable_rate"] == 0.0, agg
        assert agg["native_langgraph_available_rate"] == 1.0, agg
        assert agg["native_langgraph_invoked_rate"] == 1.0, agg
        assert agg["native_langgraph_resume_rate"] == 1.0, agg
        assert agg["native_langgraph_state_observed_rate"] == 1.0, agg
        assert agg["runtime_parity_rate"] == 1.0, agg
        assert agg["avg_runtime_parity_score"] == 1.0, agg
        assert agg["avg_runtime_advantage_score"] == 1.0, agg
        assert agg["registered_worker_count"] == 2, agg
        assert agg["active_registered_worker_count"] == 2, agg
        assert agg["worker_complete_rate"] == 1.0, agg
        assert agg["worker_pool_result_rate"] == 1.0, agg
        assert agg["worker_pool_ok_rate"] == 1.0, agg
        assert agg["max_worker_pool_workers"] == 3, agg
        assert agg["max_worker_pool_parallelism_observed"] == 3, agg
        assert agg["worker_pool_completed_task_count"] == 4, agg
        assert agg["worker_pool_failed_task_count"] == 0, agg
        assert agg["same_batch_validation_rate"] == 1.0, agg
        assert agg["same_batch_validation_pass_rate"] == 1.0, agg
        assert agg["same_batch_default_control_rate"] == 1.0, agg
        assert agg["same_batch_fixed_scx_treatment_rate"] == 1.0, agg
        assert agg["same_batch_lockagent_online_rate"] == 1.0, agg
        assert agg["same_batch_oracle_best_eval_only_rate"] == 1.0, agg
        assert agg["counterfactual_plan_rate"] == 1.0, agg
        assert agg["counterfactual_required_rate"] == 1.0, agg
        assert agg["counterfactual_task_count"] == 2, agg
        assert agg["counterfactual_executable_task_count"] == 1, agg
        assert agg["counterfactual_submitted_task_count"] == 1, agg
        assert agg["counterfactual_skill_update_planned_rate"] == 1.0, agg
        assert agg["counterfactual_skill_update_task_count"] == 1, agg
        assert agg["counterfactual_execution_rate"] == 1.0, agg
        assert agg["counterfactual_execution_ok_rate"] == 1.0, agg
        assert agg["avg_causal_chains"] == 1.0, agg
        assert agg["avg_strong_causal_chains"] == 1.0, agg
        assert agg["avg_validated_causal_chains"] == 1.0, agg
        assert agg["avg_refuted_causal_chains"] == 0.0, agg
        assert agg["causal_same_batch_required_rate"] == 0.0, agg
        assert agg["avg_causal_blame_nodes"] == 3.0, agg
        assert agg["avg_causal_blame_edges"] == 1.0, agg
        assert agg["causal_profile_skill_scheduler_path_rate"] == 1.0, agg
        assert agg["avg_fine_grained_blame_paths"] == 1.0, agg
        assert agg["avg_diagnostic_edges"] == 8.0, agg
        assert agg["avg_regression_blame_chains_graph"] == 1.0, agg
        assert agg["dependency_blame_graph_rate"] == 1.0, agg
        assert agg["avg_dependency_blame_graph_nodes"] == 6.0, agg
        assert agg["avg_dependency_blame_graph_edges"] == 8.0, agg
        assert agg["avg_causal_dependency_edges"] == 2.0, agg
        assert agg["avg_causal_dependency_chains_graph"] == 1.0, agg
        assert agg["causal_profile_skill_decision_action_outcome_path_rate"] == 1.0, agg
        assert agg["avg_fine_grained_causal_chains"] == 1.0, agg
        assert agg["avg_causal_dependency_chains_report"] == 1.0, agg
        assert agg["avg_regression_blame_chains_report"] == 1.0, agg
        assert agg["bad_profile_bad_skill_scheduler_regression_path_rate"] == 1.0, agg
        assert agg["bad_profile_to_regression_path_rate"] == 1.0, agg
        assert agg["skill_to_regression_path_rate"] == 1.0, agg
        assert agg["scheduler_to_regression_path_rate"] == 1.0, agg
        assert agg["avg_causal_root_candidates"] == 1.0, agg
        assert agg["systemic_override_application_rate"] == 1.0, agg
        assert agg["system_evolution_state_rate"] == 1.0, agg
        assert agg["avg_system_evolution_history"] == 1.0, agg
        assert agg["avg_system_evolution_override_history"] == 1.0, agg
        assert agg["avg_system_evolution_template_history"] == 1.0, agg
        assert agg["system_evolution_selected_template_distribution"] == {"critic_required_agent": 1}, agg
        assert agg["avg_system_evolution_failure_totals"] == 2.0, agg
        assert agg["systemic_override_applied_runs"] == 1, agg
        assert agg["systemic_override_applied_field_distribution"] == {
            "require_environment_compatibility": 1,
            "require_strict_critic": 1,
            "scheduler_disablelist": 1,
            "meta_template": 1,
        }, agg
        assert agg["systemic_override_disablelist_runs"] == 1, agg
        assert agg["meta_template_applied_distribution"] == {"critic_required_agent": 1}, agg
        assert agg["avg_meta_execution_graph_nodes"] == 3.0, agg
        assert agg["avg_meta_execution_graph_edges"] == 2.0, agg
        assert agg["avg_meta_execution_graph_step_nodes"] == 3.0, agg
        assert agg["avg_meta_execution_graph_step_edges"] == 2.0, agg
        assert agg["meta_template_graph_applied_rate"] == 1.0, agg
        assert agg["meta_template_runtime_policy_mode_distribution"] == {
            "critic_required_runtime_control": 1
        }, agg
        assert agg["avg_meta_template_runtime_skipped_steps"] == 1.0, agg
        assert agg["meta_template_runtime_skip_reason_distribution"] == {
            "meta_template:critic_required_agent:disabled_stage:skill_evolution_application": 1
        }, agg
        assert agg["avg_meta_execution_graph_declared_steps"] == 3.0, agg
        assert agg["avg_meta_execution_graph_observed_declared_steps"] == 2.0, agg
        assert agg["avg_meta_execution_graph_observed_declared_gap"] == 1.0, agg
        assert agg["observed_execution_graph_rate"] == 1.0, agg
        assert agg["avg_observed_execution_graph_step_count"] == 2.0, agg
        assert agg["avg_observed_execution_graph_declared_coverage"] == 0.667, agg
        assert agg["avg_observed_execution_graph_control_edges"] == 1.0, agg
        assert agg["avg_observed_execution_graph_artifact_dependency_edges"] == 1.0, agg
        assert agg["graph_scheduler_plan_rate"] == 1.0, agg
        assert agg["avg_graph_scheduler_declared_steps"] == 3.0, agg
        assert agg["avg_graph_scheduler_observed_declared_steps"] == 2.0, agg
        assert agg["avg_graph_scheduler_completed_steps"] == 2.0, agg
        assert agg["avg_graph_scheduler_runnable_steps"] == 1.0, agg
        assert agg["avg_graph_scheduler_next_runnable_steps"] == 1.0, agg
        assert agg["avg_graph_scheduler_artifact_dependency_edges"] == 1.0, agg
        assert agg["avg_graph_scheduler_failed_step_replay_candidates"] == 1.0, agg
        assert agg["trace_alignment_complete_rate"] == 1.0, agg
        assert agg["avg_trace_alignment_score"] == 1.0, agg
        assert agg["openai_alignment_rate"] == 1.0, agg
        assert agg["langgraph_alignment_rate"] == 1.0, agg
        assert agg["autogen_alignment_rate"] == 1.0, agg
        assert agg["crewai_alignment_rate"] == 1.0, agg
        assert agg["framework_adapter_bundle_rate"] == 1.0, agg
        assert agg["framework_adapter_alignment_rate"] == 1.0, agg
        assert agg["avg_framework_adapter_score"] == 1.0, agg
        assert agg["framework_contract_alignment_rate"] == 1.0, agg
        assert agg["avg_framework_contract_score"] == 1.0, agg
        assert agg["avg_framework_contract_checks"] == 4.0, agg
        assert agg["framework_contract_openai_rate"] == 1.0, agg
        assert agg["framework_contract_langgraph_rate"] == 1.0, agg
        assert agg["framework_contract_autogen_rate"] == 1.0, agg
        assert agg["framework_contract_crewai_rate"] == 1.0, agg
        assert agg["avg_framework_adapter_concepts"] == 19.0, agg
        assert agg["typed_handoff_alignment_rate"] == 1.0, agg
        assert agg["durable_execution_alignment_rate"] == 1.0, agg
        assert agg["typed_event_bus_alignment_rate"] == 1.0, agg
        assert agg["global_memory_record_rate"] == 1.0, agg
        assert agg["memory_mode_distribution"] == {"run_local_defaults": 1}, agg
        assert agg["memory_isolation_source_distribution"] == {"online_manifest": 1}, agg
        assert agg["memory_path_scope_distribution"] == {"run_local": 1}, agg
        assert agg["memory_isolation_enabled_rate"] == 1.0, agg
        assert agg["avg_memory_explicit_paths"] == 0.0, agg
        assert agg["avg_memory_rewritten_paths"] == 2.0, agg
        assert agg["dry_run_memory_run_count"] == 1, agg
        assert agg["dry_run_memory_isolated_rate"] == 1.0, agg
        assert agg["dry_run_memory_shared_path_risk_rate"] == 0.0, agg
        assert agg["production_memory_run_count"] == 0, agg
        assert agg["production_shared_memory_rate"] is None, agg
        assert agg["skill_repository_update_runs"] == 1, agg
        assert agg["skill_repository_mutation_runs"] == 1, agg
        assert agg["skill_repository_update_with_ledger_runs"] == 1, agg
        assert agg["skill_update_validation_gate_pass_rate"] == 1.0, agg
        assert agg["skill_update_protected_status_count"] == 0, agg
        assert agg["skill_evolution_application_rate"] == 1.0, agg
        assert agg["skill_evolution_application_applied_count"] == 2, agg
        assert agg["skill_evolution_application_transition_count"] == 1, agg
        assert agg["skill_evolution_application_skipped_count"] == 1, agg
        assert agg["skill_evolution_application_transition_distribution"] == {"candidate->validated": 1}, agg
        out_json = Path(td) / "process_eval.json"
        out_csv = Path(td) / "process_eval.csv"
        rc = module.main(["--root", td, "--out-json", str(out_json), "--out-csv", str(out_csv)])
        assert rc == 0
        report = json.loads(out_json.read_text(encoding="utf-8"))
        assert report["evaluation_mode"] == "full", report
        assert report["aggregate"]["run_count"] == 1, report
        assert report["aggregate"]["evaluation_artifact_complete_rate"] == 1.0, report
        assert report["batch_aggregate"]["batch_count"] == 1, report
        assert report["batch_aggregate"]["sudo_noninteractive_blocked_count"] == 1, report
        assert report["batches"][0]["readiness_status"] == "blocked", report
        assert out_csv.exists(), report

        core_json = Path(td) / "process_eval_core.json"
        rc = module.main(
            [
                "--root",
                td,
                "--out-json",
                str(core_json),
                "--evaluation-mode",
                "core-online",
            ]
        )
        assert rc == 0
        core_report = json.loads(core_json.read_text(encoding="utf-8"))
        assert core_report["evaluation_mode"] == "core-online", core_report
        assert core_report["aggregate"]["evaluation_artifact_complete_rate"] == 1.0, core_report
        assert "runtime_api_adapter_result.json" in core_report["evaluation_requirements"]["optional"], core_report
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

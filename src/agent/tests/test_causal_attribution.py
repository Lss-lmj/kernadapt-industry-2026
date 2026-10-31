#!/usr/bin/env python3
"""Local checks for LockAgent causal attribution reports."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "causal_attribution.py"
spec = importlib.util.spec_from_file_location("causal_attribution", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main() -> int:
    graph = {
        "nodes": [
            {"id": "profile.signals", "type": "evidence", "present": True},
            {"id": "skill.choose_locknest_for_hot_lock_oltp.applicability", "type": "skill_applicability", "present": True},
            {"id": "skill.choose_locknest_for_hot_lock_oltp", "type": "skill", "present": True},
            {"id": "decision.scheduler", "type": "decision", "present": True},
            {"id": "action.scheduler_startup", "type": "scheduler_action", "present": True},
            {"id": "outcome.scheduler_startup", "type": "outcome", "present": True},
            {"id": "action.benchmark_run", "type": "scheduler_action", "present": True},
            {"id": "outcome.benchmark", "type": "outcome", "present": True},
            {"id": "action.rollback", "type": "scheduler_action", "present": True},
            {"id": "outcome.rollback", "type": "outcome", "present": True},
            {"id": "action.load_scx", "type": "action", "present": True},
            {"id": "outcome.executor_status", "type": "outcome", "present": True},
            {"id": "diagnosis.profile_quality", "type": "diagnosis", "present": True},
            {"id": "diagnosis.skill_applicability", "type": "diagnosis", "present": True},
            {"id": "diagnosis.scheduler_choice", "type": "diagnosis", "present": True},
            {"id": "diagnosis.performance_regression", "type": "diagnosis", "present": True},
            {"id": "failure.skill_misfire", "type": "failure"},
        ],
        "edges": [
            {"from": "profile.signals", "to": "skill.choose_locknest_for_hot_lock_oltp.applicability", "type": "tests_applicability"},
            {"from": "skill.choose_locknest_for_hot_lock_oltp.applicability", "to": "skill.choose_locknest_for_hot_lock_oltp", "type": "selects_skill"},
            {"from": "profile.signals", "to": "skill.choose_locknest_for_hot_lock_oltp", "type": "retrieves_or_conditions"},
            {"from": "skill.choose_locknest_for_hot_lock_oltp", "to": "decision.scheduler", "type": "guides"},
            {"from": "decision.scheduler", "to": "action.load_scx", "type": "materialized_as"},
            {"from": "decision.scheduler", "to": "action.scheduler_startup", "type": "causes_scheduler_startup"},
            {"from": "action.scheduler_startup", "to": "outcome.scheduler_startup", "type": "causes_startup_outcome"},
            {"from": "outcome.scheduler_startup", "to": "action.benchmark_run", "type": "enables_benchmark"},
            {"from": "action.benchmark_run", "to": "outcome.benchmark", "type": "causes_benchmark_outcome"},
            {"from": "outcome.benchmark", "to": "action.rollback", "type": "requires_cleanup"},
            {"from": "action.rollback", "to": "outcome.rollback", "type": "causes_rollback_outcome"},
            {"from": "action.load_scx", "to": "outcome.executor_status", "type": "produces"},
            {"from": "outcome.executor_status", "to": "failure.skill_misfire", "type": "blame_path"},
            {"from": "profile.signals", "to": "diagnosis.profile_quality", "type": "profile_quality_assessed"},
            {"from": "diagnosis.profile_quality", "to": "skill.choose_locknest_for_hot_lock_oltp.applicability", "type": "profile_conditions_skill"},
            {"from": "skill.choose_locknest_for_hot_lock_oltp.applicability", "to": "diagnosis.skill_applicability", "type": "skill_applicability_assessed"},
            {"from": "diagnosis.skill_applicability", "to": "skill.choose_locknest_for_hot_lock_oltp", "type": "skill_conditioned_by_profile"},
            {"from": "decision.scheduler", "to": "diagnosis.scheduler_choice", "type": "scheduler_choice_assessed"},
            {"from": "diagnosis.scheduler_choice", "to": "action.scheduler_startup", "type": "scheduler_choice_materializes_action"},
            {"from": "outcome.benchmark", "to": "diagnosis.performance_regression", "type": "outcome_indicates_regression"},
            {"from": "diagnosis.performance_regression", "to": "failure.skill_misfire", "type": "regression_explains_failure"},
        ],
        "blame_paths": [
            {
                "category": "skill_misfire",
                "path": [
                    "profile.signals",
                    "skill.choose_locknest_for_hot_lock_oltp",
                    "decision.scheduler",
                    "action.load_scx",
                    "outcome.executor_status",
                    "failure.skill_misfire",
                ],
            }
        ],
        "fine_grained_blame_paths": [
            {
                "category": "skill_misfire",
                "path": [
                    "profile.signals",
                    "skill.choose_locknest_for_hot_lock_oltp.applicability",
                    "skill.choose_locknest_for_hot_lock_oltp",
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
        "causal_dependency_chains": [
            {
                "category": "skill_misfire",
                "chain_type": "profile_skill_decision_action_outcome_failure",
                "path": [
                    "profile.signals",
                    "skill.choose_locknest_for_hot_lock_oltp.applicability",
                    "skill.choose_locknest_for_hot_lock_oltp",
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
                    "skill.choose_locknest_for_hot_lock_oltp.applicability",
                    "diagnosis.skill_applicability",
                    "skill.choose_locknest_for_hot_lock_oltp",
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
    }
    report = module.build_report(
        run_id="run_test",
        provenance={"dependency_graph": graph},
        failure={"categories": ["skill_misfire"], "primary_category": "skill_misfire"},
    )
    assert report["summary"]["chain_count"] == 2, report
    assert report["summary"]["strong_chain_count"] == 2, report
    assert report["summary"]["requires_same_batch_validation"] is True, report
    assert report["summary"]["profile_skill_scheduler_outcome_path"] is True, report
    assert report["summary"]["profile_skill_decision_action_outcome_path"] is True, report
    assert report["summary"]["fine_grained_chain_count"] == 1, report
    assert report["summary"]["causal_dependency_chain_count"] == 1, report
    assert report["summary"]["regression_blame_chain_count"] == 1, report
    assert report["summary"]["bad_profile_bad_skill_bad_scheduler_regression_path"] is True, report
    assert report["summary"]["bad_profile_to_regression_path"] is True, report
    assert report["summary"]["skill_to_regression_path"] is True, report
    assert report["summary"]["scheduler_to_regression_path"] is True, report
    assert report["summary"]["root_cause_candidate_count"] == 2, report
    chain = report["causal_chains"][0]
    assert chain["category"] == "skill_misfire", chain
    assert chain["chain_type"] == "profile_skill_decision_action_outcome_failure", chain
    assert chain["counterfactual_status"]["same_batch_required"] is True, chain
    assert chain["counterfactual_status"]["status"] == "needs_validation", chain
    assert "profile.signals -> skill.choose_locknest_for_hot_lock_oltp.applicability -> skill.choose_locknest_for_hot_lock_oltp -> decision.scheduler" in chain["causal_claim"], chain
    assert any("skill applicability" in item for item in chain["counterfactual_checks"]), chain
    assert any(
        item["chain_type"] == "bad_profile_bad_skill_bad_scheduler_regression"
        for item in report["causal_chains"]
    ), report
    blame = report["causal_blame_graph"]
    assert blame["has_profile_to_skill_to_scheduler_path"] is True, blame
    assert blame["has_bad_profile_bad_skill_bad_scheduler_regression_path"] is True, blame
    assert any(node["role"] == "skill_hypothesis" for node in blame["nodes"]), blame
    assert any(node["role"] == "bad_profile_diagnosis" for node in blame["nodes"]), blame
    assert any(node["role"] == "performance_regression_diagnosis" for node in blame["nodes"]), blame
    assert any(edge["type"] == "causal_blame" for edge in blame["edges"]), blame

    weak_report = module.build_report(
        run_id="run_weak",
        provenance={"dependency_graph": {"nodes": [], "edges": [], "blame_paths": graph["blame_paths"]}},
        failure={"categories": ["skill_misfire"]},
    )
    assert weak_report["summary"]["requires_same_batch_validation"] is True, weak_report
    assert weak_report["causal_chains"][0]["evidence_strength"]["label"] == "weak_graph_support", weak_report

    validated_report = module.build_report(
        run_id="run_validated",
        provenance={"dependency_graph": graph},
        failure={"categories": ["skill_misfire"], "primary_category": "skill_misfire"},
        same_batch_validation={
            "trajectory_valid": True,
            "verdict": "positive_evidence",
            "summary_path": "batch_summary.json",
            "validation": {"improvement_pct": 12.0},
        },
        counterfactual_execution={"status": "ok", "validation": "same_batch_validation.json"},
    )
    validated_chain = validated_report["causal_chains"][0]
    assert validated_chain["counterfactual_status"]["status"] == "validated_by_same_batch", validated_chain
    assert validated_chain["counterfactual_status"]["causal_effect"] == "supported", validated_chain
    assert validated_report["summary"]["requires_same_batch_validation"] is False, validated_report
    assert validated_report["summary"]["validated_chain_count"] == 2, validated_report

    refuted_report = module.build_report(
        run_id="run_refuted",
        provenance={"dependency_graph": graph},
        failure={"categories": ["skill_misfire"], "primary_category": "skill_misfire"},
        same_batch_validation={
            "trajectory_valid": False,
            "verdict": "candidate_rejected_for_this_context",
            "reasons": ["insufficient_tps_improvement"],
        },
    )
    refuted_chain = refuted_report["causal_chains"][0]
    assert refuted_chain["counterfactual_status"]["status"] == "refuted_by_same_batch", refuted_chain
    assert refuted_report["summary"]["refuted_chain_count"] == 2, refuted_report

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

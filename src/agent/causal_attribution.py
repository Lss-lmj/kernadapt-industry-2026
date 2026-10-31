#!/usr/bin/env python3
"""Build causal attribution reports from LockAgent provenance graphs."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lockagent.causal_attribution.v0.1"


COUNTERFACTUALS = {
    "environment_mismatch": [
        "Would the comparison remain valid if DB/kernel/NUMA/benchmark protocol matched?",
        "Re-run same scheduler under compatible state ledger before claiming performance regression.",
    ],
    "profile_failure": [
        "Would a second profiling window produce the same workload signals?",
        "Check whether missing/noisy profile caused wrong skill retrieval.",
    ],
    "policy_reasoning_failure": [
        "Would the decision remain unchanged if unsupported evidence was removed?",
        "Check whether cited evidence appears in profile/provenance.",
    ],
    "skill_misfire": [
        "Would fallback/default avoid the same failure?",
        "Check whether skill applicability predicates were satisfied by profile signals.",
    ],
    "scheduler_regression": [
        "Would default or best-fixed avoid the regression in a same-batch comparison?",
        "Check whether scheduler startup succeeded but outcome regressed.",
    ],
    "benchmark_failure": [
        "Would the benchmark command succeed under default scheduler?",
        "Separate workload failure from scheduler failure using same-batch control.",
    ],
    "guard_blocked": [
        "Would execution proceed after satisfying missing guard predicate?",
        "Check whether guard blocked before any host side effect.",
    ],
}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def node_map(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {node.get("id"): node for node in graph.get("nodes", []) if isinstance(node, dict)}


def edge_index(graph: dict[str, Any]) -> set[tuple[str, str]]:
    return {
        (edge.get("from"), edge.get("to"))
        for edge in graph.get("edges", [])
        if isinstance(edge, dict)
    }


def graph_edges_by_type(graph: dict[str, Any], edge_type: str) -> list[dict[str, Any]]:
    return [
        edge
        for edge in graph.get("edges", [])
        if isinstance(edge, dict) and edge.get("type") == edge_type
    ]


def build_causal_blame_graph(graph: dict[str, Any], categories: list[str]) -> dict[str, Any]:
    nodes = node_map(graph)
    edges = graph.get("edges", [])
    blame_paths = graph.get("fine_grained_blame_paths") or graph.get("blame_paths", [])
    regression_chains = graph.get("regression_blame_chains", [])
    blame_nodes: dict[str, dict[str, Any]] = {}
    blame_edges: list[dict[str, Any]] = []
    root_cause_candidates: list[dict[str, Any]] = []

    def add(node_id: str, role: str, **attrs: Any) -> None:
        source = nodes.get(node_id, {})
        blame_nodes[node_id] = {
            "id": node_id,
            "role": role,
            "present": source.get("present", True) is not False and bool(source or node_id.startswith("failure.")),
            "source_type": source.get("type"),
            **attrs,
        }

    for category in categories:
        paths = [item.get("path", []) for item in blame_paths if item.get("category") == category]
        paths.extend(
            item.get("path", [])
            for item in regression_chains
            if isinstance(item, dict) and item.get("category") == category
        )
        for path in paths:
            if not isinstance(path, list):
                continue
            for node_id in path:
                if node_id.startswith("profile."):
                    add(node_id, "profile_evidence", category=category)
                elif node_id.startswith("skill."):
                    add(node_id, "skill_hypothesis", category=category)
                elif node_id.startswith("decision."):
                    add(node_id, "policy_decision", category=category)
                elif node_id.startswith("manifest.") or node_id.startswith("action."):
                    add(node_id, "scheduler_action", category=category)
                elif node_id.startswith("outcome."):
                    add(node_id, "execution_outcome", category=category)
                elif node_id.startswith("guard."):
                    add(node_id, "runtime_guard", category=category)
                elif node_id.startswith("ledger."):
                    add(node_id, "environment_state", category=category)
                elif node_id.startswith("diagnosis.profile_quality"):
                    add(node_id, "bad_profile_diagnosis", category=category)
                elif node_id.startswith("diagnosis.skill_applicability"):
                    add(node_id, "bad_skill_diagnosis", category=category)
                elif node_id.startswith("diagnosis.scheduler_choice"):
                    add(node_id, "bad_scheduler_diagnosis", category=category)
                elif node_id.startswith("diagnosis.performance_regression"):
                    add(node_id, "performance_regression_diagnosis", category=category)
                elif node_id.startswith("diagnosis."):
                    add(node_id, "diagnosis", category=category)
                elif node_id.startswith("failure."):
                    add(node_id, "failure", category=category)
                else:
                    add(node_id, "supporting_node", category=category)
            for source, target in zip(path, path[1:]):
                blame_edges.append({"from": source, "to": target, "type": "causal_blame", "category": category})
            if path:
                first = path[0]
                root_cause_candidates.append(
                    {
                        "category": category,
                        "node": first,
                        "role": blame_nodes.get(first, {}).get("role"),
                        "confidence": "graph_supported" if first in nodes else "weak_missing_node",
                    }
                )

    # Include direct typed/runtime edges that make the path executable, not just explanatory.
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        source = edge.get("from")
        target = edge.get("to")
        if source in blame_nodes and target in blame_nodes:
            blame_edges.append({"from": source, "to": target, "type": f"observed_{edge.get('type')}", "category": edge.get("category")})

    has_regression_chain = any(
        isinstance(item, dict)
        and item.get("chain_type") == "bad_profile_bad_skill_bad_scheduler_regression"
        and item.get("category") in categories
        for item in regression_chains
    )
    return {
        "nodes": list(blame_nodes.values()),
        "edges": blame_edges,
        "root_cause_candidates": root_cause_candidates,
        "has_profile_to_skill_to_scheduler_path": any(node.startswith("profile.") for node in blame_nodes)
        and any(node.startswith("skill.") for node in blame_nodes)
        and any(node.startswith("decision.") for node in blame_nodes)
        and any(node.startswith("outcome.") for node in blame_nodes),
        "has_profile_skill_decision_action_outcome_path": any(node.startswith("profile.") for node in blame_nodes)
        and any(node.startswith("skill.") for node in blame_nodes)
        and any(node.startswith("decision.") for node in blame_nodes)
        and any(node.startswith("action.") for node in blame_nodes)
        and any(node.startswith("outcome.") for node in blame_nodes),
        "has_bad_profile_bad_skill_bad_scheduler_regression_path": has_regression_chain,
        "has_bad_profile_to_regression_path": any(node == "diagnosis.profile_quality" for node in blame_nodes)
        and any(node == "diagnosis.performance_regression" for node in blame_nodes),
        "has_skill_to_regression_path": any(node.startswith("skill.") for node in blame_nodes)
        and any(node == "diagnosis.performance_regression" for node in blame_nodes),
        "has_scheduler_to_regression_path": any(node == "diagnosis.scheduler_choice" for node in blame_nodes)
        and any(node == "diagnosis.performance_regression" for node in blame_nodes),
        "regression_blame_chains": regression_chains,
    }


def evidence_strength(path: list[str], nodes: dict[str, dict[str, Any]], edges: set[tuple[str, str]]) -> dict[str, Any]:
    present_nodes = [
        node
        for node in path
        if node in nodes and nodes[node].get("present", True) is not False
    ]
    missing_nodes = [node for node in path if node not in nodes or nodes[node].get("present", True) is False]
    present_edges = [(src, dst) for src, dst in zip(path, path[1:]) if (src, dst) in edges]
    node_score = len(present_nodes) / max(len(path), 1)
    edge_score = len(present_edges) / max(len(path) - 1, 1)
    score = round((node_score + edge_score) / 2, 3)
    if score >= 0.8:
        label = "strong_graph_support"
    elif score >= 0.5:
        label = "partial_graph_support"
    else:
        label = "weak_graph_support"
    return {
        "score": score,
        "label": label,
        "present_node_count": len(present_nodes),
        "path_node_count": len(path),
        "missing_nodes": missing_nodes,
        "present_edge_count": len(present_edges),
        "path_edge_count": max(len(path) - 1, 0),
    }


def validation_state_for_category(
    *,
    category: str,
    same_batch_validation: dict[str, Any] | None = None,
    counterfactual_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requires_same_batch = category in {"skill_misfire", "scheduler_regression", "benchmark_failure", "policy_reasoning_failure"}
    validation = same_batch_validation or {}
    counterfactual = counterfactual_execution or {}
    validation_exists = bool(validation)
    counterfactual_exists = bool(counterfactual)
    validation_passed = bool(validation.get("trajectory_valid"))
    validation_verdict = validation.get("verdict")
    validation_reasons = validation.get("reasons", []) if isinstance(validation.get("reasons", []), list) else []
    execution_status = counterfactual.get("status")

    if validation_exists and validation_passed:
        status = "validated_by_same_batch"
        causal_effect = "supported"
    elif validation_exists and not validation_passed:
        status = "refuted_by_same_batch"
        causal_effect = "refuted_or_context_specific"
    elif counterfactual_exists and execution_status in {"ok", "validation_failed"}:
        status = "counterfactual_executed_without_validation"
        causal_effect = "inconclusive"
    elif requires_same_batch:
        status = "needs_validation"
        causal_effect = "unvalidated"
    else:
        status = "not_required_for_classification"
        causal_effect = "classification_only"

    return {
        "same_batch_required": requires_same_batch,
        "status": status,
        "causal_effect": causal_effect,
        "same_batch_validation_exists": validation_exists,
        "same_batch_validation_passed": validation_passed if validation_exists else None,
        "same_batch_verdict": validation_verdict,
        "same_batch_reasons": validation_reasons,
        "counterfactual_execution_exists": counterfactual_exists,
        "counterfactual_execution_status": execution_status,
        "validation_artifacts": {
            "same_batch_summary": validation.get("summary_path"),
            "counterfactual_plan": counterfactual.get("plan"),
            "counterfactual_validation": counterfactual.get("validation"),
        },
    }


def build_report(
    *,
    run_id: str,
    provenance: dict[str, Any],
    failure: dict[str, Any],
    same_batch_validation: dict[str, Any] | None = None,
    counterfactual_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    graph = provenance.get("dependency_graph", {})
    nodes = node_map(graph)
    edges = edge_index(graph)
    categories = failure.get("categories", [])
    blame_paths = graph.get("fine_grained_blame_paths") or graph.get("blame_paths", [])
    causal_dependency_chains = graph.get("causal_dependency_chains", [])
    regression_blame_chains = graph.get("regression_blame_chains", [])
    chain_by_category: dict[str, list[dict[str, Any]]] = {}
    for chain in causal_dependency_chains:
        if isinstance(chain, dict):
            chain_by_category.setdefault(str(chain.get("category")), []).append(chain)
    for chain in regression_blame_chains:
        if isinstance(chain, dict):
            chain_by_category.setdefault(str(chain.get("category")), []).append(chain)
    causal_blame_graph = build_causal_blame_graph(graph, categories)
    causal_chains = []
    for category in categories:
        paths = [item.get("path", []) for item in blame_paths if item.get("category") == category]
        structured_chains = chain_by_category.get(category, [])
        for item in structured_chains:
            path = item.get("path", [])
            if isinstance(path, list) and path not in paths:
                paths.append(path)
        if not paths:
            paths = [[f"failure.{category}"]]
        for path in paths:
            if not isinstance(path, list):
                continue
            matching_structured = next(
                (item for item in structured_chains if item.get("path") == path),
                {},
            )
            causal_chains.append(
                {
                    "category": category,
                    "path": path,
                    "chain_type": matching_structured.get("chain_type", "legacy_blame_path"),
                    "structured_chain": matching_structured,
                    "evidence_strength": evidence_strength(path, nodes, edges),
                    "counterfactual_checks": COUNTERFACTUALS.get(category, ["Run same-batch control to validate causal claim."]),
                    "counterfactual_status": {
                        **validation_state_for_category(
                            category=category,
                            same_batch_validation=same_batch_validation,
                            counterfactual_execution=counterfactual_execution,
                        ),
                        "available_in_graph": any(node.startswith("profile.default_metrics") for node in path) or any("default" in str(node) for node in path),
                    },
                    "causal_claim": " -> ".join(path),
                }
            )
    primary = failure.get("primary_category") or (categories[0] if categories else None)
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "primary_category": primary,
        "categories": categories,
        "causal_chains": causal_chains,
        "causal_blame_graph": causal_blame_graph,
        "summary": {
            "chain_count": len(causal_chains),
            "strong_chain_count": sum(1 for item in causal_chains if item["evidence_strength"]["label"] == "strong_graph_support"),
            "requires_same_batch_validation": any(
                item.get("counterfactual_status", {}).get("status") == "needs_validation"
                for item in causal_chains
            ),
            "validated_chain_count": sum(1 for item in causal_chains if item.get("counterfactual_status", {}).get("status") == "validated_by_same_batch"),
            "refuted_chain_count": sum(1 for item in causal_chains if item.get("counterfactual_status", {}).get("status") == "refuted_by_same_batch"),
            "inconclusive_chain_count": sum(1 for item in causal_chains if item.get("counterfactual_status", {}).get("status") == "counterfactual_executed_without_validation"),
            "profile_skill_scheduler_outcome_path": causal_blame_graph["has_profile_to_skill_to_scheduler_path"],
            "profile_skill_decision_action_outcome_path": causal_blame_graph["has_profile_skill_decision_action_outcome_path"],
            "fine_grained_chain_count": sum(1 for item in causal_chains if item.get("chain_type") == "profile_skill_decision_action_outcome_failure"),
            "causal_dependency_chain_count": len(causal_dependency_chains) if isinstance(causal_dependency_chains, list) else 0,
            "regression_blame_chain_count": len(regression_blame_chains) if isinstance(regression_blame_chains, list) else 0,
            "bad_profile_bad_skill_bad_scheduler_regression_path": causal_blame_graph["has_bad_profile_bad_skill_bad_scheduler_regression_path"],
            "bad_profile_to_regression_path": causal_blame_graph["has_bad_profile_to_regression_path"],
            "skill_to_regression_path": causal_blame_graph["has_skill_to_regression_path"],
            "scheduler_to_regression_path": causal_blame_graph["has_scheduler_to_regression_path"],
            "root_cause_candidate_count": len(causal_blame_graph["root_cause_candidates"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--provenance", type=Path, required=True)
    ap.add_argument("--failure", type=Path, required=True)
    ap.add_argument("--same-batch-validation", type=Path)
    ap.add_argument("--counterfactual-execution", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    provenance = load_json(args.provenance) or {}
    failure = load_json(args.failure) or {}
    same_batch_validation = load_json(args.same_batch_validation) if args.same_batch_validation else None
    counterfactual_execution = load_json(args.counterfactual_execution) if args.counterfactual_execution else None
    report = build_report(
        run_id=args.run_id,
        provenance=provenance,
        failure=failure,
        same_batch_validation=same_batch_validation,
        counterfactual_execution=counterfactual_execution,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

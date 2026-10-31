#!/usr/bin/env python3
"""Audit that RuntimeGuard safety verdicts remain PolicyEngine-owned.

This audit is intentionally conservative. It does not replace runtime tests;
it prevents new scheduler-safety checks from silently reappearing as local
runner/executor ``if`` branches without an explicit classification.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import PolicyEngine


SCHEMA_VERSION = "lockagent.policy_audit.v0.1"
AGENT_DIR = Path(__file__).resolve().parent
REPO_ROOT = AGENT_DIR.parents[1]

AUDITED_FILES = [
    AGENT_DIR / "runtime_guard.py",
    AGENT_DIR / "scx_executor.py",
    AGENT_DIR / "run_lockagent_online.py",
]

SAFETY_TERMS = (
    "scheduler",
    "rollback",
    "ledger",
    "oracle",
    "environment",
    "manifest",
    "sched_ext",
    "benchmark",
    "residual",
    "dry_run",
    "allowlist",
    "policy",
    "guard",
    "human_approval",
)

REQUIRED_POLICY_RULES: dict[str, dict[str, str]] = {
    "decision_schema_valid": {"severity": "block", "category": "schema"},
    "scheduler_allowlisted": {"severity": "block", "category": "scheduler_allowlist"},
    "rollback_required": {"severity": "block", "category": "rollback"},
    "ledger_required": {"severity": "block", "category": "environment_compatibility"},
    "profile_required_for_non_default": {"severity": "block", "category": "profile_evidence"},
    "lock_signal_required_for_scx": {"severity": "block", "category": "profile_evidence"},
    "no_oracle_leakage": {"severity": "block", "category": "oracle_leakage"},
    "database_status_known_for_strict_actions": {
        "severity": "block",
        "category": "environment_compatibility",
    },
    "benchmark_protocol_known_for_comparison": {
        "severity": "block",
        "category": "environment_compatibility",
    },
    "manifest_required_for_executor": {"severity": "block", "category": "manifest_consistency"},
    "manifest_scheduler_matches_decision": {
        "severity": "block",
        "category": "manifest_consistency",
    },
    "reproduction_requires_compatibility": {
        "severity": "block",
        "category": "environment_compatibility",
    },
    "systemic_environment_gate": {"severity": "block", "category": "environment_compatibility"},
    "upstream_policy_preflight_allows": {"severity": "block", "category": "runtime_authorization"},
    "human_approval_required": {"severity": "block", "category": "runtime_authorization"},
    "dry_run_records_no_side_effect": {"severity": "warn", "category": "runtime_authorization"},
    "status_required": {"severity": "block", "category": "postflight_cleanup"},
    "final_sched_ext_disabled": {"severity": "block", "category": "postflight_cleanup"},
    "benchmark_log_preserved": {"severity": "warn", "category": "postflight_cleanup"},
    "no_residual_scx": {"severity": "block", "category": "postflight_cleanup"},
}


@dataclass(frozen=True)
class BranchClassificationRule:
    file_name: str
    function: str
    pattern: str
    classification: str
    rationale: str

    def matches(self, branch: dict[str, Any]) -> bool:
        return (
            branch["file"].endswith(self.file_name)
            and branch["function"] == self.function
            and re.search(self.pattern, branch["test"])
        )


BRANCH_CLASSIFICATION_RULES = [
    BranchClassificationRule(
        "runtime_guard.py",
        "build_policy_context",
        r"\bmanifest\b",
        "policy_context_builder",
        "Derives manifest fields for the canonical PolicyEngine context.",
    ),
    BranchClassificationRule(
        "runtime_guard.py",
        "build_policy_context",
        r"environment_compatible\s+is\s+None",
        "policy_context_builder",
        "Fills the default compatibility value before PolicyEngine evaluation.",
    ),
    BranchClassificationRule(
        "scx_executor.py",
        "start_scx",
        r"scheduler\s*==\s*['\"]default['\"]",
        "executor_mechanics",
        "Default scheduler is the executor no-op path after PolicyEngine authorization.",
    ),
    BranchClassificationRule(
        "scx_executor.py",
        "start_scx",
        r"\bdry_run\b",
        "executor_mechanics",
        "Suppresses scheduler side effects after the dry-run policy warning is recorded.",
    ),
    BranchClassificationRule(
        "scx_executor.py",
        "stop_scx",
        r"\bdry_run\b",
        "executor_mechanics",
        "Suppresses cleanup side effects in dry-run mode.",
    ),
    BranchClassificationRule(
        "scx_executor.py",
        "run_benchmark",
        r"\bdry_run\b",
        "executor_mechanics",
        "Suppresses benchmark side effects in dry-run mode.",
    ),
    BranchClassificationRule(
        "scx_executor.py",
        "main",
        r"guard_preflight.*verdict.*block",
        "policy_engine_owned",
        "Dispatches a blocking PolicyEngine verdict before privileged side effects.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "run",
        r"not\s+dry_run",
        "runtime_orchestration",
        "Executes command only when the caller did not request dry-run.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "run_logged",
        r"\bdry_run\b",
        "runtime_orchestration",
        "Writes command logs without performing side effects in dry-run mode.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "materialize_failure_fallback_artifacts",
        r"not\s+state_ledger_path\.exists",
        "artifact_fallback",
        "Synthesizes non-claim fallback artifacts for failure-path analysis only.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "apply_dry_run_memory_isolation",
        r"args\.dry_run",
        "runtime_orchestration",
        "Redirects dry-run memory paths to avoid mutating shared state.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "candidate_run_dirs",
        r"online_manifest|failure_attribution",
        "support_discovery",
        "Discovers prior run artifacts for memory and self-evolution support.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "build_systemic_override_application",
        r"normalized_overrides\.get",
        "systemic_evolution_bookkeeping",
        "Records which Systemic Self-Evolution overrides were applied.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "apply_systemic_overrides",
        r"normalized\.get",
        "systemic_evolution_bookkeeping",
        "Applies non-safety parameter rewrites before PolicyEngine receives context.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "restart_mariadb",
        r"not\s+dry_run",
        "executor_mechanics",
        "Runs or suppresses MariaDB restart side effects according to dry-run mode.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "infer_last_failed_scheduler",
        r"categories|scheduler",
        "systemic_evolution_bookkeeping",
        "Extracts previous scheduler failure evidence for future PolicyEngine disablelists.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "normalize_systemic_overrides",
        r"last_failed_scheduler",
        "systemic_evolution_bookkeeping",
        "Normalizes symbolic scheduler disablelist entries.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "main",
        r"template_policy",
        "systemic_evolution_bookkeeping",
        "Applies selected Meta Self-Evolution template stage gates; scheduler authorization remains PolicyEngine-owned.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "main",
        r"args\.dry_run",
        "runtime_orchestration",
        "Materializes synthetic dry-run artifacts or skips real side effects.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "main",
        r"not\s+args\.dry_run",
        "runtime_orchestration",
        "Parses real metrics only after non-dry-run execution.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "main",
        r"policy_report.*verdict.*block",
        "policy_engine_owned",
        "Dispatches a blocking PolicyEngine runtime preflight verdict.",
    ),
    BranchClassificationRule(
        "run_lockagent_online.py",
        "main",
        r"require_human_approval.*human_approval_required",
        "policy_engine_owned",
        "Handles the human-approval-only PolicyEngine block by queuing ExecutorAgent.",
    ),
]


def _normalize_source(source: str) -> str:
    return " ".join(source.split())


def _function_name(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        parent = parents.get(parent)
    return "<module>"


def _safety_relevant(test: str) -> bool:
    lowered = test.lower()
    return any(term in lowered for term in SAFETY_TERMS)


def scan_local_branches(files: list[Path] | None = None) -> dict[str, Any]:
    files = files or AUDITED_FILES
    branches: list[dict[str, Any]] = []
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test = _normalize_source(ast.get_source_segment(source, node.test) or "")
            if not _safety_relevant(test):
                continue
            branch = {
                "file": str(path.relative_to(REPO_ROOT)),
                "line": node.lineno,
                "function": _function_name(node, parents),
                "test": test,
            }
            matches = [rule for rule in BRANCH_CLASSIFICATION_RULES if rule.matches(branch)]
            if matches:
                branch["classification"] = matches[0].classification
                branch["rationale"] = matches[0].rationale
            else:
                branch["classification"] = "unclassified"
                branch["rationale"] = "Safety-relevant local branch lacks an audit classification."
            branches.append(branch)

    unclassified = [branch for branch in branches if branch["classification"] == "unclassified"]
    classification_counts: dict[str, int] = {}
    for branch in branches:
        classification_counts[branch["classification"]] = classification_counts.get(branch["classification"], 0) + 1
    return {
        "branch_count": len(branches),
        "unclassified_count": len(unclassified),
        "classification_counts": classification_counts,
        "branches": branches,
        "unclassified": unclassified,
    }


def audit_policy_rules() -> dict[str, Any]:
    rules = {rule.name: rule for rule in PolicyEngine.default_rules()}
    missing: list[str] = []
    mismatched: list[dict[str, Any]] = []
    for name, expected in REQUIRED_POLICY_RULES.items():
        rule = rules.get(name)
        if rule is None:
            missing.append(name)
            continue
        actual = {"severity": rule.severity, "category": rule.category}
        for key, expected_value in expected.items():
            if actual.get(key) != expected_value:
                mismatched.append(
                    {
                        "name": name,
                        "field": key,
                        "expected": expected_value,
                        "actual": actual.get(key),
                    }
                )

    missing_metadata = [
        rule.name
        for rule in rules.values()
        if not all(
            [
                rule.severity,
                rule.category,
                rule.description,
                rule.evidence_source,
                rule.remediation,
                rule.owner_role,
            ]
        )
    ]
    category_counts: dict[str, int] = {}
    owner_role_counts: dict[str, int] = {}
    for rule in rules.values():
        category_counts[rule.category] = category_counts.get(rule.category, 0) + 1
        owner_role_counts[rule.owner_role] = owner_role_counts.get(rule.owner_role, 0) + 1

    return {
        "rule_count": len(rules),
        "required_rule_count": len(REQUIRED_POLICY_RULES),
        "missing_required_rules": missing,
        "mismatched_required_rules": mismatched,
        "missing_metadata_rules": missing_metadata,
        "category_counts": category_counts,
        "owner_role_counts": owner_role_counts,
        "required_rules_passed": not missing and not mismatched,
        "metadata_complete": not missing_metadata,
    }


def run_audit() -> dict[str, Any]:
    policy_rules = audit_policy_rules()
    local_branches = scan_local_branches()
    status = (
        "ok"
        if policy_rules["required_rules_passed"]
        and policy_rules["metadata_complete"]
        and local_branches["unclassified_count"] == 0
        else "failed"
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "policy_rules": policy_rules,
        "local_branch_audit": local_branches,
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="Optional JSON output path.")
    args = parser.parse_args(argv)

    report = run_audit()
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

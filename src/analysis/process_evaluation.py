#!/usr/bin/env python3
"""Process-level evaluation suite for KernAdapt controller runs."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


REQUIRED_ARTIFACTS = [
    "agent_run.json",
    "runtime_trace.jsonl",
    "runtime_trace.json",
    "openai_agents_trace.json",
    "langgraph_state.json",
    "autogen_trace.json",
    "crewai_trace.json",
    "framework_adapter_bundle.json",
    "trace_schema_alignment.json",
    "memory_store.json",
    "agent_scheduler.json",
    "durable_runtime.db",
    "durable_runtime_summary.json",
    "typed_event_bus.jsonl",
    "typed_event_bus.json",
    "artifact_store.json",
    "execution_graph_runtime.json",
    "graph_scheduler_plan.json",
    "state_ledger.json",
    "profile.json",
    "llm_prompt.json",
    "decision.json",
    "critic_preflight.json",
    "runtime_policy_preflight.json",
    "execution/manifest.json",
    "execution/status.json",
    "execution/guard_preflight.json",
    "execution/guard_postflight.json",
    "critic_report.json",
    "trajectory_status.json",
    "failure_attribution.json",
    "fallback_artifacts.json",
    "provenance.json",
    "causal_attribution.json",
    "counterfactual_plan.json",
    "skill_curation.json",
    "rejected_hypothesis.json",
    "self_evolution_plan.json",
    "system_evolution_state.json",
    "systemic_override_application.json",
    "skill_evolution_application.json",
    "global_memory_record.json",
    "resume_audit.json",
    "native_langgraph_result.json",
    "runtime_parity.json",
]


OPTIONAL_PROCESS_ARTIFACTS = [
    "runtime_api_adapter_result.json",
    "worker_pool_result.json",
    "policy_audit.json",
]


FRAMEWORK_CALIBRATION_ARTIFACTS = [
    "openai_agents_trace.json",
    "langgraph_state.json",
    "autogen_trace.json",
    "crewai_trace.json",
    "framework_adapter_bundle.json",
    "trace_schema_alignment.json",
    "native_langgraph_result.json",
    "runtime_parity.json",
]


CORE_ONLINE_REQUIRED_ARTIFACTS = [
    name
    for name in REQUIRED_ARTIFACTS
    if name not in FRAMEWORK_CALIBRATION_ARTIFACTS
]


EVALUATION_MODE_ARTIFACTS = {
    "full": {
        "required": REQUIRED_ARTIFACTS,
        "optional": OPTIONAL_PROCESS_ARTIFACTS,
        "description": "Legacy strict mode: all built-in runtime, framework, parity, and process artifacts.",
    },
    "core-online": {
        "required": CORE_ONLINE_REQUIRED_ARTIFACTS,
        "optional": [
            *FRAMEWORK_CALIBRATION_ARTIFACTS,
            *OPTIONAL_PROCESS_ARTIFACTS,
            "same_batch_validation.json",
            "counterfactual_execution.json",
        ],
        "description": "Online control-plane mode without service, worker-pool, or paper evidence requirements.",
    },
    "service-api": {
        "required": [
            *CORE_ONLINE_REQUIRED_ARTIFACTS,
            "runtime_api_adapter_result.json",
            "runtime_service_events.jsonl",
        ],
        "optional": [
            *FRAMEWORK_CALIBRATION_ARTIFACTS,
            "worker_pool_result.json",
            "worker_complete.json",
            "same_batch_validation.json",
            "counterfactual_execution.json",
        ],
        "description": "Service/API mode: core online artifacts plus HTTP/API adapter traces.",
    },
    "worker-pool": {
        "required": [
            *CORE_ONLINE_REQUIRED_ARTIFACTS,
            "worker_pool_result.json",
            "worker_complete.json",
        ],
        "optional": [
            *FRAMEWORK_CALIBRATION_ARTIFACTS,
            "runtime_api_adapter_result.json",
            "runtime_service_events.jsonl",
            "same_batch_validation.json",
            "counterfactual_execution.json",
        ],
        "description": "Worker-pool mode: core online artifacts plus durable worker execution artifacts.",
    },
    "paper-evidence": {
        "required": [
            *CORE_ONLINE_REQUIRED_ARTIFACTS,
            *FRAMEWORK_CALIBRATION_ARTIFACTS,
            "same_batch_validation.json",
            "counterfactual_execution.json",
            "execution/benchmark/sysbench.log",
        ],
        "optional": [
            "runtime_api_adapter_result.json",
            "worker_pool_result.json",
            "worker_complete.json",
            "runtime_service_events.jsonl",
        ],
        "description": "Paper evidence mode: core online artifacts plus calibration, same-batch, and benchmark evidence.",
    },
}


def evaluation_requirements(mode: str) -> dict[str, Any]:
    if mode not in EVALUATION_MODE_ARTIFACTS:
        raise ValueError(f"unknown evaluation mode: {mode}")
    config = EVALUATION_MODE_ARTIFACTS[mode]
    return {
        "mode": mode,
        "description": config["description"],
        "required": list(dict.fromkeys(config["required"])),
        "optional": list(dict.fromkeys(config["optional"])),
    }


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


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


def native_langgraph_invocation_class(native_langgraph: dict[str, Any] | None) -> str:
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


def select_memory_isolation(
    online_manifest: dict[str, Any] | None,
    override_application: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    for source, data in [
        ("online_manifest", (online_manifest or {}).get("memory_isolation")),
        ("systemic_override_application", (override_application or {}).get("memory_isolation")),
    ]:
        if isinstance(data, dict):
            return source, data
    return "missing", {}


def path_scope(path: Any, run_dir: Path) -> str:
    if not path:
        return "unknown"
    try:
        candidate = Path(str(path)).expanduser().resolve()
        root = run_dir.resolve()
    except OSError:
        return "unknown"
    if candidate == root or root in candidate.parents:
        return "run_local"
    candidate_text = str(candidate)
    if "kernadapt/results/memory" in candidate_text or "LockAgent/results/memory" in candidate_text:
        return "default_shared"
    return "external_or_custom"


def summarize_memory_mode(
    run_dir: Path,
    online_manifest: dict[str, Any] | None,
    override_application: dict[str, Any] | None,
) -> dict[str, Any]:
    source, isolation = select_memory_isolation(online_manifest, override_application)
    paths = isolation.get("paths", {}) if isinstance(isolation.get("paths"), dict) else {}
    explicit = isolation.get("explicit", {}) if isinstance(isolation.get("explicit"), dict) else {}
    rewritten = isolation.get("rewritten", {}) if isinstance(isolation.get("rewritten"), dict) else {}
    mode = str(isolation.get("mode") or "unknown")
    dry_run_value = isolation.get("dry_run", (online_manifest or {}).get("dry_run"))
    dry_run = bool(dry_run_value) if dry_run_value is not None else None
    global_memory_path = paths.get("global_memory")
    scope = path_scope(global_memory_path, run_dir)
    isolated_for_dry_run = bool(
        dry_run is True
        and (
            bool(isolation.get("enabled"))
            or mode == "run_local_defaults"
            or scope == "run_local"
        )
    )
    dry_run_shared_risk = bool(dry_run is True and not isolated_for_dry_run)
    production_shared_memory = bool(dry_run is False and mode == "shared_paths")
    return {
        "memory_isolation_source": source,
        "memory_mode": mode,
        "memory_isolation_enabled": bool(isolation.get("enabled")),
        "memory_isolation_dry_run": dry_run,
        "memory_path_scope": scope,
        "global_memory_path": str(global_memory_path) if global_memory_path else None,
        "memory_explicit_path_count": sum(1 for value in explicit.values() if value is True),
        "memory_rewritten_path_count": len(rewritten),
        "memory_isolated_for_dry_run": isolated_for_dry_run,
        "dry_run_memory_shared_path_risk": dry_run_shared_risk,
        "production_shared_memory": production_shared_memory,
    }


def run_dirs(root: Path) -> list[Path]:
    if (root / "online_manifest.json").exists() or (root / "agent_run.json").exists():
        return [root]
    return sorted(path for path in root.glob("run_*") if path.is_dir())


def batch_dirs(root: Path) -> list[Path]:
    if (root / "batch_manifest.json").exists() or (root / "batch_readiness.json").exists():
        return [root]
    return sorted(path for path in root.glob("batch_*") if path.is_dir())


def summarize_batch(batch_dir: Path) -> dict[str, Any]:
    manifest = load_json(batch_dir / "batch_manifest.json")
    readiness = load_json(batch_dir / "batch_readiness.json")
    checks = [
        check
        for check in (readiness or {}).get("checks", [])
        if isinstance(check, dict)
    ]
    blocking = [
        check
        for check in checks
        if check.get("severity") == "block" and check.get("passed") is False
    ]
    warning = [
        check
        for check in checks
        if check.get("severity") == "warn" and check.get("passed") is False
    ]
    trials = (manifest or {}).get("trials", [])
    trials = trials if isinstance(trials, list) else []
    valid_trials = [
        trial
        for trial in trials
        if isinstance(trial, dict) and int(trial.get("returncode") or 0) == 0
    ]
    return {
        "batch_id": (manifest or {}).get("batch_id") or batch_dir.name,
        "batch_dir": str(batch_dir),
        "manifest_exists": manifest is not None,
        "readiness_exists": readiness is not None,
        "readiness_status": (readiness or {}).get("status"),
        "readiness_blocking_reasons": (readiness or {}).get("blocking_reasons", []),
        "readiness_check_count": len(checks),
        "readiness_blocking_check_count": len(blocking),
        "readiness_warning_check_count": len(warning),
        "readiness_blocking_checks": [str(check.get("name")) for check in blocking if check.get("name")],
        "readiness_warning_checks": [str(check.get("name")) for check in warning if check.get("name")],
        "sudo_noninteractive_available": next(
            (check.get("passed") for check in checks if check.get("name") == "sudo_noninteractive_available"),
            None,
        ),
        "check_only": bool((manifest or {}).get("check_only")),
        "dry_run": bool((manifest or {}).get("dry_run")),
        "status": (manifest or {}).get("status"),
        "returncode": (manifest or {}).get("returncode"),
        "arm_count": len((manifest or {}).get("arms", [])) if isinstance((manifest or {}).get("arms", []), list) else 0,
        "trial_count": len(trials),
        "valid_trial_count": len(valid_trials),
    }


def summarize_run(run_dir: Path, evaluation_mode: str = "full") -> dict[str, Any]:
    requirements = evaluation_requirements(evaluation_mode)
    missing = [name for name in REQUIRED_ARTIFACTS if not (run_dir / name).exists()]
    optional_missing = [name for name in OPTIONAL_PROCESS_ARTIFACTS if not (run_dir / name).exists()]
    mode_required_missing = [
        name for name in requirements["required"] if not (run_dir / name).exists()
    ]
    mode_optional_missing = [
        name for name in requirements["optional"] if not (run_dir / name).exists()
    ]
    decision = load_json(run_dir / "decision.json")
    policy = load_json(run_dir / "runtime_policy_preflight.json")
    guard_pre = load_json(run_dir / "execution" / "guard_preflight.json")
    guard_post = load_json(run_dir / "execution" / "guard_postflight.json")
    status = load_json(run_dir / "execution" / "status.json")
    trajectory = load_json(run_dir / "trajectory_status.json")
    failure = load_json(run_dir / "failure_attribution.json")
    fallback_artifacts = load_json(run_dir / "fallback_artifacts.json")
    curation = load_json(run_dir / "skill_curation.json")
    provenance = load_json(run_dir / "provenance.json")
    causal = load_json(run_dir / "causal_attribution.json")
    agent_run = load_json(run_dir / "agent_run.json")
    runtime_trace = load_json(run_dir / "runtime_trace.json")
    runtime_service_events = load_jsonl(run_dir / "runtime_service_events.jsonl")
    self_evolution = load_json(run_dir / "self_evolution_plan.json")
    system_evolution_state = load_json(run_dir / "system_evolution_state.json")
    override_application = load_json(run_dir / "systemic_override_application.json")
    skill_evolution_application = load_json(run_dir / "skill_evolution_application.json")
    skill_update = load_json(run_dir / "skill_repository_update.json")
    global_memory_record = load_json(run_dir / "global_memory_record.json")
    memory = load_json(run_dir / "memory_store.json")
    online_manifest = load_json(run_dir / "online_manifest.json")
    scheduler = load_json(run_dir / "agent_scheduler.json")
    durable_summary = load_json(run_dir / "durable_runtime_summary.json")
    typed_bus = load_json(run_dir / "typed_event_bus.json")
    typed_bus_events = load_jsonl(run_dir / "typed_event_bus.jsonl")
    human_gate = load_json(run_dir / "human_gate.json")
    openai_trace = load_json(run_dir / "openai_agents_trace.json")
    langgraph_state = load_json(run_dir / "langgraph_state.json")
    autogen_trace = load_json(run_dir / "autogen_trace.json")
    crewai_trace = load_json(run_dir / "crewai_trace.json")
    adapter_bundle = load_json(run_dir / "framework_adapter_bundle.json")
    framework_contracts = (adapter_bundle or {}).get("framework_contracts", {})
    trace_alignment = load_json(run_dir / "trace_schema_alignment.json")
    resume_plan = load_json(run_dir / "resume_plan.json")
    resume_audit = load_json(run_dir / "resume_audit.json")
    resume_result = load_json(run_dir / "resume_result.json")
    hitl_approval_result = load_json(run_dir / "hitl_approval_result.json")
    worker_result = load_json(run_dir / "worker_result.json")
    worker_pool_result = load_json(run_dir / "worker_pool_result.json")
    worker_complete = load_json(run_dir / "worker_complete.json")
    tasks_view = load_json(run_dir / "tasks_view.json")
    task_submission = load_json(run_dir / "task_submission_result.json")
    same_batch_validation = load_json(run_dir / "same_batch_validation.json")
    counterfactual_plan = load_json(run_dir / "counterfactual_plan.json")
    counterfactual_execution = load_json(run_dir / "counterfactual_execution.json")
    runtime_api_adapter_result = load_json(run_dir / "runtime_api_adapter_result.json")
    native_langgraph = load_json(run_dir / "native_langgraph_result.json")
    runtime_parity = load_json(run_dir / "runtime_parity.json")
    policy_audit = load_json(run_dir / "policy_audit.json")
    execution_graph_runtime = load_json(run_dir / "execution_graph_runtime.json")
    graph_scheduler_plan = load_json(run_dir / "graph_scheduler_plan.json")
    root_service_events = load_jsonl(run_dir.parent / "runtime_service_events.jsonl")
    worker_registry = load_json(run_dir.parent / "worker_registry.json")

    steps = agent_run.get("steps", []) if agent_run else []
    roles = [step.get("role") for step in steps if isinstance(step, dict)]
    denied = [
        step.get("capability_verdict", {}).get("denied")
        for step in steps
        if isinstance(step, dict) and step.get("capability_verdict", {}).get("denied")
    ]
    graph = (provenance or {}).get("dependency_graph", {})
    observed_graph_summary = (execution_graph_runtime or {}).get("summary", {})
    graph_scheduler_summary = (graph_scheduler_plan or {}).get("summary", {})
    graph_scheduler_declared_steps = (
        (graph_scheduler_plan or {}).get("declared_steps", [])
        if isinstance((graph_scheduler_plan or {}).get("declared_steps", []), list)
        else []
    )
    graph_scheduler_next_runnable_steps = (
        (graph_scheduler_plan or {}).get("next_runnable_steps", [])
        if isinstance((graph_scheduler_plan or {}).get("next_runnable_steps", []), list)
        else []
    )
    graph_scheduler_artifact_dependency_edges = (
        (graph_scheduler_plan or {}).get("artifact_dependency_edges", [])
        if isinstance((graph_scheduler_plan or {}).get("artifact_dependency_edges", []), list)
        else []
    )
    graph_scheduler_failed_replay_candidates = (
        (graph_scheduler_plan or {}).get("failed_step_replay_candidates", [])
        if isinstance((graph_scheduler_plan or {}).get("failed_step_replay_candidates", []), list)
        else []
    )
    scheduler_history = (scheduler or {}).get("history", [])
    scheduler_tasks = (scheduler or {}).get("tasks", [])
    memory_items = (memory or {}).get("items", [])
    typed_events = typed_bus_events or ((typed_bus or {}).get("events", []) if isinstance((typed_bus or {}).get("events", []), list) else [])
    typed_event_counter = Counter(str(event.get("event_type")) for event in typed_events if isinstance(event, dict))
    skipped_steps = [
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("status") == "skipped"
    ]
    template_skipped_steps = [
        step
        for step in skipped_steps
        if isinstance(step.get("metadata"), dict)
        and step.get("metadata", {}).get("runtime_template_policy")
    ]
    template_skip_reason_counter = Counter(
        str(step.get("metadata", {}).get("skip_reason"))
        for step in template_skipped_steps
        if isinstance(step.get("metadata"), dict) and step.get("metadata", {}).get("skip_reason")
    )
    policy_checks = (policy or {}).get("policy_engine", {}).get("checks", [])
    guard_pre_checks = (guard_pre or {}).get("policy_engine", {}).get("checks", [])
    guard_post_checks = (guard_post or {}).get("policy_engine", {}).get("checks", [])
    all_policy_checks = [
        check
        for check in [*policy_checks, *guard_pre_checks, *guard_post_checks]
        if isinstance(check, dict)
    ]
    policy_rule_names = sorted({str(check.get("name")) for check in all_policy_checks if check.get("name")})
    policy_rule_categories = sorted(
        {str(check.get("category")) for check in all_policy_checks if check.get("category")}
    )
    policy_rule_owner_roles = sorted(
        {str(check.get("owner_role")) for check in all_policy_checks if check.get("owner_role")}
    )
    policy_checks_missing_metadata = [
        str(check.get("name"))
        for check in all_policy_checks
        if check.get("name")
        and (
            not check.get("category")
            or not check.get("evidence_source")
            or not check.get("remediation")
            or not check.get("owner_role")
            or "failure_category" not in check
        )
    ]
    policy_blocking_rules = [
        str(check.get("name"))
        for check in all_policy_checks
        if check.get("severity") == "block" and check.get("passed") is False and check.get("name")
    ]
    policy_warning_rules = [
        str(check.get("name"))
        for check in all_policy_checks
        if check.get("severity") == "warn" and check.get("passed") is False and check.get("name")
    ]
    policy_audit_rules = (policy_audit or {}).get("policy_rules", {})
    policy_audit_branches = (policy_audit or {}).get("local_branch_audit", {})
    framework_contract_checks = [
        check
        for check in framework_contracts.get("checks", [])
        if isinstance(check, dict)
    ]
    framework_contract_failed = [
        str(check.get("name"))
        for check in framework_contract_checks
        if check.get("passed") is False and check.get("name")
    ]
    adapter_result_task = (runtime_api_adapter_result or {}).get("task")
    adapter_result_resume = (runtime_api_adapter_result or {}).get("resume")
    durable_resume_view = (resume_plan or {}).get("durable_resume_view", {})
    resume_graph = (resume_plan or {}).get("resume_graph", {})
    resume_graph_nodes = resume_graph.get("nodes", []) if isinstance(resume_graph, dict) else []
    resume_graph_edges = resume_graph.get("edges", []) if isinstance(resume_graph, dict) else []
    resume_audit_summary = (resume_audit or {}).get("summary", {})
    resume_audit_graph = (resume_audit or {}).get("dependency_graph", {})
    override_graph = (override_application or {}).get("execution_graph", {})
    override_step_nodes = override_graph.get("step_nodes", []) if isinstance(override_graph, dict) else []
    observed_declared_step_count = observed_graph_summary.get("observed_declared_step_count", 0)
    declared_step_node_count = observed_graph_summary.get(
        "declared_step_node_count",
        len(override_step_nodes) if isinstance(override_step_nodes, list) else 0,
    )
    meta_template_declared_gap = max(int(declared_step_node_count or 0) - int(observed_declared_step_count or 0), 0)
    meta_template_graph_applied_to_runtime = (
        override_application is not None
        and bool((override_application or {}).get("meta_template"))
        and execution_graph_runtime is not None
        and int(declared_step_node_count or 0) > 0
        and observed_graph_summary.get("declared_step_coverage", 0) > 0
    )
    hitl_requested = human_gate is not None or any(
        event.get("event_type") == "human_gate_requested"
        for event in (runtime_trace or {}).get("events", [])
        if isinstance(event, dict)
    )
    all_service_events = runtime_service_events + [
        item
        for item in root_service_events
        if not item.get("checkpoint") or str(item.get("checkpoint", "")).startswith(str(run_dir))
    ]
    service_events = Counter(str(item.get("event")) for item in all_service_events if isinstance(item, dict))
    plan_skill_evolution = (counterfactual_plan or {}).get("skill_evolution", {})
    executable_skill_updates = [
        task
        for task in (counterfactual_plan or {}).get("executable_tasks", [])
        if isinstance(task, dict)
        and isinstance(task.get("payload"), dict)
        and task.get("payload", {}).get("skill_evolution", {}).get("update_repository")
    ]
    systemic_overrides = (self_evolution or {}).get("systemic_overrides", {})
    applied_overrides = (override_application or {}).get("normalized_overrides", {})
    execution_graph = (override_application or {}).get("execution_graph", {})
    execution_graph_step_nodes = execution_graph.get("step_nodes", []) if isinstance(execution_graph, dict) else []
    execution_graph_step_edges = execution_graph.get("step_edges", []) if isinstance(execution_graph, dict) else []
    applied_override_fields = [
        str(item.get("field"))
        for item in (override_application or {}).get("applied", [])
        if isinstance(item, dict) and item.get("field")
    ]
    systemic_override_count = 0
    for key, value in systemic_overrides.items():
        if key in {"schema_version", "generated_at", "notes", "meta_template"}:
            continue
        if isinstance(value, bool):
            systemic_override_count += int(value)
        elif isinstance(value, int):
            systemic_override_count += int(value != 1)
        elif isinstance(value, list):
            systemic_override_count += int(bool(value))
        elif value is not None:
            systemic_override_count += 1
    memory_mode = summarize_memory_mode(run_dir, online_manifest, override_application)
    return {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "artifact_complete": not missing,
        "missing_artifacts": missing,
        "optional_process_artifact_complete": not optional_missing,
        "optional_process_missing_artifacts": optional_missing,
        "evaluation_mode": evaluation_mode,
        "evaluation_mode_description": requirements["description"],
        "evaluation_required_artifact_count": len(requirements["required"]),
        "evaluation_missing_artifact_count": len(mode_required_missing),
        "evaluation_artifact_complete": not mode_required_missing,
        "evaluation_missing_artifacts": mode_required_missing,
        "evaluation_optional_artifact_count": len(requirements["optional"]),
        "evaluation_optional_missing_artifact_count": len(mode_optional_missing),
        "evaluation_optional_missing_artifacts": mode_optional_missing,
        "fallback_artifacts_exists": fallback_artifacts is not None,
        "fallback_artifacts_created": bool((fallback_artifacts or {}).get("created")),
        "fallback_artifact_count": (
            len((fallback_artifacts or {}).get("records", []))
            if isinstance((fallback_artifacts or {}).get("records", []), list)
            else 0
        ),
        "fallback_safe_for_performance_claims": (fallback_artifacts or {}).get("safe_for_performance_claims"),
        "valid_decision": bool(decision and decision.get("scheduler") and decision.get("rollback_conditions")),
        "policy_verdict": (policy or {}).get("verdict"),
        "guard_preflight_verdict": (guard_pre or {}).get("verdict"),
        "guard_postflight_verdict": (guard_post or {}).get("verdict"),
        "guard_blocked": (policy or {}).get("verdict") == "block" or (guard_pre or {}).get("verdict") == "block",
        "policy_engine_check_count": len(all_policy_checks),
        "policy_engine_rule_count": len(policy_rule_names),
        "policy_engine_rule_names": policy_rule_names,
        "policy_engine_rule_categories": policy_rule_categories,
        "policy_engine_rule_owner_roles": policy_rule_owner_roles,
        "policy_engine_missing_metadata_count": len(policy_checks_missing_metadata),
        "policy_engine_missing_metadata_rules": policy_checks_missing_metadata,
        "policy_engine_blocking_rule_count": len(policy_blocking_rules),
        "policy_engine_warning_rule_count": len(policy_warning_rules),
        "policy_engine_blocking_rules": policy_blocking_rules,
        "policy_engine_warning_rules": policy_warning_rules,
        "policy_engine_has_oracle_leakage_rule": "no_oracle_leakage" in policy_rule_names,
        "policy_engine_has_evidence_resolution_rule": "evidence_resolves_to_profile_or_skill" in policy_rule_names,
        "policy_engine_has_lock_signal_rule": "lock_signal_required_for_scx" in policy_rule_names,
        "policy_audit_exists": policy_audit is not None,
        "policy_audit_status": (policy_audit or {}).get("status"),
        "policy_audit_required_rules_passed": bool(policy_audit_rules.get("required_rules_passed")),
        "policy_audit_metadata_complete": bool(policy_audit_rules.get("metadata_complete")),
        "policy_audit_missing_required_rule_count": len(policy_audit_rules.get("missing_required_rules", [])) if isinstance(policy_audit_rules, dict) else 0,
        "policy_audit_mismatched_required_rule_count": len(policy_audit_rules.get("mismatched_required_rules", [])) if isinstance(policy_audit_rules, dict) else 0,
        "policy_audit_missing_metadata_rule_count": len(policy_audit_rules.get("missing_metadata_rules", [])) if isinstance(policy_audit_rules, dict) else 0,
        "policy_audit_local_branch_count": policy_audit_branches.get("branch_count", 0) if isinstance(policy_audit_branches, dict) else 0,
        "policy_audit_unclassified_branch_count": policy_audit_branches.get("unclassified_count", 0) if isinstance(policy_audit_branches, dict) else 0,
        "policy_audit_branch_classifications": policy_audit_branches.get("classification_counts", {}) if isinstance(policy_audit_branches, dict) else {},
        "rollback_success": bool(status and status.get("final_sched_ext_state") == "disabled"),
        "executor_status": (status or {}).get("status"),
        "executor_returncode": (status or {}).get("returncode"),
        "executor_failure_class": (status or {}).get("executor_failure_class"),
        "executor_error_recorded": bool((status or {}).get("executor_error")),
        "trajectory_valid": (trajectory or {}).get("valid"),
        "failure_categories": (failure or {}).get("categories", []),
        "skill": (curation or {}).get("skill"),
        "skill_verdict": (curation or {}).get("verdict"),
        "trace_step_count": len(steps),
        "trace_roles": roles,
        "capability_denied": denied,
        "dependency_nodes": len(graph.get("nodes", [])),
        "dependency_edges": len(graph.get("edges", [])),
        "execution_edge_count": len(graph.get("execution_edges", [])),
        "dependency_edge_count": len(graph.get("dependency_edges", [])),
        "handoff_edge_count": len(graph.get("handoff_edges", [])),
        "artifact_edge_count": len(graph.get("artifact_edges", [])),
        "blame_edge_count": len(graph.get("blame_edges", [])),
        "diagnostic_edge_count": len(graph.get("diagnostic_edges", [])),
        "regression_blame_chain_count_graph": len(graph.get("regression_blame_chains", [])),
        "dependency_blame_graph_node_count": len(graph.get("dependency_blame_graph", {}).get("nodes", [])),
        "dependency_blame_graph_edge_count": len(graph.get("dependency_blame_graph", {}).get("edges", [])),
        "blame_path_count": len(graph.get("blame_paths", [])),
        "fine_grained_blame_path_count": len(graph.get("fine_grained_blame_paths", [])),
        "causal_dependency_edge_count": len(graph.get("causal_dependency_edges", [])),
        "causal_dependency_chain_count_graph": len(graph.get("causal_dependency_chains", [])),
        "causal_chain_count": len((causal or {}).get("causal_chains", [])),
        "strong_causal_chain_count": (causal or {}).get("summary", {}).get("strong_chain_count", 0),
        "validated_causal_chain_count": (causal or {}).get("summary", {}).get("validated_chain_count", 0),
        "refuted_causal_chain_count": (causal or {}).get("summary", {}).get("refuted_chain_count", 0),
        "inconclusive_causal_chain_count": (causal or {}).get("summary", {}).get("inconclusive_chain_count", 0),
        "causal_requires_same_batch": (causal or {}).get("summary", {}).get("requires_same_batch_validation"),
        "causal_blame_node_count": len((causal or {}).get("causal_blame_graph", {}).get("nodes", [])),
        "causal_blame_edge_count": len((causal or {}).get("causal_blame_graph", {}).get("edges", [])),
        "causal_profile_skill_scheduler_path": bool((causal or {}).get("summary", {}).get("profile_skill_scheduler_outcome_path")),
        "causal_profile_skill_decision_action_outcome_path": bool((causal or {}).get("summary", {}).get("profile_skill_decision_action_outcome_path")),
        "fine_grained_causal_chain_count": (causal or {}).get("summary", {}).get("fine_grained_chain_count", 0),
        "causal_dependency_chain_count_report": (causal or {}).get("summary", {}).get("causal_dependency_chain_count", 0),
        "regression_blame_chain_count_report": (causal or {}).get("summary", {}).get("regression_blame_chain_count", 0),
        "bad_profile_bad_skill_scheduler_regression_path": bool(
            (causal or {}).get("summary", {}).get("bad_profile_bad_skill_bad_scheduler_regression_path")
        ),
        "bad_profile_to_regression_path": bool((causal or {}).get("summary", {}).get("bad_profile_to_regression_path")),
        "skill_to_regression_path": bool((causal or {}).get("summary", {}).get("skill_to_regression_path")),
        "scheduler_to_regression_path": bool((causal or {}).get("summary", {}).get("scheduler_to_regression_path")),
        "causal_root_candidate_count": (causal or {}).get("summary", {}).get("root_cause_candidate_count", 0),
        "systemic_action_count": len((self_evolution or {}).get("systemic_actions", [])),
        "meta_template": (self_evolution or {}).get("meta_template"),
        "system_evolution_state_exists": system_evolution_state is not None,
        "system_evolution_history_count": len((system_evolution_state or {}).get("history", [])),
        "system_evolution_override_history_count": len((system_evolution_state or {}).get("override_history", [])),
        "system_evolution_template_history_count": len((system_evolution_state or {}).get("template_history", [])),
        "system_evolution_selected_template": (system_evolution_state or {}).get("selected_meta_template"),
        "system_evolution_failure_total_count": sum((system_evolution_state or {}).get("failure_totals", {}).values()),
        "system_evolution_template_scores": (system_evolution_state or {}).get("template_scores", {}),
        "systemic_override_count": systemic_override_count,
        "systemic_override_application_exists": override_application is not None,
        "systemic_override_applied_count": (override_application or {}).get("applied_count", 0),
        "systemic_override_applied_fields": applied_override_fields,
        "systemic_override_ignored": (override_application or {}).get("ignored"),
        "systemic_override_profile_s_original": (override_application or {}).get("profile_s", {}).get("original"),
        "systemic_override_profile_s_final": (override_application or {}).get("profile_s", {}).get("final"),
        "systemic_override_disablelist": applied_overrides.get("scheduler_disablelist", []),
        "meta_template_applied": (override_application or {}).get("meta_template"),
        "meta_execution_graph_nodes": len(execution_graph.get("nodes", [])) if isinstance(execution_graph, dict) else 0,
        "meta_execution_graph_edges": len(execution_graph.get("edges", [])) if isinstance(execution_graph, dict) else 0,
        "meta_execution_graph_step_nodes": len(execution_graph_step_nodes),
        "meta_execution_graph_step_edges": len(execution_graph_step_edges),
        "meta_execution_graph_declared_step_count": declared_step_node_count,
        "meta_execution_graph_observed_declared_step_count": observed_declared_step_count,
        "meta_execution_graph_observed_declared_gap": meta_template_declared_gap,
        "meta_template_graph_applied_to_runtime": meta_template_graph_applied_to_runtime,
        "meta_template_runtime_policy_mode": (override_application or {}).get("runtime_template_policy", {}).get("mode"),
        "meta_template_runtime_skipped_step_count": len(template_skipped_steps),
        "meta_template_runtime_skipped_steps": [step.get("name") for step in template_skipped_steps],
        "meta_template_runtime_skip_reasons": dict(template_skip_reason_counter),
        "observed_execution_graph_exists": execution_graph_runtime is not None,
        "observed_execution_graph_step_count": observed_graph_summary.get("observed_step_count", 0),
        "observed_execution_graph_declared_coverage": observed_graph_summary.get("declared_step_coverage", 0),
        "observed_execution_graph_control_edges": observed_graph_summary.get("observed_control_edge_count", 0),
        "observed_execution_graph_artifact_dependency_edges": observed_graph_summary.get("observed_artifact_dependency_edge_count", 0),
        "graph_scheduler_plan_exists": graph_scheduler_plan is not None,
        "graph_scheduler_declared_step_count": graph_scheduler_summary.get("declared_step_count", len(graph_scheduler_declared_steps)),
        "graph_scheduler_observed_declared_step_count": graph_scheduler_summary.get("observed_declared_step_count", 0),
        "graph_scheduler_completed_step_count": graph_scheduler_summary.get("completed_step_count", 0),
        "graph_scheduler_skipped_step_count": graph_scheduler_summary.get("skipped_step_count", 0),
        "graph_scheduler_failed_step_count": graph_scheduler_summary.get("failed_step_count", 0),
        "graph_scheduler_blocked_step_count": graph_scheduler_summary.get("blocked_step_count", 0),
        "graph_scheduler_waiting_step_count": graph_scheduler_summary.get("waiting_step_count", 0),
        "graph_scheduler_runnable_step_count": graph_scheduler_summary.get("runnable_step_count", 0),
        "graph_scheduler_next_runnable_count": graph_scheduler_summary.get("next_runnable_count", len(graph_scheduler_next_runnable_steps)),
        "graph_scheduler_artifact_dependency_edge_count": graph_scheduler_summary.get(
            "artifact_dependency_edge_count",
            len(graph_scheduler_artifact_dependency_edges),
        ),
        "graph_scheduler_failed_step_replay_candidate_count": graph_scheduler_summary.get(
            "failed_step_replay_candidate_count",
            len(graph_scheduler_failed_replay_candidates),
        ),
        "graph_scheduler_missing_required_artifact_count": graph_scheduler_summary.get("missing_required_artifact_count", 0),
        "skill_repository_updated": bool((skill_update or {}).get("updated")),
        "skill_repository_mutated": bool((skill_update or {}).get("repository_mutated")),
        "skill_transition": (skill_update or {}).get("transition"),
        "skill_update_blocked_reason": (skill_update or {}).get("blocked_reason"),
        "skill_update_protected_status": bool((skill_update or {}).get("protected_status")),
        "skill_update_validation_gate_passed": bool((skill_update or {}).get("validation_gate", {}).get("passed")),
        "skill_update_evolution_ledger_exists": bool(
            (skill_update or {}).get("evolution_ledger")
            and Path(str((skill_update or {}).get("evolution_ledger"))).exists()
        ),
        "skill_evolution_application_exists": skill_evolution_application is not None,
        "skill_evolution_application_applied_count": (skill_evolution_application or {}).get("applied_count", 0),
        "skill_evolution_application_transition_count": (skill_evolution_application or {}).get("transition_count", 0),
        "skill_evolution_application_dry_run": (skill_evolution_application or {}).get("dry_run"),
        "skill_evolution_application_skipped_count": (skill_evolution_application or {}).get("skipped_count", 0),
        "skill_evolution_application_transitions": [
            item.get("transition")
            for item in (skill_evolution_application or {}).get("transitions", [])
            if isinstance(item, dict) and item.get("transition")
        ],
        "global_memory_recorded": global_memory_record is not None,
        **memory_mode,
        "memory_item_count": len(memory_items),
        "memory_namespaces": sorted({str(item.get("namespace")) for item in memory_items if isinstance(item, dict)}),
        "durable_checkpoint_count": (durable_summary or {}).get("checkpoint_count", 0),
        "durable_step_count": (durable_summary or {}).get("step_count", 0),
        "durable_event_count": (durable_summary or {}).get("event_count", 0),
        "durable_handoff_count": (durable_summary or {}).get("handoff_count", 0),
        "durable_task_count": (durable_summary or {}).get("task_count", 0),
        "durable_leased_task_count": (durable_summary or {}).get("leased_task_count", 0),
        "durable_worker_count": (durable_summary or {}).get("worker_count", 0),
        "durable_step_status_counts": (durable_summary or {}).get("step_status_counts", {}),
        "durable_task_status_counts": (durable_summary or {}).get("task_status_counts", {}),
        "typed_event_count": len(typed_events),
        "typed_event_distribution": dict(typed_event_counter),
        "typed_handoff_event_count": typed_event_counter.get("handoff", 0),
        "typed_step_start_event_count": typed_event_counter.get("step_start", 0),
        "typed_step_end_event_count": typed_event_counter.get("step_end", 0),
        "scheduler_task_count": len(scheduler_tasks),
        "scheduler_history_count": len(scheduler_history),
        "scheduler_lifecycle_events": dict(Counter(str(item.get("event")) for item in scheduler_history if isinstance(item, dict))),
        "runtime_service_event_count": len(runtime_service_events),
        "root_runtime_service_event_count": len(root_service_events),
        "runtime_service_events": dict(service_events),
        "http_auth_failure_events": service_events.get("http_auth_failure", 0),
        "http_memory_query_events": service_events.get("http_memory_query", 0),
        "http_memory_summary_events": service_events.get("http_memory_summary", 0),
        "http_memory_retrieve_events": service_events.get("http_memory_retrieve", 0),
        "http_memory_append_events": service_events.get("http_memory_append", 0),
        "http_hitl_approve_events": service_events.get("http_hitl_approve", 0),
        "http_resume_events": service_events.get("http_resume", 0),
        "http_task_list_events": service_events.get("http_task_list", 0),
        "http_task_submit_events": service_events.get("http_task_submit", 0),
        "http_worker_once_events": service_events.get("http_worker_once", 0),
        "http_worker_lease_events": service_events.get("http_worker_lease", 0),
        "http_worker_complete_events": service_events.get("http_worker_complete", 0),
        "http_worker_heartbeat_events": service_events.get("http_worker_heartbeat", 0),
        "http_worker_register_events": service_events.get("http_worker_register", 0),
        "http_worker_registry_heartbeat_events": service_events.get("http_worker_registry_heartbeat", 0),
        "http_adapter_invoke_events": service_events.get("http_adapter_invoke", 0),
        "runtime_api_adapter_result_exists": runtime_api_adapter_result is not None,
        "runtime_api_adapter_framework": (runtime_api_adapter_result or {}).get("framework"),
        "runtime_api_adapter_invocation_type": (runtime_api_adapter_result or {}).get("invocation_type"),
        "runtime_api_adapter_status": (runtime_api_adapter_result or {}).get("status"),
        "runtime_api_adapter_submitted_task_count": 1 if isinstance(adapter_result_task, dict) else 0,
        "runtime_api_adapter_resume_status": adapter_result_resume.get("status") if isinstance(adapter_result_resume, dict) else None,
        "runtime_api_adapter_event_count": typed_event_counter.get("runtime_api_adapter.invoke", 0),
        "native_langgraph_result_exists": native_langgraph is not None,
        "native_langgraph_status": (native_langgraph or {}).get("status"),
        "native_langgraph_backend_validation_source": (native_langgraph or {}).get("backend_validation_source"),
        "native_langgraph_backend_invocation_class": native_langgraph_invocation_class(native_langgraph),
        "native_langgraph_graph_mode": (native_langgraph or {}).get("graph_mode"),
        "native_langgraph_available": bool((native_langgraph or {}).get("native_runtime_available")),
        "native_langgraph_invoked": bool((native_langgraph or {}).get("invoked")),
        "native_langgraph_resumed": bool((native_langgraph or {}).get("resumed")),
        "native_langgraph_state_observed": bool((native_langgraph or {}).get("state_observed")),
        "runtime_parity_exists": runtime_parity is not None,
        "runtime_parity_status": (runtime_parity or {}).get("summary", {}).get("status"),
        "runtime_parity_score": (runtime_parity or {}).get("summary", {}).get("parity_score", 0),
        "runtime_advantage_score": (runtime_parity or {}).get("summary", {}).get("advantage_score", 0),
        "runtime_parity_failed": (runtime_parity or {}).get("summary", {}).get("failed_parity", []),
        "runtime_advantage_failed": (runtime_parity or {}).get("summary", {}).get("failed_advantage", []),
        "registered_worker_count": len((worker_registry or {}).get("workers", {})),
        "active_registered_worker_count": sum(
            1
            for worker in (worker_registry or {}).get("workers", {}).values()
            if isinstance(worker, dict) and worker.get("last_seen_epoch")
        ),
        "worker_lease_events": sum(1 for item in scheduler_history if item.get("event") == "task_leased"),
        "worker_heartbeat_events": sum(1 for item in scheduler_history if item.get("event") == "task_heartbeat"),
        "worker_lease_expired_events": sum(1 for item in scheduler_history if item.get("event") == "task_lease_expired"),
        "human_gate_requested": bool(hitl_requested),
        "human_gate_approved": (human_gate or {}).get("status") == "approved",
        "openai_trace_schema": (openai_trace or {}).get("schema"),
        "langgraph_state_schema": (langgraph_state or {}).get("schema"),
        "autogen_trace_schema": (autogen_trace or {}).get("schema"),
        "crewai_trace_schema": (crewai_trace or {}).get("schema"),
        "trace_alignment_status": (trace_alignment or {}).get("status"),
        "trace_alignment_score": (trace_alignment or {}).get("alignment_score", 0),
        "openai_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("openai_agents", {}).get("passed")),
        "langgraph_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("langgraph", {}).get("passed")),
        "autogen_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("autogen_core", {}).get("passed")),
        "crewai_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("crewai", {}).get("passed")),
        "typed_handoff_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("typed_handoff", {}).get("passed")),
        "durable_execution_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("durable_execution", {}).get("passed")),
        "typed_event_bus_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("typed_event_bus", {}).get("passed")),
        "resume_graph_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("resume_graph", {}).get("passed")),
        "framework_adapter_alignment_passed": bool((trace_alignment or {}).get("checks", {}).get("framework_adapter_bundle", {}).get("passed")),
        "openai_trace_exists": (run_dir / "openai_agents_trace.json").exists(),
        "langgraph_state_exists": (run_dir / "langgraph_state.json").exists(),
        "autogen_trace_exists": (run_dir / "autogen_trace.json").exists(),
        "crewai_trace_exists": (run_dir / "crewai_trace.json").exists(),
        "framework_adapter_bundle_exists": (run_dir / "framework_adapter_bundle.json").exists(),
        "framework_adapter_status": (adapter_bundle or {}).get("coverage", {}).get("status"),
        "framework_adapter_score": (adapter_bundle or {}).get("coverage", {}).get("coverage_score", 0),
        "framework_contract_status": framework_contracts.get("status"),
        "framework_contract_score": framework_contracts.get("contract_score", 0),
        "framework_contract_check_count": framework_contracts.get("check_count", len(framework_contract_checks)),
        "framework_contract_passed_count": framework_contracts.get("passed_count", 0),
        "framework_contract_failed": framework_contract_failed,
        "framework_contract_openai_passed": bool(framework_contracts.get("frameworks", {}).get("openai_agents", {}).get("passed")),
        "framework_contract_langgraph_passed": bool(framework_contracts.get("frameworks", {}).get("langgraph", {}).get("passed")),
        "framework_contract_autogen_passed": bool(framework_contracts.get("frameworks", {}).get("autogen_core", {}).get("passed")),
        "framework_contract_crewai_passed": bool(framework_contracts.get("frameworks", {}).get("crewai", {}).get("passed")),
        "framework_adapter_openai_concepts": (adapter_bundle or {}).get("coverage", {}).get("concept_count", {}).get("openai_agents", 0),
        "framework_adapter_langgraph_concepts": (adapter_bundle or {}).get("coverage", {}).get("concept_count", {}).get("langgraph", 0),
        "framework_adapter_autogen_concepts": (adapter_bundle or {}).get("coverage", {}).get("concept_count", {}).get("autogen_core", 0),
        "framework_adapter_crewai_concepts": (adapter_bundle or {}).get("coverage", {}).get("concept_count", {}).get("crewai", 0),
        "trace_alignment_exists": (run_dir / "trace_schema_alignment.json").exists(),
        "resume_plan_exists": (run_dir / "resume_plan.json").exists(),
        "resume_audit_exists": (run_dir / "resume_audit.json").exists(),
        "resume_result_exists": (run_dir / "resume_result.json").exists(),
        "resume_planned_step_count": len((resume_plan or {}).get("planned_steps", [])),
        "resume_graph_node_count": len(resume_graph_nodes) if isinstance(resume_graph_nodes, list) else 0,
        "resume_graph_edge_count": len(resume_graph_edges) if isinstance(resume_graph_edges, list) else 0,
        "resume_graph_dependency_edge_count": len(resume_graph.get("dependency_edges", [])) if isinstance(resume_graph, dict) and isinstance(resume_graph.get("dependency_edges", []), list) else 0,
        "resume_graph_execution_edge_count": len(resume_graph.get("execution_edges", [])) if isinstance(resume_graph, dict) and isinstance(resume_graph.get("execution_edges", []), list) else 0,
        "resume_graph_replayable_step_count": resume_graph.get("replayable_step_count", 0) if isinstance(resume_graph, dict) else 0,
        "resume_graph_non_replayable_step_count": resume_graph.get("non_replayable_step_count", 0) if isinstance(resume_graph, dict) else 0,
        "resume_graph_queued_task_replay_count": resume_graph.get("queued_task_replay_count", 0) if isinstance(resume_graph, dict) else 0,
        "resume_graph_skipped_unselected_step_count": resume_graph.get("skipped_unselected_step_count", 0) if isinstance(resume_graph, dict) else 0,
        "resume_graph_replay_selection_edge_count": len(resume_graph.get("replay_selection_edges", [])) if isinstance(resume_graph, dict) else 0,
        "resume_audit_status": (resume_audit or {}).get("status"),
        "resume_audit_planned_step_count": resume_audit_summary.get("planned_step_count", 0),
        "resume_audit_replayed_count": resume_audit_summary.get("replayed_count", 0),
        "resume_audit_safe_to_replay_count": resume_audit_summary.get("safe_to_replay_count", 0),
        "resume_audit_unsafe_to_replay_count": resume_audit_summary.get("unsafe_to_replay_count", 0),
        "resume_audit_skipped_count": resume_audit_summary.get("skipped_count", 0),
        "resume_audit_failed_count": resume_audit_summary.get("failed_count", 0),
        "resume_audit_dependency_node_count": resume_audit_summary.get("dependency_node_count", 0),
        "resume_audit_dependency_edge_count": resume_audit_summary.get("dependency_edge_count", 0),
        "resume_audit_resume_dependency_edge_count": len(resume_audit_graph.get("resume_dependency_edges", [])) if isinstance(resume_audit_graph, dict) else 0,
        "resume_audit_artifact_dependency_edge_count": len(resume_audit_graph.get("artifact_dependency_edges", [])) if isinstance(resume_audit_graph, dict) else 0,
        "resume_audit_artifact_generation_edge_count": len(resume_audit_graph.get("artifact_generation_edges", [])) if isinstance(resume_audit_graph, dict) else 0,
        "resume_audit_replay_selection_edge_count": len(resume_audit_graph.get("replay_selection_edges", [])) if isinstance(resume_audit_graph, dict) else 0,
        "resume_audit_skipped_unselected_step_count": resume_audit_summary.get("skipped_unselected_step_count", 0),
        "resume_audit_input_hash_changed_count": resume_audit_summary.get("input_hash_changed_count", 0),
        "resume_audit_input_hash_missing_count": resume_audit_summary.get("input_hash_missing_count", 0),
        "resume_audit_input_hash_unverified_count": resume_audit_summary.get("input_hash_unverified_count", 0),
        "resume_audit_input_hash_ok_count": resume_audit_summary.get("input_hash_ok_count", 0),
        "resume_audit_input_hash_pending_replay_output_count": resume_audit_summary.get("input_hash_pending_replay_output_count", 0),
        "resume_audit_side_effect_safe_count": resume_audit_summary.get("side_effect_safe_count", 0),
        "resume_audit_side_effect_unsafe_count": resume_audit_summary.get("side_effect_unsafe_count", 0),
        "resume_audit_scx_load_replay_count": resume_audit_summary.get("scx_load_replay_count", 0),
        "resume_audit_service_restart_replay_count": resume_audit_summary.get("service_restart_replay_count", 0),
        "resume_audit_benchmark_replay_count": resume_audit_summary.get("benchmark_replay_count", 0),
        "resume_audit_root_control_replay_count": resume_audit_summary.get("root_control_replay_count", 0),
        "durable_resume_step_count": len(durable_resume_view.get("resumable_steps", [])) if isinstance(durable_resume_view, dict) else 0,
        "durable_resume_task_count": len(durable_resume_view.get("resumable_command_tasks", [])) if isinstance(durable_resume_view, dict) else 0,
        "durable_resume_source": durable_resume_view.get("source") if isinstance(durable_resume_view, dict) else None,
        "resume_status": (resume_result or {}).get("status"),
        "hitl_service_approval_recorded": hitl_approval_result is not None,
        "hitl_service_resume_returncode": (hitl_approval_result or {}).get("resume_returncode"),
        "worker_result_recorded": worker_result is not None,
        "worker_status": (worker_result or {}).get("status"),
        "worker_pool_result_recorded": worker_pool_result is not None,
        "worker_pool_status": (worker_pool_result or {}).get("status"),
        "worker_pool_worker_count": (worker_pool_result or {}).get("worker_count", 0),
        "worker_pool_attempt_count": (worker_pool_result or {}).get("attempt_count", 0),
        "worker_pool_completed_count": (worker_pool_result or {}).get("completed_count", 0),
        "worker_pool_failed_count": (worker_pool_result or {}).get("failed_count", 0),
        "worker_pool_no_task_count": (worker_pool_result or {}).get("no_task_count", 0),
        "worker_pool_parallelism_observed": len((worker_pool_result or {}).get("worker_iterations", {})) if isinstance((worker_pool_result or {}).get("worker_iterations", {}), dict) else 0,
        "worker_complete_recorded": worker_complete is not None,
        "worker_complete_ok": (worker_complete or {}).get("ok"),
        "tasks_view_recorded": tasks_view is not None,
        "tasks_view_task_count": (tasks_view or {}).get("task_count", 0),
        "tasks_view_durable_task_count": (tasks_view or {}).get("durable_task_count", 0),
        "task_submission_recorded": task_submission is not None,
        "task_submission_status": (task_submission or {}).get("status"),
        "task_submission_role": (task_submission or {}).get("task", {}).get("role") if isinstance((task_submission or {}).get("task"), dict) else None,
        "same_batch_validation_exists": same_batch_validation is not None,
        "same_batch_validation_passed": bool((same_batch_validation or {}).get("trajectory_valid")),
        "same_batch_validation_verdict": (same_batch_validation or {}).get("verdict"),
        "same_batch_improvement_pct": (same_batch_validation or {}).get("validation", {}).get("improvement_pct"),
        "same_batch_has_default_control": (
            (same_batch_validation or {}).get("comparison_arms", {}).get("arms", {}).get("default", {}).get("role")
            == "default_control"
        ),
        "same_batch_has_fixed_scx_treatment": any(
            isinstance(item, dict) and item.get("role") == "fixed_scx_treatment"
            for item in (same_batch_validation or {}).get("comparison_arms", {}).get("arms", {}).values()
        ),
        "same_batch_has_kernadapt_online": any(
            isinstance(item, dict) and item.get("role") in {"kernadapt_online", "lockagent_online"}
            for item in (same_batch_validation or {}).get("comparison_arms", {}).get("arms", {}).values()
        ),
        "same_batch_has_lockagent_online": any(
            isinstance(item, dict) and item.get("role") in {"kernadapt_online", "lockagent_online"}
            for item in (same_batch_validation or {}).get("comparison_arms", {}).get("arms", {}).values()
        ),
        "same_batch_has_oracle_best_eval_only": any(
            isinstance(item, dict) and item.get("role") == "oracle_best_evaluation_only"
            for item in (same_batch_validation or {}).get("comparison_arms", {}).get("arms", {}).values()
        ),
        "counterfactual_plan_exists": counterfactual_plan is not None,
        "counterfactual_required": bool((counterfactual_plan or {}).get("requires_counterfactual")),
        "counterfactual_task_count": (counterfactual_plan or {}).get("task_count", 0),
        "counterfactual_executable_task_count": (counterfactual_plan or {}).get("executable_task_count", 0),
        "counterfactual_submitted_task_count": (counterfactual_plan or {}).get("submission", {}).get("submitted_count", 0),
        "counterfactual_skill_update_planned": bool(plan_skill_evolution.get("update_repository")) or bool(executable_skill_updates),
        "counterfactual_skill_update_task_count": len(executable_skill_updates),
        "counterfactual_skill_update_repository": plan_skill_evolution.get("repository"),
        "counterfactual_execution_exists": counterfactual_execution is not None,
        "counterfactual_execution_status": (counterfactual_execution or {}).get("status"),
        "counterfactual_execution_dry_run": (counterfactual_execution or {}).get("dry_run"),
    }


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    failures = Counter()
    skills = Counter()
    for row in rows:
        failures.update(row.get("failure_categories", []))
        if row.get("skill"):
            skills[row["skill"]] += 1
    if n == 0:
        return {"run_count": 0}
    dry_run_memory_rows = [row for row in rows if row.get("memory_isolation_dry_run") is True]
    production_memory_rows = [row for row in rows if row.get("memory_isolation_dry_run") is False]
    return {
        "run_count": n,
        "artifact_complete_rate": sum(1 for row in rows if row["artifact_complete"]) / n,
        "evaluation_mode_distribution": dict(
            Counter(row["evaluation_mode"] for row in rows if row.get("evaluation_mode"))
        ),
        "evaluation_artifact_complete_rate": sum(
            1 for row in rows if row["evaluation_artifact_complete"]
        )
        / n,
        "avg_evaluation_missing_artifacts": sum(row["evaluation_missing_artifact_count"] for row in rows) / n,
        "evaluation_missing_artifact_distribution": dict(
            Counter(artifact for row in rows for artifact in row.get("evaluation_missing_artifacts", []))
        ),
        "avg_evaluation_optional_missing_artifacts": sum(
            row["evaluation_optional_missing_artifact_count"] for row in rows
        )
        / n,
        "evaluation_optional_missing_artifact_distribution": dict(
            Counter(artifact for row in rows for artifact in row.get("evaluation_optional_missing_artifacts", []))
        ),
        "optional_process_artifact_complete_rate": sum(
            1 for row in rows if row["optional_process_artifact_complete"]
        )
        / n,
        "fallback_artifacts_record_rate": sum(1 for row in rows if row["fallback_artifacts_exists"]) / n,
        "fallback_artifacts_created_rate": sum(1 for row in rows if row["fallback_artifacts_created"]) / n,
        "avg_fallback_artifacts": sum(row["fallback_artifact_count"] for row in rows) / n,
        "fallback_performance_safe_rate": sum(
            1 for row in rows if row["fallback_safe_for_performance_claims"] is True
        )
        / n,
        "valid_decision_rate": sum(1 for row in rows if row["valid_decision"]) / n,
        "guard_block_rate": sum(1 for row in rows if row["guard_blocked"]) / n,
        "avg_policy_engine_checks": sum(row["policy_engine_check_count"] for row in rows) / n,
        "avg_policy_engine_rules": sum(row["policy_engine_rule_count"] for row in rows) / n,
        "policy_engine_category_distribution": dict(
            Counter(category for row in rows for category in row.get("policy_engine_rule_categories", []))
        ),
        "policy_engine_owner_role_distribution": dict(
            Counter(owner for row in rows for owner in row.get("policy_engine_rule_owner_roles", []))
        ),
        "avg_policy_engine_missing_metadata": sum(row["policy_engine_missing_metadata_count"] for row in rows) / n,
        "policy_engine_missing_metadata_distribution": dict(
            Counter(rule for row in rows for rule in row.get("policy_engine_missing_metadata_rules", []))
        ),
        "policy_engine_blocking_rule_distribution": dict(
            Counter(rule for row in rows for rule in row.get("policy_engine_blocking_rules", []))
        ),
        "policy_engine_warning_rule_distribution": dict(
            Counter(rule for row in rows for rule in row.get("policy_engine_warning_rules", []))
        ),
        "policy_engine_oracle_rule_coverage_rate": sum(
            1 for row in rows if row["policy_engine_has_oracle_leakage_rule"]
        )
        / n,
        "policy_engine_evidence_resolution_rule_coverage_rate": sum(
            1 for row in rows if row["policy_engine_has_evidence_resolution_rule"]
        )
        / n,
        "policy_engine_lock_signal_rule_coverage_rate": sum(
            1 for row in rows if row["policy_engine_has_lock_signal_rule"]
        )
        / n,
        "policy_audit_record_rate": sum(1 for row in rows if row["policy_audit_exists"]) / n,
        "policy_audit_ok_rate": (
            sum(1 for row in rows if row["policy_audit_status"] == "ok") / max(1, sum(1 for row in rows if row["policy_audit_exists"]))
            if any(row["policy_audit_exists"] for row in rows)
            else None
        ),
        "policy_audit_required_rules_pass_rate": (
            sum(1 for row in rows if row["policy_audit_required_rules_passed"]) / max(1, sum(1 for row in rows if row["policy_audit_exists"]))
            if any(row["policy_audit_exists"] for row in rows)
            else None
        ),
        "policy_audit_metadata_complete_rate": (
            sum(1 for row in rows if row["policy_audit_metadata_complete"]) / max(1, sum(1 for row in rows if row["policy_audit_exists"]))
            if any(row["policy_audit_exists"] for row in rows)
            else None
        ),
        "avg_policy_audit_unclassified_branches": (
            sum(row["policy_audit_unclassified_branch_count"] for row in rows if row["policy_audit_exists"])
            / max(1, sum(1 for row in rows if row["policy_audit_exists"]))
            if any(row["policy_audit_exists"] for row in rows)
            else None
        ),
        "policy_audit_branch_classification_distribution": dict(
            Counter(
                classification
                for row in rows
                for classification, count in row.get("policy_audit_branch_classifications", {}).items()
                for _ in range(int(count))
            )
        ),
        "rollback_success_rate": sum(1 for row in rows if row["rollback_success"]) / n,
        "executor_failure_class_distribution": dict(
            Counter(row["executor_failure_class"] for row in rows if row.get("executor_failure_class"))
        ),
        "executor_error_record_rate": sum(1 for row in rows if row["executor_error_recorded"]) / n,
        "trajectory_valid_rate": sum(1 for row in rows if row["trajectory_valid"]) / n,
        "capability_denied_count": sum(len(row["capability_denied"]) for row in rows),
        "failure_distribution": dict(failures),
        "skill_hit_distribution": dict(skills),
        "avg_dependency_nodes": sum(row["dependency_nodes"] for row in rows) / n,
        "avg_dependency_edges": sum(row["dependency_edges"] for row in rows) / n,
        "avg_execution_edges": sum(row["execution_edge_count"] for row in rows) / n,
        "avg_explicit_dependency_edges": sum(row["dependency_edge_count"] for row in rows) / n,
        "avg_handoff_edges": sum(row["handoff_edge_count"] for row in rows) / n,
        "avg_artifact_edges": sum(row["artifact_edge_count"] for row in rows) / n,
        "avg_blame_edges": sum(row["blame_edge_count"] for row in rows) / n,
        "avg_diagnostic_edges": sum(row["diagnostic_edge_count"] for row in rows) / n,
        "avg_regression_blame_chains_graph": sum(row["regression_blame_chain_count_graph"] for row in rows) / n,
        "dependency_blame_graph_rate": sum(1 for row in rows if row["dependency_blame_graph_node_count"] > 0) / n,
        "avg_dependency_blame_graph_nodes": sum(row["dependency_blame_graph_node_count"] for row in rows) / n,
        "avg_dependency_blame_graph_edges": sum(row["dependency_blame_graph_edge_count"] for row in rows) / n,
        "avg_fine_grained_blame_paths": sum(row["fine_grained_blame_path_count"] for row in rows) / n,
        "avg_causal_dependency_edges": sum(row["causal_dependency_edge_count"] for row in rows) / n,
        "avg_causal_dependency_chains_graph": sum(row["causal_dependency_chain_count_graph"] for row in rows) / n,
        "avg_causal_chains": sum(row["causal_chain_count"] for row in rows) / n,
        "avg_strong_causal_chains": sum(row["strong_causal_chain_count"] for row in rows) / n,
        "avg_validated_causal_chains": sum(row["validated_causal_chain_count"] for row in rows) / n,
        "avg_refuted_causal_chains": sum(row["refuted_causal_chain_count"] for row in rows) / n,
        "avg_inconclusive_causal_chains": sum(row["inconclusive_causal_chain_count"] for row in rows) / n,
        "causal_same_batch_required_rate": sum(1 for row in rows if row["causal_requires_same_batch"]) / n,
        "avg_causal_blame_nodes": sum(row["causal_blame_node_count"] for row in rows) / n,
        "avg_causal_blame_edges": sum(row["causal_blame_edge_count"] for row in rows) / n,
        "causal_profile_skill_scheduler_path_rate": sum(1 for row in rows if row["causal_profile_skill_scheduler_path"]) / n,
        "causal_profile_skill_decision_action_outcome_path_rate": sum(
            1 for row in rows if row["causal_profile_skill_decision_action_outcome_path"]
        )
        / n,
        "avg_fine_grained_causal_chains": sum(row["fine_grained_causal_chain_count"] for row in rows) / n,
        "avg_causal_dependency_chains_report": sum(row["causal_dependency_chain_count_report"] for row in rows) / n,
        "avg_regression_blame_chains_report": sum(row["regression_blame_chain_count_report"] for row in rows) / n,
        "bad_profile_bad_skill_scheduler_regression_path_rate": sum(
            1 for row in rows if row["bad_profile_bad_skill_scheduler_regression_path"]
        )
        / n,
        "bad_profile_to_regression_path_rate": sum(1 for row in rows if row["bad_profile_to_regression_path"]) / n,
        "skill_to_regression_path_rate": sum(1 for row in rows if row["skill_to_regression_path"]) / n,
        "scheduler_to_regression_path_rate": sum(1 for row in rows if row["scheduler_to_regression_path"]) / n,
        "avg_causal_root_candidates": sum(row["causal_root_candidate_count"] for row in rows) / n,
        "blame_path_runs": sum(1 for row in rows if row["blame_path_count"] > 0),
        "systemic_action_runs": sum(1 for row in rows if row["systemic_action_count"] > 0),
        "system_evolution_state_rate": sum(1 for row in rows if row["system_evolution_state_exists"]) / n,
        "avg_system_evolution_history": sum(row["system_evolution_history_count"] for row in rows) / n,
        "avg_system_evolution_override_history": sum(row["system_evolution_override_history_count"] for row in rows) / n,
        "avg_system_evolution_template_history": sum(row["system_evolution_template_history_count"] for row in rows) / n,
        "system_evolution_selected_template_distribution": dict(
            Counter(row["system_evolution_selected_template"] for row in rows if row.get("system_evolution_selected_template"))
        ),
        "avg_system_evolution_failure_totals": sum(row["system_evolution_failure_total_count"] for row in rows) / n,
        "systemic_override_runs": sum(1 for row in rows if row["systemic_override_count"] > 0),
        "systemic_override_application_rate": sum(1 for row in rows if row["systemic_override_application_exists"]) / n,
        "systemic_override_applied_runs": sum(1 for row in rows if row["systemic_override_applied_count"] > 0),
        "systemic_override_applied_field_distribution": dict(
            Counter(field for row in rows for field in row.get("systemic_override_applied_fields", []))
        ),
        "systemic_override_disablelist_runs": sum(1 for row in rows if row.get("systemic_override_disablelist")),
        "meta_template_applied_distribution": dict(
            Counter(row["meta_template_applied"] for row in rows if row.get("meta_template_applied"))
        ),
        "avg_meta_execution_graph_nodes": sum(row["meta_execution_graph_nodes"] for row in rows) / n,
        "avg_meta_execution_graph_edges": sum(row["meta_execution_graph_edges"] for row in rows) / n,
        "avg_meta_execution_graph_step_nodes": sum(row["meta_execution_graph_step_nodes"] for row in rows) / n,
        "avg_meta_execution_graph_step_edges": sum(row["meta_execution_graph_step_edges"] for row in rows) / n,
        "meta_template_graph_applied_rate": sum(1 for row in rows if row.get("meta_template_graph_applied_to_runtime")) / n,
        "meta_template_runtime_policy_mode_distribution": dict(
            Counter(row["meta_template_runtime_policy_mode"] for row in rows if row.get("meta_template_runtime_policy_mode"))
        ),
        "avg_meta_template_runtime_skipped_steps": sum(row["meta_template_runtime_skipped_step_count"] for row in rows) / n,
        "meta_template_runtime_skip_reason_distribution": dict(
            Counter(
                reason
                for row in rows
                for reason, count in row.get("meta_template_runtime_skip_reasons", {}).items()
                for _ in range(int(count))
            )
        ),
        "avg_meta_execution_graph_declared_steps": sum(row["meta_execution_graph_declared_step_count"] for row in rows) / n,
        "avg_meta_execution_graph_observed_declared_steps": sum(row["meta_execution_graph_observed_declared_step_count"] for row in rows) / n,
        "avg_meta_execution_graph_observed_declared_gap": sum(row["meta_execution_graph_observed_declared_gap"] for row in rows) / n,
        "observed_execution_graph_rate": sum(1 for row in rows if row["observed_execution_graph_exists"]) / n,
        "avg_observed_execution_graph_step_count": sum(row["observed_execution_graph_step_count"] for row in rows) / n,
        "avg_observed_execution_graph_declared_coverage": sum(row["observed_execution_graph_declared_coverage"] for row in rows) / n,
        "avg_observed_execution_graph_control_edges": sum(row["observed_execution_graph_control_edges"] for row in rows) / n,
        "avg_observed_execution_graph_artifact_dependency_edges": sum(row["observed_execution_graph_artifact_dependency_edges"] for row in rows) / n,
        "graph_scheduler_plan_rate": sum(1 for row in rows if row["graph_scheduler_plan_exists"]) / n,
        "avg_graph_scheduler_declared_steps": sum(row["graph_scheduler_declared_step_count"] for row in rows) / n,
        "avg_graph_scheduler_observed_declared_steps": sum(row["graph_scheduler_observed_declared_step_count"] for row in rows) / n,
        "avg_graph_scheduler_completed_steps": sum(row["graph_scheduler_completed_step_count"] for row in rows) / n,
        "avg_graph_scheduler_skipped_steps": sum(row["graph_scheduler_skipped_step_count"] for row in rows) / n,
        "avg_graph_scheduler_failed_steps": sum(row["graph_scheduler_failed_step_count"] for row in rows) / n,
        "avg_graph_scheduler_blocked_steps": sum(row["graph_scheduler_blocked_step_count"] for row in rows) / n,
        "avg_graph_scheduler_waiting_steps": sum(row["graph_scheduler_waiting_step_count"] for row in rows) / n,
        "avg_graph_scheduler_runnable_steps": sum(row["graph_scheduler_runnable_step_count"] for row in rows) / n,
        "avg_graph_scheduler_next_runnable_steps": sum(row["graph_scheduler_next_runnable_count"] for row in rows) / n,
        "avg_graph_scheduler_artifact_dependency_edges": sum(row["graph_scheduler_artifact_dependency_edge_count"] for row in rows) / n,
        "avg_graph_scheduler_failed_step_replay_candidates": sum(row["graph_scheduler_failed_step_replay_candidate_count"] for row in rows) / n,
        "avg_graph_scheduler_missing_required_artifacts": sum(row["graph_scheduler_missing_required_artifact_count"] for row in rows) / n,
        "skill_repository_update_runs": sum(1 for row in rows if row["skill_repository_updated"]),
        "skill_repository_mutation_runs": sum(1 for row in rows if row["skill_repository_mutated"]),
        "skill_repository_update_with_ledger_runs": sum(1 for row in rows if row["skill_update_evolution_ledger_exists"]),
        "skill_update_validation_gate_pass_rate": sum(1 for row in rows if row["skill_update_validation_gate_passed"]) / n,
        "skill_update_protected_status_count": sum(1 for row in rows if row["skill_update_protected_status"]),
        "skill_transition_distribution": dict(Counter(row["skill_transition"] for row in rows if row.get("skill_transition"))),
        "skill_evolution_application_rate": sum(1 for row in rows if row["skill_evolution_application_exists"]) / n,
        "skill_evolution_application_applied_count": sum(row["skill_evolution_application_applied_count"] for row in rows),
        "skill_evolution_application_transition_count": sum(row["skill_evolution_application_transition_count"] for row in rows),
        "skill_evolution_application_skipped_count": sum(row["skill_evolution_application_skipped_count"] for row in rows),
        "skill_evolution_application_dry_run_count": sum(
            1 for row in rows if row["skill_evolution_application_dry_run"]
        ),
        "skill_evolution_application_transition_distribution": dict(
            Counter(
                transition
                for row in rows
                for transition in row.get("skill_evolution_application_transitions", [])
            )
        ),
        "global_memory_record_rate": sum(1 for row in rows if row["global_memory_recorded"]) / n,
        "memory_mode_distribution": dict(Counter(row["memory_mode"] for row in rows if row.get("memory_mode"))),
        "memory_isolation_source_distribution": dict(
            Counter(row["memory_isolation_source"] for row in rows if row.get("memory_isolation_source"))
        ),
        "memory_path_scope_distribution": dict(
            Counter(row["memory_path_scope"] for row in rows if row.get("memory_path_scope"))
        ),
        "memory_isolation_enabled_rate": sum(1 for row in rows if row["memory_isolation_enabled"]) / n,
        "avg_memory_explicit_paths": sum(row["memory_explicit_path_count"] for row in rows) / n,
        "avg_memory_rewritten_paths": sum(row["memory_rewritten_path_count"] for row in rows) / n,
        "dry_run_memory_run_count": len(dry_run_memory_rows),
        "dry_run_memory_isolated_rate": (
            sum(1 for row in dry_run_memory_rows if row["memory_isolated_for_dry_run"]) / len(dry_run_memory_rows)
            if dry_run_memory_rows
            else None
        ),
        "dry_run_memory_shared_path_risk_rate": (
            sum(1 for row in dry_run_memory_rows if row["dry_run_memory_shared_path_risk"]) / len(dry_run_memory_rows)
            if dry_run_memory_rows
            else None
        ),
        "production_memory_run_count": len(production_memory_rows),
        "production_shared_memory_rate": (
            sum(1 for row in production_memory_rows if row["production_shared_memory"]) / len(production_memory_rows)
            if production_memory_rows
            else None
        ),
        "avg_memory_items": sum(row["memory_item_count"] for row in rows) / n,
        "avg_durable_checkpoints": sum(row["durable_checkpoint_count"] for row in rows) / n,
        "avg_durable_steps": sum(row["durable_step_count"] for row in rows) / n,
        "avg_durable_events": sum(row["durable_event_count"] for row in rows) / n,
        "avg_durable_handoffs": sum(row["durable_handoff_count"] for row in rows) / n,
        "avg_durable_tasks": sum(row["durable_task_count"] for row in rows) / n,
        "avg_durable_leased_tasks": sum(row["durable_leased_task_count"] for row in rows) / n,
        "max_durable_workers": max(row["durable_worker_count"] for row in rows),
        "avg_typed_events": sum(row["typed_event_count"] for row in rows) / n,
        "avg_typed_handoff_events": sum(row["typed_handoff_event_count"] for row in rows) / n,
        "typed_handoff_event_count": sum(row["typed_handoff_event_count"] for row in rows),
        "typed_step_start_event_count": sum(row["typed_step_start_event_count"] for row in rows),
        "typed_step_end_event_count": sum(row["typed_step_end_event_count"] for row in rows),
        "avg_scheduler_history_events": sum(row["scheduler_history_count"] for row in rows) / n,
        "runtime_service_event_count": sum(row["runtime_service_event_count"] for row in rows),
        "root_runtime_service_event_count": sum(row["root_runtime_service_event_count"] for row in rows),
        "http_auth_failure_event_count": sum(row["http_auth_failure_events"] for row in rows),
        "http_memory_query_event_count": sum(row["http_memory_query_events"] for row in rows),
        "http_memory_summary_event_count": sum(row["http_memory_summary_events"] for row in rows),
        "http_memory_retrieve_event_count": sum(row["http_memory_retrieve_events"] for row in rows),
        "http_memory_append_event_count": sum(row["http_memory_append_events"] for row in rows),
        "http_hitl_approve_event_count": sum(row["http_hitl_approve_events"] for row in rows),
        "http_resume_event_count": sum(row["http_resume_events"] for row in rows),
        "http_task_list_event_count": sum(row["http_task_list_events"] for row in rows),
        "http_task_submit_event_count": sum(row["http_task_submit_events"] for row in rows),
        "http_worker_once_event_count": sum(row["http_worker_once_events"] for row in rows),
        "http_worker_lease_event_count": sum(row["http_worker_lease_events"] for row in rows),
        "http_worker_complete_event_count": sum(row["http_worker_complete_events"] for row in rows),
        "http_worker_heartbeat_event_count": sum(row["http_worker_heartbeat_events"] for row in rows),
        "http_worker_register_event_count": sum(row["http_worker_register_events"] for row in rows),
        "http_worker_registry_heartbeat_event_count": sum(row["http_worker_registry_heartbeat_events"] for row in rows),
        "http_adapter_invoke_event_count": sum(row["http_adapter_invoke_events"] for row in rows),
        "runtime_api_adapter_result_rate": sum(1 for row in rows if row["runtime_api_adapter_result_exists"]) / n,
        "runtime_api_adapter_framework_distribution": dict(
            Counter(row["runtime_api_adapter_framework"] for row in rows if row.get("runtime_api_adapter_framework"))
        ),
        "runtime_api_adapter_invocation_distribution": dict(
            Counter(row["runtime_api_adapter_invocation_type"] for row in rows if row.get("runtime_api_adapter_invocation_type"))
        ),
        "runtime_api_adapter_submitted_task_count": sum(row["runtime_api_adapter_submitted_task_count"] for row in rows),
        "runtime_api_adapter_event_count": sum(row["runtime_api_adapter_event_count"] for row in rows),
        "runtime_api_adapter_resume_ok_rate": sum(
            1
            for row in rows
            if row["runtime_api_adapter_resume_status"] in {"ok", "nothing_to_resume"}
        )
        / n,
        "native_langgraph_result_rate": sum(1 for row in rows if row["native_langgraph_result_exists"]) / n,
        "native_langgraph_backend_validation_source_distribution": dict(
            Counter(
                row["native_langgraph_backend_validation_source"]
                for row in rows
                if row.get("native_langgraph_backend_validation_source")
            )
        ),
        "native_langgraph_backend_invocation_class_distribution": dict(
            Counter(row["native_langgraph_backend_invocation_class"] for row in rows)
        ),
        "native_langgraph_graph_mode_distribution": dict(
            Counter(row["native_langgraph_graph_mode"] for row in rows if row.get("native_langgraph_graph_mode"))
        ),
        "native_langgraph_real_backend_invoked_rate": sum(
            1 for row in rows if row["native_langgraph_backend_invocation_class"] == "real_backend_invoked"
        )
        / n,
        "native_langgraph_fake_tested_rate": sum(
            1 for row in rows if row["native_langgraph_backend_invocation_class"] == "fake_tested"
        )
        / n,
        "native_langgraph_unavailable_rate": sum(
            1 for row in rows if row["native_langgraph_backend_invocation_class"] == "unavailable"
        )
        / n,
        "native_langgraph_available_rate": sum(1 for row in rows if row["native_langgraph_available"]) / n,
        "native_langgraph_invoked_rate": sum(1 for row in rows if row["native_langgraph_invoked"]) / n,
        "native_langgraph_resume_rate": sum(1 for row in rows if row["native_langgraph_resumed"]) / n,
        "native_langgraph_state_observed_rate": sum(1 for row in rows if row["native_langgraph_state_observed"]) / n,
        "runtime_parity_rate": sum(1 for row in rows if row["runtime_parity_status"] == "native_not_weaker") / n,
        "avg_runtime_parity_score": sum(row["runtime_parity_score"] for row in rows) / n,
        "avg_runtime_advantage_score": sum(row["runtime_advantage_score"] for row in rows) / n,
        "runtime_parity_failed_distribution": dict(
            Counter(item for row in rows for item in row.get("runtime_parity_failed", []))
        ),
        "runtime_advantage_failed_distribution": dict(
            Counter(item for row in rows for item in row.get("runtime_advantage_failed", []))
        ),
        "registered_worker_count": max(row["registered_worker_count"] for row in rows),
        "active_registered_worker_count": max(row["active_registered_worker_count"] for row in rows),
        "worker_lease_event_count": sum(row["worker_lease_events"] for row in rows),
        "worker_heartbeat_event_count": sum(row["worker_heartbeat_events"] for row in rows),
        "worker_lease_expired_event_count": sum(row["worker_lease_expired_events"] for row in rows),
        "human_gate_request_rate": sum(1 for row in rows if row["human_gate_requested"]) / n,
        "human_gate_approval_rate": sum(1 for row in rows if row["human_gate_approved"]) / n,
        "openai_trace_complete_rate": sum(1 for row in rows if row["openai_trace_exists"]) / n,
        "langgraph_state_complete_rate": sum(1 for row in rows if row["langgraph_state_exists"]) / n,
        "autogen_trace_complete_rate": sum(1 for row in rows if row["autogen_trace_exists"]) / n,
        "crewai_trace_complete_rate": sum(1 for row in rows if row["crewai_trace_exists"]) / n,
        "framework_adapter_bundle_rate": sum(1 for row in rows if row["framework_adapter_bundle_exists"]) / n,
        "framework_adapter_alignment_rate": sum(1 for row in rows if row["framework_adapter_alignment_passed"]) / n,
        "avg_framework_adapter_score": sum(row["framework_adapter_score"] for row in rows) / n,
        "framework_contract_alignment_rate": sum(1 for row in rows if row["framework_contract_status"] == "aligned") / n,
        "avg_framework_contract_score": sum(row["framework_contract_score"] for row in rows) / n,
        "avg_framework_contract_checks": sum(row["framework_contract_check_count"] for row in rows) / n,
        "framework_contract_failed_distribution": dict(
            Counter(item for row in rows for item in row.get("framework_contract_failed", []))
        ),
        "framework_contract_openai_rate": sum(1 for row in rows if row["framework_contract_openai_passed"]) / n,
        "framework_contract_langgraph_rate": sum(1 for row in rows if row["framework_contract_langgraph_passed"]) / n,
        "framework_contract_autogen_rate": sum(1 for row in rows if row["framework_contract_autogen_passed"]) / n,
        "framework_contract_crewai_rate": sum(1 for row in rows if row["framework_contract_crewai_passed"]) / n,
        "avg_framework_adapter_concepts": sum(
            row["framework_adapter_openai_concepts"]
            + row["framework_adapter_langgraph_concepts"]
            + row["framework_adapter_autogen_concepts"]
            + row["framework_adapter_crewai_concepts"]
            for row in rows
        )
        / n,
        "trace_alignment_complete_rate": sum(1 for row in rows if row["trace_alignment_exists"]) / n,
        "avg_trace_alignment_score": sum(row["trace_alignment_score"] for row in rows) / n,
        "openai_alignment_rate": sum(1 for row in rows if row["openai_alignment_passed"]) / n,
        "langgraph_alignment_rate": sum(1 for row in rows if row["langgraph_alignment_passed"]) / n,
        "autogen_alignment_rate": sum(1 for row in rows if row["autogen_alignment_passed"]) / n,
        "crewai_alignment_rate": sum(1 for row in rows if row["crewai_alignment_passed"]) / n,
        "typed_handoff_alignment_rate": sum(1 for row in rows if row["typed_handoff_alignment_passed"]) / n,
        "durable_execution_alignment_rate": sum(1 for row in rows if row["durable_execution_alignment_passed"]) / n,
        "typed_event_bus_alignment_rate": sum(1 for row in rows if row["typed_event_bus_alignment_passed"]) / n,
        "resume_graph_alignment_rate": sum(1 for row in rows if row["resume_graph_alignment_passed"]) / n,
        "resume_plan_rate": sum(1 for row in rows if row["resume_plan_exists"]) / n,
        "resume_audit_rate": sum(1 for row in rows if row["resume_audit_exists"]) / n,
        "resume_result_rate": sum(1 for row in rows if row["resume_result_exists"]) / n,
        "durable_resume_plan_rate": sum(1 for row in rows if row["durable_resume_source"] == "durable_runtime.db") / n,
        "avg_resume_graph_nodes": sum(row["resume_graph_node_count"] for row in rows) / n,
        "avg_resume_graph_edges": sum(row["resume_graph_edge_count"] for row in rows) / n,
        "avg_resume_graph_dependency_edges": sum(row["resume_graph_dependency_edge_count"] for row in rows) / n,
        "avg_resume_graph_execution_edges": sum(row["resume_graph_execution_edge_count"] for row in rows) / n,
        "avg_resume_graph_replayable_steps": sum(row["resume_graph_replayable_step_count"] for row in rows) / n,
        "avg_resume_graph_non_replayable_steps": sum(row["resume_graph_non_replayable_step_count"] for row in rows) / n,
        "avg_resume_graph_queued_task_replays": sum(row["resume_graph_queued_task_replay_count"] for row in rows) / n,
        "avg_resume_graph_skipped_unselected_steps": sum(row["resume_graph_skipped_unselected_step_count"] for row in rows) / n,
        "avg_resume_graph_replay_selection_edges": sum(row["resume_graph_replay_selection_edge_count"] for row in rows) / n,
        "avg_resume_audit_replayed": sum(row["resume_audit_replayed_count"] for row in rows) / n,
        "avg_resume_audit_safe_to_replay": sum(row["resume_audit_safe_to_replay_count"] for row in rows) / n,
        "avg_resume_audit_unsafe_to_replay": sum(row["resume_audit_unsafe_to_replay_count"] for row in rows) / n,
        "avg_resume_audit_dependency_nodes": sum(row["resume_audit_dependency_node_count"] for row in rows) / n,
        "avg_resume_audit_dependency_edges": sum(row["resume_audit_dependency_edge_count"] for row in rows) / n,
        "avg_resume_audit_resume_dependency_edges": sum(row["resume_audit_resume_dependency_edge_count"] for row in rows) / n,
        "avg_resume_audit_artifact_dependency_edges": sum(row["resume_audit_artifact_dependency_edge_count"] for row in rows) / n,
        "avg_resume_audit_artifact_generation_edges": sum(row["resume_audit_artifact_generation_edge_count"] for row in rows) / n,
        "avg_resume_audit_replay_selection_edges": sum(row["resume_audit_replay_selection_edge_count"] for row in rows) / n,
        "avg_resume_audit_skipped_unselected_steps": sum(row["resume_audit_skipped_unselected_step_count"] for row in rows) / n,
        "avg_resume_audit_input_hash_changed": sum(row["resume_audit_input_hash_changed_count"] for row in rows) / n,
        "avg_resume_audit_input_hash_missing": sum(row["resume_audit_input_hash_missing_count"] for row in rows) / n,
        "avg_resume_audit_input_hash_unverified": sum(row["resume_audit_input_hash_unverified_count"] for row in rows) / n,
        "avg_resume_audit_input_hash_ok": sum(row["resume_audit_input_hash_ok_count"] for row in rows) / n,
        "avg_resume_audit_input_hash_pending_replay_output": sum(row["resume_audit_input_hash_pending_replay_output_count"] for row in rows) / n,
        "avg_resume_audit_side_effect_safe": sum(row["resume_audit_side_effect_safe_count"] for row in rows) / n,
        "avg_resume_audit_side_effect_unsafe": sum(row["resume_audit_side_effect_unsafe_count"] for row in rows) / n,
        "avg_resume_audit_scx_load_replays": sum(row["resume_audit_scx_load_replay_count"] for row in rows) / n,
        "avg_resume_audit_service_restart_replays": sum(row["resume_audit_service_restart_replay_count"] for row in rows) / n,
        "avg_resume_audit_benchmark_replays": sum(row["resume_audit_benchmark_replay_count"] for row in rows) / n,
        "avg_resume_audit_root_control_replays": sum(row["resume_audit_root_control_replay_count"] for row in rows) / n,
        "avg_durable_resume_steps": sum(row["durable_resume_step_count"] for row in rows) / n,
        "avg_durable_resume_tasks": sum(row["durable_resume_task_count"] for row in rows) / n,
        "hitl_service_approval_rate": sum(1 for row in rows if row["hitl_service_approval_recorded"]) / n,
        "worker_result_rate": sum(1 for row in rows if row["worker_result_recorded"]) / n,
        "worker_pool_result_rate": sum(1 for row in rows if row["worker_pool_result_recorded"]) / n,
        "worker_pool_ok_rate": sum(1 for row in rows if row["worker_pool_status"] == "ok") / n,
        "max_worker_pool_workers": max(row["worker_pool_worker_count"] for row in rows),
        "max_worker_pool_parallelism_observed": max(row["worker_pool_parallelism_observed"] for row in rows),
        "worker_pool_completed_task_count": sum(row["worker_pool_completed_count"] for row in rows),
        "worker_pool_failed_task_count": sum(row["worker_pool_failed_count"] for row in rows),
        "worker_complete_rate": sum(1 for row in rows if row["worker_complete_recorded"]) / n,
        "tasks_view_rate": sum(1 for row in rows if row["tasks_view_recorded"]) / n,
        "task_submission_rate": sum(1 for row in rows if row["task_submission_recorded"]) / n,
        "avg_tasks_view_tasks": sum(row["tasks_view_task_count"] for row in rows) / n,
        "avg_tasks_view_durable_tasks": sum(row["tasks_view_durable_task_count"] for row in rows) / n,
        "same_batch_validation_rate": sum(1 for row in rows if row["same_batch_validation_exists"]) / n,
        "same_batch_validation_pass_rate": sum(1 for row in rows if row["same_batch_validation_passed"]) / n,
        "same_batch_default_control_rate": sum(1 for row in rows if row["same_batch_has_default_control"]) / n,
        "same_batch_fixed_scx_treatment_rate": sum(1 for row in rows if row["same_batch_has_fixed_scx_treatment"]) / n,
        "same_batch_kernadapt_online_rate": sum(1 for row in rows if row["same_batch_has_kernadapt_online"]) / n,
        "same_batch_lockagent_online_rate": sum(1 for row in rows if row["same_batch_has_kernadapt_online"]) / n,
        "same_batch_oracle_best_eval_only_rate": sum(1 for row in rows if row["same_batch_has_oracle_best_eval_only"]) / n,
        "counterfactual_plan_rate": sum(1 for row in rows if row["counterfactual_plan_exists"]) / n,
        "counterfactual_required_rate": sum(1 for row in rows if row["counterfactual_required"]) / n,
        "counterfactual_task_count": sum(row["counterfactual_task_count"] for row in rows),
        "counterfactual_executable_task_count": sum(row["counterfactual_executable_task_count"] for row in rows),
        "counterfactual_submitted_task_count": sum(row["counterfactual_submitted_task_count"] for row in rows),
        "counterfactual_skill_update_planned_rate": sum(1 for row in rows if row["counterfactual_skill_update_planned"]) / n,
        "counterfactual_skill_update_task_count": sum(row["counterfactual_skill_update_task_count"] for row in rows),
        "counterfactual_execution_rate": sum(1 for row in rows if row["counterfactual_execution_exists"]) / n,
        "counterfactual_execution_ok_rate": sum(1 for row in rows if row["counterfactual_execution_status"] in {"ok", "skipped"}) / n,
    }


def aggregate_batches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"batch_count": 0}
    return {
        "batch_count": n,
        "readiness_record_rate": sum(1 for row in rows if row["readiness_exists"]) / n,
        "readiness_ready_rate": sum(1 for row in rows if row["readiness_status"] == "ready") / n,
        "readiness_blocked_rate": sum(1 for row in rows if row["readiness_status"] == "blocked") / n,
        "check_only_count": sum(1 for row in rows if row["check_only"]),
        "dry_run_count": sum(1 for row in rows if row["dry_run"]),
        "total_trials": sum(row["trial_count"] for row in rows),
        "total_valid_trials": sum(row["valid_trial_count"] for row in rows),
        "readiness_blocking_reason_distribution": dict(
            Counter(reason for row in rows for reason in row.get("readiness_blocking_reasons", []))
        ),
        "readiness_blocking_check_distribution": dict(
            Counter(check for row in rows for check in row.get("readiness_blocking_checks", []))
        ),
        "readiness_warning_check_distribution": dict(
            Counter(check for row in rows for check in row.get("readiness_warning_checks", []))
        ),
        "sudo_noninteractive_blocked_count": sum(
            1 for row in rows if row.get("sudo_noninteractive_available") is False
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "run_id",
        "artifact_complete",
        "optional_process_artifact_complete",
        "optional_process_missing_artifacts",
        "evaluation_mode",
        "evaluation_required_artifact_count",
        "evaluation_missing_artifact_count",
        "evaluation_artifact_complete",
        "evaluation_missing_artifacts",
        "evaluation_optional_artifact_count",
        "evaluation_optional_missing_artifact_count",
        "evaluation_optional_missing_artifacts",
        "fallback_artifacts_exists",
        "fallback_artifacts_created",
        "fallback_artifact_count",
        "fallback_safe_for_performance_claims",
        "valid_decision",
        "policy_verdict",
        "guard_preflight_verdict",
        "guard_postflight_verdict",
        "guard_blocked",
        "policy_engine_check_count",
        "policy_engine_rule_count",
        "policy_engine_rule_names",
        "policy_engine_rule_categories",
        "policy_engine_rule_owner_roles",
        "policy_engine_missing_metadata_count",
        "policy_engine_missing_metadata_rules",
        "policy_engine_blocking_rule_count",
        "policy_engine_warning_rule_count",
        "policy_engine_blocking_rules",
        "policy_engine_warning_rules",
        "policy_engine_has_oracle_leakage_rule",
        "policy_engine_has_evidence_resolution_rule",
        "policy_engine_has_lock_signal_rule",
        "policy_audit_exists",
        "policy_audit_status",
        "policy_audit_required_rules_passed",
        "policy_audit_metadata_complete",
        "policy_audit_missing_required_rule_count",
        "policy_audit_mismatched_required_rule_count",
        "policy_audit_missing_metadata_rule_count",
        "policy_audit_local_branch_count",
        "policy_audit_unclassified_branch_count",
        "policy_audit_branch_classifications",
        "rollback_success",
        "executor_status",
        "executor_returncode",
        "executor_failure_class",
        "executor_error_recorded",
        "trajectory_valid",
        "failure_categories",
        "skill",
        "skill_verdict",
        "trace_step_count",
        "capability_denied",
        "dependency_nodes",
        "dependency_edges",
        "execution_edge_count",
        "dependency_edge_count",
        "handoff_edge_count",
        "artifact_edge_count",
        "blame_edge_count",
        "diagnostic_edge_count",
        "regression_blame_chain_count_graph",
        "dependency_blame_graph_node_count",
        "dependency_blame_graph_edge_count",
        "blame_path_count",
        "fine_grained_blame_path_count",
        "causal_dependency_edge_count",
        "causal_dependency_chain_count_graph",
        "causal_chain_count",
        "strong_causal_chain_count",
        "validated_causal_chain_count",
        "refuted_causal_chain_count",
        "inconclusive_causal_chain_count",
        "causal_requires_same_batch",
        "causal_blame_node_count",
        "causal_blame_edge_count",
        "causal_profile_skill_scheduler_path",
        "causal_profile_skill_decision_action_outcome_path",
        "fine_grained_causal_chain_count",
        "causal_dependency_chain_count_report",
        "regression_blame_chain_count_report",
        "bad_profile_bad_skill_scheduler_regression_path",
        "bad_profile_to_regression_path",
        "skill_to_regression_path",
        "scheduler_to_regression_path",
        "causal_root_candidate_count",
        "systemic_action_count",
        "systemic_override_count",
        "meta_template",
        "system_evolution_state_exists",
        "system_evolution_history_count",
        "system_evolution_override_history_count",
        "system_evolution_template_history_count",
        "system_evolution_selected_template",
        "system_evolution_failure_total_count",
        "system_evolution_template_scores",
        "systemic_override_application_exists",
        "systemic_override_applied_count",
        "systemic_override_applied_fields",
        "systemic_override_ignored",
        "systemic_override_profile_s_original",
        "systemic_override_profile_s_final",
        "systemic_override_disablelist",
        "meta_template_applied",
        "meta_execution_graph_nodes",
        "meta_execution_graph_edges",
        "meta_execution_graph_step_nodes",
        "meta_execution_graph_step_edges",
        "meta_execution_graph_declared_step_count",
        "meta_execution_graph_observed_declared_step_count",
        "meta_execution_graph_observed_declared_gap",
        "meta_template_graph_applied_to_runtime",
        "meta_template_runtime_policy_mode",
        "meta_template_runtime_skipped_step_count",
        "meta_template_runtime_skipped_steps",
        "meta_template_runtime_skip_reasons",
        "observed_execution_graph_exists",
        "observed_execution_graph_step_count",
        "observed_execution_graph_declared_coverage",
        "observed_execution_graph_control_edges",
        "observed_execution_graph_artifact_dependency_edges",
        "graph_scheduler_plan_exists",
        "graph_scheduler_declared_step_count",
        "graph_scheduler_observed_declared_step_count",
        "graph_scheduler_completed_step_count",
        "graph_scheduler_skipped_step_count",
        "graph_scheduler_failed_step_count",
        "graph_scheduler_blocked_step_count",
        "graph_scheduler_waiting_step_count",
        "graph_scheduler_runnable_step_count",
        "graph_scheduler_next_runnable_count",
        "graph_scheduler_artifact_dependency_edge_count",
        "graph_scheduler_failed_step_replay_candidate_count",
        "graph_scheduler_missing_required_artifact_count",
        "skill_repository_updated",
        "skill_repository_mutated",
        "skill_transition",
        "skill_update_blocked_reason",
        "skill_update_protected_status",
        "skill_update_validation_gate_passed",
        "skill_update_evolution_ledger_exists",
        "skill_evolution_application_exists",
        "skill_evolution_application_applied_count",
        "skill_evolution_application_transition_count",
        "skill_evolution_application_dry_run",
        "skill_evolution_application_skipped_count",
        "skill_evolution_application_transitions",
        "global_memory_recorded",
        "memory_isolation_source",
        "memory_mode",
        "memory_isolation_enabled",
        "memory_isolation_dry_run",
        "memory_path_scope",
        "global_memory_path",
        "memory_explicit_path_count",
        "memory_rewritten_path_count",
        "memory_isolated_for_dry_run",
        "dry_run_memory_shared_path_risk",
        "production_shared_memory",
        "memory_item_count",
        "memory_namespaces",
        "durable_checkpoint_count",
        "durable_step_count",
        "durable_event_count",
        "durable_handoff_count",
        "durable_task_count",
        "durable_leased_task_count",
        "durable_worker_count",
        "durable_step_status_counts",
        "durable_task_status_counts",
        "typed_event_count",
        "typed_event_distribution",
        "typed_handoff_event_count",
        "typed_step_start_event_count",
        "typed_step_end_event_count",
        "scheduler_task_count",
        "scheduler_history_count",
        "runtime_service_event_count",
        "root_runtime_service_event_count",
        "runtime_service_events",
        "http_auth_failure_events",
        "http_memory_query_events",
        "http_memory_summary_events",
        "http_memory_retrieve_events",
        "http_memory_append_events",
        "http_hitl_approve_events",
        "http_resume_events",
        "http_task_list_events",
        "http_task_submit_events",
        "http_worker_once_events",
        "http_worker_lease_events",
        "http_worker_complete_events",
        "http_worker_heartbeat_events",
        "http_worker_register_events",
        "http_worker_registry_heartbeat_events",
        "http_adapter_invoke_events",
        "runtime_api_adapter_result_exists",
        "runtime_api_adapter_framework",
        "runtime_api_adapter_invocation_type",
        "runtime_api_adapter_status",
        "runtime_api_adapter_submitted_task_count",
        "runtime_api_adapter_resume_status",
        "runtime_api_adapter_event_count",
        "native_langgraph_result_exists",
        "native_langgraph_status",
        "native_langgraph_backend_validation_source",
        "native_langgraph_backend_invocation_class",
        "native_langgraph_graph_mode",
        "native_langgraph_available",
        "native_langgraph_invoked",
        "native_langgraph_resumed",
        "native_langgraph_state_observed",
        "runtime_parity_exists",
        "runtime_parity_status",
        "runtime_parity_score",
        "runtime_advantage_score",
        "runtime_parity_failed",
        "runtime_advantage_failed",
        "registered_worker_count",
        "active_registered_worker_count",
        "worker_lease_events",
        "worker_heartbeat_events",
        "worker_lease_expired_events",
        "human_gate_requested",
        "human_gate_approved",
        "openai_trace_exists",
        "langgraph_state_exists",
        "autogen_trace_exists",
        "crewai_trace_exists",
        "framework_adapter_bundle_exists",
        "framework_adapter_status",
        "framework_adapter_score",
        "framework_contract_status",
        "framework_contract_score",
        "framework_contract_check_count",
        "framework_contract_passed_count",
        "framework_contract_failed",
        "framework_contract_openai_passed",
        "framework_contract_langgraph_passed",
        "framework_contract_autogen_passed",
        "framework_contract_crewai_passed",
        "framework_adapter_openai_concepts",
        "framework_adapter_langgraph_concepts",
        "framework_adapter_autogen_concepts",
        "framework_adapter_crewai_concepts",
        "trace_alignment_exists",
        "trace_alignment_status",
        "trace_alignment_score",
        "openai_alignment_passed",
        "langgraph_alignment_passed",
        "autogen_alignment_passed",
        "crewai_alignment_passed",
        "typed_handoff_alignment_passed",
        "durable_execution_alignment_passed",
        "typed_event_bus_alignment_passed",
        "resume_graph_alignment_passed",
        "framework_adapter_alignment_passed",
        "resume_plan_exists",
        "resume_audit_exists",
        "resume_result_exists",
        "resume_planned_step_count",
        "resume_graph_node_count",
        "resume_graph_edge_count",
        "resume_graph_dependency_edge_count",
        "resume_graph_execution_edge_count",
        "resume_graph_replayable_step_count",
        "resume_graph_non_replayable_step_count",
        "resume_graph_queued_task_replay_count",
        "resume_graph_skipped_unselected_step_count",
        "resume_graph_replay_selection_edge_count",
        "resume_audit_status",
        "resume_audit_planned_step_count",
        "resume_audit_replayed_count",
        "resume_audit_safe_to_replay_count",
        "resume_audit_unsafe_to_replay_count",
        "resume_audit_skipped_count",
        "resume_audit_failed_count",
        "resume_audit_dependency_node_count",
        "resume_audit_dependency_edge_count",
        "resume_audit_resume_dependency_edge_count",
        "resume_audit_artifact_dependency_edge_count",
        "resume_audit_artifact_generation_edge_count",
        "resume_audit_replay_selection_edge_count",
        "resume_audit_skipped_unselected_step_count",
        "resume_audit_input_hash_changed_count",
        "resume_audit_input_hash_missing_count",
        "resume_audit_input_hash_unverified_count",
        "resume_audit_input_hash_ok_count",
        "resume_audit_input_hash_pending_replay_output_count",
        "resume_audit_side_effect_safe_count",
        "resume_audit_side_effect_unsafe_count",
        "resume_audit_scx_load_replay_count",
        "resume_audit_service_restart_replay_count",
        "resume_audit_benchmark_replay_count",
        "resume_audit_root_control_replay_count",
        "durable_resume_step_count",
        "durable_resume_task_count",
        "durable_resume_source",
        "resume_status",
        "hitl_service_approval_recorded",
        "hitl_service_resume_returncode",
        "worker_result_recorded",
        "worker_status",
        "worker_pool_result_recorded",
        "worker_pool_status",
        "worker_pool_worker_count",
        "worker_pool_attempt_count",
        "worker_pool_completed_count",
        "worker_pool_failed_count",
        "worker_pool_no_task_count",
        "worker_pool_parallelism_observed",
        "worker_complete_recorded",
        "worker_complete_ok",
        "tasks_view_recorded",
        "tasks_view_task_count",
        "tasks_view_durable_task_count",
        "task_submission_recorded",
        "task_submission_status",
        "task_submission_role",
        "same_batch_validation_exists",
        "same_batch_validation_passed",
        "same_batch_validation_verdict",
        "same_batch_improvement_pct",
        "same_batch_has_default_control",
        "same_batch_has_fixed_scx_treatment",
        "same_batch_has_kernadapt_online",
        "same_batch_has_lockagent_online",
        "same_batch_has_oracle_best_eval_only",
        "counterfactual_plan_exists",
        "counterfactual_required",
        "counterfactual_task_count",
        "counterfactual_executable_task_count",
        "counterfactual_submitted_task_count",
        "counterfactual_skill_update_planned",
        "counterfactual_skill_update_task_count",
        "counterfactual_skill_update_repository",
        "counterfactual_execution_exists",
        "counterfactual_execution_status",
        "counterfactual_execution_dry_run",
        "run_dir",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            flat = {}
            for field in fields:
                value = row.get(field)
                if isinstance(value, (list, dict)):
                    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
                flat[field] = value
            writer.writerow(flat)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out-json", type=Path, required=True)
    ap.add_argument("--out-csv", type=Path)
    ap.add_argument(
        "--evaluation-mode",
        choices=sorted(EVALUATION_MODE_ARTIFACTS),
        default="full",
        help="Artifact completeness profile to apply to each run.",
    )
    args = ap.parse_args(argv)

    requirements = evaluation_requirements(args.evaluation_mode)
    rows = [summarize_run(path, evaluation_mode=args.evaluation_mode) for path in run_dirs(args.root)]
    batches = [summarize_batch(path) for path in batch_dirs(args.root)]
    result = {
        "schema_version": "lockagent.process_eval.v0.1",
        "evaluation_mode": args.evaluation_mode,
        "evaluation_requirements": requirements,
        "aggregate": aggregate(rows),
        "batch_aggregate": aggregate_batches(batches),
        "runs": rows,
        "batches": batches,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_csv:
        write_csv(args.out_csv, rows)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Compatibility entry for the KernAdapt online control loop.

The filename is kept temporarily for old scripts and tests.  Active defaults
belong to ``kernadapt/`` rather than the historical ``LockAgent/`` workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from agent_runtime import (
    CAP_ATTRIBUTE_FAILURE,
    CAP_BUILD_PROFILE,
    CAP_CALL_LLM,
    CAP_CURATE_SKILL,
    CAP_EMIT_PROVENANCE,
    CAP_EVALUATE_POLICY,
    CAP_LOAD_SCX,
    CAP_READ_SYSTEM_STATE,
    CAP_RESTART_SERVICE,
    CAP_ROOT_CONTROL,
    CAP_RUN_BENCHMARK,
    CAP_VALIDATE_ARTIFACT,
    CAP_WRITE_ARTIFACT,
    ROLE_CRITIC,
    ROLE_CURATOR,
    ROLE_EXECUTOR,
    ROLE_GUARD,
    ROLE_POLICY,
    ROLE_PROFILER,
    AgentRun,
    ExecutionGraph,
    PolicyEngine,
    RuntimeReplayExecutor,
)
from runtime_parity import evaluate_run as evaluate_runtime_parity
from runtime_guard import build_policy_context, postflight as runtime_guard_postflight
from sysbench_metrics import parse_sysbench_log


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = Path(__file__).resolve().parent
KERNADAPT_ROOT = REPO_ROOT
DEFAULT_OUT = KERNADAPT_ROOT / "results" / "mariadb_online"
SKILL_CARDS = AGENT_DIR.parent / "skills" / "scheduler_skill_cards.json"
DEFAULT_GLOBAL_MEMORY = KERNADAPT_ROOT / "results" / "memory" / "global_memory.jsonl"
DEFAULT_GLOBAL_MEMORY_SUMMARY = KERNADAPT_ROOT / "results" / "memory" / "global_memory_summary.json"
DEFAULT_SYSTEMIC_OVERRIDES = KERNADAPT_ROOT / "results" / "memory" / "systemic_overrides.json"
DEFAULT_SYSTEM_EVOLUTION_STATE = KERNADAPT_ROOT / "results" / "memory" / "system_evolution_state.json"
CLI_DESCRIPTION = "Run the KernAdapt online control loop for a workload phase."


def run(cmd: list[str], *, dry_run: bool = False) -> int:
    print("+", " ".join(str(c) for c in cmd))
    if not dry_run:
        return subprocess.run(cmd, cwd=REPO_ROOT).returncode
    return 0


def privileged_cmd(*args: str) -> list[str]:
    if os.geteuid() == 0:
        return list(args)
    return ["sudo", "-n", *args]


def run_logged(cmd: list[str], log: Path, *, dry_run: bool = False) -> int:
    print("+", " ".join(str(c) for c in cmd), ">", log)
    if dry_run:
        return 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as f:
        return subprocess.run(cmd, cwd=REPO_ROOT, stdout=f, stderr=subprocess.STDOUT, text=True).returncode


class StageError(RuntimeError):
    def __init__(self, stage: str, returncode: int) -> None:
        super().__init__(f"{stage} failed with return code {returncode}")
        self.stage = stage
        self.returncode = returncode


def runtime_template_policy(template: str | None) -> dict[str, Any]:
    """Map a meta template to concrete runtime stage gates.

    The safety path is intentionally conservative: RuntimeGuard and executor
    postflight artifacts are never skipped. Templates only change whether
    Critic preflight is mandatory and whether training/validation stages run.
    """

    selected = str(template or "full_default")
    policies: dict[str, dict[str, Any]] = {
        "full_default": {
            "template": None,
            "requires_critic_preflight": True,
            "run_skill_curator": True,
            "run_causal_attribution": True,
            "run_counterfactual_planner": True,
            "run_skill_evolution_application": True,
            "mode": "full_default",
        },
        "fast_path_single_agent": {
            "template": "fast_path_single_agent",
            "requires_critic_preflight": False,
            "run_skill_curator": False,
            "run_causal_attribution": False,
            "run_counterfactual_planner": False,
            "run_skill_evolution_application": False,
            "mode": "fast_runtime_control",
        },
        "guarded_policy_agent": {
            "template": "guarded_policy_agent",
            "requires_critic_preflight": False,
            "run_skill_curator": False,
            "run_causal_attribution": False,
            "run_counterfactual_planner": False,
            "run_skill_evolution_application": False,
            "mode": "guarded_runtime_control",
        },
        "critic_required_agent": {
            "template": "critic_required_agent",
            "requires_critic_preflight": True,
            "run_skill_curator": False,
            "run_causal_attribution": True,
            "run_counterfactual_planner": False,
            "run_skill_evolution_application": False,
            "mode": "critic_required_runtime_control",
        },
        "curator_training_mode": {
            "template": "curator_training_mode",
            "requires_critic_preflight": True,
            "run_skill_curator": True,
            "run_causal_attribution": True,
            "run_counterfactual_planner": True,
            "run_skill_evolution_application": True,
            "mode": "curator_training",
        },
    }
    return policies.get(selected, policies["guarded_policy_agent"])


def template_skip_reason(template_policy: dict[str, Any], stage_name: str) -> str:
    return f"meta_template:{template_policy.get('template')}:disabled_stage:{stage_name}"


def record_skipped_stage(
    *,
    runtime_run: AgentRun,
    stage_name: str,
    role: str,
    reason: str,
    output_artifacts: list[Path],
    artifact_payloads: dict[Path, dict[str, Any]],
    manifest: dict[str, object],
    input_artifacts: list[str | Path] | None = None,
    capabilities: list[str] | None = None,
) -> None:
    def execute(_: Any) -> dict[str, Any]:
        for artifact, payload in artifact_payloads.items():
            runtime_run.store.write_json(
                artifact,
                payload,
                artifact_type=stage_name,
                producer="run_lockagent_online",
            )
        return {
            "status": "skipped",
            "returncode": 0,
            "error": reason,
            "output_artifacts": output_artifacts,
        }

    runtime_run.execute_step(
        name=stage_name,
        role=role,
        executor=execute,
        required_capabilities=capabilities or [CAP_WRITE_ARTIFACT],
        input_artifacts=input_artifacts,
        output_artifacts=output_artifacts,
        metadata={
            "skip_reason": reason,
            "runtime_template_policy": True,
        },
    )
    stages = manifest.setdefault("stages", [])
    if isinstance(stages, list):
        stages.append({"stage": stage_name, "returncode": 0, "status": "skipped", "reason": reason})


def run_stage(
    stage: str,
    cmd: list[str],
    manifest: dict[str, object],
    *,
    dry_run: bool = False,
    raise_on_error: bool = True,
    runtime_run: AgentRun | None = None,
    role: str = ROLE_EXECUTOR,
    capabilities: list[str] | None = None,
    input_artifacts: list[str | Path] | None = None,
    output_artifacts: list[str | Path] | None = None,
) -> None:
    rc_holder: dict[str, int] = {"returncode": 0}

    def execute(_: Any) -> dict[str, Any]:
        rc = run(cmd, dry_run=dry_run)
        rc_holder["returncode"] = rc
        return {
            "status": "ok" if rc == 0 else "failed",
            "returncode": rc,
            "error": None if rc == 0 else f"{stage} failed with return code {rc}",
            "output_artifacts": list(output_artifacts or []),
        }

    if runtime_run:
        runtime_run.execute_step(
            name=stage,
            role=role,
            required_capabilities=capabilities or [CAP_WRITE_ARTIFACT],
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            metadata={"command": cmd, "dry_run": dry_run, "runner_entrypoint": "run_stage"},
            executor=execute,
        )
        rc = rc_holder["returncode"]
    else:
        rc = run(cmd, dry_run=dry_run)
    stages = manifest.setdefault("stages", [])
    if isinstance(stages, list):
        stages.append({"stage": stage, "returncode": rc})
    if rc != 0 and raise_on_error:
        raise StageError(stage, rc)


def shell_quote_env(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def safe_load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = load_json(path)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_optional_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    return safe_load_json(path)


def materialize_failure_fallback_artifacts(
    *,
    run_id: str,
    workload: str,
    threads: int,
    db_name: str,
    end_time: str,
    manifest: dict[str, object],
    status_path: Path,
    decision_path: Path,
    profile_path: Path,
    state_ledger_path: Path,
    fallback_path: Path,
) -> list[dict[str, Any]]:
    """Write synthetic artifacts only to keep postfailure analysis auditable."""

    fallback_records: list[dict[str, Any]] = []
    if not status_path.exists():
        fallback_records.append(
            {
                "artifact": str(status_path),
                "artifact_type": "execution_status",
                "reason": "execution_status_missing_on_failure_path",
                "safe_for_performance_claims": False,
            }
        )
        write_json(
            status_path,
            {
                "schema_version": "lockagent.synthetic_failure_fallback.v0.1",
                "status": "not_run",
                "returncode": manifest.get("returncode", 1),
                "final_sched_ext_state": "unknown",
                "synthetic_failure_fallback": True,
                "safe_for_performance_claims": False,
                "fallback_reason": "execution_status_missing_on_failure_path",
                "end_time": end_time,
            },
        )
    if not decision_path.exists():
        fallback_records.append(
            {
                "artifact": str(decision_path),
                "artifact_type": "decision",
                "reason": "decision_missing_on_failure_path",
                "safe_for_policy_claims": False,
            }
        )
        write_json(
            decision_path,
            {
                "schema_version": "lockagent.synthetic_failure_fallback.v0.1",
                "scheduler": "default",
                "confidence": 0.0,
                "rationale": "No LLM decision was produced before failure.",
                "evidence_used": ["failure_path"],
                "expected_benefit": {"throughput": "unknown", "latency": "unknown", "fairness": "unknown"},
                "risks": ["failure before policy decision"],
                "validation_plan": ["inspect online_manifest"],
                "rollback_conditions": ["failure before execution"],
                "data_provenance": {
                    "synthetic_failure_fallback": True,
                    "safe_for_policy_claims": False,
                    "reason": "decision_missing_on_failure_path",
                },
            },
        )
    if not profile_path.exists():
        fallback_records.append(
            {
                "artifact": str(profile_path),
                "artifact_type": "profile",
                "reason": "profile_missing_on_failure_path",
                "safe_for_policy_claims": False,
            }
        )
        write_json(
            profile_path,
            {
                "schema_version": "lockagent.synthetic_failure_fallback.v0.1",
                "profile_id": f"missing_profile_{run_id}",
                "workload": {"name": workload, "threads": threads},
                "signals": {},
                "current_scheduler": "default",
                "candidate_schedulers": ["default"],
                "objective": {"primary": "unknown"},
                "data_provenance": {
                    "contains_oracle_best": False,
                    "synthetic_failure_fallback": True,
                    "safe_for_policy_claims": False,
                    "note": "profile missing on failure path",
                },
            },
        )
    if not state_ledger_path.exists():
        fallback_records.append(
            {
                "artifact": str(state_ledger_path),
                "artifact_type": "state_ledger",
                "reason": "state_ledger_missing_on_failure_path",
                "safe_for_environment_claims": False,
            }
        )
        write_json(
            state_ledger_path,
            {
                "schema_version": "lockagent.synthetic_failure_fallback.v0.1",
                "run_id": run_id,
                "timestamp": end_time,
                "synthetic_failure_fallback": True,
                "safe_for_environment_claims": False,
                "database": {"engine": "mariadb", "db_name": db_name, "collection_status": "unknown"},
                "benchmark": {"workload": workload, "threads": threads},
            },
        )
    write_json(
        fallback_path,
        {
            "schema_version": "lockagent.fallback_artifacts.v0.1",
            "run_id": run_id,
            "created": bool(fallback_records),
            "records": fallback_records,
            "safe_for_performance_claims": not fallback_records,
            "note": "Synthetic fallback artifacts preserve postfailure analysis paths; they are not evidence for scheduler performance or policy quality.",
        },
    )
    return fallback_records


def option_was_provided(argv: list[str], option: str) -> bool:
    return any(item == option or item.startswith(f"{option}=") for item in argv)


def apply_dry_run_memory_isolation(args: argparse.Namespace, argv: list[str], outdir: Path) -> dict[str, Any]:
    """Keep dry-run memory writes local unless the caller opts into shared paths."""

    tracked_options = {
        "global_memory": "--global-memory",
        "global_memory_summary": "--global-memory-summary",
        "systemic_overrides": "--systemic-overrides",
        "system_evolution_state": "--system-evolution-state",
    }
    explicit = {
        field: option_was_provided(argv, option)
        for field, option in tracked_options.items()
    }
    memory_dir = outdir / "memory"
    rewritten: dict[str, dict[str, str]] = {}

    if args.dry_run:
        local_defaults = {
            "global_memory": memory_dir / "global_memory.jsonl",
            "global_memory_summary": memory_dir / "global_memory_summary.json",
            "systemic_overrides": memory_dir / "systemic_overrides.json",
            "system_evolution_state": memory_dir / "system_evolution_state.json",
        }
        for field, local_path in local_defaults.items():
            if explicit[field]:
                continue
            old_path = getattr(args, field)
            setattr(args, field, local_path)
            rewritten[field] = {"from": str(old_path), "to": str(local_path)}

    return {
        "schema_version": "lockagent.memory_isolation.v0.1",
        "enabled": bool(args.dry_run and rewritten),
        "mode": "run_local_defaults" if rewritten else ("explicit_paths" if args.dry_run else "shared_paths"),
        "dry_run": bool(args.dry_run),
        "memory_dir": str(memory_dir),
        "explicit": explicit,
        "rewritten": rewritten,
        "paths": {
            "global_memory": str(args.global_memory),
            "global_memory_summary": str(args.global_memory_summary),
            "systemic_overrides": str(args.systemic_overrides),
            "system_evolution_state": str(args.system_evolution_state),
        },
    }


def finalize_runtime_artifacts(runtime_run: AgentRun, manifest: dict[str, object]) -> None:
    """Materialize resume/parity artifacts after the run checkpoint is final."""

    checkpoint = runtime_run.checkpoint_path
    resume_status = "not_run"
    parity_status = "not_run"
    try:
        resume_result = RuntimeReplayExecutor(checkpoint, dry_run=True).replay(from_step=None, only_failed=False, drain_queue=True)
        resume_status = str(resume_result.get("status"))
        runtime_run.store.register(runtime_run.artifact_root / "resume_plan.json", artifact_type="resume_plan", producer="RuntimeReplayExecutor")
        runtime_run.store.register(runtime_run.artifact_root / "resume_result.json", artifact_type="resume_result", producer="RuntimeReplayExecutor")
        runtime_run.store.register(runtime_run.artifact_root / "resume_audit.json", artifact_type="resume_audit", producer="RuntimeReplayExecutor")
    except Exception as exc:
        resume_status = "failed"
        write_json(
            runtime_run.artifact_root / "resume_plan.json",
            {
                "schema_version": "lockagent.runtime.v0.1",
                "run_id": runtime_run.run_id,
                "status": "failed",
                "checkpoint": str(checkpoint),
                "phase": "finalize_runtime_artifacts",
                "error": str(exc),
                "planned_steps": [],
                "execution_order": [],
                "dependency_order": [],
                "artifact_records": [],
                "resume_graph": {"nodes": [], "edges": [], "dependency_edges": []},
            },
        )
        write_json(
            runtime_run.artifact_root / "resume_result.json",
            {
                "schema_version": "lockagent.runtime.v0.1",
                "run_id": runtime_run.run_id,
                "status": "failed",
                "checkpoint": str(checkpoint),
                "phase": "finalize_runtime_artifacts",
                "error": str(exc),
                "resume_plan": str(runtime_run.artifact_root / "resume_plan.json"),
            },
        )
        write_json(
            runtime_run.artifact_root / "resume_audit.json",
            {
                "schema_version": "lockagent.runtime.v0.1",
                "run_id": runtime_run.run_id,
                "status": "failed",
                "error": str(exc),
                "phase": "finalize_runtime_artifacts",
                "resume_plan": str(runtime_run.artifact_root / "resume_plan.json"),
            },
        )
        runtime_run.store.register(runtime_run.artifact_root / "resume_plan.json", artifact_type="resume_plan", producer="RuntimeReplayExecutor")
        runtime_run.store.register(runtime_run.artifact_root / "resume_result.json", artifact_type="resume_result", producer="RuntimeReplayExecutor")
        runtime_run.store.register(runtime_run.artifact_root / "resume_audit.json", artifact_type="resume_audit", producer="RuntimeReplayExecutor")

    try:
        parity = evaluate_runtime_parity(runtime_run.artifact_root)
        write_json(runtime_run.artifact_root / "runtime_parity.json", parity)
        parity_status = str(parity.get("summary", {}).get("status"))
        runtime_run.store.register(runtime_run.artifact_root / "runtime_parity.json", artifact_type="runtime_parity", producer="runtime_parity")
    except Exception as exc:
        parity_status = "failed"
        write_json(
            runtime_run.artifact_root / "runtime_parity.json",
            {
                "schema_version": "lockagent.runtime_parity.v0.1",
                "run_dir": str(runtime_run.artifact_root),
                "summary": {"status": "failed", "parity_score": 0, "failed_parity": ["runtime_parity_exception"]},
                "error": str(exc),
            },
        )
        runtime_run.store.register(runtime_run.artifact_root / "runtime_parity.json", artifact_type="runtime_parity", producer="runtime_parity")

    manifest["resume_plan"] = str(runtime_run.artifact_root / "resume_plan.json")
    manifest["resume_audit"] = str(runtime_run.artifact_root / "resume_audit.json")
    manifest["resume_result"] = str(runtime_run.artifact_root / "resume_result.json")
    manifest["runtime_parity"] = str(runtime_run.artifact_root / "runtime_parity.json")
    manifest["runtime_finalization"] = {
        "resume_status": resume_status,
        "runtime_parity_status": parity_status,
    }


def _run_sort_key(path: Path) -> str:
    return path.name


def candidate_run_dirs(root: Path) -> list[Path]:
    if not root.exists():
        return []
    if (root / "online_manifest.json").exists() or (root / "failure_attribution.json").exists():
        return [root]
    return sorted((path for path in root.glob("run_*") if path.is_dir()), key=_run_sort_key)


def infer_last_failed_scheduler(root: Path) -> str | None:
    for run_dir in reversed(candidate_run_dirs(root)):
        failure = safe_load_json(run_dir / "failure_attribution.json")
        categories = failure.get("categories", [])
        if not any(category in categories for category in ("scheduler_startup_failure", "scheduler_regression")):
            continue
        decision = safe_load_json(run_dir / "decision.json")
        scheduler = decision.get("scheduler")
        if scheduler and scheduler != "default":
            return str(scheduler)
    return None


def normalize_systemic_overrides(overrides: dict[str, Any], *, history_root: Path | None = None) -> dict[str, Any]:
    if not overrides:
        return {}
    normalized = dict(overrides)
    disablelist = []
    for item in normalized.get("scheduler_disablelist", []) or []:
        if item == "last_failed_scheduler":
            inferred = infer_last_failed_scheduler(history_root or DEFAULT_OUT)
            if inferred:
                disablelist.append(inferred)
        elif item:
            disablelist.append(str(item))
    normalized["scheduler_disablelist"] = sorted(set(disablelist))
    return normalized


def build_systemic_override_application(
    *,
    raw_overrides: dict[str, Any],
    normalized_overrides: dict[str, Any],
    original_profile_s: int,
    final_profile_s: int,
    require_human_approval: bool,
    graph: ExecutionGraph,
    ignored: bool,
) -> dict[str, Any]:
    applied = []
    if normalized_overrides.get("require_human_approval"):
        applied.append({"field": "require_human_approval", "effect": "enabled_human_gate"})
    if int(normalized_overrides.get("profile_s_multiplier") or 1) > 1:
        applied.append(
            {
                "field": "profile_s_multiplier",
                "effect": "extended_profile_window",
                "from": original_profile_s,
                "to": final_profile_s,
            }
        )
    if normalized_overrides.get("require_environment_compatibility"):
        applied.append({"field": "require_environment_compatibility", "effect": "enabled_guard_environment_gate"})
    if normalized_overrides.get("require_strict_critic"):
        applied.append({"field": "require_strict_critic", "effect": "requires_critic_allow_before_executor"})
    if normalized_overrides.get("scheduler_disablelist"):
        applied.append(
            {
                "field": "scheduler_disablelist",
                "effect": "disabled_schedulers_for_current_environment",
                "schedulers": normalized_overrides.get("scheduler_disablelist", []),
            }
        )
    template = normalized_overrides.get("meta_template")
    if template:
        applied.append(
            {
                "field": "meta_template",
                "effect": "selected_execution_graph",
                "template": template,
                "nodes": graph.nodes,
                "edges": [{"from": source, "to": target} for source, target in graph.edges],
            }
        )
    return {
        "schema_version": "lockagent.systemic_override_application.v0.1",
        "ignored": ignored,
        "raw_overrides": raw_overrides,
        "normalized_overrides": normalized_overrides,
        "applied": applied,
        "applied_count": len(applied),
        "profile_s": {"original": original_profile_s, "final": final_profile_s},
        "require_human_approval": require_human_approval,
        "meta_template": template,
        "execution_graph": graph.to_dict(),
    }


def apply_systemic_overrides(args: argparse.Namespace, overrides: dict[str, Any], *, history_root: Path | None = None) -> dict[str, Any]:
    raw = dict(overrides or {})
    normalized = normalize_systemic_overrides(raw, history_root=history_root)
    original_profile_s = int(args.profile_s)
    if not normalized:
        args.systemic_overrides_applied = {}
        return build_systemic_override_application(
            raw_overrides=raw,
            normalized_overrides={},
            original_profile_s=original_profile_s,
            final_profile_s=int(args.profile_s),
            require_human_approval=bool(args.require_human_approval),
            graph=graph_from_meta_template(None),
            ignored=not bool(raw),
        )
    if normalized.get("require_human_approval"):
        args.require_human_approval = True
    multiplier = int(normalized.get("profile_s_multiplier") or 1)
    if multiplier > 1:
        args.profile_s *= multiplier
    args.systemic_overrides_applied = normalized
    return build_systemic_override_application(
        raw_overrides=raw,
        normalized_overrides=normalized,
        original_profile_s=original_profile_s,
        final_profile_s=int(args.profile_s),
        require_human_approval=bool(args.require_human_approval),
        graph=graph_from_meta_template(normalized.get("meta_template")),
        ignored=False,
    )


def graph_from_meta_template(template: str | None) -> ExecutionGraph:
    base_without_training = [
        ("state_ledger", ROLE_PROFILER),
        ("restart_mariadb_for_profile", ROLE_EXECUTOR),
        ("default_profile_window", ROLE_EXECUTOR),
        ("profile_builder", ROLE_PROFILER),
        ("llm_policy_agent", ROLE_POLICY),
        ("runtime_policy_preflight", ROLE_GUARD),
        ("restart_mariadb_for_execution", ROLE_EXECUTOR),
        ("scx_executor", ROLE_EXECUTOR),
        ("critic_postflight", ROLE_CRITIC),
        ("build_provenance", ROLE_CRITIC),
        ("self_evolution_planner", ROLE_CURATOR),
        ("global_memory_append", ROLE_CURATOR),
        ("build_final_provenance", ROLE_CRITIC),
    ]
    templates = {
        "fast_path_single_agent": [
            ("state_ledger", ROLE_PROFILER),
            ("restart_mariadb_for_profile", ROLE_EXECUTOR),
            ("default_profile_window", ROLE_EXECUTOR),
            ("profile_builder", ROLE_PROFILER),
            ("llm_policy_agent", ROLE_POLICY),
            ("runtime_policy_preflight", ROLE_GUARD),
            ("restart_mariadb_for_execution", ROLE_EXECUTOR),
            ("scx_executor", ROLE_EXECUTOR),
            ("critic_postflight", ROLE_CRITIC),
            ("build_provenance", ROLE_CRITIC),
            ("self_evolution_planner", ROLE_CURATOR),
            ("global_memory_append", ROLE_CURATOR),
            ("build_final_provenance", ROLE_CRITIC),
        ],
        "guarded_policy_agent": base_without_training,
        "critic_required_agent": [
            ("state_ledger", ROLE_PROFILER),
            ("restart_mariadb_for_profile", ROLE_EXECUTOR),
            ("default_profile_window", ROLE_EXECUTOR),
            ("profile_builder", ROLE_PROFILER),
            ("llm_policy_agent", ROLE_POLICY),
            ("critic_preflight", ROLE_CRITIC),
            ("runtime_policy_preflight", ROLE_GUARD),
            ("restart_mariadb_for_execution", ROLE_EXECUTOR),
            ("scx_executor", ROLE_EXECUTOR),
            ("critic_postflight", ROLE_CRITIC),
            ("build_provenance", ROLE_CRITIC),
            ("skill_curator", ROLE_CURATOR),
            ("self_evolution_planner", ROLE_CURATOR),
            ("global_memory_append", ROLE_CURATOR),
            ("build_final_provenance", ROLE_CRITIC),
            ("causal_attribution", ROLE_CRITIC),
        ],
        "curator_training_mode": [
            ("state_ledger", ROLE_PROFILER),
            ("restart_mariadb_for_profile", ROLE_EXECUTOR),
            ("default_profile_window", ROLE_EXECUTOR),
            ("profile_builder", ROLE_PROFILER),
            ("llm_policy_agent", ROLE_POLICY),
            ("critic_preflight", ROLE_CRITIC),
            ("runtime_policy_preflight", ROLE_GUARD),
            ("restart_mariadb_for_execution", ROLE_EXECUTOR),
            ("scx_executor", ROLE_EXECUTOR),
            ("critic_postflight", ROLE_CRITIC),
            ("build_provenance", ROLE_CRITIC),
            ("skill_curator", ROLE_CURATOR),
            ("self_evolution_planner", ROLE_CURATOR),
            ("global_memory_append", ROLE_CURATOR),
            ("build_final_provenance", ROLE_CRITIC),
            ("causal_attribution", ROLE_CRITIC),
            ("counterfactual_planner", ROLE_CRITIC),
            ("counterfactual_execute_validation", ROLE_CURATOR),
            ("skill_evolution_application", ROLE_CURATOR),
        ],
    }
    selected = templates.get(str(template or ""))
    if selected is None:
        return ExecutionGraph.default()
    return ExecutionGraph.from_step_template(selected)


def attach_skill_cards(profile_path: Path, skill_cards_path: Path) -> None:
    profile_data = load_json(profile_path)
    cards = load_json(skill_cards_path)
    profile_data["scheduler_skill_cards"] = cards.get("skills", [])
    profile_data.setdefault("data_provenance", {})["scheduler_skill_cards"] = str(skill_cards_path)
    write_json(profile_path, profile_data)


def attach_experience_card_ids(profile_path: Path, experience_path: Path) -> None:
    profile_data = load_json(profile_path)
    experience_data = load_json(experience_path)
    cards = experience_data.get("cards", [])
    card_ids = [
        str(card.get("id"))
        for card in cards
        if isinstance(card, dict) and card.get("id")
    ]
    if card_ids:
        existing = profile_data.get("experience_card_ids", [])
        profile_data["experience_card_ids"] = sorted(
            set(str(item) for item in existing if item) | set(card_ids)
        )
        profile_data.setdefault("data_provenance", {})["experience_cards"] = str(experience_path)
    write_json(profile_path, profile_data)


def build_critic_preflight(profile_path: Path, decision_path: Path, ledger_path: Path) -> dict[str, Any]:
    profile = load_json(profile_path) if profile_path.exists() else None
    decision = load_json(decision_path) if decision_path.exists() else None
    ledger = load_json(ledger_path) if ledger_path.exists() else None
    policy_context = build_policy_context(
        phase="preflight",
        decision=decision,
        ledger=ledger,
        profile=profile,
        dry_run=False,
        intent="online",
        requires_executor=False,
    )
    policy_verdict = PolicyEngine().evaluate(policy_context)
    policy_checks = policy_verdict.get("checks", [])
    critic_blocking_rules = {
        "decision_exists",
        "decision_schema_valid",
        "confidence_in_range",
        "evidence_required",
        "evidence_resolves_to_profile_or_skill",
        "no_oracle_leakage",
        "validation_plan_required",
        "risks_required",
        "rollback_required",
        "ledger_required",
    }
    checks = [
        {
            "name": check.get("name"),
            "passed": check.get("passed"),
            "reason": check.get("reason"),
            "category": check.get("category"),
            "severity": check.get("severity"),
            "evidence_source": check.get("evidence_source"),
            "remediation": check.get("remediation"),
            "failure_category": check.get("failure_category"),
            "owner_role": check.get("owner_role"),
            "source": "PolicyEngine",
        }
        for check in policy_checks
        if isinstance(check, dict)
    ]
    critic_blocking = [
        check
        for check in checks
        if check.get("passed") is False
        and check.get("severity") == "block"
        and check.get("name") in critic_blocking_rules
    ]
    deferred = [
        check
        for check in checks
        if check.get("passed") is False
        and check.get("severity") == "block"
        and check.get("name") not in critic_blocking_rules
    ]
    return {
        "schema_version": "lockagent.critic_preflight.v0.1",
        "phase": "critic_preflight",
        "enforcement_model": "PolicyEngine",
        "verdict": "allow" if not critic_blocking else "block",
        "checks": checks,
        "blocking_reasons": [check.get("reason") for check in critic_blocking if check.get("reason")],
        "deferred_policy_reasons": [check.get("reason") for check in deferred if check.get("reason")],
        "critic_blocking_rules": sorted(critic_blocking_rules),
        "warnings": policy_verdict.get("warnings", []),
        "policy_context": policy_context,
        "policy_engine": policy_verdict,
    }


def decision_for_default() -> dict[str, Any]:
    return {
        "scheduler": "default",
        "confidence": 1.0,
        "rationale": "Live default profiling window before LLM decision.",
        "evidence_used": ["online_profile_window"],
        "expected_benefit": {
            "throughput": "baseline measurement",
            "latency": "baseline measurement",
            "fairness": "baseline measurement",
        },
        "risks": ["This is a profiling run, not the final scheduler decision."],
        "validation_plan": ["parse sysbench TPS and latency metrics"],
        "rollback_conditions": ["profiling command fails"],
    }


def mariadb_benchmark_cmd(args: argparse.Namespace, outdir: Path, duration_s: int) -> str:
    return (
        f"{sys.executable} {shell_quote_env(str(AGENT_DIR / 'run_mariadb_sysbench.py'))} "
        f"--threads {args.threads} --time {duration_s} "
        f"--db-name {shell_quote_env(args.db_name)} "
        f"--db-user {shell_quote_env(args.db_user)} "
        f"--db-pass {shell_quote_env(args.db_pass)} "
        f"--no-restart-mariadb "
        f"--out {shell_quote_env(str(outdir / 'benchmark'))}"
    )


def restart_mariadb(
    stage: str,
    outdir: Path,
    manifest: dict[str, object],
    dry_run: bool,
    *,
    runtime_run: AgentRun | None = None,
) -> None:
    log_path = outdir / "mariadb_restart.log"
    command = privileged_cmd("systemctl", "restart", "mariadb")
    rc_holder: dict[str, int] = {"returncode": 0}

    def execute(_: Any) -> dict[str, Any]:
        rc = run_logged(command, log_path, dry_run=dry_run)
        rc_holder["returncode"] = rc
        return {
            "status": "ok" if rc == 0 else "failed",
            "returncode": rc,
            "error": None if rc == 0 else f"{stage} failed with return code {rc}",
            "output_artifacts": [log_path],
        }

    if runtime_run:
        runtime_run.execute_step(
            name=stage,
            role=ROLE_EXECUTOR,
            required_capabilities=[CAP_RESTART_SERVICE, CAP_ROOT_CONTROL, CAP_WRITE_ARTIFACT],
            output_artifacts=[log_path],
            metadata={"command": command, "dry_run": dry_run, "runner_entrypoint": "restart_mariadb"},
            executor=execute,
        )
        rc = rc_holder["returncode"]
    else:
        rc = run_logged(command, log_path, dry_run=dry_run)
    stages = manifest.setdefault("stages", [])
    if isinstance(stages, list):
        stages.append({"stage": stage, "returncode": rc})
    if rc != 0:
        raise StageError(stage, rc)
    if not dry_run:
        time.sleep(5)


def sysbench_log_for(benchmark_base: Path) -> Path:
    return benchmark_base / "benchmark" / "sysbench.log"


def materialize_guard_blocked_executor_artifacts(
    *,
    runtime_run: AgentRun | None = None,
    exec_out: Path,
    status_path: Path,
    decision: dict[str, Any],
    benchmark_cmd: str,
    timeout_s: int,
    dry_run: bool,
    intent: str,
    state_ledger: Path,
    profile: Path,
    runtime_policy_preflight: Path,
    policy_report: dict[str, Any],
    policy_verdict: dict[str, Any],
    policy_context: dict[str, Any],
    status: str = "guard_blocked",
    final_sched_ext_state: str = "disabled",
    extra_status: dict[str, Any] | None = None,
) -> None:
    """Write ExecutorAgent intent artifacts when RuntimeGuard blocks execution."""

    reasons = list(dict.fromkeys(str(reason) for reason in policy_report.get("reasons", []) if reason))
    scheduler = decision.get("scheduler")
    write_json(
        exec_out / "manifest.json",
        {
            "decision": decision,
            "scheduler": scheduler,
            "benchmark_cmd": benchmark_cmd,
            "timeout_s": timeout_s,
            "dry_run": dry_run,
            "intent": intent,
            "state_ledger": str(state_ledger),
            "profile": str(profile),
            "runtime_policy_preflight": str(runtime_policy_preflight),
            "policy_enforcement": {
                "model": "PolicyEngine",
                "mode": "blocked_before_executor",
                "runtime_policy_preflight": str(runtime_policy_preflight),
            },
            "status": "blocked_before_executor",
            "blocked_by": reasons,
            "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        },
    )
    write_json(
        exec_out / "guard_preflight.json",
        {
            "phase": "preflight",
            "schema_version": "lockagent.runtime_guard.v0.1",
            "enforcement_model": "PolicyEngine",
            "verdict": "block",
            "reasons": reasons,
            "warnings": policy_report.get("warnings", []),
            "policy_context": policy_context,
            "policy_engine": policy_verdict,
            "scheduler": scheduler,
            "dry_run": dry_run,
            "intent": intent,
            "upstream_policy_preflight": str(runtime_policy_preflight),
        },
    )
    status_payload: dict[str, Any] = {
        "status": status,
        "returncode": 2,
        "executor_failure_class": "guard_blocked",
        "executor_error": ";".join(reasons) if reasons else "runtime_guard_blocked",
        "policy_enforcement": {
            "model": "PolicyEngine",
            "mode": "blocked_before_executor",
            "guard_preflight": str(exec_out / "guard_preflight.json"),
            "runtime_policy_preflight": str(runtime_policy_preflight),
        },
        "final_sched_ext_state": final_sched_ext_state,
        "end_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if extra_status:
        status_payload.update(extra_status)
    write_json(status_path, status_payload)
    write_json(exec_out / "guard_postflight.json", runtime_guard_postflight(status_path, None))
    if runtime_run:
        runtime_run.store.register(exec_out / "manifest.json", artifact_type="execution_manifest", producer="RuntimeGuard")
        runtime_run.store.register(exec_out / "guard_preflight.json", artifact_type="guard_preflight", producer="RuntimeGuard")
        runtime_run.store.register(status_path, artifact_type="execution_status", producer="RuntimeGuard")
        runtime_run.store.register(exec_out / "guard_postflight.json", artifact_type="guard_postflight", producer="RuntimeGuard")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=CLI_DESCRIPTION)
    ap.add_argument("--workload", choices=["mariadb-sysbench"], required=True)
    ap.add_argument("--threads", type=int, default=256)
    ap.add_argument("--profile-s", type=int, default=10)
    ap.add_argument("--run-s", type=int, default=60)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-llm", action="store_true", help="Use an existing --decision file")
    ap.add_argument("--decision", type=Path, help="Existing decision JSON when --skip-llm is used")
    ap.add_argument("--db-user", default=os.environ.get("KERNADAPT_DB_USER", os.environ.get("LOCKAGENT_DB_USER", "sbuser")))
    ap.add_argument("--db-pass", default=os.environ.get("KERNADAPT_DB_PASS", os.environ.get("LOCKAGENT_DB_PASS", "password")))
    ap.add_argument("--db-name", default=os.environ.get("KERNADAPT_DB_NAME", os.environ.get("LOCKAGENT_DB_NAME", "sbtest")))
    ap.add_argument(
        "--require-human-approval",
        action="store_true",
        default=os.environ.get("KERNADAPT_HITL", os.environ.get("LOCKAGENT_HITL", "0")) == "1",
        help="Pause before ExecutorAgent loads a non-default scheduler until human approval is recorded.",
    )
    ap.add_argument(
        "--update-skills",
        action=argparse.BooleanOptionalAction,
        default=os.environ.get("KERNADAPT_UPDATE_SKILLS", os.environ.get("LOCKAGENT_UPDATE_SKILLS", "auto")),
        help="Allow CuratorAgent to update the scheduler skill repository. Default auto updates real runs and skips dry-runs.",
    )
    ap.add_argument("--promote-after", type=int, default=int(os.environ.get("KERNADAPT_PROMOTE_AFTER", os.environ.get("LOCKAGENT_PROMOTE_AFTER", "2"))))
    ap.add_argument("--reject-after", type=int, default=int(os.environ.get("KERNADAPT_REJECT_AFTER", os.environ.get("LOCKAGENT_REJECT_AFTER", "2"))))
    ap.add_argument("--global-memory", type=Path, default=DEFAULT_GLOBAL_MEMORY)
    ap.add_argument("--global-memory-summary", type=Path, default=DEFAULT_GLOBAL_MEMORY_SUMMARY)
    ap.add_argument("--systemic-overrides", type=Path, default=DEFAULT_SYSTEMIC_OVERRIDES)
    ap.add_argument("--system-evolution-state", type=Path, default=DEFAULT_SYSTEM_EVOLUTION_STATE)
    ap.add_argument("--ignore-systemic-overrides", action="store_true")
    args = ap.parse_args(argv)
    if isinstance(args.update_skills, str):
        value = args.update_skills.strip().lower()
        args.update_skills = value in {"1", "true", "yes", "on"} if value != "auto" else not args.dry_run

    run_id = time.strftime("run_%Y%m%d_%H%M%S")
    outdir = args.out / run_id
    outdir.mkdir(parents=True, exist_ok=True)
    memory_isolation = apply_dry_run_memory_isolation(args, argv, outdir)
    overrides = {} if args.ignore_systemic_overrides else load_optional_json(args.systemic_overrides)
    override_application = apply_systemic_overrides(
        args,
        overrides,
        history_root=args.out,
    )
    applied_overrides = getattr(args, "systemic_overrides_applied", {})
    template_policy = runtime_template_policy(applied_overrides.get("meta_template"))
    if applied_overrides.get("require_strict_critic"):
        template_policy = {**template_policy, "requires_critic_preflight": True, "strict_critic_forced": True}

    profile = outdir / "profile.json"
    provided_decision = args.decision
    decision = outdir / "decision.json"
    state_ledger = outdir / "state_ledger.json"
    provenance = outdir / "provenance.json"
    causal_attribution = outdir / "causal_attribution.json"
    counterfactual_plan = outdir / "counterfactual_plan.json"
    counterfactual_out = outdir / "counterfactual_validation"
    same_batch_validation = counterfactual_out / "same_batch_validation.json"
    counterfactual_execution = counterfactual_out / "counterfactual_execution.json"
    critic_report = outdir / "critic_report.json"
    trajectory_status = outdir / "trajectory_status.json"
    failure_attribution = outdir / "failure_attribution.json"
    fallback_artifacts = outdir / "fallback_artifacts.json"
    skill_curation = outdir / "skill_curation.json"
    rejected_hypothesis = outdir / "rejected_hypothesis.json"
    skill_repository_update = outdir / "skill_repository_update.json"
    skill_evolution_application = outdir / "skill_evolution_application.json"
    self_evolution_plan = outdir / "self_evolution_plan.json"
    run_system_evolution_state = outdir / "system_evolution_state.json"
    systemic_override_application = outdir / "systemic_override_application.json"
    global_memory_record = outdir / "global_memory_record.json"
    memory_context = outdir / "memory_context.json"
    default_profile_out = outdir / "default_profile"
    default_profile_decision = default_profile_out / "decision.json"
    default_profile_metrics = outdir / "default_profile_metrics.json"
    exec_out = outdir / "execution"
    status_path = exec_out / "status.json"
    benchmark_log = exec_out / "benchmark" / "sysbench.log"
    runtime_run = AgentRun(
        run_id=run_id,
        intent="dry_run" if args.dry_run else "online",
        artifact_root=outdir,
        environment_fingerprint={
            "workload": args.workload,
            "threads": args.threads,
            "profile_s": args.profile_s,
            "run_s": args.run_s,
            "db_name": args.db_name,
        },
        graph=graph_from_meta_template(applied_overrides.get("meta_template")),
    )
    override_application["run_id"] = run_id
    override_application["memory_isolation"] = memory_isolation
    override_application["runtime_template_policy"] = template_policy
    runtime_run.store.write_json(
        systemic_override_application,
        override_application,
        artifact_type="systemic_override_application",
        producer="run_lockagent_online",
    )
    runtime_run.memory.add(
        namespace="systemic_override_application",
        key=run_id,
        value=override_application,
        source=str(systemic_override_application),
    )

    manifest: dict[str, object] = {
        "run_id": run_id,
        "profile": str(profile),
        "state_ledger": str(state_ledger),
        "provenance": str(provenance),
        "critic_report": str(critic_report),
        "trajectory_status": str(trajectory_status),
        "failure_attribution": str(failure_attribution),
        "fallback_artifacts": str(fallback_artifacts),
        "skill_curation": str(skill_curation),
        "rejected_hypothesis": str(rejected_hypothesis),
        "skill_evolution_application": str(skill_evolution_application),
        "default_profile": str(default_profile_out),
        "default_profile_metrics": str(default_profile_metrics),
        "decision": str(decision),
        "execution": str(exec_out),
        "workload": args.workload,
        "threads": args.threads,
        "profile_s": args.profile_s,
        "run_s": args.run_s,
        "dry_run": args.dry_run,
        "require_human_approval": args.require_human_approval,
        "update_skills": args.update_skills,
        "promote_after": args.promote_after,
        "reject_after": args.reject_after,
        "global_memory": str(args.global_memory),
        "global_memory_summary": str(args.global_memory_summary),
        "memory_context": str(memory_context),
        "systemic_overrides": str(args.systemic_overrides),
        "system_evolution_state": str(args.system_evolution_state),
        "memory_isolation": memory_isolation,
        "run_system_evolution_state": str(run_system_evolution_state),
        "systemic_overrides_applied": applied_overrides,
        "systemic_override_application": str(systemic_override_application),
        "meta_template_applied": applied_overrides.get("meta_template"),
        "runtime_template_policy": template_policy,
        "execution_graph": runtime_run.graph.to_dict(),
        "status": "running",
        "returncode": 1,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    try:
        run_stage(
            "state_ledger",
            [
                sys.executable,
                str(AGENT_DIR / "state_ledger.py"),
                "--run-id",
                run_id,
                "--workload",
                args.workload,
                "--threads",
                str(args.threads),
                "--profile-s",
                str(args.profile_s),
                "--run-s",
                str(args.run_s),
                "--db-name",
                args.db_name,
                "--db-user",
                args.db_user,
                "--db-pass",
                args.db_pass,
                "--out",
                str(state_ledger),
            ],
            manifest,
            dry_run=args.dry_run,
            runtime_run=runtime_run,
            role=ROLE_PROFILER,
            capabilities=[CAP_READ_SYSTEM_STATE, CAP_WRITE_ARTIFACT],
            output_artifacts=[state_ledger],
        )
        if args.dry_run:
            write_json(
                state_ledger,
                {
                    "run_id": run_id,
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "host": {"kernel": "dry_run", "numa_nodes": None, "cpu_count": None},
                    "sched_ext": {"initial_state": "dry_run", "allowed_schedulers": []},
                    "database": {"engine": "mariadb", "db_name": args.db_name, "collection_status": "unknown"},
                    "benchmark": {
                        "workload": args.workload,
                        "threads": args.threads,
                        "profile_s": args.profile_s,
                        "run_s": args.run_s,
                        "restart_protocol": "restart_before_profile_and_before_execution",
                    },
                },
            )
            runtime_run.store.register(state_ledger, artifact_type="state_ledger", producer="state_ledger")

        restart_mariadb("restart_mariadb_for_profile", default_profile_out, manifest, args.dry_run, runtime_run=runtime_run)
        write_json(default_profile_decision, decision_for_default())
        runtime_run.store.register(default_profile_decision, artifact_type="decision", producer="default_profile")
        run_stage(
            "default_profile_window",
            [
                sys.executable,
                str(AGENT_DIR / "scx_executor.py"),
                "--decision",
                str(default_profile_decision),
                "--benchmark-cmd",
                mariadb_benchmark_cmd(args, default_profile_out, args.profile_s),
                "--out",
                str(default_profile_out / "execution"),
                "--timeout-s",
                str(max(args.profile_s + 180, 300)),
                "--ledger",
                str(state_ledger),
            ]
            + (["--dry-run"] if args.dry_run else []),
            manifest,
            dry_run=False,
            runtime_run=runtime_run,
            role=ROLE_EXECUTOR,
            capabilities=[CAP_RUN_BENCHMARK, CAP_WRITE_ARTIFACT],
            input_artifacts=[default_profile_decision, state_ledger],
            output_artifacts=[
                default_profile_out / "execution" / "manifest.json",
                default_profile_out / "execution" / "status.json",
                default_profile_out / "execution" / "guard_preflight.json",
                default_profile_out / "execution" / "guard_postflight.json",
                sysbench_log_for(default_profile_out),
            ],
        )
        if not args.dry_run:
            metrics = parse_sysbench_log(sysbench_log_for(default_profile_out))
            write_json(default_profile_metrics, metrics)
            manifest["default_profile_observed"] = metrics
        else:
            write_json(default_profile_metrics, {"dry_run": True})
        runtime_run.store.register(default_profile_metrics, artifact_type="default_profile_metrics", producer="default_profile_window")

        run_stage(
            "profile_builder",
            [
                sys.executable,
                str(AGENT_DIR / "profile_builder.py"),
                "--workload",
                args.workload,
                "--threads",
                str(args.threads),
                "--profile-s",
                str(args.profile_s),
                "--default-metrics",
                str(default_profile_metrics),
                "--out",
                str(profile),
            ],
            manifest,
            dry_run=args.dry_run,
            runtime_run=runtime_run,
            role=ROLE_PROFILER,
            capabilities=[CAP_BUILD_PROFILE, CAP_WRITE_ARTIFACT],
            input_artifacts=[default_profile_metrics, state_ledger],
            output_artifacts=[profile],
        )
        if not args.dry_run:
            attach_skill_cards(profile, SKILL_CARDS)
            attach_experience_card_ids(profile, AGENT_DIR / "experience_cards.json")
        else:
            experience_data = load_json(AGENT_DIR / "experience_cards.json")
            dry_profile = {
                "profile_id": f"dry_run_{run_id}",
                "workload": {"name": args.workload, "threads": args.threads, "duration_s": args.profile_s},
                "signals": {
                    "lock_wait": 1.0,
                    "hot_lock_fanin": max(1, int(args.threads)),
                    "signal_source": "synthetic_dry_run",
                },
                "current_scheduler": "default",
                "candidate_schedulers": ["default", "scx_lockcoop", "scx_lockcoop"],
                "objective": {"primary": "dry-run"},
                "data_provenance": {
                    "contains_oracle_best": False,
                    "scheduler_skill_cards": str(SKILL_CARDS),
                    "experience_cards": str(AGENT_DIR / "experience_cards.json"),
                    "signals": "synthetic_dry_run_for_policy_path_testing",
                },
                "scheduler_skill_cards": load_json(SKILL_CARDS).get("skills", []),
                "experience_card_ids": [
                    str(card.get("id"))
                    for card in experience_data.get("cards", [])
                    if isinstance(card, dict) and card.get("id")
                ],
            }
            write_json(profile, dry_profile)
        runtime_run.store.register(profile, artifact_type="profile", producer="profile_builder")

        if not args.skip_llm:
            llm_cmd = [
                sys.executable,
                str(AGENT_DIR / "llm_policy_agent.py"),
                "--profile",
                str(profile),
                "--experience",
                str(AGENT_DIR / "experience_cards.json"),
                "--out",
                str(decision),
                "--prompt-out",
                str(outdir / "llm_prompt.json"),
                "--memory",
                str(args.global_memory),
                "--memory-out",
                str(memory_context),
            ]
            if args.dry_run:
                llm_cmd.append("--dry-run")
            run_stage(
                "llm_policy_agent",
                llm_cmd,
                manifest,
                dry_run=False,
                runtime_run=runtime_run,
                role=ROLE_POLICY,
                capabilities=[CAP_CALL_LLM, CAP_WRITE_ARTIFACT],
                input_artifacts=[profile, AGENT_DIR / "experience_cards.json", SKILL_CARDS, args.global_memory],
                output_artifacts=[decision, outdir / "llm_prompt.json", memory_context],
            )
            if args.dry_run and not decision.exists():
                write_json(decision, decision_for_default())
        else:
            if provided_decision is None:
                if not args.dry_run:
                    raise FileNotFoundError("--skip-llm requires --decision")
            elif not provided_decision.exists():
                if not args.dry_run:
                    raise FileNotFoundError(f"--decision does not exist: {provided_decision}")
            else:
                write_json(decision, load_json(provided_decision))
            prompt_path = outdir / "llm_prompt.json"
            if not prompt_path.exists():
                write_json(
                    prompt_path,
                    {
                        "mode": "skip_llm",
                        "provided_decision": str(provided_decision) if provided_decision else None,
                        "decision_snapshot": str(decision),
                    },
                )
        runtime_run.store.register(decision, artifact_type="decision", producer="llm_policy_agent")
        if memory_context.exists():
            runtime_run.store.register(memory_context, artifact_type="memory_context", producer="llm_policy_agent")
        decision_data = safe_load_json(decision)
        runtime_run.memory.add(
            namespace="policy_decisions",
            key=run_id,
            value={
                "scheduler": decision_data.get("scheduler"),
                "confidence": decision_data.get("confidence"),
                "evidence_used": decision_data.get("evidence_used", []),
                "decision_path": str(decision),
            },
            source=str(decision),
        )

        critic_preflight_path = outdir / "critic_preflight.json"
        if template_policy["requires_critic_preflight"]:
            critic_preflight = build_critic_preflight(profile, decision, state_ledger)
            runtime_run.store.write_json(
                critic_preflight_path,
                critic_preflight,
                artifact_type="critic_preflight",
                producer="CriticAgent",
            )
            runtime_run.execute_step(
                name="critic_preflight",
                role=ROLE_CRITIC,
                required_capabilities=[CAP_VALIDATE_ARTIFACT, CAP_WRITE_ARTIFACT],
                input_artifacts=[profile, decision, state_ledger],
                output_artifacts=[critic_preflight_path],
                metadata={"verdict": critic_preflight["verdict"], "checks": critic_preflight["checks"]},
                executor=lambda _: {
                    "status": "ok" if critic_preflight["verdict"] == "allow" else "blocked",
                    "returncode": 0 if critic_preflight["verdict"] == "allow" else 2,
                    "output_artifacts": [critic_preflight_path],
                },
            )
            if critic_preflight["verdict"] == "block":
                raise StageError("critic_preflight", 2)
        else:
            critic_preflight = {
                "schema_version": "lockagent.critic_preflight.v0.1",
                "phase": "critic_preflight",
                "verdict": "skipped",
                "checks": [],
                "blocking_reasons": [],
                "deferred_policy_reasons": [],
                "skip_reason": template_skip_reason(template_policy, "critic_preflight"),
                "policy_context": {"runtime_template_policy": template_policy},
            }
            record_skipped_stage(
                runtime_run=runtime_run,
                stage_name="critic_preflight",
                role=ROLE_CRITIC,
                reason=str(critic_preflight["skip_reason"]),
                input_artifacts=[profile, decision, state_ledger],
                output_artifacts=[critic_preflight_path],
                artifact_payloads={critic_preflight_path: critic_preflight},
                manifest=manifest,
                capabilities=[CAP_VALIDATE_ARTIFACT, CAP_WRITE_ARTIFACT],
            )

        decision_for_policy = load_json(decision)
        policy_context = build_policy_context(
            phase="preflight",
            decision=decision_for_policy,
            ledger=load_json(state_ledger),
            profile=load_json(profile),
            dry_run=args.dry_run,
            intent="online",
            requires_human_approval=args.require_human_approval
            and decision_for_policy.get("scheduler") != "default",
            human_approved=runtime_run.human_gate.is_approved(),
            require_environment_compatibility=applied_overrides.get("require_environment_compatibility", False),
            environment_compatible=True,
            require_strict_critic=applied_overrides.get("require_strict_critic", False),
            critic_verdict="allow" if critic_preflight.get("verdict") == "skipped" else critic_preflight.get("verdict"),
            scheduler_disablelist=[
                item
                for item in applied_overrides.get("scheduler_disablelist", [])
            ],
        )
        policy_verdict = PolicyEngine().evaluate(policy_context)
        policy_report = {
            "schema_version": "lockagent.runtime_policy_preflight.v0.1",
            "phase": "preflight",
            "enforcement_model": "PolicyEngine",
            "verdict": policy_verdict["verdict"],
            "reasons": policy_verdict.get("blocking_reasons", []),
            "warnings": policy_verdict.get("warnings", []),
            "policy_context": policy_context,
            "policy_engine": policy_verdict,
        }
        runtime_run.store.write_json(
            outdir / "runtime_policy_preflight.json",
            policy_report,
            artifact_type="policy_verdict",
            producer="PolicyEngine",
        )
        runtime_run.execute_step(
            name="runtime_policy_preflight",
            role=ROLE_GUARD,
            required_capabilities=[CAP_EVALUATE_POLICY, CAP_WRITE_ARTIFACT],
            input_artifacts=[decision, state_ledger, profile],
            output_artifacts=[outdir / "runtime_policy_preflight.json"],
            metadata={"verdict": policy_report["verdict"], "checks": policy_verdict["checks"]},
            executor=lambda _: {
                "status": "ok" if policy_report["verdict"] == "allow" else "blocked",
                "returncode": 0 if policy_report["verdict"] == "allow" else 2,
                "output_artifacts": [outdir / "runtime_policy_preflight.json"],
            },
        )
        if policy_report["verdict"] == "block":
            policy_reasons = list(dict.fromkeys(str(reason) for reason in policy_report.get("reasons", []) if reason))
            benchmark_cmd = mariadb_benchmark_cmd(args, exec_out, args.run_s)
            timeout_s = max(args.run_s + 180, 300)
            if args.require_human_approval and set(policy_reasons) == {"human_approval_required"}:
                human_request = runtime_run.pause_for_human(
                    step_name="scx_executor",
                    reason="ExecutorAgent requires approval before non-default sched_ext loading.",
                    artifacts=[decision, state_ledger, outdir / "runtime_policy_preflight.json"],
                )
                runtime_run.execute_step(
                    name="human_approval_gate",
                    role=ROLE_GUARD,
                    required_capabilities=[CAP_EVALUATE_POLICY, CAP_WRITE_ARTIFACT],
                    input_artifacts=[decision, state_ledger, outdir / "runtime_policy_preflight.json"],
                    output_artifacts=[runtime_run.human_gate.path],
                    metadata={
                        "requires_human_approval": True,
                        "resume_token": human_request.get("resume_token"),
                        "command": [
                            sys.executable,
                            str(AGENT_DIR / "agent_runtime.py"),
                            "approve-human-gate",
                            "--checkpoint",
                            str(runtime_run.checkpoint_path),
                            "--approved-by",
                            "$USER",
                        ],
                    },
                    executor=lambda _: {
                        "status": "paused",
                        "returncode": 2,
                        "output_artifacts": [runtime_run.human_gate.path],
                    },
                )
                runtime_run.submit_task(
                    task_id=f"{run_id}:pending:scx_executor",
                    role=ROLE_EXECUTOR,
                    step_name="scx_executor",
                    payload={
                        "decision": str(decision),
                        "state_ledger": str(state_ledger),
                        "execution": str(exec_out),
                        "command": [
                            sys.executable,
                            str(AGENT_DIR / "scx_executor.py"),
                            "--decision",
                            str(decision),
                            "--benchmark-cmd",
                            benchmark_cmd,
                            "--out",
                            str(exec_out),
                            "--timeout-s",
                            str(timeout_s),
                            "--ledger",
                            str(state_ledger),
                            "--profile",
                            str(profile),
                            "--runtime-policy-preflight",
                            str(outdir / "runtime_policy_preflight.json"),
                        ]
                        + (["--dry-run"] if args.dry_run else []),
                        "required_capabilities": [CAP_LOAD_SCX, CAP_ROOT_CONTROL, CAP_RUN_BENCHMARK, CAP_WRITE_ARTIFACT],
                        "input_artifacts": [str(decision), str(state_ledger), str(profile)],
                        "output_artifacts": [
                            str(exec_out / "manifest.json"),
                            str(exec_out / "status.json"),
                            str(exec_out / "guard_preflight.json"),
                            str(exec_out / "guard_postflight.json"),
                            str(benchmark_log),
                        ],
                    },
                    priority=10,
                )
                materialize_guard_blocked_executor_artifacts(
                    runtime_run=runtime_run,
                    exec_out=exec_out,
                    status_path=status_path,
                    decision=decision_for_policy,
                    benchmark_cmd=benchmark_cmd,
                    timeout_s=timeout_s,
                    dry_run=args.dry_run,
                    intent="online",
                    state_ledger=state_ledger,
                    profile=profile,
                    runtime_policy_preflight=outdir / "runtime_policy_preflight.json",
                    policy_report=policy_report,
                    policy_verdict=policy_verdict,
                    policy_context=policy_context,
                    status="paused_for_human_approval",
                    final_sched_ext_state="disabled",
                    extra_status={"human_gate": str(runtime_run.human_gate.path)},
                )
            else:
                materialize_guard_blocked_executor_artifacts(
                    runtime_run=runtime_run,
                    exec_out=exec_out,
                    status_path=status_path,
                    decision=decision_for_policy,
                    benchmark_cmd=benchmark_cmd,
                    timeout_s=timeout_s,
                    dry_run=args.dry_run,
                    intent="online",
                    state_ledger=state_ledger,
                    profile=profile,
                    runtime_policy_preflight=outdir / "runtime_policy_preflight.json",
                    policy_report=policy_report,
                    policy_verdict=policy_verdict,
                    policy_context=policy_context,
                )
            raise StageError("runtime_policy_preflight", 2)

        restart_mariadb("restart_mariadb_for_execution", exec_out, manifest, args.dry_run, runtime_run=runtime_run)
        run_stage(
            "scx_executor",
            [
                sys.executable,
                str(AGENT_DIR / "scx_executor.py"),
                "--decision",
                str(decision),
                "--benchmark-cmd",
                mariadb_benchmark_cmd(args, exec_out, args.run_s),
                "--out",
                str(exec_out),
                "--timeout-s",
                str(max(args.run_s + 180, 300)),
                "--ledger",
                str(state_ledger),
                "--profile",
                str(profile),
                "--runtime-policy-preflight",
                str(outdir / "runtime_policy_preflight.json"),
            ]
            + (["--dry-run"] if args.dry_run else []),
            manifest,
            dry_run=False,
            runtime_run=runtime_run,
            role=ROLE_EXECUTOR,
            capabilities=[CAP_LOAD_SCX, CAP_ROOT_CONTROL, CAP_RUN_BENCHMARK, CAP_WRITE_ARTIFACT],
            input_artifacts=[decision, state_ledger, profile],
            output_artifacts=[
                exec_out / "manifest.json",
                exec_out / "status.json",
                exec_out / "guard_preflight.json",
                exec_out / "guard_postflight.json",
                benchmark_log,
            ],
        )

        manifest["status"] = "ok"
        manifest["returncode"] = 0
    except StageError as exc:
        manifest["status"] = "failed"
        manifest["failed_stage"] = exc.stage
        manifest["returncode"] = exc.returncode
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["returncode"] = 1
    finally:
        manifest["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        fallback_records = materialize_failure_fallback_artifacts(
            run_id=run_id,
            workload=args.workload,
            threads=args.threads,
            db_name=args.db_name,
            end_time=str(manifest["end_time"]),
            manifest=manifest,
            status_path=status_path,
            decision_path=decision,
            profile_path=profile,
            state_ledger_path=state_ledger,
            fallback_path=fallback_artifacts,
        )
        runtime_run.store.register(
            fallback_artifacts,
            artifact_type="fallback_artifacts",
            producer="run_lockagent_online",
        )
        manifest["fallback_artifacts_created"] = bool(fallback_records)
        critic_cmd = [
            sys.executable,
            str(AGENT_DIR / "critic.py"),
            "--run-id",
            run_id,
            "--ledger",
            str(state_ledger),
            "--profile",
            str(profile),
            "--decision",
            str(decision),
            "--status",
            str(status_path),
            "--benchmark-log",
            str(benchmark_log),
            "--preflight",
            str(exec_out / "guard_preflight.json"),
            "--postflight",
            str(exec_out / "guard_postflight.json"),
            "--critic-out",
            str(critic_report),
            "--trajectory-out",
            str(trajectory_status),
            "--failure-out",
            str(failure_attribution),
        ]
        run_stage(
            "critic_postflight",
            critic_cmd,
            manifest,
            dry_run=False,
            raise_on_error=False,
            runtime_run=runtime_run,
            role=ROLE_CRITIC,
            capabilities=[CAP_VALIDATE_ARTIFACT, CAP_ATTRIBUTE_FAILURE, CAP_WRITE_ARTIFACT],
            input_artifacts=[state_ledger, profile, decision, status_path, benchmark_log],
            output_artifacts=[critic_report, trajectory_status, failure_attribution],
        )
        provenance_cmd = [
            sys.executable,
            str(AGENT_DIR / "provenance.py"),
            "--run-id",
            run_id,
            "--ledger",
            str(state_ledger),
            "--profile",
            str(profile),
            "--decision",
            str(decision),
            "--manifest",
            str(exec_out / "manifest.json"),
            "--status",
            str(status_path),
            "--benchmark-log",
            str(benchmark_log),
            "--default-metrics",
            str(default_profile_metrics),
            "--agent-run",
            str(runtime_run.checkpoint_path),
            "--failure",
            str(failure_attribution),
            "--artifact-store",
            str(runtime_run.store.index_path),
            "--memory-store",
            str(runtime_run.memory.path),
            "--skill-curation",
            str(skill_curation),
            "--guard-preflight",
            str(exec_out / "guard_preflight.json"),
            "--guard-postflight",
            str(exec_out / "guard_postflight.json"),
            "--human-gate",
            str(runtime_run.human_gate.path),
            "--durable-summary",
            str(runtime_run.artifact_root / "durable_runtime_summary.json"),
            "--typed-event-bus",
            str(runtime_run.event_bus.index_path),
            "--out",
            str(provenance),
        ]
        run_stage(
            "build_provenance",
            provenance_cmd,
            manifest,
            dry_run=False,
            raise_on_error=False,
            runtime_run=runtime_run,
            role=ROLE_CRITIC,
            capabilities=[CAP_EMIT_PROVENANCE, CAP_WRITE_ARTIFACT],
            input_artifacts=[
                state_ledger,
                profile,
                decision,
                exec_out / "manifest.json",
                status_path,
                benchmark_log,
                failure_attribution,
                runtime_run.checkpoint_path,
                runtime_run.store.index_path,
                runtime_run.memory.path,
                skill_curation,
                exec_out / "guard_preflight.json",
                exec_out / "guard_postflight.json",
                runtime_run.human_gate.path,
            ],
            output_artifacts=[provenance],
        )
        curator_cmd = [
            sys.executable,
            str(AGENT_DIR / "skill_curator.py"),
            "curate-run",
            "--repository",
            str(SKILL_CARDS),
            "--decision",
            str(decision),
            "--critic",
            str(critic_report),
            "--failure",
            str(failure_attribution),
            "--trajectory",
            str(trajectory_status),
            "--run-dir",
            str(outdir),
            "--curation-out",
            str(skill_curation),
            "--rejected-out",
            str(rejected_hypothesis),
        ]
        if args.update_skills:
            curator_cmd.extend(
                [
                    "--update-repository",
                    "--promotion-out",
                    str(skill_repository_update),
                    "--promote-after",
                    str(args.promote_after),
                    "--reject-after",
                    str(args.reject_after),
                ]
            )
        if template_policy["run_skill_curator"]:
            run_stage(
                "skill_curator",
                curator_cmd,
                manifest,
                dry_run=False,
                raise_on_error=False,
                runtime_run=runtime_run,
                role=ROLE_CURATOR,
                capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
                input_artifacts=[SKILL_CARDS, decision, critic_report, failure_attribution, trajectory_status],
                output_artifacts=[skill_curation, rejected_hypothesis, skill_repository_update],
            )
        else:
            skip_reason = template_skip_reason(template_policy, "skill_curator")
            record_skipped_stage(
                runtime_run=runtime_run,
                stage_name="skill_curator",
                role=ROLE_CURATOR,
                reason=skip_reason,
                input_artifacts=[SKILL_CARDS, decision, critic_report, failure_attribution, trajectory_status],
                output_artifacts=[skill_curation, rejected_hypothesis, skill_repository_update],
                artifact_payloads={
                    skill_curation: {
                        "schema_version": "lockagent.skill_curation.v0.1",
                        "status": "skipped",
                        "skill": None,
                        "verdict": "not_evaluated",
                        "run_dir": str(outdir),
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                    rejected_hypothesis: {
                        "schema_version": "lockagent.rejected_hypothesis.v0.1",
                        "status": "skipped",
                        "rejected": False,
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                    skill_repository_update: {
                        "schema_version": "lockagent.skill_repository_update.v0.1",
                        "status": "skipped",
                        "updated": False,
                        "repository_mutated": False,
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                },
                manifest=manifest,
                capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
            )
        run_stage(
            "self_evolution_planner",
            [
                sys.executable,
                str(AGENT_DIR / "self_evolution.py"),
                "--root",
                str(outdir),
                "--out",
                str(self_evolution_plan),
                "--state",
                str(args.system_evolution_state),
                "--state-out",
                str(run_system_evolution_state),
                "--overrides-out",
                str(args.systemic_overrides),
            ],
            manifest,
            dry_run=False,
            raise_on_error=False,
            runtime_run=runtime_run,
            role=ROLE_CURATOR,
            capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
            input_artifacts=[failure_attribution, trajectory_status, skill_curation],
            output_artifacts=[self_evolution_plan, run_system_evolution_state],
        )
        if run_system_evolution_state.exists():
            args.system_evolution_state.parent.mkdir(parents=True, exist_ok=True)
            args.system_evolution_state.write_text(run_system_evolution_state.read_text(encoding="utf-8"), encoding="utf-8")
        run_stage(
            "global_memory_append",
            [
                sys.executable,
                str(AGENT_DIR / "global_memory.py"),
                "append-run",
                "--memory",
                str(args.global_memory),
                "--run-dir",
                str(outdir),
                "--summary-out",
                str(args.global_memory_summary),
                "--record-out",
                str(global_memory_record),
            ],
            manifest,
            dry_run=False,
            raise_on_error=False,
            runtime_run=runtime_run,
            role=ROLE_CURATOR,
            capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
            input_artifacts=[decision, failure_attribution, trajectory_status, skill_curation, self_evolution_plan],
            output_artifacts=[args.global_memory, args.global_memory_summary, global_memory_record],
        )
        critic_data = safe_load_json(critic_report)
        trajectory_data = safe_load_json(trajectory_status)
        failure_data = safe_load_json(failure_attribution)
        skill_data = safe_load_json(skill_curation)
        evolution_data = safe_load_json(self_evolution_plan)
        runtime_run.memory.add(
            namespace="execution_outcomes",
            key=run_id,
            value={
                "manifest_status": manifest.get("status"),
                "returncode": manifest.get("returncode"),
                "trajectory_valid": trajectory_data.get("valid"),
                "failure_categories": failure_data.get("categories", []),
                "critic_valid": critic_data.get("valid"),
            },
            source=str(trajectory_status),
        )
        runtime_run.memory.add(
            namespace="skill_evidence",
            key=str(skill_data.get("skill", "unknown")),
            value=skill_data,
            source=str(skill_curation),
        )
        runtime_run.memory.add(
            namespace="systemic_evolution",
            key=run_id,
            value={
                "failure_distribution": evolution_data.get("failure_distribution", {}),
                "systemic_actions": evolution_data.get("systemic_actions", []),
                "meta_template": evolution_data.get("meta_template"),
            },
            source=str(self_evolution_plan),
        )
        final_provenance_cmd = [
            sys.executable,
            str(AGENT_DIR / "provenance.py"),
            "--run-id",
            run_id,
            "--ledger",
            str(state_ledger),
            "--profile",
            str(profile),
            "--decision",
            str(decision),
            "--manifest",
            str(exec_out / "manifest.json"),
            "--status",
            str(status_path),
            "--benchmark-log",
            str(benchmark_log),
            "--default-metrics",
            str(default_profile_metrics),
            "--agent-run",
            str(runtime_run.checkpoint_path),
            "--failure",
            str(failure_attribution),
            "--artifact-store",
            str(runtime_run.store.index_path),
            "--memory-store",
            str(runtime_run.memory.path),
            "--skill-curation",
            str(skill_curation),
            "--guard-preflight",
            str(exec_out / "guard_preflight.json"),
            "--guard-postflight",
            str(exec_out / "guard_postflight.json"),
            "--human-gate",
            str(runtime_run.human_gate.path),
            "--durable-summary",
            str(runtime_run.artifact_root / "durable_runtime_summary.json"),
            "--typed-event-bus",
            str(runtime_run.event_bus.index_path),
            "--out",
            str(provenance),
        ]
        run_stage(
            "build_final_provenance",
            final_provenance_cmd,
            manifest,
            dry_run=False,
            raise_on_error=False,
            runtime_run=runtime_run,
            role=ROLE_CRITIC,
            capabilities=[CAP_EMIT_PROVENANCE, CAP_WRITE_ARTIFACT],
            input_artifacts=[
                state_ledger,
                profile,
                decision,
                exec_out / "manifest.json",
                status_path,
                benchmark_log,
                failure_attribution,
                runtime_run.checkpoint_path,
                runtime_run.store.index_path,
                runtime_run.memory.path,
                runtime_run.artifact_root / "durable_runtime_summary.json",
                runtime_run.event_bus.index_path,
                skill_curation,
                exec_out / "guard_preflight.json",
                exec_out / "guard_postflight.json",
                runtime_run.human_gate.path,
            ],
            output_artifacts=[provenance],
        )
        if template_policy["run_causal_attribution"]:
            run_stage(
                "causal_attribution",
                [
                    sys.executable,
                    str(AGENT_DIR / "causal_attribution.py"),
                    "--run-id",
                    run_id,
                    "--provenance",
                    str(provenance),
                    "--failure",
                    str(failure_attribution),
                    "--same-batch-validation",
                    str(same_batch_validation),
                    "--counterfactual-execution",
                    str(counterfactual_execution),
                    "--out",
                    str(causal_attribution),
                ],
                manifest,
                dry_run=False,
                raise_on_error=False,
                runtime_run=runtime_run,
                role=ROLE_CRITIC,
                capabilities=[CAP_ATTRIBUTE_FAILURE, CAP_WRITE_ARTIFACT],
                input_artifacts=[provenance, failure_attribution, same_batch_validation, counterfactual_execution],
                output_artifacts=[causal_attribution],
            )
        else:
            skip_reason = template_skip_reason(template_policy, "causal_attribution")
            record_skipped_stage(
                runtime_run=runtime_run,
                stage_name="causal_attribution",
                role=ROLE_CRITIC,
                reason=skip_reason,
                input_artifacts=[provenance, failure_attribution, same_batch_validation, counterfactual_execution],
                output_artifacts=[causal_attribution],
                artifact_payloads={
                    causal_attribution: {
                        "schema_version": "lockagent.causal_attribution.v0.1",
                        "run_id": run_id,
                        "status": "skipped",
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                        "causal_chains": [],
                        "summary": {
                            "status": "skipped",
                            "root_cause_candidate_count": 0,
                            "bad_profile_bad_skill_bad_scheduler_regression_path": False,
                            "bad_profile_to_regression_path": False,
                            "skill_to_regression_path": False,
                            "scheduler_to_regression_path": False,
                        },
                    },
                },
                manifest=manifest,
                capabilities=[CAP_ATTRIBUTE_FAILURE, CAP_WRITE_ARTIFACT],
            )
        counterfactual_cmd = [
            sys.executable,
            str(AGENT_DIR / "counterfactual_planner.py"),
            "--causal",
            str(causal_attribution),
            "--run-dir",
            str(outdir),
            "--out-root",
            str(outdir.parent),
            "--checkpoint",
            str(runtime_run.checkpoint_path),
            "--submit-tasks",
            "--repository",
            str(SKILL_CARDS),
            "--promote-after",
            str(args.promote_after),
            "--reject-after",
            str(args.reject_after),
            "--out",
            str(counterfactual_plan),
        ]
        if args.update_skills:
            counterfactual_cmd.append("--update-repository")
        if template_policy["run_counterfactual_planner"]:
            run_stage(
                "counterfactual_planner",
                counterfactual_cmd,
                manifest,
                dry_run=False,
                raise_on_error=False,
                runtime_run=runtime_run,
                role=ROLE_CRITIC,
                capabilities=[CAP_ATTRIBUTE_FAILURE, CAP_WRITE_ARTIFACT],
                input_artifacts=[causal_attribution],
                output_artifacts=[counterfactual_plan],
            )
        else:
            skip_reason = template_skip_reason(template_policy, "counterfactual_planner")
            record_skipped_stage(
                runtime_run=runtime_run,
                stage_name="counterfactual_planner",
                role=ROLE_CRITIC,
                reason=skip_reason,
                input_artifacts=[causal_attribution],
                output_artifacts=[counterfactual_plan, counterfactual_execution, same_batch_validation],
                artifact_payloads={
                    counterfactual_plan: {
                        "schema_version": "lockagent.counterfactual_plan.v0.1",
                        "run_id": run_id,
                        "status": "skipped",
                        "requires_counterfactual": False,
                        "checks": [],
                        "tasks": [],
                        "executable_tasks": [],
                        "task_count": 0,
                        "executable_task_count": 0,
                        "submission": {"submitted_count": 0, "status": "skipped"},
                        "skill_evolution": {"update_repository": False, "repository": str(SKILL_CARDS)},
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                    counterfactual_execution: {
                        "schema_version": "lockagent.counterfactual_execution.v0.1",
                        "status": "skipped",
                        "dry_run": True,
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                    same_batch_validation: {
                        "schema_version": "lockagent.same_batch_validation.v0.1",
                        "status": "skipped",
                        "trajectory_valid": False,
                        "validation": {
                            "environment_compatible": None,
                            "counterfactual_default_checked": False,
                            "same_batch_control": False,
                        },
                        "reasons": [skip_reason],
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                },
                manifest=manifest,
                capabilities=[CAP_ATTRIBUTE_FAILURE, CAP_WRITE_ARTIFACT],
            )
        skill_evolution_cmd = [
            sys.executable,
            str(AGENT_DIR / "skill_curator.py"),
            "apply-evolution",
            "--repository",
            str(SKILL_CARDS),
            "--root",
            str(outdir),
            "--out",
            str(skill_evolution_application),
            "--promote-after",
            str(args.promote_after),
            "--reject-after",
            str(args.reject_after),
        ]
        if not args.update_skills:
            skill_evolution_cmd.append("--dry-run")
        if template_policy["run_skill_evolution_application"]:
            run_stage(
                "skill_evolution_application",
                skill_evolution_cmd,
                manifest,
                dry_run=False,
                raise_on_error=False,
                runtime_run=runtime_run,
                role=ROLE_CURATOR,
                capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
                input_artifacts=[SKILL_CARDS, skill_curation, same_batch_validation, counterfactual_execution],
                output_artifacts=[skill_evolution_application],
            )
        else:
            skip_reason = template_skip_reason(template_policy, "skill_evolution_application")
            record_skipped_stage(
                runtime_run=runtime_run,
                stage_name="skill_evolution_application",
                role=ROLE_CURATOR,
                reason=skip_reason,
                input_artifacts=[SKILL_CARDS, skill_curation, same_batch_validation, counterfactual_execution],
                output_artifacts=[skill_evolution_application],
                artifact_payloads={
                    skill_evolution_application: {
                        "schema_version": "lockagent.skill_evolution_application.v0.1",
                        "status": "skipped",
                        "dry_run": True,
                        "applied_count": 0,
                        "transition_count": 0,
                        "skipped_count": 1,
                        "transitions": [],
                        "repository_mutated": False,
                        "skip_reason": skip_reason,
                        "runtime_template_policy": template_policy,
                    },
                },
                manifest=manifest,
                capabilities=[CAP_CURATE_SKILL, CAP_WRITE_ARTIFACT],
            )
        online_manifest = outdir / "online_manifest.json"
        runtime_run.store.write_json(online_manifest, manifest, artifact_type="online_manifest", producer="run_lockagent_online")
        runtime_run.finish(status=str(manifest["status"]))
        finalize_runtime_artifacts(runtime_run, manifest)
        runtime_run.store.write_json(online_manifest, manifest, artifact_type="online_manifest", producer="run_lockagent_online")
        runtime_run.checkpoint()
        print(outdir)
    return int(manifest["returncode"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

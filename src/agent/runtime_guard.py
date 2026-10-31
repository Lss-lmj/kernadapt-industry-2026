#!/usr/bin/env python3
"""Runtime guard checks for privileged LockAgent scheduler actions."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
from pathlib import Path
from typing import Any

from agent_runtime import PolicyEngine
from llm_policy_agent import validate_decision


ALLOWED_SCHEDULERS = {
    "default",
    "scx_simple",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
    "scx_hybrid",
    "scx_toponest",
    "scx_cfl",
    "scx_lcas",
}
SCX_PROCESS_NAMES = ALLOWED_SCHEDULERS - {"default"}


POLICY_CONTEXT_SCHEMA = "lockagent.policy_context.v0.1"


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def scx_processes() -> list[str]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,comm=,args="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    matches: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 2)
        if len(parts) < 2:
            continue
        pid = parts[0]
        comm = parts[1]
        args = parts[2] if len(parts) > 2 else ""
        names = {comm}
        try:
            argv = shlex.split(args)
        except ValueError:
            argv = args.split()
        if argv:
            names.add(Path(argv[0]).name)
        if len(argv) >= 2 and Path(argv[0]).name in {"sudo", "doas"}:
            names.add(Path(argv[1]).name)
        if names & SCX_PROCESS_NAMES:
            matches.append(f"{pid} {comm} {args}".strip())
    return matches


def validate_decision_schema(decision: dict[str, Any] | None) -> list[str]:
    if not decision:
        return ["missing_decision"]
    try:
        validate_decision(decision)
    except Exception as exc:
        return [str(exc)]
    return []


def build_policy_context(
    *,
    phase: str,
    decision: dict[str, Any] | None = None,
    ledger: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
    manifest: dict[str, Any] | None = None,
    decision_schema_errors: list[str] | None = None,
    dry_run: bool = False,
    intent: str = "online",
    requires_executor: bool = False,
    requires_human_approval: bool = False,
    human_approved: bool = False,
    environment_compatible: bool | None = None,
    require_environment_compatibility: bool = False,
    require_strict_critic: bool = False,
    critic_verdict: str | None = None,
    scheduler_disablelist: list[str] | None = None,
    upstream_policy_required: bool = False,
    upstream_policy_exists: bool = False,
    upstream_policy_verdict: str | None = None,
    status: dict[str, Any] | None = None,
    benchmark_log_expected: bool = False,
    benchmark_log_exists: bool = False,
    scx_residual_processes: list[str] | None = None,
) -> dict[str, Any]:
    """Build the canonical RuntimeGuard -> PolicyEngine context.

    `runtime_guard.py`, `scx_executor.py`, and the online runner should all
    reason over this shape so enforcement artifacts can be compared directly.
    """

    manifest_scheduler = None
    if manifest:
        manifest_scheduler = manifest.get("scheduler") or manifest.get("decision", {}).get("scheduler")
    if environment_compatible is None:
        environment_compatible = intent != "reproduction"
    data_provenance = profile.get("data_provenance", {}) if isinstance(profile, dict) else {}
    signals = profile.get("signals", {}) if isinstance(profile, dict) else {}
    profile_scheduler_cards = profile.get("scheduler_skill_cards", []) if isinstance(profile, dict) else []
    experience_card_ids = profile.get("experience_card_ids", []) if isinstance(profile, dict) else []
    database = ledger.get("database", {}) if isinstance(ledger, dict) else {}
    benchmark = ledger.get("benchmark", {}) if isinstance(ledger, dict) else {}
    return {
        "schema_version": POLICY_CONTEXT_SCHEMA,
        "phase": phase,
        "decision": decision,
        "decision_schema_errors": (
            decision_schema_errors
            if decision_schema_errors is not None
            else (validate_decision_schema(decision) if phase == "preflight" else [])
        ),
        "ledger_exists": ledger is not None,
        "ledger_database_status": database.get("collection_status"),
        "ledger_benchmark_protocol": benchmark.get("restart_protocol") or benchmark.get("protocol"),
        "profile_exists": profile is not None,
        "profile_signals": signals,
        "profile_contains_oracle_best": bool(data_provenance.get("contains_oracle_best")),
        "profile_workload_family": (
            profile.get("workload", {}).get("family")
            or profile.get("workload", {}).get("name")
            if isinstance(profile, dict)
            else None
        ),
        "profile_candidate_schedulers": profile.get("candidate_schedulers", []) if isinstance(profile, dict) else [],
        "skill_cards": profile_scheduler_cards,
        "experience_card_ids": experience_card_ids,
        "manifest_exists": manifest is not None,
        "manifest_scheduler": manifest_scheduler,
        "requires_executor": requires_executor,
        "dry_run": dry_run,
        "intent": intent,
        "environment_compatible": environment_compatible,
        "require_environment_compatibility": require_environment_compatibility,
        "require_strict_critic": require_strict_critic,
        "critic_verdict": critic_verdict,
        "scheduler_disablelist": scheduler_disablelist or [],
        "upstream_policy_required": upstream_policy_required,
        "upstream_policy_exists": upstream_policy_exists,
        "upstream_policy_verdict": upstream_policy_verdict,
        "requires_human_approval": requires_human_approval,
        "human_approved": human_approved,
        "status_exists": status is not None,
        "final_sched_ext_state": (status or {}).get("final_sched_ext_state"),
        "executor_returncode": (status or {}).get("returncode"),
        "benchmark_log_expected": benchmark_log_expected,
        "benchmark_log_exists": benchmark_log_exists,
        "scx_residual_processes": scx_residual_processes or [],
    }


def evaluate_policy_context(context: dict[str, Any]) -> dict[str, Any]:
    return PolicyEngine().evaluate(context)


def preflight(
    *,
    decision_path: Path,
    ledger_path: Path,
    profile_path: Path | None = None,
    manifest_path: Path | None = None,
    dry_run: bool,
    intent: str,
    requires_human_approval: bool = False,
    human_approved: bool = False,
    environment_compatible: bool | None = None,
    require_environment_compatibility: bool = False,
    require_strict_critic: bool = False,
    critic_verdict: str | None = None,
    scheduler_disablelist: list[str] | None = None,
    upstream_policy_preflight_path: Path | None = None,
) -> dict[str, Any]:
    decision = load_json(decision_path)
    ledger = load_json(ledger_path)
    profile = load_json(profile_path) if profile_path else None
    manifest = load_json(manifest_path) if manifest_path else None
    upstream_policy = load_json(upstream_policy_preflight_path) if upstream_policy_preflight_path else None

    scheduler = decision.get("scheduler") if decision else None
    policy_context = build_policy_context(
        phase="preflight",
        decision=decision,
        ledger=ledger,
        profile=profile,
        manifest=manifest,
        dry_run=dry_run,
        intent=intent,
        requires_executor=bool(manifest_path),
        requires_human_approval=requires_human_approval,
        human_approved=human_approved,
        environment_compatible=environment_compatible,
        require_environment_compatibility=require_environment_compatibility,
        require_strict_critic=require_strict_critic,
        critic_verdict=critic_verdict,
        scheduler_disablelist=scheduler_disablelist or [],
        upstream_policy_required=upstream_policy_preflight_path is not None,
        upstream_policy_exists=upstream_policy is not None,
        upstream_policy_verdict=(upstream_policy or {}).get("verdict"),
    )
    policy_verdict = evaluate_policy_context(policy_context)
    reasons = list(dict.fromkeys([reason for reason in policy_verdict.get("blocking_reasons", []) if reason]))
    warnings = list(dict.fromkeys([warning for warning in policy_verdict.get("warnings", []) if warning]))

    verdict = "allow" if not reasons else "block"
    return {
        "schema_version": "lockagent.runtime_guard.v0.1",
        "phase": "preflight",
        "enforcement_model": "PolicyEngine",
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        "policy_context": policy_context,
        "policy_engine": policy_verdict,
        "scheduler": scheduler,
        "dry_run": dry_run,
        "intent": intent,
        "requires_human_approval": requires_human_approval,
        "human_approved": human_approved,
        "require_environment_compatibility": require_environment_compatibility,
        "require_strict_critic": require_strict_critic,
        "scheduler_disablelist": scheduler_disablelist or [],
        "upstream_policy_preflight": str(upstream_policy_preflight_path) if upstream_policy_preflight_path else None,
    }


def postflight(status_path: Path, benchmark_log: Path | None = None) -> dict[str, Any]:
    status = load_json(status_path)
    residual = scx_processes()
    policy_context = build_policy_context(
        phase="postflight",
        status=status,
        benchmark_log_expected=benchmark_log is not None,
        benchmark_log_exists=benchmark_log.exists() if benchmark_log else False,
        scx_residual_processes=residual,
    )
    policy_verdict = evaluate_policy_context(policy_context)
    reasons = [reason for reason in policy_verdict.get("blocking_reasons", []) if reason]
    warnings = [warning for warning in policy_verdict.get("warnings", []) if warning]
    verdict = policy_verdict.get("verdict", "block")
    return {
        "schema_version": "lockagent.runtime_guard.v0.1",
        "phase": "postflight",
        "enforcement_model": "PolicyEngine",
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        "policy_context": policy_context,
        "policy_engine": policy_verdict,
        "final_sched_ext_state": (status or {}).get("final_sched_ext_state"),
        "scx_residual_processes": residual,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pre = sub.add_parser("preflight")
    pre.add_argument("--decision", type=Path, required=True)
    pre.add_argument("--ledger", type=Path, required=True)
    pre.add_argument("--profile", type=Path)
    pre.add_argument("--manifest", type=Path)
    pre.add_argument("--dry-run", action="store_true")
    pre.add_argument("--intent", default="online")
    pre.add_argument("--requires-human-approval", action="store_true")
    pre.add_argument("--human-approved", action="store_true")
    pre.add_argument("--environment-compatible", action="store_true")
    pre.add_argument("--require-environment-compatibility", action="store_true")
    pre.add_argument("--require-strict-critic", action="store_true")
    pre.add_argument("--critic-verdict")
    pre.add_argument("--scheduler-disablelist", action="append", default=[])
    pre.add_argument("--runtime-policy-preflight", type=Path)
    pre.add_argument("--out", type=Path, required=True)
    post = sub.add_parser("postflight")
    post.add_argument("--status", type=Path, required=True)
    post.add_argument("--benchmark-log", type=Path)
    post.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    if args.cmd == "preflight":
        result = preflight(
            decision_path=args.decision,
            ledger_path=args.ledger,
            profile_path=args.profile,
            manifest_path=args.manifest,
            dry_run=args.dry_run,
            intent=args.intent,
            requires_human_approval=args.requires_human_approval,
            human_approved=args.human_approved,
            environment_compatible=args.environment_compatible if args.intent == "reproduction" else None,
            require_environment_compatibility=args.require_environment_compatibility,
            require_strict_critic=args.require_strict_critic,
            critic_verdict=args.critic_verdict,
            scheduler_disablelist=args.scheduler_disablelist,
            upstream_policy_preflight_path=args.runtime_policy_preflight,
        )
    else:
        result = postflight(args.status, args.benchmark_log)
    write_json(args.out, result)
    print(args.out)
    return 0 if result["verdict"] == "allow" else 2


if __name__ == "__main__":
    raise SystemExit(main())

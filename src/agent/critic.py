#!/usr/bin/env python3
"""Critic and failure-attribution logic for LockAgent trajectories."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


FAILURE_CATEGORIES = [
    "profile_failure",
    "environment_mismatch",
    "policy_reasoning_failure",
    "skill_misfire",
    "guard_blocked",
    "executor_failure",
    "scheduler_startup_failure",
    "scheduler_regression",
    "benchmark_failure",
    "rollback_failure",
    "comparison_invalid",
]


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else None


def add(categories: list[str], category: str) -> None:
    if category in FAILURE_CATEGORIES and category not in categories:
        categories.append(category)


def infer_failures(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    invalid_reasons: list[str] = []
    ledger = load_json(args.ledger)
    profile = load_json(args.profile)
    decision = load_json(args.decision)
    status = load_json(args.status)
    preflight = load_json(args.preflight) if args.preflight else None
    postflight = load_json(args.postflight) if args.postflight else None
    compatibility = load_json(args.compatibility) if getattr(args, "compatibility", None) else None

    if profile is None:
        add(failures, "profile_failure")
        invalid_reasons.append("missing_profile")
    if ledger is None:
        add(failures, "environment_mismatch")
        invalid_reasons.append("missing_state_ledger")
    elif ledger.get("database", {}).get("collection_status") != "ok":
        add(failures, "environment_mismatch")
        invalid_reasons.append("database_state_unknown")

    if decision is None:
        add(failures, "policy_reasoning_failure")
        invalid_reasons.append("missing_decision")
    else:
        evidence_used = decision.get("evidence_used")
        rollback = decision.get("rollback_conditions")
        if not isinstance(evidence_used, list) or not evidence_used:
            add(failures, "policy_reasoning_failure")
            invalid_reasons.append("decision_missing_evidence")
        if not isinstance(rollback, list) or not rollback:
            add(failures, "policy_reasoning_failure")
            invalid_reasons.append("decision_missing_rollback")

    if preflight and preflight.get("verdict") == "block":
        add(failures, "guard_blocked")
        invalid_reasons.extend(preflight.get("reasons", []))

    if status is None:
        add(failures, "executor_failure")
        invalid_reasons.append("missing_execution_status")
    else:
        executor_status = str(status.get("status", ""))
        executor_failure_class = str(status.get("executor_failure_class") or "")
        execution_was_guard_blocked = executor_status in {"guard_blocked", "paused_for_human_approval"}
        if execution_was_guard_blocked:
            add(failures, "guard_blocked")
            invalid_reasons.append(executor_status)
        elif status.get("returncode") != 0:
            add(failures, "executor_failure")
        if executor_failure_class == "scheduler_startup_failure" or (
            executor_status.startswith("error:")
            and any(token in executor_status for token in ("exited early", "scx binary", "sched_ext did not become"))
        ):
            add(failures, "scheduler_startup_failure")
            invalid_reasons.append("scheduler_startup_failure")
        if executor_failure_class in {"benchmark_failure", "benchmark_timeout"} or executor_status == "benchmark_failed":
            add(failures, "benchmark_failure")
            invalid_reasons.append(executor_failure_class or executor_status)
        if status.get("final_sched_ext_state") != "disabled":
            add(failures, "rollback_failure")
            invalid_reasons.append("sched_ext_not_disabled")

    if postflight and postflight.get("verdict") == "block":
        if "sched_ext_not_disabled" in postflight.get("reasons", []):
            add(failures, "rollback_failure")
        invalid_reasons.extend(postflight.get("reasons", []))

    if compatibility and compatibility.get("verdict") == "incompatible":
        add(failures, "environment_mismatch")
        add(failures, "comparison_invalid")
        invalid_reasons.extend(compatibility.get("reasons", []))

    status_value = str((status or {}).get("status", ""))
    if args.benchmark_log and not args.benchmark_log.exists() and status_value not in {"guard_blocked", "paused_for_human_approval", "not_run"}:
        add(failures, "benchmark_failure")
        invalid_reasons.append("missing_benchmark_log")

    return failures, sorted(set(invalid_reasons))


def build_reports(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    failures, invalid_reasons = infer_failures(args)
    status = load_json(args.status)
    decision = load_json(args.decision)
    valid = not failures and bool(status and status.get("returncode") == 0)
    if decision and decision.get("scheduler") == "default" and status and status.get("returncode") == 0:
        valid = valid and True

    critic_report = {
        "run_id": args.run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "valid": valid,
        "scheduler": decision.get("scheduler") if decision else None,
        "failures": failures,
        "invalid_reasons": invalid_reasons,
        "checks": {
            "has_ledger": args.ledger.exists(),
            "has_profile": args.profile.exists(),
            "has_decision": args.decision.exists(),
            "has_execution_status": args.status.exists(),
            "has_benchmark_log": args.benchmark_log.exists() if args.benchmark_log else None,
        },
    }
    trajectory_status = {
        "run_id": args.run_id,
        "valid": valid,
        "status": "valid" if valid else "invalid",
        "invalid_reasons": invalid_reasons,
        "failure_categories": failures,
        "benchmark_returncode": status.get("returncode") if status else None,
        "final_sched_ext_state": status.get("final_sched_ext_state") if status else None,
        "executor_failure_class": status.get("executor_failure_class") if status else None,
    }
    failure_attribution = {
        "run_id": args.run_id,
        "categories": failures,
        "primary_category": failures[0] if failures else None,
        "invalid_reasons": invalid_reasons,
        "known_categories": FAILURE_CATEGORIES,
    }
    return critic_report, trajectory_status, failure_attribution


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--profile", type=Path, required=True)
    ap.add_argument("--decision", type=Path, required=True)
    ap.add_argument("--status", type=Path, required=True)
    ap.add_argument("--benchmark-log", type=Path, required=True)
    ap.add_argument("--preflight", type=Path)
    ap.add_argument("--postflight", type=Path)
    ap.add_argument("--compatibility", type=Path)
    ap.add_argument("--critic-out", type=Path, required=True)
    ap.add_argument("--trajectory-out", type=Path, required=True)
    ap.add_argument("--failure-out", type=Path, required=True)
    args = ap.parse_args(argv)

    critic_report, trajectory_status, failure_attribution = build_reports(args)
    write_json(args.critic_out, critic_report)
    write_json(args.trajectory_out, trajectory_status)
    write_json(args.failure_out, failure_attribution)
    print(args.critic_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

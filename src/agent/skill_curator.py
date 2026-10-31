#!/usr/bin/env python3
"""Skill lifecycle helpers for LockAgent scheduler skills."""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path
from typing import Any


VALID_STATUSES = {"candidate", "validated", "deprecated", "rejected"}
REQUIRED_FIELDS = {
    "name",
    "version",
    "status",
    "applicability",
    "action",
    "expected_effect",
    "validation_plan",
    "rollback_conditions",
    "known_failures",
    "validated_on",
}


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_skill(skill: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(skill))
    if missing:
        errors.append(f"missing_fields:{','.join(missing)}")
    if skill.get("status") not in VALID_STATUSES:
        errors.append("invalid_status")
    if not isinstance(skill.get("rollback_conditions"), list) or not skill.get("rollback_conditions"):
        errors.append("missing_rollback_conditions")
    action = skill.get("action")
    if not isinstance(action, dict) or not action.get("scheduler"):
        errors.append("missing_action_scheduler")
    return errors


def validate_repository(path: Path) -> dict[str, Any]:
    data = load_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("skills"), list):
        raise ValueError("skill repository must be an object with a skills list")
    results = []
    for skill in data["skills"]:
        if not isinstance(skill, dict):
            results.append({"name": None, "valid": False, "errors": ["skill_not_object"]})
            continue
        errors = validate_skill(skill)
        results.append({"name": skill.get("name"), "valid": not errors, "errors": errors})
    return {"valid": all(r["valid"] for r in results), "skills": results}


def find_skill_for_scheduler(repo: dict[str, Any], scheduler: str | None) -> dict[str, Any] | None:
    if scheduler is None:
        return None
    for skill in repo.get("skills", []):
        if isinstance(skill, dict) and skill.get("action", {}).get("scheduler") == scheduler:
            return skill
    return None


def append_unique(items: list[Any], value: Any) -> None:
    if value not in items:
        items.append(value)


def upsert_by_run_dir(items: list[Any], value: dict[str, Any]) -> None:
    run_dir = value.get("run_dir")
    if not run_dir:
        append_unique(items, value)
        return
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get("run_dir") == run_dir:
            items[index] = {**item, **value}
            return
    items.append(value)


def validation_gate(curation: dict[str, Any]) -> dict[str, Any]:
    validation = curation.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    same_batch = bool(validation.get("same_batch_control"))
    compatible = validation.get("environment_compatible", True) is not False
    metric = validation.get("primary_metric")
    improvement = validation.get("improvement_pct")
    improvement_ok = isinstance(improvement, (int, float)) and improvement > 0
    counterfactual = bool(validation.get("counterfactual_default_checked"))
    passed = bool(same_batch and compatible and counterfactual and (improvement_ok or metric is None))
    missing = []
    if not same_batch:
        missing.append("same_batch_control")
    if not counterfactual:
        missing.append("counterfactual_default_checked")
    if not compatible:
        missing.append("environment_compatible")
    if isinstance(improvement, (int, float)) and improvement <= 0:
        missing.append("positive_improvement")
    return {
        "passed": passed,
        "same_batch_control": same_batch,
        "counterfactual_default_checked": counterfactual,
        "environment_compatible": compatible,
        "primary_metric": metric,
        "improvement_pct": improvement,
        "missing": missing,
    }


def validation_evidence(curation: dict[str, Any]) -> dict[str, Any]:
    gate = validation_gate(curation)
    return {
        "run_dir": curation.get("run_dir"),
        "same_batch_validation_path": curation.get("same_batch_validation_path"),
        "primary_metric": gate.get("primary_metric"),
        "improvement_pct": gate.get("improvement_pct"),
        "environment_compatible": gate.get("environment_compatible"),
        "validation_gate_passed": gate.get("passed"),
        "failure_categories": curation.get("failure_categories", []),
        "verdict": curation.get("verdict"),
        "trajectory_valid": curation.get("trajectory_valid"),
        "reasons": curation.get("reasons", []),
    }


def append_evolution_ledger(repository_path: Path, event: dict[str, Any]) -> Path:
    ledger = repository_path.with_name("skill_evolution_ledger.jsonl")
    record = {"schema_version": "lockagent.skill_evolution.v0.1", "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **event}
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return ledger


def load_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = load_json(path)
    return data if isinstance(data, dict) else None


def collect_run_dirs(root: Path) -> list[Path]:
    if (root / "skill_curation.json").exists():
        return [root]
    return sorted(path for path in root.glob("run_*") if path.is_dir())


def merge_validation_into_curation(curation: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    merged = dict(curation)
    validation_paths = [
        run_dir / "same_batch_validation.json",
        run_dir / "counterfactual_validation" / "same_batch_validation.json",
        run_dir / "counterfactual_executor" / "same_batch_validation.json",
    ]
    validation_doc = None
    for path in validation_paths:
        validation_doc = load_optional_json(path)
        if validation_doc:
            merged["same_batch_validation_path"] = str(path)
            break
    if validation_doc:
        validation = validation_doc.get("validation") if isinstance(validation_doc.get("validation"), dict) else {}
        merged["trajectory_valid"] = bool(validation_doc.get("trajectory_valid"))
        merged["verdict"] = "positive_evidence" if validation_doc.get("trajectory_valid") else "negative_or_inconclusive_evidence"
        merged["reasons"] = validation_doc.get("reasons", merged.get("reasons", []))
        merged["same_batch_comparison_arms"] = validation_doc.get("comparison_arms")
        merged["validation"] = {
            **(merged.get("validation") if isinstance(merged.get("validation"), dict) else {}),
            "same_batch_control": True,
            "counterfactual_default_checked": True,
            "environment_compatible": validation.get("environment_compatible", True),
            "primary_metric": validation.get("primary_metric"),
            "improvement_pct": validation.get("improvement_pct"),
        }
        merged["failure_categories"] = validation_doc.get("failure_categories", merged.get("failure_categories", []))
    merged["run_dir"] = str(run_dir)
    return merged


def apply_evolution(
    *,
    repository_path: Path,
    root: Path,
    out_path: Path,
    promote_after: int,
    reject_after: int,
    dry_run: bool = False,
) -> dict[str, Any]:
    run_dirs = collect_run_dirs(root)
    repo_snapshot = load_json(repository_path) if dry_run else None
    applications: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    transitions: list[dict[str, Any]] = []
    for run_dir in run_dirs:
        curation_path = run_dir / "skill_curation.json"
        curation = load_optional_json(curation_path)
        if not curation:
            skipped.append({"run_dir": str(run_dir), "reason": "missing_skill_curation"})
            continue
        merged = merge_validation_into_curation(curation, run_dir)
        gate = validation_gate(merged)
        if dry_run:
            update = apply_curation_to_repository_data(
                repo=repo_snapshot,
                curation=merged,
                promote_after=promote_after,
                reject_after=reject_after,
            )
            update["dry_run"] = True
        else:
            update = update_repository_with_evidence(
                repository_path=repository_path,
                curation=merged,
                promote_after=promote_after,
                reject_after=reject_after,
            )
        application = {
        "run_dir": str(run_dir),
        "curation": str(curation_path),
        "skill": merged.get("skill"),
        "scheduler": merged.get("scheduler"),
        "verdict": merged.get("verdict"),
        "validation_gate": gate,
        "validation_evidence": validation_evidence(merged),
        "update": update,
    }
        applications.append(application)
        if update.get("transition"):
            transitions.append(
                {
                    "run_dir": str(run_dir),
                    "skill": update.get("skill"),
                    "transition": update.get("transition"),
                    "old_status": update.get("old_status"),
                    "new_status": update.get("new_status"),
                    "primary_metric": gate.get("primary_metric"),
                    "improvement_pct": gate.get("improvement_pct"),
                    "environment_compatible": gate.get("environment_compatible"),
                    "same_batch_validation_path": merged.get("same_batch_validation_path"),
                    "repository_mutated": update.get("repository_mutated", False),
                }
            )
    result = {
        "schema_version": "lockagent.skill_evolution_application.v0.1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repository": str(repository_path),
        "root": str(root),
        "dry_run": dry_run,
        "promote_after": promote_after,
        "reject_after": reject_after,
        "run_count": len(run_dirs),
        "applied_count": len(applications),
        "skipped_count": len(skipped),
        "transition_count": len(transitions),
        "transitions": transitions,
        "applications": applications,
        "skipped": skipped,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def apply_curation_to_repository_data(
    *,
    repo: dict[str, Any] | None,
    curation: dict[str, Any],
    promote_after: int,
    reject_after: int,
) -> dict[str, Any]:
    if not isinstance(repo, dict) or not isinstance(repo.get("skills"), list):
        raise ValueError("skill repository must be an object with a skills list")

    skill_name = curation.get("skill")
    if not skill_name:
        return {"updated": False, "reason": "no_matching_skill"}

    updated_skill: dict[str, Any] | None = None
    for skill in repo["skills"]:
        if isinstance(skill, dict) and skill.get("name") == skill_name:
            updated_skill = skill
            break
    if updated_skill is None:
        return {"updated": False, "reason": "skill_not_found"}

    updated_skill.setdefault("evidence", [])
    updated_skill.setdefault("known_failures", [])
    updated_skill.setdefault("validated_on", [])
    upsert_by_run_dir(updated_skill["evidence"], curation)

    verdict = curation.get("verdict")
    gate = validation_gate(curation)
    if verdict == "positive_evidence":
        upsert_by_run_dir(
            updated_skill["validated_on"],
            {
                "run_dir": curation.get("run_dir"),
                "timestamp": curation.get("timestamp"),
                "scheduler": curation.get("scheduler"),
                "trajectory_valid": curation.get("trajectory_valid"),
                "validation_gate": gate,
                "same_batch_validation_path": curation.get("same_batch_validation_path"),
                "primary_metric": gate.get("primary_metric"),
                "improvement_pct": gate.get("improvement_pct"),
                "environment_compatible": gate.get("environment_compatible"),
                "evidence_source": curation.get("same_batch_validation_path") or curation.get("run_dir"),
            },
        )
    elif verdict in {"candidate_rejected_for_this_context", "environment_invalid", "negative_or_inconclusive_evidence"}:
        upsert_by_run_dir(
            updated_skill["known_failures"],
            {
                "run_dir": curation.get("run_dir"),
                "timestamp": curation.get("timestamp"),
                "failure_categories": curation.get("failure_categories", []),
                "verdict": verdict,
                "scheduler": curation.get("scheduler"),
                "validation_gate": gate,
                "same_batch_validation_path": curation.get("same_batch_validation_path"),
                "primary_metric": gate.get("primary_metric"),
                "improvement_pct": gate.get("improvement_pct"),
                "environment_compatible": gate.get("environment_compatible"),
                "reasons": curation.get("reasons", []),
                "evidence_source": curation.get("same_batch_validation_path") or curation.get("run_dir"),
            },
        )

    positive_count = sum(1 for item in updated_skill.get("evidence", []) if item.get("verdict") == "positive_evidence")
    validated_positive_count = sum(
        1
        for item in updated_skill.get("evidence", [])
        if item.get("verdict") == "positive_evidence" and validation_gate(item).get("passed")
    )
    negative_count = sum(
        1
        for item in updated_skill.get("evidence", [])
        if item.get("verdict") in {"candidate_rejected_for_this_context", "negative_or_inconclusive_evidence"}
        and validation_gate(item).get("environment_compatible")
    )
    environment_invalid_count = sum(
        1
        for item in updated_skill.get("evidence", [])
        if item.get("verdict") == "environment_invalid"
        or validation_gate(item).get("environment_compatible") is False
        or "environment_mismatch" in item.get("failure_categories", [])
        or "comparison_invalid" in item.get("failure_categories", [])
    )
    old_status = updated_skill.get("status")
    transition = None
    blocked_reason = None
    protected_status = old_status == "validated"
    if old_status == "candidate" and positive_count >= promote_after and validated_positive_count < promote_after:
        blocked_reason = "promotion_requires_same_batch_validation"
    if old_status == "candidate" and validated_positive_count >= promote_after:
        updated_skill["status"] = "validated"
        transition = "candidate->validated"
    elif old_status == "candidate" and negative_count >= reject_after:
        updated_skill["status"] = "rejected"
        transition = "candidate->rejected"
    elif protected_status and verdict in {"candidate_rejected_for_this_context", "negative_or_inconclusive_evidence"}:
        blocked_reason = "validated_skill_status_protected_append_known_failure_only"

    return {
        "updated": True,
        "repository_mutated": False,
        "skill": skill_name,
        "old_status": old_status,
        "new_status": updated_skill.get("status"),
        "transition": transition,
        "blocked_reason": blocked_reason,
        "protected_status": protected_status,
        "validation_gate": gate,
        "validation_evidence": validation_evidence(curation),
        "positive_evidence_count": positive_count,
        "validated_positive_evidence_count": validated_positive_count,
        "negative_evidence_count": negative_count,
        "environment_invalid_count": environment_invalid_count,
    }


def update_repository_with_evidence(
    *,
    repository_path: Path,
    curation: dict[str, Any],
    promote_after: int,
    reject_after: int,
) -> dict[str, Any]:
    repo = load_json(repository_path)
    if not isinstance(repo, dict) or not isinstance(repo.get("skills"), list):
        raise ValueError("skill repository must be an object with a skills list")

    backup = repository_path.with_suffix(repository_path.suffix + f".bak.{int(time.time())}")
    shutil.copy2(repository_path, backup)
    update = apply_curation_to_repository_data(
        repo=repo,
        curation=curation,
        promote_after=promote_after,
        reject_after=reject_after,
    )
    if not update.get("updated"):
        update["backup"] = str(backup)
        update["repository_mutated"] = False
        return update

    repository_path.write_text(json.dumps(repo, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    ledger = append_evolution_ledger(
        repository_path,
        {
            "skill": update.get("skill"),
            "old_status": update.get("old_status"),
            "new_status": update.get("new_status"),
            "transition": update.get("transition"),
            "verdict": curation.get("verdict"),
            "validation_gate": update.get("validation_gate"),
            "blocked_reason": update.get("blocked_reason"),
            "protected_status": update.get("protected_status"),
            "same_batch_validation_path": curation.get("same_batch_validation_path"),
            "primary_metric": update.get("validation_gate", {}).get("primary_metric") if isinstance(update.get("validation_gate"), dict) else None,
            "improvement_pct": update.get("validation_gate", {}).get("improvement_pct") if isinstance(update.get("validation_gate"), dict) else None,
            "environment_compatible": update.get("validation_gate", {}).get("environment_compatible") if isinstance(update.get("validation_gate"), dict) else None,
            "positive_evidence_count": update.get("positive_evidence_count"),
            "validated_positive_evidence_count": update.get("validated_positive_evidence_count"),
            "negative_evidence_count": update.get("negative_evidence_count"),
            "environment_invalid_count": update.get("environment_invalid_count"),
            "validation_evidence": update.get("validation_evidence"),
            "run_dir": curation.get("run_dir"),
        },
    )
    update["evolution_ledger"] = str(ledger)
    update["backup"] = str(backup)
    update["repository_mutated"] = True
    return update


def curate_run(
    *,
    repository_path: Path,
    decision_path: Path,
    critic_path: Path,
    failure_path: Path,
    trajectory_path: Path,
    run_dir: Path,
) -> dict[str, Any]:
    repo = load_json(repository_path)
    decision = load_json(decision_path)
    critic = load_json(critic_path)
    failure = load_json(failure_path)
    trajectory = load_json(trajectory_path)
    scheduler = decision.get("scheduler") if isinstance(decision, dict) else None
    skill = find_skill_for_scheduler(repo, scheduler if isinstance(scheduler, str) else None)
    categories = failure.get("categories", []) if isinstance(failure, dict) else []
    valid = bool(trajectory.get("valid")) if isinstance(trajectory, dict) else False

    if skill is None:
        verdict = "no_matching_skill"
        recommendation = "record_only"
    elif valid:
        verdict = "positive_evidence"
        recommendation = "keep_candidate_until_same_batch_validation"
    elif "policy_reasoning_failure" in categories or "skill_misfire" in categories:
        verdict = "candidate_rejected_for_this_context"
        recommendation = "append_negative_example"
    elif "environment_mismatch" in categories or "comparison_invalid" in categories:
        verdict = "environment_invalid"
        recommendation = "do_not_update_skill_performance_claim"
    else:
        verdict = "negative_or_inconclusive_evidence"
        recommendation = "append_known_failure_if_reproduced"

    evidence = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_dir": str(run_dir),
        "scheduler": scheduler,
        "skill": skill.get("name") if skill else None,
        "skill_status": skill.get("status") if skill else None,
        "trajectory_valid": valid,
        "failure_categories": categories,
        "critic_valid": critic.get("valid") if isinstance(critic, dict) else None,
        "verdict": verdict,
        "recommendation": recommendation,
        "validation": {
            "same_batch_control": bool((critic.get("same_batch_control") if isinstance(critic, dict) else False) or (trajectory.get("same_batch_control") if isinstance(trajectory, dict) else False)),
            "counterfactual_default_checked": bool((critic.get("counterfactual_default_checked") if isinstance(critic, dict) else False) or (trajectory.get("counterfactual_default_checked") if isinstance(trajectory, dict) else False)),
            "environment_compatible": not ("environment_mismatch" in categories or "comparison_invalid" in categories),
            "primary_metric": (critic.get("primary_metric") if isinstance(critic, dict) else None),
            "improvement_pct": (critic.get("improvement_pct") if isinstance(critic, dict) else None),
        },
    }
    rejected = {
        "rejected": verdict in {"candidate_rejected_for_this_context", "environment_invalid"},
        "reason": verdict,
        "evidence": evidence,
    }
    return {"skill_curation": evidence, "rejected_hypothesis": rejected}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repository", type=Path)
    ap.add_argument("--out", type=Path)
    sub = ap.add_subparsers(dest="cmd")
    validate = sub.add_parser("validate")
    validate.add_argument("--repository", type=Path, required=True)
    validate.add_argument("--out", type=Path)
    curate = sub.add_parser("curate-run")
    curate.add_argument("--repository", type=Path, required=True)
    curate.add_argument("--decision", type=Path, required=True)
    curate.add_argument("--critic", type=Path, required=True)
    curate.add_argument("--failure", type=Path, required=True)
    curate.add_argument("--trajectory", type=Path, required=True)
    curate.add_argument("--run-dir", type=Path, required=True)
    curate.add_argument("--curation-out", type=Path, required=True)
    curate.add_argument("--rejected-out", type=Path, required=True)
    curate.add_argument("--update-repository", action="store_true")
    curate.add_argument("--promotion-out", type=Path)
    curate.add_argument("--promote-after", type=int, default=2)
    curate.add_argument("--reject-after", type=int, default=2)
    apply = sub.add_parser("apply-evolution")
    apply.add_argument("--repository", type=Path, required=True)
    apply.add_argument("--root", type=Path, required=True)
    apply.add_argument("--out", type=Path, required=True)
    apply.add_argument("--promote-after", type=int, default=2)
    apply.add_argument("--reject-after", type=int, default=2)
    apply.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd in (None, "validate"):
        repository = args.repository
        if repository is None:
            raise ValueError("--repository is required")
        result = validate_repository(repository)
        output = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        return 0 if result["valid"] else 1

    if args.cmd == "apply-evolution":
        result = apply_evolution(
            repository_path=args.repository,
            root=args.root,
            out_path=args.out,
            promote_after=args.promote_after,
            reject_after=args.reject_after,
            dry_run=args.dry_run,
        )
        print(args.out)
        return 0 if result.get("skipped_count", 0) == 0 or result.get("applied_count", 0) > 0 else 1

    result = curate_run(
        repository_path=args.repository,
        decision_path=args.decision,
        critic_path=args.critic,
        failure_path=args.failure,
        trajectory_path=args.trajectory,
        run_dir=args.run_dir,
    )
    args.curation_out.parent.mkdir(parents=True, exist_ok=True)
    args.curation_out.write_text(
        json.dumps(result["skill_curation"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    args.rejected_out.parent.mkdir(parents=True, exist_ok=True)
    args.rejected_out.write_text(
        json.dumps(result["rejected_hypothesis"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.update_repository:
        promotion = update_repository_with_evidence(
            repository_path=args.repository,
            curation=result["skill_curation"],
            promote_after=args.promote_after,
            reject_after=args.reject_after,
        )
        promotion_out = args.promotion_out or (args.run_dir / "skill_repository_update.json")
        promotion_out.parent.mkdir(parents=True, exist_ok=True)
        promotion_out.write_text(json.dumps(promotion, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.curation_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

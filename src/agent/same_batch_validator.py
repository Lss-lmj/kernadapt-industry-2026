#!/usr/bin/env python3
"""Turn same-batch control/treatment results into skill validation evidence."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lockagent.same_batch_validation.v0.1"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def find_arm(summary: dict[str, Any], arm: str) -> dict[str, Any] | None:
    for item in summary.get("aggregate", []):
        if isinstance(item, dict) and item.get("arm") == arm:
            return item
    return None


def comparison_arms(summary: dict[str, Any], *, control_arm: str, treatment_arm: str) -> dict[str, Any]:
    aggregate = [item for item in summary.get("aggregate", []) if isinstance(item, dict)]

    def role_for(arm: str) -> str:
        if arm == control_arm:
            return "default_control"
        if arm == treatment_arm:
            return "fixed_scx_treatment"
        if arm == "kernadapt_online":
            return "kernadapt_online"
        if arm == "lockagent_online":
            # Keep the legacy label when validating older batch summaries.
            return "lockagent_online"
        if arm in {"oracle_best", "oracle-best", "oracle"}:
            return "oracle_best_evaluation_only"
        return "additional_observation"

    arms = {
        str(item.get("arm")): {
            "role": role_for(str(item.get("arm"))),
            "scheduler_values": item.get("scheduler_values"),
            "n_total": item.get("n_total"),
            "n_valid": item.get("n_valid"),
            "tps_mean": item.get("tps_mean"),
            "lat_p95_ms_mean": item.get("lat_p95_ms_mean"),
        }
        for item in aggregate
        if item.get("arm")
    }
    return {
        "schema_version": "lockagent.same_batch_arms.v0.1",
        "required_roles": ["default_control", "fixed_scx_treatment"],
        "optional_roles": ["kernadapt_online", "lockagent_online", "oracle_best_evaluation_only"],
        "control_arm": control_arm,
        "treatment_arm": treatment_arm,
        "oracle_best_is_evaluation_only": True,
        "arms": arms,
    }


def valid_number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def build_validation(
    *,
    summary_path: Path,
    skill: str,
    scheduler: str,
    control_arm: str,
    treatment_arm: str,
    min_improvement_pct: float,
    max_p95_regression_pct: float,
) -> dict[str, Any]:
    summary = load_json(summary_path)
    control = find_arm(summary, control_arm)
    treatment = find_arm(summary, treatment_arm)
    arms = comparison_arms(summary, control_arm=control_arm, treatment_arm=treatment_arm)
    reasons: list[str] = []
    structural_reasons: list[str] = []
    if control is None:
        reason = f"missing_control_arm:{control_arm}"
        reasons.append(reason)
        structural_reasons.append(reason)
    if treatment is None:
        reason = f"missing_treatment_arm:{treatment_arm}"
        reasons.append(reason)
        structural_reasons.append(reason)
    control_valid = int((control or {}).get("n_valid") or 0)
    treatment_valid = int((treatment or {}).get("n_valid") or 0)
    if control_valid <= 0:
        reasons.append("control_has_no_valid_trials")
        structural_reasons.append("control_has_no_valid_trials")
    if treatment_valid <= 0:
        reasons.append("treatment_has_no_valid_trials")
        structural_reasons.append("treatment_has_no_valid_trials")

    control_tps = valid_number((control or {}).get("tps_mean"))
    treatment_tps = valid_number((treatment or {}).get("tps_mean"))
    control_p95 = valid_number((control or {}).get("lat_p95_ms_mean"))
    treatment_p95 = valid_number((treatment or {}).get("lat_p95_ms_mean"))
    if control_tps is None or control_tps <= 0:
        reasons.append("missing_control_tps")
        structural_reasons.append("missing_control_tps")
    if treatment_tps is None:
        reasons.append("missing_treatment_tps")
        structural_reasons.append("missing_treatment_tps")

    improvement_pct = None
    p95_delta_pct = None
    if control_tps and treatment_tps is not None:
        improvement_pct = ((treatment_tps - control_tps) / control_tps) * 100.0
        if improvement_pct < min_improvement_pct:
            reasons.append("insufficient_tps_improvement")
    if control_p95 and treatment_p95 is not None:
        p95_delta_pct = ((treatment_p95 - control_p95) / control_p95) * 100.0
        if p95_delta_pct > max_p95_regression_pct:
            reasons.append("p95_regression_too_large")

    passed = not reasons
    comparison_invalid = bool(structural_reasons)
    performance_misfire = bool(reasons) and not comparison_invalid
    validation = {
        "same_batch_control": True,
        "counterfactual_default_checked": True,
        "environment_compatible": not comparison_invalid,
        "oracle_best_is_evaluation_only": True,
        "primary_metric": "tps",
        "improvement_pct": improvement_pct,
        "p95_delta_pct": p95_delta_pct,
        "control_arm": control_arm,
        "treatment_arm": treatment_arm,
        "control_n_valid": control_valid,
        "treatment_n_valid": treatment_valid,
        "min_improvement_pct": min_improvement_pct,
        "max_p95_regression_pct": max_p95_regression_pct,
    }
    if passed:
        verdict = "positive_evidence"
        failure_categories: list[str] = []
        recommendation = "promote_or_keep_validated"
    elif comparison_invalid:
        verdict = "negative_or_inconclusive_evidence"
        failure_categories = ["comparison_invalid", "environment_mismatch"]
        recommendation = "record_environment_failure"
    elif performance_misfire:
        verdict = "candidate_rejected_for_this_context"
        failure_categories = ["skill_misfire"]
        recommendation = "append_negative_example"
    else:
        verdict = "negative_or_inconclusive_evidence"
        failure_categories = ["comparison_invalid"]
        recommendation = "record_only"
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "summary_path": str(summary_path),
        "batch_id": summary.get("batch_id"),
        "skill": skill,
        "scheduler": scheduler,
        "comparison_arms": arms,
        "trajectory_valid": passed,
        "critic_valid": passed,
        "failure_categories": failure_categories,
        "verdict": verdict,
        "recommendation": recommendation,
        "validation": validation,
        "reasons": reasons,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--summary", type=Path, required=True)
    ap.add_argument("--skill", required=True)
    ap.add_argument("--scheduler", required=True)
    ap.add_argument("--control-arm", default="default")
    ap.add_argument("--treatment-arm", default="fixed_locknest")
    ap.add_argument("--min-improvement-pct", type=float, default=1.0)
    ap.add_argument("--max-p95-regression-pct", type=float, default=5.0)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    result = build_validation(
        summary_path=args.summary,
        skill=args.skill,
        scheduler=args.scheduler,
        control_arm=args.control_arm,
        treatment_arm=args.treatment_arm,
        min_improvement_pct=args.min_improvement_pct,
        max_p95_regression_pct=args.max_p95_regression_pct,
    )
    write_json(args.out, result)
    print(args.out)
    return 0 if result["trajectory_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Execute or materialize counterfactual same-batch validation plans."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = Path(__file__).resolve().parent
KERNADAPT_ROOT = AGENT_DIR.parent
ANALYSIS_DIR = KERNADAPT_ROOT / "analysis"
SKILL_REPO = KERNADAPT_ROOT / "skills" / "scheduler_skill_cards.json"
SCHEMA_VERSION = "lockagent.counterfactual_executor.v0.1"


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cmd: list[str], *, cwd: Path = REPO_ROOT, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        return {"cmd": cmd, "returncode": 0, "dry_run": True}
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    return {"cmd": cmd, "returncode": proc.returncode, "stdout_tail": proc.stdout[-4000:], "dry_run": False}


def mock_batch_summary(out_dir: Path, *, treatment_arm: str, treatment_scheduler: str) -> Path:
    summary = {
        "batch_id": "dry_run_counterfactual",
        "aggregate": [
            {
                "arm": "default",
                "n_total": 1,
                "n_valid": 1,
                "scheduler_values": "default",
                "tps_mean": 100.0,
                "lat_p95_ms_mean": 10.0,
            },
            {
                "arm": treatment_arm,
                "n_total": 1,
                "n_valid": 1,
                "scheduler_values": treatment_scheduler,
                "tps_mean": 120.0,
                "lat_p95_ms_mean": 9.5,
            },
        ],
        "trials": [],
    }
    path = out_dir / "batch_summary.json"
    write_json(path, summary)
    return path


def execute_plan(
    *,
    plan_path: Path,
    out_dir: Path,
    skill: str,
    scheduler: str,
    threads: int,
    run_s: int,
    profile_s: int,
    reps: int,
    treatment_arm: str,
    min_improvement_pct: float,
    max_p95_regression_pct: float,
    update_repository: bool,
    repository: Path,
    promote_after: int,
    reject_after: int,
    dry_run: bool,
) -> dict[str, Any]:
    plan = load_json(plan_path)
    out_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    if not plan.get("requires_counterfactual"):
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "skipped",
            "reason": "plan_does_not_require_counterfactual",
            "plan": str(plan_path),
        }
        write_json(out_dir / "counterfactual_execution.json", result)
        return result

    if dry_run:
        summary_path = mock_batch_summary(out_dir, treatment_arm=treatment_arm, treatment_scheduler=scheduler)
        events.append({"stage": "mock_batch_summary", "returncode": 0, "summary": str(summary_path)})
    else:
        comparison_root = out_dir / "comparison"
        cmd = [
            sys.executable,
            str(AGENT_DIR / "run_mariadb_comparison.py"),
            "--threads",
            str(threads),
            "--run-s",
            str(run_s),
            "--profile-s",
            str(profile_s),
            "--reps",
            str(reps),
            "--out",
            str(comparison_root),
            "--arms",
            "default",
            "fixed_locknest",
            "--fixed-scheduler",
            scheduler,
        ]
        comparison = run(cmd)
        events.append({"stage": "run_mariadb_comparison", **comparison})
        if comparison["returncode"] != 0:
            result = {"schema_version": SCHEMA_VERSION, "status": "failed", "events": events, "plan": str(plan_path)}
            write_json(out_dir / "counterfactual_execution.json", result)
            return result
        batch_dirs = sorted(comparison_root.glob("batch_*"))
        if not batch_dirs:
            raise FileNotFoundError(f"no batch directory under {comparison_root}")
        batch_dir = batch_dirs[-1]
        summary_cmd = [sys.executable, str(ANALYSIS_DIR / "batch_summary.py"), "--batch", str(batch_dir)]
        summary = run(summary_cmd)
        events.append({"stage": "batch_summary", **summary})
        summary_path = batch_dir / "batch_summary.json"

    validation_path = out_dir / "same_batch_validation.json"
    validator_cmd = [
        sys.executable,
        str(AGENT_DIR / "same_batch_validator.py"),
        "--summary",
        str(summary_path),
        "--skill",
        skill,
        "--scheduler",
        scheduler,
        "--control-arm",
        "default",
        "--treatment-arm",
        treatment_arm,
        "--min-improvement-pct",
        str(min_improvement_pct),
        "--max-p95-regression-pct",
        str(max_p95_regression_pct),
        "--out",
        str(validation_path),
    ]
    validation = run(validator_cmd, dry_run=False)
    events.append({"stage": "same_batch_validator", **validation})

    update_path = out_dir / "skill_repository_update.json"
    if update_repository:
        update_cmd = [
            sys.executable,
            str(AGENT_DIR / "skill_curator.py"),
            "curate-run",
            "--repository",
            str(repository),
            "--decision",
            str(out_dir / "decision_from_validation.json"),
            "--critic",
            str(out_dir / "critic_from_validation.json"),
            "--failure",
            str(out_dir / "failure_from_validation.json"),
            "--trajectory",
            str(out_dir / "trajectory_from_validation.json"),
            "--run-dir",
            str(out_dir),
            "--curation-out",
            str(out_dir / "skill_curation.json"),
            "--rejected-out",
            str(out_dir / "rejected_hypothesis.json"),
            "--update-repository",
            "--promotion-out",
            str(update_path),
            "--promote-after",
            str(promote_after),
            "--reject-after",
            str(reject_after),
        ]
        validation_doc = load_json(validation_path)
        write_json(out_dir / "decision_from_validation.json", {"scheduler": scheduler})
        write_json(
            out_dir / "critic_from_validation.json",
            {
                "valid": validation_doc.get("trajectory_valid"),
                **(validation_doc.get("validation") if isinstance(validation_doc.get("validation"), dict) else {}),
            },
        )
        write_json(out_dir / "failure_from_validation.json", {"categories": validation_doc.get("failure_categories", [])})
        write_json(
            out_dir / "trajectory_from_validation.json",
            {
                "valid": validation_doc.get("trajectory_valid"),
                **(validation_doc.get("validation") if isinstance(validation_doc.get("validation"), dict) else {}),
            },
        )
        update = run(update_cmd, dry_run=False)
        events.append({"stage": "skill_curator_update", **update})

    result = {
        "schema_version": SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "status": "ok" if load_json(validation_path).get("trajectory_valid") else "validation_failed",
        "plan": str(plan_path),
        "summary": str(summary_path),
        "validation": str(validation_path),
        "skill_repository_update": str(update_path) if update_repository else None,
        "events": events,
        "dry_run": dry_run,
    }
    write_json(out_dir / "counterfactual_execution.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--skill", default="choose_locknest_for_hot_lock_oltp")
    ap.add_argument("--scheduler", default="scx_lockcoop")
    ap.add_argument("--threads", type=int, default=64)
    ap.add_argument("--run-s", type=int, default=5)
    ap.add_argument("--profile-s", type=int, default=2)
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--treatment-arm", default="fixed_locknest")
    ap.add_argument("--min-improvement-pct", type=float, default=1.0)
    ap.add_argument("--max-p95-regression-pct", type=float, default=5.0)
    ap.add_argument("--update-repository", action="store_true")
    ap.add_argument("--repository", type=Path, default=SKILL_REPO)
    ap.add_argument("--promote-after", type=int, default=1)
    ap.add_argument("--reject-after", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    result = execute_plan(
        plan_path=args.plan,
        out_dir=args.out,
        skill=args.skill,
        scheduler=args.scheduler,
        threads=args.threads,
        run_s=args.run_s,
        profile_s=args.profile_s,
        reps=args.reps,
        treatment_arm=args.treatment_arm,
        min_improvement_pct=args.min_improvement_pct,
        max_p95_regression_pct=args.max_p95_regression_pct,
        update_repository=args.update_repository,
        repository=args.repository,
        promote_after=args.promote_after,
        reject_after=args.reject_after,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in {"ok", "skipped"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

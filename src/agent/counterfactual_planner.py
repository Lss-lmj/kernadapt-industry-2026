#!/usr/bin/env python3
"""Plan counterfactual control runs from causal attribution reports."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from agent_runtime import AgentRun, CAP_CURATE_SKILL, ROLE_CURATOR


SCHEMA_VERSION = "lockagent.counterfactual_plan.v0.1"
AGENT_DIR = Path(__file__).resolve().parent


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def build_plan(
    *,
    causal_path: Path,
    run_dir: Path | None,
    out_root: Path | None,
    plan_path: Path | None = None,
    executor_out: Path | None = None,
    skill: str = "choose_locknest_for_hot_lock_oltp",
    scheduler: str = "scx_lockcoop",
    dry_run_executor: bool = True,
    update_repository: bool = False,
    repository: Path | None = None,
    promote_after: int = 1,
    reject_after: int = 1,
) -> dict[str, Any]:
    causal = load_json(causal_path)
    run_id = causal.get("run_id")
    chains = causal.get("causal_chains", [])
    tasks: list[dict[str, Any]] = []
    executable_tasks: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []
    for index, chain in enumerate(chains):
        if not isinstance(chain, dict):
            continue
        status = chain.get("counterfactual_status", {})
        if not isinstance(status, dict) or not status.get("same_batch_required"):
            continue
        category = chain.get("category")
        base_payload = {
            "causal_chain_index": index,
            "category": category,
            "causal_claim": chain.get("causal_claim"),
            "source_run_dir": str(run_dir) if run_dir else None,
        }
        checks.append(
            {
                "check_id": f"counterfactual.{index}.{category}",
                "category": category,
                "reason": "same_batch_required",
                "chain_path": chain.get("path", []),
                "status": "planned",
            }
        )
        tasks.append(
            {
                "task_id": f"{run_id}:counterfactual:{index}:default",
                "role": "ExecutorAgent",
                "step_name": "counterfactual_default_control",
                "priority": 50,
                "payload": {
                    **base_payload,
                    "scheduler": "default",
                    "arm": "default",
                    "command": [],
                    "note": "Fill command from workload-specific comparison runner before execution.",
                },
            }
        )
        tasks.append(
            {
                "task_id": f"{run_id}:counterfactual:{index}:treatment",
                "role": "ExecutorAgent",
                "step_name": "counterfactual_treatment",
                "priority": 51,
                "payload": {
                    **base_payload,
                    "scheduler": "selected",
                    "arm": "treatment",
                    "command": [],
                    "note": "Fill command from workload-specific comparison runner before execution.",
                },
            }
        )
        executor_out_dir = executor_out or ((out_root or causal_path.parent) / "counterfactual_executor")
        command = [
            sys.executable,
            str(AGENT_DIR / "counterfactual_executor.py"),
            "--plan",
            str(plan_path or (causal_path.parent / "counterfactual_plan.json")),
            "--out",
            str(executor_out_dir),
            "--skill",
            skill,
            "--scheduler",
            scheduler,
        ]
        if dry_run_executor:
            command.append("--dry-run")
        if update_repository:
            command.extend(
                [
                    "--update-repository",
                    "--repository",
                    str(repository) if repository else str((AGENT_DIR.parent / "skills" / "scheduler_skill_cards.json")),
                    "--promote-after",
                    str(promote_after),
                    "--reject-after",
                    str(reject_after),
                ]
            )
        executable_tasks.append(
            {
                "task_id": f"{run_id}:counterfactual:{index}:execute_validation",
                "role": ROLE_CURATOR,
                "step_name": "counterfactual_execute_validation",
                "priority": 49,
                "payload": {
                    **base_payload,
                    "command": command,
                    "required_capabilities": [CAP_CURATE_SKILL],
                    "input_artifacts": [str(causal_path)],
                    "output_artifacts": [
                        str(executor_out_dir / "counterfactual_execution.json"),
                        str(executor_out_dir / "same_batch_validation.json"),
                        str(executor_out_dir / "skill_repository_update.json"),
                    ],
                    "skill_evolution": {
                        "update_repository": update_repository,
                        "repository": str(repository) if repository else None,
                        "promote_after": promote_after,
                        "reject_after": reject_after,
                    },
                    "note": "Executable validation task generated from causal counterfactual requirement.",
                },
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "run_id": run_id,
        "causal_attribution": str(causal_path),
        "run_dir": str(run_dir) if run_dir else None,
        "out_root": str(out_root) if out_root else None,
        "requires_counterfactual": bool(tasks),
        "checks": checks,
        "tasks": tasks,
        "executable_tasks": executable_tasks,
        "skill_evolution": {
            "update_repository": update_repository,
            "repository": str(repository) if repository else None,
            "promote_after": promote_after,
            "reject_after": reject_after,
        },
        "task_count": len(tasks),
        "executable_task_count": len(executable_tasks),
    }


def submit_executable_tasks(checkpoint: Path, plan: dict[str, Any]) -> dict[str, Any]:
    run = AgentRun.load(checkpoint)
    submitted = []
    for task in plan.get("executable_tasks", []):
        if not isinstance(task, dict):
            continue
        submitted_task = run.submit_task(
            task_id=str(task["task_id"]),
            role=str(task["role"]),
            step_name=str(task["step_name"]),
            payload=task.get("payload") if isinstance(task.get("payload"), dict) else {},
            priority=int(task.get("priority", 100)),
        )
        submitted.append(submitted_task.to_dict())
    run.checkpoint()
    return {
        "schema_version": SCHEMA_VERSION,
        "checkpoint": str(checkpoint),
        "submitted_count": len(submitted),
        "submitted_tasks": submitted,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--causal", type=Path, required=True)
    ap.add_argument("--run-dir", type=Path)
    ap.add_argument("--out-root", type=Path)
    ap.add_argument("--executor-out", type=Path)
    ap.add_argument("--skill", default="choose_locknest_for_hot_lock_oltp")
    ap.add_argument("--scheduler", default="scx_lockcoop")
    ap.add_argument("--real-executor", action="store_true")
    ap.add_argument("--update-repository", action="store_true")
    ap.add_argument("--repository", type=Path)
    ap.add_argument("--promote-after", type=int, default=1)
    ap.add_argument("--reject-after", type=int, default=1)
    ap.add_argument("--checkpoint", type=Path)
    ap.add_argument("--submit-tasks", action="store_true")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    plan = build_plan(
        causal_path=args.causal,
        run_dir=args.run_dir,
        out_root=args.out_root,
        plan_path=args.out,
        executor_out=args.executor_out,
        skill=args.skill,
        scheduler=args.scheduler,
        dry_run_executor=not args.real_executor,
        update_repository=args.update_repository,
        repository=args.repository,
        promote_after=args.promote_after,
        reject_after=args.reject_after,
    )
    if args.submit_tasks:
        if args.checkpoint is None:
            raise ValueError("--checkpoint is required with --submit-tasks")
        plan["submission"] = submit_executable_tasks(args.checkpoint, plan)
    write_json(args.out, plan)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

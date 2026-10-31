#!/usr/bin/env python3
"""Local checks for counterfactual execution materialization."""

from __future__ import annotations

import importlib.util
import json
import shutil
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
AGENT_ROOT = KERNADAPT_ROOT / "agent"
MODULE_PATH = AGENT_ROOT / "counterfactual_executor.py"
REPO = KERNADAPT_ROOT / "skills" / "scheduler_skill_cards.json"
sys.path.insert(0, str(AGENT_ROOT))
spec = importlib.util.spec_from_file_location("counterfactual_executor", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["counterfactual_executor"] = module
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        plan = base / "counterfactual_plan.json"
        out = base / "exec"
        repo_copy = base / "skills.json"
        shutil.copy2(REPO, repo_copy)
        write(
            plan,
            {
                "requires_counterfactual": True,
                "tasks": [
                    {"task_id": "run:counterfactual:0:default"},
                    {"task_id": "run:counterfactual:0:treatment"},
                ],
            },
        )
        result = module.execute_plan(
            plan_path=plan,
            out_dir=out,
            skill="choose_locknest_for_hot_lock_oltp",
            scheduler="scx_lockcoop",
            threads=64,
            run_s=1,
            profile_s=1,
            reps=1,
            treatment_arm="fixed_locknest",
            min_improvement_pct=1.0,
            max_p95_regression_pct=5.0,
            update_repository=True,
            repository=repo_copy,
            promote_after=1,
            reject_after=99,
            dry_run=True,
        )
        assert result["status"] == "ok", result
        validation = json.loads((out / "same_batch_validation.json").read_text(encoding="utf-8"))
        assert validation["trajectory_valid"] is True, validation
        update = json.loads((out / "skill_repository_update.json").read_text(encoding="utf-8"))
        assert update["transition"] == "candidate->validated", update
        assert update["validation_gate"]["passed"] is True, update
        execution = json.loads((out / "counterfactual_execution.json").read_text(encoding="utf-8"))
        assert execution["dry_run"] is True, execution
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

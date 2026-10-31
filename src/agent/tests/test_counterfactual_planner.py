#!/usr/bin/env python3
"""Local checks for causal counterfactual planning."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "counterfactual_planner.py"
spec = importlib.util.spec_from_file_location("counterfactual_planner", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["counterfactual_planner"] = module
spec.loader.exec_module(module)
import agent_runtime as runtime


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        causal = base / "causal.json"
        causal.write_text(
            json.dumps(
                {
                    "run_id": "run_test",
                    "causal_chains": [
                        {
                            "category": "skill_misfire",
                            "path": ["profile.signals", "skill.x", "decision.scheduler", "outcome.executor_status", "failure.skill_misfire"],
                            "causal_claim": "profile.signals -> skill.x -> decision.scheduler -> outcome.executor_status",
                            "counterfactual_status": {"same_batch_required": True},
                        },
                        {
                            "category": "environment_mismatch",
                            "path": ["ledger.environment", "failure.environment_mismatch"],
                            "counterfactual_status": {"same_batch_required": False},
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        plan_path = base / "counterfactual_plan.json"
        repo = base / "skills.json"
        plan = module.build_plan(
            causal_path=causal,
            run_dir=base / "run_test",
            out_root=base / "out",
            plan_path=plan_path,
            update_repository=True,
            repository=repo,
            promote_after=2,
            reject_after=3,
        )
        assert plan["requires_counterfactual"] is True, plan
        assert plan["task_count"] == 2, plan
        assert plan["executable_task_count"] == 1, plan
        assert plan["tasks"][0]["payload"]["arm"] == "default", plan
        assert plan["tasks"][1]["payload"]["arm"] == "treatment", plan
        command = plan["executable_tasks"][0]["payload"]["command"]
        assert any(item.endswith("counterfactual_executor.py") for item in command), plan
        assert str(plan_path) in command, plan
        assert "--update-repository" in command, plan
        assert str(repo) in command, plan
        assert "--promote-after" in command and "2" in command, plan
        assert plan["skill_evolution"]["update_repository"] is True, plan
        assert plan["executable_tasks"][0]["payload"]["skill_evolution"]["reject_after"] == 3, plan
        assert any(path.endswith("skill_repository_update.json") for path in plan["executable_tasks"][0]["payload"]["output_artifacts"]), plan
        assert plan["checks"][0]["category"] == "skill_misfire", plan
        run = runtime.AgentRun(
            run_id="run_test",
            intent="online",
            artifact_root=base / "run_test",
            environment_fingerprint={"kernel": "test"},
        )
        submitted = module.submit_executable_tasks(run.checkpoint_path, plan)
        assert submitted["submitted_count"] == 1, submitted
        loaded = runtime.AgentRun.load(run.checkpoint_path)
        assert any(task.task_id.endswith("execute_validation") for task in loaded.scheduler._queue), [task.to_dict() for task in loaded.scheduler._queue]
        tasks = loaded.durable.list_tasks("run_test")
        assert any(task["step_name"] == "counterfactual_execute_validation" for task in tasks), tasks
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Tests for real-agent instance planning."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PLAN = HERE / "plan_real_agent_instances.py"


class PlanRealAgentInstancesTests(unittest.TestCase):
    def run_plan(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(PLAN), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return proc

    def test_jsonl_selects_diverse_three_instance_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            tasks = tmp / "tasks.jsonl"
            rows = [
                {"instance_id": "django__django-1", "repo": "django/django", "FAIL_TO_PASS": ["a"]},
                {"instance_id": "django__django-2", "repo": "django/django", "FAIL_TO_PASS": ["a", "b"]},
                {"instance_id": "sympy__sympy-1", "repo": "sympy/sympy", "FAIL_TO_PASS": ["a"]},
                {"instance_id": "astropy__astropy-1", "repo": "astropy/astropy", "PASS_TO_PASS": ["p"]},
            ]
            tasks.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            output = tmp / "plan.json"
            markdown = tmp / "plan.md"
            script = tmp / "capture.sh"

            self.run_plan(
                "--task-list",
                str(tasks),
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
                "--script-output",
                str(script),
                "--capture-dir",
                "experiments/results/real-agent-captures",
                "--repo-root",
                "benchmarks/harness-replaybench/repos",
                "--raw-output-template",
                "runs/{instance_id}.traj",
                "--endpoint-template",
                "runs/{instance_id}.endpoint.json",
                "--harness-command-template",
                "sweagent run --instance-id {instance_id}",
            )

            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "pass", plan["blockers"])
            self.assertEqual(plan["summary"]["selected_instances"], 3)
            self.assertEqual(plan["summary"]["selected_unique_repos"], 3)
            selected_repos = {row["repo"] for row in plan["selected"]}
            self.assertEqual(selected_repos, {"django/django", "sympy/sympy", "astropy/astropy"})
            first_cmd = plan["selected"][0]["capture_command"]
            self.assertIn("--", first_cmd)
            self.assertIn("sweagent", first_cmd)
            self.assertTrue(script.exists())
            self.assertIn("capture_real_agent_run.py", script.read_text(encoding="utf-8"))
            self.assertIn("FlowCausal real-agent three-instance plan", markdown.read_text(encoding="utf-8"))

    def test_missing_fields_are_reported_but_valid_rows_are_selected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            tasks = tmp / "tasks.json"
            tasks.write_text(
                json.dumps(
                    {
                        "instances": [
                            {"instance_id": "missingrepo__1"},
                            {"repo": "missing/id"},
                            {"task_id": "django__django-1", "repository": "django/django"},
                            {"task_id": "sympy__sympy-1", "repository": "sympy/sympy"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = tmp / "plan.json"

            self.run_plan("--task-list", str(tasks), "--num-instances", "2", "--output", str(output))

            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "pass")
            self.assertEqual(plan["summary"]["eligible_tasks"], 2)
            self.assertTrue(any("missing repo" in warning for warning in plan["warnings"]))
            self.assertTrue(any("missing instance_id" in warning for warning in plan["warnings"]))

    def test_insufficient_candidates_returns_partial_not_fake_instances(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            tasks = tmp / "tasks.json"
            tasks.write_text(json.dumps([{"instance_id": "django__django-1", "repo": "django/django"}]), encoding="utf-8")
            output = tmp / "plan.json"

            self.run_plan("--task-list", str(tasks), "--num-instances", "3", "--output", str(output))

            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "partial")
            self.assertEqual(plan["summary"]["selected_instances"], 1)
            self.assertIn("selected instances 1 < requested 3", plan["blockers"])

    def test_mini_swebench_mode_generates_current_harness_capture_script(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            tasks = tmp / "tasks.jsonl"
            tasks.write_text(
                json.dumps({"instance_id": "django__django-14608", "repo": "django/django"}) + "\n",
                encoding="utf-8",
            )
            output = tmp / "plan.mini.json"
            script = tmp / "capture-selected.mini.sh"
            mini_extra = "/abs/current-harness/.venv/bin/mini-extra"
            env_script = "/abs/current-harness/env.sh"

            self.run_plan(
                "--task-list",
                str(tasks),
                "--num-instances",
                "1",
                "--output",
                str(output),
                "--script-output",
                str(script),
                "--capture-dir",
                "flowcausal/experiments/results/real-agent-captures",
                "--repo-root",
                "flowcausal/benchmarks/harness-replaybench/repos",
                "--source",
                "mini-swe-agent",
                "--mini-swebench",
                "--mini-extra",
                mini_extra,
                "--mini-yolo",
                "--env-script",
                env_script,
                "--require-any-env",
                "OPENAI_API_KEY",
                "--require-any-env",
                "ANTHROPIC_API_KEY",
            )

            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["status"], "pass", plan["blockers"])
            self.assertEqual(plan["run_environment"]["mode"], "mini-swe-agent")
            cmd = plan["selected"][0]["capture_command"]
            self.assertIn(str(HERE / "run_mini_swebench_single_capture.py"), cmd)
            self.assertIn("--trajectory", cmd)
            self.assertIn("flowcausal/experiments/results/real-agent-captures/runs/django__django-14608.traj", cmd)
            self.assertIn("--endpoint", cmd)
            self.assertIn("flowcausal/experiments/results/real-agent-captures/runs/django__django-14608.endpoint.json", cmd)
            self.assertIn("--mini-extra", cmd)
            self.assertIn(mini_extra, cmd)
            self.assertIn("--yolo", cmd)
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(f"source {env_script}", script_text)
            self.assertIn("set at least one API key before capture: OPENAI_API_KEY ANTHROPIC_API_KEY", script_text)
            self.assertIn("mkdir -p flowcausal/experiments/results/real-agent-captures/runs", script_text)
            self.assertNotIn("real-agent-harness/.venv/bin/mini-extra", script_text)


if __name__ == "__main__":
    unittest.main()

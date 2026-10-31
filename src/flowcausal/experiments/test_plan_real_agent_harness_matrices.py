#!/usr/bin/env python3
"""Tests for generating full-harness matrix plans from real-agent plans."""

from __future__ import annotations

import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PLANNER = HERE / "plan_real_agent_harness_matrices.py"


def write_executable(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def instance_plan(root: pathlib.Path, *, mini_extra: pathlib.Path) -> dict:
    rows = []
    for name in ("django__django-14608", "sympy__sympy-21171", "matplotlib__matplotlib-23314"):
        repo = root / "repos" / name
        repo.mkdir(parents=True, exist_ok=True)
        raw = root / "captures" / "runs" / f"{name}.traj"
        endpoint = root / "captures" / "runs" / f"{name}.endpoint.json"
        rows.append(
            {
                "instance_id": name,
                "repo_dir": str(repo),
                "capture_command": [
                    sys.executable,
                    str(HERE / "capture_real_agent_run.py"),
                    "--instance-id",
                    name,
                    "--repo-dir",
                    str(repo),
                    "--raw-agent-output",
                    str(raw),
                    "--endpoint-result",
                    str(endpoint),
                    "--",
                    sys.executable,
                    str(HERE / "run_mini_swebench_single_capture.py"),
                    "--instance-id",
                    name,
                    "--trajectory",
                    str(raw),
                    "--endpoint",
                    str(endpoint),
                    "--subset",
                    "lite",
                    "--split",
                    "test",
                    "--cost-limit",
                    "1.5",
                    "--mini-extra",
                    str(mini_extra),
                    "--yolo",
                ],
            }
        )
    return {
        "schema": "flowcausal.real_agent_instance_plan.v1",
        "selected": rows,
        "run_environment": {
            "env_script": str(root / "harness" / "env.sh"),
            "mode": "mini-swe-agent",
            "require_any_env": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        },
    }


class PlanRealAgentHarnessMatricesTests(unittest.TestCase):
    def run_planner(self, *args: str, expect_ok: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PLANNER), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return json.loads(proc.stdout)

    def test_generates_three_instance_full_harness_matrix_runbook(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            mini_extra = root / "harness" / ".venv" / "bin" / "mini-extra"
            env_script = root / "harness" / "env.sh"
            write_executable(mini_extra, "#!/bin/sh\necho mini-extra\n")
            write_executable(env_script, "#!/usr/bin/env bash\ntrue\n")
            plan = root / "instance-plan.mini.json"
            output = root / "harness-matrix-plan.json"
            markdown = root / "harness-matrix-plan.md"
            script = root / "run-harness-matrices.sh"
            plan.write_text(json.dumps(instance_plan(root, mini_extra=mini_extra)), encoding="utf-8")

            self.run_planner(
                "--instance-plan",
                str(plan),
                "--root",
                str(root),
                "--matrix-root",
                str(root / "matrices"),
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
                "--script-output",
                str(script),
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass", payload["blockers"])
            self.assertEqual(payload["summary"]["selected_instances"], 3)
            self.assertEqual(payload["summary"]["row_pass"], 3)
            first_cmd = payload["rows"][0]["matrix_command"]
            self.assertIn(str(HERE / "run_real_agent_harness_matrix.py"), first_cmd)
            self.assertIn("default,nice_priority,static_affinity,static_cgroup,default_observed,flowcausal_no_width,flowcausal_width", first_cmd)
            self.assertIn("--nice-value", first_cmd)
            self.assertIn("--static-affinity-taskset", first_cmd)
            self.assertIn("--static-cgroup-cpu-weight", first_cmd)
            self.assertIn("--require-any-env", first_cmd)
            self.assertIn("OPENAI_API_KEY", first_cmd)
            self.assertIn("ANTHROPIC_API_KEY", first_cmd)
            command_template = first_cmd[first_cmd.index("--command-template") + 1]
            self.assertIn("run_mini_swebench_single_capture.py", command_template)
            self.assertIn("--mini-extra", command_template)
            self.assertIn(str(mini_extra), command_template)
            self.assertIn("--yolo", command_template)
            script_text = script.read_text(encoding="utf-8")
            self.assertIn(f"source {env_script}", script_text)
            self.assertIn("API-key guard skipped because this plan has execute=false", script_text)
            self.assertNotIn("set at least one API key before full-harness matrices", script_text)
            self.assertIn("run_real_agent_harness_matrix.py", script_text)
            self.assertIn("FlowCausal real-agent full-harness matrix plan", markdown.read_text(encoding="utf-8"))

    def test_execute_plan_keeps_api_guard(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            mini_extra = root / "harness" / ".venv" / "bin" / "mini-extra"
            env_script = root / "harness" / "env.sh"
            write_executable(mini_extra, "#!/bin/sh\necho mini-extra\n")
            write_executable(env_script, "#!/usr/bin/env bash\ntrue\n")
            plan = root / "instance-plan.mini.json"
            output = root / "harness-matrix-plan.json"
            script = root / "run-harness-matrices.sh"
            plan.write_text(json.dumps(instance_plan(root, mini_extra=mini_extra)), encoding="utf-8")

            self.run_planner(
                "--instance-plan",
                str(plan),
                "--root",
                str(root),
                "--matrix-root",
                str(root / "matrices"),
                "--execute",
                "--output",
                str(output),
                "--script-output",
                str(script),
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertTrue(payload["summary"]["execute"])
            self.assertIn("--execute", payload["rows"][0]["matrix_command"])
            script_text = script.read_text(encoding="utf-8")
            self.assertIn("set at least one API key before full-harness matrices", script_text)

    def test_missing_mini_extra_fails_plan(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            stale = root / "old" / ".venv" / "bin" / "mini-extra"
            plan = root / "instance-plan.mini.json"
            output = root / "harness-matrix-plan.json"
            plan.write_text(json.dumps(instance_plan(root, mini_extra=stale)), encoding="utf-8")

            self.run_planner(
                "--instance-plan",
                str(plan),
                "--root",
                str(root),
                "--matrix-root",
                str(root / "matrices"),
                "--output",
                str(output),
                expect_ok=False,
            )

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "fail")
            self.assertIn("mini-extra missing", " ".join(payload["blockers"]))


if __name__ == "__main__":
    unittest.main()

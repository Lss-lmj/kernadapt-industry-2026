#!/usr/bin/env python3
"""Tests for real-agent capture-plan preflight."""

from __future__ import annotations

import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PREFLIGHT = HERE / "preflight_real_agent_capture_plan.py"


def write_executable(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def plan_payload(root: pathlib.Path, *, mini_extra: pathlib.Path) -> dict:
    capture_dir = root / "captures"
    repo_root = root / "repos"
    env_script = root / "harness" / "env.sh"
    selected = []
    for name in ("django__django-14608", "sympy__sympy-21171", "matplotlib__matplotlib-23314"):
        repo_dir = repo_root / name
        repo_dir.mkdir(parents=True, exist_ok=True)
        raw = capture_dir / "runs" / f"{name}.traj"
        endpoint = capture_dir / "runs" / f"{name}.endpoint.json"
        selected.append(
            {
                "instance_id": name,
                "repo": name.split("__")[0],
                "repo_dir": str(repo_dir),
                "capture_command": [
                    sys.executable,
                    str(HERE / "capture_real_agent_run.py"),
                    "--out-dir",
                    str(capture_dir),
                    "--instance-id",
                    name,
                    "--repo-dir",
                    str(repo_dir),
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
                    "--mini-extra",
                    str(mini_extra),
                ],
            }
        )
    return {
        "schema": "flowcausal.real_agent_instance_plan.v1",
        "capture_dir": str(capture_dir),
        "repo_root": str(repo_root),
        "selected": selected,
        "run_environment": {
            "mode": "mini-swe-agent",
            "env_script": str(env_script),
            "require_any_env": ["OPENAI_API_KEY", "ANTHROPIC_API_KEY"],
        },
    }


class PreflightRealAgentCapturePlanTests(unittest.TestCase):
    def run_preflight(self, *args: str, expect_ok: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PREFLIGHT), *args],
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

    def test_current_mini_capture_plan_passes_launch_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            mini_extra = root / "harness" / ".venv" / "bin" / "mini-extra"
            env_script = root / "harness" / "env.sh"
            write_executable(mini_extra, "#!/bin/sh\necho mini-extra\n")
            write_executable(env_script, "#!/usr/bin/env bash\ntrue\n")
            plan = root / "plan.json"
            script = root / "capture.sh"
            plan.write_text(json.dumps(plan_payload(root, mini_extra=mini_extra)), encoding="utf-8")
            write_executable(
                script,
                f"#!/usr/bin/env bash\nsource {env_script}\nFLOWCAUSAL_HAS_REQUIRED_ENV=0\n"
                "for key in OPENAI_API_KEY ANTHROPIC_API_KEY; do echo $key; done\n",
            )
            output = root / "preflight.json"
            markdown = root / "preflight.md"

            report = self.run_preflight(
                "--plan",
                str(plan),
                "--script",
                str(script),
                "--root",
                str(root),
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
            )

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["summary"]["row_pass"], 3)
            self.assertTrue(output.exists())
            self.assertIn("capture-plan preflight", markdown.read_text(encoding="utf-8"))

    def test_stale_mini_extra_path_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            env_script = root / "harness" / "env.sh"
            stale_mini_extra = root / "old-harness" / ".venv" / "bin" / "mini-extra"
            write_executable(env_script, "#!/usr/bin/env bash\ntrue\n")
            plan = root / "plan.json"
            script = root / "capture.sh"
            plan.write_text(json.dumps(plan_payload(root, mini_extra=stale_mini_extra)), encoding="utf-8")
            write_executable(
                script,
                f"#!/usr/bin/env bash\nsource {env_script}\nFLOWCAUSAL_HAS_REQUIRED_ENV=0\n"
                "for key in OPENAI_API_KEY ANTHROPIC_API_KEY; do echo $key; done\n",
            )

            report = self.run_preflight("--plan", str(plan), "--script", str(script), "--root", str(root), expect_ok=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("mini-extra is not executable", " ".join(report["blockers"]))

    def test_missing_script_api_guard_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            mini_extra = root / "harness" / ".venv" / "bin" / "mini-extra"
            env_script = root / "harness" / "env.sh"
            write_executable(mini_extra, "#!/bin/sh\necho mini-extra\n")
            write_executable(env_script, "#!/usr/bin/env bash\ntrue\n")
            plan = root / "plan.json"
            script = root / "capture.sh"
            plan.write_text(json.dumps(plan_payload(root, mini_extra=mini_extra)), encoding="utf-8")
            write_executable(script, f"#!/usr/bin/env bash\nsource {env_script}\n")

            report = self.run_preflight("--plan", str(plan), "--script", str(script), "--root", str(root), expect_ok=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("script missing API-key guard variable", " ".join(report["blockers"]))


if __name__ == "__main__":
    unittest.main()

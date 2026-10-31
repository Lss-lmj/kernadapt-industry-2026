#!/usr/bin/env python3
"""Tests for real-agent repo preparation script generation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PREPARE = HERE / "prepare_real_agent_repos.py"


def plan_payload() -> dict:
    return {
        "schema": "flowcausal.real_agent_instance_plan.v1",
        "selected": [
            {
                "instance_id": "django__django-1",
                "repo": "django/django",
                "repo_dir": "benchmarks/harness-replaybench/repos/django__django-1",
                "raw": {"base_commit": "abc123"},
            },
            {
                "instance_id": "sympy__sympy-2",
                "repo": "https://github.com/sympy/sympy.git",
                "repo_dir": "benchmarks/harness-replaybench/repos/sympy__sympy-2",
                "raw": {},
            },
        ],
    }


class PrepareRealAgentReposTests(unittest.TestCase):
    def run_prepare(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(PREPARE), *args],
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

    def test_generates_clone_checkout_script_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            plan = tmp / "plan.json"
            plan.write_text(json.dumps(plan_payload()), encoding="utf-8")
            output = tmp / "repo-manifest.json"
            markdown = tmp / "repo-manifest.md"
            script = tmp / "prepare-repos.sh"

            self.run_prepare(
                "--plan",
                str(plan),
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
                "--script-output",
                str(script),
            )

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["summary"]["repo_rows"], 2)
            self.assertEqual(report["summary"]["missing_base_commit"], 1)
            self.assertTrue(any("missing base_commit" in item for item in report["warnings"]))
            text = script.read_text(encoding="utf-8")
            self.assertIn("https://github.com/django/django.git", text)
            self.assertIn("https://github.com/sympy/sympy.git", text)
            self.assertIn("checkout --detach abc123", text)
            self.assertIn("leaving repository at current HEAD", text)
            self.assertIn("FlowCausal real-agent repo preparation", markdown.read_text(encoding="utf-8"))

    def test_repo_root_override_changes_repo_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            plan = tmp / "plan.json"
            plan.write_text(json.dumps(plan_payload()), encoding="utf-8")
            output = tmp / "repo-manifest.json"
            script = tmp / "prepare-repos.sh"

            self.run_prepare(
                "--plan",
                str(plan),
                "--repo-root",
                str(tmp / "repos"),
                "--output",
                str(output),
                "--script-output",
                str(script),
            )

            report = json.loads(output.read_text(encoding="utf-8"))
            repo_dirs = [row["repo_dir"] for row in report["repos"]]
            self.assertTrue(all(str(tmp / "repos") in path for path in repo_dirs))

    def test_execute_skips_when_git_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            plan = tmp / "plan.json"
            plan.write_text(json.dumps(plan_payload()), encoding="utf-8")
            output = tmp / "repo-manifest.json"
            script = tmp / "prepare-repos.sh"

            self.run_prepare(
                "--plan",
                str(plan),
                "--git",
                str(tmp / "missing-git"),
                "--output",
                str(output),
                "--script-output",
                str(script),
                "--execute",
            )

            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "partial")
            self.assertIn("--git executable does not exist", " ".join(report["blockers"]))
            self.assertEqual(report["run"], {"skipped": "not executed because repo preparation blockers exist"})


if __name__ == "__main__":
    unittest.main()

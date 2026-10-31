#!/usr/bin/env python3
"""Tests for real-agent harness readiness checks."""

from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
CHECK = HERE / "check_real_agent_readiness.py"


def write_executable(path: pathlib.Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def write_capture_manifest(capture_dir: pathlib.Path, count: int) -> None:
    capture_dir.mkdir(parents=True, exist_ok=True)
    instances = []
    for index in range(count):
        iid = f"django__issue-{index}"
        raw = capture_dir / "raw" / f"{iid}.traj"
        endpoint = capture_dir / "endpoints" / f"{iid}.json"
        repo = capture_dir / "repos" / iid
        raw.parent.mkdir(parents=True, exist_ok=True)
        endpoint.parent.mkdir(parents=True, exist_ok=True)
        repo.mkdir(parents=True, exist_ok=True)
        raw.write_text('{"trajectory":[]}', encoding="utf-8")
        endpoint.write_text('{"resolved": true}', encoding="utf-8")
        instances.append(
            {
                "instance_id": iid,
                "raw_agent_output": str(raw.relative_to(capture_dir)),
                "repo_dir": str(repo.relative_to(capture_dir)),
                "full_harness_result": str(endpoint.relative_to(capture_dir)),
            }
        )
    (capture_dir / "real-agent-manifest.json").write_text(
        json.dumps({"defaults": {}, "instances": instances}),
        encoding="utf-8",
    )


class CheckRealAgentReadinessTests(unittest.TestCase):
    def run_check(self, *args: str, env: dict[str, str] | None = None) -> dict:
        proc = subprocess.run(
            [sys.executable, str(CHECK), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_missing_framework_tools_fail_run_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            empty_path = tmp / "bin"
            empty_path.mkdir()

            report = self.run_check(
                "--capture-dir",
                str(tmp / "captures"),
                "--framework-command",
                "definitely-missing-flowcausal-agent",
                "--framework-module",
                "definitely_missing_flowcausal_module",
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--skip-docker-check",
                "--path",
                str(empty_path),
                env={**os.environ, "PATH": str(empty_path)},
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["run_readiness_status"], "fail")
            self.assertIn("no SWE-agent/mini-swe-agent/OpenHands/SWE-bench command or Python module found", report["blockers"])

    def test_fake_runnable_environment_without_captures_is_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "sweagent", "#!/bin/sh\necho sweagent 1.0\n")
            write_executable(bin_dir / "docker", "#!/bin/sh\necho docker ok\n")
            env = {**os.environ, "PATH": str(bin_dir), "FLOWCAUSAL_TEST_API_KEY": "secret"}

            report = self.run_check(
                "--capture-dir",
                str(tmp / "captures"),
                "--framework-command",
                "sweagent",
                "--framework-module",
                "definitely_missing_flowcausal_module",
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--path",
                str(bin_dir),
                env=env,
            )

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["run_readiness_status"], "pass")
            self.assertEqual(report["evidence_capture_status"], "fail")
            self.assertIn("run capture_real_agent_run.py", " ".join(report["next_actions"]))

    def test_default_framework_set_accepts_mini_swe_agent_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "mini-swe-agent", "#!/bin/sh\necho mini-swe-agent 1.0\n")
            write_executable(bin_dir / "docker", "#!/bin/sh\necho docker ok\n")
            env = {**os.environ, "PATH": str(bin_dir), "FLOWCAUSAL_TEST_API_KEY": "secret"}

            report = self.run_check(
                "--capture-dir",
                str(tmp / "captures"),
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--path",
                str(bin_dir),
                env=env,
            )

            self.assertEqual(report["run_readiness_status"], "pass", report["blockers"])
            self.assertIn("mini-swe-agent", report["requirements"]["framework_commands"])
            self.assertIn("commands=mini-swe-agent", " ".join(report["stages"][0]["evidence"]))

    def test_missing_required_capture_command_fails_run_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            write_executable(bin_dir / "mini-swe-agent", "#!/bin/sh\necho mini-swe-agent 1.0\n")
            write_executable(bin_dir / "docker", "#!/bin/sh\necho docker ok\n")
            env = {**os.environ, "PATH": str(bin_dir), "FLOWCAUSAL_TEST_API_KEY": "secret"}

            report = self.run_check(
                "--capture-dir",
                str(tmp / "captures"),
                "--framework-command",
                "mini-swe-agent",
                "--framework-module",
                "definitely_missing_flowcausal_module",
                "--required-command",
                "mini-extra",
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--path",
                str(bin_dir),
                env=env,
            )

            self.assertEqual(report["run_readiness_status"], "fail")
            self.assertIn("required capture command not found: mini-extra", report["blockers"])

    def test_required_capture_command_accepts_exact_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bin_dir = tmp / "bin"
            harness_dir = tmp / "harness" / ".venv" / "bin"
            bin_dir.mkdir()
            harness_dir.mkdir(parents=True)
            write_executable(bin_dir / "mini-swe-agent", "#!/bin/sh\necho mini-swe-agent 1.0\n")
            write_executable(bin_dir / "docker", "#!/bin/sh\necho docker ok\n")
            mini_extra = harness_dir / "mini-extra"
            write_executable(mini_extra, "#!/bin/sh\necho mini-extra 1.0\n")
            env = {**os.environ, "PATH": str(bin_dir), "FLOWCAUSAL_TEST_API_KEY": "secret"}

            report = self.run_check(
                "--capture-dir",
                str(tmp / "captures"),
                "--framework-command",
                "mini-swe-agent",
                "--framework-module",
                "definitely_missing_flowcausal_module",
                "--required-command",
                str(mini_extra),
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--path",
                str(bin_dir),
                env=env,
            )

            self.assertEqual(report["run_readiness_status"], "pass", report["blockers"])
            stages = {row["name"]: row for row in report["stages"]}
            self.assertEqual(stages["required capture commands"]["status"], "pass")
            self.assertIn(str(mini_extra), report["checks"]["required_commands"][0]["path"])

    def test_three_captured_instances_pass_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bin_dir = tmp / "bin"
            capture_dir = tmp / "captures"
            bin_dir.mkdir()
            write_executable(bin_dir / "sweagent", "#!/bin/sh\necho sweagent 1.0\n")
            write_executable(bin_dir / "docker", "#!/bin/sh\necho docker ok\n")
            write_capture_manifest(capture_dir, 3)
            env = {**os.environ, "PATH": str(bin_dir), "FLOWCAUSAL_TEST_API_KEY": "secret"}
            output = tmp / "readiness.json"
            markdown = tmp / "readiness.md"

            report = self.run_check(
                "--capture-dir",
                str(capture_dir),
                "--framework-command",
                "sweagent",
                "--framework-module",
                "definitely_missing_flowcausal_module",
                "--api-env",
                "FLOWCAUSAL_TEST_API_KEY",
                "--path",
                str(bin_dir),
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
                env=env,
            )

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["run_readiness_status"], "pass")
            self.assertEqual(report["evidence_capture_status"], "pass")
            self.assertTrue(output.exists())
            self.assertIn("FlowCausal real-agent harness readiness", markdown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

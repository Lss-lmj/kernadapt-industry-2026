#!/usr/bin/env python3
"""Tests for isolated real-agent harness bootstrap generation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
BOOTSTRAP = HERE / "bootstrap_real_agent_harness.py"


class BootstrapRealAgentHarnessTests(unittest.TestCase):
    def run_bootstrap(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(BOOTSTRAP), *args],
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

    def test_default_generation_writes_install_env_and_readiness_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "harness"

            self.run_bootstrap("--out-dir", str(out), "--capture-dir", str(tmp / "captures"))

            install = out / "install-harness.sh"
            env = out / "env.sh"
            api = out / "api-keys.env.template"
            readiness = out / "post-install-readiness.sh"
            report = json.loads((out / "bootstrap-report.json").read_text(encoding="utf-8"))
            self.assertTrue(install.exists())
            self.assertTrue(env.exists())
            self.assertTrue(api.exists())
            self.assertTrue(readiness.exists())
            install_text = install.read_text(encoding="utf-8")
            self.assertIn("git clone --depth 1", install_text)
            self.assertIn("uv tool install openhands --python 3.12", install_text)
            self.assertIn("mini_swe_agent", install_text)
            self.assertIn("command -v \"$cmd\"", install_text)
            self.assertNotIn("--install-dir", install_text)
            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertIn("swe-agent-source", report["backends"])
            self.assertIn("openhands-uv", report["backends"])

    def test_custom_backend_generation(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "harness"

            self.run_bootstrap(
                "--out-dir",
                str(out),
                "--backend",
                "mini-swe-agent",
                "--backend",
                "swebench-pip",
            )

            install_text = (out / "install-harness.sh").read_text(encoding="utf-8")
            self.assertIn("python -m pip install mini-swe-agent", install_text)
            self.assertIn("python -m pip install swebench", install_text)
            report = json.loads((out / "bootstrap-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["backends"], ["mini-swe-agent", "swebench-pip"])

    def test_execute_is_skipped_when_blockers_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "harness"

            self.run_bootstrap(
                "--out-dir",
                str(out),
                "--python",
                str(tmp / "missing-python"),
                "--execute",
                expect_ok=False,
            )
            report = json.loads((out / "bootstrap-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertIn("--python executable does not exist", " ".join(report["blockers"]))
            self.assertEqual(report["run"], {"skipped": "not executed because bootstrap blockers exist"})


if __name__ == "__main__":
    unittest.main()

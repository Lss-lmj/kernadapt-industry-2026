#!/usr/bin/env python3
"""Regression tests for the external scx sandbox wrapper."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "run_external_scx_sandbox.py"


def load_module() -> Any:
    spec = importlib.util.spec_from_file_location("run_external_scx_sandbox", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


external_mod = load_module()


class RunExternalScxSandboxTests(unittest.TestCase):
    def test_parse_workload_command_strips_separator(self) -> None:
        self.assertEqual(
            external_mod.parse_workload_command(["--", "python3", "-V"]),
            ["python3", "-V"],
        )

    def test_wrapper_runs_workload_and_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            out_dir = pathlib.Path(raw_tmp) / "external-scx"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--out-dir",
                    str(out_dir),
                    "--scheduler",
                    sys.executable,
                    "--scheduler-arg=-c",
                    "--scheduler-arg",
                    "import time; time.sleep(60)",
                    "--scheduler-sudo",
                    "never",
                    "--scheduler-ready-timeout-sec",
                    "0",
                    "--scheduler-startup-grace-sec",
                    "0",
                    "--",
                    sys.executable,
                    "-c",
                    "print('workload-ok')",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=10,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], external_mod.SCHEMA)
            self.assertEqual(manifest["workload_returncode"], 0)
            self.assertEqual(manifest["scheduler"], str(pathlib.Path(sys.executable).resolve()))
            self.assertIn("scheduler_returncode", manifest)
            self.assertTrue((out_dir / "scheduler.log").exists())
            self.assertIn("workload-ok", (out_dir / "workload.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

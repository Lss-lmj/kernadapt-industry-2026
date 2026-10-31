#!/usr/bin/env python3
"""Regression tests for the live sandbox harness."""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any


MODULE_PATH = pathlib.Path(__file__).with_name("run_live_sandbox.py")


def load_live_module() -> Any:
    spec = importlib.util.spec_from_file_location("run_live_sandbox", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live_mod = load_live_module()


class RunLiveSandboxTests(unittest.TestCase):
    def test_oracle_frontier_label_file_is_precreated_for_workload_writer(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "nested" / "oracle-frontier-labels.jsonl"
            path.parent.mkdir()
            path.write_text("stale\n", encoding="utf-8")

            live_mod.prepare_oracle_frontier_label_file(path)

            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual(path.stat().st_uid, os.geteuid())
            with path.open("a", encoding="utf-8") as handle:
                handle.write('{"type":"frontier_label"}\n')
            self.assertIn("frontier_label", path.read_text(encoding="utf-8"))

    def test_wait_then_stop_process_group_preserves_clean_helper_exit(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.05)"],
            start_new_session=True,
            text=True,
        )

        rc = live_mod.wait_then_stop_process_group(
            proc,
            "short-helper",
            settle_timeout_sec=1.0,
            stop_timeout_sec=1.0,
        )

        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()

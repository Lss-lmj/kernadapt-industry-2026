#!/usr/bin/env python3
"""Tests for HarnessReplayBench replay-time system profiling."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PROFILE = HERE / "harness_replay_system_profile.py"


def load_profile_module():
    spec = importlib.util.spec_from_file_location("harness_replay_system_profile", PROFILE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {PROFILE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


profile_mod = load_profile_module()


class HarnessReplaySystemProfileTests(unittest.TestCase):
    def test_recorder_samples_process_tree_and_writes_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            stage_state = tmp / "stage-state.json"
            stage_state.write_text(json.dumps({"stage": "TEST", "phase": "begin"}), encoding="utf-8")
            proc = subprocess.Popen(
                [sys.executable, "-c", "import subprocess, time; p=subprocess.Popen(['sleep','0.15']); time.sleep(0.2); p.wait()"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            recorder = profile_mod.SystemProfileRecorder(
                root_pid=proc.pid,
                out_dir=tmp,
                stage_state_file=stage_state,
                interval_ms=10,
                provenance={"instance_id": "profile-fixture"},
            )
            recorder.start()
            returncode = proc.wait(timeout=5)
            self.assertEqual(returncode, 0)
            result = recorder.stop()

            self.assertTrue(result["enabled"])
            self.assertGreaterEqual(result["samples"], 1)
            self.assertTrue((tmp / "system-profile.ndjson").is_file())
            self.assertTrue((tmp / "system-profile-summary.json").is_file())
            self.assertTrue((tmp / "system-profile-summary.md").is_file())
            summary = json.loads((tmp / "system-profile-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema"], "flowcausal.harness_replay_system_profile.v1")
            self.assertGreaterEqual(summary["unique_pids_seen"], 1)
            self.assertGreaterEqual(summary["max_process_count"], 1)
            self.assertIn("TEST", summary["stage_sample_counts"])
            self.assertIn("claim_discipline", summary)
            self.assertIn("not by itself", (tmp / "system-profile-summary.md").read_text(encoding="utf-8"))

    def test_summarize_records_classifies_no_samples_as_s0(self) -> None:
        summary = profile_mod.summarize_records([], root_pid=os.getpid(), started_ns=0, provenance={})
        self.assertEqual(summary["profile_level"], "S0: no replay-time samples")
        self.assertFalse(summary["schedstat_observed"])
        self.assertEqual(summary["sample_count"], 0)


if __name__ == "__main__":
    unittest.main()

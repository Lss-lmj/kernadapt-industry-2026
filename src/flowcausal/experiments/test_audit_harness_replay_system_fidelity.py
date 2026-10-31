#!/usr/bin/env python3
"""Tests for system profile comparison audit."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_harness_replay_system_fidelity.py"


def profile(**overrides):
    payload = {
        "schema": "flowcausal.harness_replay_system_profile.v1",
        "profile_level": "S1: process-tree profile with schedstat deltas",
        "sample_count": 100,
        "max_process_count": 10,
        "max_leaf_count": 6,
        "unique_pids_seen": 20,
        "cpu_migration_events": 2,
        "aggregate_delta": {
            "sched_runtime_delta_ns": 1_000_000,
            "sched_wait_delta_ns": 10_000,
        },
        "stage_sample_counts": {"BUILD": 40, "TEST": 50, "SHELL": 10},
        "stage_delta_summary": {},
        "provenance": {"instance_id": "fixture"},
    }
    payload.update(overrides)
    return payload


class SystemFidelityAuditTests(unittest.TestCase):
    def run_audit(self, reference: dict, replay: dict, *extra: str) -> tuple[int, dict, str]:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            ref = tmp / "ref.json"
            rep = tmp / "rep.json"
            out = tmp / "audit.json"
            md = tmp / "audit.md"
            ref.write_text(json.dumps(reference), encoding="utf-8")
            rep.write_text(json.dumps(replay), encoding="utf-8")
            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--reference-profile",
                    str(ref),
                    "--replay-profile",
                    str(rep),
                    "--output",
                    str(out),
                    "--markdown-output",
                    str(md),
                    *extra,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            data = json.loads(out.read_text(encoding="utf-8"))
            return proc.returncode, data, md.read_text(encoding="utf-8")

    def test_similar_profiles_pass(self) -> None:
        replay = profile(
            sample_count=96,
            max_process_count=11,
            unique_pids_seen=22,
            aggregate_delta={"sched_runtime_delta_ns": 1_200_000, "sched_wait_delta_ns": 40_000},
            stage_sample_counts={"BUILD": 38, "TEST": 48, "SHELL": 10},
        )

        rc, data, markdown = self.run_audit(profile(), replay)

        self.assertEqual(rc, 0, data)
        self.assertEqual(data["status"], "pass")
        self.assertEqual(data["fidelity_level"], "S3: system profile comparison pass")
        wait = next(check for check in data["checks"] if check["name"] == "sched_wait_delta_ns")
        self.assertEqual(wait["status"], "warn")
        self.assertIn("Scalar checks", markdown)

    def test_missing_stage_fails(self) -> None:
        replay = profile(stage_sample_counts={"BUILD": 90, "SHELL": 10})

        rc, data, _markdown = self.run_audit(profile(), replay)

        self.assertNotEqual(rc, 0)
        self.assertEqual(data["status"], "fail")
        self.assertIn("TEST", str(data["blockers"]))
        self.assertEqual(data["fidelity_level"], "S2: comparable profiles collected; comparison not passed")

    def test_schema_mismatch_fails(self) -> None:
        bad = profile(schema="wrong")

        rc, data, _markdown = self.run_audit(bad, profile())

        self.assertNotEqual(rc, 0)
        self.assertIn("reference profile schema mismatch", data["blockers"])


if __name__ == "__main__":
    unittest.main()

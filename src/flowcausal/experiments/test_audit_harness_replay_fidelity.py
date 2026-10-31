#!/usr/bin/env python3
"""Tests for HarnessReplayBench structural replay fidelity auditing."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_harness_replay_fidelity.py"


class HarnessReplayFidelityAuditTests(unittest.TestCase):
    def test_single_log_passes_with_observed_nonzero_allow_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            log = tmp / "workload.log"
            out = tmp / "audit.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "case-a",
                        "commands": [
                            {"id": "inspect", "cmd": "grep x file", "stage": "INSPECT"},
                            {
                                "id": "probe",
                                "cmd": "python probe.py",
                                "stage": "TEST",
                                "observed_returncode": 1,
                                "allow_failure": True,
                            },
                            {
                                "id": "submit",
                                "cmd": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
                                "stage": "PACKAGE_BACKGROUND",
                                "observed_returncode": -1,
                                "allow_failure": True,
                                "critical": True,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            records = [
                {
                    "type": "agent_stage_end",
                    "command_index": 0,
                    "source_id": "inspect",
                    "stage": "INSPECT",
                    "returncode": 0,
                    "elapsed_sec": 0.01,
                },
                {
                    "type": "agent_stage_end",
                    "command_index": 1,
                    "source_id": "probe",
                    "stage": "TEST",
                    "returncode": 1,
                    "elapsed_sec": 0.02,
                },
                {
                    "type": "agent_stage_end",
                    "command_index": 2,
                    "source_id": "submit",
                    "stage": "PACKAGE_BACKGROUND",
                    "returncode": 0,
                    "elapsed_sec": 0.01,
                },
                {"type": "flowcausal_metric", "name": "critical_sec", "value": 0.05},
                {"type": "workload_end", "returncode": 0},
            ]
            log.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--trace",
                    str(trace),
                    "--log",
                    str(log),
                    "--output",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["command_count"], 3)
            self.assertEqual(len(payload["replay_nonzero"]), 1)
            self.assertEqual(payload["observed_nonzero"][0]["index"], 1)

    def test_single_log_fails_on_stage_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            log = tmp / "workload.log"
            out = tmp / "audit.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "case-b",
                        "commands": [{"id": "test", "cmd": "make test", "stage": "TEST"}],
                    }
                ),
                encoding="utf-8",
            )
            log.write_text(
                json.dumps(
                    {
                        "type": "agent_stage_end",
                        "command_index": 0,
                        "source_id": "test",
                        "stage": "INSPECT",
                        "returncode": 0,
                    }
                )
                + "\n"
                + json.dumps({"type": "flowcausal_metric", "name": "critical_sec", "value": 1.0})
                + "\n"
                + json.dumps({"type": "workload_end", "returncode": 0})
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--trace",
                    str(trace),
                    "--log",
                    str(log),
                    "--output",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(proc.returncode, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "fail")
            self.assertTrue(payload["mismatches"])

    def test_single_log_accepts_packaged_harness_replaybench_trace_schema(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            log = tmp / "workload.log"
            out = tmp / "audit.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "harness_replaybench.trace.v1",
                        "instance_id": "case-c",
                        "commands": [{"id": "inspect", "cmd": "true", "stage": "INSPECT"}],
                    }
                ),
                encoding="utf-8",
            )
            log.write_text(
                json.dumps(
                    {
                        "type": "agent_stage_end",
                        "command_index": 0,
                        "source_id": "inspect",
                        "stage": "INSPECT",
                        "returncode": 0,
                    }
                )
                + "\n"
                + json.dumps({"type": "flowcausal_metric", "name": "critical_sec", "value": 0.1})
                + "\n"
                + json.dumps({"type": "workload_end", "returncode": 0})
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--trace",
                    str(trace),
                    "--log",
                    str(log),
                    "--output",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "pass")


if __name__ == "__main__":
    unittest.main()

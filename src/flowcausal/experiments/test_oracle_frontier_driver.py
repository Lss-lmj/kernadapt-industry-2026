#!/usr/bin/env python3
"""Tests for the frontier_label -> oracle flowctl bridge."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
DRIVER = HERE / "run_oracle_frontier_driver.py"


class OracleFrontierDriverTests(unittest.TestCase):
    def test_dry_run_translates_frontier_label_to_flowctl_oracle_command(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            label_file = tmp / "labels.jsonl"
            label_file.write_text(
                json.dumps(
                    {
                        "type": "frontier_label",
                        "pid": 12345,
                        "stage": "TEST",
                        "command_index": 7,
                        "source_id": "pytest-target",
                        "label": {"confidence": 88, "width_control": True},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--label-file",
                    str(label_file),
                    "--root-pid",
                    "999999",
                    "--session",
                    "pid:999999",
                    "--flowctl",
                    "/tmp/flowctl.py",
                    "--pin-dir",
                    "/tmp/pin",
                    "--task-map",
                    "/tmp/pin/task_ctx",
                    "--policy-map",
                    "/tmp/pin/session_policy",
                    "--dry-run",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--frontier-pid 12345", proc.stdout)
            self.assertIn("--stage-kind TEST", proc.stdout)
            self.assertIn("--stage-label 7-pytest-target", proc.stdout)
            self.assertIn("--pin-dir /tmp/pin", proc.stdout)
            self.assertIn("--task-map /tmp/pin/task_ctx", proc.stdout)
            self.assertIn("--policy-map /tmp/pin/session_policy", proc.stdout)
            self.assertIn("--confidence 88", proc.stdout)
            self.assertIn("--width-control", proc.stdout)

    def test_width_control_cli_is_accepted_as_global_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            label_file = tmp / "labels.jsonl"
            label_file.write_text(
                json.dumps(
                    {
                        "type": "frontier_label",
                        "pid": 23456,
                        "stage": "BUILD",
                        "command_index": 3,
                        "source_id": "compile",
                        "label": {"confidence": 77},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--label-file",
                    str(label_file),
                    "--root-pid",
                    "999999",
                    "--flowctl",
                    "/tmp/flowctl.py",
                    "--width-control",
                    "--dry-run",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--frontier-pid 23456", proc.stdout)
            self.assertIn("--width-control", proc.stdout)

    def test_repo_stage_names_are_mapped_to_flowctl_stage_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            label_file = tmp / "labels.jsonl"
            label_file.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "frontier_label",
                                "pid": 34567,
                                "stage": "INSPECT",
                                "command_index": 0,
                                "source_id": "turn0:inspect",
                                "label": {"confidence": 99},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "frontier_label",
                                "pid": 34568,
                                "stage": "RETRY_PROBE",
                                "command_index": 3,
                                "source_id": "turn0:retry",
                                "label": {"confidence": 99},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(DRIVER),
                    "--label-file",
                    str(label_file),
                    "--root-pid",
                    "999999",
                    "--flowctl",
                    "/tmp/flowctl.py",
                    "--dry-run",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=5,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("--frontier-pid 34567", proc.stdout)
            self.assertIn("--stage-kind SEARCH", proc.stdout)
            self.assertIn("--frontier-pid 34568", proc.stdout)
            self.assertIn("--stage-kind RETRY", proc.stdout)


if __name__ == "__main__":
    unittest.main()

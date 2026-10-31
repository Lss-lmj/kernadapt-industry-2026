#!/usr/bin/env python3
"""Tests for manual/oracle frontier label annotation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
ANNOTATOR = HERE / "annotate_frontier_labels.py"


class AnnotateFrontierLabelsTests(unittest.TestCase):
    def run_annotator(self, *args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(ANNOTATOR), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_success:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return proc

    def write_trace(self, path: pathlib.Path, *, existing_label: dict | None = None) -> None:
        commands = [
            {"id": "inspect", "cmd": "rg target .", "stage": "INSPECT", "cwd": ".", "turn": 0},
            {"id": "test", "cmd": "pytest tests/test_target.py", "stage": "TEST", "cwd": ".", "turn": 1},
        ]
        if existing_label is not None:
            commands[1]["frontier_label"] = existing_label
        path.write_text(
            json.dumps(
                {
                    "schema": "flowcausal.agent_trace.v1",
                    "source": "fixture",
                    "instance_id": "django__issue",
                    "commands": commands,
                    "provenance": {"input_file": "/tmp/raw.traj", "input_sha256": "0" * 64},
                }
            ),
            encoding="utf-8",
        )

    def test_applies_manual_label_by_command_index_and_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "manual-labels.json"
            out = tmp / "annotated.json"
            self.write_trace(trace)
            labels.write_text(
                json.dumps(
                    {
                        "labels": [
                            {
                                "command_index": 1,
                                "source_id": "test",
                                "reason": "pytest command gates agent-visible resolution",
                                "confidence": 100,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            proc = self.run_annotator(
                "--input",
                str(trace),
                "--annotations",
                str(labels),
                "--output",
                str(out),
                "--annotator",
                "human-a",
                "--mark-critical",
                "--created-at",
                "2026-01-01T00:00:00+00:00",
            )

            self.assertIn('"annotated_count": 1', proc.stdout)
            annotated = json.loads(out.read_text(encoding="utf-8"))
            command = annotated["commands"][1]
            self.assertTrue(command["critical"])
            self.assertEqual(command["frontier_label"]["source"], "manual")
            self.assertEqual(command["frontier_label"]["annotator"], "human-a")
            self.assertEqual(command["frontier_label"]["confidence"], 100)
            history = annotated["provenance"]["frontier_label_annotations"]
            self.assertEqual(history[0]["annotated_count"], 1)
            self.assertEqual(history[0]["applied"][0]["source_id"], "test")
            self.assertIn("annotation_sha256", history[0])

    def test_rejects_heuristic_annotation_source_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "labels.jsonl"
            out = tmp / "annotated.json"
            self.write_trace(trace)
            labels.write_text(
                json.dumps(
                    {
                        "command_index": 1,
                        "source_id": "test",
                        "label": {"source": "flowcausal-normalizer-heuristic"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            proc = self.run_annotator(
                "--input",
                str(trace),
                "--annotations",
                str(labels),
                "--output",
                str(out),
                expect_success=False,
            )

            self.assertIn("heuristic", proc.stderr)
            self.assertFalse(out.exists())

    def test_replace_heuristic_requires_explicit_flag(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "manual-labels.json"
            out = tmp / "annotated.json"
            self.write_trace(trace, existing_label={"source": "flowcausal-normalizer-heuristic"})
            labels.write_text(json.dumps([{"command_index": 1, "source_id": "test", "source": "manual"}]), encoding="utf-8")

            proc = self.run_annotator(
                "--input",
                str(trace),
                "--annotations",
                str(labels),
                "--output",
                str(out),
                expect_success=False,
            )
            self.assertIn("already has frontier_label", proc.stderr)

            self.run_annotator(
                "--input",
                str(trace),
                "--annotations",
                str(labels),
                "--output",
                str(out),
                "--replace-heuristic",
            )
            annotated = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(annotated["commands"][1]["frontier_label"]["source"], "manual")

    def test_mismatched_identity_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "manual-labels.json"
            out = tmp / "annotated.json"
            self.write_trace(trace)
            labels.write_text(json.dumps([{"command_index": 1, "source_id": "wrong"}]), encoding="utf-8")

            proc = self.run_annotator(
                "--input",
                str(trace),
                "--annotations",
                str(labels),
                "--output",
                str(out),
                expect_success=False,
            )

            self.assertIn("did not match", proc.stderr)


if __name__ == "__main__":
    unittest.main()

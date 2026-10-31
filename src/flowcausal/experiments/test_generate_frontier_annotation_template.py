#!/usr/bin/env python3
"""Tests for frontier annotation template generation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
GENERATOR = HERE / "generate_frontier_annotation_template.py"
ANNOTATOR = HERE / "annotate_frontier_labels.py"


class GenerateFrontierAnnotationTemplateTests(unittest.TestCase):
    def write_trace(self, path: pathlib.Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "schema": "flowcausal.agent_trace.v1",
                    "instance_id": "django__issue",
                    "commands": [
                        {"id": "inspect", "cmd": "rg target .", "stage": "INSPECT", "cwd": ".", "turn": 0},
                        {"id": "build", "cmd": "python3 -m py_compile pkg.py", "stage": "BUILD", "cwd": ".", "turn": 1},
                        {"id": "test", "cmd": "pytest tests/test_target.py", "stage": "TEST", "cwd": ".", "turn": 2, "critical": True},
                        {"id": "package", "cmd": "tar -cf out.tar pkg.py", "stage": "PACKAGE_BACKGROUND", "cwd": ".", "turn": 3},
                    ],
                    "provenance": {"input_file": "/tmp/raw.traj", "input_sha256": "0" * 64},
                }
            ),
            encoding="utf-8",
        )

    def run_generator(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_default_template_is_disabled_and_selects_critical_or_test(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            out = tmp / "labels.template.json"
            self.write_trace(trace)

            proc = self.run_generator(
                "--input",
                str(trace),
                "--output",
                str(out),
                "--created-at",
                "2026-01-01T00:00:00+00:00",
            )

            self.assertIn('"candidate_count": 1', proc.stdout)
            template = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(template["schema"], "flowcausal.frontier_annotation_template.v1")
            self.assertEqual(template["candidate_policy"], "critical-or-test")
            self.assertEqual(len(template["labels"]), 1)
            row = template["labels"][0]
            self.assertFalse(row["enabled"])
            self.assertEqual(row["command_index"], 2)
            self.assertEqual(row["source_id"], "test")
            self.assertEqual(row["label"]["source"], "manual")
            self.assertIn("input_trace_sha256", template["provenance"])

    def test_enabled_oracle_template_can_feed_annotator(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "labels.json"
            annotated = tmp / "annotated.json"
            self.write_trace(trace)
            self.run_generator(
                "--input",
                str(trace),
                "--output",
                str(labels),
                "--candidate-policy",
                "last-critical-or-test",
                "--source",
                "oracle",
                "--enable-candidates",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ANNOTATOR),
                    "--input",
                    str(trace),
                    "--annotations",
                    str(labels),
                    "--output",
                    str(annotated),
                    "--source",
                    "oracle",
                    "--mark-critical",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(annotated.read_text(encoding="utf-8"))
            self.assertEqual(data["commands"][2]["frontier_label"]["source"], "oracle")

    def test_disabled_template_rows_are_ignored_by_annotator(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            labels = tmp / "labels.template.json"
            annotated = tmp / "annotated.json"
            self.write_trace(trace)
            self.run_generator("--input", str(trace), "--output", str(labels))

            proc = subprocess.run(
                [
                    sys.executable,
                    str(ANNOTATOR),
                    "--input",
                    str(trace),
                    "--annotations",
                    str(labels),
                    "--output",
                    str(annotated),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("no labels", proc.stderr)
            self.assertFalse(annotated.exists())

    def test_all_policy_includes_background_for_explicit_review(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            out = tmp / "labels.template.json"
            self.write_trace(trace)
            self.run_generator("--input", str(trace), "--output", str(out), "--candidate-policy", "all")

            template = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(template["labels"]), 4)
            self.assertEqual(template["labels"][-1]["stage"], "PACKAGE_BACKGROUND")


if __name__ == "__main__":
    unittest.main()

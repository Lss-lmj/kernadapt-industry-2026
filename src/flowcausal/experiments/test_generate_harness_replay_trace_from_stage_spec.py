#!/usr/bin/env python3
"""Tests for repo-backed HarnessReplayBench trace generation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
GENERATOR = HERE / "generate_harness_replay_trace_from_stage_spec.py"


class GenerateHarnessReplayTraceTests(unittest.TestCase):
    def run_generator(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(GENERATOR), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_inline_stage_commands_generate_normalized_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            out = tmp / "trace.json"

            summary = self.run_generator(
                "--repo-dir",
                str(repo),
                "--output",
                str(out),
                "--instance-id",
                "inline-case",
                "--created-at",
                "2026-07-20T00:00:00+00:00",
                "--emit-heuristic-frontier-labels",
                "--stage-command",
                "INSPECT::find . -maxdepth 1 -type f",
                "--stage-command",
                "BUILD::python3 -m compileall -q .",
                "--stage-command",
                "TEST::python3 - <<'PY'\nprint('ok')\nPY",
                "--stage-command",
                "PACKAGE_BACKGROUND::tar -czf \"$FLOWCAUSAL_STAGE_OUT/out.tar.gz\" .",
            )

            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["commands"], 4)
            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["schema"], "flowcausal.agent_trace.v1")
            self.assertEqual(trace["source_format"], "repo-stage-spec")
            self.assertEqual(trace["instance_id"], "inline-case")
            self.assertEqual([cmd["stage"] for cmd in trace["commands"]], ["INSPECT", "BUILD", "TEST", "PACKAGE_BACKGROUND"])
            self.assertTrue(trace["commands"][2]["critical"])
            self.assertEqual(trace["commands"][2]["frontier_label"]["source"], "repo-stage-generator-heuristic")
            self.assertEqual(trace["provenance"]["source_kind"], "repo-backed-stage-spec")
            self.assertIn("not a live agent trajectory", " ".join(trace["provenance"]["notes"]))

    def test_json_spec_preserves_explicit_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            spec = tmp / "spec.json"
            out = tmp / "trace.json"
            spec.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.repo_stage_spec.v1",
                        "instance_id": "spec-case",
                        "commands": [
                            {"id": "inspect", "stage": "INSPECT", "cmd": "ls"},
                            {"id": "build", "stage": "BUILD", "cmd": "make all", "allow_failure": True},
                            {
                                "id": "test",
                                "stage": "TEST",
                                "cmd": "pytest",
                                "critical": True,
                                "frontier_label": {"source": "manual-fixture"},
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.run_generator(
                "--repo-dir",
                str(repo),
                "--output",
                str(out),
                "--spec",
                str(spec),
                "--created-at",
                "2026-07-20T00:00:00+00:00",
            )

            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["instance_id"], "spec-case")
            self.assertTrue(trace["commands"][1]["allow_failure"])
            self.assertTrue(trace["commands"][2]["critical"])
            self.assertEqual(trace["commands"][2]["frontier_label"], {"source": "manual-fixture"})
            self.assertEqual(trace["provenance"]["input_file"], str(spec.resolve()))
            self.assertTrue(trace["provenance"]["input_sha256"])

    def test_builtin_flowcausal_python_preset_marks_build_and_package(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            (repo / "experiments").mkdir(parents=True)
            (repo / "src" / "controller").mkdir(parents=True)
            out = tmp / "trace.json"

            self.run_generator(
                "--repo-dir",
                str(repo),
                "--output",
                str(out),
                "--preset",
                "flowcausal-python",
                "--created-at",
                "2026-07-20T00:00:00+00:00",
            )

            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["instance_id"], "flowcausal-python")
            stages = [cmd["stage"] for cmd in trace["commands"]]
            self.assertIn("BUILD", stages)
            self.assertIn("TEST", stages)
            self.assertIn("PACKAGE_BACKGROUND", stages)
            self.assertTrue(any(cmd.get("critical") for cmd in trace["commands"]))
            self.assertEqual(trace["provenance"]["preset"], "flowcausal-python")

    def test_builtin_breadth_presets_are_available_and_mark_foreground_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            for preset in (
                "flowcausal-controller-ci",
                "flowcausal-flowbench-c",
                "flowcausal-flowbench-pressure",
                "flowcausal-flowbench-stress",
                "flowcausal-paper-tables",
            ):
                out = tmp / f"{preset}.json"
                summary = self.run_generator(
                    "--repo-dir",
                    str(repo),
                    "--output",
                    str(out),
                    "--preset",
                    preset,
                    "--created-at",
                    "2026-07-20T00:00:00+00:00",
                )
                self.assertEqual(summary["instance_id"], preset)
                trace = json.loads(out.read_text(encoding="utf-8"))
                stages = [cmd["stage"] for cmd in trace["commands"]]
                self.assertIn("BUILD", stages)
                self.assertIn("TEST", stages)
                self.assertIn("PACKAGE_BACKGROUND", stages)
                critical = [cmd for cmd in trace["commands"] if cmd.get("critical")]
                self.assertEqual(len(critical), 1)
                self.assertEqual(critical[0]["frontier_label"]["source"], "repo-stage-preset-heuristic")
                self.assertEqual(trace["provenance"]["preset"], preset)


if __name__ == "__main__":
    unittest.main()

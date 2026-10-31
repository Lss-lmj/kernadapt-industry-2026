#!/usr/bin/env python3
"""Tests for matrix-level system profile normalization."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "summarize_harness_replay_profiles.py"


def load_module():
    spec = importlib.util.spec_from_file_location("summarize_harness_replay_profiles", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


summary_mod = load_module()


def write_profile(path: pathlib.Path, *, wait: int, runtime: int, samples: int, elapsed: float, migrations: int) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": "flowcausal.harness_replay_system_profile.v1",
                "profile_level": "S1: process-tree profile with schedstat deltas",
                "sample_count": samples,
                "sampled_elapsed_sec": elapsed,
                "samples_with_schedstat": samples,
                "max_process_count": 4,
                "max_leaf_count": 2,
                "unique_pids_seen": 8,
                "cpu_migration_events": migrations,
                "aggregate_delta": {
                    "sched_wait_delta_ns": wait,
                    "sched_runtime_delta_ns": runtime,
                },
                "stage_delta_summary": {
                    "TEST": {
                        "sample_count": samples // 2,
                        "aggregate_delta": {
                            "sched_wait_delta_ns": wait // 2,
                            "sched_runtime_delta_ns": runtime // 2,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


class SummarizeHarnessReplayProfilesTests(unittest.TestCase):
    def test_summarize_matrix_normalizes_profile_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            default_profile = tmp / "default-profile.json"
            flow_profile = tmp / "flow-profile.json"
            write_profile(default_profile, wait=100, runtime=50, samples=10, elapsed=2.0, migrations=4)
            write_profile(flow_profile, wait=150, runtime=75, samples=10, elapsed=3.0, migrations=3)
            matrix = tmp / "matrix.json"
            matrix.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.harness_replay_matrix.v1",
                        "variants": [
                            {
                                "name": "default",
                                "kind": "baseline",
                                "trials": [
                                    {
                                        "trial": 1,
                                        "cases": [
                                            {
                                                "instance_id": "case-a",
                                                "run_dir": str(tmp / "default-run"),
                                                "system_profile": {
                                                    "enabled": True,
                                                    "summary": str(default_profile),
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                            {
                                "name": "flowcausal_attached",
                                "kind": "flowcausal",
                                "trials": [
                                    {
                                        "trial": 1,
                                        "cases": [
                                            {
                                                "instance_id": "case-a",
                                                "run_dir": str(tmp / "flow-run"),
                                                "system_profile": {
                                                    "enabled": True,
                                                    "summary": str(flow_profile),
                                                },
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            payload = summary_mod.summarize_matrix(matrix)

            self.assertEqual(payload["schema"], "flowcausal.harness_replay_profile_matrix_summary.v1")
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["profile_count"], 2)
            default_summary = payload["variants"][0]["summary"]
            self.assertEqual(default_summary["metrics"]["sched_wait_per_elapsed_sec"]["mean"], 50.0)
            self.assertEqual(default_summary["metrics"]["sched_wait_per_sample_ns"]["mean"], 10.0)
            self.assertEqual(default_summary["metrics"]["cpu_migration_per_sample"]["mean"], 0.4)
            self.assertEqual(default_summary["stage"]["TEST"]["sched_wait_per_stage_sample_ns_mean"], 10.0)
            comparison = payload["variants"][1]["comparison_to_baseline"]
            self.assertEqual(comparison["sched_wait_per_elapsed_sec"]["mean_delta"], 0.0)
            self.assertEqual(comparison["sched_wait_per_sample_ns"]["mean_delta"], 0.5)

    def test_cli_writes_json_markdown_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            profile = tmp / "profile.json"
            write_profile(profile, wait=100, runtime=50, samples=10, elapsed=2.0, migrations=4)
            matrix = tmp / "matrix.json"
            matrix.write_text(
                json.dumps(
                    {
                        "variants": [
                            {
                                "name": "default",
                                "trials": [
                                    {
                                        "trial": 1,
                                        "cases": [
                                            {
                                                "instance_id": "case-a",
                                                "system_profile": {"enabled": True, "summary": str(profile)},
                                            }
                                        ],
                                    }
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            output = tmp / "summary.json"
            markdown = tmp / "summary.md"
            csv_path = tmp / "summary.csv"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--matrix-summary",
                    str(matrix),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--csv-output",
                    str(csv_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(output.is_file())
            self.assertTrue(markdown.is_file())
            self.assertTrue(csv_path.is_file())
            self.assertIn("diagnostic only", markdown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

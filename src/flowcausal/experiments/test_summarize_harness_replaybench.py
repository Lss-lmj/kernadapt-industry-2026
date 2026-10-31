#!/usr/bin/env python3
"""Tests for HarnessReplayBench workload summary."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SUMMARY = HERE / "summarize_harness_replaybench.py"


def load_summary_module():
    spec = importlib.util.spec_from_file_location("summarize_harness_replaybench", SUMMARY)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SUMMARY}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


summary_mod = load_summary_module()


class HarnessReplayBenchSummaryTests(unittest.TestCase):
    def test_cli_summarizes_trace_and_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text(
                json.dumps(
                    {
                        "messages": [
                            {"role": "user", "created_at": "2026-07-20T00:00:00+00:00"},
                            {"role": "assistant", "created_at": "2026-07-20T00:00:10+00:00"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "case-a",
                        "source": "mini-swe-agent-real-capture",
                        "source_format": "mini-swe-agent",
                        "provenance": {"input_file": str(raw), "input_sha256": "abc"},
                        "commands": [
                            {
                                "id": "a",
                                "cmd": "rg foo",
                                "stage": "INSPECT",
                                "turn": 1,
                                "observed_returncode": 0,
                                "duration_sec": 2.0,
                            },
                            {
                                "id": "b",
                                "cmd": "pytest tests/test_a.py",
                                "stage": "TEST",
                                "turn": 2,
                                "observed_returncode": 1,
                                "allow_failure": True,
                                "critical": True,
                                "duration_sec": 3.0,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix.json"
            matrix.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.harness_replay_matrix.v1",
                        "status": "pass",
                        "trials_per_variant": 1,
                        "variants": [
                            {
                                "name": "default",
                                "kind": "baseline",
                                "summary": {
                                    "pass_trials": 1,
                                    "trials": 1,
                                    "makespan_mean_sec": 10.0,
                                    "mean_critical_mean_sec": 6.0,
                                    "p95_critical_mean_sec": 6.0,
                                },
                                "comparison_to_baseline": {
                                    "makespan_delta": 0.0,
                                    "mean_critical_delta": 0.0,
                                },
                                "trials": [
                                    {
                                        "status": "pass",
                                        "cases": [
                                            {
                                                "instance_id": "case-a",
                                                "parsed": {
                                                    "command_count": 2,
                                                    "stage_seconds": {"INSPECT": 1.5, "TEST": 4.5},
                                                },
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            mix = tmp / "mix.json"
            mix.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.harness_replay_mix.v1",
                        "status": "pass",
                        "makespan_sec": 7.0,
                        "aggregate": {"mean_critical_sec": 4.0},
                        "cases": [
                            {
                                "instance_id": "case-a",
                                "returncode": 0,
                                "parsed": {
                                    "command_count": 2,
                                    "critical_sec": 4.0,
                                    "stage_counts": {"INSPECT": 1, "TEST": 1},
                                    "stage_seconds": {"INSPECT": 1.0, "TEST": 3.0},
                                },
                                "system_profile": {
                                    "enabled": True,
                                    "samples": 10,
                                    "max_process_count": 4,
                                    "unique_pids_seen": 6,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            output = tmp / "summary.json"
            markdown = tmp / "summary.md"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SUMMARY),
                    "--trace",
                    str(trace),
                    "--mix-summary",
                    str(mix),
                    "--matrix-summary",
                    str(matrix),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "flowcausal.harness_replaybench_summary.v1")
            trace_summary = data["traces"][0]
            self.assertEqual(trace_summary["command_count"], 2)
            self.assertEqual(trace_summary["stage_counts"], {"INSPECT": 1, "TEST": 1})
            self.assertEqual(trace_summary["observed_returncode_counts"], {"0": 1, "1": 1})
            self.assertEqual(trace_summary["observed_nonzero_command_count"], 1)
            self.assertEqual(trace_summary["allow_failure_count"], 1)
            self.assertEqual(trace_summary["timing_status"], "pass")
            self.assertEqual(trace_summary["tool_execution_sec"], 5.0)
            self.assertEqual(trace_summary["raw_timing"]["timestamp_count"], 2)
            self.assertEqual(trace_summary["raw_timing"]["llm_timestamp_span_sec"], 10.0)
            self.assertEqual(trace_summary["tool_execution_share_of_raw_span"], 0.5)
            mix_summary = data["mixes"][0]
            self.assertEqual(mix_summary["case_count"], 1)
            self.assertEqual(mix_summary["stage_seconds_total"], {"INSPECT": 1.0, "TEST": 3.0})
            self.assertEqual(mix_summary["system_profile_max_process_count_max"], 4.0)
            matrix_summary = data["matrices"][0]["variants"][0]
            self.assertEqual(matrix_summary["stage_seconds_mean_per_trial"], {"INSPECT": 1.5, "TEST": 4.5})
            self.assertTrue(markdown.is_file())
            self.assertIn("case-a", markdown.read_text(encoding="utf-8"))

    def test_missing_timestamps_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text(json.dumps({"messages": [{"role": "assistant"}]}), encoding="utf-8")
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "case-b",
                        "provenance": {"input_file": str(raw)},
                        "commands": [{"cmd": "pytest x.py", "stage": "TEST"}],
                    }
                ),
                encoding="utf-8",
            )
            summary = summary_mod.summarize_trace(trace, raw_root=None)
            self.assertEqual(summary["timing_status"], "missing_timestamps")
            self.assertIsNone(summary["tool_execution_share_of_raw_span"])


if __name__ == "__main__":
    unittest.main()

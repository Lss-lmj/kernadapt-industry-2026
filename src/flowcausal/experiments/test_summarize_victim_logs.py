#!/usr/bin/env python3
"""Tests for matrix-level victim latency aggregation."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE / "summarize_victim_logs.py"


def load_module():
    spec = importlib.util.spec_from_file_location("summarize_victim_logs", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


summary_mod = load_module()


def write_victim_log(path: pathlib.Path, *, p95: float, p99: float, max_ms: float, mean_ms: float) -> None:
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "victim_begin",
                        "pid": 123,
                        "workers": 1,
                        "duration_sec": 1.0,
                        "period_ms": 10.0,
                        "work_us": 500.0,
                        "emit_samples": False,
                    }
                ),
                json.dumps({"type": "victim_worker_summary", "worker": 0, "pid": 124, "iterations": 10, "checksum": 7}),
                json.dumps(
                    {
                        "type": "victim_summary",
                        "pid": 123,
                        "workers": 1,
                        "iterations": 10,
                        "returncode": 0,
                        "lateness": {
                            "count": 10.0,
                            "mean_ms": mean_ms,
                            "p50_ms": mean_ms / 2.0,
                            "p95_ms": p95,
                            "p99_ms": p99,
                            "max_ms": max_ms,
                        },
                        "runtime": {
                            "count": 10.0,
                            "mean_ms": 0.5,
                            "p50_ms": 0.5,
                            "p95_ms": 0.6,
                            "p99_ms": 0.7,
                            "max_ms": 0.8,
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class SummarizeVictimLogsTests(unittest.TestCase):
    def test_summarize_matrix_normalizes_victim_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            default_log = tmp / "default-victim.log"
            flow_log = tmp / "flow-victim.log"
            write_victim_log(default_log, p95=2.0, p99=3.0, max_ms=4.0, mean_ms=1.0)
            write_victim_log(flow_log, p95=4.0, p99=6.0, max_ms=8.0, mean_ms=2.0)
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
                                        "live_wrapper": {"victim_log": str(default_log)},
                                    }
                                ],
                            },
                            {
                                "name": "flowcausal_attached",
                                "trials": [
                                    {
                                        "trial": 1,
                                        "live_wrapper": {"victim_log": str(flow_log)},
                                    }
                                ],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            payload = summary_mod.summarize_matrix(matrix)

            self.assertEqual(payload["schema"], "flowcausal.victim_latency_matrix_summary.v1")
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(payload["victim_runs"], 2)
            default_summary = payload["variants"][0]["summary"]
            self.assertEqual(default_summary["metrics"]["lateness_p95_ms"]["mean"], 2.0)
            self.assertEqual(default_summary["metrics"]["lateness_max_ms"]["mean"], 4.0)
            self.assertEqual(payload["variants"][1]["comparison_to_baseline"]["lateness_p95_ms"]["mean_delta"], 1.0)

    def test_cli_writes_json_markdown_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            log = tmp / "victim.log"
            write_victim_log(log, p95=2.0, p99=3.0, max_ms=4.0, mean_ms=1.0)
            matrix = tmp / "matrix.json"
            matrix.write_text(
                json.dumps({"variants": [{"name": "default", "trials": [{"trial": 1, "live_wrapper": {"victim_log": str(log)}}]}]}),
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
            self.assertIn("safety diagnostic", markdown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

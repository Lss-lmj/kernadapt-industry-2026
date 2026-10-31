#!/usr/bin/env python3
"""Tests for full-harness endpoint result normalization."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
NORMALIZER = HERE / "normalize_full_harness_result.py"


class NormalizeFullHarnessResultTests(unittest.TestCase):
    def normalize(self, input_path: pathlib.Path, output_path: pathlib.Path, *extra: str) -> dict:
        proc = subprocess.run(
            [
                sys.executable,
                str(NORMALIZER),
                "--input",
                str(input_path),
                "--output",
                str(output_path),
                "--created-at",
                "2026-01-01T00:00:00+00:00",
                *extra,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(output_path.read_text(encoding="utf-8"))

    def test_swe_bench_resolved_id_summary_selects_requested_instance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "swebench_report.json"
            raw.write_text(
                json.dumps(
                    {
                        "resolved_ids": ["django__issue-1"],
                        "unresolved_ids": ["sympy__issue-2"],
                    }
                ),
                encoding="utf-8",
            )
            out = tmp / "endpoint.json"

            endpoint = self.normalize(
                raw,
                out,
                "--instance-id",
                "django__issue-1",
                "--scheduler-variant",
                "flowcausal_width",
            )

            self.assertEqual(endpoint["schema"], "flowcausal.full_harness_endpoint.v1")
            self.assertEqual(endpoint["source_format"], "swe-bench")
            self.assertEqual(endpoint["instance_id"], "django__issue-1")
            self.assertTrue(endpoint["resolved"])
            self.assertEqual(endpoint["scheduler_variant"], "flowcausal_width")
            self.assertEqual(endpoint["provenance"]["candidate_count"], 2)
            self.assertIn("input_sha256", endpoint["provenance"])

    def test_openhands_jsonl_row_preserves_elapsed_and_test_counts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "openhands_output.jsonl"
            raw.write_text(
                json.dumps(
                    {
                        "instance_id": "astropy__issue-3",
                        "success": True,
                        "metrics": {"duration_sec": 42.5},
                        "test_status": {
                            "FAIL_TO_PASS": {"success": ["test_a"], "failure": []},
                            "PASS_TO_PASS": {"success": ["test_b", "test_c"]},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            out = tmp / "endpoint.json"

            endpoint = self.normalize(raw, out, "--format", "openhands")

            self.assertEqual(endpoint["source_format"], "openhands")
            self.assertEqual(endpoint["instance_id"], "astropy__issue-3")
            self.assertTrue(endpoint["resolved"])
            self.assertEqual(endpoint["elapsed_sec"], 42.5)
            self.assertEqual(endpoint["tests"]["fail_to_pass_success_count"], 1)
            self.assertEqual(endpoint["tests"]["pass_to_pass_success_count"], 2)

    def test_multi_instance_input_requires_instance_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "multi.json"
            raw.write_text(
                json.dumps(
                    [
                        {"instance_id": "django__issue-1", "resolved": True},
                        {"instance_id": "sympy__issue-2", "resolved": False},
                    ]
                ),
                encoding="utf-8",
            )
            out = tmp / "endpoint.json"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(NORMALIZER),
                    "--input",
                    str(raw),
                    "--output",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("--instance-id", proc.stderr)


if __name__ == "__main__":
    unittest.main()

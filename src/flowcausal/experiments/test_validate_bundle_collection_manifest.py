#!/usr/bin/env python3
"""Tests for bundle collection manifest preflight."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
VALIDATE = HERE / "validate_bundle_collection_manifest.py"


def matrix_summary(trace_path: pathlib.Path, instance_id: str) -> dict:
    trial = {
        "workload": {
            "workload_begin": {
                "trace_file": str(trace_path),
                "instance_id": instance_id,
                "command_count": 2,
            }
        }
    }
    return {
        "config": {"command": f"python3 agent_trace_workflow.py --trace-file {trace_path}"},
        "variants": [{"name": "default_taskset", "trials": [trial]}],
    }


class ValidateBundleCollectionManifestTests(unittest.TestCase):
    def write_inputs(self, tmp: pathlib.Path, instance_id: str) -> dict[str, str]:
        raw = tmp / f"{instance_id}.traj"
        raw.write_text("{}", encoding="utf-8")
        full = tmp / f"{instance_id}.full.json"
        full.write_text("{}", encoding="utf-8")
        labels = tmp / f"{instance_id}.labels.json"
        labels.write_text('{"labels":[]}', encoding="utf-8")
        repo = tmp / f"{instance_id}.repo"
        repo.mkdir()
        return {
            "raw_agent_output": raw.name,
            "full_harness_result": full.name,
            "frontier_annotations": labels.name,
            "repo_dir": repo.name,
        }

    def run_validate(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(VALIDATE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_preflight_passes_with_existing_paths_and_matching_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out_dir = tmp / "collection"
            instance_id = "django__issue-1"
            paths = self.write_inputs(tmp, instance_id)
            expected_trace = out_dir / "bundles" / instance_id / "traces" / f"{instance_id}.annotated.flowcausal-trace.json"
            matrix = tmp / "matrix.json"
            matrix.write_text(json.dumps(matrix_summary(expected_trace, instance_id)), encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps({"instances": [{"instance_id": instance_id, **paths, "matrix_summary": matrix.name}]}),
                encoding="utf-8",
            )

            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(out_dir))

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["summary"]["instances"], 1)
            self.assertIn("matrix trace references match", " ".join(report["instances"][0]["evidence"]))

    def test_missing_optional_artifacts_are_partial_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            paths = self.write_inputs(tmp, "sympy__issue-2")
            paths.pop("frontier_annotations")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps({"instances": [{"instance_id": "sympy__issue-2", **paths}]}),
                encoding="utf-8",
            )

            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "partial")
            self.assertIn("frontier_annotations missing", " ".join(report["warnings"]))
            self.assertIn("matrix_summary missing", " ".join(report["warnings"]))

    def test_strict_mode_and_duplicates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            paths = self.write_inputs(tmp, "astropy__issue-3")
            paths.pop("frontier_annotations")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "instances": [
                            {"instance_id": "astropy__issue-3", **paths},
                            {"instance_id": "astropy__issue-3", **paths},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = self.run_validate(
                "--manifest",
                str(manifest),
                "--out-dir",
                str(tmp / "collection"),
                "--require-frontier-annotations",
                "--require-matrix-summary",
            )

            self.assertEqual(report["status"], "fail")
            blockers = " ".join(report["blockers"])
            self.assertIn("missing frontier_annotations", blockers)
            self.assertIn("missing matrix_summary", blockers)
            self.assertIn("duplicate instance_id", blockers)

    def test_matrix_trace_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance_id = "django__issue-4"
            paths = self.write_inputs(tmp, instance_id)
            matrix = tmp / "matrix.json"
            matrix.write_text(json.dumps(matrix_summary(tmp / "wrong-trace.json", instance_id)), encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps({"instances": [{"instance_id": instance_id, **paths, "matrix_summary": matrix.name}]}),
                encoding="utf-8",
            )

            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "fail")
            self.assertIn("does not match expected bundle trace", " ".join(report["blockers"]))

    def test_optional_victim_and_live_paths_are_checked_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance_id = "django__issue-5"
            paths = self.write_inputs(tmp, instance_id)
            live_dir = tmp / "live"
            live_dir.mkdir()
            victim = live_dir / "victim.log"
            victim.write_text('{"type":"victim_summary","iterations":1,"returncode":0,"lateness":{"p99_ms":1.0,"max_ms":2.0}}\n', encoding="utf-8")
            harness_summary = tmp / "harness-summary.json"
            harness_summary.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.real_agent_harness_matrix.v1",
                        "trials": [
                            {
                                "variant": "flowcausal_width",
                                "trial": 2,
                                "raw_agent_output": str(tmp / paths["raw_agent_output"]),
                                "full_harness_result": str(tmp / paths["full_harness_result"]),
                                "victim_log": str(victim),
                                "live_dir": str(live_dir),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            harness_audit = tmp / "harness-audit.json"
            harness_audit.write_text('{"schema":"flowcausal.real_agent_harness_matrix_audit.v1"}', encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "instances": [
                            {
                                "instance_id": instance_id,
                                **paths,
                                "victim_log": "live/victim.log",
                                "live_dir": "live",
                                "harness_matrix_summary": "harness-summary.json",
                                "harness_matrix_audit": "harness-audit.json",
                                "harness_matrix_source_variant": "flowcausal_width",
                                "harness_matrix_source_trial": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "partial")
            evidence = " ".join(report["instances"][0]["evidence"])
            self.assertIn("victim_log exists", evidence)
            self.assertIn("live_dir exists", evidence)
            self.assertIn("harness_matrix_summary exists", evidence)
            self.assertIn("harness_matrix_audit exists", evidence)
            self.assertIn("harness matrix selected trial found", evidence)
            self.assertIn("harness selected trial victim_log matches", evidence)

            victim.unlink()
            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "fail")
            self.assertIn("victim_log path does not exist", " ".join(report["blockers"]))

            victim.write_text("{}", encoding="utf-8")
            harness_audit.unlink()
            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "fail")
            self.assertIn("harness_matrix_audit path does not exist", " ".join(report["blockers"]))

    def test_harness_matrix_artifacts_require_source_variant_and_trial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance_id = "django__issue-6"
            paths = self.write_inputs(tmp, instance_id)
            harness_summary = tmp / "harness-summary.json"
            harness_summary.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.real_agent_harness_matrix.v1",
                        "trials": [
                            {
                                "variant": "flowcausal_width",
                                "trial": 2,
                                "raw_agent_output": str(tmp / paths["raw_agent_output"]),
                                "full_harness_result": str(tmp / paths["full_harness_result"]),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            harness_audit = tmp / "harness-audit.json"
            harness_audit.write_text('{"schema":"flowcausal.real_agent_harness_matrix_audit.v1"}', encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "instances": [
                            {
                                "instance_id": instance_id,
                                **paths,
                                "harness_matrix_summary": "harness-summary.json",
                                "harness_matrix_audit": "harness-audit.json",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(report["status"], "fail")
            blockers = " ".join(report["blockers"])
            self.assertIn("missing harness_matrix_source_variant", blockers)
            self.assertIn("missing harness_matrix_source_trial", blockers)

            manifest.write_text(
                json.dumps(
                    {
                        "instances": [
                            {
                                "instance_id": instance_id,
                                **paths,
                                "harness_matrix_summary": "harness-summary.json",
                                "harness_matrix_audit": "harness-audit.json",
                                "harness_matrix_source_variant": "flowcausal_width",
                                "harness_matrix_source_trial": 99,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            missing_trial = self.run_validate("--manifest", str(manifest), "--out-dir", str(tmp / "collection"))

            self.assertEqual(missing_trial["status"], "fail")
            self.assertIn("selected trial not found", " ".join(missing_trial["blockers"]))


if __name__ == "__main__":
    unittest.main()

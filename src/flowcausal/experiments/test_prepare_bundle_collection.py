#!/usr/bin/env python3
"""Tests for batch real-agent bundle preparation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PREPARE = HERE / "prepare_bundle_collection.py"
GATES = (
    "frontier correctness",
    "endpoint improvement",
    "scheduler mechanism",
    "safety/fairness",
    "baseline comparison",
    "reproducibility/artifact",
)


def matrix_summary(trace_path: pathlib.Path, instance_id: str) -> dict:
    trial = {
        "workload": {
            "workload_begin": {
                "workload": "agent_trace_workflow",
                "trace_file": str(trace_path),
                "instance_id": instance_id,
                "command_count": 2,
            },
            "stage_records": [
                {"stage": "TEST", "elapsed_sec": 1.0, "command_index": 1, "source_id": "test"}
            ],
        }
    }
    return {
        "config": {"command": f"python3 agent_trace_workflow.py --trace-file {trace_path}"},
        "variants": [
            {"name": "default_taskset", "kind": "default", "aggregate": {"critical_mean_sec": 10.0}, "trials": [trial] * 3},
            {"name": "flowcausal_width", "kind": "flowcausal", "aggregate": {"critical_mean_sec": 8.0}, "trials": [trial] * 3},
            {"name": "static_cgroup", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.8}, "trials": []},
            {"name": "nice_priority", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.6}, "trials": []},
            {"name": "static_affinity", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.7}, "trials": []},
            {"name": "oracle_frontier", "kind": "baseline", "aggregate": {"critical_mean_sec": 7.5}, "trials": []},
        ],
        "claim_gate_audit": {
            "highest_supported_level": "L5 fixture",
            "gates": [
                {"name": name, "status": "pass", "evidence": [], "blockers": []}
                for name in (
                    "stage/episode outcome",
                    "controller/BPF admission",
                    "direct CPU mechanism",
                    "victim/fairness safety",
                )
            ],
        },
    }


class PrepareBundleCollectionTests(unittest.TestCase):
    def write_instance_inputs(self, root: pathlib.Path, instance_id: str) -> dict[str, str]:
        raw = root / f"{instance_id}.traj"
        raw.write_text(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "trajectory": [
                        {"id": "inspect", "action": "rg target ."},
                        {"id": "test", "action": "pytest tests/test_target.py"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        labels = root / f"{instance_id}.labels.json"
        labels.write_text(
            json.dumps({"labels": [{"command_index": 1, "source_id": "test", "confidence": 100}]}),
            encoding="utf-8",
        )
        full = root / f"{instance_id}.full.json"
        full.write_text(json.dumps({"instance_id": instance_id, "resolved": True, "elapsed_sec": 30.0}), encoding="utf-8")
        repo = root / f"{instance_id}.repo"
        repo.mkdir()
        return {
            "raw_agent_output": raw.name,
            "frontier_annotations": labels.name,
            "full_harness_result": full.name,
            "repo_dir": repo.name,
        }

    def run_prepare(self, *args: str) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(PREPARE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def test_manifest_defaults_prepare_bundles_and_collection_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out_dir = tmp / "collection"
            instance_id = "django__issue-1"
            paths = self.write_instance_inputs(tmp, instance_id)
            annotated = out_dir / "bundles" / instance_id / "traces" / f"{instance_id}.annotated.flowcausal-trace.json"
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(annotated, instance_id)), encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "defaults": {
                            "raw_format": "swe-agent",
                            "source": "swe-agent-fixture",
                            "taskset": "0-1",
                            "created_at": "2026-01-01T00:00:00+00:00",
                        },
                        "instances": [
                            {
                                "instance_id": instance_id,
                                **paths,
                                "matrix_summary": matrix.name,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.run_prepare(
                "--manifest",
                str(manifest),
                "--out-dir",
                str(out_dir),
                "--overwrite",
                "--min-ready-bundles",
                "1",
            )

            collection = json.loads((out_dir / "collection-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(collection["status"], "pass", collection["blockers"])
            batch_manifest = json.loads((out_dir / "bundle-collection-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(batch_manifest["instances"][0]["instance_id"], instance_id)
            self.assertTrue(batch_manifest["instances"][0]["expected_audit"].endswith("agent-trace-bundle-audit.json"))
            self.assertTrue(batch_manifest["instances"][0]["expected_audit_markdown"].endswith("agent-trace-bundle-audit.md"))
            prepare_all = (out_dir / "prepare-all.sh").read_text(encoding="utf-8")
            self.assertIn("--taskset 0-1", prepare_all)
            self.assertIn("--matrix-summary", prepare_all)

    def test_generate_only_writes_scripts_without_running_children(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out_dir = tmp / "collection"
            paths = self.write_instance_inputs(tmp, "sympy__issue-2")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "defaults": {"raw_format": "swe-agent"},
                        "instances": [{"instance_id": "sympy__issue-2", **paths}],
                    }
                ),
                encoding="utf-8",
            )

            self.run_prepare(
                "--manifest",
                str(manifest),
                "--out-dir",
                str(out_dir),
                "--generate-only",
            )

            self.assertTrue((out_dir / "prepare-all.sh").exists())
            self.assertTrue((out_dir / "audit-collection.sh").exists())
            self.assertFalse((out_dir / "collection-audit.json").exists())
            batch_manifest = json.loads((out_dir / "bundle-collection-manifest.json").read_text(encoding="utf-8"))
            self.assertTrue(batch_manifest["generate_only"])

    def test_generate_only_can_archive_manifest_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out_dir = tmp / "collection"
            paths = self.write_instance_inputs(tmp, "pytest__issue-3")
            live_dir = tmp / "live"
            live_dir.mkdir()
            victim = live_dir / "victim.log"
            victim.write_text('{"type":"victim_summary","iterations":1,"returncode":0,"lateness":{"p99_ms":1.0,"max_ms":2.0}}\n', encoding="utf-8")
            harness_summary = tmp / "harness-matrix-summary.json"
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
            harness_audit = tmp / "harness-matrix-audit.json"
            harness_audit.write_text('{"schema":"flowcausal.real_agent_harness_matrix_audit.v1"}', encoding="utf-8")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "defaults": {"raw_format": "swe-agent"},
                        "instances": [
                            {
                                "instance_id": "pytest__issue-3",
                                **paths,
                                "victim_log": "live/victim.log",
                                "live_dir": "live",
                                "harness_matrix_summary": "harness-matrix-summary.json",
                                "harness_matrix_audit": "harness-matrix-audit.json",
                                "harness_matrix_source_variant": "flowcausal_width",
                                "harness_matrix_source_trial": 2,
                                "harness_matrix_trial_selection": "median-elapsed",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            preflight = out_dir / "manifest-preflight.json"

            self.run_prepare(
                "--manifest",
                str(manifest),
                "--out-dir",
                str(out_dir),
                "--generate-only",
                "--preflight-output",
                str(preflight),
            )

            report = json.loads(preflight.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "partial")
            self.assertTrue(any("matrix_summary missing" in item for item in report["warnings"]))
            evidence = " ".join(report["instances"][0]["evidence"])
            self.assertIn("victim_log exists", evidence)
            self.assertIn("live_dir exists", evidence)
            self.assertIn("harness_matrix_summary exists", evidence)
            self.assertIn("harness_matrix_audit exists", evidence)
            self.assertIn("harness matrix selected trial found", evidence)
            self.assertIn("harness selected trial victim_log matches", evidence)
            batch_manifest = json.loads((out_dir / "bundle-collection-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(batch_manifest["artifacts"]["manifest_preflight_json"], str(preflight))
            self.assertEqual(batch_manifest["preflight"]["schema"], "flowcausal.bundle_collection_manifest_preflight.v1")
            instance = batch_manifest["instances"][0]
            self.assertTrue(instance["expected_audit_markdown"].endswith("agent-trace-bundle-audit.md"))
            self.assertTrue(pathlib.Path(instance["source_artifacts"]["victim_log"]).exists())
            self.assertTrue(pathlib.Path(instance["source_artifacts"]["live_dir"]).is_dir())
            self.assertTrue(pathlib.Path(instance["source_artifacts"]["harness_matrix_summary"]).exists())
            self.assertTrue(pathlib.Path(instance["source_artifacts"]["harness_matrix_audit"]).exists())
            self.assertEqual(instance["harness_matrix_provenance"]["harness_matrix_source_variant"], "flowcausal_width")
            self.assertEqual(instance["harness_matrix_provenance"]["harness_matrix_source_trial"], 2)
            prepare_all = (out_dir / "prepare-all.sh").read_text(encoding="utf-8")
            self.assertIn("--harness-matrix-summary", prepare_all)
            self.assertIn("--harness-matrix-audit", prepare_all)
            self.assertIn("--victim-log", prepare_all)
            self.assertIn("--live-dir", prepare_all)
            self.assertIn("--harness-matrix-source-variant flowcausal_width", prepare_all)
            self.assertIn("--harness-matrix-source-trial 2", prepare_all)

    def test_strict_preflight_blocks_missing_matrix_summary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out_dir = tmp / "collection"
            paths = self.write_instance_inputs(tmp, "astropy__issue-4")
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "defaults": {"raw_format": "swe-agent"},
                        "instances": [{"instance_id": "astropy__issue-4", **paths}],
                    }
                ),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE),
                    "--manifest",
                    str(manifest),
                    "--out-dir",
                    str(out_dir),
                    "--generate-only",
                    "--strict-preflight",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(proc.returncode, 0)
            report_path = out_dir / "manifest-preflight.strict.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("missing matrix_summary" in item for item in report["blockers"]))
            self.assertFalse((out_dir / "prepare-all.sh").exists())


if __name__ == "__main__":
    unittest.main()

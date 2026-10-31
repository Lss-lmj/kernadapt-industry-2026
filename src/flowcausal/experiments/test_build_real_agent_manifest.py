#!/usr/bin/env python3
"""Tests for real-agent artifact intake manifest generation."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
BUILD = HERE / "build_real_agent_manifest.py"


def matrix_summary(instance_id: str) -> dict:
    return {
        "variants": [
            {
                "name": "default_taskset",
                "trials": [
                    {
                        "workload": {
                            "workload_begin": {
                                "trace_file": f"traces/{instance_id}.annotated.flowcausal-trace.json",
                                "instance_id": instance_id,
                            }
                        }
                    }
                ],
            }
        ]
    }


class BuildRealAgentManifestTests(unittest.TestCase):
    def run_build(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(BUILD), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return proc

    def test_builds_manifest_from_complete_real_agent_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw_dir = tmp / "raw"
            repo_root = tmp / "repos"
            endpoint_dir = tmp / "endpoints"
            label_dir = tmp / "labels"
            matrix_dir = tmp / "matrices"
            for path in (raw_dir, repo_root, endpoint_dir, label_dir, matrix_dir):
                path.mkdir()
            instance_id = "django__django-12345"
            (raw_dir / f"{instance_id}.traj").write_text(
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
            (repo_root / instance_id).mkdir()
            (endpoint_dir / "swebench-results.json").write_text(
                json.dumps({"resolved_ids": [instance_id], "unresolved_ids": ["sympy__sympy-1"]}),
                encoding="utf-8",
            )
            (label_dir / f"{instance_id}.frontier-labels.json").write_text(
                json.dumps({"instance_id": instance_id, "labels": [{"command_index": 1, "enabled": True}]}),
                encoding="utf-8",
            )
            (matrix_dir / "matrix-summary.json").write_text(json.dumps(matrix_summary(instance_id)), encoding="utf-8")
            output = tmp / "collection" / "real-agent-manifest.json"
            report = tmp / "collection" / "intake-report.json"

            self.run_build(
                "--raw-dir",
                str(raw_dir),
                "--repo-root",
                str(repo_root),
                "--endpoint-dir",
                str(endpoint_dir),
                "--label-dir",
                str(label_dir),
                "--matrix-dir",
                str(matrix_dir),
                "--output",
                str(output),
                "--report",
                str(report),
                "--source",
                "swe-agent",
                "--agent-repo-root",
                "/testbed",
                "--taskset",
                "0-1",
                "--require-frontier-annotations",
                "--require-matrix-summary",
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["defaults"]["source"], "swe-agent")
            self.assertEqual(manifest["defaults"]["agent_repo_root"], "/testbed")
            self.assertEqual(manifest["defaults"]["taskset"], "0-1")
            self.assertEqual(len(manifest["instances"]), 1)
            row = manifest["instances"][0]
            self.assertEqual(row["instance_id"], instance_id)
            self.assertEqual(row["raw_format"], "swe-agent")
            self.assertTrue(row["raw_agent_output"].endswith(f"raw/{instance_id}.traj"))
            self.assertTrue(row["repo_dir"].endswith(f"repos/{instance_id}"))
            self.assertTrue(row["full_harness_result"].endswith("endpoints/swebench-results.json"))
            self.assertTrue(row["frontier_annotations"].endswith(f"labels/{instance_id}.frontier-labels.json"))
            self.assertTrue(row["matrix_summary"].endswith("matrices/matrix-summary.json"))

            intake = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(intake["status"], "pass", intake["instances"][0]["blockers"])
            self.assertEqual(intake["summary"]["ready_instances"], 1)

    def test_missing_endpoint_excludes_instance_and_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw_dir = tmp / "raw"
            repo_root = tmp / "repos"
            endpoint_dir = tmp / "endpoints"
            raw_dir.mkdir()
            repo_root.mkdir()
            endpoint_dir.mkdir()
            instance_id = "sympy__sympy-99"
            (raw_dir / f"{instance_id}.traj").write_text(
                json.dumps({"instance_id": instance_id, "trajectory": [{"action": "pytest"}]}),
                encoding="utf-8",
            )
            (repo_root / instance_id).mkdir()
            output = tmp / "manifest.json"
            report = tmp / "report.json"

            self.run_build(
                "--raw-dir",
                str(raw_dir),
                "--repo-root",
                str(repo_root),
                "--endpoint-dir",
                str(endpoint_dir),
                "--output",
                str(output),
                "--report",
                str(report),
                expect_ok=False,
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["instances"], [])
            intake = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(intake["status"], "fail")
            self.assertIn("missing full_harness_result", intake["instances"][0]["blockers"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for the real-agent post-capture evidence pipeline."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PIPELINE = HERE / "run_real_agent_postcapture_pipeline.py"


class RunRealAgentPostcapturePipelineTests(unittest.TestCase):
    def run_pipeline(self, *args: str, expect_ok: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PIPELINE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return json.loads(proc.stdout)

    def test_empty_capture_dir_generates_failing_reports_without_fake_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            capture = tmp / "captures"
            repos = tmp / "repos"
            capture.mkdir()
            repos.mkdir()
            report_path = tmp / "postcapture.json"

            report = self.run_pipeline(
                "--capture-dir",
                str(capture),
                "--repo-root",
                str(repos),
                "--report-output",
                str(report_path),
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["summary"]["ready_instances"], 0)
            self.assertTrue((capture / "real-agent-manifest.json").exists())
            manifest = json.loads((capture / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["instances"], [])
            dashboard = json.loads((capture / "evidence-dashboard.current.json").read_text(encoding="utf-8"))
            self.assertNotEqual(dashboard["highest_supported_scope"], "EuroSys paper-ready real-agent evidence")
            self.assertIn("manifest has no instances", " ".join(report["blockers"]))
            self.assertTrue(report_path.exists())

    def test_ready_raw_repo_endpoint_triple_reaches_intake_and_manifest_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            capture = tmp / "captures"
            repos = tmp / "repos"
            raw_dir = capture / "raw"
            endpoint_dir = capture / "endpoints"
            raw_dir.mkdir(parents=True)
            endpoint_dir.mkdir(parents=True)
            instance_id = "django__django-14608"
            repo = repos / instance_id
            repo.mkdir(parents=True)
            (raw_dir / f"{instance_id}.traj").write_text(
                json.dumps({"instance_id": instance_id, "trajectory": [{"action": "pytest"}]}),
                encoding="utf-8",
            )
            (endpoint_dir / f"{instance_id}.full-harness.raw.json").write_text(
                json.dumps({"instance_id": instance_id, "resolved": True, "elapsed_sec": 12.0}),
                encoding="utf-8",
            )
            dashboard_md = capture / "dashboard.md"

            report = self.run_pipeline(
                "--capture-dir",
                str(capture),
                "--repo-root",
                str(repos),
                "--dashboard-markdown-output",
                str(dashboard_md),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["summary"]["ready_instances"], 1)
            manifest = json.loads((capture / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["instances"]), 1)
            self.assertEqual(manifest["instances"][0]["instance_id"], instance_id)
            intake = json.loads((capture / "intake.current.json").read_text(encoding="utf-8"))
            self.assertEqual(intake["summary"]["ready_instances"], 1)
            preflight = json.loads((capture / "manifest-preflight.current.json").read_text(encoding="utf-8"))
            self.assertEqual(preflight["summary"]["instances"], 1)
            self.assertEqual(preflight["summary"]["blockers"], 0)
            dashboard = json.loads((capture / "evidence-dashboard.current.json").read_text(encoding="utf-8"))
            self.assertEqual(dashboard["status"], "partial")
            self.assertIn("no collection audit or bundle audit inputs provided", dashboard["blockers"])
            self.assertTrue(dashboard_md.exists())


if __name__ == "__main__":
    unittest.main()

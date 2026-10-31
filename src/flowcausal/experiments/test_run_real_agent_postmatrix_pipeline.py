#!/usr/bin/env python3
"""Tests for the real-agent post-matrix evidence pipeline."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PIPELINE = HERE / "run_real_agent_postmatrix_pipeline.py"


def write_trial_artifacts(root: pathlib.Path, instance_id: str, variant: str, trial: int, *, elapsed: float, resolved: bool = True) -> dict:
    trial_dir = root / variant / f"trial-{trial:02d}"
    capture = trial_dir / "capture"
    raw = capture / "raw" / f"{instance_id}-{variant}-{trial}.traj"
    endpoint = capture / "endpoints" / f"{instance_id}-{variant}-{trial}.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({"instance_id": instance_id, "trajectory": [{"action": "pytest"}]}), encoding="utf-8")
    endpoint.write_text(json.dumps({"instance_id": instance_id, "resolved": resolved, "elapsed_sec": elapsed}), encoding="utf-8")
    (capture / "capture-report.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    return {
        "capture_dir": str(capture),
        "raw_agent_output": str(raw),
        "full_harness_result": str(endpoint),
        "trial_dir": str(trial_dir),
    }


def aggregate(elapsed: float, *, resolved_true: int = 3) -> dict:
    return {
        "trials": 3,
        "passes": 3,
        "endpoint_rows": 3,
        "resolved_true": resolved_true,
        "resolved_false": 3 - resolved_true,
        "resolved_unknown": 0,
        "elapsed_sec_mean": elapsed,
        "elapsed_sec_min": elapsed,
        "elapsed_sec_max": elapsed,
        "elapsed_sec_std": 0.0,
    }


def write_matrix(matrix_dir: pathlib.Path, repo_root: pathlib.Path, instance_id: str, *, flow_elapsed: float = 8.0) -> pathlib.Path:
    repo = repo_root / instance_id
    repo.mkdir(parents=True, exist_ok=True)
    trials = []
    for trial in range(1, 4):
        trials.append(
            {
                "variant": "default",
                "trial": trial,
                "status": "pass",
                "run_returncode": 0,
                "resolved": True,
                "elapsed_sec": 10.0,
                "warnings": [],
                "blockers": [],
                **write_trial_artifacts(matrix_dir, instance_id, "default", trial, elapsed=10.0),
            }
        )
        trials.append(
            {
                "variant": "flowcausal_width",
                "trial": trial,
                "status": "pass",
                "run_returncode": 0,
                "resolved": True,
                "elapsed_sec": flow_elapsed,
                "warnings": [],
                "blockers": [],
                **write_trial_artifacts(matrix_dir, instance_id, "flowcausal_width", trial, elapsed=flow_elapsed),
            }
        )
    summary = {
        "schema": "flowcausal.real_agent_harness_matrix.v1",
        "status": "pass",
        "instance_id": instance_id,
        "baseline_variant": "default",
        "trials": trials,
        "aggregate": {
            "default": aggregate(10.0),
            "default_observed": aggregate(10.1),
            "flowcausal_no_width": aggregate(flow_elapsed + 0.5),
            "flowcausal_width": aggregate(flow_elapsed),
        },
        "comparisons": {
            "flowcausal_width": {
                "baseline_variant": "default",
                "elapsed_delta_sec": flow_elapsed - 10.0,
                "elapsed_delta_pct": (flow_elapsed - 10.0) * 100.0 / 10.0,
                "speedup_x": 10.0 / flow_elapsed,
            }
        },
    }
    (matrix_dir / "real-agent-harness-matrix-manifest.json").write_text(
        json.dumps({"schema": "flowcausal.real_agent_harness_matrix.v1", "repo_dir": str(repo)}),
        encoding="utf-8",
    )
    path = matrix_dir / "harness-matrix-summary.json"
    path.write_text(json.dumps(summary), encoding="utf-8")
    return path


class RunRealAgentPostmatrixPipelineTests(unittest.TestCase):
    def run_pipeline(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PIPELINE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_empty_matrix_root_fails_honestly_without_endpoint_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            matrix_root = tmp / "matrices"
            report = self.run_pipeline(
                "--matrix-root",
                str(matrix_root),
                "--repo-root",
                str(tmp / "repos"),
                "--dashboard-output",
                str(tmp / "dashboard.json"),
                "--report-output",
                str(tmp / "postmatrix.json"),
            )

            self.assertEqual(report["status"], "fail")
            self.assertEqual(report["summary"]["matrix_summaries"], 0)
            self.assertIn("no harness-matrix-summary.json files found", report["blockers"])
            aggregate = json.loads((tmp / "harness-matrix-audit.collection.current.json").read_text(encoding="utf-8"))
            self.assertEqual(aggregate["status"], "fail")

    def test_three_passing_matrices_generate_audits_exports_and_dashboard_input(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            matrix_root = tmp / "matrices"
            repo_root = tmp / "repos"
            for instance_id in ("django__django-1", "sympy__sympy-2", "matplotlib__matplotlib-3"):
                write_matrix(matrix_root / instance_id, repo_root, instance_id)
            dashboard = tmp / "dashboard.json"
            dashboard_md = tmp / "dashboard.md"
            report = self.run_pipeline(
                "--matrix-root",
                str(matrix_root),
                "--repo-root",
                str(repo_root),
                "--dashboard-output",
                str(dashboard),
                "--dashboard-markdown-output",
                str(dashboard_md),
                "--report-output",
                str(tmp / "postmatrix.json"),
            )

            self.assertEqual(report["summary"]["matrix_summaries"], 3)
            self.assertEqual(report["summary"]["pass_audits"], 3)
            self.assertEqual(report["summary"]["bundle_export_pass"], 3)
            self.assertEqual(report["status"], "partial")
            aggregate = json.loads((tmp / "harness-matrix-audit.collection.current.json").read_text(encoding="utf-8"))
            self.assertEqual(aggregate["status"], "partial")
            endpoint = next(row for row in aggregate["chains"] if row["name"] == "endpoint improvement")
            self.assertEqual(endpoint["status"], "pass")
            baseline = next(row for row in aggregate["chains"] if row["name"] == "baseline comparison")
            self.assertEqual(baseline["status"], "pass")
            self.assertTrue((matrix_root / "django__django-1" / "harness-matrix-audit.current.json").exists())
            self.assertTrue((matrix_root / "django__django-1" / "harness-matrix-bundle-manifest.json").exists())
            dashboard_payload = json.loads(dashboard.read_text(encoding="utf-8"))
            self.assertEqual(dashboard_payload["status"], "partial")
            self.assertTrue(dashboard_md.exists())


if __name__ == "__main__":
    unittest.main()

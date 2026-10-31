#!/usr/bin/env python3
"""Tests for exporting bundle manifests from full harness matrices."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
EXPORT = HERE / "export_harness_matrix_bundle_manifest.py"
IMPORTER = HERE / "import_real_agent_harness_matrix.py"
AUDIT = HERE / "audit_real_agent_harness_matrix.py"
VALIDATE = HERE / "validate_bundle_collection_manifest.py"


def write_artifacts(tmp: pathlib.Path, variant: str, trial: int) -> dict:
    capture = tmp / "runs" / variant / f"trial-{trial:02d}" / "capture"
    raw = capture / "raw" / f"{variant}-{trial}.traj"
    endpoint = capture / "endpoints" / f"{variant}-{trial}.endpoint.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        json.dumps({"instance_id": "django__django-1", "trajectory": [{"action": "pytest"}]}),
        encoding="utf-8",
    )
    endpoint.write_text(
        json.dumps({"instance_id": "django__django-1", "resolved": True, "elapsed_sec": 10.0 + trial}),
        encoding="utf-8",
    )
    return {
        "capture_dir": str(capture),
        "raw_agent_output": str(raw),
        "full_harness_result": str(endpoint),
    }


def summary(tmp: pathlib.Path) -> dict:
    trials = []
    for variant in ("default", "flowcausal_width"):
        for idx in (1, 2, 3):
            trials.append(
                {
                    "variant": variant,
                    "trial": idx,
                    "status": "pass",
                    "run_returncode": 0,
                    "resolved": True,
                    "elapsed_sec": 10.0 + idx if variant == "default" else 8.0 + idx,
                    "warnings": [],
                    "blockers": [],
                    **write_artifacts(tmp, variant, idx),
                }
            )
    return {
        "schema": "flowcausal.real_agent_harness_matrix.v1",
        "status": "pass",
        "instance_id": "django__django-1",
        "baseline_variant": "default",
        "trials": trials,
        "aggregate": {
            "default": {"trials": 3, "passes": 3, "endpoint_rows": 3, "resolved_true": 3, "resolved_unknown": 0, "elapsed_sec_mean": 12.0},
            "flowcausal_width": {"trials": 3, "passes": 3, "endpoint_rows": 3, "resolved_true": 3, "resolved_unknown": 0, "elapsed_sec_mean": 10.0},
        },
        "comparisons": {
            "flowcausal_width": {"baseline_variant": "default", "elapsed_delta_pct": -16.7, "speedup_x": 1.2}
        },
    }


def audit(status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_matrix_audit.v1",
        "status": status,
        "summary": {
            "instance_id": "django__django-1",
            "baseline_variant": "default",
            "flowcausal_variant": "flowcausal_width",
            "min_trials": 3,
        },
        "blockers": [] if status == "pass" else ["endpoint improvement failed"],
    }


def write_external_endpoint(root: pathlib.Path, instance_id: str, variant: str, trial: int, elapsed: float) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    run_dir = root / "external" / variant / f"trial-{trial:02d}"
    raw = run_dir / "agent.traj"
    endpoint = run_dir / "endpoint.json"
    capture = run_dir / "capture"
    live = run_dir / "live"
    victim = live / "victim.log"
    capture.mkdir(parents=True, exist_ok=True)
    live.mkdir(parents=True, exist_ok=True)
    raw.write_text(
        json.dumps({"instance_id": instance_id, "trajectory": [{"action": "pytest", "observation": "ok"}]}),
        encoding="utf-8",
    )
    endpoint.write_text(
        json.dumps(
            {
                "instance_id": instance_id,
                "resolved": True,
                "elapsed_sec": elapsed,
                "status": "resolved",
            }
        ),
        encoding="utf-8",
    )
    victim.write_text(
        json.dumps(
            {
                "type": "victim_summary",
                "iterations": 100,
                "returncode": 0,
                "lateness": {"p99_ms": 1.0, "max_ms": 2.0},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return endpoint, raw, capture, live, victim


class ExportHarnessMatrixBundleManifestTests(unittest.TestCase):
    def run_export(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(EXPORT), *args],
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

    def test_exports_baseline_first_pass_trial_and_preflight_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            summary_path = tmp / "harness-matrix-summary.json"
            audit_path = tmp / "harness-audit.json"
            output = tmp / "collection" / "manifest.json"
            report = tmp / "collection" / "export-report.json"
            summary_path.write_text(json.dumps(summary(tmp)), encoding="utf-8")
            audit_path.write_text(json.dumps(audit("pass")), encoding="utf-8")

            self.run_export(
                "--summary",
                str(summary_path),
                "--harness-matrix-audit",
                str(audit_path),
                "--repo-dir",
                str(repo),
                "--output",
                str(output),
                "--report",
                str(report),
                "--raw-format",
                "swe-agent",
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["instances"]), 1)
            row = manifest["instances"][0]
            self.assertEqual(row["instance_id"], "django__django-1")
            self.assertEqual(row["harness_matrix_source_variant"], "default")
            self.assertEqual(row["harness_matrix_source_trial"], 1)
            self.assertTrue(row["raw_agent_output"].endswith("default-1.traj"))
            self.assertTrue(row["full_harness_result"].endswith("default-1.endpoint.json"))

            preflight = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATE),
                    "--manifest",
                    str(output),
                    "--out-dir",
                    str(tmp / "bundles"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            preflight_report = json.loads(preflight.stdout)
            self.assertEqual(preflight_report["blockers"], [])

    def test_partial_audit_is_rejected_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            summary_path = tmp / "summary.json"
            audit_path = tmp / "audit.json"
            output = tmp / "manifest.json"
            report = tmp / "report.json"
            summary_path.write_text(json.dumps(summary(tmp)), encoding="utf-8")
            audit_path.write_text(json.dumps(audit("partial")), encoding="utf-8")

            self.run_export(
                "--summary",
                str(summary_path),
                "--harness-matrix-audit",
                str(audit_path),
                "--repo-dir",
                str(repo),
                "--output",
                str(output),
                "--report",
                str(report),
                expect_ok=False,
            )

            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "fail")
            self.assertIn("harness matrix audit is not pass", " ".join(payload["blockers"]))
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["instances"], [])

    def test_can_export_audit_selected_flowcausal_variant(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            summary_path = tmp / "summary.json"
            audit_path = tmp / "audit.json"
            output = tmp / "manifest.json"
            report = tmp / "report.json"
            summary_path.write_text(json.dumps(summary(tmp)), encoding="utf-8")
            audit_path.write_text(json.dumps(audit("pass")), encoding="utf-8")

            self.run_export(
                "--summary",
                str(summary_path),
                "--harness-matrix-audit",
                str(audit_path),
                "--repo-dir",
                str(repo),
                "--source-variant",
                "audited-flowcausal",
                "--trial-selection",
                "median-elapsed",
                "--output",
                str(output),
                "--report",
                str(report),
            )

            row = json.loads(output.read_text(encoding="utf-8"))["instances"][0]
            self.assertEqual(row["harness_matrix_source_variant"], "flowcausal_width")
            self.assertEqual(row["harness_matrix_source_trial"], 2)
            self.assertTrue(row["raw_agent_output"].endswith("flowcausal_width-2.traj"))

    def test_imported_external_harness_matrix_exports_to_preflight_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance_id = "django__django-14608"
            repo = tmp / "repo"
            repo.mkdir()
            imported = tmp / "imported"
            endpoint_specs: list[str] = []
            elapsed_by_variant = {
                "default": [10.0, 10.2, 9.8],
                "default_observed": [10.1],
                "flowcausal_no_width": [9.0],
                "flowcausal_width": [8.0, 8.2, 7.8],
            }
            for variant, values in elapsed_by_variant.items():
                for trial_no, elapsed in enumerate(values, start=1):
                    endpoint, raw, capture, live, victim = write_external_endpoint(tmp, instance_id, variant, trial_no, elapsed)
                    endpoint_specs.extend(
                        [
                            "--endpoint",
                            f"variant={variant},trial={trial_no},path={endpoint},raw={raw},capture={capture},live={live},victim={victim}",
                        ]
                    )

            importer = subprocess.run(
                [
                    sys.executable,
                    str(IMPORTER),
                    "--out-dir",
                    str(imported),
                    "--instance-id",
                    instance_id,
                    "--created-at",
                    "2026-07-20T00:00:00+00:00",
                    *endpoint_specs,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(importer.returncode, 0, importer.stderr)
            summary_path = imported / "harness-matrix-summary.json"

            audit_path = imported / "harness-matrix-audit.json"
            auditor = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--summary",
                    str(summary_path),
                    "--output",
                    str(audit_path),
                    "--min-trials",
                    "3",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(auditor.returncode, 0, auditor.stderr)
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual(audit_payload["status"], "pass", audit_payload["blockers"])

            output = tmp / "collection" / "manifest.json"
            report = tmp / "collection" / "export-report.json"
            self.run_export(
                "--summary",
                str(summary_path),
                "--harness-matrix-audit",
                str(audit_path),
                "--repo-dir",
                str(repo),
                "--source-variant",
                "audited-flowcausal",
                "--trial-selection",
                "median-elapsed",
                "--output",
                str(output),
                "--report",
                str(report),
                "--raw-format",
                "swe-agent",
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["instances"]), 1)
            row = manifest["instances"][0]
            self.assertEqual(row["instance_id"], instance_id)
            self.assertEqual(row["harness_matrix_source_variant"], "flowcausal_width")
            self.assertEqual(row["harness_matrix_source_trial"], 1)
            self.assertTrue(pathlib.Path(tmp, row["raw_agent_output"]).exists())
            self.assertTrue(pathlib.Path(tmp, row["full_harness_result"]).exists())
            self.assertTrue(pathlib.Path(tmp, row["victim_log"]).exists())
            self.assertTrue(pathlib.Path(tmp, row["live_dir"]).is_dir())
            self.assertTrue(pathlib.Path(tmp, row["harness_matrix_summary"]).exists())
            self.assertTrue(pathlib.Path(tmp, row["harness_matrix_audit"]).exists())

            preflight = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATE),
                    "--manifest",
                    str(output),
                    "--out-dir",
                    str(tmp / "bundles"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            preflight_report = json.loads(preflight.stdout)
            self.assertEqual(preflight_report["status"], "partial")
            self.assertEqual(preflight_report["blockers"], [])
            self.assertIn("victim_log exists", " ".join(preflight_report["instances"][0]["evidence"]))
            self.assertIn("live_dir exists", " ".join(preflight_report["instances"][0]["evidence"]))
            self.assertIn("harness_matrix_summary exists", " ".join(preflight_report["instances"][0]["evidence"]))
            self.assertIn("harness_matrix_audit exists", " ".join(preflight_report["instances"][0]["evidence"]))
            self.assertIn("matrix_summary missing", " ".join(preflight_report["warnings"]))


if __name__ == "__main__":
    unittest.main()

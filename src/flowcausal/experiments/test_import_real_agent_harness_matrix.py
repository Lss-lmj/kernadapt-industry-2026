#!/usr/bin/env python3
"""Tests for importing externally produced full-harness endpoint matrices."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
IMPORTER = HERE / "import_real_agent_harness_matrix.py"
AUDIT = HERE / "audit_real_agent_harness_matrix.py"


def write_endpoint(path: pathlib.Path, *, instance: str, resolved: bool = True, elapsed: float = 10.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "instance_id": instance,
                "resolved": resolved,
                "elapsed_sec": elapsed,
                "status": "resolved" if resolved else "unresolved",
            }
        ),
        encoding="utf-8",
    )


def write_raw(path: pathlib.Path, *, instance: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"instance_id": instance, "trajectory": [{"action": "pytest", "observation": "ok"}]}),
        encoding="utf-8",
    )


def write_victim_log(path: pathlib.Path, *, p99_ms: float, max_ms: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "victim_begin", "workers": 1},
        {
            "type": "victim_summary",
            "iterations": 100,
            "returncode": 0,
            "lateness": {"p99_ms": p99_ms, "max_ms": max_ms},
            "runtime": {"p99_ms": 0.1, "max_ms": 0.2},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class ImportRealAgentHarnessMatrixTests(unittest.TestCase):
    def run_importer(self, *args: str, expect_ok: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(IMPORTER), *args],
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

    def run_audit(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(AUDIT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_imported_external_endpoints_can_feed_endpoint_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance = "django__django-14608"
            out = tmp / "imported-matrix"
            specs: list[str] = []
            elapsed_by_variant = {
                "default": [10.0, 10.2, 9.8],
                "default_observed": [10.1],
                "flowcausal_no_width": [8.8],
                "flowcausal_width": [8.0, 8.2, 7.8],
            }
            for variant, values in elapsed_by_variant.items():
                for trial, elapsed in enumerate(values, start=1):
                    endpoint = tmp / "external" / variant / f"trial-{trial:02d}" / "endpoint.json"
                    raw = tmp / "external" / variant / f"trial-{trial:02d}" / "agent.traj"
                    capture = tmp / "external" / variant / f"trial-{trial:02d}" / "capture"
                    capture.mkdir(parents=True)
                    write_endpoint(endpoint, instance=instance, elapsed=elapsed)
                    write_raw(raw, instance=instance)
                    specs.extend(
                        [
                            "--endpoint",
                            f"variant={variant},trial={trial},path={endpoint},raw={raw},capture={capture}",
                        ]
                    )

            result = self.run_importer(
                "--out-dir",
                str(out),
                "--instance-id",
                instance,
                "--created-at",
                "2026-07-20T00:00:00+00:00",
                *specs,
            )

            self.assertEqual(result["status"], "pass")
            summary_path = out / "harness-matrix-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["schema"], "flowcausal.real_agent_harness_matrix.v1")
            self.assertEqual(summary["aggregate"]["default"]["trials"], 3)
            self.assertEqual(summary["aggregate"]["flowcausal_width"]["trials"], 3)
            self.assertAlmostEqual(summary["aggregate"]["default"]["elapsed_sec_mean"], 10.0)
            self.assertAlmostEqual(summary["aggregate"]["flowcausal_width"]["elapsed_sec_mean"], 8.0)
            self.assertLess(summary["comparisons"]["flowcausal_width"]["elapsed_delta_pct"], -10.0)

            audit = self.run_audit("--summary", str(summary_path), "--min-trials", "3")

            self.assertEqual(audit["status"], "pass", audit["blockers"])
            endpoint_gate = next(row for row in audit["gates"] if row["name"] == "endpoint improvement")
            self.assertEqual(endpoint_gate["status"], "pass")
            artifact_gate = next(row for row in audit["gates"] if row["name"] == "reproducibility/artifact")
            self.assertEqual(artifact_gate["status"], "pass")

    def test_missing_raw_path_keeps_import_and_artifact_audit_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance = "sympy__sympy-21171"
            out = tmp / "imported-matrix"
            endpoint = tmp / "external" / "default" / "endpoint.json"
            write_endpoint(endpoint, instance=instance, elapsed=5.0)

            result = self.run_importer(
                "--out-dir",
                str(out),
                "--instance-id",
                instance,
                "--endpoint",
                f"variant=default,trial=1,path={endpoint}",
                "--allow-partial",
            )

            self.assertEqual(result["status"], "pass")
            summary_path = out / "harness-matrix-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertIn("raw_agent_output path missing", " ".join(summary["warnings"]))

            audit = self.run_audit("--summary", str(summary_path), "--min-trials", "1")

            artifact_gate = next(row for row in audit["gates"] if row["name"] == "reproducibility/artifact")
            self.assertEqual(artifact_gate["status"], "partial")
            self.assertIn("missing raw_agent_output path", " ".join(artifact_gate["blockers"]))

    def test_imported_victim_logs_feed_required_safety_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance = "django__django-14608"
            out = tmp / "imported-matrix"
            specs: list[str] = []
            for variant, elapsed_values, p99, max_late in (
                ("default", [10.0, 10.1, 9.9], 1.0, 2.0),
                ("default_observed", [10.2], 1.0, 2.0),
                ("flowcausal_no_width", [8.7], 1.05, 2.2),
                ("flowcausal_width", [8.0, 8.1, 7.9], 1.1, 2.4),
            ):
                for trial, elapsed in enumerate(elapsed_values, start=1):
                    run_dir = tmp / "external" / variant / f"trial-{trial:02d}"
                    endpoint = run_dir / "endpoint.json"
                    raw = run_dir / "agent.traj"
                    capture = run_dir / "capture"
                    victim = run_dir / "victim.log"
                    capture.mkdir(parents=True)
                    write_endpoint(endpoint, instance=instance, elapsed=elapsed)
                    write_raw(raw, instance=instance)
                    write_victim_log(victim, p99_ms=p99, max_ms=max_late)
                    specs.extend(
                        [
                            "--endpoint",
                            f"variant={variant},trial={trial},path={endpoint},raw={raw},capture={capture},victim={victim}",
                        ]
                    )

            result = self.run_importer(
                "--out-dir",
                str(out),
                "--instance-id",
                instance,
                "--created-at",
                "2026-07-20T00:00:00+00:00",
                *specs,
            )

            self.assertEqual(result["status"], "pass", result)
            summary_path = out / "harness-matrix-summary.json"
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            first_flow = next(row for row in summary["trials"] if row["variant"] == "flowcausal_width")
            self.assertTrue(first_flow["victim_log"].endswith("victim.log"))
            capture_report = json.loads(pathlib.Path(first_flow["capture_dir"], "capture-report.json").read_text(encoding="utf-8"))
            self.assertIn("victim_log", capture_report["artifacts"])

            audit = self.run_audit(
                "--summary",
                str(summary_path),
                "--min-trials",
                "3",
                "--require-victim-safety",
                "--max-victim-p99-slowdown-pct",
                "25",
                "--max-victim-max-slowdown-pct",
                "50",
            )

            self.assertEqual(audit["status"], "pass", audit["blockers"])
            safety_gate = next(row for row in audit["gates"] if row["name"] == "safety/fairness")
            self.assertEqual(safety_gate["status"], "pass", safety_gate["blockers"])
            self.assertIn("victim_p99_slowdown_pct=10.0", " ".join(safety_gate["evidence"]))

    def test_missing_imported_victim_log_keeps_import_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            instance = "django__django-14608"
            out = tmp / "imported-matrix"
            endpoint = tmp / "external" / "default" / "endpoint.json"
            raw = tmp / "external" / "default" / "agent.traj"
            capture = tmp / "external" / "default" / "capture"
            capture.mkdir(parents=True)
            write_endpoint(endpoint, instance=instance, elapsed=10.0)
            write_raw(raw, instance=instance)
            missing_victim = tmp / "external" / "default" / "missing-victim.log"

            result = self.run_importer(
                "--out-dir",
                str(out),
                "--instance-id",
                instance,
                "--endpoint",
                f"variant=default,trial=1,path={endpoint},raw={raw},capture={capture},victim={missing_victim}",
                "--allow-partial",
            )

            self.assertEqual(result["status"], "partial")
            summary = json.loads((out / "harness-matrix-summary.json").read_text(encoding="utf-8"))
            self.assertIn("victim_log path does not exist", " ".join(summary["blockers"]))


if __name__ == "__main__":
    unittest.main()

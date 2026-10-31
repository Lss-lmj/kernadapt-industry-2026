#!/usr/bin/env python3
"""Tests for full real-agent harness endpoint matrix auditing."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_real_agent_harness_matrix.py"


def touch_trial_artifacts(root: pathlib.Path, variant: str, trial: int) -> dict:
    trial_dir = root / "runs" / variant / f"trial-{trial:02d}"
    capture = trial_dir / "capture"
    raw = capture / "raw" / f"{variant}-{trial}.traj"
    endpoint = capture / "endpoints" / f"{variant}-{trial}.json"
    capture.mkdir(parents=True, exist_ok=True)
    raw.parent.mkdir(parents=True, exist_ok=True)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps({"trajectory": [{"action": "pytest"}]}), encoding="utf-8")
    endpoint.write_text(json.dumps({"instance_id": "django__django-1", "resolved": True, "elapsed_sec": 1.0}), encoding="utf-8")
    (capture / "capture-report.json").write_text(json.dumps({"status": "pass"}), encoding="utf-8")
    return {
        "capture_dir": str(capture),
        "raw_agent_output": str(raw),
        "full_harness_result": str(endpoint),
    }


def trial(root: pathlib.Path, variant: str, trial_no: int, *, resolved: bool = True, elapsed: float = 10.0, status: str = "pass") -> dict:
    paths = touch_trial_artifacts(root, variant, trial_no)
    return {
        "variant": variant,
        "trial": trial_no,
        "status": status,
        "run_returncode": 0,
        "resolved": resolved,
        "elapsed_sec": elapsed,
        "warnings": [],
        "blockers": [],
        **paths,
    }


def write_victim_log(live_dir: pathlib.Path, *, p99_ms: float, max_ms: float, iterations: int = 100, returncode: int = 0) -> None:
    live_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {"type": "victim_begin", "workers": 1},
        {
            "type": "victim_summary",
            "iterations": iterations,
            "returncode": returncode,
            "lateness": {"p99_ms": p99_ms, "max_ms": max_ms},
            "runtime": {"p99_ms": 0.1, "max_ms": 0.2},
        },
    ]
    (live_dir / "victim.log").write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def add_victim_logs(payload: dict, root: pathlib.Path, *, baseline_p99: float, baseline_max: float, flow_p99: float, flow_max: float) -> None:
    for row in payload["trials"]:
        variant = row["variant"]
        trial_no = int(row["trial"])
        live_dir = root / "runs" / variant / f"trial-{trial_no:02d}" / "live"
        row["live_dir"] = str(live_dir)
        if variant == "default":
            write_victim_log(live_dir, p99_ms=baseline_p99, max_ms=baseline_max)
        elif variant == "flowcausal_width":
            write_victim_log(live_dir, p99_ms=flow_p99, max_ms=flow_max)


def aggregate(elapsed: float, *, resolved_true: int = 3, trials: int = 3) -> dict:
    return {
        "trials": trials,
        "passes": trials,
        "endpoint_rows": trials,
        "resolved_true": resolved_true,
        "resolved_false": trials - resolved_true,
        "resolved_unknown": 0,
        "elapsed_sec_mean": elapsed,
        "elapsed_sec_min": elapsed,
        "elapsed_sec_max": elapsed,
        "elapsed_sec_std": 0.0,
    }


def summary(root: pathlib.Path, *, baseline_elapsed: float = 10.0, flow_elapsed: float = 8.0, flow_resolved: int = 3) -> dict:
    rows = []
    for idx in range(1, 4):
        rows.append(trial(root, "default", idx, elapsed=baseline_elapsed))
        rows.append(trial(root, "flowcausal_width", idx, resolved=idx <= flow_resolved, elapsed=flow_elapsed))
    return {
        "schema": "flowcausal.real_agent_harness_matrix.v1",
        "status": "pass",
        "instance_id": "django__django-1",
        "baseline_variant": "default",
        "trials": rows,
        "aggregate": {
            "default": aggregate(baseline_elapsed),
            "default_observed": aggregate(baseline_elapsed + 0.1),
            "flowcausal_no_width": aggregate(flow_elapsed + 0.5),
            "flowcausal_width": aggregate(flow_elapsed, resolved_true=flow_resolved),
        },
        "comparisons": {
            "default": {"baseline_variant": "default", "elapsed_delta_pct": 0.0, "speedup_x": 1.0},
            "flowcausal_width": {
                "baseline_variant": "default",
                "elapsed_delta_sec": flow_elapsed - baseline_elapsed,
                "elapsed_delta_pct": (flow_elapsed - baseline_elapsed) * 100.0 / baseline_elapsed,
                "speedup_x": baseline_elapsed / flow_elapsed,
            },
        },
    }


class AuditRealAgentHarnessMatrixTests(unittest.TestCase):
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

    def test_passing_endpoint_matrix_supports_full_harness_endpoint_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            path = tmp / "summary.json"
            path.write_text(json.dumps(summary(tmp, baseline_elapsed=10.0, flow_elapsed=8.0)), encoding="utf-8")

            report = self.run_audit("--summary", str(path), "--min-trials", "3")

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["highest_supported_scope"], "full-harness endpoint-improvement evidence")
            endpoint_gate = next(row for row in report["gates"] if row["name"] == "endpoint improvement")
            self.assertEqual(endpoint_gate["status"], "pass")
            frontier_chain = next(row for row in report["chains"] if row["name"] == "frontier correctness")
            self.assertEqual(frontier_chain["status"], "missing")
            mechanism_chain = next(row for row in report["chains"] if row["name"] == "scheduler mechanism")
            self.assertEqual(mechanism_chain["status"], "partial")
            safety_chain = next(row for row in report["chains"] if row["name"] == "safety/fairness")
            self.assertEqual(safety_chain["status"], "missing")

    def test_slower_flowcausal_fails_endpoint_improvement_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            path = tmp / "summary.json"
            path.write_text(json.dumps(summary(tmp, baseline_elapsed=10.0, flow_elapsed=11.0)), encoding="utf-8")

            report = self.run_audit("--summary", str(path), "--min-trials", "3")

            self.assertEqual(report["status"], "partial")
            endpoint_gate = next(row for row in report["gates"] if row["name"] == "endpoint improvement")
            self.assertEqual(endpoint_gate["status"], "fail")
            self.assertIn("slower", " ".join(endpoint_gate["blockers"]))

    def test_missing_archived_paths_keep_artifact_gate_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            payload = summary(tmp, baseline_elapsed=10.0, flow_elapsed=8.0)
            payload["trials"][0]["raw_agent_output"] = str(tmp / "missing.traj")
            path = tmp / "summary.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            report = self.run_audit("--summary", str(path), "--min-trials", "3")

            artifact_gate = next(row for row in report["gates"] if row["name"] == "reproducibility/artifact")
            self.assertEqual(artifact_gate["status"], "partial")
            self.assertIn("does not exist", " ".join(artifact_gate["blockers"]))

    def test_resolved_regression_fails_endpoint_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            path = tmp / "summary.json"
            path.write_text(json.dumps(summary(tmp, baseline_elapsed=10.0, flow_elapsed=7.0, flow_resolved=2)), encoding="utf-8")

            report = self.run_audit("--summary", str(path), "--min-trials", "3")

            endpoint_gate = next(row for row in report["gates"] if row["name"] == "endpoint improvement")
            self.assertEqual(endpoint_gate["status"], "fail")
            self.assertIn("solves fewer", " ".join(endpoint_gate["blockers"]))

    def test_required_victim_safety_passes_with_live_victim_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            payload = summary(tmp, baseline_elapsed=10.0, flow_elapsed=8.0)
            add_victim_logs(payload, tmp, baseline_p99=1.0, baseline_max=2.0, flow_p99=1.1, flow_max=2.4)
            path = tmp / "summary.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            report = self.run_audit("--summary", str(path), "--min-trials", "3", "--require-victim-safety")

            self.assertEqual(report["status"], "pass", report["blockers"])
            safety_gate = next(row for row in report["gates"] if row["name"] == "safety/fairness")
            self.assertEqual(safety_gate["status"], "pass", safety_gate["blockers"])
            self.assertIn("victim_p99_slowdown_pct=10.0", " ".join(safety_gate["evidence"]))
            safety_chain = next(row for row in report["chains"] if row["name"] == "safety/fairness")
            self.assertEqual(safety_chain["status"], "pass")

    def test_required_victim_safety_fails_when_flowcausal_hurts_victim_p99(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            payload = summary(tmp, baseline_elapsed=10.0, flow_elapsed=8.0)
            add_victim_logs(payload, tmp, baseline_p99=1.0, baseline_max=2.0, flow_p99=2.0, flow_max=2.2)
            path = tmp / "summary.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            report = self.run_audit(
                "--summary",
                str(path),
                "--min-trials",
                "3",
                "--require-victim-safety",
                "--max-victim-p99-slowdown-pct",
                "25",
            )

            self.assertEqual(report["status"], "partial")
            safety_gate = next(row for row in report["gates"] if row["name"] == "safety/fairness")
            self.assertEqual(safety_gate["status"], "fail")
            self.assertIn("victim p99 slowdown", " ".join(safety_gate["blockers"]))


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Tests for collection-level real-agent bundle auditing."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_bundle_collection.py"
GATES = (
    "frontier correctness",
    "endpoint improvement",
    "scheduler mechanism",
    "safety/fairness",
    "baseline comparison",
    "reproducibility/artifact",
)


def bundle_report(instance_id: str, *, status: str = "pass", failed_gate: str = "") -> dict:
    gates = []
    for name in GATES:
        gate_status = "pass" if status == "pass" else "partial"
        blockers: list[str] = []
        if name == failed_gate:
            gate_status = "fail"
            blockers = [f"{name} blocker"]
        gates.append({"name": name, "status": gate_status, "evidence": [f"{name} evidence"], "blockers": blockers})
    return {
        "schema": "flowcausal.agent_trace_bundle_audit.v1",
        "status": status,
        "highest_supported_scope": "L5-ready evidence bundle" if status == "pass" else "partial trace-replay evidence; not L5",
        "normalized_trace": {"instance_id": instance_id},
        "full_harness_endpoint": {"instance_id": instance_id, "resolved": True},
        "gates": gates,
    }


class BundleCollectionAuditTests(unittest.TestCase):
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

    def test_collection_passes_when_enough_l5_bundles_exist(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            paths = []
            for index in range(3):
                path = tmp / f"bundle-{index}" / "agent-trace-bundle-audit.json"
                path.parent.mkdir()
                path.write_text(json.dumps(bundle_report(f"django__issue-{index}")), encoding="utf-8")
                paths.append(str(path.parent))

            report = self.run_audit(*paths, "--min-ready-bundles", "3")

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["summary"]["bundle_count"], 3)
            self.assertEqual(report["summary"]["l5_ready_count"], 3)
            self.assertEqual(report["gate_counts"]["frontier correctness"]["pass"], 3)

    def test_collection_is_partial_when_ready_count_is_too_small(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            good = tmp / "good.json"
            partial = tmp / "partial.json"
            good.write_text(json.dumps(bundle_report("django__issue-1")), encoding="utf-8")
            partial.write_text(
                json.dumps(bundle_report("sympy__issue-2", status="partial", failed_gate="frontier correctness")),
                encoding="utf-8",
            )

            report = self.run_audit(str(good), str(partial), "--min-ready-bundles", "2")

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["summary"]["l5_ready_count"], 1)
            blockers = " ".join(report["blockers"])
            self.assertIn("L5-ready bundle count 1 < required 2", blockers)
            self.assertIn("frontier correctness blocker", report["top_blockers"][0]["blocker"])

    def test_recursive_discovery_and_markdown_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            nested = tmp / "runs" / "bundle-a"
            nested.mkdir(parents=True)
            (nested / "agent-trace-bundle-audit.json").write_text(
                json.dumps(bundle_report("astropy__issue-3")),
                encoding="utf-8",
            )
            out_json = tmp / "collection.json"
            out_md = tmp / "collection.md"

            report = self.run_audit(
                str(tmp),
                "--recursive",
                "--min-ready-bundles",
                "1",
                "--output",
                str(out_json),
                "--markdown-output",
                str(out_md),
            )

            self.assertEqual(report["status"], "pass")
            self.assertTrue(out_json.exists())
            markdown = out_md.read_text(encoding="utf-8")
            self.assertIn("FlowCausal bundle collection audit", markdown)
            self.assertIn("Selected-trial binding", markdown)

    def test_full_harness_matrix_bundles_require_selected_trial_binding(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bad = bundle_report("django__issue-1")
            bad["inputs"] = {
                "harness_matrix_summary": str(tmp / "harness-matrix-summary.json"),
                "harness_matrix_audit": str(tmp / "harness-matrix-audit.json"),
                "victim_log": str(tmp / "victim.log"),
            }
            bad_path = tmp / "bad" / "agent-trace-bundle-audit.json"
            bad_path.parent.mkdir()
            bad_path.write_text(json.dumps(bad), encoding="utf-8")

            report = self.run_audit(str(bad_path.parent), "--min-ready-bundles", "1")

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["summary"]["l5_ready_count"], 1)
            self.assertEqual(report["summary"]["selected_trial_binding"]["missing"], 1)
            self.assertIn("selected-trial binding missing", " ".join(report["blockers"]))
            self.assertEqual(report["bundles"][0]["selected_trial_binding"]["missing"], ["raw_agent_output", "full_harness_result", "victim_log"])

            good = bundle_report("django__issue-2")
            good["inputs"] = {
                "harness_matrix_summary": str(tmp / "harness-matrix-summary.json"),
                "harness_matrix_audit": str(tmp / "harness-matrix-audit.json"),
                "victim_log": str(tmp / "victim.log"),
            }
            repro_gate = next(gate for gate in good["gates"] if gate["name"] == "reproducibility/artifact")
            repro_gate["evidence"].extend(
                [
                    "harness selected trial raw_agent_output matches bundle raw",
                    "harness selected trial full_harness_result matches normalized endpoint provenance",
                    "harness selected trial victim_log matches bundle victim log",
                ]
            )
            good_path = tmp / "good" / "agent-trace-bundle-audit.json"
            good_path.parent.mkdir()
            good_path.write_text(json.dumps(good), encoding="utf-8")
            (good_path.parent / "agent-trace-bundle-audit.md").write_text("# bundle audit\n", encoding="utf-8")

            good_report = self.run_audit(str(good_path.parent), "--min-ready-bundles", "1")

            self.assertEqual(good_report["status"], "pass")
            self.assertEqual(good_report["summary"]["selected_trial_binding"]["pass"], 1)
            self.assertTrue(good_report["bundles"][0]["markdown_report"].endswith("agent-trace-bundle-audit.md"))


if __name__ == "__main__":
    unittest.main()

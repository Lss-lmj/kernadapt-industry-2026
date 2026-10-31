#!/usr/bin/env python3
"""Tests for preparing real-agent evidence bundle scaffolds."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PREPARE = HERE / "prepare_agent_trace_bundle.py"


def matrix_summary(trace_path: pathlib.Path) -> dict:
    stage_trial = {
        "workload": {
            "workload_begin": {
                "workload": "agent_trace_workflow",
                "trace_file": str(trace_path),
                "instance_id": "django__issue",
                "command_count": 2,
            },
            "stage_records": [
                {"stage": "TEST", "elapsed_sec": 1.0, "command_index": 1, "source_id": "test"}
            ],
        }
    }
    variants = [
        {
            "name": "default_taskset",
            "kind": "default",
            "aggregate": {"critical_mean_sec": 10.0},
            "trials": [stage_trial, stage_trial, stage_trial],
        },
        {
            "name": "flowcausal_width",
            "kind": "flowcausal",
            "aggregate": {"critical_mean_sec": 8.0},
            "trials": [stage_trial, stage_trial, stage_trial],
        },
        {"name": "static_cgroup", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.8}, "trials": []},
        {"name": "nice_priority", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.6}, "trials": []},
        {"name": "static_affinity", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.7}, "trials": []},
        {"name": "oracle_frontier", "kind": "baseline", "aggregate": {"critical_mean_sec": 7.5}, "trials": []},
    ]
    return {
        "config": {"command": f"python3 agent_trace_workflow.py --trace-file {trace_path}"},
        "variants": variants,
        "claim_gate_audit": {
            "highest_supported_level": "L5 fixture",
            "gates": [
                {"name": "stage/episode outcome", "status": "pass", "evidence": [], "blockers": []},
                {"name": "controller/BPF admission", "status": "pass", "evidence": [], "blockers": []},
                {"name": "direct CPU mechanism", "status": "pass", "evidence": [], "blockers": []},
                {"name": "victim/fairness safety", "status": "pass", "evidence": [], "blockers": []},
            ],
        },
    }


class PrepareAgentTraceBundleTests(unittest.TestCase):
    def write_inputs(self, tmp: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
        raw = tmp / "raw.traj"
        raw.write_text(
            json.dumps(
                {
                    "instance_id": "django__issue",
                    "trajectory": [
                        {"id": "inspect", "action": "rg target ."},
                        {"id": "test", "action": "pytest tests/test_target.py"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        labels = tmp / "frontier-labels.json"
        labels.write_text(
            json.dumps(
                {
                    "labels": [
                        {
                            "command_index": 1,
                            "source_id": "test",
                            "reason": "pytest gates benchmark resolution",
                            "confidence": 100,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        full = tmp / "full.json"
        full.write_text(json.dumps({"instance_id": "django__issue", "resolved": True, "elapsed_sec": 30.0}), encoding="utf-8")
        repo = tmp / "repo"
        repo.mkdir()
        return raw, labels, full, repo

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

    def test_generate_only_creates_standard_bundle_and_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw, labels, full, repo = self.write_inputs(tmp)
            out_dir = tmp / "bundle"

            self.run_prepare(
                "--out-dir",
                str(out_dir),
                "--raw-agent-output",
                str(raw),
                "--raw-format",
                "swe-agent",
                "--source",
                "swe-agent-fixture",
                "--instance-id",
                "django__issue",
                "--repo-dir",
                str(repo),
                "--frontier-annotations",
                str(labels),
                "--annotator",
                "human-a",
                "--full-harness-result",
                str(full),
                "--created-at",
                "2026-01-01T00:00:00+00:00",
            )

            manifest = json.loads((out_dir / "bundle-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], "flowcausal.agent_trace_bundle_manifest.v1")
            self.assertEqual(manifest["status"], "matrix-ready")
            annotated = pathlib.Path(manifest["artifacts"]["trace_for_matrix"])
            endpoint = pathlib.Path(manifest["artifacts"]["full_harness_endpoint"])
            self.assertTrue(annotated.exists())
            self.assertTrue(endpoint.exists())
            trace = json.loads(annotated.read_text(encoding="utf-8"))
            self.assertIn("frontier_label_annotations", trace["provenance"])
            run_matrix = (out_dir / "run-matrix.sh").read_text(encoding="utf-8")
            self.assertIn("--include-oracle-baseline", run_matrix)
            self.assertIn(str(annotated), run_matrix)

    def test_existing_matrix_summary_runs_bundle_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw, labels, full, repo = self.write_inputs(tmp)
            out_dir = tmp / "bundle"
            # The generated annotated trace path is deterministic, so the
            # fixture matrix can reference it before the prepare script runs.
            annotated = out_dir / "traces" / "django__issue.annotated.flowcausal-trace.json"
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(annotated)), encoding="utf-8")
            harness_summary = tmp / "harness-matrix-summary.json"
            live = tmp / "live"
            live.mkdir()
            victim = live / "victim.log"
            victim.write_text('{"type":"victim_summary","iterations":1,"returncode":0,"lateness":{"p99_ms":1.0,"max_ms":2.0}}\n', encoding="utf-8")
            harness_summary.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.real_agent_harness_matrix.v1",
                        "trials": [
                            {
                                "variant": "flowcausal_width",
                                "trial": 2,
                                "raw_agent_output": str(raw),
                                "full_harness_result": str(full),
                                "victim_log": str(victim),
                                "live_dir": str(live),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            harness_audit = tmp / "harness-matrix-audit.json"
            harness_audit.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.real_agent_harness_matrix_audit.v1",
                        "status": "pass",
                        "chains": [
                            {"name": "endpoint improvement", "status": "pass"},
                            {"name": "safety/fairness", "status": "pass"},
                            {"name": "baseline comparison", "status": "pass"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.run_prepare(
                "--out-dir",
                str(out_dir),
                "--raw-agent-output",
                str(raw),
                "--raw-format",
                "swe-agent",
                "--instance-id",
                "django__issue",
                "--repo-dir",
                str(repo),
                "--frontier-annotations",
                str(labels),
                "--full-harness-result",
                str(full),
                "--matrix-summary",
                str(matrix),
                "--harness-matrix-summary",
                str(harness_summary),
                "--harness-matrix-audit",
                str(harness_audit),
                "--victim-log",
                str(victim),
                "--live-dir",
                str(live),
                "--harness-matrix-source-variant",
                "flowcausal_width",
                "--harness-matrix-source-trial",
                "2",
                "--harness-matrix-trial-selection",
                "median-elapsed",
                "--created-at",
                "2026-01-01T00:00:00+00:00",
            )

            report = json.loads((out_dir / "agent-trace-bundle-audit.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass", report["gates"])
            self.assertEqual(report["inputs"]["harness_matrix_audit"], str(harness_audit.resolve()))
            self.assertEqual(report["inputs"]["victim_log"], str(victim.resolve()))
            markdown = out_dir / "agent-trace-bundle-audit.md"
            self.assertTrue(markdown.exists())
            self.assertIn("harness selected trial victim_log matches", markdown.read_text(encoding="utf-8"))
            manifest = json.loads((out_dir / "bundle-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "audited")
            self.assertEqual(manifest["inputs"]["harness_matrix_audit"], str(harness_audit.resolve()))
            self.assertEqual(manifest["inputs"]["harness_matrix_source_variant"], "flowcausal_width")
            self.assertEqual(manifest["inputs"]["harness_matrix_source_trial"], 2)
            self.assertEqual(manifest["artifacts"]["victim_log"], str(victim.resolve()))
            self.assertEqual(manifest["artifacts"]["bundle_audit_markdown"], str(markdown))
            self.assertTrue((out_dir / "run-audit.sh").exists())
            audit_script = (out_dir / "run-audit.sh").read_text(encoding="utf-8")
            self.assertIn("--markdown-output", audit_script)
            self.assertIn("--harness-matrix-audit", audit_script)
            self.assertIn("--victim-log", audit_script)
            self.assertIn("--harness-matrix-source-trial 2", audit_script)

    def test_missing_annotations_generate_disabled_template(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw, _labels, full, repo = self.write_inputs(tmp)
            out_dir = tmp / "bundle"

            self.run_prepare(
                "--out-dir",
                str(out_dir),
                "--raw-agent-output",
                str(raw),
                "--raw-format",
                "swe-agent",
                "--instance-id",
                "django__issue",
                "--repo-dir",
                str(repo),
                "--full-harness-result",
                str(full),
                "--created-at",
                "2026-01-01T00:00:00+00:00",
            )

            manifest = json.loads((out_dir / "bundle-manifest.json").read_text(encoding="utf-8"))
            template = pathlib.Path(manifest["artifacts"]["frontier_annotation_template"])
            self.assertTrue(template.exists())
            data = json.loads(template.read_text(encoding="utf-8"))
            self.assertTrue(data["labels"])
            self.assertTrue(all(row["enabled"] is False for row in data["labels"]))
            run_matrix = (out_dir / "run-matrix.sh").read_text(encoding="utf-8")
            self.assertNotIn("--include-oracle-baseline", run_matrix)


if __name__ == "__main__":
    unittest.main()

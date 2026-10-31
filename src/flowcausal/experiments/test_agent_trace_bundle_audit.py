#!/usr/bin/env python3
"""Tests for real-agent trace evidence bundle auditing."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_agent_trace_bundle.py"


def file_sha256(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def matrix_summary(
    *,
    trace_path: pathlib.Path | None = None,
    include_static_baselines: bool = True,
) -> dict:
    stage_trial = {
        "workload": {
            "workload_begin": {
                "workload": "agent_trace_workflow",
                "trace_file": str(trace_path) if trace_path is not None else "",
                "instance_id": "django__issue",
                "command_count": 3,
            },
            "stage_records": [
                {"stage": "TEST", "elapsed_sec": 1.0, "command_index": 2, "source_id": "test"}
            ]
        }
    }
    variants = [
        {
            "name": "default_taskset",
            "kind": "default",
            "aggregate": {
                "critical_mean_sec": 10.0,
                "characterization_mean": {
                    "frontier_wait_per_frontier_sample_ns": 4_000_000,
                    "frontier_sched_wait_delta_ns": 40_000_000,
                    "samples_with_frontier": 10,
                },
                "victim_mean": {
                    "iterations": 10,
                    "lateness_p95_ms": 5.0,
                    "lateness_p99_ms": 7.0,
                    "lateness_max_ms": 10.0,
                },
            },
            "trials": [stage_trial, stage_trial, stage_trial],
        },
        {
            "name": "flowcausal_width",
            "kind": "flowcausal",
            "aggregate": {
                "critical_mean_sec": 8.0,
                "controller": {"frontier_records": 10},
                "flowmon_mean": {"frontier_enqueues": 25, "frontier_runnable_wait_ns": 20_000_000},
                "characterization_mean": {
                    "frontier_wait_per_frontier_sample_ns": 2_000_000,
                    "frontier_sched_wait_delta_ns": 20_000_000,
                    "samples_with_frontier": 10,
                },
                "victim_mean": {
                    "iterations": 10,
                    "lateness_p95_ms": 5.1,
                    "lateness_p99_ms": 7.2,
                    "lateness_max_ms": 10.2,
                },
            },
            "trials": [stage_trial, stage_trial, stage_trial],
        },
    ]
    if include_static_baselines:
        variants.extend(
            [
                {"name": "static_cgroup", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.8}, "trials": []},
                {"name": "nice_priority", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.6}, "trials": []},
                {"name": "static_affinity", "kind": "baseline", "aggregate": {"critical_mean_sec": 9.7}, "trials": []},
                {"name": "oracle_frontier", "kind": "flowcausal", "aggregate": {"critical_mean_sec": 7.5}, "trials": []},
            ]
        )
    return {
        "config": {
            "command": (
                f"python3 experiments/workloads/agent_trace_workflow.py --trace-file {trace_path}"
                if trace_path is not None
                else "python3 experiments/workloads/agent_trace_workflow.py"
            )
        },
        "variants": variants,
    }


def harness_matrix_audit() -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_matrix_audit.v1",
        "status": "pass",
        "highest_supported_scope": "full-harness endpoint-improvement evidence with victim safety/fairness",
        "summary": {
            "instance_id": "django__issue",
            "baseline_variant": "default",
            "flowcausal_variant": "flowcausal_width",
            "min_trials": 3,
            "require_victim_safety": True,
        },
        "chains": [
            {"name": "endpoint improvement", "status": "pass", "evidence": ["full harness endpoint improves"], "blockers": []},
            {"name": "safety/fairness", "status": "pass", "evidence": ["victim p99 bounded"], "blockers": []},
            {"name": "baseline comparison", "status": "pass", "evidence": ["default/default_observed/no_width/width"], "blockers": []},
            {"name": "reproducibility/artifact", "status": "pass", "evidence": ["raw/endpoint/capture archived"], "blockers": []},
        ],
        "gates": [],
        "blockers": [],
    }


class AgentTraceBundleAuditTests(unittest.TestCase):
    def run_audit(self, *args: str, expect_success: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(AUDIT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_success:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return json.loads(proc.stdout)

    def test_complete_bundle_passes_structural_audit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text(json.dumps({"trajectory": []}), encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "swe-agent",
                        "source_format": "swe-agent",
                        "instance_id": "django__issue",
                        "commands": [
                            {"id": "inspect", "cmd": "open x.py", "stage": "INSPECT"},
                            {"id": "edit", "cmd": "git apply /tmp/fix.patch", "stage": "EDIT"},
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "manual", "role": "foreground-gate"},
                            },
                        ],
                        "provenance": {"input_file": str(raw), "input_sha256": file_sha256(raw)},
                    }
                ),
                encoding="utf-8",
            )
            normalized_data = json.loads(normalized.read_text(encoding="utf-8"))
            normalized_data["provenance"]["frontier_label_annotations"] = [
                {
                    "tool": "flowcausal/experiments/annotate_frontier_labels.py",
                    "annotation_file": str(tmp / "manual-labels.json"),
                    "annotation_sha256": "0" * 64,
                    "annotated_count": 1,
                    "applied": [{"command_index": 2, "source_id": "test"}],
                }
            ]
            normalized.write_text(json.dumps(normalized_data), encoding="utf-8")
            endpoint_src = tmp / "external-endpoint.json"
            endpoint_src.write_text(json.dumps({"instance_id": "django__issue", "resolved": True, "elapsed_sec": 27.0}), encoding="utf-8")
            full = tmp / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.full_harness_endpoint.v1",
                        "source": "swe-bench",
                        "source_format": "swe-bench",
                        "instance_id": "django__issue",
                        "resolved": True,
                        "elapsed_sec": 30.0,
                        "provenance": {"input_file": str(endpoint_src), "input_sha256": file_sha256(endpoint_src)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(trace_path=normalized)), encoding="utf-8")

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
                "--full-harness-result",
                str(full),
                "--matrix-summary",
                str(matrix),
            )

            self.assertEqual(report["status"], "pass")
            self.assertEqual(report["highest_supported_scope"], "L5-ready evidence bundle")
            self.assertTrue(all(g["status"] == "pass" for g in report["gates"]), report["gates"])

    def test_heuristic_label_and_missing_full_harness_are_not_l5(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text(json.dumps({"trajectory": []}), encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "swe-agent",
                        "source_format": "swe-agent",
                        "instance_id": "django__issue",
                        "commands": [
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "flowcausal-normalizer-heuristic"},
                            }
                        ],
                        "provenance": {"input_file": str(raw), "input_sha256": file_sha256(raw)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(
                json.dumps(matrix_summary(trace_path=normalized, include_static_baselines=False)),
                encoding="utf-8",
            )

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
                "--matrix-summary",
                str(matrix),
                expect_success=True,
            )

            self.assertEqual(report["status"], "partial")
            frontier_gate = next(g for g in report["gates"] if g["name"] == "frontier correctness")
            endpoint_gate = next(g for g in report["gates"] if g["name"] == "endpoint improvement")
            baseline_gate = next(g for g in report["gates"] if g["name"] == "baseline comparison")
            self.assertEqual(frontier_gate["status"], "partial")
            self.assertIn("heuristic", " ".join(frontier_gate["blockers"]))
            self.assertEqual(endpoint_gate["status"], "partial")
            self.assertEqual(baseline_gate["status"], "partial")

    def test_instance_mismatch_blocks_endpoint_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text("{}", encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "django__issue",
                        "commands": [
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "manual"},
                            }
                        ],
                        "provenance": {"input_file": str(raw), "input_sha256": file_sha256(raw)},
                    }
                ),
                encoding="utf-8",
            )
            full = tmp / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.full_harness_endpoint.v1",
                        "instance_id": "sympy__issue",
                        "resolved": True,
                        "provenance": {"input_file": str(raw), "input_sha256": file_sha256(raw)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(trace_path=normalized)), encoding="utf-8")

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
                "--full-harness-result",
                str(full),
                "--matrix-summary",
                str(matrix),
                expect_success=True,
            )

            endpoint_gate = next(g for g in report["gates"] if g["name"] == "endpoint improvement")
            self.assertEqual(endpoint_gate["status"], "partial")
            self.assertIn("instance_id mismatch", " ".join(endpoint_gate["blockers"]))

    def test_missing_checksum_and_trace_reference_block_reproducibility(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "raw.traj"
            raw.write_text("{}", encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "django__issue",
                        "commands": [
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "manual"},
                            }
                        ],
                        "provenance": {"input_file": str(raw)},
                    }
                ),
                encoding="utf-8",
            )
            full = tmp / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.full_harness_endpoint.v1",
                        "instance_id": "django__issue",
                        "resolved": True,
                        "provenance": {"input_file": str(raw), "input_sha256": file_sha256(raw)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(trace_path=None)), encoding="utf-8")

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
                "--full-harness-result",
                str(full),
                "--matrix-summary",
                str(matrix),
            )

            repro_gate = next(g for g in report["gates"] if g["name"] == "reproducibility/artifact")
            self.assertEqual(repro_gate["status"], "partial")
            blockers = " ".join(repro_gate["blockers"])
            self.assertIn("input_sha256", blockers)
            self.assertIn("trace-file", blockers)

    def test_full_harness_matrix_audit_contributes_endpoint_safety_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            endpoint_src = tmp / "external-endpoint.json"
            endpoint_src.write_text(json.dumps({"instance_id": "django__issue", "resolved": True, "elapsed_sec": 27.0}), encoding="utf-8")
            raw = tmp / "raw.traj"
            raw.write_text(json.dumps({"trajectory": []}), encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "swe-agent",
                        "source_format": "swe-agent",
                        "instance_id": "django__issue",
                        "commands": [
                            {"id": "inspect", "cmd": "open x.py", "stage": "INSPECT"},
                            {"id": "edit", "cmd": "git apply /tmp/fix.patch", "stage": "EDIT"},
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "manual", "role": "foreground-gate"},
                            },
                        ],
                        "provenance": {
                            "input_file": str(raw),
                            "input_sha256": file_sha256(raw),
                            "frontier_label_annotations": [
                                {
                                    "annotation_file": str(tmp / "manual-labels.json"),
                                    "annotation_sha256": "0" * 64,
                                    "annotated_count": 1,
                                    "applied": [{"command_index": 2, "source_id": "test"}],
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            full = tmp / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.full_harness_endpoint.v1",
                        "source": "swe-bench",
                        "source_format": "swe-bench",
                        "instance_id": "django__issue",
                        "resolved": True,
                        "elapsed_sec": 30.0,
                        "provenance": {"input_file": str(endpoint_src), "input_sha256": file_sha256(endpoint_src)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix_data = matrix_summary(trace_path=normalized)
            matrix_data["claim_gate_audit"] = {
                "highest_supported_level": "fixture with replay outcome/safety failure",
                "gates": [
                    {"name": "stage/episode outcome", "status": "fail", "evidence": [], "blockers": ["replay outcome failed"]},
                    {"name": "controller/BPF admission", "status": "pass", "evidence": [], "blockers": []},
                    {"name": "direct CPU mechanism", "status": "pass", "evidence": [], "blockers": []},
                    {"name": "victim/fairness safety", "status": "fail", "evidence": [], "blockers": ["replay victim failed"]},
                ],
            }
            matrix.write_text(json.dumps(matrix_data), encoding="utf-8")
            harness_summary = tmp / "harness-matrix-summary.json"
            live = tmp / "live"
            live.mkdir()
            victim = live / "victim.log"
            victim.write_text(
                '{"type":"victim_summary","iterations":100,"returncode":0,"lateness":{"p99_ms":1.0,"max_ms":2.0}}\n',
                encoding="utf-8",
            )
            harness_summary_payload = {
                "schema": "flowcausal.real_agent_harness_matrix.v1",
                "trials": [
                    {
                        "variant": "flowcausal_width",
                        "trial": 2,
                        "status": "pass",
                        "raw_agent_output": str(raw),
                        "full_harness_result": str(endpoint_src),
                        "victim_log": str(victim),
                        "live_dir": str(live),
                    }
                ],
            }
            harness_summary.write_text(json.dumps(harness_summary_payload), encoding="utf-8")
            harness_audit = tmp / "harness-matrix-audit.json"
            harness_audit.write_text(json.dumps(harness_matrix_audit()), encoding="utf-8")
            markdown = tmp / "agent-trace-bundle-audit.md"

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
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
                "--markdown-output",
                str(markdown),
            )

            self.assertEqual(report["status"], "pass", report["gates"])
            markdown_text = markdown.read_text(encoding="utf-8")
            self.assertIn("# FlowCausal agent trace bundle audit", markdown_text)
            self.assertIn("| reproducibility/artifact | `pass` |", markdown_text)
            self.assertIn("harness selected trial raw_agent_output matches", markdown_text)
            self.assertIn("harness selected trial victim_log matches", markdown_text)
            endpoint_gate = next(g for g in report["gates"] if g["name"] == "endpoint improvement")
            safety_gate = next(g for g in report["gates"] if g["name"] == "safety/fairness")
            mechanism_gate = next(g for g in report["gates"] if g["name"] == "scheduler mechanism")
            repro_gate = next(g for g in report["gates"] if g["name"] == "reproducibility/artifact")
            self.assertIn("full-harness matrix audit passes endpoint improvement", " ".join(endpoint_gate["evidence"]))
            self.assertIn("full-harness matrix audit passes safety/fairness", " ".join(safety_gate["evidence"]))
            self.assertEqual(mechanism_gate["status"], "pass")
            self.assertIn("full-harness matrix audit exists", " ".join(repro_gate["evidence"]))
            self.assertIn("harness selected trial raw_agent_output matches", " ".join(repro_gate["evidence"]))
            self.assertIn("harness selected trial full_harness_result matches", " ".join(repro_gate["evidence"]))
            self.assertIn("harness selected trial victim_log matches", " ".join(repro_gate["evidence"]))
            self.assertEqual(report["inputs"]["harness_matrix_audit"], str(harness_audit.resolve()))
            self.assertEqual(report["inputs"]["victim_log"], str(victim.resolve()))

            wrong_raw = tmp / "wrong.traj"
            wrong_raw.write_text("{}", encoding="utf-8")
            harness_summary_payload["trials"][0]["raw_agent_output"] = str(wrong_raw)
            harness_summary.write_text(json.dumps(harness_summary_payload), encoding="utf-8")

            mismatch = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
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
            )

            mismatch_repro = next(g for g in mismatch["gates"] if g["name"] == "reproducibility/artifact")
            self.assertEqual(mismatch_repro["status"], "partial")
            self.assertIn("raw_agent_output does not match", " ".join(mismatch_repro["blockers"]))

    def test_harness_matrix_artifacts_require_selected_source_trial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            endpoint_src = tmp / "external-endpoint.json"
            endpoint_src.write_text(
                json.dumps({"instance_id": "django__issue", "resolved": True, "elapsed_sec": 27.0}),
                encoding="utf-8",
            )
            raw = tmp / "raw.traj"
            raw.write_text(json.dumps({"trajectory": []}), encoding="utf-8")
            normalized = tmp / "trace.json"
            normalized.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "instance_id": "django__issue",
                        "commands": [
                            {
                                "id": "test",
                                "cmd": "pytest tests/test_x.py",
                                "stage": "TEST",
                                "critical": True,
                                "frontier_label": {"source": "manual", "role": "foreground-gate"},
                            }
                        ],
                        "provenance": {
                            "input_file": str(raw),
                            "input_sha256": file_sha256(raw),
                            "frontier_label_annotations": [
                                {
                                    "annotation_file": str(tmp / "manual-labels.json"),
                                    "annotation_sha256": "0" * 64,
                                    "annotated_count": 1,
                                    "applied": [{"command_index": 0, "source_id": "test"}],
                                }
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )
            full = tmp / "full.json"
            full.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.full_harness_endpoint.v1",
                        "instance_id": "django__issue",
                        "resolved": True,
                        "elapsed_sec": 30.0,
                        "provenance": {"input_file": str(endpoint_src), "input_sha256": file_sha256(endpoint_src)},
                    }
                ),
                encoding="utf-8",
            )
            matrix = tmp / "matrix-summary.json"
            matrix.write_text(json.dumps(matrix_summary(trace_path=normalized)), encoding="utf-8")
            harness_summary = tmp / "harness-matrix-summary.json"
            harness_summary.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.real_agent_harness_matrix.v1",
                        "trials": [
                            {
                                "variant": "flowcausal_width",
                                "trial": 2,
                                "status": "pass",
                                "raw_agent_output": str(raw),
                                "full_harness_result": str(endpoint_src),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            harness_audit = tmp / "harness-matrix-audit.json"
            harness_audit.write_text(json.dumps(harness_matrix_audit()), encoding="utf-8")

            report = self.run_audit(
                "--raw-agent-output",
                str(raw),
                "--normalized-trace",
                str(normalized),
                "--full-harness-result",
                str(full),
                "--matrix-summary",
                str(matrix),
                "--harness-matrix-summary",
                str(harness_summary),
                "--harness-matrix-audit",
                str(harness_audit),
            )

            self.assertEqual(report["status"], "partial")
            endpoint_gate = next(g for g in report["gates"] if g["name"] == "endpoint improvement")
            repro_gate = next(g for g in report["gates"] if g["name"] == "reproducibility/artifact")
            self.assertEqual(endpoint_gate["status"], "pass")
            self.assertEqual(repro_gate["status"], "partial")
            blockers = " ".join(repro_gate["blockers"])
            self.assertIn("harness matrix source variant is missing", blockers)
            self.assertIn("harness matrix source trial is missing", blockers)


if __name__ == "__main__":
    unittest.main()

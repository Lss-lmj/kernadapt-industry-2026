#!/usr/bin/env python3
"""Tests for real-agent evidence dashboard."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
SUMMARY = HERE / "summarize_real_agent_evidence.py"
GATES = (
    "frontier correctness",
    "endpoint improvement",
    "scheduler mechanism",
    "safety/fairness",
    "baseline comparison",
    "reproducibility/artifact",
)


def intake_report(ready: int) -> dict:
    return {
        "schema": "flowcausal.real_agent_manifest_intake.v1",
        "status": "pass" if ready else "fail",
        "summary": {
            "ready_instances": ready,
            "raw_instances": ready,
            "endpoint_instances": ready,
            "repo_instances": ready,
        },
        "instances": [
            {
                "instance_id": f"django__issue-{idx}",
                "ready": True,
                "blockers": [],
                "warnings": [],
            }
            for idx in range(ready)
        ],
    }


def preflight_report(status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.bundle_collection_manifest_preflight.v1",
        "status": status,
        "summary": {"instances": 3, "warnings": 0, "blockers": 0},
        "warnings": [],
        "blockers": [],
    }


def collection_report(*, ready: int, partial_frontier: bool = False) -> dict:
    gate_counts = {}
    for name in GATES:
        passes = ready
        partial = 0
        if partial_frontier and name == "frontier correctness":
            passes = 1
            partial = 1
        gate_counts[name] = {"pass": passes, "partial": partial, "fail": 0, "missing": 0}
    blockers = []
    if ready < 3:
        blockers.append(f"L5-ready bundle count {ready} < required 3")
    if partial_frontier:
        blockers.append("gate 'frontier correctness' has only 1 pass bundle(s)")
    return {
        "schema": "flowcausal.bundle_collection_audit.v1",
        "status": "pass" if ready >= 3 and not partial_frontier else "partial",
        "summary": {
            "bundle_count": ready + (1 if partial_frontier else 0),
            "l5_ready_count": ready,
            "partial_count": 1 if partial_frontier else 0,
            "fail_count": 0,
        },
        "gate_counts": gate_counts,
        "blockers": blockers,
        "top_blockers": [{"blocker": "heuristic frontier labels are not oracle labels", "count": 1}] if partial_frontier else [],
    }


def readiness_report(status: str = "partial") -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_readiness.v1",
        "status": status,
        "run_readiness_status": "pass" if status in {"pass", "partial"} else "fail",
        "evidence_capture_status": "pass" if status == "pass" else "fail",
        "stages": [
            {"name": "framework tools", "status": "pass", "evidence": ["commands=mini-swe-agent, mini-extra"], "blockers": []},
            {"name": "required capture commands", "status": "pass", "evidence": ["mini-extra=/tmp/mini-extra"], "blockers": []},
            {"name": "api credentials", "status": "pass" if status == "pass" else "fail", "evidence": [], "blockers": []},
        ],
        "blockers": [] if status == "pass" else ["captured manifest instances 0 < required 3"],
        "next_actions": [] if status == "pass" else ["run capture_real_agent_run.py for three selected instances"],
    }


def bootstrap_report(*, mode: str = "generated", status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_bootstrap.v1",
        "status": status,
        "mode": mode,
        "backends": ["mini-swe-agent", "swebench-pip"],
        "artifacts": {
            "install_script": "/tmp/install-harness.sh",
            "env_script": "/tmp/env.sh",
            "post_install_readiness_script": "/tmp/post-install-readiness.sh",
        },
        "blockers": [],
        "next_actions": ["review /tmp/install-harness.sh", "run /tmp/install-harness.sh when ready"],
    }


def capture_plan_preflight_report(status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.real_agent_capture_plan_preflight.v1",
        "status": status,
        "summary": {
            "selected_instances": 3,
            "min_instances": 3,
            "row_pass": 3 if status == "pass" else 2,
            "row_fail": 0 if status == "pass" else 1,
            "mode": "mini-swe-agent",
        },
        "script_check": {"status": "pass" if status == "pass" else "fail"},
        "blockers": [] if status == "pass" else ["mini-extra is not executable: /tmp/old/mini-extra"],
    }


def harness_matrix_plan_report(status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_matrix_plan.v1",
        "status": status,
        "summary": {
            "selected_instances": 3,
            "row_pass": 3 if status == "pass" else 2,
            "row_fail": 0 if status == "pass" else 1,
            "trials": 3,
            "variants": ["default", "default_observed", "flowcausal_no_width", "flowcausal_width"],
            "execute": False,
        },
        "blockers": [] if status == "pass" else ["sympy__sympy-21171: mini-extra missing"],
    }


def harness_matrix_runbook_preflight_report(status: str = "pass") -> dict:
    return {
        "schema": "flowcausal.real_agent_harness_matrix_runbook_preflight.v1",
        "status": status,
        "summary": {
            "rows": 3,
            "row_pass": 3 if status == "pass" else 2,
            "row_fail": 0 if status == "pass" else 1,
            "min_trials": 3,
            "required_variants": ["default", "nice_priority", "static_affinity", "static_cgroup", "flowcausal_width"],
        },
        "blockers": [] if status == "pass" else ["django__django-1: static_cgroup trial count 0 < required 3"],
    }


def repo_prep_report(*, missing_base_commit: int = 0) -> dict:
    return {
        "schema": "flowcausal.real_agent_repo_preparation.v1",
        "status": "pass",
        "summary": {
            "selected_instances": 3,
            "repo_rows": 3,
            "missing_base_commit": missing_base_commit,
        },
        "warnings": [f"issue-{idx}: missing base_commit; checkout is not pinned" for idx in range(missing_base_commit)],
        "blockers": [],
    }


def harness_matrix_audit_report(status: str = "pass") -> dict:
    endpoint_status = status
    safety_status = "pass" if status == "pass" else "partial"
    coverage_status = "pass" if status == "pass" else "partial"
    artifact_status = "pass" if status == "pass" else "partial"
    return {
        "schema": "flowcausal.real_agent_harness_matrix_audit.v1",
        "status": status,
        "highest_supported_scope": (
            "full-harness endpoint-improvement evidence with victim safety/fairness"
            if status == "pass"
            else "partial full-harness endpoint evidence"
        ),
        "summary": {
            "input_summary": "/tmp/harness-matrix-summary.json",
            "instance_id": "django__issue-1",
            "baseline_variant": "default",
            "flowcausal_variant": "flowcausal_width",
            "min_trials": 3,
            "require_victim_safety": status == "pass",
        },
        "gates": [
            {
                "name": "endpoint improvement",
                "status": endpoint_status,
                "blockers": [] if status == "pass" else ["flowcausal_width is slower than default by 5.0%"],
                "next_actions": [] if status == "pass" else ["collect more full-harness trials or revise the scheduling policy"],
            },
            {
                "name": "safety/fairness",
                "status": safety_status,
                "blockers": [] if safety_status == "pass" else ["victim safety missing"],
                "next_actions": [] if safety_status == "pass" else ["run live full-harness trials with --victim-command"],
            }
        ],
        "chains": [
            {
                "name": "frontier correctness",
                "status": "missing",
                "blockers": ["full-harness endpoint matrix does not prove selected frontier correctness"],
                "next_actions": ["add manual/oracle frontier annotations and audit the replay bundle"],
            },
            {
                "name": "endpoint improvement",
                "status": endpoint_status,
                "blockers": [] if endpoint_status == "pass" else ["flowcausal_width is slower than default by 5.0%"],
                "next_actions": [] if endpoint_status == "pass" else ["collect more full-harness trials or revise the scheduling policy"],
            },
            {
                "name": "scheduler mechanism",
                "status": "partial" if status == "pass" else "missing",
                "blockers": ["endpoint matrix alone does not prove direct CPU/frontier-wait mechanism"],
                "next_actions": ["run trace replay/claim-gate audit with controller, flowmon, and schedstat evidence"],
            },
            {
                "name": "safety/fairness",
                "status": safety_status,
                "blockers": [] if safety_status == "pass" else ["victim safety missing"],
                "next_actions": [] if safety_status == "pass" else ["run live full-harness trials with --victim-command"],
            },
            {
                "name": "baseline comparison",
                "status": coverage_status,
                "blockers": [] if coverage_status == "pass" else ["baseline/variant coverage incomplete"],
                "next_actions": [] if coverage_status == "pass" else ["include observed default and no-width ablation"],
            },
            {
                "name": "reproducibility/artifact",
                "status": artifact_status,
                "blockers": [] if artifact_status == "pass" else ["artifact paths incomplete"],
                "next_actions": [] if artifact_status == "pass" else ["archive raw trajectory and endpoint artifacts"],
            }
        ],
        "blockers": [] if status == "pass" else ["flowcausal_width is slower than default by 5.0%"],
    }


def repo_stage_matrix_report(
    *,
    label: str = "sympy21171-real-repo-3trial",
    default_critical: float = 3.8,
    default_total: float = 4.5,
    flow_critical: float = 3.7,
    flow_total: float = 5.4,
    direct_status: str = "partial",
    outcome_status: str = "pass",
    safety_status: str = "fail",
) -> dict:
    def variant(name: str, critical: float, total: float, victim_p99: float = 0.2) -> dict:
        return {
            "name": name,
            "trials": 3,
            "aggregate": {
                "critical_mean_sec": critical,
                "critical_summary": {"n": 3},
                "total_mean_sec": total,
                "victim_mean": {
                    "lateness_p99_ms": victim_p99,
                    "lateness_max_ms": victim_p99 * 2,
                },
            },
        }

    return {
        "claim_gate_audit": {
            "highest_supported_level": "L1: mechanism connectivity",
            "gates": [
                {"name": "direct CPU mechanism", "status": direct_status},
                {"name": "stage/episode outcome", "status": outcome_status},
                {"name": "victim/fairness safety", "status": safety_status},
            ],
        },
        "config": {"label": label},
        "created_at": "2026-07-20 03:01:49 +0800",
        "variants": [
            variant("default_taskset", default_critical, default_total, victim_p99=0.3),
            variant("static_affinity", default_critical - 0.1, default_total - 0.1, victim_p99=0.1),
            variant("flowcausal_width", flow_critical, flow_total, victim_p99=9.0),
        ],
    }


def mechanism_smoke_matrix_report() -> dict:
    def variant(
        name: str,
        *,
        critical: float,
        elapsed: float,
        frontier: float = 0.0,
        fallback: float = 0.0,
        labels: float = 0.0,
        oracle_rc: int | None = None,
    ) -> dict:
        trial = {
            "path": f"/tmp/{name}/trial-00",
            "manifest": {},
            "workload": {
                "returncode": 0,
                "workload_elapsed_sec": elapsed,
                "frontier_label_count": int(labels),
            },
        }
        if oracle_rc is not None:
            trial["manifest"]["oracle_frontier_driver_returncode"] = oracle_rc
        return {
            "name": name,
            "trials": [trial],
            "aggregate": {
                "critical_mean_sec": critical,
                "critical_summary": {"n": 1},
                "workload_elapsed_mean_sec": elapsed,
                "total_mean_sec": elapsed + 1.0,
                "workload_frontier_label_count_mean": labels,
                "victim_mean": {
                    "lateness_p99_ms": 0.15,
                    "lateness_max_ms": 0.20,
                },
                "flowmon_mean": {
                    "frontier_enqueues": frontier,
                    "fallback_enqueues": fallback,
                },
            },
        }

    return {
        "claim_gate_audit": {
            "highest_supported_level": "L1: mechanism connectivity",
            "gates": [
                {"name": "direct CPU mechanism", "status": "partial"},
                {"name": "stage/episode outcome", "status": "partial"},
                {"name": "victim/fairness safety", "status": "partial"},
            ],
        },
        "config": {"label": "django14608-wakeup-minblock5ms-oracle-desc-cleanrc-1trial"},
        "created_at": "2026-07-20 12:31:20 +0800",
        "variants": [
            variant("default_taskset", critical=1.23, elapsed=1.43, labels=4),
            variant("oracle_frontier", critical=1.22, elapsed=1.42, frontier=15, fallback=0, labels=4, oracle_rc=0),
            variant("flowcausal_width", critical=1.10, elapsed=1.30, frontier=70, fallback=2, labels=3),
        ],
    }


class SummarizeRealAgentEvidenceTests(unittest.TestCase):
    def run_summary(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(SUMMARY), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_no_inputs_report_missing_real_agent_evidence(self) -> None:
        report = self.run_summary("--min-ready-bundles", "3")

        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["highest_supported_scope"], "not paper-ready")
        self.assertIn("no intake report provided", report["blockers"])
        self.assertTrue(any(row["status"] == "missing" for row in report["chains"]))

    def test_partial_collection_stays_not_paper_ready(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            intake = tmp / "intake.json"
            preflight = tmp / "preflight.json"
            collection = tmp / "collection.json"
            intake.write_text(json.dumps(intake_report(ready=2)), encoding="utf-8")
            preflight.write_text(json.dumps(preflight_report("partial")), encoding="utf-8")
            collection.write_text(json.dumps(collection_report(ready=1, partial_frontier=True)), encoding="utf-8")

            report = self.run_summary(
                "--intake-report",
                str(intake),
                "--preflight-report",
                str(preflight),
                "--collection-audit",
                str(collection),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            self.assertIn("ready real-agent artifact triples 2 < required 3", report["blockers"])
            frontier = next(row for row in report["chains"] if row["name"] == "frontier correctness")
            self.assertEqual(frontier["status"], "partial")
            self.assertIn("add manual/oracle frontier annotations", frontier["next_actions"][0])

    def test_complete_reports_produce_paper_ready_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            intake = tmp / "intake.json"
            repo_prep = tmp / "repo-prep.json"
            preflight = tmp / "preflight.json"
            collection = tmp / "collection.json"
            output = tmp / "dashboard.json"
            markdown = tmp / "dashboard.md"
            intake.write_text(json.dumps(intake_report(ready=3)), encoding="utf-8")
            repo_prep.write_text(json.dumps(repo_prep_report(missing_base_commit=0)), encoding="utf-8")
            preflight.write_text(json.dumps(preflight_report("pass")), encoding="utf-8")
            collection.write_text(json.dumps(collection_report(ready=3)), encoding="utf-8")

            report = self.run_summary(
                "--intake-report",
                str(intake),
                "--repo-prep-report",
                str(repo_prep),
                "--preflight-report",
                str(preflight),
                "--collection-audit",
                str(collection),
                "--min-ready-bundles",
                "3",
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
            )

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["highest_supported_scope"], "EuroSys paper-ready real-agent evidence")
            self.assertTrue(output.exists())
            self.assertIn("FlowCausal real-agent evidence dashboard", markdown.read_text(encoding="utf-8"))
            self.assertTrue(all(row["status"] == "pass" for row in report["chains"]))
            repo_stage = next(stage for stage in report["stages"] if stage["name"] == "repo preparation")
            self.assertEqual(repo_stage["status"], "pass")

    def test_readiness_report_is_included_as_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            readiness = tmp / "readiness.json"
            readiness.write_text(json.dumps(readiness_report("partial")), encoding="utf-8")

            report = self.run_summary(
                "--readiness-report",
                str(readiness),
                "--min-ready-bundles",
                "3",
            )

            harness = next(stage for stage in report["stages"] if stage["name"] == "harness readiness")
            self.assertEqual(harness["status"], "partial")
            self.assertIn("run_readiness=pass", harness["evidence"])
            self.assertIn("required capture commands=pass", harness["evidence"])
            self.assertIn("api credentials=fail", harness["evidence"])

    def test_bootstrap_report_is_included_without_upgrading_generated_plan_to_pass(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            bootstrap = tmp / "bootstrap.json"
            bootstrap.write_text(json.dumps(bootstrap_report(mode="generated")), encoding="utf-8")

            report = self.run_summary(
                "--bootstrap-report",
                str(bootstrap),
                "--min-ready-bundles",
                "3",
            )

            stage = next(item for item in report["stages"] if item["name"] == "harness bootstrap")
            self.assertEqual(stage["status"], "partial")
            self.assertIn("mode=generated", stage["evidence"])
            self.assertIn("installer has not been executed", " ".join(stage["blockers"]))
            self.assertEqual(report["inputs"]["bootstrap_report"], str(bootstrap.resolve()))

    def test_capture_plan_preflight_is_reported_as_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            preflight = tmp / "capture-plan-preflight.json"
            preflight.write_text(json.dumps(capture_plan_preflight_report("pass")), encoding="utf-8")

            report = self.run_summary(
                "--capture-plan-preflight",
                str(preflight),
                "--min-ready-bundles",
                "3",
            )

            stage = next(item for item in report["stages"] if item["name"] == "capture plan preflight")
            self.assertEqual(stage["status"], "pass")
            self.assertIn("selected_instances=3", stage["evidence"])
            self.assertIn("script_check=pass", stage["evidence"])
            self.assertEqual(report["inputs"]["capture_plan_preflight"], str(preflight.resolve()))

    def test_harness_matrix_plan_is_reported_as_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            plan = tmp / "harness-matrix-plan.json"
            plan.write_text(json.dumps(harness_matrix_plan_report("pass")), encoding="utf-8")

            report = self.run_summary(
                "--harness-matrix-plan",
                str(plan),
                "--min-ready-bundles",
                "3",
            )

            stage = next(item for item in report["stages"] if item["name"] == "full-harness matrix plan")
            self.assertEqual(stage["status"], "pass")
            self.assertIn("selected_instances=3", stage["evidence"])
            self.assertIn("variants=default,default_observed,flowcausal_no_width,flowcausal_width", stage["evidence"])
            self.assertIn("execute=False", stage["evidence"])
            self.assertIn("execute the generated full-harness matrix scripts", " ".join(stage["next_actions"]))
            self.assertEqual(report["inputs"]["harness_matrix_plan"], str(plan.resolve()))

    def test_harness_matrix_runbook_preflight_is_reported_as_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            preflight = tmp / "harness-matrix-runbook-preflight.json"
            preflight.write_text(json.dumps(harness_matrix_runbook_preflight_report("pass")), encoding="utf-8")

            report = self.run_summary(
                "--harness-matrix-runbook-preflight",
                str(preflight),
                "--min-ready-bundles",
                "3",
            )

            stage = next(item for item in report["stages"] if item["name"] == "full-harness matrix runbook preflight")
            self.assertEqual(stage["status"], "pass")
            self.assertIn("row_pass=3", stage["evidence"])
            self.assertIn("required_variants=default,nice_priority,static_affinity,static_cgroup,flowcausal_width", stage["evidence"])
            self.assertIn("execute the guarded per-instance full-harness matrix run scripts", " ".join(stage["next_actions"]))
            self.assertEqual(report["inputs"]["harness_matrix_runbook_preflight"], str(preflight.resolve()))

    def test_repo_prep_missing_base_commit_keeps_dashboard_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            intake = tmp / "intake.json"
            repo_prep = tmp / "repo-prep.json"
            preflight = tmp / "preflight.json"
            collection = tmp / "collection.json"
            intake.write_text(json.dumps(intake_report(ready=3)), encoding="utf-8")
            repo_prep.write_text(json.dumps(repo_prep_report(missing_base_commit=1)), encoding="utf-8")
            preflight.write_text(json.dumps(preflight_report("pass")), encoding="utf-8")
            collection.write_text(json.dumps(collection_report(ready=3)), encoding="utf-8")

            report = self.run_summary(
                "--intake-report",
                str(intake),
                "--repo-prep-report",
                str(repo_prep),
                "--preflight-report",
                str(preflight),
                "--collection-audit",
                str(collection),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            repo_stage = next(stage for stage in report["stages"] if stage["name"] == "repo preparation")
            self.assertEqual(repo_stage["status"], "partial")
            self.assertIn("1 repo row(s) lack base_commit pinning", repo_stage["blockers"])

    def test_harness_matrix_audit_is_included_as_pipeline_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            intake = tmp / "intake.json"
            preflight = tmp / "preflight.json"
            collection = tmp / "collection.json"
            harness = tmp / "harness-audit.json"
            intake.write_text(json.dumps(intake_report(ready=3)), encoding="utf-8")
            preflight.write_text(json.dumps(preflight_report("pass")), encoding="utf-8")
            collection.write_text(json.dumps(collection_report(ready=3)), encoding="utf-8")
            harness.write_text(json.dumps(harness_matrix_audit_report("partial")), encoding="utf-8")

            report = self.run_summary(
                "--intake-report",
                str(intake),
                "--preflight-report",
                str(preflight),
                "--collection-audit",
                str(collection),
                "--harness-matrix-audit",
                str(harness),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            stage = next(item for item in report["stages"] if item["name"] == "full-harness endpoint matrix")
            self.assertEqual(stage["status"], "partial")
            self.assertIn("flowcausal_width is slower than default by 5.0%", stage["blockers"])
            self.assertIn("add manual/oracle frontier annotations", " ".join(report["next_actions"]))

    def test_harness_matrix_audit_contributes_chain_evidence_without_replacing_collection(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            harness = tmp / "harness-audit.json"
            harness.write_text(json.dumps(harness_matrix_audit_report("pass")), encoding="utf-8")

            report = self.run_summary(
                "--harness-matrix-audit",
                str(harness),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            self.assertEqual(report["highest_supported_scope"], "not paper-ready")
            endpoint = next(row for row in report["chains"] if row["name"] == "endpoint improvement")
            safety = next(row for row in report["chains"] if row["name"] == "safety/fairness")
            frontier = next(row for row in report["chains"] if row["name"] == "frontier correctness")
            self.assertEqual(endpoint["status"], "partial")
            self.assertEqual(endpoint["counts"]["harness_matrix"], "pass")
            self.assertIn("harness matrix evidence exists", " ".join(endpoint["blockers"]))
            self.assertEqual(safety["status"], "partial")
            self.assertEqual(safety["counts"]["harness_matrix"], "pass")
            self.assertEqual(frontier["status"], "missing")
            self.assertEqual(frontier["counts"]["harness_matrix"], "missing")

    def test_repo_stage_matrix_is_reported_without_upgrading_endpoint_claim(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            matrix = tmp / "matrix-summary.json"
            markdown = tmp / "dashboard.md"
            latex = tmp / "table.tex"
            matrix.write_text(json.dumps(repo_stage_matrix_report()), encoding="utf-8")

            report = self.run_summary(
                "--repo-stage-input",
                str(matrix),
                "--markdown-output",
                str(markdown),
                "--latex-output",
                str(latex),
                "--min-ready-bundles",
                "3",
            )

            stage = next(item for item in report["stages"] if item["name"] == "repo-stage matrix evidence")
            self.assertEqual(stage["status"], "partial")
            self.assertIn("repo-stage evidence is not full SWE-agent/OpenHands endpoint evidence", stage["blockers"])
            self.assertEqual(len(report["repo_stage_evidence"]), 1)
            evidence = report["repo_stage_evidence"][0]
            self.assertEqual(evidence["claim_status"], "partial_or_unsafe")
            self.assertLess(evidence["metrics"]["flow_vs_default_critical_delta_pct"], 0)
            self.assertGreater(evidence["metrics"]["flow_vs_default_total_delta_pct"], 0)
            self.assertIn("Repo-stage matrix evidence", markdown.read_text(encoding="utf-8"))
            latex_text = latex.read_text(encoding="utf-8")
            self.assertIn(r"\label{tab:flowcausal-repo-stage-current}", latex_text)
            self.assertIn(r"SymPy 21171", latex_text)
            self.assertIn(r"\%", latex_text)

    def test_repo_stage_dashboard_prefers_workload_elapsed_over_harness_total(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            matrix = tmp / "matrix-summary.json"
            data = repo_stage_matrix_report(
                default_critical=10.0,
                default_total=10.0,
                flow_critical=9.0,
                flow_total=20.0,
                direct_status="partial",
                outcome_status="partial",
                safety_status="pass",
            )
            for variant in data["variants"]:
                aggregate = variant["aggregate"]
                if variant["name"] == "default_taskset":
                    aggregate["workload_elapsed_mean_sec"] = 10.0
                elif variant["name"] == "flowcausal_width":
                    aggregate["workload_elapsed_mean_sec"] = 9.0
            matrix.write_text(json.dumps(data), encoding="utf-8")

            report = self.run_summary(
                "--repo-stage-input",
                str(matrix),
                "--min-ready-bundles",
                "3",
            )

            metrics = report["repo_stage_evidence"][0]["metrics"]
            self.assertLess(metrics["flow_vs_default_total_delta_pct"], 0)
            self.assertGreater(metrics["flow_vs_default_harness_total_delta_pct"], 0)

    def test_mechanism_smoke_is_reported_without_upgrading_paper_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            matrix = tmp / "matrix-summary.json"
            markdown = tmp / "dashboard.md"
            matrix.write_text(json.dumps(mechanism_smoke_matrix_report()), encoding="utf-8")

            report = self.run_summary(
                "--mechanism-smoke-input",
                str(matrix),
                "--markdown-output",
                str(markdown),
                "--min-ready-bundles",
                "3",
            )

            self.assertEqual(report["status"], "partial")
            stage = next(item for item in report["stages"] if item["name"] == "mechanism smoke evidence")
            self.assertEqual(stage["status"], "partial")
            self.assertIn("mechanism smoke is not full SWE-agent/OpenHands endpoint evidence", stage["blockers"])
            self.assertEqual(len(report["mechanism_smoke_evidence"]), 1)
            smoke = report["mechanism_smoke_evidence"][0]
            self.assertEqual(smoke["status"], "connected")
            self.assertEqual(smoke["oracle_driver_returncodes"], [0])
            self.assertEqual(smoke["metrics"]["oracle_frontier_enqueues"], 15.0)
            self.assertEqual(smoke["metrics"]["flowcausal_frontier_enqueues"], 70.0)
            self.assertIn("Mechanism smoke evidence", markdown.read_text(encoding="utf-8"))
            self.assertTrue(all(row["status"] == "missing" for row in report["chains"]))


if __name__ == "__main__":
    unittest.main()

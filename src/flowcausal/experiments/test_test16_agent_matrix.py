#!/usr/bin/env python3
"""Regression tests for the FlowCausal Test16 agent-like matrix wrapper."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import tempfile
import unittest
from typing import Any


MODULE_PATH = pathlib.Path(__file__).with_name("run_test16_agent_matrix.py")


def load_matrix_module() -> Any:
    sys.path.insert(0, str(MODULE_PATH.parent))
    spec = importlib.util.spec_from_file_location("run_test16_agent_matrix", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix_mod = load_matrix_module()


def make_flow_variant(
    *,
    name: str = "flowcausal_width",
    frontier_records: int = 2,
    frontier_enqueues: float = 5.0,
    fallback_enqueues: float = 1.0,
    adaptive: bool = False,
    adaptive_records: int = 0,
    adaptive_widened_records: int = 0,
    victim_iterations: float = 0.0,
    trials: int = 1,
) -> dict[str, Any]:
    trial_list = [{"trial": i} for i in range(trials)]
    return {
        "name": name,
        "kind": "flowcausal",
        "width_control": True,
        "frontier_width": 2,
        "max_frontier_width": 2,
        "adaptive_frontier_width": adaptive,
        "adaptive_max_frontier_width": 8 if adaptive else None,
        "trials": trial_list,
        "aggregate": {
            "test16_summary": {
                "total_n": {"mean": 4.0},
                "success_rate": {"mean": 1.0},
                "timeout_rate": {"mean": 0.0},
                "task_completion_ms_mean": {"mean": 123.0},
                "critical_path_ms_mean": {"mean": 100.0},
                "end_to_end_ms_mean": {"mean": 123.0},
                "cost_proxy_ms_mean": {"mean": 150.0},
                "cpu_total_ms_mean": {"mean": 27.0},
            },
            "workload_summary": {
                "shell_mix": {
                    "n": {"n": 1.0, "mean": 4.0, "std": 0.0, "p95": 4.0, "min": 4.0, "max": 4.0},
                    "success_rate": {"n": 1.0, "mean": 1.0, "std": 0.0, "p95": 1.0, "min": 1.0, "max": 1.0},
                    "timeout_rate": {"n": 1.0, "mean": 0.0, "std": 0.0, "p95": 0.0, "min": 0.0, "max": 0.0},
                    "task_completion_ms_mean": {"n": 1.0, "mean": 123.0, "std": 0.0, "p95": 123.0, "min": 123.0, "max": 123.0},
                    "task_completion_ms_p95": {"n": 1.0, "mean": 130.0, "std": 0.0, "p95": 130.0, "min": 130.0, "max": 130.0},
                    "critical_path_ms_mean": {"n": 1.0, "mean": 100.0, "std": 0.0, "p95": 100.0, "min": 100.0, "max": 100.0},
                    "end_to_end_ms_mean": {"n": 1.0, "mean": 123.0, "std": 0.0, "p95": 123.0, "min": 123.0, "max": 123.0},
                    "cost_proxy_ms_mean": {"n": 1.0, "mean": 150.0, "std": 0.0, "p95": 150.0, "min": 150.0, "max": 150.0},
                    "cpu_total_ms_mean": {"n": 1.0, "mean": 27.0, "std": 0.0, "p95": 27.0, "min": 27.0, "max": 27.0},
                }
            },
            "controller": {
                "frontier_records": frontier_records,
                "selected_samples": frontier_records,
                "propagation_anchors": 0,
                "adaptive_records": adaptive_records,
                "adaptive_enabled_records": adaptive_records,
                "adaptive_widened_records": adaptive_widened_records,
                "adaptive_max_effective_width": 8 if adaptive_records else 0,
                "adaptive_width_counts": {"2": max(0, adaptive_records - adaptive_widened_records), "8": adaptive_widened_records}
                if adaptive_records
                else {},
                "adaptive_reason_counts": {"wide-parent-wait": adaptive_widened_records}
                if adaptive_widened_records
                else {},
            },
            "flowmon_mean": {
                "seen_enqueues": frontier_enqueues + fallback_enqueues,
                "frontier_enqueues": frontier_enqueues,
                "fallback_enqueues": fallback_enqueues,
                "width_held_enqueues": 0.0,
                "frontier_width_releases": 0.0,
                "global_frontier_dispatches": 0.0,
                "global_shared_dispatches": 0.0,
                "global_shared_first_dispatches": 0.0,
                "global_frontier_guard_trips": 0.0,
                "global_frontier_guard_misses": 0.0,
                "frontier_runnable_wait_ns": 0.0,
                "frontier_runtime_ns": 0.0,
                "frontier_ratio": 0.0,
                "fallback_ratio": 0.0,
            },
            "victim_mean": {
                "iterations": victim_iterations,
                "lateness_p95_ms": 0.0,
                "lateness_p99_ms": 0.0,
                "lateness_max_ms": 0.0,
            },
        },
    }


class Test16ParsingTests(unittest.TestCase):
    def test_parse_test16_cases_computes_weighted_means(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "summary_agent_cases.csv"
            path.write_text(
                "\n".join(
                    [
                        "experiment,scheduler,workload,concurrency,n,success_rate,timeout_rate,task_completion_ms_mean,task_completion_ms_p95,critical_path_ms_mean,end_to_end_ms_mean,cost_proxy_ms_mean,cpu_total_ms_mean",
                        "scheduler,default,shell_mix,2,2,1.0,0.0,100,140,90,100,120,20",
                        "scheduler,default,python_test,2,1,0.0,1.0,300,360,280,300,340,50",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_test16_cases(path)

        self.assertEqual(parsed["total_n"], 3.0)
        self.assertAlmostEqual(parsed["success_rate"], 2.0 / 3.0)
        self.assertAlmostEqual(parsed["timeout_rate"], 1.0 / 3.0)
        self.assertAlmostEqual(parsed["task_completion_ms_mean"], (2 * 100 + 1 * 300) / 3)
        self.assertEqual(parsed["workloads"], ["python_test", "shell_mix"])
        self.assertEqual(parsed["by_workload"]["shell_mix"]["n"], 2.0)

    def test_parse_test16_runs_converts_boolean_fields(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "summary_agent_runs.csv"
            path.write_text(
                "\n".join(
                    [
                        "experiment,scheduler,workload,concurrency,run_id,agent_id,timeout,success,task_completion_ms,cpu_total_ms",
                        "scheduler,default,shell_mix,2,1,0,False,True,100,20",
                        "scheduler,default,shell_mix,2,1,1,True,False,500,30",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_test16_runs(path)

        self.assertEqual(parsed["agents"], 2)
        self.assertEqual(parsed["successes"], 1)
        self.assertEqual(parsed["timeouts"], 1)
        self.assertIs(parsed["rows"][0]["success"], True)
        self.assertIs(parsed["rows"][1]["timeout"], True)

    def test_aggregate_trials_preserves_per_workload_summary(self) -> None:
        def case(workload: str, n: float, success: float, task_ms: float, cpu_ms: float) -> dict[str, float]:
            return {
                "n": n,
                "success_rate": success,
                "timeout_rate": 1.0 - success,
                "task_completion_ms_mean": task_ms,
                "task_completion_ms_p95": task_ms + 10.0,
                "critical_path_ms_mean": task_ms - 10.0,
                "end_to_end_ms_mean": task_ms,
                "cost_proxy_ms_mean": task_ms + cpu_ms,
                "cpu_total_ms_mean": cpu_ms,
            }

        trial_a = {
            "test16": {
                "cases": {
                    "total_n": 3.0,
                    "success_rate": 1.0,
                    "timeout_rate": 0.0,
                    "task_completion_ms_mean": 100.0,
                    "task_completion_ms_p95": 120.0,
                    "critical_path_ms_mean": 90.0,
                    "end_to_end_ms_mean": 100.0,
                    "cost_proxy_ms_mean": 130.0,
                    "cpu_total_ms_mean": 30.0,
                    "by_workload": {
                        "shell_mix": case("shell_mix", 2.0, 1.0, 80.0, 30.0),
                        "python_test": case("python_test", 1.0, 1.0, 140.0, 30.0),
                    },
                }
            },
            "controller": {},
            "flowmon": {},
            "victim": {},
        }
        trial_b = {
            "test16": {
                "cases": {
                    "total_n": 2.0,
                    "success_rate": 0.5,
                    "timeout_rate": 0.5,
                    "task_completion_ms_mean": 200.0,
                    "task_completion_ms_p95": 240.0,
                    "critical_path_ms_mean": 180.0,
                    "end_to_end_ms_mean": 200.0,
                    "cost_proxy_ms_mean": 260.0,
                    "cpu_total_ms_mean": 60.0,
                    "by_workload": {
                        "shell_mix": case("shell_mix", 2.0, 0.5, 200.0, 60.0),
                    },
                }
            },
            "controller": {},
            "flowmon": {},
            "victim": {},
        }

        aggregate = matrix_mod.aggregate_trials([trial_a, trial_b])

        workloads = aggregate["workload_summary"]
        self.assertEqual(sorted(workloads), ["python_test", "shell_mix"])
        self.assertAlmostEqual(workloads["shell_mix"]["task_completion_ms_mean"]["mean"], 140.0)
        self.assertAlmostEqual(workloads["shell_mix"]["success_rate"]["mean"], 0.75)
        self.assertAlmostEqual(workloads["python_test"]["n"]["mean"], 1.0)

    def test_aggregate_trials_counts_failed_live_harness_trials(self) -> None:
        failed = {
            "manifest": {"live_harness_failed": True, "live_harness_returncode": 143},
            "test16": {"cases": {}},
            "controller": {},
            "flowmon": {},
            "victim": {},
        }
        ok = {
            "manifest": {"live_harness_returncode": 0},
            "test16": {"cases": {}},
            "controller": {},
            "flowmon": {},
            "victim": {},
        }

        aggregate = matrix_mod.aggregate_trials([failed, ok])

        self.assertEqual(aggregate["trial_status"]["total"], 2)
        self.assertEqual(aggregate["trial_status"]["successful"], 1)
        self.assertEqual(aggregate["trial_status"]["failed"], 1)
        self.assertEqual(aggregate["trial_status"]["failed_returncodes"], {"143": 1})


class Test16AuditTests(unittest.TestCase):
    def test_default_variant_never_supports_flowcausal_claims(self) -> None:
        claim = matrix_mod.audit_variant({"name": "default_taskset", "kind": "default"})

        self.assertEqual(claim["highest_level"], "baseline")
        self.assertIn("FlowCausal speedup", claim["forbidden_claims"])
        self.assertIn("no controller trace", claim["blockers_for_next_level"])

    def test_live_variant_reaches_only_l1_mechanism_connectivity(self) -> None:
        claim = matrix_mod.audit_variant(make_flow_variant())

        self.assertEqual(claim["highest_level"], "L1: mechanism connectivity")
        self.assertTrue(
            any("controller selected 2 frontier records" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertIn("FlowCausal accelerates code agents", claim["forbidden_claims"])
        self.assertIn("fewer than 3 trials; not enough for performance or safety claims", claim["blockers_for_next_level"])

    def test_live_variant_without_bpf_admission_remains_l0(self) -> None:
        claim = matrix_mod.audit_variant(
            make_flow_variant(frontier_records=2, frontier_enqueues=0.0, fallback_enqueues=0.0)
        )

        self.assertEqual(claim["highest_level"], "L0: runnable/instrumented only")
        self.assertTrue(
            any("flowmon-final lacks positive frontier admission" in item for item in claim["blockers_for_next_level"]),
            claim["blockers_for_next_level"],
        )

    def test_failed_live_variant_is_not_l1_even_with_partial_counters(self) -> None:
        variant = make_flow_variant(frontier_records=2, frontier_enqueues=5.0)
        variant["aggregate"]["trial_status"] = {
            "total": 1,
            "successful": 0,
            "failed": 1,
            "failed_returncodes": {"143": 1},
        }

        claim = matrix_mod.audit_variant(variant)

        self.assertEqual(claim["highest_level"], "L0: runnable/instrumented only")
        self.assertTrue(
            any("live trial(s) failed" in item for item in claim["blockers_for_next_level"]),
            claim["blockers_for_next_level"],
        )

    def test_safety_counters_are_reported_as_mechanism_evidence(self) -> None:
        variant = make_flow_variant(frontier_records=2, frontier_enqueues=5.0)
        variant.update(
            {
                "frontier_dispatch_quota": 1,
                "guard_cooldown_dispatches": 2,
                "pressure_fallback_enqueues": 4,
                "pressure_fallback_ms": 20,
                "shared_wait_pressure_us": 2000,
                "pressure_dispatch_ms": 5,
                "frontier_dispatch_window_ms": 20,
                "frontier_dispatch_window_budget": 1,
            }
        )
        variant["aggregate"]["flowmon_mean"].update(
            {
                "global_frontier_guard_trips": 7.0,
                "global_shared_cooldown_entries": 3.0,
                "global_pressure_fallback_entries": 5.0,
                "global_pressure_fallback_enqueues": 4.0,
                "global_shared_wait_pressure_events": 2.0,
                "global_pressure_dispatch_entries": 2.0,
                "global_frontier_budget_trips": 6.0,
            }
        )

        claim = matrix_mod.audit_variant(variant)

        evidence = "; ".join(claim["evidence"])
        self.assertIn("dispatch guard tripped", evidence)
        self.assertIn("shared-first cooldown entries", evidence)
        self.assertIn("pressure fallback entries/enqueues", evidence)
        self.assertIn("shared-wait pressure events", evidence)
        self.assertIn("pressure dispatch backoff entries", evidence)
        self.assertIn("dispatch-window budget trips", evidence)

    def test_adaptive_variant_adds_audit_but_forbids_performance_claims(self) -> None:
        claim = matrix_mod.audit_variant(
            make_flow_variant(
                name="flowcausal_width_adaptive8",
                adaptive=True,
                adaptive_records=3,
                adaptive_widened_records=2,
            )
        )

        self.assertEqual(claim["highest_level"], "L1: mechanism connectivity")
        self.assertTrue(any("adaptive width audit records=3" in item for item in claim["evidence"]), claim["evidence"])
        self.assertIn(
            "adaptive width improves performance or correctly identifies the critical cohort",
            claim["forbidden_claims"],
        )

    def test_csv_and_markdown_make_adaptive_width_visible(self) -> None:
        variant = make_flow_variant(
            name="flowcausal_width_adaptive8",
            adaptive=True,
            adaptive_records=3,
            adaptive_widened_records=2,
        )
        matrix = {
            "config": {"include_adaptive_width_variant": True},
            "variants": [variant],
        }
        matrix["claim_audit"] = matrix_mod.audit_matrix(matrix)

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            csv_path = tmp / "matrix-summary.csv"
            workload_csv_path = tmp / "matrix-workloads.csv"
            md_path = tmp / "README.md"

            matrix_mod.write_csv(matrix, csv_path)
            matrix_mod.write_workload_csv(matrix, workload_csv_path)
            matrix_mod.write_markdown(matrix, md_path)

            csv_text = csv_path.read_text(encoding="utf-8")
            workload_csv_text = workload_csv_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        header = csv_text.splitlines()[0]
        self.assertIn("adaptive_frontier_width", header)
        self.assertIn("failed_trials", header)
        self.assertIn("frontier_dispatch_quota", header)
        self.assertIn("frontier_action", header)
        self.assertIn("global_frontier_budget_trips_mean", header)
        self.assertIn("controller_adaptive_widened_records", header)
        self.assertIn("frontier_runtime_ms_mean", header)
        self.assertIn("flowcausal_width_adaptive8", csv_text)
        self.assertIn("variant,kind,workload", workload_csv_text.splitlines()[0])
        self.assertIn("flowcausal_width_adaptive8,flowcausal,shell_mix", workload_csv_text)
        self.assertIn("| flowcausal_width_adaptive8 | 2/2 | - | on/3/2/8 |", md_text)
        self.assertIn("Safety counters", md_text)
        self.assertIn("Frontier rec/enq", md_text)
        self.assertIn("Variant-local audit", md_text)
        self.assertIn("Per-workload Test16 summary", md_text)
        self.assertIn("| flowcausal_width_adaptive8 | shell_mix |", md_text)
        self.assertIn("Variant-local instrumentation audit", md_text)
        self.assertIn("does not override the claim-gate audit", md_text)
        self.assertIn("FlowCausal accelerates code agents", md_text)


if __name__ == "__main__":
    unittest.main()

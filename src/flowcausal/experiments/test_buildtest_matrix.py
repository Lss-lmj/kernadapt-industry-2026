#!/usr/bin/env python3
"""Regression tests for FlowCausal build/test matrix audit semantics."""

from __future__ import annotations

import importlib.util
import pathlib
import tempfile
import unittest
from typing import Any


MODULE_PATH = pathlib.Path(__file__).with_name("run_buildtest_matrix.py")


def load_matrix_module() -> Any:
    spec = importlib.util.spec_from_file_location("run_buildtest_matrix", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix_mod = load_matrix_module()


def flow_variant(
    *,
    name: str = "flowcausal_width_policy_fallback",
    policy_fallback: bool = True,
    adaptive_frontier_width: bool = False,
    adaptive_records: int = 0,
    adaptive_enabled_records: int = 0,
    adaptive_widened_records: int = 0,
    adaptive_max_effective_width: int = 0,
    pressure_fallback_enqueues: int = 0,
    pressure_fallback_ms: int = 0,
    pressure_dispatch_ms: int = 0,
    frontier_records: int = 3,
    selected_samples: int = 2,
    seen_enqueues: int = 10,
    frontier_enqueues: int = 0,
    fallback_enqueues: int = 7,
    frontier_dispatch_window_ms: int = 0,
    frontier_dispatch_window_budget: int = 0,
    frontier_budget_trips: int = 0,
    frontier_budget_shared: int = 0,
    frontier_budget_misses: int = 0,
    pressure_dispatch_entries: int = 0,
    pressure_dispatches: int = 0,
    pressure_dispatch_misses: int = 0,
    victim_iterations: int = 0,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "flowcausal",
        "width_control": True,
        "frontier_width": 2,
        "max_frontier_width": 2,
        "adaptive_frontier_width": adaptive_frontier_width,
        "adaptive_max_frontier_width": 8 if adaptive_frontier_width else None,
        "policy_fallback": policy_fallback,
        "frontier_dispatch_quota": 4,
        "guard_cooldown_dispatches": 0,
        "pressure_fallback_enqueues": pressure_fallback_enqueues,
        "pressure_fallback_ms": pressure_fallback_ms,
        "pressure_dispatch_ms": pressure_dispatch_ms,
        "frontier_dispatch_window_ms": frontier_dispatch_window_ms,
        "frontier_dispatch_window_budget": frontier_dispatch_window_budget,
        "trials": [{"trial": 0}],
        "aggregate": {
            "stage_summary": {},
            "total_summary": {
                "mean_sec": 0.0,
                "std_sec": 0.0,
                "min_sec": 0.0,
                "max_sec": 0.0,
            },
            "total_mean_sec": 0.0,
            "controller": {
                "frontier_records": frontier_records,
                "selected_samples": selected_samples,
                "propagation_anchors": 0,
                "stability_suppressed": 0,
                "stability_held": 0,
                "adaptive_records": adaptive_records,
                "adaptive_enabled_records": adaptive_enabled_records,
                "adaptive_widened_records": adaptive_widened_records,
                "adaptive_max_effective_width": adaptive_max_effective_width,
                "adaptive_width_counts": (
                    {
                        "2": max(0, adaptive_records - adaptive_widened_records),
                        "8": adaptive_widened_records,
                    }
                    if adaptive_records
                    else {}
                ),
                "adaptive_reason_counts": (
                    {"wide-parent-wait": adaptive_widened_records}
                    if adaptive_widened_records
                    else {}
                ),
            },
            "flowmon_mean": {
                "seen_enqueues": seen_enqueues,
                "frontier_enqueues": frontier_enqueues,
                "fallback_enqueues": fallback_enqueues,
                "width_held_enqueues": 0,
                "global_frontier_dispatches": 0,
                "global_shared_dispatches": 0,
                "global_shared_first_dispatches": 0,
                "global_frontier_guard_trips": 0,
                "global_frontier_guard_misses": 0,
                "global_shared_cooldown_entries": 0,
                "global_shared_cooldown_dispatches": 0,
                "global_shared_cooldown_misses": 0,
                "global_pressure_fallback_entries": 0,
                "global_pressure_fallback_enqueues": 0,
                "global_shared_wait_pressure_events": 0,
                "global_shared_wait_pressure_wait_ns": 0,
                "global_pressure_dispatch_entries": pressure_dispatch_entries,
                "global_pressure_dispatches": pressure_dispatches,
                "global_pressure_dispatch_misses": pressure_dispatch_misses,
                "global_frontier_budget_windows": 0,
                "global_frontier_budget_trips": frontier_budget_trips,
                "global_frontier_budget_shared": frontier_budget_shared,
                "global_frontier_budget_misses": frontier_budget_misses,
                "frontier_ratio": 0.0,
                "fallback_ratio": fallback_enqueues / seen_enqueues if seen_enqueues else 0.0,
                "budget_used_ns": 0.0,
                "budget_total_ns": 0.0,
                "budget_used_ratio": 0.0,
                "frontier_runnable_wait_ns": 0.0,
                "frontier_runtime_ns": 0.0,
            },
            "victim_mean": {
                "iterations": victim_iterations,
                "lateness_p95_ms": 0.0,
                "lateness_p99_ms": 0.0,
                "lateness_max_ms": 0.0,
            },
        },
    }


def default_variant() -> dict[str, Any]:
    return {
        "name": "default_taskset",
        "kind": "default",
        "trials": [{"trial": 0}],
        "aggregate": {
            "stage_summary": {},
            "total_summary": {
                "mean_sec": 0.0,
                "std_sec": 0.0,
                "min_sec": 0.0,
                "max_sec": 0.0,
            },
            "total_mean_sec": 0.0,
            "controller": {},
            "flowmon_mean": {},
            "victim_mean": {},
        },
    }


class PolicyFallbackAuditTests(unittest.TestCase):
    def test_parse_characterization_summary_derives_normalized_wait(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "summary.json"
            path.write_text(
                """{
  "characterization_level": "C1",
  "sample_count": 10,
  "samples_with_frontier": 4,
  "samples_with_schedstat": 5,
  "sampled_elapsed_sec": 2.0,
  "aggregate_delta": {
    "frontier_sched_wait_delta_ns": 8000000,
    "frontier_sched_runtime_delta_ns": 16000000,
    "sched_wait_delta_ns": 12000000,
    "sched_runtime_delta_ns": 24000000
  },
  "stage_delta_summary": {
    "TEST": {
      "sample_count": 6,
      "samples_with_frontier": 3,
      "samples_with_schedstat": 4,
      "aggregate_delta": {
        "frontier_sched_wait_delta_ns": 6000000,
        "frontier_sched_runtime_delta_ns": 12000000,
        "sched_wait_delta_ns": 9000000,
        "sched_runtime_delta_ns": 18000000
      },
      "frontier_wait_share": 0.6666667,
      "frontier_runtime_share": 0.6666667
    }
  },
  "frontier_wait_share": 0.6666667,
  "frontier_runtime_share": 0.6666667
}
""",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_characterization_summary(path)

        self.assertEqual(parsed["frontier_wait_per_frontier_sample_ns"], 2_000_000.0)
        self.assertEqual(parsed["frontier_wait_per_schedstat_sample_ns"], 1_600_000.0)
        self.assertEqual(parsed["frontier_wait_per_elapsed_sec"], 4_000_000.0)
        self.assertEqual(parsed["frontier_runtime_per_frontier_sample_ns"], 4_000_000.0)
        self.assertEqual(
            parsed["stage_delta_summary"]["TEST"]["frontier_wait_per_frontier_sample_ns"],
            2_000_000.0,
        )

    def test_aggregate_trials_preserves_characterization_means(self) -> None:
        aggregate = matrix_mod.aggregate_trials(
            [
                {
                    "workload": {"stage_seconds": {"BUILD": [1.0]}, "total_sec": 1.0},
                    "controller": {},
                    "flowmon": {},
                    "victim": {},
                    "characterization": {
                        "sample_count": 10.0,
                        "samples_with_frontier": 8.0,
                        "samples_with_schedstat": 6.0,
                        "frontier_sched_wait_delta_ns": 4_000_000.0,
                        "sched_wait_delta_ns": 8_000_000.0,
                        "frontier_wait_share": 0.5,
                        "frontier_wait_per_frontier_sample_ns": 500_000.0,
                        "frontier_wait_per_elapsed_sec": 2_000_000.0,
                    },
                },
                {
                    "workload": {"stage_seconds": {"BUILD": [3.0]}, "total_sec": 3.0},
                    "controller": {},
                    "flowmon": {},
                    "victim": {},
                    "characterization": {
                        "sample_count": 20.0,
                        "samples_with_frontier": 16.0,
                        "samples_with_schedstat": 10.0,
                        "frontier_sched_wait_delta_ns": 2_000_000.0,
                        "sched_wait_delta_ns": 4_000_000.0,
                        "frontier_wait_share": 0.5,
                        "frontier_wait_per_frontier_sample_ns": 125_000.0,
                        "frontier_wait_per_elapsed_sec": 1_000_000.0,
                    },
                },
            ]
        )

        char = aggregate["characterization_mean"]
        self.assertEqual(char["sample_count"], 15.0)
        self.assertEqual(char["samples_with_frontier"], 12.0)
        self.assertEqual(char["samples_with_schedstat"], 8.0)
        self.assertEqual(char["frontier_sched_wait_delta_ns"], 3_000_000.0)
        self.assertEqual(char["sched_wait_delta_ns"], 6_000_000.0)
        self.assertEqual(char["frontier_wait_per_frontier_sample_ns"], 312_500.0)
        self.assertEqual(char["frontier_wait_per_elapsed_sec"], 1_500_000.0)

    def test_policy_fallback_variant_is_l1_safety_connectivity(self) -> None:
        claim = matrix_mod.audit_variant_claim(flow_variant())

        self.assertEqual(claim["highest_level"], "L1: safety-fallback connectivity")
        self.assertIn("explicit policy fallback routing", claim["summary"])
        self.assertTrue(
            any("fallback_enqueues=7.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertIn("victim slowdown bound", claim["forbidden_claims"])
        self.assertIn("fairness improvement", claim["forbidden_claims"])

    def test_fallback_counters_without_policy_fallback_do_not_promote_claim(self) -> None:
        claim = matrix_mod.audit_variant_claim(flow_variant(policy_fallback=False))

        self.assertEqual(claim["highest_level"], "L0: runnable/instrumented only")
        self.assertTrue(
            any("frontier admission or fallback routing" in item for item in claim["blockers_for_next_level"]),
            claim["blockers_for_next_level"],
        )

    def test_matrix_audit_recognizes_policy_fallback_variant(self) -> None:
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_policy_fallback_variant": True,
                },
                "variants": [default_variant(), flow_variant()],
            }
        )

        self.assertEqual(audit["highest_supported_claim_level"], "L1: mechanism connectivity")
        self.assertNotIn(
            "no explicit policy-fallback safety-valve variant",
            audit["paper_claim_gaps"],
        )
        self.assertTrue(
            any("explicit policy fallback" in item for item in audit["permitted_claims"]),
            audit["permitted_claims"],
        )

    def test_csv_and_markdown_make_policy_fallback_visible(self) -> None:
        variant = flow_variant()
        claim = matrix_mod.audit_variant_claim(variant)
        matrix = {
            "config": {"include_policy_fallback_variant": True},
            "claim_audit": {
                "highest_supported_claim_level": "L1: mechanism connectivity",
                "summary": "test audit",
                "permitted_claims": ["explicit policy fallback can be audited"],
                "forbidden_claims": ["victim slowdown is bounded"],
                "paper_claim_gaps": [],
                "variant_claims": [claim],
            },
            "variants": [default_variant(), variant],
        }

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            csv_path = tmp / "matrix-summary.csv"
            md_path = tmp / "README.md"

            matrix_mod.write_csv(matrix, csv_path)
            matrix_mod.write_markdown(matrix, md_path)

            csv_text = csv_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        self.assertIn("policy_fallback", csv_text.splitlines()[0])
        self.assertIn("flowcausal_width_policy_fallback", csv_text)
        self.assertIn(",True,", csv_text)
        self.assertIn("Policy fallback", md_text)
        self.assertIn("| flowcausal_width_policy_fallback |", md_text)


class PressureFallbackAuditTests(unittest.TestCase):
    def test_pressure_fallback_variant_is_l1_connectivity_when_epoch_fires(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_guard1_pressure2",
            policy_fallback=False,
            pressure_fallback_enqueues=2,
            frontier_enqueues=0,
            fallback_enqueues=5,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 3
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 5

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: pressure-fallback connectivity")
        self.assertIn("shared-pressure-triggered admission fallback", claim["summary"])
        self.assertTrue(
            any("pressure fallback mean=entries=3.0, enqueues=5.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertIn("victim slowdown bound", claim["forbidden_claims"])

    def test_pressure_fallback_variant_keeps_frontier_admission_evidence(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_guard1_pressure1",
            policy_fallback=False,
            pressure_fallback_enqueues=1,
            frontier_enqueues=4,
            fallback_enqueues=5,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 4
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 4

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: mechanism + pressure-fallback connectivity")
        self.assertIn("BPF frontier admission", claim["summary"])
        self.assertIn("shared-pressure-triggered admission fallback", claim["summary"])
        self.assertTrue(
            any("frontier_enqueues=4.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertTrue(
            any("pressure fallback mean=entries=4.0, enqueues=4.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )

    def test_pressure_fallback_ttl_only_variant_is_l1_connectivity(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_guard1_pressure_ttl20",
            policy_fallback=False,
            pressure_fallback_enqueues=0,
            pressure_fallback_ms=20,
            frontier_enqueues=0,
            fallback_enqueues=5,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 2
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 5

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: pressure-fallback connectivity")
        self.assertIn("shared-pressure-triggered admission fallback", claim["summary"])

    def test_pressure_dispatch_combo_is_distinct_l1_connectivity(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_shared_wait_pressure_dispatch5",
            policy_fallback=False,
            pressure_fallback_enqueues=4,
            pressure_fallback_ms=20,
            pressure_dispatch_ms=5,
            frontier_enqueues=8,
            fallback_enqueues=12,
            pressure_dispatch_entries=6,
            pressure_dispatches=14,
            pressure_dispatch_misses=21,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 6
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 12

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(
            claim["highest_level"],
            "L1: mechanism + pressure-fallback+pressure-dispatch connectivity",
        )
        self.assertIn("dispatch-side shared-first backoff", claim["summary"])
        self.assertTrue(
            any("pressure dispatch mean=entries=6.0, shared=14.0, misses=21.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )

    def test_matrix_audit_recognizes_pressure_fallback_variants(self) -> None:
        pressure_variant = flow_variant(
            name="flowcausal_width_guard1_pressure2",
            policy_fallback=False,
            pressure_fallback_enqueues=2,
        )
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_pressure_fallback_variants": True,
                },
                "variants": [default_variant(), pressure_variant],
            }
        )

        self.assertNotIn(
            "no shared-pressure admission-fallback variants",
            audit["paper_claim_gaps"],
        )
        self.assertTrue(
            any("shared-pressure admission fallback" in item for item in audit["permitted_claims"]),
            audit["permitted_claims"],
        )

    def test_matrix_audit_promotes_actual_pressure_connectivity(self) -> None:
        pressure_variant = flow_variant(
            name="flowcausal_width_guard1_pressure1",
            policy_fallback=False,
            pressure_fallback_enqueues=1,
            frontier_enqueues=4,
            fallback_enqueues=5,
        )
        pressure_variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 4
        pressure_variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 4

        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_pressure_fallback_variants": True,
                },
                "variants": [default_variant(), pressure_variant],
            }
        )

        self.assertEqual(
            audit["highest_supported_claim_level"],
            "L1: mechanism + pressure-fallback connectivity",
        )

    def test_matrix_audit_promotes_pressure_dispatch_connectivity(self) -> None:
        pressure_variant = flow_variant(
            name="flowcausal_width_shared_wait_pressure_dispatch5",
            policy_fallback=False,
            pressure_fallback_enqueues=4,
            pressure_fallback_ms=20,
            pressure_dispatch_ms=5,
            frontier_enqueues=8,
            fallback_enqueues=12,
            pressure_dispatch_entries=6,
            pressure_dispatches=14,
            pressure_dispatch_misses=21,
        )
        pressure_variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 6
        pressure_variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 12

        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "pressure_dispatch_ms": 5,
                },
                "variants": [default_variant(), pressure_variant],
            }
        )

        self.assertEqual(
            audit["highest_supported_claim_level"],
            "L1: mechanism + pressure-fallback+pressure-dispatch connectivity",
        )

    def test_matrix_audit_keeps_pressure_gap_without_actual_variant(self) -> None:
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_pressure_fallback_variants": True,
                },
                "variants": [default_variant(), flow_variant()],
            }
        )

        self.assertIn(
            "no shared-pressure admission-fallback variants",
            audit["paper_claim_gaps"],
        )


class DispatchBudgetAuditTests(unittest.TestCase):
    def test_dispatch_budget_pressure_combo_gets_combined_l1(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget1",
            policy_fallback=False,
            pressure_fallback_enqueues=4,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=1,
            frontier_budget_trips=6,
            frontier_budget_shared=2,
            frontier_budget_misses=4,
            frontier_enqueues=5,
            fallback_enqueues=7,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 2
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 6

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(
            claim["highest_level"],
            "L1: mechanism + pressure+dispatch-budget connectivity",
        )
        self.assertIn("dispatch-window shared-first probe", claim["summary"])
        self.assertIn("pressure fallback", claim["summary"])

    def test_matrix_audit_promotes_pressure_dispatch_budget_combo(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget1",
            policy_fallback=False,
            pressure_fallback_enqueues=4,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=1,
            frontier_budget_trips=3,
            frontier_budget_shared=1,
            frontier_budget_misses=2,
            frontier_enqueues=4,
            fallback_enqueues=6,
        )
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_entries"] = 1
        variant["aggregate"]["flowmon_mean"]["global_pressure_fallback_enqueues"] = 4
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_dispatch_budget_variants": True,
                    "include_pressure_fallback_variants": True,
                },
                "variants": [default_variant(), variant],
            }
        )

        self.assertEqual(
            audit["highest_supported_claim_level"],
            "L1: mechanism + pressure+dispatch-budget connectivity",
        )

    def test_dispatch_budget_with_frontier_admission_gets_combined_l1(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget1",
            policy_fallback=False,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=1,
            frontier_budget_trips=6,
            frontier_budget_shared=2,
            frontier_budget_misses=4,
            frontier_enqueues=5,
            fallback_enqueues=1,
        )

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: mechanism + dispatch-budget connectivity")
        self.assertIn("BPF frontier admission", claim["summary"])
        self.assertIn("windowed frontier-dispatch budget path", claim["summary"])

    def test_dispatch_budget_variant_is_l1_connectivity_when_budget_trips(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget2",
            policy_fallback=False,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=2,
            frontier_budget_trips=6,
            frontier_budget_shared=2,
            frontier_budget_misses=4,
            frontier_enqueues=0,
            fallback_enqueues=0,
        )

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: dispatch-budget connectivity")
        self.assertIn("windowed frontier-dispatch budget path", claim["summary"])
        self.assertTrue(
            any("dispatch-window budget mean=trips=6.0, shared=2.0, misses=4.0" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertIn("victim slowdown bound", claim["forbidden_claims"])

    def test_matrix_audit_promotes_actual_mechanism_dispatch_budget_connectivity(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget1",
            policy_fallback=False,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=1,
            frontier_budget_trips=3,
            frontier_budget_shared=1,
            frontier_budget_misses=2,
            frontier_enqueues=4,
            fallback_enqueues=2,
        )
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_dispatch_budget_variants": True,
                },
                "variants": [default_variant(), variant],
            }
        )

        self.assertEqual(
            audit["highest_supported_claim_level"],
            "L1: mechanism + dispatch-budget connectivity",
        )

    def test_matrix_audit_recognizes_dispatch_budget_variants(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget1",
            policy_fallback=False,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=1,
            frontier_budget_trips=3,
            frontier_budget_shared=1,
            frontier_budget_misses=2,
            frontier_enqueues=0,
            fallback_enqueues=0,
        )
        audit = matrix_mod.audit_matrix_claims(
            {
                "config": {
                    "trials": 1,
                    "include_dispatch_budget_variants": True,
                },
                "variants": [default_variant(), variant],
            }
        )

        self.assertEqual(audit["highest_supported_claim_level"], "L1: dispatch-budget connectivity")
        self.assertNotIn(
            "no frontier dispatch-window budget ablation variants",
            audit["paper_claim_gaps"],
        )
        self.assertTrue(
            any("frontier dispatch-window budget" in item for item in audit["permitted_claims"]),
            audit["permitted_claims"],
        )

    def test_csv_and_markdown_make_dispatch_budget_visible(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_dbudget2",
            policy_fallback=False,
            frontier_dispatch_window_ms=20,
            frontier_dispatch_window_budget=2,
            frontier_budget_trips=6,
            frontier_budget_shared=2,
            frontier_budget_misses=4,
        )
        claim = matrix_mod.audit_variant_claim(variant)
        matrix = {
            "config": {"include_dispatch_budget_variants": True},
            "claim_audit": {
                "highest_supported_claim_level": "L1: dispatch-budget connectivity",
                "summary": "test audit",
                "permitted_claims": ["frontier dispatch-window budget can be audited"],
                "forbidden_claims": ["victim slowdown is bounded"],
                "paper_claim_gaps": [],
                "variant_claims": [claim],
            },
            "variants": [default_variant(), variant],
        }

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            csv_path = tmp / "matrix-summary.csv"
            md_path = tmp / "README.md"

            matrix_mod.write_csv(matrix, csv_path)
            matrix_mod.write_markdown(matrix, md_path)

            csv_text = csv_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        header = csv_text.splitlines()[0]
        self.assertIn("frontier_dispatch_window_ms", header)
        self.assertIn("pressure_fallback_ms", header)
        self.assertIn("global_frontier_budget_trips_mean", header)
        self.assertIn("flowcausal_width_dbudget2", csv_text)
        self.assertIn(",20,2,", csv_text)
        self.assertIn("Dispatch budget trip/shared/miss", md_text)
        self.assertIn("| flowcausal_width_dbudget2 |", md_text)
        self.assertIn("| 20 ms/2 |", md_text)
        self.assertIn("| 6.0/2.0/4.0 |", md_text)


class AdaptiveWidthAuditTests(unittest.TestCase):
    def test_parse_controller_collects_adaptive_width_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "controller.ndjson"
            rows = [
                {
                    "type": "flowctl_watch",
                    "tick": 0,
                    "stage": "BUILD",
                    "frontiers": [{"pid": 10, "comm": "cc1", "score": 200, "reason": "compiler worker"}],
                    "adaptive_width": {
                        "enabled": True,
                        "requested_frontier_width": 2,
                        "requested_max_frontier_width": 2,
                        "adaptive_max_frontier_width": 8,
                        "frontier_width": 8,
                        "max_frontier_width": 8,
                        "reason": "wide-parent-wait",
                        "serviceable_leaves": 9,
                        "eligible_candidates": 8,
                        "parent_wait_candidates": 6,
                        "barrier_tail_width": 4,
                    },
                },
                {
                    "type": "flowctl_watch",
                    "tick": 1,
                    "stage": "TEST",
                    "frontiers": [],
                    "adaptive_width": {
                        "enabled": True,
                        "requested_frontier_width": 2,
                        "requested_max_frontier_width": 2,
                        "adaptive_max_frontier_width": 8,
                        "frontier_width": 2,
                        "max_frontier_width": 2,
                        "reason": "requested-width",
                    },
                },
            ]
            path.write_text("\n".join(matrix_mod.json.dumps(row) for row in rows) + "\n")

            parsed = matrix_mod.parse_controller(path)

        self.assertEqual(parsed["adaptive_records"], 2)
        self.assertEqual(parsed["adaptive_enabled_records"], 2)
        self.assertEqual(parsed["adaptive_widened_records"], 1)
        self.assertEqual(parsed["adaptive_max_effective_width"], 8)
        self.assertEqual(parsed["adaptive_width_counts"], {"8": 1, "2": 1})
        self.assertEqual(parsed["adaptive_reason_counts"]["wide-parent-wait"], 1)
        self.assertEqual(parsed["frontier_records"], 1)

    def test_parse_controller_collects_frontier_action_admission_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "controller.ndjson"
            rows = [
                {
                    "type": "flowctl_watch",
                    "tick": 0,
                    "stage": "BUILD",
                    "frontiers": [
                        {
                            "pid": 10,
                            "comm": "cc1",
                            "score": 303,
                            "reason": "compiler worker,state=R,leaf,parent-wait:make,barrier-tail:1",
                        }
                    ],
                    "frontier_action": {
                        "requested": "advance",
                        "action": "advance",
                        "policy_fallback": False,
                        "reason": "requested-advance,build-tail-only-observe:max_tail=1,link_tail=0",
                        "task_observe_only_pids": [10],
                        "build_frontier_admission": "tail-only",
                        "build_admission_triggered": True,
                        "build_admission_reason": "build-tail-only-observe:max_tail=1,link_tail=0",
                        "build_selected_max_barrier_tail": 1,
                    },
                },
                {
                    "type": "flowctl_watch",
                    "tick": 1,
                    "stage": "TEST",
                    "frontiers": [
                        {
                            "pid": 11,
                            "comm": "test_runner",
                            "score": 270,
                            "reason": "test worker,state=R,leaf,parent-wait:python3",
                        }
                    ],
                    "frontier_action": {
                        "requested": "advance",
                        "action": "advance",
                        "policy_fallback": False,
                        "reason": "requested-advance",
                        "build_frontier_admission": "tail-only",
                        "build_admission_triggered": False,
                    },
                },
            ]
            path.write_text("\n".join(matrix_mod.json.dumps(row) for row in rows) + "\n")

            parsed = matrix_mod.parse_controller(path)

        self.assertEqual(parsed["frontier_action_records"], 2)
        self.assertEqual(parsed["frontier_action_observe_records"], 0)
        self.assertEqual(parsed["frontier_action_policy_fallback_records"], 0)
        self.assertEqual(parsed["build_admission_triggered_records"], 1)
        self.assertEqual(parsed["frontier_action_counts"], {"advance": 2})
        self.assertEqual(parsed["build_admission_mode_counts"], {"tail-only": 2})
        self.assertEqual(
            parsed["build_admission_reason_counts"],
            {"build-tail-only-observe:max_tail=1,link_tail=0": 1},
        )
        self.assertEqual(parsed["frontier_action_examples"][0]["stage"], "BUILD")

    def test_adaptive_width_variant_adds_audit_evidence(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_adaptive8",
            policy_fallback=False,
            adaptive_frontier_width=True,
            adaptive_records=3,
            adaptive_enabled_records=3,
            adaptive_widened_records=2,
            adaptive_max_effective_width=8,
            frontier_enqueues=4,
            fallback_enqueues=1,
        )

        claim = matrix_mod.audit_variant_claim(variant)

        self.assertEqual(claim["highest_level"], "L1: mechanism connectivity")
        self.assertTrue(
            any("adaptive width audit present" in item for item in claim["evidence"]),
            claim["evidence"],
        )
        self.assertIn(
            "adaptive width improves performance or correctly identifies the critical cohort",
            claim["forbidden_claims"],
        )

    def test_csv_and_markdown_make_adaptive_width_visible(self) -> None:
        variant = flow_variant(
            name="flowcausal_width_adaptive8",
            policy_fallback=False,
            adaptive_frontier_width=True,
            adaptive_records=3,
            adaptive_enabled_records=3,
            adaptive_widened_records=2,
            adaptive_max_effective_width=8,
            frontier_enqueues=4,
            fallback_enqueues=1,
        )
        claim = matrix_mod.audit_variant_claim(variant)
        matrix = {
            "config": {"include_adaptive_width_variant": True},
            "claim_audit": {
                "highest_supported_claim_level": "L1: mechanism connectivity",
                "summary": "test audit",
                "permitted_claims": ["static and adaptive frontier-width choices can be audited"],
                "forbidden_claims": ["adaptive width improves performance"],
                "paper_claim_gaps": [],
                "variant_claims": [claim],
            },
            "variants": [default_variant(), variant],
        }

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            csv_path = tmp / "matrix-summary.csv"
            md_path = tmp / "README.md"

            matrix_mod.write_csv(matrix, csv_path)
            matrix_mod.write_markdown(matrix, md_path)

            csv_text = csv_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        header = csv_text.splitlines()[0]
        self.assertIn("adaptive_frontier_width", header)
        self.assertIn("controller_adaptive_widened_records", header)
        self.assertIn("flowcausal_width_adaptive8", csv_text)
        self.assertIn("| flowcausal_width_adaptive8 | 2/2 | on/3/2/8 |", md_text)


if __name__ == "__main__":
    unittest.main()

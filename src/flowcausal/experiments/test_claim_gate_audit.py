#!/usr/bin/env python3
"""Regression tests for claim-gate auditing."""

from __future__ import annotations

import unittest

import audit_claim_gate


def variant(
    name: str,
    *,
    total_sec: float = 0.0,
    critical_sec: float = 0.0,
    frontier_records: int = 0,
    frontier_enqueues: float = 0.0,
    frontier_wait_ns: float = 0.0,
    char_frontier_wait_ns: float = 0.0,
    victim_p95: float = 0.0,
    victim_p99: float = 0.0,
    victim_max: float = 0.0,
    trials: int = 3,
    hint_dry_run: bool = False,
) -> dict:
    return {
        "name": name,
        "kind": "default" if name.startswith("default") else "flowcausal",
        "hint_dry_run": hint_dry_run,
        "trials": [{} for _ in range(trials)],
        "aggregate": {
            "total_mean_sec": total_sec,
            "critical_mean_sec": critical_sec,
            "controller": {
                "frontier_records": frontier_records,
            },
            "flowmon_mean": {
                "frontier_enqueues": frontier_enqueues,
                "fallback_enqueues": 0.0,
                "width_held_enqueues": 0.0,
                "frontier_runnable_wait_ns": frontier_wait_ns,
            },
            "victim_mean": {
                "lateness_p95_ms": victim_p95,
                "lateness_p99_ms": victim_p99,
                "lateness_max_ms": victim_max,
            },
            "characterization_mean": {
                "sample_count": 10.0 if char_frontier_wait_ns else 0.0,
                "samples_with_frontier": 10.0 if char_frontier_wait_ns else 0.0,
                "samples_with_schedstat": 8.0 if char_frontier_wait_ns else 0.0,
                "frontier_sched_wait_delta_ns": char_frontier_wait_ns,
                "sched_wait_delta_ns": char_frontier_wait_ns * 2.0,
            },
        },
    }


class ClaimGateAuditTests(unittest.TestCase):
    def test_l1_when_admission_exists_but_no_direct_mechanism_baseline(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant("default_taskset", total_sec=10.0, victim_p95=2.0, victim_p99=4.0, victim_max=8.0),
                    variant(
                        "flowcausal_width",
                        total_sec=9.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        frontier_wait_ns=1_000_000,
                        victim_p95=2.1,
                        victim_p99=4.2,
                        victim_max=8.1,
                    ),
                ]
            }
        )

        self.assertEqual(report["highest_supported_level"], "L1: mechanism connectivity")
        self.assertEqual(report["gates"][1]["status"], audit_claim_gate.STATUS_PASS)
        self.assertEqual(report["gates"][2]["status"], audit_claim_gate.STATUS_PARTIAL)

    def test_safety_fails_when_victim_tail_regresses(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant("default_taskset", total_sec=10.0, victim_p95=2.0, victim_p99=4.0, victim_max=8.0),
                    variant(
                        "flowcausal_width",
                        total_sec=8.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        frontier_wait_ns=1_000_000,
                        victim_p95=20.0,
                        victim_p99=40.0,
                        victim_max=80.0,
                    ),
                ]
            }
        )

        self.assertEqual(report["gates"][4]["status"], audit_claim_gate.STATUS_FAIL)
        self.assertIn("victim", "; ".join(report["gates"][4]["blockers"]))

    def test_zero_filled_victim_summary_is_not_safety_evidence(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant("default_taskset", total_sec=10.0),
                    variant(
                        "flowcausal_width",
                        total_sec=9.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        frontier_wait_ns=1_000_000,
                    ),
                ]
            }
        )

        self.assertEqual(report["gates"][4]["status"], audit_claim_gate.STATUS_FAIL)
        self.assertIn("no default victim", "; ".join(report["gates"][4]["blockers"]))

    def test_l4_candidate_when_all_gates_pass(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant(
                        "default_taskset",
                        total_sec=10.0,
                        frontier_wait_ns=2_000_000,
                        victim_p95=5.0,
                        victim_p99=7.0,
                        victim_max=10.0,
                    ),
                    variant(
                        "flowcausal_oracle",
                        total_sec=8.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        frontier_wait_ns=1_000_000,
                        victim_p95=5.2,
                        victim_p99=7.5,
                        victim_max=10.5,
                    ),
                ]
            }
        )

        self.assertTrue(report["highest_supported_level"].startswith("L4"))
        self.assertTrue(all(gate["status"] == audit_claim_gate.STATUS_PASS for gate in report["gates"]))

    def test_schedstat_characterization_can_pass_direct_mechanism_gate(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant(
                        "default_taskset",
                        total_sec=10.0,
                        char_frontier_wait_ns=4_000_000,
                        victim_p95=5.0,
                        victim_p99=7.0,
                        victim_max=10.0,
                    ),
                    variant(
                        "flowcausal_width",
                        total_sec=8.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        char_frontier_wait_ns=2_000_000,
                        victim_p95=5.1,
                        victim_p99=7.2,
                        victim_max=10.2,
                    ),
                ]
            }
        )

        self.assertEqual(report["gates"][2]["status"], audit_claim_gate.STATUS_PASS)
        self.assertTrue(
            any(
                "schedstat_frontier_wait_per_frontier_sample_ns" in item
                for item in report["gates"][2]["evidence"]
            ),
            report["gates"][2]["evidence"],
        )

    def test_shared_only_control_does_not_support_mechanism_or_outcome_claims(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant(
                        "default_taskset",
                        total_sec=10.0,
                        char_frontier_wait_ns=4_000_000,
                        victim_p95=5.0,
                        victim_p99=7.0,
                        victim_max=10.0,
                    ),
                    variant(
                        "flowcausal_shared_only",
                        total_sec=8.0,
                        frontier_records=20,
                        char_frontier_wait_ns=1_000_000,
                        victim_p95=5.1,
                        victim_p99=7.1,
                        victim_max=10.1,
                        hint_dry_run=True,
                    ),
                    variant(
                        "flowcausal_width",
                        total_sec=11.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        char_frontier_wait_ns=5_000_000,
                        victim_p95=5.1,
                        victim_p99=7.2,
                        victim_max=10.2,
                    ),
                ]
            }
        )

        mechanism_text = " ".join(report["gates"][2]["evidence"])
        outcome_text = " ".join(report["gates"][3]["evidence"])
        self.assertNotIn("flowcausal_shared_only", mechanism_text)
        self.assertIn("flowcausal_width", outcome_text)
        self.assertNotIn("flowcausal_shared_only", outcome_text)
        self.assertEqual(report["gates"][3]["status"], audit_claim_gate.STATUS_FAIL)

    def test_critical_path_metric_takes_precedence_over_cleanup_total(self) -> None:
        report = audit_claim_gate.audit_matrix(
            {
                "variants": [
                    variant(
                        "default_taskset",
                        total_sec=12.0,
                        critical_sec=10.0,
                        char_frontier_wait_ns=4_000_000,
                        victim_p95=5.0,
                        victim_p99=7.0,
                        victim_max=10.0,
                    ),
                    variant(
                        "flowcausal_width",
                        total_sec=15.0,
                        critical_sec=8.0,
                        frontier_records=10,
                        frontier_enqueues=25,
                        char_frontier_wait_ns=2_000_000,
                        victim_p95=5.1,
                        victim_p99=7.2,
                        victim_max=10.2,
                    ),
                ]
            }
        )

        outcome_text = " ".join(report["gates"][3]["evidence"])
        self.assertIn("critical_mean_sec", outcome_text)
        self.assertIn("delta=-20.0%", outcome_text)
        self.assertEqual(report["gates"][3]["status"], audit_claim_gate.STATUS_PASS)


if __name__ == "__main__":
    unittest.main()

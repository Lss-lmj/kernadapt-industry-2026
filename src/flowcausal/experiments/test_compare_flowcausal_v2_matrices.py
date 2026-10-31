#!/usr/bin/env python3
"""核对最新版结果与历史同条件结果比较程序的关键约束。"""

from __future__ import annotations

import unittest

from compare_flowcausal_v2_matrices import (
    check_compatible,
    common_trial_facts,
    independent_reduction,
)


def trial(*, suite: str = "suite-a", taskset: str = "0-7") -> dict:
    return {
        "suite_id": suite,
        "taskset": taskset,
        "replay_mode": "tool-execution",
        "cases": [{"instance_id": "a"}, {"instance_id": "b"}],
    }


def payload(*, taskset: str = "0-7") -> dict:
    return {
        "experiment_control": {
            "cleanup_success_artifacts": True,
            "copy_keep_build_artifacts": True,
            "drop_caches_before_trial": True,
            "pace_scale": 0.0,
            "replay_mode": "tool-execution",
            "taskset": taskset,
        }
    }


class ComparisonTests(unittest.TestCase):
    def test_independent_reduction_and_interval(self) -> None:
        result = independent_reduction([10.0, 10.0, 10.0], [8.0, 8.0, 8.0])
        self.assertAlmostEqual(result["reduction_percent"], 20.0)
        self.assertAlmostEqual(result["welch_95_low"], 20.0)
        self.assertAlmostEqual(result["welch_95_high"], 20.0)

    def test_common_trial_facts_rejects_mixed_conditions(self) -> None:
        with self.assertRaises(SystemExit):
            common_trial_facts([trial(), trial(taskset="0-3")])

    def test_compatible_conditions_pass(self) -> None:
        facts = check_compatible(payload(), [trial()], payload(), [trial()])
        self.assertEqual(facts["instances"], ["a", "b"])

    def test_experiment_control_mismatch_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            check_compatible(
                payload(), [trial()], payload(taskset="0-3"), [trial()]
            )


if __name__ == "__main__":
    unittest.main()

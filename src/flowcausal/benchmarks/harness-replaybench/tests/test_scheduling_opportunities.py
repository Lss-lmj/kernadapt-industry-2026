#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "analyze_scheduling_opportunities.py"
SPEC = importlib.util.spec_from_file_location("analyze_scheduling_opportunities", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SchedulingOpportunityTests(unittest.TestCase):
    def test_requires_old_runtime_and_current_wait_in_same_synchronous_window(self) -> None:
        window = {
            "synchronous": True,
            "elapsed_sec": 0.2,
            "older_generation_live_tasks": 3,
            "older_generation_overlap_task_sec": 0.3,
            "older_generation_runtime_in_window_lower_bound_sec": 0.02,
            "owned_runnable_wait_in_window_lower_bound_sec": 0.01,
        }
        self.assertTrue(MODULE.confirmed_cross_tool_contention(window))
        self.assertTrue(MODULE.target_cross_tool_window(window))
        for field in (
            "older_generation_runtime_in_window_lower_bound_sec",
            "owned_runnable_wait_in_window_lower_bound_sec",
        ):
            changed = dict(window, **{field: 0.0})
            self.assertFalse(MODULE.confirmed_cross_tool_contention(changed))
        self.assertFalse(
            MODULE.confirmed_cross_tool_contention(dict(window, synchronous=False))
        )
        self.assertFalse(
            MODULE.target_cross_tool_window(
                dict(window, older_generation_runtime_in_window_lower_bound_sec=0.005)
            )
        )


if __name__ == "__main__":
    unittest.main()

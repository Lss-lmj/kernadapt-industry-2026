#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "analyze_behavior_profiles.py"
SPEC = importlib.util.spec_from_file_location("analyze_behavior_profiles", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class BehaviorProfileTests(unittest.TestCase):
    def test_activity_bounds_only_count_unavoidable_in_window_activity(self) -> None:
        bounds = MODULE.activity_bounds(
            [
                {
                    "birth_ns": 100_000_000,
                    "exit_ns": 300_000_000,
                    "runtime_ns": 120_000_000,
                    "runnable_wait_ns": 110_000_000,
                },
                {
                    "birth_ns": 150_000_000,
                    "exit_ns": 250_000_000,
                    "runtime_ns": 10_000_000,
                    "runnable_wait_ns": 5_000_000,
                },
            ],
            200_000_000,
            300_000_000,
        )
        self.assertEqual(bounds["overlap_task_ns"], 150_000_000)
        self.assertEqual(bounds["runtime_lower_ns"], 20_000_000)
        self.assertEqual(bounds["runtime_upper_ns"], 110_000_000)
        self.assertEqual(bounds["wait_lower_ns"], 10_000_000)
        self.assertEqual(bounds["wait_upper_ns"], 105_000_000)

    def test_detects_cross_generation_and_parallel_join_windows(self) -> None:
        trace = {
            "commands": [
                {"id": "old", "stage": "BUILD"},
                {"id": "next", "stage": "EDIT"},
                {"id": "join", "stage": "TEST", "critical": True},
            ]
        }
        tool_ids = {name: MODULE.stable_id(name) for name in ("old", "next", "join")}
        rows: list[dict[str, object]] = []
        windows = {
            "old": (100_000_000, 200_000_000),
            "next": (220_000_000, 400_000_000),
            "join": (500_000_000, 800_000_000),
        }
        for name, (begin, end) in windows.items():
            rows.extend(
                [
                    {"record": "mark", "kind": "tool_begin", "tool_id": tool_ids[name], "time_ns": begin, "stage": trace["commands"][["old", "next", "join"].index(name)]["stage"]},
                    {"record": "mark", "kind": "tool_end", "tool_id": tool_ids[name], "time_ns": end, "stage": trace["commands"][["old", "next", "join"].index(name)]["stage"]},
                ]
            )
        rows.append({"record": "task_summary", "pid": 1, "parent_pid": 0, "comm": "old-child", "tool_id": tool_ids["old"], "birth_ns": 150_000_000, "exit_ns": 350_000_000, "runtime_ns": 10_000_000, "runnable_wait_ns": 2_000_000, "max_runnable_wait_ns": 1_000_000})
        rows.append({"record": "task_summary", "pid": 2, "parent_pid": 0, "comm": "next", "tool_id": tool_ids["next"], "birth_ns": 230_000_000, "exit_ns": 390_000_000, "runtime_ns": 20_000_000, "runnable_wait_ns": 3_000_000, "max_runnable_wait_ns": 2_000_000})
        for pid in range(10, 14):
            rows.append({"record": "task_summary", "pid": pid, "parent_pid": 10 if pid > 10 else 0, "comm": "join", "tool_id": tool_ids["join"], "birth_ns": 510_000_000, "exit_ns": 790_000_000, "runtime_ns": 40_000_000, "runnable_wait_ns": 5_000_000, "max_runnable_wait_ns": 2_000_000})
        rows.append({"record": "capture_end", "time_ns": 900_000_000})
        with tempfile.TemporaryDirectory() as temp:
            path = pathlib.Path(temp) / "capture.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
            result = MODULE.raw_capture_metrics(path, trace, {"critical_commands": ["join"]})
        self.assertEqual(result["cross_generation_window_count"], 1)
        self.assertEqual(result["cross_generation_windows"][0]["command_id"], "next")
        self.assertEqual(result["cross_generation_windows"][0]["older_generation_source_commands"], ["old"])
        self.assertEqual(result["join_window_count"], 1)
        self.assertEqual(result["join_windows"][0]["command_id"], "join")
        self.assertEqual(result["critical_tool_peak_live_tasks"], 4)
        self.assertEqual(result["join_windows"][0]["owned_generation_depth"], 2)
        self.assertAlmostEqual(result["join_windows"][0]["owned_runnable_wait_sec"], 0.02)
        self.assertEqual(result["join_windows"][0]["return_cohort_task_count"], 4)


if __name__ == "__main__":
    unittest.main()

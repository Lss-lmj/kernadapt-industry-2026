#!/usr/bin/env python3
"""关键基线运行程序的实验条件一致性测试。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "run_reference_baselines.py"
SPEC = importlib.util.spec_from_file_location("run_reference_baselines", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ManagedCpuValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.method = MODULE.Method("flowcausal_v2_latest", "/tmp/scx_flowcausal_v2")

    def validate(self, cpu_set: str, arguments: list[str]) -> None:
        MODULE.validate_managed_cpus(
            {"cpu_set": cpu_set},
            [self.method],
            {self.method.name: arguments},
        )

    def test_default_range_matches_default_scheduler_configuration(self) -> None:
        self.validate("0-7", [])

    def test_explicit_range_matches_scaled_suite(self) -> None:
        self.validate("0-3", ["--managed-cpus", "0-3"])

    def test_missing_scaled_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "任务使用 0-3"):
            self.validate("0-3", [])

    def test_mismatched_explicit_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "管理的处理器范围为 0-7"):
            self.validate("0-15", ["--managed-cpus", "0-7"])

    def test_unrelated_scheduler_is_not_constrained(self) -> None:
        method = MODULE.Method("scx_simple", "/tmp/scx_simple")
        MODULE.validate_managed_cpus(
            {"cpu_set": "0-3"},
            [method],
            {method.name: []},
        )


if __name__ == "__main__":
    unittest.main()

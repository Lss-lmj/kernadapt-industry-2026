#!/usr/bin/env python3
"""Regression tests for generic repo/CI matrix workload parsing."""

from __future__ import annotations

import importlib.util
import pathlib
import shlex
import sys
import tempfile
import unittest
from types import SimpleNamespace
from typing import Any


MODULE_PATH = pathlib.Path(__file__).with_name("run_repo_ci_matrix.py")


def load_matrix_module() -> Any:
    spec = importlib.util.spec_from_file_location("run_repo_ci_matrix", MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix_mod = load_matrix_module()


def make_args(**overrides: Any) -> SimpleNamespace:
    defaults = {
        "include_static_baselines": False,
        "include_oracle_baseline": False,
        "include_shared_only_variant": False,
        "skip_no_width_variant": False,
        "frontier_width": 1,
        "max_frontier_width": 1,
        "taskset": "0-3",
        "static_affinity_taskset": None,
        "nice_value": -5,
        "static_cgroup_cpu_weight": 10000,
        "static_cgroup_cpu_max": "",
        "static_cgroup_wrapper": str(MODULE_PATH.with_name("run_static_cgroup.py")),
        "static_cgroup_sudo": False,
        "command": "make -j2 test",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class RepoCiMatrixParsingTests(unittest.TestCase):
    def test_parse_agent_stage_markers_and_critical_metric(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "workload.log"
            path.write_text(
                "\n".join(
                    [
                        "ordinary compiler output",
                        '{"type":"workload_begin","workload":"agent_trace_workflow","trace_file":"/tmp/trace.json","instance_id":"django__issue","command_count":3,"stages":["INSPECT","BUILD","TEST"],"time_ns":1000000000}',
                        '{"type":"agent_stage_begin","stage":"TEST","turn":0,"pid":101}',
                        '{"type":"agent_stage_end","stage":"TEST","turn":0,"pid":101,"returncode":0,"elapsed_sec":1.5}',
                        '{"type":"agent_stage_end","stage":"TEST","turn":1,"pid":102,"returncode":0,"elapsed_sec":2.5}',
                        '{"type":"agent_stage_end","stage":"BUILD","turn":1,"pid":103,"returncode":0,"elapsed_sec":3.0}',
                        '{"type":"flowcausal_metric","name":"critical_sec","value":8.0}',
                        '{"type":"workload_end","returncode":0,"time_ns":3500000000}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_workload_log(path)

        self.assertEqual(parsed["metrics"]["critical_sec"], 8.0)
        self.assertEqual(parsed["stage_seconds"]["TEST"], [1.5, 2.5])
        self.assertEqual(parsed["stage_summary"]["TEST"]["total_sec"], 4.0)
        self.assertEqual(parsed["stage_summary"]["TEST"]["mean_sec"], 2.0)
        self.assertEqual(parsed["stage_records"][0]["pid"], 101)
        self.assertEqual(parsed["workload_begin"]["trace_file"], "/tmp/trace.json")
        self.assertEqual(parsed["workload_begin"]["instance_id"], "django__issue")
        self.assertEqual(parsed["workload_begin"]["command_count"], 3)
        self.assertEqual(parsed["workload_elapsed_sec"], 2.5)

    def test_aggregate_variant_summarizes_stage_totals_across_trials(self) -> None:
        aggregate = matrix_mod.aggregate_variant(
            {
                "trials": [
                    {
                        "returncode": 0,
                        "workload": {
                            "returncode": 0,
                            "total_sec": 10.0,
                            "workload_elapsed_sec": 8.0,
                            "critical_sec": 8.0,
                            "stage_seconds": {"TEST": [1.0, 3.0], "BUILD": [2.0]},
                            "frontier_label_count": 2,
                        },
                        "controller": {},
                        "flowmon": {},
                        "victim": {},
                        "internal_noise": {},
                        "characterization": {},
                    },
                    {
                        "returncode": 0,
                        "workload": {
                            "returncode": 0,
                            "total_sec": 12.0,
                            "workload_elapsed_sec": 9.0,
                            "critical_sec": 9.0,
                            "stage_seconds": {"TEST": [2.0, 4.0], "BUILD": [3.0]},
                            "frontier_label_count": 4,
                        },
                        "controller": {},
                        "flowmon": {},
                        "victim": {},
                        "internal_noise": {},
                        "characterization": {},
                    },
                ]
            }
        )

        self.assertEqual(aggregate["critical_mean_sec"], 8.5)
        self.assertEqual(aggregate["workload_elapsed_mean_sec"], 8.5)
        self.assertEqual(aggregate["workload_frontier_label_count_mean"], 3.0)
        self.assertEqual(aggregate["workload_frontier_label_count_min"], 2.0)
        self.assertEqual(aggregate["stage_summary"]["TEST"]["total_mean_sec"], 5.0)
        self.assertEqual(aggregate["stage_summary"]["TEST"]["occurrence_mean_sec"], 2.5)
        self.assertEqual(aggregate["stage_summary"]["BUILD"]["total_mean_sec"], 2.5)

    def test_aggregate_variant_summarizes_characterization_by_inferred_stage(self) -> None:
        aggregate = matrix_mod.aggregate_variant(
            {
                "trials": [
                    {
                        "returncode": 0,
                        "workload": {"returncode": 0, "total_sec": 1.0, "stage_seconds": {}},
                        "controller": {},
                        "flowmon": {},
                        "victim": {},
                        "internal_noise": {},
                        "characterization": {
                            "stage_delta_summary": {
                                "TEST": {
                                    "sample_count": 6.0,
                                    "samples_with_frontier": 3.0,
                                    "samples_with_schedstat": 4.0,
                                    "frontier_sched_wait_delta_ns": 6_000_000.0,
                                    "sched_wait_delta_ns": 9_000_000.0,
                                    "frontier_wait_per_frontier_sample_ns": 2_000_000.0,
                                    "frontier_wait_share": 2.0 / 3.0,
                                }
                            }
                        },
                    },
                    {
                        "returncode": 0,
                        "workload": {"returncode": 0, "total_sec": 1.0, "stage_seconds": {}},
                        "controller": {},
                        "flowmon": {},
                        "victim": {},
                        "internal_noise": {},
                        "characterization": {
                            "stage_delta_summary": {
                                "TEST": {
                                    "sample_count": 8.0,
                                    "samples_with_frontier": 4.0,
                                    "samples_with_schedstat": 5.0,
                                    "frontier_sched_wait_delta_ns": 4_000_000.0,
                                    "sched_wait_delta_ns": 10_000_000.0,
                                    "frontier_wait_per_frontier_sample_ns": 1_000_000.0,
                                    "frontier_wait_share": 0.4,
                                }
                            }
                        },
                    },
                ]
            }
        )

        test_stage = aggregate["characterization_stage_summary"]["TEST"]
        self.assertEqual(test_stage["trials"], 2.0)
        self.assertEqual(test_stage["sample_count_mean"], 7.0)
        self.assertEqual(test_stage["samples_with_frontier_mean"], 3.5)
        self.assertEqual(test_stage["frontier_wait_per_frontier_sample_ns_mean"], 1_500_000.0)

    def test_static_baseline_variants_are_explicit_rows(self) -> None:
        variants = matrix_mod.build_variants(make_args(include_static_baselines=True))

        names = [variant["name"] for variant in variants]
        self.assertEqual(
            names,
            [
                "default_taskset",
                "nice_priority",
                "static_affinity",
                "static_cgroup",
                "flowcausal_no_width",
                "flowcausal_width",
            ],
        )
        static_affinity = next(variant for variant in variants if variant["name"] == "static_affinity")
        self.assertEqual(static_affinity["taskset"], "0")

    def test_oracle_baseline_variant_is_explicit_row(self) -> None:
        variants = matrix_mod.build_variants(make_args(include_oracle_baseline=True))

        names = [variant["name"] for variant in variants]
        self.assertEqual(
            names,
            [
                "default_taskset",
                "oracle_frontier",
                "flowcausal_no_width",
                "flowcausal_width",
            ],
        )
        oracle = next(variant for variant in variants if variant["name"] == "oracle_frontier")
        self.assertEqual(oracle["kind"], "baseline")
        self.assertEqual(oracle["baseline_type"], "oracle_frontier")
        self.assertTrue(oracle["width_control"])

    def test_static_baselines_require_real_affinity_configuration(self) -> None:
        with self.assertRaises(SystemExit):
            matrix_mod.build_variants(
                make_args(
                    include_static_baselines=True,
                    taskset=None,
                    static_affinity_taskset=None,
                )
            )

    def test_baseline_command_wrappers_preserve_original_workload(self) -> None:
        args = make_args(command="python3 -m pytest tests/test_x.py", static_cgroup_cpu_max="50000 100000")
        nice_cmd = matrix_mod.baseline_command(args.command, {"baseline_type": "nice_priority"}, args)
        self.assertIn("nice -n -5", nice_cmd)
        self.assertIn("python3 -m pytest tests/test_x.py", nice_cmd)

        cgroup_cmd = matrix_mod.baseline_command(
            args.command,
            {"name": "static_cgroup", "baseline_type": "static_cgroup"},
            args,
        )
        tokens = shlex.split(cgroup_cmd)
        self.assertIn(str(MODULE_PATH.with_name("run_static_cgroup.py")), tokens)
        self.assertIn("--cpu-weight", tokens)
        self.assertIn("10000", tokens)
        self.assertIn("--cpu-max", tokens)
        self.assertIn("50000 100000", tokens)
        self.assertEqual(tokens[-3:], ["bash", "-lc", "python3 -m pytest tests/test_x.py"])

        sudo_args = make_args(command="python3 -m pytest tests/test_x.py", static_cgroup_sudo=True)
        sudo_cgroup_cmd = matrix_mod.baseline_command(
            sudo_args.command,
            {"name": "static_cgroup", "baseline_type": "static_cgroup"},
            sudo_args,
        )
        sudo_tokens = shlex.split(sudo_cgroup_cmd)
        self.assertEqual(sudo_tokens[:2], ["sudo", "-n"])
        self.assertIn("--preserve-env=FLOWCAUSAL_VARIANT,FLOWCAUSAL_TRIAL,FLOWCAUSAL_TRIAL_DIR,FLOWCAUSAL_BUILD_OUT", sudo_tokens)
        self.assertIn(sys.executable, sudo_tokens)

    def test_parse_controller_summarizes_adaptive_propagation_depth(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "controller.ndjson"
            path.write_text(
                "\n".join(
                    [
                        '{"type":"flowctl_watch","tick":0,"stage":"SHELL","frontiers":[],"propagation_anchors":[{"pid":10}],"adaptive_propagation":{"enabled":true,"requested_depth":2,"propagation_depth":2,"frontier_count":0,"anchor_count":1,"reason":"spawn-anchor-use-requested-depth"}}',
                        '{"type":"flowctl_watch","tick":1,"stage":"TEST","stage_source":"state-file","frontiers":[{"pid":12,"comm":"test_runner","score":270,"reason":"test worker,parent-wait"}],"propagation_anchors":[{"pid":11}],"adaptive_propagation":{"enabled":true,"requested_depth":2,"propagation_depth":1,"frontier_count":1,"anchor_count":1,"reason":"concrete-frontier-depth1"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_controller(path)
            aggregate = matrix_mod.aggregate_controller([parsed])

        self.assertEqual(parsed["adaptive_propagation_records"], 2)
        self.assertEqual(parsed["stage_source_counts"], {"state-file": 1, "unknown": 1})
        self.assertEqual(parsed["adaptive_propagation_enabled_records"], 2)
        self.assertEqual(parsed["adaptive_propagation_depth_counts"], {"1": 1, "2": 1})
        self.assertEqual(
            parsed["adaptive_propagation_reason_counts"],
            {"concrete-frontier-depth1": 1, "spawn-anchor-use-requested-depth": 1},
        )
        self.assertEqual(aggregate["adaptive_propagation_records"], 2)
        self.assertEqual(aggregate["stage_source_counts"], {"state-file": 1, "unknown": 1})
        self.assertEqual(aggregate["adaptive_propagation_depth_counts"], {"1": 1, "2": 1})

    def test_parse_controller_summarizes_stage_aware_propagation_policy(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            path = pathlib.Path(raw_tmp) / "controller.ndjson"
            path.write_text(
                "\n".join(
                    [
                        '{"type":"flowctl_watch","tick":0,"stage":"BUILD","frontiers":[{"pid":22,"comm":"cc1","score":290,"reason":"compiler worker,parent-wait:make,barrier-tail:2"}],"propagation_anchors":[{"pid":21}],"adaptive_propagation":{"enabled":true,"policy":"stage-aware","stage":"BUILD","requested_depth":2,"propagation_depth":0,"frontier_count":1,"anchor_count":1,"eligible_candidate_count":2,"selected_parent_wait_count":1,"selected_max_barrier_tail":2,"reason":"stage-aware-build-wide-or-weak-tail-depth0"}}',
                        '{"type":"flowctl_watch","tick":1,"stage":"TEST","frontiers":[{"pid":32,"comm":"test_runner","score":270,"reason":"test worker,parent-wait:python3,barrier-tail:1"}],"propagation_anchors":[{"pid":31}],"adaptive_propagation":{"enabled":true,"policy":"stage-aware","stage":"TEST","requested_depth":2,"propagation_depth":1,"frontier_count":1,"anchor_count":1,"eligible_candidate_count":1,"selected_parent_wait_count":1,"selected_max_barrier_tail":1,"reason":"stage-aware-test-barrier-depth1"}}',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            parsed = matrix_mod.parse_controller(path)
            aggregate = matrix_mod.aggregate_controller([parsed])

        self.assertEqual(parsed["adaptive_propagation_policy_counts"], {"stage-aware": 2})
        self.assertEqual(parsed["adaptive_propagation_stage_counts"], {"BUILD": 1, "TEST": 1})
        self.assertEqual(parsed["adaptive_propagation_depth_counts"], {"0": 1, "1": 1})
        self.assertEqual(
            aggregate["adaptive_propagation_policy_counts"],
            {"stage-aware": 2},
        )
        self.assertEqual(
            aggregate["adaptive_propagation_stage_counts"],
            {"BUILD": 1, "TEST": 1},
        )

    def test_aggregate_controller_preserves_build_admission_counts(self) -> None:
        rows = [
            {
                "frontier_action_records": 2,
                "frontier_action_observe_records": 0,
                "frontier_action_policy_fallback_records": 0,
                "build_admission_triggered_records": 1,
                "frontier_action_counts": {"advance": 2},
                "build_admission_mode_counts": {"tail-only": 2},
                "build_admission_reason_counts": {
                    "build-tail-only-observe:max_tail=1,link_tail=0": 1,
                },
                "frontier_action_examples": [
                    {
                        "tick": 0,
                        "stage": "BUILD",
                        "action": "observe",
                        "build_admission_triggered": True,
                    }
                ],
            },
            {
                "frontier_action_records": 1,
                "frontier_action_observe_records": 0,
                "frontier_action_policy_fallback_records": 0,
                "build_admission_triggered_records": 0,
                "frontier_action_counts": {"advance": 1},
                "build_admission_mode_counts": {"tail-only": 1},
                "build_admission_reason_counts": {},
            },
        ]

        aggregate = matrix_mod.aggregate_controller(rows)

        self.assertEqual(aggregate["frontier_action_records"], 3)
        self.assertEqual(aggregate["frontier_action_observe_records"], 0)
        self.assertEqual(aggregate["build_admission_triggered_records"], 1)
        self.assertEqual(aggregate["frontier_action_counts"], {"advance": 3})
        self.assertEqual(aggregate["build_admission_mode_counts"], {"tail-only": 3})
        self.assertEqual(
            aggregate["build_admission_reason_counts"],
            {"build-tail-only-observe:max_tail=1,link_tail=0": 1},
        )
        self.assertEqual(aggregate["frontier_action_examples"][0]["stage"], "BUILD")


if __name__ == "__main__":
    unittest.main()

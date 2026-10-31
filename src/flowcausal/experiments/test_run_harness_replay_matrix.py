#!/usr/bin/env python3
"""Tests for the HarnessReplayBench matrix runner."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace


HERE = pathlib.Path(__file__).resolve().parent
MATRIX = HERE / "run_harness_replay_matrix.py"


def load_matrix_module():
    spec = importlib.util.spec_from_file_location("run_harness_replay_matrix", MATRIX)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MATRIX}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix_mod = load_matrix_module()


class HarnessReplayMatrixTests(unittest.TestCase):
    def test_parse_variant_with_taskset(self) -> None:
        variant = matrix_mod.parse_variant("cpuset_0_1:taskset=0-1")
        self.assertEqual(variant.name, "cpuset_0_1")
        self.assertEqual(variant.kind, "baseline")
        self.assertEqual(variant.taskset, "0-1")

    def test_parse_variant_with_nice(self) -> None:
        variant = matrix_mod.parse_variant("nice5:nice=5")
        self.assertEqual(variant.name, "nice5")
        self.assertEqual(variant.kind, "baseline")
        self.assertEqual(variant.nice, 5)

    def test_parse_variant_with_static_cgroup(self) -> None:
        variant = matrix_mod.parse_variant("cg_weight100:cpu_weight=100,cpu_max=50000/100000")
        self.assertEqual(variant.name, "cg_weight100")
        self.assertEqual(variant.kind, "baseline")
        self.assertEqual(variant.cgroup_cpu_weight, 100)
        self.assertEqual(variant.cgroup_cpu_max, "50000 100000")

    def test_parse_flowcausal_variant(self) -> None:
        variant = matrix_mod.parse_variant(
            "flowcausal_width:kind=flowcausal,width_control=true,"
            "frontier_propagation=on,stage_aware_frontier_propagation_depth=yes,"
            "background_dsq=true,background_dispatch_quota=12,taskset=0-3,"
            "frontier_width=2,max_frontier_width=2,frontier_action=auto,"
            "build_frontier_admission=tail-only"
        )
        self.assertEqual(variant.name, "flowcausal_width")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertEqual(variant.taskset, "0-3")
        self.assertIsNone(variant.nice)
        self.assertTrue(variant.width_control)
        self.assertTrue(variant.frontier_propagation)
        self.assertTrue(variant.stage_aware_frontier_propagation_depth)
        self.assertTrue(variant.background_dsq)
        self.assertEqual(variant.background_dispatch_quota, 12)
        self.assertEqual(variant.frontier_width, 2)
        self.assertEqual(variant.max_frontier_width, 2)
        self.assertEqual(variant.frontier_action, "auto")
        self.assertEqual(variant.build_frontier_admission, "tail-only")

    def test_parse_named_flowcausal_alias(self) -> None:
        variant = matrix_mod.parse_variant("flowcausal_width")

        self.assertEqual(variant.name, "flowcausal_width")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertTrue(variant.width_control)
        self.assertTrue(variant.frontier_propagation)
        self.assertTrue(variant.stage_aware_frontier_propagation_depth)

    def test_parse_named_flowd_affinity_alias(self) -> None:
        variant = matrix_mod.parse_variant("flowcausal_flowd_frontier_depth1_affinity_rr")

        self.assertEqual(variant.name, "flowcausal_flowd_frontier_depth1_affinity_rr")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertTrue(variant.per_case_flowd)
        self.assertEqual(variant.frontier_propagation_depth, 1)
        self.assertEqual(variant.flowd_anchor_policy, "round-robin")

        frontier_only = matrix_mod.parse_variant("flowcausal_flowd_frontier_depth1_affinity_frontieronly_rr")
        self.assertTrue(frontier_only.locality_frontier_only)
        self.assertEqual(frontier_only.flowd_anchor_policy, "round-robin")

        remembered = matrix_mod.parse_variant(
            "flowcausal_flowd_frontier_depth1_affinity_frontieronly_remembered_rr"
        )
        self.assertTrue(remembered.locality_frontier_only)
        self.assertTrue(remembered.locality_remembered_first)

        pressure_frontier_only = matrix_mod.parse_variant(
            "flowcausal_flowd_frontier_depth1_pressure_wait5_affinity_frontieronly_rr"
        )
        self.assertTrue(pressure_frontier_only.locality_frontier_only)
        self.assertEqual(pressure_frontier_only.shared_wait_pressure_us, 5000)

    def test_parse_named_flowcausal_stage_aware_alias(self) -> None:
        variant = matrix_mod.parse_variant("flowcausal_case_stage_aware")

        self.assertEqual(variant.name, "flowcausal_case_stage_aware")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertTrue(variant.per_case_flowctl)
        self.assertEqual(variant.frontier_action, "stage-aware")
        self.assertTrue(variant.frontier_propagation)
        self.assertTrue(variant.stage_aware_frontier_propagation_depth)
        self.assertTrue(variant.frontier_fallback_local_dsq)
        self.assertFalse(variant.background_dsq)
        self.assertEqual(variant.background_dispatch_quota, 0)

    def test_parse_named_flowcausal_stage_aware_background_alias(self) -> None:
        variant = matrix_mod.parse_variant("flowcausal_case_stage_aware_bg")

        self.assertEqual(variant.name, "flowcausal_case_stage_aware_bg")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertTrue(variant.per_case_flowctl)
        self.assertEqual(variant.frontier_action, "stage-aware")
        self.assertTrue(variant.frontier_fallback_local_dsq)
        self.assertTrue(variant.background_dsq)
        self.assertEqual(variant.background_dispatch_quota, 12)

    def test_parse_named_flowcausal_attached_alias(self) -> None:
        variant = matrix_mod.parse_variant("flowcausal_attached")

        self.assertEqual(variant.name, "flowcausal_attached")
        self.assertEqual(variant.kind, "flowcausal")
        self.assertTrue(variant.no_controller)

    def test_parse_no_python_control_plane_and_bpf_aliases(self) -> None:
        cplane = matrix_mod.parse_variant("flowcausal_cplane")
        cplane2 = matrix_mod.parse_variant("flowcausal2_cplane")
        bpf = matrix_mod.parse_variant("flowcausal_bpf")
        bpf2 = matrix_mod.parse_variant("flowcausal2_bpf")
        bpf3 = matrix_mod.parse_variant("flowcausal3_bpf")
        converge = matrix_mod.parse_variant("flowcausal_converge")
        bpf_cassist = matrix_mod.parse_variant("flowcausal_bpf_cassist")
        bpf2_cassist = matrix_mod.parse_variant("flowcausal2_bpf_cassist")
        bpf3_cassist = matrix_mod.parse_variant("flowcausal3_bpf_cassist")
        converge_cassist = matrix_mod.parse_variant("flowcausal_converge_cassist")

        self.assertTrue(cplane.flowd_control_plane)
        self.assertEqual(cplane.flowcausal_scheduler, "/tmp/scx_flowcausal")
        self.assertTrue(cplane2.flowd_control_plane)
        self.assertEqual(cplane2.flowcausal_scheduler, "/tmp/scx_flowcausal2")
        self.assertFalse(bpf.flowd_control_plane)
        self.assertEqual(bpf.flowcausal_scheduler, "/tmp/scx_flowcausal_bpf")
        self.assertFalse(bpf2.flowd_control_plane)
        self.assertEqual(bpf2.flowcausal_scheduler, "/tmp/scx_flowcausal2_bpf")
        self.assertFalse(bpf3.flowd_control_plane)
        self.assertEqual(bpf3.flowcausal_scheduler, "/tmp/scx_flowcausal3_bpf")
        self.assertFalse(converge.flowd_control_plane)
        self.assertEqual(converge.flowcausal_scheduler, "/tmp/scx_flowcausal_converge")
        self.assertTrue(bpf_cassist.flowd_control_plane)
        self.assertTrue(bpf_cassist.flowd_assist_bpf_auto)
        self.assertEqual(bpf_cassist.flowcausal_scheduler, "/tmp/scx_flowcausal_bpf")
        self.assertTrue(bpf2_cassist.flowd_control_plane)
        self.assertTrue(bpf2_cassist.flowd_assist_bpf_auto)
        self.assertEqual(bpf2_cassist.flowcausal_scheduler, "/tmp/scx_flowcausal2_bpf")
        self.assertTrue(bpf3_cassist.flowd_control_plane)
        self.assertTrue(bpf3_cassist.flowd_assist_bpf_auto)
        self.assertEqual(bpf3_cassist.flowcausal_scheduler, "/tmp/scx_flowcausal3_bpf")
        self.assertTrue(converge_cassist.flowd_control_plane)
        self.assertTrue(converge_cassist.flowd_assist_bpf_auto)
        self.assertEqual(
            converge_cassist.flowcausal_scheduler,
            "/tmp/scx_flowcausal_converge",
        )
        self.assertTrue(
            all(v.per_case_flowd for v in (cplane, cplane2, bpf, bpf2, bpf3, converge))
        )
        self.assertTrue(
            all(v.no_controller for v in (cplane, cplane2, bpf, bpf2, bpf3, converge))
        )

    def test_parse_named_flowcausal_attached_placement_aliases(self) -> None:
        shared_local = matrix_mod.parse_variant("flowcausal_attached_shared_local")
        shared_percpu = matrix_mod.parse_variant("flowcausal_attached_shared_percpu")

        self.assertTrue(shared_local.no_controller)
        self.assertTrue(shared_local.shared_local_dsq)
        self.assertFalse(shared_local.shared_percpu_dsq)
        self.assertTrue(shared_percpu.no_controller)
        self.assertTrue(shared_percpu.shared_percpu_dsq)
        self.assertFalse(shared_percpu.shared_local_dsq)

    def test_parse_named_scx_alias(self) -> None:
        variant = matrix_mod.parse_variant("scx_simple")

        self.assertEqual(variant.name, "scx_simple")
        self.assertEqual(variant.kind, "scx")
        self.assertTrue(variant.scx_scheduler.endswith("/scx_simple"))
        self.assertEqual(variant.scx_scheduler_args, ())

    def test_parse_custom_scx_variant_with_args(self) -> None:
        variant = matrix_mod.parse_variant(
            "batch30:kind=scx,scx_scheduler=/tmp/scx_batch,scx_args=-s 30 -m 0-3"
        )

        self.assertEqual(variant.name, "batch30")
        self.assertEqual(variant.kind, "scx")
        self.assertEqual(variant.scx_scheduler, "/tmp/scx_batch")
        self.assertEqual(variant.scx_scheduler_args, ("-s", "30", "-m", "0-3"))

    def test_expand_variant_specs_splits_bare_comma_list_only(self) -> None:
        expanded = matrix_mod.expand_variant_specs(
            [
                "default,flowcausal_no_width,flowcausal_width",
                "custom:kind=flowcausal,width_control=true,frontier_width=2",
            ]
        )

        self.assertEqual(
            expanded,
            [
                "default",
                "flowcausal_no_width",
                "flowcausal_width",
                "custom:kind=flowcausal,width_control=true,frontier_width=2",
            ],
        )

    def test_build_mix_cmd_for_nice_baseline(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_exclude=["experiments/results", "paper/*.pdf"],
            copy_keep_git=True,
            docker_user="current",
            env=["FOO=bar"],
            manifest=None,
            stagger_ms=25,
            taskset="0-1",
        )
        variant = matrix_mod.parse_variant("nice5:nice=5")
        cmd = matrix_mod.build_mix_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], str(matrix_mod.MIX_RUNNER))
        self.assertIn("--nice", cmd)
        self.assertEqual(cmd[cmd.index("--nice") + 1], "5")
        self.assertIn("--taskset", cmd)
        self.assertEqual(cmd[cmd.index("--taskset") + 1], "0-1")
        self.assertIn("--copy-keep-git", cmd)
        self.assertIn("--cleanup-success-artifacts", cmd)
        self.assertIn("--cleanup-use-sudo", cmd)
        self.assertEqual(cmd.count("--copy-exclude"), 2)
        first_exclude = cmd.index("--copy-exclude")
        self.assertEqual(cmd[first_exclude + 1], "experiments/results")

    def test_build_mix_cmd_forwards_system_profile(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_exclude=[],
            copy_keep_git=False,
            docker_user="current",
            env=[],
            manifest=None,
            stagger_ms=0,
            system_profile=True,
            system_profile_interval_ms=10,
            taskset="",
        )
        variant = matrix_mod.parse_variant("default")
        cmd = matrix_mod.build_mix_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertIn("--system-profile", cmd)
        self.assertIn("--system-profile-interval-ms", cmd)
        self.assertEqual(cmd[cmd.index("--system-profile-interval-ms") + 1], "10")

    def test_build_mix_cmd_forwards_host_and_container_env_separately(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_env=["FLOWCAUSAL_REPLAY_PYTHON=/opt/py/bin/python"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_exclude=[],
            copy_keep_git=False,
            docker_user="current",
            env=["COMMON_FLAG=1"],
            host_env=["PATH=/host/tool_shims:/usr/bin", "FLOWCAUSAL_REPLAY_PYTHON=python"],
            manifest=None,
            stagger_ms=0,
            system_profile=False,
            taskset="",
        )
        variant = matrix_mod.parse_variant("default")
        cmd = matrix_mod.build_mix_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertIn("--env", cmd)
        self.assertIn("COMMON_FLAG=1", cmd)
        self.assertIn("--host-env", cmd)
        self.assertIn("PATH=/host/tool_shims:/usr/bin", cmd)
        self.assertIn("FLOWCAUSAL_REPLAY_PYTHON=python", cmd)
        self.assertIn("--container-env", cmd)
        self.assertIn("FLOWCAUSAL_REPLAY_PYTHON=/opt/py/bin/python", cmd)

    def test_build_mix_cmd_for_static_cgroup_baseline(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            cgroup_sudo="auto",
            cgroup_wrapper="/wrapper/run_static_cgroup.py",
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_keep_git=False,
            docker_user="current",
            env=[],
            manifest=None,
            stagger_ms=0,
            taskset="",
        )
        variant = matrix_mod.parse_variant("cg_weight100:cpu_weight=100,cpu_max=50000/100000")
        cmd = matrix_mod.build_mix_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertIn("--cgroup-cpu-weight", cmd)
        self.assertEqual(cmd[cmd.index("--cgroup-cpu-weight") + 1], "100")
        self.assertIn("--cgroup-cpu-max", cmd)
        self.assertEqual(cmd[cmd.index("--cgroup-cpu-max") + 1], "50000 100000")
        self.assertIn("--cgroup-wrapper", cmd)
        self.assertEqual(cmd[cmd.index("--cgroup-wrapper") + 1], "/wrapper/run_static_cgroup.py")
        self.assertIn("--cgroup-sudo", cmd)
        self.assertEqual(cmd[cmd.index("--cgroup-sudo") + 1], "auto")

    def test_build_external_scx_cmd_wraps_mix_runner_without_flowcausal_args(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_exclude=[],
            copy_keep_git=False,
            docker_user="current",
            env=[],
            manifest=None,
            scx_scheduler_ready_timeout_sec=0.0,
            scx_scheduler_startup_grace_sec=0.0,
            scx_scheduler_sudo="never",
            stagger_ms=0,
            system_profile=False,
            taskset="",
            victim_command="",
        )
        variant = matrix_mod.parse_variant(
            "simple:kind=scx,scx_scheduler=/tmp/scx_simple,scx_args=-f"
        )

        cmd = matrix_mod.build_external_scx_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertEqual(cmd[0], sys.executable)
        self.assertEqual(cmd[1], str(matrix_mod.EXTERNAL_SCX_SANDBOX))
        self.assertIn("--scheduler", cmd)
        self.assertEqual(cmd[cmd.index("--scheduler") + 1], "/tmp/scx_simple")
        self.assertIn("--scheduler-arg=-f", cmd)
        self.assertIn(str(matrix_mod.MIX_RUNNER), cmd)
        self.assertNotIn("--attach-scheduler", cmd)
        self.assertNotIn("--flowctl", cmd)

    def test_build_flowcausal_cmd_wraps_mix_runner(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_keep_git=True,
            docker_user="current",
            env=["FOO=bar"],
            flowcausal_background_dispatch_quota=0,
            flowcausal_boost_budget_ms=10,
            flowcausal_build_frontier_admission="tail-only",
            flowcausal_characterization_interval_ms=50,
            flowcausal_characterization_top_candidates=6,
            flowcausal_collect_characterization=False,
            flowcausal_confidence=75,
            flowcausal_flowctl="/flowctl.py",
            flowcausal_flowctl_sudo="never",
            flowcausal_flowmon="/tmp/flowmon",
            flowcausal_frontier_bypass_score=140,
            flowcausal_frontier_dispatch_quota=4,
            flowcausal_frontier_dispatch_window_budget=0,
            flowcausal_frontier_dispatch_window_ms=0,
            flowcausal_frontier_hold_ms=0,
            flowcausal_frontier_propagation_depth=2,
            flowcausal_frontier_selected_stability=False,
            flowcausal_frontier_stability_action="observe",
            flowcausal_frontier_stability_samples=1,
            flowcausal_frontier_wakeup_preempt=False,
            flowcausal_frontier_width=1,
            flowcausal_guard_cooldown_dispatches=0,
            flowcausal_interval_ms=100,
            flowcausal_max_frontier_width=1,
            flowcausal_max_vtime_boost_us=500,
            flowcausal_min_frontier_score=50,
            flowcausal_no_event_flowmon=True,
            flowcausal_no_final_snapshot=False,
            flowcausal_pin_dir="/sys/fs/bpf/flowcausal",
            flowcausal_policy_fallback=False,
            flowcausal_policy_window_ms=100,
            flowcausal_pressure_dispatch_ms=0,
            flowcausal_pressure_fallback_enqueues=0,
            flowcausal_pressure_fallback_ms=0,
            flowcausal_scheduler="/tmp/scx_flowcausal",
            flowcausal_shared_slice_us=0,
            flowcausal_shared_wait_pressure_us=0,
            flowcausal_shared_wakeup_min_block_us=0,
            flowcausal_shared_wakeup_preempt=False,
            flowcausal_shared_wakeup_slice_us=0,
            flowcausal_stage_kind="AUTO",
            flowcausal_ttl_ms=1000,
            manifest=None,
            stagger_ms=50,
            taskset="",
            victim_command="/tmp/cpu_victim --period-us 1000",
            victim_taskset="2",
            victim_warmup_sec=0.3,
        )
        variant = matrix_mod.parse_variant(
            "flowcausal_width:kind=flowcausal,width_control=true,"
            "frontier_propagation=true,background_dsq=true,background_dispatch_quota=12,"
            "frontier_width=2,max_frontier_width=2,no_controller=true,"
            "shared_local_dsq=true,tracked_local_dsq=true,shared_percpu_dsq=true"
        )
        trial_dir = pathlib.Path("/tmp/flowcausal-matrix-test")
        cmd = matrix_mod.build_flowcausal_cmd(args, variant, trial_dir)

        self.assertIn(str(matrix_mod.LIVE_SANDBOX), cmd)
        self.assertIn("--attach-scheduler", cmd)
        self.assertIn("--width-control", cmd)
        self.assertIn("--frontier-width", cmd)
        self.assertEqual(cmd[cmd.index("--frontier-width") + 1], "2")
        self.assertIn("--max-frontier-width", cmd)
        self.assertEqual(cmd[cmd.index("--max-frontier-width") + 1], "2")
        self.assertIn("--frontier-propagation", cmd)
        self.assertIn("--background-dsq", cmd)
        self.assertIn("--background-dispatch-quota", cmd)
        self.assertIn("--build-frontier-admission", cmd)
        self.assertEqual(cmd[cmd.index("--build-frontier-admission") + 1], "tail-only")
        self.assertIn("--no-event-flowmon", cmd)
        self.assertIn("--no-controller", cmd)
        self.assertIn("--shared-local-dsq", cmd)
        self.assertIn("--tracked-local-dsq", cmd)
        self.assertIn("--shared-percpu-dsq", cmd)
        self.assertIn("--victim-command", cmd)
        self.assertEqual(cmd[cmd.index("--victim-command") + 1], "/tmp/cpu_victim --period-us 1000")
        self.assertIn("--victim-taskset", cmd)
        self.assertEqual(cmd[cmd.index("--victim-taskset") + 1], "2")
        self.assertIn("--victim-warmup-sec", cmd)
        self.assertEqual(cmd[cmd.index("--victim-warmup-sec") + 1], "0.3")
        sep = cmd.index("--")
        inner = cmd[sep + 1 :]
        self.assertEqual(inner[0], sys.executable)
        self.assertEqual(inner[1], str(matrix_mod.MIX_RUNNER))
        self.assertIn("--out-dir", inner)
        self.assertIn(str(trial_dir), inner)
        self.assertNotIn("--victim-command", inner)

    def test_build_victim_baseline_cmd_wraps_without_scheduler(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_exclude=[],
            copy_keep_git=False,
            docker_user="current",
            env=[],
            manifest=None,
            stagger_ms=0,
            system_profile=False,
            system_profile_interval_ms=25,
            taskset="0-1",
            victim_command="/tmp/cpu_victim --duration-sec 5",
            victim_taskset="2",
            victim_warmup_sec=0.1,
        )
        variant = matrix_mod.parse_variant("default")
        trial_dir = pathlib.Path("/tmp/flowcausal-matrix-test")
        cmd = matrix_mod.build_victim_baseline_cmd(args, variant, trial_dir)

        self.assertIn(str(matrix_mod.LIVE_SANDBOX), cmd)
        self.assertIn("--hint-dry-run", cmd)
        self.assertIn("--no-controller", cmd)
        self.assertIn("--no-flowmon", cmd)
        self.assertNotIn("--attach-scheduler", cmd)
        self.assertIn("--victim-command", cmd)
        self.assertEqual(cmd[cmd.index("--victim-command") + 1], "/tmp/cpu_victim --duration-sec 5")
        self.assertIn("--victim-taskset", cmd)
        self.assertEqual(cmd[cmd.index("--victim-taskset") + 1], "2")
        sep = cmd.index("--")
        inner = cmd[sep + 1 :]
        self.assertEqual(inner[0], sys.executable)
        self.assertEqual(inner[1], str(matrix_mod.MIX_RUNNER))
        self.assertIn("--taskset", inner)
        self.assertEqual(inner[inner.index("--taskset") + 1], "0-1")
        self.assertNotIn("--victim-command", inner)

    def test_build_postprocess_specs_includes_profile_and_victim(self) -> None:
        args = Namespace(system_profile=True, victim_command="python3 victim.py")
        out_dir = pathlib.Path("/tmp/matrix-out")
        summary = out_dir / "harness-replay-matrix-summary.json"
        specs = matrix_mod.build_postprocess_specs(args, out_dir=out_dir, matrix_summary=summary)

        self.assertEqual([spec["name"] for spec in specs], ["system_profile_matrix_summary", "victim_latency_matrix_summary"])
        self.assertIn("system-profile-matrix-summary.json", specs[0]["output"])
        self.assertIn("victim-latency-matrix-summary.json", specs[1]["output"])
        self.assertIn(str(summary), specs[0]["cmd"])
        self.assertIn(str(summary), specs[1]["cmd"])

    def test_write_markdown_includes_postprocess_section(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "summary.md"
            payload = {
                "status": "pass",
                "trials_per_variant": 1,
                "variants": [
                    {
                        "name": "default",
                        "kind": "baseline",
                        "taskset": "",
                        "summary": {
                            "trials": 1,
                            "pass_trials": 1,
                            "makespan_mean_sec": 1.0,
                            "mean_critical_mean_sec": 1.0,
                            "p95_critical_mean_sec": 1.0,
                            "failed_stage_commands_mean": 0.0,
                        },
                        "comparison_to_baseline": {"baseline": "default"},
                    }
                ],
                "postprocess": {
                    "system_profile_matrix_summary": {
                        "status": "pass",
                        "output": "/tmp/system-profile-matrix-summary.json",
                    }
                },
            }
            matrix_mod.write_markdown(out, payload)

            text = out.read_text(encoding="utf-8")
            self.assertIn("## Postprocess artifacts", text)
            self.assertIn("system_profile_matrix_summary", text)

    def test_build_flowcausal_cmd_with_per_case_flowctl(self) -> None:
        args = Namespace(
            allow_failure_stage=["RETRY_PROBE"],
            case=["toy=/trace.json::/repo"],
            container_path_env="/env/bin:/usr/bin",
            container_python="/env/bin/python",
            copy_keep_git=False,
            docker_user="current",
            env=[],
            flowcausal_boost_budget_ms=10,
            flowcausal_build_frontier_admission="tail-only",
            flowcausal_characterization_interval_ms=50,
            flowcausal_characterization_top_candidates=6,
            flowcausal_collect_characterization=False,
            flowcausal_confidence=75,
            flowcausal_flowctl="/flowctl.py",
            flowcausal_flowctl_sudo="never",
            flowcausal_flowmon="/tmp/flowmon",
            flowcausal_frontier_bypass_score=140,
            flowcausal_frontier_dispatch_quota=4,
            flowcausal_frontier_dispatch_window_budget=0,
            flowcausal_frontier_dispatch_window_ms=0,
            flowcausal_frontier_hold_ms=0,
            flowcausal_frontier_propagation_depth=2,
            flowcausal_frontier_selected_stability=False,
            flowcausal_frontier_stability_action="observe",
            flowcausal_frontier_stability_samples=1,
            flowcausal_frontier_wakeup_preempt=False,
            flowcausal_frontier_width=1,
            flowcausal_guard_cooldown_dispatches=0,
            flowcausal_interval_ms=100,
            flowcausal_max_frontier_width=1,
            flowcausal_max_vtime_boost_us=500,
            flowcausal_min_frontier_score=50,
            flowcausal_no_event_flowmon=True,
            flowcausal_no_final_snapshot=False,
            flowcausal_pin_dir="/sys/fs/bpf/flowcausal",
            flowcausal_policy_fallback=False,
            flowcausal_policy_window_ms=100,
            flowcausal_pressure_dispatch_ms=0,
            flowcausal_pressure_fallback_enqueues=0,
            flowcausal_pressure_fallback_ms=0,
            flowcausal_scheduler="/tmp/scx_flowcausal",
            flowcausal_shared_slice_us=0,
            flowcausal_shared_wait_pressure_us=0,
            flowcausal_shared_wakeup_min_block_us=0,
            flowcausal_shared_wakeup_preempt=False,
            flowcausal_shared_wakeup_slice_us=0,
            flowcausal_stage_kind="AUTO",
            flowcausal_ttl_ms=1000,
            manifest=None,
            stagger_ms=0,
            taskset="",
        )
        variant = matrix_mod.parse_variant(
            "flowcausal_case:kind=flowcausal,per_case_flowctl=true,"
            "width_control=true,frontier_propagation=true,"
            "stage_aware_frontier_propagation_depth=true,"
            "frontier_width=2,max_frontier_width=2,frontier_action=auto,"
            "build_frontier_admission=observe"
        )
        cmd = matrix_mod.build_flowcausal_cmd(args, variant, pathlib.Path("/tmp/trial"))

        self.assertNotIn("--hint-dry-run", cmd)
        self.assertIn("--no-controller", cmd)
        inner = cmd[cmd.index("--") + 1 :]
        self.assertIn("--per-case-flowctl", inner)
        self.assertIn("--flowctl", inner)
        self.assertIn("/flowctl.py", inner)
        self.assertIn("--flowctl-width-control", inner)
        self.assertEqual(inner[inner.index("--flowctl-frontier-width") + 1], "2")
        self.assertEqual(inner[inner.index("--flowctl-max-frontier-width") + 1], "2")
        self.assertIn("--flowctl-frontier-propagation", inner)
        self.assertIn("--flowctl-stage-aware-frontier-propagation-depth", inner)
        self.assertIn("--flowctl-frontier-action", inner)
        self.assertEqual(inner[inner.index("--flowctl-frontier-action") + 1], "auto")
        self.assertIn("--flowctl-build-frontier-admission", inner)
        self.assertEqual(inner[inner.index("--flowctl-build-frontier-admission") + 1], "observe")
        self.assertIn("--flowctl-no-background-hints", inner)

    def test_toy_host_matrix_generates_summary_and_fidelity(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "value.txt").write_text("ok\n", encoding="utf-8")
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "fixture",
                        "instance_id": "toy",
                        "commands": [
                            {"id": "inspect", "cmd": "grep ok value.txt", "stage": "INSPECT"},
                            {"id": "test", "cmd": "test -f value.txt", "stage": "TEST", "critical": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = tmp / "matrix"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(MATRIX),
                    "--case",
                    f"toy={trace}::{repo}",
                    "--out-dir",
                    str(out_dir),
                    "--variant",
                    "default",
                    "--trials",
                    "1",
                    "--overwrite",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads((out_dir / "harness-replay-matrix-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema"], "flowcausal.harness_replay_matrix.v1")
            self.assertEqual(summary["status"], "pass")
            self.assertEqual([variant["name"] for variant in summary["variants"]], ["default"])
            self.assertEqual(summary["variants"][0]["comparison_to_baseline"]["makespan_delta"], 0.0)
            self.assertEqual(summary["variants"][0]["summary"]["pass_trials"], 1)
            self.assertIn("elapsed_jain_fairness_mean", summary["variants"][0]["summary"])
            self.assertIn("critical_jain_fairness_mean", summary["variants"][0]["summary"])
            trial = summary["variants"][0]["trials"][0]
            self.assertEqual(trial["fidelity"]["status"], "pass")
            self.assertTrue((out_dir / "harness-replay-matrix-summary.csv").is_file())
            self.assertTrue((out_dir / "harness-replay-matrix-summary.md").is_file())
            self.assertTrue(
                (
                    out_dir
                    / "runs"
                    / "default"
                    / "trial-01"
                    / "harness-replay-fidelity-audit.json"
                ).is_file()
            )


if __name__ == "__main__":
    unittest.main()

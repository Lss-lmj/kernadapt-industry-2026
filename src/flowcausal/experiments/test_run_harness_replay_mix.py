#!/usr/bin/env python3
"""Tests for the multi-harness replay mix runner."""

from __future__ import annotations

import json
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace


HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "run_harness_replay_mix.py"


def load_runner_module():
    spec = importlib.util.spec_from_file_location("run_harness_replay_mix", RUNNER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {RUNNER}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner_mod = load_runner_module()


class HarnessReplayMixTests(unittest.TestCase):
    def test_parse_cpu_list_and_round_robin_flowd_anchor(self) -> None:
        self.assertEqual(runner_mod.parse_cpu_list("0-2,4,2"), [0, 1, 2, 4])
        args = Namespace(
            flowd_anchor_policy="round-robin",
            flowd_anchor_cpus="0-1,3",
            taskset="",
            flowd_anchor_cpu=-1,
        )

        self.assertEqual(runner_mod.resolve_flowd_anchor_cpu(args, 0), 0)
        self.assertEqual(runner_mod.resolve_flowd_anchor_cpu(args, 1), 1)
        self.assertEqual(runner_mod.resolve_flowd_anchor_cpu(args, 2), 3)
        self.assertEqual(runner_mod.resolve_flowd_anchor_cpu(args, 3), 0)

    def test_cleanup_case_artifacts_removes_stale_control_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            run_dir = tmp / "run"
            run_dir.mkdir()
            work = run_dir / "work"
            work.mkdir()
            for name in (
                "stage-state.json",
                "frontier-labels.jsonl",
                "controller.log",
                "controller.ndjson",
                "system-profile.ndjson",
                "system-profile-summary.json",
                "system-profile-summary.md",
                "container.cid",
            ):
                (run_dir / name).write_text("stale\n", encoding="utf-8")

            runner_mod.cleanup_case_artifacts(run_dir)

            self.assertTrue(work.is_dir())
            self.assertFalse((run_dir / "stage-state.json").exists())
            self.assertFalse((run_dir / "system-profile-summary.json").exists())

    def test_build_flowctl_cmd_for_case_session(self) -> None:
        case = runner_mod.parse_case_spec(
            f"case-a={RUNNER}::{HERE}",
            index=0,
            stagger_ms=0,
        )
        args = Namespace(
            flowctl="/flowctl.py",
            flowctl_boost_budget_ms=10,
            flowctl_build_frontier_admission="tail-only",
            flowctl_confidence=80,
            flowctl_dry_run=True,
            flowctl_frontier_action="advance",
            flowctl_frontier_bypass_score=140,
            flowctl_frontier_hold_ms=0,
            flowctl_frontier_propagation=True,
            flowctl_frontier_propagation_depth=2,
            flowctl_frontier_stability_action="observe",
            flowctl_frontier_stability_samples=1,
            flowctl_frontier_width=1,
            flowctl_interval_ms=100,
            flowctl_max_frontier_width=1,
            flowctl_max_vtime_boost_us=500,
            flowctl_min_frontier_score=50,
            flowctl_pin_dir="/sys/fs/bpf/flowcausal",
            flowctl_policy_fallback=False,
            flowctl_policy_window_ms=100,
            flowctl_selected_stability=False,
            flowctl_stage_aware_frontier_propagation_depth=True,
            flowctl_stage_kind="AUTO",
            flowctl_sudo="never",
            flowctl_ttl_ms=1000,
            flowctl_width_control=True,
        )
        cmd = runner_mod.build_flowctl_cmd(args, case, HERE, 12345)

        self.assertEqual(cmd[:3], [sys.executable, "-u", "/flowctl.py"])
        self.assertIn("--root-pid", cmd)
        self.assertEqual(cmd[cmd.index("--root-pid") + 1], "12345")
        self.assertIn("--session", cmd)
        self.assertEqual(cmd[cmd.index("--session") + 1], "harness:case-a")
        self.assertIn("--stage-state-file", cmd)
        self.assertEqual(cmd[cmd.index("--stage-state-file") + 1], str(HERE / "stage-state.json"))
        self.assertIn("--width-control", cmd)
        self.assertIn("--frontier-propagation", cmd)
        self.assertIn("--stage-aware-frontier-propagation-depth", cmd)
        self.assertIn("--dry-run", cmd)

    def test_docker_case_builds_container_workload_command(self) -> None:
        case = runner_mod.parse_case_spec(
            f"case-a={RUNNER}::{HERE}::example/image:latest",
            index=0,
            stagger_ms=0,
        )
        case = runner_mod.ReplayCase(
            instance_id=case.instance_id,
            trace_file=case.trace_file,
            repo_dir=case.repo_dir,
            start_offset_ms=case.start_offset_ms,
            container_image=case.container_image,
            work_dir_aliases=("/sdf-xarray",),
        )
        cmd = runner_mod.build_workload_cmd(
            case,
            HERE,
            taskset="0-1",
            nice=5,
            allow_failure_stages=["RETRY_PROBE"],
            env_entries=[],
            host_env_entries=[],
            container_env_entries=[],
            copy_keep_git=True,
            copy_exclude=["experiments/results", ".venv"],
            container_python="/opt/py/bin/python",
            container_path_env="/opt/py/bin:/usr/bin",
            docker_user="current",
            cgroup_cpu_weight=0,
            cgroup_cpu_max="",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("--cidfile", cmd)
        cidfile_index = cmd.index("--cidfile") + 1
        self.assertEqual(cmd[cidfile_index], str(HERE / "container.cid"))
        self.assertIn("example/image:latest", cmd)
        self.assertIn("--cpuset-cpus", cmd)
        self.assertIn("0-1", cmd)
        self.assertNotEqual(cmd[:2], ["taskset", "-c"])
        image_index = cmd.index("example/image:latest")
        self.assertEqual(cmd[image_index + 1 : image_index + 4], ["nice", "-n", "5"])
        self.assertEqual(cmd[image_index + 4], "/opt/py/bin/python")
        self.assertIn("/flowcausal/experiments/workloads/agent_trace_workflow.py", cmd)
        self.assertIn("--copy-keep-git", cmd)
        self.assertIn("--copy-exclude", cmd)
        self.assertIn("experiments/results", cmd)
        self.assertIn("--work-dir-alias", cmd)
        self.assertEqual(cmd[cmd.index("--work-dir-alias") + 1], "/sdf-xarray")
        self.assertIn("HOME=/tmp/flowcausal-home", cmd)
        self.assertIn("XDG_CACHE_HOME=/tmp/flowcausal-cache", cmd)
        self.assertIn("PIP_CACHE_DIR=/tmp/flowcausal-pip-cache", cmd)
        self.assertIn("PATH=/opt/py/bin:/usr/bin", cmd)
        self.assertIn(f"{runner_mod.FLOWCAUSAL_ROOT.resolve()}:/flowcausal", cmd)
        self.assertIn(f"{HERE.resolve()}:/repo:ro", cmd)

    def test_host_and_container_env_entries_are_isolated(self) -> None:
        host_case = runner_mod.parse_case_spec(
            f"host-a={RUNNER}::{HERE}",
            index=0,
            stagger_ms=0,
        )
        docker_case = runner_mod.parse_case_spec(
            f"docker-a={RUNNER}::{HERE}::example/image:latest",
            index=0,
            stagger_ms=0,
        )

        common = ["COMMON_FLAG=1"]
        host_only = ["PATH=/host/tool_shims:/usr/bin", "FLOWCAUSAL_REPLAY_PYTHON=python"]
        container_only = ["FLOWCAUSAL_REPLAY_PYTHON=/opt/py/bin/python"]
        host_cmd = runner_mod.build_workload_cmd(
            host_case,
            HERE,
            taskset="",
            nice=0,
            allow_failure_stages=[],
            env_entries=common,
            host_env_entries=host_only,
            container_env_entries=container_only,
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="/opt/py/bin:/usr/bin",
            docker_user="current",
            cgroup_cpu_weight=0,
            cgroup_cpu_max="",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )
        docker_cmd = runner_mod.build_workload_cmd(
            docker_case,
            HERE,
            taskset="",
            nice=0,
            allow_failure_stages=[],
            env_entries=common,
            host_env_entries=host_only,
            container_env_entries=container_only,
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="/opt/py/bin:/usr/bin",
            docker_user="current",
            cgroup_cpu_weight=0,
            cgroup_cpu_max="",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        self.assertIn("COMMON_FLAG=1", host_cmd)
        self.assertIn("PATH=/host/tool_shims:/usr/bin", host_cmd)
        self.assertIn("FLOWCAUSAL_REPLAY_PYTHON=python", host_cmd)
        self.assertNotIn("FLOWCAUSAL_REPLAY_PYTHON=/opt/py/bin/python", host_cmd)

        self.assertIn("COMMON_FLAG=1", docker_cmd)
        self.assertIn("PATH=/opt/py/bin:/usr/bin", docker_cmd)
        self.assertIn("FLOWCAUSAL_REPLAY_PYTHON=/opt/py/bin/python", docker_cmd)
        self.assertNotIn("PATH=/host/tool_shims:/usr/bin", docker_cmd)
        self.assertNotIn("FLOWCAUSAL_REPLAY_PYTHON=python", docker_cmd)

    def test_docker_case_user_can_be_overridden_per_case(self) -> None:
        case = runner_mod.ReplayCase(
            instance_id="docker-a",
            trace_file=RUNNER,
            repo_dir=HERE,
            start_offset_ms=0,
            container_image="example/image:latest",
            docker_user="root",
        )

        cmd = runner_mod.build_workload_cmd(
            case,
            HERE,
            taskset="",
            nice=0,
            allow_failure_stages=[],
            env_entries=[],
            host_env_entries=[],
            container_env_entries=[],
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="",
            docker_user="current",
            cgroup_cpu_weight=0,
            cgroup_cpu_max="",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        self.assertEqual(cmd[cmd.index("--user") + 1], "0:0")

    def test_manifest_loads_work_dir_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            repo = tmp / "repo"
            trace.write_text(json.dumps({"commands": [{"cmd": "true"}]}), encoding="utf-8")
            repo.mkdir()
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "instance_id": "case-a",
                                "trace_file": str(trace),
                                "repo_dir": str(repo),
                                "container_image": "example/image:latest",
                                "work_dir_aliases": ["/sdf-xarray"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cases = runner_mod.load_manifest(manifest, stagger_ms=0)

            self.assertEqual(len(cases), 1)
            self.assertEqual(cases[0].work_dir_aliases, ("/sdf-xarray",))

    def test_manifest_loads_container_env_entries(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trace = tmp / "trace.json"
            repo = tmp / "repo"
            trace.write_text(json.dumps({"commands": [{"cmd": "true"}]}), encoding="utf-8")
            repo.mkdir()
            manifest = tmp / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "instance_id": "case-a",
                                "trace_file": str(trace),
                                "repo_dir": str(repo),
                                "container_image": "example/image:latest",
                                "container_env": [
                                    "PIP_FIND_LINKS=/wheelhouse",
                                    "PIP_NO_INDEX=1",
                                ],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            cases = runner_mod.load_manifest(manifest, stagger_ms=0)

            self.assertEqual(cases[0].container_env_entries, ("PIP_FIND_LINKS=/wheelhouse", "PIP_NO_INDEX=1"))

    def test_manifest_path_base_benchmark_root_resolves_benchmark_relative_paths(self) -> None:
        manifest = runner_mod.FLOWCAUSAL_ROOT / "benchmarks" / "harness-replaybench" / "manifests" / "formal-mix-8harness.json"

        cases = runner_mod.load_manifest(manifest, stagger_ms=0)

        self.assertGreaterEqual(len(cases), 1)
        self.assertEqual(
            cases[0].trace_file,
            (
                runner_mod.FLOWCAUSAL_ROOT
                / "benchmarks"
                / "harness-replaybench"
                / "traces"
                / "local"
                / "django__django-14608.mini-swe-agent.hrb-trace.json"
            ).resolve(),
        )
        self.assertEqual(
            cases[0].container_image,
            "swebench/sweb.eval.x86_64.django_1776_django-14608:latest",
        )
        self.assertEqual(
            cases[1].container_image,
            "swebench/sweb.eval.x86_64.matplotlib_1776_matplotlib-23314:latest",
        )
        self.assertEqual(cases[-1].docker_user, "root")
        self.assertEqual(
            cases[0].repo_dir,
            (
                runner_mod.FLOWCAUSAL_ROOT
                / "benchmarks"
                / "harness-replaybench"
                / "repos"
                / "django__django-14608"
            ).resolve(),
        )

    def test_host_case_builds_nice_workload_command(self) -> None:
        case = runner_mod.parse_case_spec(
            f"case-a={RUNNER}::{HERE}",
            index=0,
            stagger_ms=0,
        )
        cmd = runner_mod.build_workload_cmd(
            case,
            HERE,
            taskset="",
            nice=5,
            allow_failure_stages=[],
            env_entries=[],
            host_env_entries=[],
            container_env_entries=[],
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="",
            docker_user="current",
            cgroup_cpu_weight=0,
            cgroup_cpu_max="",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        self.assertEqual(cmd[:3], ["nice", "-n", "5"])
        self.assertEqual(cmd[3], sys.executable)
        self.assertIn(str(runner_mod.WORKLOAD), cmd)

    def test_docker_case_builds_cgroup_cpu_flags(self) -> None:
        case = runner_mod.parse_case_spec(
            f"case-a={RUNNER}::{HERE}::example/image:latest",
            index=0,
            stagger_ms=0,
        )
        cmd = runner_mod.build_workload_cmd(
            case,
            HERE,
            taskset="",
            nice=0,
            allow_failure_stages=[],
            env_entries=[],
            host_env_entries=[],
            container_env_entries=[],
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="",
            docker_user="current",
            cgroup_cpu_weight=100,
            cgroup_cpu_max="50000 100000",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        image_index = cmd.index("example/image:latest")
        self.assertIn("--cpu-shares", cmd[:image_index])
        self.assertEqual(
            cmd[cmd.index("--cpu-shares") + 1],
            str(runner_mod.cgroup_weight_to_docker_cpu_shares(100)),
        )
        self.assertIn("--cpu-quota", cmd[:image_index])
        self.assertEqual(cmd[cmd.index("--cpu-quota") + 1], "50000")
        self.assertIn("--cpu-period", cmd[:image_index])
        self.assertEqual(cmd[cmd.index("--cpu-period") + 1], "100000")

    def test_host_case_builds_static_cgroup_wrapper(self) -> None:
        case = runner_mod.parse_case_spec(
            f"case-a={RUNNER}::{HERE}",
            index=0,
            stagger_ms=0,
        )
        cmd = runner_mod.build_workload_cmd(
            case,
            HERE,
            taskset="0-1",
            nice=5,
            allow_failure_stages=[],
            env_entries=[],
            host_env_entries=[],
            container_env_entries=[],
            copy_keep_git=False,
            copy_exclude=[],
            container_python="/opt/py/bin/python",
            container_path_env="",
            docker_user="current",
            cgroup_cpu_weight=100,
            cgroup_cpu_max="max 100000",
            cgroup_wrapper=str(runner_mod.STATIC_CGROUP_WRAPPER),
            cgroup_sudo="never",
            cgroup_name_prefix="test-cgroup",
        )

        self.assertEqual(cmd[:2], [sys.executable, str(runner_mod.STATIC_CGROUP_WRAPPER.resolve())])
        self.assertIn("--cpu-weight", cmd)
        self.assertEqual(cmd[cmd.index("--cpu-weight") + 1], "100")
        self.assertIn("--cpu-max", cmd)
        self.assertEqual(cmd[cmd.index("--cpu-max") + 1], "max 100000")
        sep = cmd.index("--")
        self.assertEqual(cmd[sep + 1 : sep + 4], ["nice", "-n", "5"])
        self.assertEqual(cmd[sep + 4 : sep + 7], ["taskset", "-c", "0-1"])

    def test_two_case_mix_emits_summary_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo_a = tmp / "repo-a"
            repo_b = tmp / "repo-b"
            repo_a.mkdir()
            repo_b.mkdir()
            (repo_a / "value.txt").write_text("alpha\n", encoding="utf-8")
            (repo_b / "value.txt").write_text("beta\n", encoding="utf-8")
            trace_a = tmp / "trace-a.json"
            trace_b = tmp / "trace-b.json"
            trace_a.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "fixture",
                        "instance_id": "case-a",
                        "commands": [
                            {"id": "inspect", "cmd": "grep alpha value.txt", "stage": "INSPECT"},
                            {"id": "test", "cmd": "test -f value.txt", "stage": "TEST", "critical": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            trace_b.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "source": "fixture",
                        "instance_id": "case-b",
                        "commands": [
                            {"id": "inspect", "cmd": "cat value.txt", "stage": "INSPECT"},
                            {
                                "id": "package",
                                "cmd": "mkdir -p out && cp value.txt out/value.txt",
                                "stage": "PACKAGE_BACKGROUND",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = tmp / "mix"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--case",
                    f"case-a={trace_a}::{repo_a}",
                    "--case",
                    f"case-b={trace_b}::{repo_b}",
                    "--out-dir",
                    str(out_dir),
                    "--stagger-ms",
                    "10",
                    "--nice",
                    "5",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            stdout_summary = json.loads(proc.stdout)
            self.assertEqual(stdout_summary["status"], "pass")
            summary = json.loads((out_dir / "harness-replay-mix-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["schema"], "flowcausal.harness_replay_mix.v1")
            self.assertEqual(summary["status"], "pass")
            self.assertEqual(summary["nice"], 5)
            self.assertEqual(summary["cleanup_success_artifacts"], True)
            self.assertEqual(summary["cleanup_use_sudo"], True)
            self.assertEqual(len(summary["cases"]), 2)
            self.assertIn("aggregate", summary)
            self.assertGreater(summary["aggregate"]["mean_critical_sec"], 0)
            self.assertGreater(summary["aggregate"]["p95_critical_sec"], 0)
            self.assertGreater(summary["aggregate"]["elapsed_jain_fairness"], 0)
            self.assertGreater(summary["aggregate"]["critical_jain_fairness"], 0)
            by_id = {case["instance_id"]: case for case in summary["cases"]}
            self.assertGreater(by_id["case-a"]["process_pid"], 0)
            self.assertEqual(by_id["case-a"]["docker_identity"], {})
            self.assertEqual(by_id["case-a"]["parsed"]["stage_counts"], {"INSPECT": 1, "TEST": 1})
            self.assertEqual(by_id["case-a"]["parsed"]["critical_sec"] > 0, True)
            self.assertEqual(
                by_id["case-b"]["parsed"]["stage_counts"],
                {"INSPECT": 1, "PACKAGE_BACKGROUND": 1},
            )
            self.assertTrue((out_dir / "harness-replay-mix-summary.csv").is_file())
            self.assertTrue((out_dir / "harness-replay-mix-summary.md").is_file())
            self.assertTrue(pathlib.Path(by_id["case-a"]["stdout"]).is_file())
            self.assertTrue(pathlib.Path(by_id["case-b"]["stderr"]).is_file())
            self.assertFalse((out_dir / "runs" / "case-a" / "work").exists())
            self.assertFalse((out_dir / "runs" / "case-b" / "work").exists())
            self.assertFalse((out_dir / "runs" / "case-b" / "stage-out").exists())
            self.assertGreaterEqual(by_id["case-a"]["cleanup"]["removed_bytes"], 0)

    def test_keep_success_artifacts_preserves_successful_work_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "value.txt").write_text("alpha\n", encoding="utf-8")
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.agent_trace.v1",
                        "commands": [
                            {"id": "inspect", "cmd": "grep alpha value.txt", "stage": "INSPECT"},
                            {"id": "test", "cmd": "test -f value.txt", "stage": "TEST", "critical": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            out_dir = tmp / "mix"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--case",
                    f"case-a={trace}::{repo}",
                    "--out-dir",
                    str(out_dir),
                    "--keep-success-artifacts",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            summary = json.loads((out_dir / "harness-replay-mix-summary.json").read_text(encoding="utf-8"))
            case = summary["cases"][0]
            self.assertEqual(case["cleanup"]["enabled"], False)
            self.assertEqual(case["cleanup"]["use_sudo"], True)
            self.assertTrue((out_dir / "runs" / "case-a" / "work").is_dir())


if __name__ == "__main__":
    unittest.main()

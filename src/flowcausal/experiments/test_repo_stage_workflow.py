#!/usr/bin/env python3
"""Regression tests for the staged real-repository workload wrapper."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
WORKLOAD = HERE / "workloads" / "repo_stage_workflow.py"
MATRIX = HERE / "run_repo_ci_matrix.py"


def load_matrix_module() -> Any:
    spec = importlib.util.spec_from_file_location("run_repo_ci_matrix", MATRIX)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {MATRIX}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


matrix_mod = load_matrix_module()


def json_records(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.startswith("{"):
            continue
        records.append(json.loads(line))
    return records


class RepoStageWorkflowTests(unittest.TestCase):
    def test_stage_wrapper_emits_parseable_critical_and_stage_markers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
            work = tmp / "work"
            stage_state = tmp / "stage-state.json"
            label_file = tmp / "frontier-labels.jsonl"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(work),
                    "--overwrite",
                    "--stage-state-file",
                    str(stage_state),
                    "--frontier-label-file",
                    str(label_file),
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--inspect-cmd",
                    "find . -type f | sort > inspect.log",
                    "--build-cmd",
                    "mkdir -p build && cp hello.txt build/artifact.txt",
                    "--retry-cmd",
                    "test -f missing-file",
                    "--test-cmd",
                    "test -f build/artifact.txt",
                    "--package-cmd",
                    "tar -cf package.tar build",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertFalse((repo / "build" / "artifact.txt").exists())
            self.assertTrue((work / "build" / "artifact.txt").exists())
            self.assertTrue((work / "package.tar").exists())
            self.assertTrue(stage_state.exists())
            final_state = json.loads(stage_state.read_text(encoding="utf-8"))
            self.assertEqual(final_state["type"], "flowcausal_stage_state")
            self.assertEqual(final_state["phase"], "workload_end")
            self.assertEqual(final_state["stage"], "SHELL")

            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertEqual(pathlib.Path(begin["stage_state_file"]), stage_state)
            stage_ends = [r for r in records if r.get("type") == "agent_stage_end"]
            self.assertEqual(
                [r.get("stage") for r in stage_ends],
                ["INSPECT", "BUILD", "RETRY_PROBE", "TEST", "PACKAGE_BACKGROUND"],
            )
            retry = next(r for r in stage_ends if r.get("stage") == "RETRY_PROBE")
            self.assertNotEqual(retry.get("returncode"), 0)
            metrics = [r for r in records if r.get("type") == "flowcausal_metric"]
            self.assertEqual(metrics[0]["name"], "critical_sec")
            self.assertGreater(metrics[0]["value"], 0.0)
            metric_index = records.index(metrics[0])
            package_begin_index = next(
                idx
                for idx, record in enumerate(records)
                if record.get("type") == "agent_stage_begin"
                and record.get("stage") == "PACKAGE_BACKGROUND"
            )
            self.assertLess(metric_index, package_begin_index)
            labels = [r for r in records if r.get("type") == "frontier_label"]
            self.assertEqual(
                [r.get("stage") for r in labels],
                ["INSPECT", "BUILD", "RETRY_PROBE", "TEST", "PACKAGE_BACKGROUND"],
            )
            self.assertEqual(labels[0]["label"]["source"], "oracle")
            self.assertEqual(labels[0]["label"]["reason"], "repo-stage-active-command")
            self.assertEqual(labels[0]["source_id"], "turn0:inspect")
            label_file_records = json_records(label_file.read_text(encoding="utf-8"))
            self.assertEqual(label_file_records, labels)

            log = tmp / "workload.log"
            log.write_text(proc.stdout, encoding="utf-8")
            parsed = matrix_mod.parse_workload_log(log)
            self.assertGreater(parsed["metrics"]["critical_sec"], 0.0)
            self.assertIn("BUILD", parsed["stage_seconds"])
            self.assertIn("TEST", parsed["stage_seconds"])
            self.assertIn("PACKAGE_BACKGROUND", parsed["stage_seconds"])
            parsed_build = next(r for r in parsed["stage_records"] if r["stage"] == "BUILD")
            self.assertEqual(parsed_build["source_id"], "turn0:build")
            self.assertEqual(parsed_build["cmd"], "mkdir -p build && cp hello.txt build/artifact.txt")
            self.assertEqual(parsed["frontier_label_count"], 5)
            self.assertEqual(parsed["frontier_label_records"][0]["label"]["source"], "oracle")

    def test_missing_requested_critical_stage_falls_back_to_last_main_stage(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "Makefile").write_text("all:\n\t@echo ok\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--critical-after",
                    "TEST",
                    "--build-cmd",
                    "make all",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertEqual(begin["critical_after"], "BUILD")
            critical = next(r for r in records if r.get("type") == "flowcausal_critical_path")
            self.assertEqual(critical["stage"], "BUILD")

    def test_copy_root_preserves_adjacent_workspace_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            workspace = tmp / "workspace"
            repo = workspace / "project" / "src"
            deps = workspace / "deps"
            repo.mkdir(parents=True)
            deps.mkdir(parents=True)
            (deps / "value.txt").write_text("ok\n", encoding="utf-8")
            (repo / "Makefile").write_text(
                "check:\n\t@test -f ../../deps/value.txt\n",
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--copy-root",
                    str(workspace),
                    "--work-dir",
                    str(tmp / "copy"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--build-cmd",
                    "make check",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertEqual(pathlib.Path(begin["copy_root"]), workspace)
            self.assertEqual(pathlib.Path(begin["copy_dst"]), tmp / "copy")
            self.assertEqual(pathlib.Path(begin["work_dir"]), tmp / "copy" / "project" / "src")

    def test_copy_exclude_supports_relative_path_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            (repo / "experiments" / "results").mkdir(parents=True)
            (repo / "experiments" / "keep").mkdir(parents=True)
            (repo / "experiments" / "results" / "huge.txt").write_text("do not copy\n", encoding="utf-8")
            (repo / "experiments" / "keep" / "small.txt").write_text("copy me\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--copy-exclude",
                    "experiments/results",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--inspect-cmd",
                    "test -f experiments/keep/small.txt && test ! -e experiments/results",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((tmp / "work" / "experiments" / "keep" / "small.txt").exists())
            self.assertFalse((tmp / "work" / "experiments" / "results").exists())

    def test_nested_results_work_dir_auto_excludes_results_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            (repo / "experiments" / "results" / "old").mkdir(parents=True)
            (repo / "experiments" / "keep").mkdir(parents=True)
            (repo / "experiments" / "results" / "old" / "huge.txt").write_text("do not copy\n", encoding="utf-8")
            (repo / "experiments" / "keep" / "small.txt").write_text("copy me\n", encoding="utf-8")
            work = repo / "experiments" / "results" / "new-run" / "work"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(work),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--inspect-cmd",
                    "test -f experiments/keep/small.txt && test ! -e experiments/results/old",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((work / "experiments" / "keep" / "small.txt").exists())
            self.assertFalse((work / "experiments" / "results" / "old").exists())

    def test_copy_can_keep_compiled_build_artifacts_for_pinned_repos(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "module.so").write_text("binary placeholder\n", encoding="utf-8")
            (repo / "build").mkdir()
            (repo / "build" / "artifact.o").write_text("object placeholder\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--copy-keep-build-artifacts",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--test-cmd",
                    "test -f module.so && test -f build/artifact.o",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((tmp / "work" / "module.so").exists())
            self.assertTrue((tmp / "work" / "build" / "artifact.o").exists())
            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertTrue(begin["copy_keep_build_artifacts"])

    def test_stage_spec_file_supplies_commands_without_command_args(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "Makefile").write_text(
                "all:\n\t@mkdir -p build\n\t@echo built > build/artifact.txt\n"
                "test:\n\t@test -f build/artifact.txt\n",
                encoding="utf-8",
            )
            spec = tmp / "stage-spec.json"
            spec.write_text(
                json.dumps(
                    {
                        "inspect": "find . -type f | wc -l > /dev/null",
                        "build": "make all",
                        "test": "make test",
                    }
                ),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--stage-spec-file",
                    str(spec),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            records = json_records(proc.stdout)
            stage_ends = [r for r in records if r.get("type") == "agent_stage_end"]
            self.assertEqual([r.get("stage") for r in stage_ends], ["INSPECT", "BUILD", "TEST"])
            metric = next(r for r in records if r.get("type") == "flowcausal_metric")
            self.assertEqual(metric["meaning"], "final_turn_test_complete")

    def test_optional_frontier_descendant_labels_cover_foreground_workers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            label_file = tmp / "frontier-labels.jsonl"

            worker_code = "import time; time.sleep(0.35)"
            driver_code = (
                "import subprocess, sys, time; "
                f"p = subprocess.Popen([sys.executable, '-c', {worker_code!r}]); "
                "time.sleep(0.05); "
                "p.wait()"
            )
            build_cmd = f"{shlex.quote(sys.executable)} -c {shlex.quote(driver_code)}"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--frontier-label-file",
                    str(label_file),
                    "--frontier-label-descendants",
                    "--frontier-label-follow-ms",
                    "500",
                    "--frontier-label-follow-interval-ms",
                    "10",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--build-cmd",
                    build_cmd,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            labels = [r for r in json_records(proc.stdout) if r.get("type") == "frontier_label"]
            active_labels = [
                r for r in labels if r.get("label", {}).get("reason") == "repo-stage-active-command"
            ]
            descendant_labels = [
                r for r in labels if r.get("label", {}).get("reason") == "repo-stage-descendant-worker"
            ]
            self.assertEqual(len(active_labels), 1)
            self.assertGreaterEqual(len(descendant_labels), 1)
            self.assertTrue(all(r.get("root_pid") == active_labels[0]["pid"] for r in descendant_labels))
            self.assertTrue(all(r.get("parent_pid", 0) > 0 for r in descendant_labels))
            self.assertTrue(all(r.get("label", {}).get("role") == "foreground-worker" for r in descendant_labels))
            self.assertTrue(all(":desc:" in str(r.get("source_id")) for r in descendant_labels))
            self.assertEqual(json_records(label_file.read_text(encoding="utf-8")), labels)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Regression tests for the recorded code-agent trace workload wrapper."""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
WORKLOAD = HERE / "workloads" / "agent_trace_workflow.py"
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


class AgentTraceWorkflowTests(unittest.TestCase):
    def test_json_trace_emits_parseable_stage_markers_and_critical_path(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
            (repo / "Makefile").write_text(
                "all:\n\t@mkdir -p build\n\t@cp hello.txt build/artifact.txt\n"
                "test:\n\t@test -f build/artifact.txt\n",
                encoding="utf-8",
            )
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "source": "swe-agent-fixture",
                        "instance_id": "toy__issue-1",
                        "commands": [
                            {
                                "id": "inspect",
                                "cmd": "find . -maxdepth 1 -type f | sort > inspect.log",
                            },
                            {
                                "id": "edit",
                                "stage": "PATCH",
                                "cmd": "mkdir -p notes && echo patched > notes/change.txt",
                            },
                            {"id": "build", "cmd": "make all"},
                            {"id": "retry", "stage": "RETRY", "cmd": "test -f missing-file"},
                            {
                                "id": "test",
                                "cmd": "make test",
                                "critical": True,
                                "frontier_label": {
                                    "role": "foreground-gate",
                                    "reason": "last visible test command",
                                },
                            },
                            {"id": "package", "stage": "PACKAGE", "cmd": "tar -cf package.tar build"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            work = tmp / "work"
            stage_state = tmp / "stage-state.json"
            label_file = tmp / "frontier-labels.jsonl"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
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
                    "--critical-after-stage",
                    "BUILD",
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
            final_state = json.loads(stage_state.read_text(encoding="utf-8"))
            self.assertEqual(final_state["type"], "flowcausal_stage_state")
            self.assertEqual(final_state["phase"], "workload_end")
            self.assertEqual(final_state["stage"], "SHELL")

            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertEqual(begin["workload"], "agent_trace_workflow")
            self.assertEqual(begin["trace_source"], "swe-agent-fixture")
            self.assertEqual(begin["instance_id"], "toy__issue-1")
            self.assertEqual(begin["critical_command_index"], 4)
            stage_ends = [r for r in records if r.get("type") == "agent_stage_end"]
            self.assertEqual(
                [r.get("stage") for r in stage_ends],
                ["INSPECT", "EDIT", "BUILD", "RETRY_PROBE", "TEST", "PACKAGE_BACKGROUND"],
            )
            retry = next(r for r in stage_ends if r.get("stage") == "RETRY_PROBE")
            self.assertNotEqual(retry.get("returncode"), 0)

            labels = [r for r in records if r.get("type") == "frontier_label"]
            self.assertEqual(len(labels), 1)
            self.assertEqual(labels[0]["command_index"], 4)
            self.assertEqual(labels[0]["source_id"], "test")
            self.assertEqual(labels[0]["label"]["role"], "foreground-gate")
            label_file_records = json_records(label_file.read_text(encoding="utf-8"))
            self.assertEqual(label_file_records, labels)

            metric = next(r for r in records if r.get("type") == "flowcausal_metric")
            self.assertEqual(metric["name"], "critical_sec")
            metric_index = records.index(metric)
            package_begin_index = next(
                idx
                for idx, record in enumerate(records)
                if record.get("type") == "agent_stage_begin"
                and record.get("stage") == "PACKAGE_BACKGROUND"
            )
            self.assertLess(metric_index, package_begin_index)

            log = tmp / "workload.log"
            log.write_text(proc.stdout, encoding="utf-8")
            parsed = matrix_mod.parse_workload_log(log)
            self.assertGreater(parsed["metrics"]["critical_sec"], 0.0)
            self.assertIn("BUILD", parsed["stage_seconds"])
            self.assertIn("TEST", parsed["stage_seconds"])
            self.assertIn("PACKAGE_BACKGROUND", parsed["stage_seconds"])
            parsed_test = next(r for r in parsed["stage_records"] if r["stage"] == "TEST")
            self.assertEqual(parsed_test["command_index"], 4)
            self.assertEqual(parsed_test["source_id"], "test")
            self.assertEqual(parsed["frontier_label_count"], 1)
            self.assertEqual(parsed["frontier_label_records"][0]["source_id"], "test")

    def test_harness_replay_env_aliases_are_exported_to_trace_commands(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "source": "hrb-fixture",
                        "instance_id": "toy__hrb-env",
                        "commands": [
                            {
                                "id": "env-check",
                                "stage": "TEST",
                                "cmd": (
                                    "python3 - <<'PY'\n"
                                    "import json, os, pathlib\n"
                                    "out = pathlib.Path(os.environ['HRB_STAGE_OUT']) / 'env.json'\n"
                                    "out.parent.mkdir(parents=True, exist_ok=True)\n"
                                    "out.write_text(json.dumps({\n"
                                    "    'hrb_work_dir': os.environ['HRB_WORK_DIR'],\n"
                                    "    'flowcausal_work_dir': os.environ['FLOWCAUSAL_WORK_DIR'],\n"
                                    "    'hrb_replay_python': os.environ['HRB_REPLAY_PYTHON'],\n"
                                    "}), encoding='utf-8')\n"
                                    "PY"
                                ),
                                "critical": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            work = tmp / "work"
            stage_out = tmp / "stage-out"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(work),
                    "--overwrite",
                    "--stage-out-dir",
                    str(stage_out),
                    "--env",
                    "FLOWCAUSAL_REPLAY_PYTHON=/custom/python",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads((stage_out / "env.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["hrb_work_dir"], str(work.resolve()))
            self.assertEqual(payload["flowcausal_work_dir"], str(work.resolve()))
            self.assertEqual(payload["hrb_replay_python"], "/custom/python")

    def test_jsonl_nested_action_records_and_cwd_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            subdir = repo / "pkg"
            subdir.mkdir(parents=True)
            (subdir / "value.txt").write_text("ok\n", encoding="utf-8")
            trace = tmp / "trace.jsonl"
            trace.write_text(
                "\n".join(
                    [
                        json.dumps({"action": {"command": "grep ok value.txt"}, "cwd": "pkg"}),
                        json.dumps({"args": {"shell": "python3 -m py_compile generated.py"}, "cwd": "pkg"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            (subdir / "generated.py").write_text("x = 1\n", encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            records = json_records(proc.stdout)
            stage_ends = [r for r in records if r.get("type") == "agent_stage_end"]
            self.assertEqual([r.get("stage") for r in stage_ends], ["INSPECT", "BUILD"])
            self.assertTrue(all(pathlib.Path(r["cwd"]).name == "pkg" for r in stage_ends))

    def test_copy_keep_git_supports_replayed_git_commands(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (repo / "value.txt").write_text("before\n", encoding="utf-8")
            subprocess.run(["git", "add", "value.txt"], cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "commands": [
                            {"id": "edit", "stage": "EDIT", "cmd": "printf after > value.txt"},
                            {
                                "id": "diff",
                                "stage": "PACKAGE_BACKGROUND",
                                "cmd": "git diff -- value.txt > patch.txt",
                                "critical": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            work = tmp / "work"

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(work),
                    "--overwrite",
                    "--copy-keep-git",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue((work / ".git").is_dir())
            self.assertIn("value.txt", (work / "patch.txt").read_text(encoding="utf-8"))
            records = json_records(proc.stdout)
            stage_ends = [r for r in records if r.get("type") == "agent_stage_end"]
            self.assertEqual([r.get("returncode") for r in stage_ends], [0, 0])

    def test_trace_command_cwd_escape_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps({"commands": [{"cmd": "true", "cwd": ".."}]}),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertNotEqual(proc.returncode, 0)
            records = json_records(proc.stdout)
            error = next(r for r in records if r.get("type") == "workload_error")
            self.assertIn("cwd escapes work_dir", error["error"])
            end = next(r for r in records if r.get("type") == "workload_end")
            self.assertNotEqual(end["returncode"], 0)

    def test_work_dir_alias_points_to_replay_work_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            (repo / "value.txt").write_text("alias-ok\n", encoding="utf-8")
            alias = tmp / "alias-root"
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "id": "alias-read",
                                "stage": "INSPECT",
                                "cmd": f"test \"$(cat {alias}/value.txt)\" = alias-ok",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--work-dir-alias",
                    str(alias),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            records = json_records(proc.stdout)
            begin = next(r for r in records if r.get("type") == "workload_begin")
            self.assertEqual(begin["work_dir_aliases"], [str(alias)])
            self.assertFalse(alias.exists() or alias.is_symlink())

    def test_extra_env_path_is_not_overridden_by_login_profile(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            home = tmp / "home"
            home.mkdir()
            (home / ".bash_profile").write_text(
                "export FLOWCAUSAL_LOGIN_PROFILE_MARK=bad\nexport PATH=/usr/bin:/bin\n",
                encoding="utf-8",
            )
            fakebin = tmp / "fakebin"
            fakebin.mkdir()
            fake_pip = fakebin / "pip"
            fake_pip.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
            fake_pip.chmod(0o755)
            trace = tmp / "trace.json"
            trace.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "id": "path-check",
                                "stage": "SHELL",
                                "cmd": (
                                    'test "${FLOWCAUSAL_LOGIN_PROFILE_MARK:-}" != bad '
                                    '&& test "$(command -v pip)" = "$FLOWCAUSAL_FAKEBIN/pip"'
                                ),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            proc = subprocess.run(
                [
                    sys.executable,
                    str(WORKLOAD),
                    "--trace-file",
                    str(trace),
                    "--repo-dir",
                    str(repo),
                    "--work-dir",
                    str(tmp / "work"),
                    "--overwrite",
                    "--pre-sleep-ms",
                    "0",
                    "--post-sleep-ms",
                    "0",
                    "--env",
                    f"HOME={home}",
                    "--env",
                    f"FLOWCAUSAL_FAKEBIN={fakebin}",
                    "--env",
                    f"PATH={fakebin}{os.pathsep}{os.environ.get('PATH', '')}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()

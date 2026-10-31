#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
REPLAY = ROOT / "runners" / "replay_case.py"


class ReplayCaseTests(unittest.TestCase):
    def test_release_to_completion_includes_waiting_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repo = root/'repo'
            repo.mkdir()
            trace = self.make_trace(root)
            events, gate, ready = root/'events.jsonl', root/'start.gate', root/'ready'
            process = subprocess.Popen([
                sys.executable, str(REPLAY), '--trace', str(trace), '--repo', str(repo),
                '--work', str(root/'work'), '--events', str(events),
                '--command-log', str(root/'output.log'), '--gate', str(gate), '--ready', str(ready)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic()+5
                while not ready.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail('replay did not reach the ready gate')
                    time.sleep(.005)
                os.kill(process.pid, signal.SIGSTOP)
                gate.write_text(str(time.monotonic_ns())+'\n')
                time.sleep(.05)
                os.kill(process.pid, signal.SIGCONT)
                _, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                records = [json.loads(line) for line in events.read_text().splitlines()]
                begin = next(row for row in records if row['record']=='workload_begin')
                end = records[-1]
                self.assertTrue(begin['release_recorded'])
                self.assertGreaterEqual(begin['release_to_start_ns'], 40_000_000)
                self.assertEqual(end['workflow_completion_ns']-end['elapsed_ns'], begin['release_to_start_ns'])
            finally:
                if process.poll() is None:
                    os.kill(process.pid, signal.SIGCONT)
                    process.kill()
                    process.communicate()

    def test_legacy_empty_gate_retains_execution_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            gate = root/'start.gate'
            gate.touch()
            end = self.run_mode(root, 'tool-execution', ['--gate', str(gate)])
            self.assertEqual(end['workflow_completion_ns'], end['elapsed_ns'])

    def make_trace(self, directory: pathlib.Path) -> pathlib.Path:
        trace = {
            "schema": "harness_replaybench.trace.v2",
            "benchmark": "HarnessReplayBench",
            "version": "1.0",
            "instance_id": "toy",
            "source_task": {"framework": "imported", "repository": "local/toy"},
            "workspace": {
                "kind": "repository",
                "container_repo_dir": "",
                "command_environment": {
                    "interpreter": ["/bin/bash", "-c"],
                    "env": {"HRB_SOURCE_ENV_TEST": "present"},
                },
                "restore": "copy-before-replay",
                "preserve_git_metadata": True,
            },
            "capture": {
                "clock": "CLOCK_MONOTONIC",
                "source_trajectory": "toy.json",
                "online_collector": "framework-timestamps",
                "session_id": 1,
                "capture_elapsed_ns": 0,
                "dropped_events": 0,
            },
            "commands": [
                {
                    "id": "background",
                    "stage": "SHELL",
                    "cmd": "sleep 0.03; printf background > background.txt",
                    "cwd": ".",
                    "async": True,
                    "timing": {"tool_duration_ns": 30_000_000},
                },
                {
                    "id": "result",
                    "stage": "TEST",
                    "cmd": "test -f background.txt; printf result > result.txt",
                    "cwd": ".",
                    "critical": True,
                    "wait_for": ["background"],
                    "timing": {"tool_duration_ns": 1_000_000},
                },
            ],
            "replay": {
                "modes": ["tool-execution"],
                "primary_mode": "tool-execution",
                "output_checks": [{"type": "critical-command-completes"}],
            },
        }
        path = directory / "trace.json"
        path.write_text(json.dumps(trace), encoding="utf-8")
        return path

    def run_mode(
        self,
        root: pathlib.Path,
        mode: str,
        extra_args: list[str] | None = None,
        *,
        manual_result: bool = False,
    ) -> dict[str, object]:
        repo = root / "repo"
        repo.mkdir(exist_ok=True)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        trace = self.make_trace(root)
        if manual_result:
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["commands"][1]["frontier_label"] = {
                "source": "manual-benchmark-spec",
                "critical_policy": "人工逐项核对",
            }
            trace.write_text(json.dumps(value), encoding="utf-8")
        output = root / mode
        result = subprocess.run(
            [
                sys.executable,
                str(REPLAY),
                "--trace", str(trace),
                "--repo", str(repo),
                "--work", str(output / "work"),
                "--events", str(output / "events.jsonl"),
                "--command-log", str(output / "commands.log"),
                "--mode", mode,
                "--preserve-git",
                *(extra_args or []),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
        return next(record for record in reversed(records) if record["record"] == "workload_end")

    def test_tool_execution_preserves_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            end = self.run_mode(pathlib.Path(temp), "tool-execution")
            self.assertEqual(end["status"], "pass")
            self.assertEqual(end["completed_commands"], 2)
            self.assertGreater(int(end["critical_ns"]), 0)
            self.assertEqual(end["critical_metric_status"], "deprecated_heuristic_alias")
            self.assertEqual(
                end["selected_tool_milestone_semantics"], "heuristic_not_critical_path"
            )
            self.assertGreaterEqual(
                int(end["workflow_completion_ns"]), int(end["selected_tool_milestone_ns"])
            )

    def test_replay_uses_recorded_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["commands"] = [
                {
                    "id": "recorded",
                    "stage": "TEST",
                    "cmd": "printf recorded > selected.txt",
                    "normalized_cmd": "printf wrong > selected.txt; exit 9",
                    "cwd": ".",
                    "critical": True,
                    "timing": {"tool_duration_ns": 1},
                }
            ]
            trace.write_text(json.dumps(value), encoding="utf-8")
            repo = root / "repo"
            repo.mkdir()
            output = root / "recorded-command"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                    "--mode", "tool-execution",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (output / "work" / "selected.txt").read_text(encoding="utf-8"),
                "recorded",
            )

    def test_recorded_command_starts_from_recorded_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["workspace"]["replay_repo_subdir"] = "nested/repository"
            value["commands"] = [
                {
                    "id": "nested-root",
                    "stage": "TEST",
                    "cmd": (
                        "test -f nested/repository/seed.txt; "
                        "printf nested > nested/repository/selected.txt"
                    ),
                    "normalized_cmd": "exit 9",
                    "cwd": ".",
                    "critical": True,
                    "timing": {"tool_duration_ns": 1},
                }
            ]
            trace.write_text(json.dumps(value), encoding="utf-8")
            repo = root / "repo"
            repo.mkdir()
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            output = root / "nested-command"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                    "--mode", "tool-execution",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            repository = output / "work" / "nested" / "repository"
            self.assertEqual(
                (repository / "selected.txt").read_text(encoding="utf-8"),
                "nested",
            )

    def test_synchronous_tool_may_leave_real_descendant_for_next_tool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["commands"] = [
                {
                    "id": "launch-real-descendant",
                    "stage": "SHELL",
                    "cmd": "nohup sh -c 'sleep 0.08; printf alive > survivor.txt' >/dev/null 2>&1 &",
                    "cwd": ".",
                    "timing": {"tool_duration_ns": 1},
                },
                {
                    "id": "observe-real-descendant",
                    "stage": "TEST",
                    "cmd": "sleep 0.15; test -f survivor.txt",
                    "cwd": ".",
                    "critical": True,
                    "timing": {"tool_duration_ns": 1},
                },
            ]
            trace.write_text(json.dumps(value), encoding="utf-8")
            repo = root / "repo"
            repo.mkdir()
            output = root / "descendant"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                    "--mode", "tool-execution",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            end = next(record for record in reversed(records) if record["record"] == "workload_end")
            self.assertEqual(end["status"], "pass")

    def test_flowcausal_tool_events_use_persistent_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            socket_path = root / "flowd.sock"
            received: list[str] = []
            ready = threading.Event()

            def serve() -> None:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                    server.bind(str(socket_path))
                    server.listen(4)
                    ready.set()
                    client, _ = server.accept()
                    with client, client.makefile("r", encoding="utf-8") as stream:
                        for _ in range(6):
                            received.append(stream.readline().strip())
                            client.sendall(b"OK\n")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            self.assertTrue(ready.wait(timeout=2))
            end = self.run_mode(
                root,
                "tool-execution",
                ["--tool-event-socket", str(socket_path)],
                manual_result=True,
            )
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(end["status"], "pass")
            self.assertEqual([item.split()[0] for item in received], ["2"] * 6)
            self.assertEqual([item.split()[1] for item in received], [
                "tool-begin", "tool-update", "tool-end",
                "tool-begin", "tool-update", "tool-end",
            ])
            # 首个命令异步执行，第二个命令由执行器同步等待。类别标签
            # 只用于估计并行行为，不能单独触发提前运行。
            self.assertEqual(
                [item.split()[7] for item in received],
                ["asynchronous"] * 3 + ["synchronous"] * 3,
            )
            self.assertEqual(
                [item.split()[8] for item in received],
                ["SHELL"] * 3 + ["TEST"] * 3,
            )
            # 工具开始先登记当前工具，命令建立后再补充其真实根 pid；
            # 结束事件不再传 pid。
            roots = [int(item.split()[9]) for item in received]
            self.assertEqual(roots[0::3], [0, 0])
            self.assertTrue(all(value > 0 for value in roots[1::3]))
            self.assertEqual(roots[2::3], [0, 0])
            # 启发式关键标记不进入人工对照；人工逐项核对的第二个工具
            # 在开始、更新和结束事件中保持明确标记。
            manual = [int(item.split()[10]) for item in received]
            self.assertEqual(manual, [0, 0, 0, 1, 1, 1])

    def test_command_timeout_stops_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repo = root / "repo"
            repo.mkdir()
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["commands"] = [
                {
                    "id": "slow",
                    "stage": "TEST",
                    "cmd": "sleep 10",
                    "cwd": ".",
                    "critical": True,
                    "timeout_sec": 0.05,
                    "timing": {"tool_duration_ns": 10_000_000_000},
                }
            ]
            trace.write_text(json.dumps(value), encoding="utf-8")
            output = root / "timeout"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            end = next(record for record in reversed(records) if record["record"] == "workload_end")
            self.assertEqual(end["status"], "fail")
            self.assertIn("超过", end["failure"])

    def test_source_timeout_can_be_replayed_as_allowed_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repo = root / "repo"
            repo.mkdir()
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["commands"] = [
                {
                    "id": "source-timeout",
                    "stage": "TEST",
                    "cmd": "sleep 10",
                    "cwd": ".",
                    "critical": True,
                    "allow_failure": True,
                    "observed_returncode": -1,
                    "timeout_sec": 0.05,
                    "timing": {"tool_duration_ns": 50_000_000},
                }
            ]
            trace.write_text(json.dumps(value), encoding="utf-8")
            output = root / "allowed-timeout"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [json.loads(line) for line in (output / "events.jsonl").read_text().splitlines()]
            command_end = next(record for record in records if record["record"] == "command_end")
            workload_end = next(record for record in reversed(records) if record["record"] == "workload_end")
            self.assertTrue(command_end["timed_out"])
            self.assertEqual(command_end["returncode"], -1)
            self.assertEqual(workload_end["status"], "pass")

    def test_replay_restores_nested_repository_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["workspace"]["replay_repo_subdir"] = "owner__repo__1.0"
            value["commands"] = [
                {
                    "id": "nested",
                    "stage": "INSPECT",
                    "cmd": "test -f owner__repo__1.0/seed.txt",
                    "cwd": ".",
                    "critical": True,
                    "timing": {"tool_duration_ns": 1},
                }
            ]
            value["replay"]["modes"] = ["tool-execution"]
            trace.write_text(json.dumps(value), encoding="utf-8")
            output = root / "nested"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_replay_restores_recorded_absolute_repository_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            repo = root / "repo"
            repo.mkdir()
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            alias = root / "agent-repo"
            trace = self.make_trace(root)
            value = json.loads(trace.read_text(encoding="utf-8"))
            value["workspace"]["agent_repo_root"] = str(alias)
            value["commands"] = [
                {
                    "id": "absolute",
                    "stage": "INSPECT",
                    "cmd": f"test -f {alias}/seed.txt",
                    "cwd": ".",
                    "critical": True,
                    "timing": {"tool_duration_ns": 1},
                }
            ]
            value["replay"]["modes"] = ["tool-execution"]
            trace.write_text(json.dumps(value), encoding="utf-8")
            output = root / "absolute"
            result = subprocess.run(
                [
                    sys.executable,
                    str(REPLAY),
                    "--trace", str(trace),
                    "--repo", str(repo),
                    "--work", str(output / "work"),
                    "--events", str(output / "events.jsonl"),
                    "--command-log", str(output / "commands.log"),
                    "--restore-agent-paths",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

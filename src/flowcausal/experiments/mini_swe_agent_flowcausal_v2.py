#!/usr/bin/env python3
"""mini-swe-agent 与 FlowCausal v2 的薄适配层。

这个模块不执行调度判断。它只把框架已经明确知道的每次工具调用交给
容器内的 ``flowevent_v2``；工具开始、真实根进程号和结束事件均由该 C
程序发送，调度与识别仍完全位于 flowd_v2、eBPF 和 sched_ext 中。
"""
from __future__ import annotations

import atexit
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any

from minisweagent.agents.default import DefaultAgent


def stable_id(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def privileged(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


class FlowCausalV2MiniAgent(DefaultAgent):
    """保持 mini-swe-agent 行为不变，只为工具命令添加 C 事件入口。"""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._flowd: subprocess.Popen[str] | None = None
        self._flowd_log = None
        self._tool_index = 0
        self._stopped = False
        self._start_flowcausal()
        atexit.register(self._stop_flowcausal)

    def _docker(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        executable = getattr(getattr(self.env, "config", None), "executable", "docker")
        return subprocess.run(
            [executable, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=check,
        )

    def _start_flowcausal(self) -> None:
        container_id = getattr(self.env, "container_id", None)
        if not container_id:
            raise RuntimeError("FlowCausal v2 的 mini-swe-agent 适配只支持 Docker 环境")

        inspect = self._docker("inspect", "--format", "{{.State.Pid}}", container_id)
        root_pid = int(inspect.stdout.strip())
        if root_pid <= 0:
            raise RuntimeError("无法获得智能体容器的宿主机进程号")

        flowd = pathlib.Path(os.environ.get("FLOWCAUSAL_V2_FLOWD", "/tmp/flowd_v2")).resolve()
        event_client = pathlib.Path(
            os.environ.get("FLOWCAUSAL_V2_EVENT_CLIENT", "/tmp/flowevent_v2-static")
        ).resolve()
        if not flowd.is_file() or not os.access(flowd, os.X_OK):
            raise RuntimeError(f"flowd_v2 不可执行：{flowd}")
        if not event_client.is_file() or not os.access(event_client, os.X_OK):
            raise RuntimeError(f"flowevent_v2 静态程序不可执行：{event_client}")

        self._docker("cp", str(event_client), f"{container_id}:/tmp/flowevent_v2")
        self._docker("exec", "--user", "0", container_id, "chmod", "0555", "/tmp/flowevent_v2")

        suffix = str(container_id)[:12]
        self._session = os.environ.get("FLOWCAUSAL_V2_SESSION", f"mini-swe-agent:{suffix}")
        self._session_id = stable_id(self._session)
        socket_name = f"flowcausal-v2-{suffix}.sock"
        self._container_socket = f"/tmp/{socket_name}"
        self._host_socket = pathlib.Path(f"/proc/{root_pid}/root/tmp/{socket_name}")
        subprocess.run(
            privileged(["rm", "-f", str(self._host_socket)]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        log_dir = pathlib.Path(
            os.environ.get("FLOWCAUSAL_V2_REAL_AGENT_LOG_DIR", tempfile.gettempdir())
        ).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        self._flowd_log_path = log_dir / f"flowd-v2-{suffix}.log"
        self._stats_path = log_dir / f"flowd-v2-{suffix}.stats.jsonl"
        self._flowd_log = self._flowd_log_path.open("w", encoding="utf-8")
        command = privileged(
            [
                str(flowd),
                "--root-pid",
                str(root_pid),
                "--session",
                self._session,
                "--pin-dir",
                os.environ.get("FLOWCAUSAL_V2_PIN_DIR", "/sys/fs/bpf/flowcausal_v2"),
                "--serve-socket",
                str(self._host_socket),
            ]
        )
        self._flowd = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self._flowd_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._flowd.poll() is not None:
                self._flowd_log.flush()
                raise RuntimeError(
                    f"flowd_v2 提前退出，状态 {self._flowd.returncode}：{self._flowd_log_path}"
                )
            socket_ready = self._docker(
                "exec", container_id, "test", "-S", self._container_socket, check=False
            )
            if socket_ready.returncode == 0:
                return
            time.sleep(0.02)
        self._stop_flowcausal()
        raise RuntimeError(f"等待 flowd_v2 工具事件套接字超时：{self._flowd_log_path}")

    def _wrap_action(self, action: dict[str, Any]) -> dict[str, Any]:
        command = str(action.get("command") or "")
        self._tool_index += 1
        tool_id = self._tool_index
        sequence = 3 * self._tool_index - 2
        wrapper = [
            "/tmp/flowevent_v2",
            "--socket",
            self._container_socket,
            "--session-id",
            str(self._session_id),
            "--tool-id",
            str(tool_id),
            "--generation",
            str(tool_id),
            "--sequence",
            str(sequence),
            "--execution-mode",
            "synchronous",
            "--stage",
            "unknown",
            "--",
            "bash",
            "-c",
            command,
        ]
        return {**action, "command": shlex.join(wrapper)}

    def execute_actions(self, message: dict) -> list[dict]:
        wrapped = {
            **message,
            "extra": {
                **message.get("extra", {}),
                "actions": [self._wrap_action(action) for action in message.get("extra", {}).get("actions", [])],
            },
        }
        return super().execute_actions(wrapped)

    def _stop_flowcausal(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        process = self._flowd
        if process is not None and process.poll() is None:
            try:
                stats = subprocess.run(
                    privileged(
                        [
                            os.environ.get("FLOWCAUSAL_V2_FLOWD", "/tmp/flowd_v2"),
                            "--operation",
                            "stats",
                            "--session",
                            self._session,
                            "--pin-dir",
                            os.environ.get("FLOWCAUSAL_V2_PIN_DIR", "/sys/fs/bpf/flowcausal_v2"),
                            "--once",
                        ]
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=10,
                    check=False,
                )
                if stats.returncode == 0 and stats.stdout.strip():
                    self._stats_path.write_text(stats.stdout, encoding="utf-8")
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if self._flowd_log is not None:
            self._flowd_log.close()
        subprocess.run(
            privileged(["rm", "-f", str(self._host_socket)]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    def run(self, task: str = "", **kwargs: Any) -> dict:
        try:
            return super().run(task, **kwargs)
        finally:
            self._stop_flowcausal()

#!/usr/bin/env python3
"""SWE-agent 与 FlowCausal v2 的真实工具事件适配。

SWE-agent 在一个持续存在的 shell 中执行动作。直接用子 shell 包裹动作会改变
``cd``、环境变量和自定义命令的语义，因此本适配器保持动作文本不变：动作开始
前在同一容器中启动休眠的 C 事件进程，动作完成后再通知该进程结束。调度判断
仍只发生在 flowd_v2、eBPF 与 sched_ext 中。
"""
from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any

from sweagent.agent.hooks.abstract import AbstractAgentHook
from sweagent.run.common import BasicCLI
from sweagent.run.hooks.abstract import RunHook
from sweagent.run.run_single import RunSingle, RunSingleConfig


def stable_id(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def privileged(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


class FlowCausalV2ActionHook(AbstractAgentHook):
    """只报告 SWE-agent 已决定执行的动作，不改写动作。"""

    def __init__(self, env: Any, session_id: int, socket_path: str):
        self.env = env
        self.session_id = session_id
        self.socket_path = socket_path
        self.tool_index = 0
        self.active = False

    def on_init(self, *, agent: Any) -> None:
        self.agent = agent

    def on_action_started(self, *, step: Any) -> None:
        if self.active:
            self._end_active()
        self.tool_index += 1
        tool_id = self.tool_index
        sequence = 3 * tool_id - 2
        ready = f"/tmp/flowcausal-v2-action-{tool_id}.ready"
        log = f"/tmp/flowcausal-v2-action-{tool_id}.log"
        command = (
            f"rm -f {ready} {log}; "
            f"/tmp/flowevent_v2 --socket {self.socket_path} "
            f"--session-id {self.session_id} --tool-id {tool_id} "
            f"--generation {tool_id} --sequence {sequence} "
            f"--execution-mode synchronous --stage unknown --hold --ready-file {ready} "
            f">{log} 2>&1 & export FLOWCAUSAL_V2_HOLDER=$!; "
            "FLOWCAUSAL_V2_SPIN=0; "
            f"while [ ! -e {ready} ] && [ $FLOWCAUSAL_V2_SPIN -lt 1000000 ]; do "
            "FLOWCAUSAL_V2_SPIN=$((FLOWCAUSAL_V2_SPIN + 1)); done; "
            f"[ -e {ready} ]"
        )
        self.env.communicate(command, timeout=15, check="raise", error_msg="FlowCausal 工具开始事件失败")
        self.active = True

    def _end_active(self) -> None:
        if not self.active:
            return
        self.env.communicate(
            "if [ -n \"${FLOWCAUSAL_V2_HOLDER:-}\" ]; then "
            "kill -USR1 \"$FLOWCAUSAL_V2_HOLDER\" 2>/dev/null || true; "
            "wait \"$FLOWCAUSAL_V2_HOLDER\" 2>/dev/null || true; "
            "unset FLOWCAUSAL_V2_HOLDER FLOWCAUSAL_V2_SPIN; fi",
            timeout=15,
            check="warn",
            error_msg="FlowCausal 工具结束事件失败",
        )
        self.active = False

    def on_action_executed(self, *, step: Any) -> None:
        self._end_active()

    def on_run_done(self, *, trajectory: Any, info: Any) -> None:
        self._end_active()


class FlowCausalV2RunHook(RunHook):
    def __init__(self) -> None:
        self.run: RunSingle | None = None
        self.flowd: subprocess.Popen[str] | None = None
        self.flowd_log = None
        self.stopped = False
        self.host_socket: pathlib.Path | None = None
        self.action_hook: FlowCausalV2ActionHook | None = None

    def on_init(self, *, run: RunSingle) -> None:
        self.run = run

    def on_instance_start(self, *, index: int, env: Any, problem_statement: Any) -> None:
        deployment = env.deployment
        container_name = getattr(deployment, "container_name", None)
        if not container_name:
            raise RuntimeError("FlowCausal v2 的 SWE-agent 适配只支持 Docker 环境")
        runtime = getattr(getattr(deployment, "_config", None), "container_runtime", "docker")
        inspect = subprocess.run(
            [runtime, "inspect", "--format", "{{.State.Pid}}", container_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=True,
        )
        root_pid = int(inspect.stdout.strip())
        flowd = pathlib.Path(os.environ.get("FLOWCAUSAL_V2_FLOWD", "/tmp/flowd_v2")).resolve()
        event_client = pathlib.Path(
            os.environ.get("FLOWCAUSAL_V2_EVENT_CLIENT", "/tmp/flowevent_v2-static")
        ).resolve()
        if not flowd.is_file() or not os.access(flowd, os.X_OK):
            raise RuntimeError(f"flowd_v2 不可执行：{flowd}")
        if not event_client.is_file() or not os.access(event_client, os.X_OK):
            raise RuntimeError(f"flowevent_v2 静态程序不可执行：{event_client}")
        subprocess.run([runtime, "cp", str(event_client), f"{container_name}:/tmp/flowevent_v2"], check=True)
        subprocess.run([runtime, "exec", "--user", "0", container_name, "chmod", "0555", "/tmp/flowevent_v2"], check=True)

        suffix = container_name[-12:].replace("-", "")
        session = os.environ.get("FLOWCAUSAL_V2_SESSION", f"SWE-agent:{suffix}")
        session_id = stable_id(session)
        socket_name = f"flowcausal-v2-{suffix}.sock"
        container_socket = f"/tmp/{socket_name}"
        self.host_socket = pathlib.Path(f"/proc/{root_pid}/root/tmp/{socket_name}")
        subprocess.run(
            privileged(["rm", "-f", str(self.host_socket)]),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

        log_dir = pathlib.Path(
            os.environ.get("FLOWCAUSAL_V2_REAL_AGENT_LOG_DIR", tempfile.gettempdir())
        ).resolve()
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        self.suffix = suffix
        self.session = session
        self.flowd_log_path = log_dir / f"flowd-v2-swe-agent-{suffix}.log"
        self.flowd_log = self.flowd_log_path.open("w", encoding="utf-8")
        command = privileged(
            [
                str(flowd),
                "--root-pid",
                str(root_pid),
                "--session",
                session,
                "--pin-dir",
                os.environ.get("FLOWCAUSAL_V2_PIN_DIR", "/sys/fs/bpf/flowcausal_v2"),
                "--serve-socket",
                str(self.host_socket),
            ]
        )
        self.flowd = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=self.flowd_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.flowd.poll() is not None:
                self.flowd_log.flush()
                raise RuntimeError(f"flowd_v2 提前退出：{self.flowd_log_path}")
            ready = subprocess.run(
                [runtime, "exec", container_name, "test", "-S", container_socket],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if ready.returncode == 0:
                self.action_hook = FlowCausalV2ActionHook(env, session_id, container_socket)
                if self.run is None:
                    raise RuntimeError("SWE-agent 运行对象尚未初始化")
                self.run.agent.add_hook(self.action_hook)
                return
            time.sleep(0.02)
        self.stop()
        raise RuntimeError(f"等待 flowd_v2 工具事件套接字超时：{self.flowd_log_path}")

    def on_instance_completed(self, *, result: Any) -> None:
        self.stop()

    def on_end(self) -> None:
        self.stop()

    def stop(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        if self.action_hook is not None:
            try:
                self.action_hook._end_active()
            except Exception:
                pass
        if self.flowd is not None and self.flowd.poll() is None:
            try:
                stats = subprocess.run(
                    privileged(
                        [
                            os.environ.get("FLOWCAUSAL_V2_FLOWD", "/tmp/flowd_v2"),
                            "--operation",
                            "stats",
                            "--session",
                            self.session,
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
                    (self.log_dir / f"flowd-v2-swe-agent-{self.suffix}.stats.jsonl").write_text(
                        stats.stdout, encoding="utf-8"
                    )
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                os.killpg(self.flowd.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.flowd.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.flowd.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.flowd.wait(timeout=5)
        if self.flowd_log is not None:
            self.flowd_log.close()
        if self.host_socket is not None:
            subprocess.run(
                privileged(["rm", "-f", str(self.host_socket)]),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )


def main() -> int:
    config = BasicCLI(
        RunSingleConfig, help_text="运行带 FlowCausal v2 工具事件的 SWE-agent"
    ).get_config(sys.argv[1:])
    run = RunSingle.from_config(config)
    hook = FlowCausalV2RunHook()
    run.add_hook(hook)
    try:
        run.run()
    finally:
        hook.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

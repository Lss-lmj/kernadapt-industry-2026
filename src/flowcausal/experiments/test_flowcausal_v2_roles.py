#!/usr/bin/env python3
"""核对 FlowCausal 第二版四类会话内处理是否真正进入内核调度路径。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socket
import subprocess
import tempfile
import time
from typing import TextIO


SCHEDULER = pathlib.Path("/tmp/scx_flowcausal_v2")
FLOWD = pathlib.Path("/tmp/flowd_v2")
PIN_DIR = pathlib.Path("/sys/fs/bpf/flowcausal_v2")
SCX_STATE = pathlib.Path("/sys/kernel/sched_ext/state")


def stable_id(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def wait_until(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(description)


def privileged(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["sudo", "-n", *command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"命令失败：{' '.join(command)}\n{result.stdout}{result.stderr}")
    return result


def connect(path: pathlib.Path) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(path))
    return client


def request(client: socket.socket, line: str) -> None:
    client.sendall((line + "\n").encode())
    data = bytearray()
    while b"\n" not in data:
        part = client.recv(256)
        if not part:
            raise RuntimeError("控制连接在返回结果前关闭")
        data.extend(part)
    response = data.decode(errors="replace").strip()
    if response != "OK":
        raise RuntimeError(f"控制程序拒绝请求：{line} -> {response}")


def blocking_wait(process: subprocess.Popen[bytes], timeout: int = 20) -> int:
    """使用真正的阻塞 waitpid；带超时的 Popen.wait 会改用非阻塞轮询。"""

    def expired(_signum, _frame):
        raise TimeoutError(f"等待进程 {process.pid} 结束超时")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(timeout)
    try:
        _, status = os.waitpid(process.pid, 0)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    return os.waitstatus_to_exitcode(status)


def stop(process: subprocess.Popen, name: str) -> None:
    if process.poll() is not None:
        return
    privileged("kill", "-TERM", str(process.pid), check=False)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        privileged("pkill", "-KILL", "-x", name, check=False)
        process.wait(timeout=8)


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 FlowCausal 第二版的四类处理")
    parser.add_argument(
        "--scheduler-arg", action="append", default=[],
        help="附加调度器参数，可重复使用",
    )
    args = parser.parse_args()
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    if not shutil_which("stress-ng"):
        raise SystemExit("缺少 stress-ng")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-roles-"))
    session = f"flowcausal-v2:roles:{os.getpid()}"
    session_id = stable_id(session)
    socket_path = output / "flowd.sock"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    flowd_log: TextIO = (output / "flowd.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen | None = None
    flowd: subprocess.Popen | None = None
    background: subprocess.Popen[bytes] | None = None
    client: socket.socket | None = None
    result: dict[str, object] = {"session_id": session_id, "output": str(output)}

    try:
        scheduler = subprocess.Popen(
            ["sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
             "--interactive", "--detailed-accounting", "--quiet",
             *args.scheduler_arg],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled"
            and privileged(
                "test", "-e", str(PIN_DIR / "session_config"), check=False
            ).returncode == 0,
            15,
            "等待第二版调度器启动超时",
        )
        flowd = subprocess.Popen(
            ["sudo", "-n", str(FLOWD), "--root-pid", str(os.getpid()),
             "--session", session, "--pin-dir", str(PIN_DIR),
             "--serve-socket", str(socket_path), "--events"],
            stdin=subprocess.DEVNULL,
            stdout=flowd_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        wait_until(lambda: socket_path.exists(), 10, "等待 C 控制程序启动超时")
        client = connect(socket_path)

        # 先留下一个异步的处理器密集任务，再开始新的同步工具。
        background = subprocess.Popen(
            ["taskset", "-c", "0-7", "stress-ng", "--cpu", "8",
             "--timeout", "3s", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        request(
            client,
            f"2 tool-begin {session_id} 100 0 1 1 asynchronous PACKAGE {background.pid}",
        )
        time.sleep(0.15)
        request(client, f"2 tool-end {session_id} 100 0 1 2 asynchronous PACKAGE 0")

        # 套接字对端（本测试进程）是执行范围根，它会真正阻塞等待工具。
        request(client, f"2 tool-begin {session_id} 200 0 3 3 synchronous TEST 0")
        foreground = subprocess.Popen(
            ["taskset", "-c", "0-7", "bash", "-c",
             "stress-ng --cpu 2 --timeout 1s --quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        request(
            client,
            f"2 tool-update {session_id} 200 0 3 4 synchronous TEST {foreground.pid}",
        )
        foreground_rc = blocking_wait(foreground)
        foreground.returncode = foreground_rc
        request(client, f"2 tool-end {session_id} 200 0 3 5 synchronous TEST 0")
        if foreground_rc:
            raise RuntimeError(f"同步工具异常结束：{foreground_rc}")

        stats_run = privileged(
            str(FLOWD), "--operation", "stats", "--session-id",
            str(session_id), "--pin-dir", str(PIN_DIR), "--once"
        )
        lines = [line for line in stats_run.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise RuntimeError(f"没有取得统计：{stats_run.stdout}{stats_run.stderr}")
        stats = json.loads(lines[-1])
        result["stats"] = stats
        required = {
            "critical_progress": stats["return_promotions"] > 0,
            "short_interaction": stats["interactive_wakeups"] > 0
            and stats["interactive_runs"] > 0,
            "ordinary_execution": stats["role_enqueues"][0] > 0,
            "background_execution": stats["role_enqueues"][4] > 0
            and stats["stale_delays"] > 0,
        }
        if "--adaptive-session-weight" in args.scheduler_arg:
            required["动态权重启动"] = stats["adaptive_weight_activations"] > 0
            required["动态权重恢复"] = stats["adaptive_weight_deactivations"] > 0
            required["动态权重参与调度"] = stats["adaptive_weight_dispatches"] > 0
            required["动态权重超过基础值"] = stats["adaptive_weight_peak"] > 100
        result["checks"] = required
        if not all(required.values()):
            raise RuntimeError(f"四类处理没有全部触发：{required}\n{json.dumps(stats, ensure_ascii=False)}")
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(output)
        print(json.dumps(required, ensure_ascii=False))
        return 0
    finally:
        if client is not None:
            client.close()
        if background is not None and background.poll() is None:
            background.terminate()
            try:
                background.wait(timeout=5)
            except subprocess.TimeoutExpired:
                background.kill()
                background.wait()
        if flowd is not None:
            stop(flowd, "flowd_v2")
        if scheduler is not None:
            stop(scheduler, "scx_flowcausal_v2")
        scheduler_log.close()
        flowd_log.close()


def shutil_which(command: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = pathlib.Path(directory) / command
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


if __name__ == "__main__":
    raise SystemExit(main())

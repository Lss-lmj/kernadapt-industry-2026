#!/usr/bin/env python3
"""核对第二版工具协议、事件顺序和同会话多工具状态。"""

from __future__ import annotations

import errno
import json
import os
import pathlib
import signal
import socket
import subprocess
import tempfile

from test_flowcausal_v2_lifecycle import (
    FLOWD,
    HELPER,
    PIN_DIR,
    SCX_STATE,
    SCHEDULER,
    cgroup_path,
    privileged,
    stable_id,
    start_flowd,
    stop_process,
    wait_until,
)


def exchange(client: socket.socket, line: str) -> str:
    client.sendall((line + "\n").encode("utf-8"))
    data = bytearray()
    while b"\n" not in data:
        part = client.recv(128)
        if not part:
            raise RuntimeError("控制连接在返回结果前关闭")
        data.extend(part)
    return data.decode("utf-8", errors="replace").strip()


def expect(client: socket.socket, line: str, response: str) -> None:
    actual = exchange(client, line)
    if actual != response:
        raise RuntimeError(f"协议返回不符：{line} -> {actual}，预期 {response}")


def assert_state(
    session_id: int,
    cgroup_id: int,
    count: int,
    current: int,
    tool: int,
    parent: int,
    generation: int,
    stage: int,
) -> None:
    privileged(
        str(HELPER),
        "assert-tool-state",
        str(PIN_DIR),
        str(session_id),
        str(cgroup_id),
        str(count),
        str(current),
        str(tool),
        str(parent),
        str(generation),
        str(stage),
    )


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-protocol-"))
    session = f"flowcausal-v2:protocol:{os.getpid()}"
    session_id = stable_id(session)
    cgroup_id = cgroup_path(os.getpid()).stat().st_ino
    socket_path = output / "flowd.sock"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    flowd: subprocess.Popen[str] | None = None
    flowd_log = None
    client: socket.socket | None = None
    second_client: socket.socket | None = None
    checks: dict[str, str] = {}

    try:
        scheduler = subprocess.Popen(
            ["sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled"
            and privileged("test", "-e", str(PIN_DIR / "session_config"), check=False).returncode == 0,
            timeout=15,
            description="等待第二版调度器启动超时",
        )
        flowd, flowd_log = start_flowd(
            session=session,
            root_pid=os.getpid(),
            socket_path=socket_path,
            log_path=output / "flowd.log",
        )
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(3)
        client.connect(str(socket_path))

        expect(client, "tool-begin 10 0 1 synchronous 0", f"ERR {errno.EPROTONOSUPPORT}")
        checks["旧格式明确拒绝"] = "通过"
        expect(
            client,
            "2 tool-begin 1 10 0 1 1 synchronous TEST 0",
            f"ERR {errno.EACCES}",
        )
        checks["错误会话拒绝"] = "通过"

        expect(
            client,
            f"2 tool-begin {session_id} 10 0 1 1 asynchronous PACKAGE 0",
            "OK",
        )

        second_client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        second_client.settimeout(3)
        second_client.connect(str(socket_path))
        expect(
            second_client,
            f"2 tool-begin {session_id} 20 0 2 2 synchronous TEST 0",
            "OK",
        )
        expect(
            second_client,
            f"2 tool-begin {session_id} 30 0 3 3 asynchronous SERVICE 0",
            "OK",
        )
        assert_state(session_id, cgroup_id, 3, 20, 20, 0, 2, 5)
        checks["同会话保留两个活动工具"] = "通过"
        checks["并发控制连接"] = "通过"

        expect(
            second_client,
            f"2 tool-begin {session_id} 20 0 3 4 synchronous TEST 0",
            f"ERR {errno.EALREADY}",
        )
        expect(
            second_client,
            f"2 tool-dependency {session_id} 20 10 2 4 synchronous TEST 0",
            "OK",
        )
        assert_state(session_id, cgroup_id, 3, 20, 20, 10, 2, 5)
        checks["工具依赖更新"] = "通过"

        expect(
            second_client,
            f"2 tool-update {session_id} 20 10 2 4 synchronous TEST 0",
            f"ERR {errno.ESTALE}",
        )
        expect(
            second_client,
            f"2 tool-dependency {session_id} 20 999 2 5 synchronous TEST 0",
            f"ERR {errno.ENOENT}",
        )
        expect(
            second_client,
            f"2 tool-update {session_id} 20 10 2 5 synchronous TEST 0 1",
            "OK",
        )
        privileged(
            str(HELPER), "assert-tool-flags", str(PIN_DIR),
            str(session_id), "20", "1",
        )
        checks["人工指定工具标记写入内核"] = "通过"
        checks["重复乱序和未知依赖拒绝"] = "通过"

        second_client.close()
        second_client = None
        wait_until(
            lambda: subprocess.run(
                [
                    "sudo", "-n", str(HELPER), "assert-tool-state",
                    str(PIN_DIR), str(session_id), str(cgroup_id),
                    "1", "10", "10", "0", "1", "7",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            ).returncode == 0,
            timeout=3,
            description="并发连接断开后没有清理它启动的全部工具",
        )
        assert_state(session_id, cgroup_id, 1, 10, 10, 0, 1, 7)
        checks["连接断开清理所属工具并恢复其他连接工具"] = "通过"
        expect(
            client,
            f"2 tool-end {session_id} 10 0 1 8 asynchronous PACKAGE 0",
            "OK",
        )
        checks["全部工具正常结束"] = "通过"
        client.close()
        client = None

        result = {
            "schema": "flowcausal.v2.protocol-test.v1",
            "status": "pass",
            "session_id": session_id,
            "cpu_set": "0-7",
            "checks": checks,
        }
        (output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(output)
        print(json.dumps(checks, ensure_ascii=False))
        return 0
    finally:
        if client is not None:
            client.close()
        if second_client is not None:
            second_client.close()
        if flowd is not None:
            stop_process(flowd, signal.SIGTERM)
        if flowd_log is not None:
            flowd_log.close()
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


if __name__ == "__main__":
    raise SystemExit(main())

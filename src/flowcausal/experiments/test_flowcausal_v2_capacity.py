#!/usr/bin/env python3
"""验证内核关系映射写满时，任务继续运行并记录普通调度后备路径。"""

from __future__ import annotations

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
    privileged,
    stable_id,
    start_flowd,
    stop_process,
    wait_until,
)


def exchange(client: socket.socket, line: str) -> str:
    client.sendall((line + "\n").encode())
    data = bytearray()
    while b"\n" not in data:
        part = client.recv(128)
        if not part:
            raise RuntimeError("控制连接提前关闭")
        data.extend(part)
    return data.decode(errors="replace").strip()


def map_failures() -> int:
    result = privileged(str(HELPER), "global-map-failures", str(PIN_DIR))
    return int(result.stdout.strip())


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-capacity-"))
    session = f"flowcausal-v2:capacity:{os.getpid()}"
    session_id = stable_id(session)
    socket_path = output / "flowd.sock"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    flowd: subprocess.Popen[str] | None = None
    flowd_log = None
    client: socket.socket | None = None
    filled = 0

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
            and privileged("test", "-e", str(PIN_DIR / "flow_v2_task_meta"), check=False).returncode == 0,
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
        client.settimeout(5)
        client.connect(str(socket_path))
        request = f"2 tool-begin {session_id} 1 0 1 1 synchronous TEST 0"
        if exchange(client, request) != "OK":
            raise RuntimeError("工具登记失败")

        before = map_failures()
        fill = privileged(str(HELPER), "fill-task-map", str(PIN_DIR))
        filled = int(fill.stdout.strip())
        if filled < 1000:
            raise RuntimeError(f"关系映射压力不足：只填入 {filled} 项")

        # 子进程身份无法写入时仍应按普通任务完成，不能使调度器退出。
        workload = subprocess.run(
            ["taskset", "-c", "0-7", "bash", "-c", "for ((i=0;i<200000;i++)); do :; done"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        after = map_failures()
        scheduler_alive = (
            scheduler.poll() is None
            and SCX_STATE.read_text(encoding="utf-8").strip() == "enabled"
        )
        checks = {
            "映射达到容量": filled > 0,
            "写入失败被记录": after > before,
            "任务退回普通调度并完成": workload.returncode == 0,
            "调度器继续运行": scheduler_alive,
        }
        result = {
            "schema": "flowcausal.v2.capacity-test.v1",
            "status": "pass" if all(checks.values()) else "fail",
            "inserted_entries": filled,
            "map_failures_before": before,
            "map_failures_after": after,
            "checks": checks,
        }
        (output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(output)
        print(json.dumps(result, ensure_ascii=False))
        if not all(checks.values()):
            raise RuntimeError(f"容量边界测试失败：{checks}")
        return 0
    finally:
        if filled:
            privileged(
                str(HELPER), "clear-filled-task-map", str(PIN_DIR), str(filled), check=False
            )
        if client is not None:
            try:
                exchange(client, f"2 tool-end {session_id} 1 0 1 2 synchronous TEST 0")
            except (OSError, RuntimeError):
                pass
            client.close()
        if flowd is not None:
            stop_process(flowd, signal.SIGTERM)
        if flowd_log is not None:
            flowd_log.close()
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


if __name__ == "__main__":
    raise SystemExit(main())

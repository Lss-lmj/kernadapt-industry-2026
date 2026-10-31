#!/usr/bin/env python3
"""验证非 wait 同步下，真实排队的同工具后代获得一次短时提前。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import tempfile

from test_flowcausal_v2_lifecycle import (
    FLOWD,
    PIN_DIR,
    SCX_STATE,
    SCHEDULER,
    privileged,
    read_session_stats,
    stable_id,
    start_flowd,
    stop_process,
    wait_until,
)
from test_flowcausal_v2_resources import write_privileged


FLOWEVENT = pathlib.Path("/tmp/flowevent_v2")
WORKLOAD = pathlib.Path("/tmp/flowcausal_v2_pipe_workload")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    output = pathlib.Path(args.output) if args.output else pathlib.Path(
        tempfile.mkdtemp(prefix="flowcausal-v2-sleeping-descendant-")
    )
    output.mkdir(parents=True, exist_ok=True)
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    flowd: subprocess.Popen[str] | None = None
    flowd_log = None
    holder: subprocess.Popen[bytes] | None = None
    pressure: subprocess.Popen[bytes] | None = None
    tool: subprocess.Popen[bytes] | None = None
    socket_path = pathlib.Path(f"/tmp/fcv2-sleeping-descendant-{os.getpid()}.sock")
    session_name = f"flowcausal-v2:sleeping-descendant:{os.getpid()}"
    session_id = stable_id(session_name)
    root = pathlib.Path("/sys/fs/cgroup") / f"flowcausal-v2-sleeping-descendant-{os.getpid()}"
    session_cgroup = root / "session"
    try:
        scheduler = subprocess.Popen(
            [
                "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
                "--sleeping-descendant-progress", "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )

        def scheduler_ready() -> bool:
            if scheduler is not None and scheduler.poll() is not None:
                scheduler_log.flush()
                raise RuntimeError(
                    (output / "scheduler.log").read_text(encoding="utf-8", errors="replace")
                )
            return SCX_STATE.read_text(encoding="utf-8").strip() == "enabled" and privileged(
                "test", "-e", str(PIN_DIR / "session_config"), check=False
            ).returncode == 0

        wait_until(scheduler_ready, timeout=15, description="等待 FlowCausal v2 启动超时")
        privileged("mkdir", str(root))
        write_privileged(root / "cgroup.subtree_control", "+cpu +cpuset\n")
        privileged("mkdir", str(session_cgroup))
        write_privileged(session_cgroup / "cpu.weight", "100\n")
        holder = subprocess.Popen(
            ["sleep", "60"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        write_privileged(session_cgroup / "cgroup.procs", f"{holder.pid}\n")
        flowd, flowd_log = start_flowd(
            session=session_name,
            root_pid=holder.pid,
            socket_path=socket_path,
            log_path=output / "flowd.log",
            extra_args=("--max-progress-group", "1", "--max-progress-depth", "2"),
        )
        pressure = subprocess.Popen(
            ["taskset", "-c", "0-7", "stress-ng", "--cpu", "8", "--timeout", "4", "--quiet"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        tool = subprocess.Popen(
            [
                "bash", "-c", 'read -r start; exec "$@"',
                "flowcausal-v2-sleeping-descendant", str(FLOWEVENT),
                "--socket", str(socket_path), "--session-id", str(session_id),
                "--tool-id", "1", "--generation", "1", "--sequence", "1",
                "--execution-mode", "synchronous", "--stage", "TEST", "--",
                str(WORKLOAD), "1500",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            start_new_session=True,
        )
        write_privileged(session_cgroup / "cgroup.procs", f"{tool.pid}\n")
        assert tool.stdin is not None
        tool.stdin.write(b"start\n")
        tool.stdin.close()
        tool.wait(timeout=30)
        assert tool.stderr is not None
        stderr = tool.stderr.read().decode(errors="replace")
        if tool.returncode:
            raise RuntimeError(f"管道同步任务失败：{tool.returncode}\n{stderr}")
        stats = read_session_stats(session_id)
        checks = {
            "观察到上层进程睡眠期间的后代": int(stats.get("sleeping_descendant_candidates", 0)) > 0,
            "真实排队的后代获得短时提前": int(stats.get("sleeping_descendant_promotions", 0)) > 0,
            "识别到睡眠父进程的可运行子进程": int(stats.get("max_sleeping_descendant_depth", 0)) >= 1,
            "工作负载未依赖深层wait关系": int(stats.get("nested_wait_links", 0)) == 0,
        }
        result = {"status": "pass" if all(checks.values()) else "fail", "checks": checks, "stats": stats}
        (output / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(result, ensure_ascii=False))
        if not all(checks.values()):
            raise RuntimeError(f"非wait同步下的后代识别没有完成：{checks}")
        return 0
    finally:
        for process in (tool, pressure, holder):
            if process is not None:
                stop_process(process, signal.SIGTERM)
        if flowd is not None:
            stop_process(flowd, signal.SIGTERM)
        if flowd_log is not None:
            flowd_log.close()
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()
        socket_path.unlink(missing_ok=True)
        privileged("rmdir", str(session_cgroup), check=False)
        privileged("rmdir", str(root), check=False)


if __name__ == "__main__":
    raise SystemExit(main())

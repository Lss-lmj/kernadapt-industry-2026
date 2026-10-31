#!/usr/bin/env python3
"""短时间核对第二版是否遵守会话 cgroup 的 cpu.max。"""

from __future__ import annotations

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
    stop_process,
    wait_until,
)
from test_flowcausal_v2_resources import Environment, usage_deltas, write_privileged


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-quota-"))
    cgroup_root = pathlib.Path("/sys/fs/cgroup") / f"flowcausal-v2-quota-{os.getpid()}"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    env: Environment | None = None

    try:
        scheduler = subprocess.Popen(
            [
                "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
                "--enforce-cpu-max", "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled",
            timeout=15,
            description="等待第二版调度器启动超时",
        )
        privileged("mkdir", str(cgroup_root))
        write_privileged(cgroup_root / "cgroup.subtree_control", "+cpu +cpuset\n")
        env = Environment(cgroup_root)
        limited = env.create(threads=32, cpu_max="200000 100000")
        deltas, elapsed = usage_deltas([limited], 3.0)
        used = deltas[0]
        average_cores = used / (elapsed * 1_000_000)
        stats_result = privileged(
            str(FLOWD), "--pin-dir", str(PIN_DIR), "--operation", "stats",
            "--session-id", str(limited.session_id), "--once",
        )
        stats_lines = [
            line for line in stats_result.stdout.splitlines() if line.startswith("{")
        ]
        runtime = privileged(
            "/tmp/flowcausal_v2_test_helper", "session-runtime-full",
            str(PIN_DIR), str(limited.session_id), check=False,
        )
        result = {
            "status": "pass" if 1.5 <= average_cores <= 2.35 else "fail",
            "cpu_max": "200000 100000",
            "cpu_usage_us": used,
            "elapsed_sec": elapsed,
            "average_cores": average_cores,
            "session_runtime": runtime.stdout.strip(),
            "scheduler_stats": json.loads(stats_lines[-1]) if stats_lines else {},
        }
        (output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(output)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "pass" else 1
    finally:
        if env is not None:
            env.close_all()
        privileged("rmdir", str(cgroup_root), check=False)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


if __name__ == "__main__":
    raise SystemExit(main())

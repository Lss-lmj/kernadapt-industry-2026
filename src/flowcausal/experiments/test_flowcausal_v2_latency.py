#!/usr/bin/env python3
"""比较启用和关闭短交互处理时，阻塞等待恢复后的运行延迟。"""

from __future__ import annotations

import json
import math
import os
import pathlib
import signal
import statistics
import subprocess
import tempfile
import time

from test_flowcausal_v2_lifecycle import (
    FLOWD,
    PIN_DIR,
    SCX_STATE,
    SCHEDULER,
    privileged,
    stable_id,
    stop_process,
    wait_until,
)
from test_flowcausal_v2_resources import write_privileged


WORKLOAD = pathlib.Path("/tmp/flowcausal_v2_latency_workload")


def percentile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def flowd_once(
    *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return privileged(
        str(FLOWD), "--pin-dir", str(PIN_DIR), *arguments, check=check
    )


def run_once(output: pathlib.Path, *, interactive: bool, run_index: int) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    cgroup = pathlib.Path("/sys/fs/cgroup") / (
        f"flowcausal-v2-latency-{os.getpid()}-{run_index}"
    )
    session = f"flowcausal-v2:latency:{os.getpid()}:{run_index}"
    session_id = stable_id(session)
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    background: subprocess.Popen[str] | None = None
    foreground: subprocess.Popen[str] | None = None

    try:
        scheduler_args = [
            "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
            "--no-progress", "--no-background", "--no-locality", "--quiet",
        ]
        if interactive:
            scheduler_args.append("--interactive")
        else:
            scheduler_args.append("--no-interactive")
        scheduler = subprocess.Popen(
            scheduler_args,
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
        privileged("mkdir", str(cgroup))
        background = subprocess.Popen(
            [
                "taskset", "-c", "0-7", "bash", "-c",
                "read -r start; exec stress-ng --cpu 16 --timeout 12s --quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
        foreground = subprocess.Popen(
            ["taskset", "-c", "0-7", str(WORKLOAD), "45"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        write_privileged(cgroup / "cgroup.procs", f"{background.pid}\n")
        write_privileged(cgroup / "cgroup.procs", f"{foreground.pid}\n")
        flowd_once(
            "--operation", "session-open", "--session", session,
            "--cgroup", str(cgroup), "--root-pid", str(foreground.pid), "--once",
        )
        flowd_once(
            "--operation", "tool-begin", "--session", session,
            "--cgroup", str(cgroup), "--tool-id", "100", "--generation", "1",
            "--sequence", "1", "--execution-mode", "asynchronous",
            "--stage", "PACKAGE_BACKGROUND", "--root-pid", str(background.pid),
            "--once",
        )
        flowd_once(
            "--operation", "tool-end", "--session", session,
            "--cgroup", str(cgroup), "--tool-id", "100", "--generation", "1",
            "--sequence", "2", "--execution-mode", "asynchronous",
            "--stage", "PACKAGE_BACKGROUND", "--once",
        )
        flowd_once(
            "--operation", "tool-begin", "--session", session,
            "--cgroup", str(cgroup), "--tool-id", "200", "--generation", "3",
            "--sequence", "3", "--execution-mode", "synchronous",
            "--stage", "TEST", "--root-pid", str(foreground.pid), "--once",
        )
        assert background.stdin is not None and foreground.stdin is not None
        background.stdin.write("start\n")
        background.stdin.close()
        background.stdin = None
        time.sleep(0.15)
        foreground.stdin.write("start\n")
        foreground.stdin.close()
        foreground.stdin = None
        stdout, stderr = foreground.communicate(timeout=20)
        if foreground.returncode:
            raise RuntimeError(f"短交互任务失败：{foreground.returncode}\n{stderr}")
        values = [int(line) for line in stdout.splitlines() if line.strip().isdigit()]
        if len(values) != 45:
            raise RuntimeError(f"短交互样本数不符：{len(values)}\n{stderr}")
        # 前五轮用于建立进程和映射状态，不进入延迟分布。
        values = values[5:]
        stats_run = flowd_once(
            "--operation", "stats", "--session-id", str(session_id), "--once"
        )
        stats_lines = [line for line in stats_run.stdout.splitlines() if line.startswith("{")]
        return {
            "interactive": interactive,
            "samples": len(values),
            "p50_us": percentile(values, 0.50) / 1000,
            "p95_us": percentile(values, 0.95) / 1000,
            "p99_us": percentile(values, 0.99) / 1000,
            "mean_us": statistics.fmean(values) / 1000,
            "maximum_us": max(values) / 1000,
            "scheduler_stats": json.loads(stats_lines[-1]) if stats_lines else {},
        }
    finally:
        if foreground is not None and foreground.poll() is None:
            os.killpg(foreground.pid, signal.SIGKILL)
            foreground.wait(timeout=5)
        if background is not None and background.poll() is None:
            os.killpg(background.pid, signal.SIGKILL)
            background.wait(timeout=5)
        flowd_once(
            "--operation", "session-close", "--session", session,
            "--cgroup", str(cgroup), "--once", check=False,
        ) if cgroup.exists() and scheduler is not None else None
        privileged("rmdir", str(cgroup), check=False)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-latency-"))
    order = [False, True, True, False, False, True]
    rows = [
        run_once(output / f"run-{index + 1}", interactive=value, run_index=index + 1)
        for index, value in enumerate(order)
    ]
    grouped = {
        value: [row for row in rows if bool(row["interactive"]) is value]
        for value in (False, True)
    }
    summary: dict[str, object] = {}
    for value, name in ((False, "关闭短交互"), (True, "启用短交互")):
        summary[name] = {
            metric: statistics.median(float(row[metric]) for row in grouped[value])
            for metric in ("p50_us", "p95_us", "p99_us", "mean_us", "maximum_us")
        }
    before = float(summary["关闭短交互"]["p95_us"])
    after = float(summary["启用短交互"]["p95_us"])
    improvement = (before - after) / before * 100 if before else 0.0
    interactive_runs = sum(
        int(row["scheduler_stats"].get("interactive_runs", 0))
        for row in grouped[True]
    )
    checks = {
        "短交互识别并实际运行": interactive_runs > 0,
        "关闭机制时没有短交互运行": all(
            int(row["scheduler_stats"].get("interactive_runs", 0)) == 0
            for row in grouped[False]
        ),
        "短交互没有恶化百分之九十五延迟": after <= before * 1.05,
    }
    result = {
        "schema": "flowcausal.v2.interactive-latency.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "cpu_set": "0-7",
        "runs": rows,
        "summary": summary,
        "p95_improvement_percent": improvement,
        "checks": checks,
    }
    (output / "结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

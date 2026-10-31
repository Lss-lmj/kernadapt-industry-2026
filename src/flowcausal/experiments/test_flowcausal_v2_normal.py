#!/usr/bin/env python3
"""验证关键推进和短交互存在时，普通计算任务仍保持常规公平。"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
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


NORMAL_WORKLOAD = pathlib.Path("/tmp/flowcausal_v2_normal_workload")
LATENCY_WORKLOAD = pathlib.Path("/tmp/flowcausal_v2_latency_workload")
NORMAL_PATTERN = re.compile(
    r"elapsed_ns=(\d+) gap_p95_ns=(\d+) gap_p99_ns=(\d+) "
    r"maximum_gap_ns=(\d+) samples=(\d+)"
)


def flowd_once(
    *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return privileged(
        str(FLOWD), "--pin-dir", str(PIN_DIR), *arguments, check=check
    )


def make_normal() -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["taskset", "-c", "0-7", str(NORMAL_WORKLOAD), "500"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def parse_normal(process: subprocess.Popen[str]) -> dict[str, float]:
    stdout, stderr = process.communicate(timeout=20)
    if process.returncode:
        raise RuntimeError(f"普通任务失败：{process.returncode}\n{stderr}")
    match = NORMAL_PATTERN.search(stdout)
    if not match:
        raise RuntimeError(f"普通任务统计不完整：{stdout}\n{stderr}")
    return {
        "elapsed_ms": int(match.group(1)) / 1_000_000,
        "gap_p95_us": int(match.group(2)) / 1000,
        "gap_p99_us": int(match.group(3)) / 1000,
        "maximum_gap_ms": int(match.group(4)) / 1_000_000,
        "samples": int(match.group(5)),
    }


def percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)] / 1000


def run_once(output: pathlib.Path, *, complete: bool, run_index: int) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    cgroup = pathlib.Path("/sys/fs/cgroup") / (
        f"flowcausal-v2-normal-{os.getpid()}-{run_index}"
    )
    session = f"flowcausal-v2:normal:{os.getpid()}:{run_index}"
    session_id = stable_id(session)
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    normal_processes: list[subprocess.Popen[str]] = []
    latency: subprocess.Popen[str] | None = None

    try:
        scheduler_args = [
            "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7", "--quiet"
        ]
        if not complete:
            scheduler_args.append("--grouping-only")
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
        normal_processes = [make_normal() for _ in range(8)]
        latency = subprocess.Popen(
            ["taskset", "-c", "0-7", str(LATENCY_WORKLOAD), "45"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        for process in [*normal_processes, latency]:
            write_privileged(cgroup / "cgroup.procs", f"{process.pid}\n")
        flowd_once(
            "--operation", "session-open", "--session", session,
            "--cgroup", str(cgroup), "--root-pid", str(latency.pid), "--once",
        )
        first = normal_processes[0]
        flowd_once(
            "--operation", "tool-begin", "--session", session,
            "--cgroup", str(cgroup), "--tool-id", "200",
            "--generation", "1", "--sequence", "1",
            "--execution-mode", "synchronous", "--stage", "UNKNOWN",
            "--root-pid", str(first.pid), "--once",
        )
        # 同一工具可以由多个已知执行根组成。逐个更新范围根不会改变
        # 工具代次，其余根因此仍属于当前工具，而不是被当作旧工具。
        for sequence, process in enumerate(normal_processes[1:], start=2):
            flowd_once(
                "--operation", "tool-update", "--session", session,
                "--cgroup", str(cgroup), "--tool-id", "200",
                "--generation", "1", "--sequence", str(sequence),
                "--execution-mode", "synchronous", "--stage", "UNKNOWN",
                "--root-pid", str(process.pid), "--once",
            )
        flowd_once(
            "--operation", "tool-update", "--session", session,
            "--cgroup", str(cgroup), "--tool-id", "200",
            "--generation", "1", "--sequence", "9",
            "--execution-mode", "synchronous", "--stage", "UNKNOWN",
            "--root-pid", str(latency.pid), "--once",
        )
        for process in normal_processes:
            assert process.stdin is not None
            process.stdin.write("start\n")
            process.stdin.close()
            process.stdin = None
        time.sleep(0.05)
        assert latency.stdin is not None
        latency.stdin.write("start\n")
        latency.stdin.close()
        latency.stdin = None
        latency_stdout, latency_stderr = latency.communicate(timeout=20)
        if latency.returncode:
            raise RuntimeError(
                f"短交互任务失败：{latency.returncode}\n{latency_stderr}"
            )
        latency_values = [
            int(line) for line in latency_stdout.splitlines() if line.strip().isdigit()
        ][5:]
        if len(latency_values) != 40:
            raise RuntimeError(f"短交互样本数不符：{len(latency_values)}")
        normal_rows = [parse_normal(process) for process in normal_processes]
        elapsed = [float(row["elapsed_ms"]) for row in normal_rows]
        stats_run = flowd_once(
            "--operation", "stats", "--session-id", str(session_id), "--once"
        )
        stats_lines = [line for line in stats_run.stdout.splitlines() if line.startswith("{")]
        return {
            "complete": complete,
            "ordinary": normal_rows,
            "ordinary_elapsed_median_ms": statistics.median(elapsed),
            "ordinary_elapsed_spread_ms": max(elapsed) - min(elapsed),
            "ordinary_gap_p95_us": statistics.median(
                float(row["gap_p95_us"]) for row in normal_rows
            ),
            "ordinary_gap_p99_us": statistics.median(
                float(row["gap_p99_us"]) for row in normal_rows
            ),
            "interactive_p95_us": percentile(latency_values, 0.95),
            "scheduler_stats": json.loads(stats_lines[-1]) if stats_lines else {},
        }
    finally:
        for process in [latency, *normal_processes]:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if cgroup.exists() and scheduler is not None:
            flowd_once(
                "--operation", "session-close", "--session", session,
                "--cgroup", str(cgroup), "--once", check=False,
            )
        privileged("rmdir", str(cgroup), check=False)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-normal-"))
    order = [False, True, True, False, False, True]
    rows = [
        run_once(output / f"run-{index + 1}", complete=value, run_index=index + 1)
        for index, value in enumerate(order)
    ]
    grouped = {
        value: [row for row in rows if bool(row["complete"]) is value]
        for value in (False, True)
    }
    metrics = (
        "ordinary_elapsed_median_ms", "ordinary_elapsed_spread_ms",
        "ordinary_gap_p95_us", "ordinary_gap_p99_us", "interactive_p95_us",
    )
    summary = {
        name: {
            metric: statistics.median(float(row[metric]) for row in grouped[value])
            for metric in metrics
        }
        for value, name in ((False, "仅工作流分组"), (True, "完整第二版"))
    }
    baseline = summary["仅工作流分组"]
    complete = summary["完整第二版"]
    checks = {
        "普通任务中位完成时间没有明显增加": (
            complete["ordinary_elapsed_median_ms"]
            <= baseline["ordinary_elapsed_median_ms"] * 1.10
        ),
        "普通任务百分之九十五间隔没有明显增加": (
            complete["ordinary_gap_p95_us"] <= baseline["ordinary_gap_p95_us"] * 1.15
        ),
        "普通任务百分之九十九间隔没有明显增加": (
            complete["ordinary_gap_p99_us"] <= baseline["ordinary_gap_p99_us"] * 1.15
        ),
        "关键推进或短交互实际发生": sum(
            int(row["scheduler_stats"].get("interactive_runs", 0))
            + int(row["scheduler_stats"].get("return_promotions", 0))
            for row in grouped[True]
        ) > 0,
        "普通任务没有被当作旧工具延后": all(
            int(row["scheduler_stats"].get("stale_delays", 0)) == 0
            for row in grouped[True]
        ),
    }
    result = {
        "schema": "flowcausal.v2.ordinary-fairness.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "cpu_set": "0-7",
        "runs": rows,
        "summary": summary,
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

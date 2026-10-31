#!/usr/bin/env python3
"""验证旧异步工具让路，同时保持后台任务持续取得运行机会。"""

from __future__ import annotations

import json
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


WORKLOAD = pathlib.Path("/tmp/flowcausal_v2_cpu_workload")
RESULT_PATTERN = re.compile(r"elapsed_ns=(\d+) maximum_gap_ns=(\d+)")


def flowd_once(
    *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return privileged(
        str(FLOWD), "--pin-dir", str(PIN_DIR), *arguments, check=check
    )


def make_workload(children: int) -> subprocess.Popen[str]:
    return subprocess.Popen(
        ["taskset", "-c", "0-7", str(WORKLOAD), str(children), "500"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def collect_workload(process: subprocess.Popen[str], name: str) -> dict[str, float]:
    stdout, stderr = process.communicate(timeout=20)
    if process.returncode:
        raise RuntimeError(f"{name}执行失败：{process.returncode}\n{stderr}")
    match = RESULT_PATTERN.search(stdout)
    if not match:
        raise RuntimeError(f"{name}没有输出完整统计：{stdout}\n{stderr}")
    return {
        "elapsed_ms": int(match.group(1)) / 1_000_000,
        "maximum_gap_ms": int(match.group(2)) / 1_000_000,
    }


def run_once(output: pathlib.Path, *, enabled: bool, run_index: int) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    cgroup = pathlib.Path("/sys/fs/cgroup") / (
        f"flowcausal-v2-background-{os.getpid()}-{run_index}"
    )
    session = f"flowcausal-v2:background:{os.getpid()}:{run_index}"
    session_id = stable_id(session)
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    background: subprocess.Popen[str] | None = None
    foreground: subprocess.Popen[str] | None = None

    try:
        scheduler_args = [
            "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
            "--no-progress", "--no-interactive", "--no-locality",
            "--no-parallel-width", "--quiet",
        ]
        if not enabled:
            scheduler_args.append("--no-background")
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
        # 单个旧任务与宽度明显更大的当前工具竞争，确保旧任务确实
        # 可能达到最长延后时间；对称的 8 对 8 负载会持续轮转，不能
        # 验证“达到等待上限后恢复运行”这一动作。
        background = make_workload(1)
        foreground = make_workload(16)
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
        assert background.stdin is not None
        whole_begin = time.monotonic_ns()
        background.stdin.write("start\n")
        background.stdin.close()
        background.stdin = None
        time.sleep(0.15)
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
        assert foreground.stdin is not None
        foreground.stdin.write("start\n")
        foreground.stdin.close()
        foreground.stdin = None
        foreground_result = collect_workload(foreground, "当前同步工具")
        background_result = collect_workload(background, "旧异步工具")
        whole_ms = (time.monotonic_ns() - whole_begin) / 1_000_000
        stats_run = flowd_once(
            "--operation", "stats", "--session-id", str(session_id), "--once"
        )
        stats_lines = [line for line in stats_run.stdout.splitlines() if line.startswith("{")]
        stats = json.loads(stats_lines[-1]) if stats_lines else {}
        return {
            "background_enabled": enabled,
            "foreground": foreground_result,
            "background": background_result,
            "whole_ms": whole_ms,
            "scheduler_stats": stats,
        }
    finally:
        for process in (foreground, background):
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


def median(rows: list[dict[str, object]], path: tuple[str, ...]) -> float:
    values: list[float] = []
    for row in rows:
        value: object = row
        for part in path:
            value = value[part]  # type: ignore[index]
        values.append(float(value))
    return statistics.median(values)


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-background-"))
    order = [False, True, True, False, False, True]
    rows = [
        run_once(output / f"run-{index + 1}", enabled=value, run_index=index + 1)
        for index, value in enumerate(order)
    ]
    grouped = {
        value: [row for row in rows if bool(row["background_enabled"]) is value]
        for value in (False, True)
    }
    summary: dict[str, object] = {}
    for value, name in ((False, "关闭后台处理"), (True, "启用后台处理")):
        summary[name] = {
            "当前工具完成时间毫秒": median(grouped[value], ("foreground", "elapsed_ms")),
            "旧工具完成时间毫秒": median(grouped[value], ("background", "elapsed_ms")),
            "旧工具最长未运行时间毫秒": median(
                grouped[value], ("background", "maximum_gap_ms")
            ),
            "全部完成时间毫秒": median(grouped[value], ("whole_ms",)),
        }
    before = float(summary["关闭后台处理"]["当前工具完成时间毫秒"])
    after = float(summary["启用后台处理"]["当前工具完成时间毫秒"])
    improvement = (before - after) / before * 100 if before else 0.0
    enabled_stats = [row["scheduler_stats"] for row in grouped[True]]
    checks = {
        "识别到旧任务并延后": sum(
            int(stats.get("stale_delays", 0)) for stats in enabled_stats
        ) > 0,
        "旧任务超过最长等待后恢复运行": sum(
            int(stats.get("background_rescues", 0)) for stats in enabled_stats
        ) > 0,
        "关闭机制时没有后台处理记录": all(
            int(row["scheduler_stats"].get("stale_delays", 0)) == 0
            and int(row["scheduler_stats"].get("background_rescues", 0)) == 0
            for row in grouped[False]
        ),
        "当前工具完成时间没有恶化": after <= before * 1.05,
        "旧任务均能完成": all(float(row["background"]["elapsed_ms"]) > 0 for row in rows),
    }
    result = {
        "schema": "flowcausal.v2.background-progress.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "cpu_set": "0-7",
        "runs": rows,
        "summary": summary,
        "foreground_improvement_percent": improvement,
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

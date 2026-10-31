#!/usr/bin/env python3
"""验证第二版动态并行度确实约束处理器占用，并保持单会话扩展能力。"""

from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
import tempfile
import time

from test_flowcausal_v2_lifecycle import (
    FLOWD,
    HELPER,
    PIN_DIR,
    SCX_STATE,
    SCHEDULER,
    privileged,
    stable_id,
    stop_process,
    wait_until,
)


def privileged_write(path: pathlib.Path, value: str) -> None:
    result = subprocess.run(
        ["sudo", "-n", "tee", str(path)],
        input=value,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"无法写入 {path}：{result.stderr}")


def flowd_once(*arguments: str) -> subprocess.CompletedProcess[str]:
    return privileged(str(FLOWD), "--pin-dir", str(PIN_DIR), *arguments)


def make_worker(cpu_workers: int = 32) -> subprocess.Popen[bytes]:
    workload = (
        f"stress-ng --cpu {cpu_workers} --timeout 2s --quiet"
        if cpu_workers
        else "sleep 2"
    )
    return subprocess.Popen(
        [
            "taskset",
            "-c",
            "0-7",
            "bash",
            "-c",
            f"read -r start; exec {workload}",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def read_runtime(session_id: int) -> dict[str, int]:
    values = privileged(
        str(HELPER), "session-runtime", str(PIN_DIR), str(session_id)
    ).stdout.strip().split()
    if len(values) != 4:
        raise RuntimeError(f"会话并行统计不完整：{values}")
    return {
        "session_id": session_id,
        "runnable_tasks": int(values[0]),
        "running_tasks": int(values[1]),
        "peak_running_tasks": int(values[2]),
        "current_width": int(values[3]),
    }


def run_once(
    output: pathlib.Path, *, width_enabled: bool, session_count: int = 2,
    worker_cpus: list[int] | None = None,
    scheduler_extra_args: list[str] | None = None,
    register_completed_session: bool = False,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    cgroup_root = pathlib.Path("/sys/fs/cgroup") / f"flowcausal-v2-width-{os.getpid()}"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    workers: list[subprocess.Popen[bytes]] = []
    sessions: list[tuple[str, pathlib.Path, int]] = []
    try:
        scheduler_args = [
            "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7", "--quiet"
        ]
        if not width_enabled:
            scheduler_args.append("--no-parallel-width")
        scheduler_args.extend(scheduler_extra_args or [])
        scheduler = subprocess.Popen(
            scheduler_args,
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled"
            and privileged("test", "-e", str(PIN_DIR / "session_runtime"), check=False).returncode == 0,
            timeout=15,
            description="等待第二版调度器启动超时",
        )
        privileged("mkdir", str(cgroup_root))
        privileged_write(cgroup_root / "cgroup.subtree_control", "+cpu\n")
        for index in range(session_count):
            cgroup = cgroup_root / f"session-{index}"
            privileged("mkdir", str(cgroup))
            privileged_write(cgroup / "cpu.weight", "100\n")
            cpu_workers = worker_cpus[index] if worker_cpus else 32
            worker = make_worker(cpu_workers)
            workers.append(worker)
            privileged_write(cgroup / "cgroup.procs", f"{worker.pid}\n")
            session = f"flowcausal-v2:width:{os.getpid()}:{index}"
            session_id = stable_id(session)
            flowd_once(
                "--operation", "session-open", "--session", session,
                "--cgroup", str(cgroup), "--root-pid", str(worker.pid),
                "--weight", "100", "--once",
            )
            flowd_once(
                "--operation", "tool-begin", "--session", session,
                "--cgroup", str(cgroup), "--tool-id", "1",
                "--generation", "1", "--sequence", "1",
                "--execution-mode", "synchronous", "--stage", "BUILD",
                "--root-pid", str(worker.pid), "--once",
            )
            sessions.append((session, cgroup, session_id))
        if register_completed_session:
            completed_cgroup = cgroup_root / "completed-session"
            privileged("mkdir", str(completed_cgroup))
            completed_worker = subprocess.Popen(
                ["sleep", "5"], start_new_session=True
            )
            completed_session = f"flowcausal-v2:width:{os.getpid()}:completed"
            try:
                privileged_write(
                    completed_cgroup / "cgroup.procs", f"{completed_worker.pid}\n"
                )
                flowd_once(
                    "--operation", "session-open", "--session", completed_session,
                    "--cgroup", str(completed_cgroup), "--root-pid",
                    str(completed_worker.pid), "--weight", "100", "--once",
                )
                flowd_once(
                    "--operation", "session-close", "--session", completed_session,
                    "--cgroup", str(completed_cgroup), "--once",
                )
            finally:
                stop_process(completed_worker, signal.SIGKILL)
                privileged("rmdir", str(completed_cgroup), check=False)
        for worker in workers:
            if worker.poll() is not None:
                raise RuntimeError(f"并行度测试任务提前退出：{worker.returncode}")
            assert worker.stdin is not None
            worker.stdin.write(b"start\n")
            worker.stdin.close()
        concurrent_samples: list[list[dict[str, int]]] = []
        sample_deadline = time.monotonic() + 1.5
        while time.monotonic() < sample_deadline and all(
            worker.poll() is None for worker in workers
        ):
            sample = [read_runtime(session_id) for _, _, session_id in sessions]
            if all(row["runnable_tasks"] > 0 for row in sample):
                concurrent_samples.append(sample)
            time.sleep(0.05)
        for worker in workers:
            if worker.wait(timeout=12):
                raise RuntimeError("并行度测试任务异常退出")
        runtime_rows = []
        stats_rows = []
        for session, cgroup, session_id in sessions:
            runtime_rows.append(read_runtime(session_id))
            stats_run = flowd_once(
                "--operation", "stats", "--session-id", str(session_id), "--once"
            )
            stats_rows.append(json.loads(stats_run.stdout.splitlines()[-1]))
            flowd_once(
                "--operation", "tool-end", "--session", session,
                "--cgroup", str(cgroup), "--tool-id", "1",
                "--generation", "1", "--sequence", "2",
                "--execution-mode", "synchronous", "--stage", "BUILD", "--once",
            )
            flowd_once(
                "--operation", "session-close", "--session", session,
                "--cgroup", str(cgroup), "--once",
            )
        return {
            "width_enabled": width_enabled,
            "worker_cpus": worker_cpus or [32] * session_count,
            "scheduler_extra_args": scheduler_extra_args or [],
            "sessions": runtime_rows,
            "concurrent_samples": concurrent_samples,
            "width_limits": sum(int(row["parallel_width_limits"]) for row in stats_rows),
            "width_borrows": sum(int(row["parallel_width_borrows"]) for row in stats_rows),
        }
    finally:
        for worker in workers:
            if worker.poll() is None:
                try:
                    os.killpg(worker.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for session, cgroup, _ in sessions:
            privileged(
                str(FLOWD), "--pin-dir", str(PIN_DIR),
                "--operation", "session-close", "--session", session,
                "--cgroup", str(cgroup), "--once", check=False,
            )
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        for _, cgroup, _ in reversed(sessions):
            privileged("rmdir", str(cgroup), check=False)
        privileged("rmdir", str(cgroup_root), check=False)
        scheduler_log.close()


def main() -> int:
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    if not any((pathlib.Path(path) / "stress-ng").exists() for path in os.environ["PATH"].split(os.pathsep)):
        raise SystemExit("缺少 stress-ng")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-width-"))
    enabled = run_once(output / "enabled", width_enabled=True)
    disabled = run_once(output / "disabled", width_enabled=False)
    single = run_once(output / "single-session", width_enabled=True, session_count=1)
    demand = run_once(
        output / "demand-aware",
        width_enabled=True,
        worker_cpus=[32, 2],
        scheduler_extra_args=["--demand-aware-borrow"],
    )
    logarithmic = run_once(
        output / "logarithmic-demand",
        width_enabled=True,
        worker_cpus=[32, 2],
        scheduler_extra_args=["--logarithmic-demand-share"],
    )
    balanced = run_once(
        output / "balanced-demand",
        width_enabled=True,
        worker_cpus=[32, 2],
        scheduler_extra_args=["--balanced-demand-share"],
        register_completed_session=True,
    )
    checks = {
        "两会话同时可运行时目标处理器数为四": bool(
            enabled["concurrent_samples"]
        ) and any(
            all(int(row["current_width"]) == 4 for row in sample)
            for sample in enabled["concurrent_samples"]
        ),
        "动态并行度实际触发": int(enabled["width_limits"]) > 0,
        "关闭后不触发": int(disabled["width_limits"]) == 0,
        "运行计数不超过实际处理器数": all(
            int(row["peak_running_tasks"]) <= 8
            for group in (enabled["sessions"], disabled["sessions"], single["sessions"])
            for row in group
        ),
        "单会话使用全部处理器": all(
            int(row["current_width"]) == 8
            and int(row["peak_running_tasks"]) >= 7
            for row in single["sessions"]
        ),
        "按可运行任务数区分宽窄工作流": bool(demand["concurrent_samples"])
        and any(
            int(sample[0]["current_width"]) >= 6
            and int(sample[1]["current_width"]) <= 2
            for sample in demand["concurrent_samples"]
        ),
        "按需借用路径实际触发": int(demand["width_borrows"]) > 0,
        "按可运行进程数的对数同时改变会话处理器时间和并行度": bool(
            logarithmic["concurrent_samples"]
        ) and any(
            int(sample[0]["current_width"]) >= 5
            and int(sample[1]["current_width"]) <= 2
            for sample in logarithmic["concurrent_samples"]
        ),
        "保留平均分配后仍区分并行程度不同的工作流": bool(
            balanced["concurrent_samples"]
        ) and any(
            4 <= int(sample[0]["current_width"]) <= 5
            and int(sample[1]["current_width"]) <= 2
            for sample in balanced["concurrent_samples"]
        ),
        "平衡分配仍允许使用空余处理器": int(balanced["width_borrows"]) > 0,
    }
    result = {
        "schema": "flowcausal.v2.parallel-width-test.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "cpu_set": "0-7",
        "enabled": enabled,
        "disabled": disabled,
        "single_session": single,
        "demand_aware": demand,
        "logarithmic_demand": logarithmic,
        "balanced_demand": balanced,
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

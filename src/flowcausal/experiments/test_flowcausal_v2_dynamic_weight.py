#!/usr/bin/env python3
"""验证 FlowCausal v2 按同步工具等待动态调整工作流权重。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import signal
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import TextIO

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
from test_flowcausal_v2_resources import cpu_usage_us, write_privileged


FLOWEVENT = pathlib.Path("/tmp/flowevent_v2")


@dataclass
class Session:
    index: int
    name: str
    session_id: int
    cgroup: pathlib.Path
    holder: subprocess.Popen[bytes]
    flowd: subprocess.Popen[str] | None
    flowd_log: TextIO | None
    socket_path: pathlib.Path
    tool: subprocess.Popen[bytes] | None = None


def stop_plain(process: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    privileged("kill", "-TERM", str(process.pid), check=False)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        privileged("kill", "-KILL", str(process.pid), check=False)
        process.wait(timeout=8)


def create_session(
    root: pathlib.Path,
    output: pathlib.Path,
    index: int,
    label: str,
    *,
    register_with_flowcausal: bool,
) -> Session:
    name = f"flowcausal-v2:dynamic-weight:{os.getpid()}:{label}:{index}"
    cgroup = root / f"session-{index}"
    privileged("mkdir", str(cgroup))
    write_privileged(cgroup / "cpu.weight", "100\n")
    holder = subprocess.Popen(
        ["sleep", "300"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    write_privileged(cgroup / "cgroup.procs", f"{holder.pid}\n")
    session_output = output / f"session-{index}"
    session_output.mkdir(parents=True)
    socket_path = pathlib.Path(
        f"/tmp/fcv2-dw-{os.getpid()}-{label[0]}-{index}.sock"
    )
    socket_path.unlink(missing_ok=True)
    if not register_with_flowcausal:
        return Session(
            index,
            name,
            stable_id(name),
            cgroup,
            holder,
            None,
            None,
            socket_path,
        )
    try:
        flowd, flowd_log = start_flowd(
            session=name,
            root_pid=holder.pid,
            socket_path=socket_path,
            log_path=session_output / "flowd.log",
        )
    except Exception:
        stop_plain(holder)
        socket_path.unlink(missing_ok=True)
        privileged("rmdir", str(cgroup), check=False)
        raise
    return Session(
        index, name, stable_id(name), cgroup, holder, flowd, flowd_log, socket_path
    )


def create_tool(
    session: Session, *, synchronous: bool, operations: int, linux: bool
) -> None:
    mode = "synchronous" if synchronous else "asynchronous"
    workload = [
        "stress-ng",
        "--cpu",
        "8",
        "--cpu-method",
        "sqrt",
        "--cpu-ops",
        str(operations),
        "--quiet",
    ]
    if linux:
        # 保留一个阻塞等待工作进程的命令入口，使进程结构与 C 事件程序一致。
        command = [
            "bash",
            "-c",
            "read -r start; taskset -c 0-7 \"$@\" & child=$!; wait \"$child\"",
            "flowcausal-v2-dynamic-weight-linux",
            *workload,
        ]
    else:
        command = [
            "bash",
            "-c",
            "read -r start; exec taskset -c 0-7 \"$@\"",
            "flowcausal-v2-dynamic-weight",
            str(FLOWEVENT),
            "--socket",
            str(session.socket_path),
            "--session-id",
            str(session.session_id),
            "--tool-id",
            "1",
            "--generation",
            "1",
            "--sequence",
            "1",
            "--execution-mode",
            mode,
            "--stage",
            "TEST",
            "--",
            *workload,
        ]
    tool = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    write_privileged(session.cgroup / "cgroup.procs", f"{tool.pid}\n")
    session.tool = tool


def cleanup_sessions(sessions: list[Session]) -> None:
    for session in reversed(sessions):
        if session.tool is not None:
            stop_plain(session.tool)
        stop_plain(session.holder)
        if session.flowd is not None:
            stop_plain(session.flowd)
        if session.flowd_log is not None:
            session.flowd_log.close()
        session.socket_path.unlink(missing_ok=True)
        privileged("rmdir", str(session.cgroup), check=False)


def run_once(output: pathlib.Path, *, mode: str, operations: int) -> dict[str, object]:
    adaptive = mode in {"dynamic", "automatic"}
    automatic = mode == "automatic"
    linux = mode == "linux"
    label = mode
    run_output = output
    # 临时目录和命令行指定的结果目录都可能在进入公共运行函数前存在。
    run_output.mkdir(parents=True, exist_ok=True)
    scheduler_log = (run_output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    sessions: list[Session] = []
    cgroup_root = pathlib.Path("/sys/fs/cgroup") / (
        f"flowcausal-v2-dynamic-weight-{os.getpid()}-{label}"
    )
    try:
        if not linux:
            scheduler_command = [
                "sudo",
                "-n",
                str(SCHEDULER),
                "--managed-cpus",
                "0-7",
                "--quiet",
            ]
            if adaptive:
                scheduler_command.append(
                    "--automatic-session-weight"
                    if automatic
                    else "--adaptive-session-weight"
                )
            scheduler = subprocess.Popen(
                scheduler_command,
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
                description="等待 FlowCausal v2 启动超时",
            )
        privileged("mkdir", str(cgroup_root))
        write_privileged(cgroup_root / "cgroup.subtree_control", "+cpu +cpuset\n")
        sessions = [
            create_session(
                cgroup_root,
                run_output,
                index,
                label,
                register_with_flowcausal=not linux,
            )
            for index in range(4)
        ]
        for index, session in enumerate(sessions):
            create_tool(
                session,
                # 自动功能核对故意把全部工具标成异步。此时仍能提高权重，
                # 才能证明调度器没有依赖工具执行方式标记。
                synchronous=(index == 0 and not automatic),
                operations=operations,
                linux=linux,
            )

        before = [cpu_usage_us(session.cgroup) for session in sessions]
        started = time.monotonic()
        for session in sessions:
            assert session.tool is not None and session.tool.stdin is not None
            session.tool.stdin.write(b"start\n")
            session.tool.stdin.close()

        completion: list[float | None] = [None] * len(sessions)
        returncodes: list[int | None] = [None] * len(sessions)
        deadline = started + 60
        while any(value is None for value in returncodes):
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("固定工作量在 60 秒内没有全部结束")
            for index, session in enumerate(sessions):
                if returncodes[index] is not None:
                    continue
                assert session.tool is not None
                result = session.tool.poll()
                if result is not None:
                    returncodes[index] = result
                    completion[index] = now - started
            time.sleep(0.001)
        completed = [float(value) for value in completion if value is not None]
        foreground_elapsed = completed[0]
        makespan = max(completed)
        after = [cpu_usage_us(session.cgroup) for session in sessions]
        usage = [end - begin for begin, end in zip(before, after)]
        stats = (
            [read_session_stats(session.session_id) for session in sessions]
            if not linux
            else [{} for _ in sessions]
        )
        if any(int(value) for value in returncodes if value is not None):
            errors = []
            for session in sessions:
                if session.tool is not None and session.tool.stderr is not None:
                    errors.append(session.tool.stderr.read().decode("utf-8", errors="replace"))
            raise RuntimeError(f"固定工作量异常结束：{returncodes}\n{''.join(errors)}")
        result = {
            "mode": label,
            "adaptive_session_weight": adaptive,
            "cpu_set": "0-7",
            "session_count": 4,
            "foreground_mode": "asynchronous" if automatic else "synchronous",
            "background_mode": "asynchronous",
            "workers_per_session": 8,
            "cpu_method": "sqrt",
            "cpu_operations_per_session": operations,
            "foreground_completion_sec": foreground_elapsed,
            "workflow_completion_sec": completed,
            "mean_workflow_completion_sec": statistics.mean(completed),
            "makespan_sec": makespan,
            "cgroup_cpu_usage_us": usage,
            "returncodes": returncodes,
            "session_stats": stats,
        }
        (run_output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return result
    finally:
        cleanup_sessions(sessions)
        privileged("rmdir", str(cgroup_root), check=False)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


def summarize(rows: list[dict[str, object]], trials: int) -> dict[str, object]:
    by_mode = {
        name: [row for row in rows if row["mode"] == name]
        for name in ("linux", "fixed", "dynamic")
    }
    summary: dict[str, object] = {}
    for name, values in by_mode.items():
        foreground = [float(row["foreground_completion_sec"]) for row in values]
        workflow_mean = [float(row["mean_workflow_completion_sec"]) for row in values]
        makespan = [float(row["makespan_sec"]) for row in values]
        total_cpu = [sum(int(value) for value in row["cgroup_cpu_usage_us"]) / 1_000_000 for row in values]
        summary[name] = {
            "trials": len(values),
            "foreground_completion_sec_mean": statistics.mean(foreground),
            "foreground_completion_sec_stdev": statistics.stdev(foreground) if len(foreground) > 1 else 0.0,
            "mean_workflow_completion_sec_mean": statistics.mean(workflow_mean),
            "mean_workflow_completion_sec_stdev": statistics.stdev(workflow_mean) if len(workflow_mean) > 1 else 0.0,
            "makespan_sec_mean": statistics.mean(makespan),
            "makespan_sec_stdev": statistics.stdev(makespan) if len(makespan) > 1 else 0.0,
            "cumulative_cpu_sec_mean": statistics.mean(total_cpu),
        }
    fixed = summary["fixed"]
    dynamic = summary["dynamic"]
    linux = summary["linux"]
    assert isinstance(fixed, dict) and isinstance(dynamic, dict) and isinstance(linux, dict)

    def reduction(reference: dict[str, object], candidate: dict[str, object], key: str) -> float:
        return (
            (float(reference[key]) - float(candidate[key]))
            / float(reference[key])
            * 100
        )

    comparisons = {
        "dynamic_vs_linux": {
            "foreground_completion_reduction_percent": reduction(linux, dynamic, "foreground_completion_sec_mean"),
            "mean_workflow_completion_reduction_percent": reduction(linux, dynamic, "mean_workflow_completion_sec_mean"),
            "makespan_reduction_percent": reduction(linux, dynamic, "makespan_sec_mean"),
            "cumulative_cpu_change_percent": -reduction(linux, dynamic, "cumulative_cpu_sec_mean"),
        },
        "dynamic_vs_fixed": {
            "foreground_completion_reduction_percent": reduction(fixed, dynamic, "foreground_completion_sec_mean"),
            "mean_workflow_completion_reduction_percent": reduction(fixed, dynamic, "mean_workflow_completion_sec_mean"),
            "makespan_reduction_percent": reduction(fixed, dynamic, "makespan_sec_mean"),
            "cumulative_cpu_change_percent": -reduction(fixed, dynamic, "cumulative_cpu_sec_mean"),
        },
    }
    dynamic_rows = by_mode["dynamic"]
    foreground_activated = all(
        int(row["session_stats"][0]["adaptive_weight_activations"]) > 0
        and int(row["session_stats"][0]["adaptive_weight_deactivations"]) > 0
        for row in dynamic_rows
    )
    backgrounds_unchanged = all(
        int(stats["adaptive_weight_activations"]) == 0
        for row in dynamic_rows
        for stats in row["session_stats"][1:]
    )
    checks = {
        "每种设置完成规定轮次": all(len(values) == trials for values in by_mode.values()),
        "所有固定工作量正常结束": all(not any(row["returncodes"]) for row in rows),
        "同步工具发生升权和恢复": foreground_activated,
        "异步后台工具没有升权": backgrounds_unchanged,
    }
    return {
        "schema": "flowcausal.v2.dynamic-weight-test.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "summary": summary,
        "comparisons": comparisons,
        "checks": checks,
        "runs": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 V2 动态工作流权重")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--cpu-operations", type=int, default=20000)
    args = parser.parse_args()
    if args.trials < 1 or args.cpu_operations < 1:
        raise SystemExit("轮数和工作量必须为正数")
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    for binary in (SCHEDULER, FLOWD, FLOWEVENT):
        if not binary.is_file():
            raise SystemExit(f"缺少可执行文件：{binary}")
    privileged("true")
    output = args.output or pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-dynamic-weight-"))
    output.mkdir(parents=True, exist_ok=True)
    order = ["linux", "fixed", "dynamic"] * args.trials
    random.Random(20260913).shuffle(order)
    rows: list[dict[str, object]] = []
    counts = {"linux": 0, "fixed": 0, "dynamic": 0}
    for name in order:
        counts[name] += 1
        run_dir = output / f"{name}-trial-{counts[name]:02d}"
        print(f"运行 {name} 第 {counts[name]} 轮", flush=True)
        row = run_once(
            run_dir, mode=name, operations=args.cpu_operations
        )
        row["trial"] = counts[name]
        rows.append(row)
    result = summarize(rows, args.trials)
    (output / "结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(output)
    print(json.dumps({key: result[key] for key in (
        "status", "summary", "comparisons", "checks")}, ensure_ascii=False))
    return 0 if result["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""在 Linux 或一个 sched_ext 调度器下运行 HarnessReplayBench 1.0 套件。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import subprocess
import sys
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "runners" / "run_suite.py"
SCX_STATE = pathlib.Path("/sys/kernel/sched_ext/state")
SCX_OPS = pathlib.Path("/sys/kernel/sched_ext/root/ops")
BPF_STATS = pathlib.Path("/proc/sys/kernel/bpf_stats_enabled")


def scx_state() -> tuple[str, str]:
    state = SCX_STATE.read_text(encoding="utf-8").strip() if SCX_STATE.exists() else "unavailable"
    ops = SCX_OPS.read_text(encoding="utf-8").strip() if SCX_OPS.exists() else ""
    return state, ops


def wait_state(expected: str, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    current = scx_state()
    while time.monotonic() < deadline:
        current = scx_state()
        if current[0] == expected:
            return current
        time.sleep(0.05)
    raise RuntimeError(f"sched_ext 状态未变为 {expected}，当前为 {current[0]} {current[1]}")


def privileged(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


def set_bpf_stats(value: int) -> None:
    if os.geteuid() == 0:
        BPF_STATS.write_text(f"{value}\n", encoding="utf-8")
        return
    completed = subprocess.run(
        ["sudo", "-n", "tee", str(BPF_STATS)],
        input=f"{value}\n",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"无法设置内核 BPF 统计：{completed.stderr.strip()}")


def bpf_programs() -> dict[int, dict[str, object]]:
    completed = subprocess.run(
        privileged(["bpftool", "-j", "prog", "show"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"无法读取 BPF 程序统计：{completed.stderr.strip()}")
    payload = json.loads(completed.stdout)
    return {
        int(item["id"]): item
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("id"), int)
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 HarnessReplayBench 调度器对比")
    parser.add_argument("--scheduler", required=True, help="结果中记录的方法名称")
    parser.add_argument("--binary", help="sched_ext 调度器可执行文件；Linux 默认调度不填写")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument('--runtime-directory')
    parser.add_argument('--shared-directory')
    parser.add_argument('--container-env', action='append', default=[])
    parser.add_argument(
        "--keep-command-output",
        action="store_true",
        help="成功时保留逐命令输出，用于诊断返回状态不一致",
    )
    parser.add_argument("--no-kernel-capture", action="store_true", help="关闭默认的 C/eBPF 回放统计")
    parser.add_argument("--keep-kernel-capture", action="store_true")
    parser.add_argument(
        "--detailed-tool-progress",
        action="store_true",
        help="保留并细分每次工具执行的处理器时间与进展事件",
    )
    parser.add_argument("--tool-controller", "--flowd-v2", dest="flowd_v2", help="可选的会话工具事件控制程序")
    parser.add_argument(
        "--tool-controller-pin-dir",
        "--flowd-pin-dir",
        dest="flowd_pin_dir",
        default="/sys/fs/bpf/flowcausal_v2",
    )
    parser.add_argument(
        "--tool-controller-arg",
        dest="flowd_v2_args",
        action="append",
        default=[],
        help="传给 C 工具事件控制程序的参数，可重复使用",
    )
    parser.add_argument(
        "--measure-controller-resources",
        action="store_true",
        help="记录 C 控制进程的处理器时间和峰值内存",
    )
    parser.add_argument(
        "--measure-bpf-runtime",
        action="store_true",
        help="记录本轮新装载 BPF 程序的累计执行时间和调用次数",
    )
    parser.add_argument("--ready-timeout", type=float, default=30.0)
    parser.add_argument("scheduler_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.scheduler == "linux" and (args.binary or args.flowd_v2):
        parser.error("linux 表示原生 Linux，不能同时指定 --binary 或 --tool-controller；请使用实际方案名称")
    scheduler: subprocess.Popen[str] | None = None
    log = None
    scheduler_command: list[str] = []
    run_returncode = 1
    state_before = scx_state()
    state_enabled = state_before
    state_at_workload_end = state_before
    scheduler_alive_at_workload_end = None
    scheduler_valid = False
    bpf_stats_before = int(BPF_STATS.read_text(encoding="utf-8").strip() or "0")
    bpf_ids_before: set[int] = set()
    scheduler_bpf_ids: set[int] = set()
    bpf_runtime: dict[str, object] | None = None
    output = pathlib.Path(args.output).resolve()
    log_path = output.parent / f".{output.name}.scheduler.log"
    try:
        if state_before[0] == 'enabled':
            raise RuntimeError(f'已有 sched_ext 调度器正在运行：{state_before[1]}；本轮尚未开始')
        if args.measure_bpf_runtime:
            if not args.binary:
                raise RuntimeError("BPF 运行时间测量需要 sched_ext 调度器")
            set_bpf_stats(1)
            bpf_ids_before = set(bpf_programs())
        if args.binary:
            state, ops = scx_state()
            if state == "enabled":
                raise RuntimeError(f"已有 sched_ext 调度器正在运行：{ops}")
            binary = pathlib.Path(args.binary).resolve()
            if not binary.is_file() or not os.access(binary, os.X_OK):
                raise RuntimeError(f"调度器不可执行：{binary}")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("w", encoding="utf-8")
            command = ["sudo", "-n", str(binary), *([item for item in args.scheduler_args if item != "--"])]
            scheduler_command = command
            scheduler = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            state_enabled = wait_state("enabled", args.ready_timeout)
            if args.measure_bpf_runtime:
                scheduler_bpf_ids = set(bpf_programs()) - bpf_ids_before
        command = [
            sys.executable,
            str(RUNNER),
            "--manifest", str(pathlib.Path(args.manifest).resolve()),
            "--output", str(pathlib.Path(args.output).resolve()),
            "--scheduler-label", args.scheduler,
        ]
        if args.drop_caches:
            command.append("--drop-caches")
        for option, value in [('--runtime-directory', args.runtime_directory),
                              ('--shared-directory', args.shared_directory)]:
            if value:
                command.extend([option, str(pathlib.Path(value).resolve())])
        command.extend('--container-env='+value for value in args.container_env)
        if args.keep_command_output:
            command.append("--keep-command-output")
        if not args.no_kernel_capture:
            command.append("--kernel-capture")
        if args.keep_kernel_capture:
            command.append("--keep-kernel-capture")
        if args.detailed_tool_progress:
            command.extend(["--detailed-tool-progress", "--keep-kernel-capture"])
        if args.flowd_v2:
            command.extend(
                [
                    "--tool-controller", str(pathlib.Path(args.flowd_v2).resolve()),
                    "--tool-controller-pin-dir", args.flowd_pin_dir,
                ]
            )
            command.extend(f"--tool-controller-arg={value}" for value in args.flowd_v2_args)
            if args.measure_controller_resources:
                command.append("--measure-controller-resources")
        run_returncode = subprocess.run(command, check=False).returncode
        state_at_workload_end = scx_state()
        scheduler_alive_at_workload_end = scheduler.poll() is None if scheduler is not None else None
        scheduler_valid = state_at_workload_end == state_enabled and (
            scheduler is None or scheduler_alive_at_workload_end)
        if not scheduler_valid:
            run_returncode = 1
        if args.measure_bpf_runtime:
            current = bpf_programs()
            programs = []
            for program_id in sorted(scheduler_bpf_ids):
                item = current.get(program_id)
                if item is None:
                    continue
                programs.append(
                    {
                        "id": program_id,
                        "name": str(item.get("name") or ""),
                        "type": str(item.get("type") or ""),
                        "run_time_ns": int(item.get("run_time_ns") or 0),
                        "run_count": int(item.get("run_cnt") or 0),
                    }
                )
            bpf_runtime = {
                "accounting_enabled_for_run": True,
                "programs": programs,
                "total_run_time_ns": sum(int(item["run_time_ns"]) for item in programs),
                "total_run_count": sum(int(item["run_count"]) for item in programs),
            }
    finally:
        try:
            if scheduler is not None:
                try:
                    os.killpg(scheduler.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
                try:
                    scheduler.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(scheduler.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    scheduler.wait(timeout=10)
                wait_state("disabled", args.ready_timeout)
        finally:
            if args.measure_bpf_runtime:
                set_bpf_stats(bpf_stats_before)
            if log is not None:
                log.close()
    state_after = scx_state()
    result_path = output / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        result["tested_system"] = {
            "name": args.scheduler,
            "kind": "sched_ext" if args.binary else "native_linux",
            "command": scheduler_command,
            "sched_ext_before": {"state": state_before[0], "ops": state_before[1]},
            "sched_ext_during": {"state": state_enabled[0], "ops": state_enabled[1]},
            'sched_ext_at_workload_end': {'state': state_at_workload_end[0], 'ops': state_at_workload_end[1]},
            'scheduler_alive_at_workload_end': scheduler_alive_at_workload_end,
            'scheduler_valid': scheduler_valid,
            "sched_ext_after": {"state": state_after[0], "ops": state_after[1]},
        }
        if not scheduler_valid:
            result['status'] = 'fail'
            result['scheduler_failure'] = '调度器提前退出或运行期间的 sched_ext 状态改变'
        if bpf_runtime is not None:
            bpf_runtime["accounting_restored_to"] = bpf_stats_before
            result["bpf_runtime"] = bpf_runtime
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if log_path.is_file() and output.is_dir():
        target = output / "scheduler.log"
        if target.exists():
            target.unlink()
        log_path.replace(target)
    return run_returncode


if __name__ == "__main__":
    raise SystemExit(main())

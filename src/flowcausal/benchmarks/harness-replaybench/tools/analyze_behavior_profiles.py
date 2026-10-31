#!/usr/bin/env python3
"""把轨迹语义与 C/eBPF 任务统计合成为可复现的执行行为画像。"""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import pathlib
from collections import Counter, defaultdict
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
STAGE_NAMES = {
    "INSPECT": "检索",
    "EDIT": "编辑",
    "SHELL": "命令执行",
    "BUILD": "构建",
    "TEST": "测试",
    "RESULT": "结果汇总",
    "PACKAGE_BACKGROUND": "关键完成后产物整理",
    "RETRY_PROBE": "重试探测",
}


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def read_jsonl(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"{path}:{number} 不是有效 JSON：{exc}") from exc
            if isinstance(value, dict):
                yield value


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def max_live_tasks(intervals: list[tuple[int, int]]) -> int:
    points: list[tuple[int, int]] = []
    for begin, end in intervals:
        if begin <= 0 or end <= begin:
            continue
        points.append((begin, 1))
        points.append((end, -1))
    live = maximum = 0
    for _, delta in sorted(points, key=lambda item: (item[0], item[1])):
        live += delta
        maximum = max(maximum, live)
    return maximum


def generation_depth(tasks: list[dict[str, Any]]) -> int:
    """计算同一工具代次内可观测的进程父子树深度。"""
    by_pid = {int(item["pid"]): item for item in tasks if int(item.get("pid") or 0)}
    maximum = 0
    for item in tasks:
        depth = 1
        seen = {int(item.get("pid") or 0)}
        parent = int(item.get("parent_pid") or 0)
        while parent in by_pid and parent not in seen:
            seen.add(parent)
            depth += 1
            parent = int(by_pid[parent].get("parent_pid") or 0)
        maximum = max(maximum, depth)
    return maximum


def stable_id(text: str) -> int:
    """与 hrb_mark 使用相同的 FNV-1a 标识，直接关联工具与内核任务。"""
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def activity_bounds(
    tasks: list[dict[str, Any]], begin_ns: int, end_ns: int
) -> dict[str, int]:
    """由生命周期与累计统计计算任务在给定窗口内活动量的严格上下界。"""
    runtime_lower = runtime_upper = 0
    wait_lower = wait_upper = 0
    overlap_task_ns = 0
    for item in tasks:
        birth_ns = int(item.get("birth_ns") or 0)
        exit_ns = int(item.get("exit_ns") or 0)
        if not birth_ns or exit_ns <= birth_ns:
            continue
        overlap_ns = max(0, min(end_ns, exit_ns) - max(begin_ns, birth_ns))
        if not overlap_ns:
            continue
        lifetime_ns = exit_ns - birth_ns
        outside_ns = lifetime_ns - overlap_ns
        runtime_ns = int(item.get("runtime_ns") or 0)
        wait_ns = int(item.get("runnable_wait_ns") or 0)
        overlap_task_ns += overlap_ns
        runtime_lower += max(0, runtime_ns - outside_ns)
        runtime_upper += min(runtime_ns, overlap_ns)
        wait_lower += max(0, wait_ns - outside_ns)
        wait_upper += min(wait_ns, overlap_ns)
    return {
        "overlap_task_ns": overlap_task_ns,
        "runtime_lower_ns": runtime_lower,
        "runtime_upper_ns": runtime_upper,
        "wait_lower_ns": wait_lower,
        "wait_upper_ns": wait_upper,
    }


def trace_metrics(trace: dict[str, Any]) -> dict[str, Any]:
    stages: Counter[str] = Counter()
    recorded_stage_ns: Counter[str] = Counter()
    explicit_async = 0
    dependency_edges = 0
    commands = trace.get("commands", [])
    for command in commands:
        if not isinstance(command, dict):
            continue
        stage = str(command.get("stage") or "UNKNOWN")
        stages[stage] += 1
        recorded_stage_ns[stage] += int(command.get("timing", {}).get("tool_duration_ns") or 0)
        explicit_async += int(
            bool(command.get("async"))
            or bool(command.get("asynchronous"))
            or bool(command.get("background"))
        )
        dependency_edges += len(command.get("wait_for", []))
    return {
        "command_count": len(commands),
        "stage_command_counts": dict(sorted(stages.items())),
        "recorded_stage_seconds": {
            stage: value / 1_000_000_000 for stage, value in sorted(recorded_stage_ns.items())
        },
        "explicit_async_commands": explicit_async,
        "dependency_edges": dependency_edges,
        "replay_modes": list(trace.get("replay", {}).get("modes", [])),
    }


def raw_capture_metrics(
    path: pathlib.Path,
    trace: dict[str, Any],
    annotation: dict[str, Any] | None = None,
    session_id: int | None = None,
) -> dict[str, Any]:
    tool_begin: dict[int, int] = {}
    tool_end: dict[int, int] = {}
    tool_stage: dict[int, str] = {}
    task_rows: list[dict[str, Any]] = []
    capture_end_ns = 0
    for row in read_jsonl(path):
        record = row.get("record")
        if record == "capture_end":
            capture_end_ns = int(row.get("time_ns") or 0)
            continue
        if session_id is not None and int(row.get("session_id") or 0) != session_id:
            continue
        if record == "mark":
            tool_id = int(row.get("tool_id") or 0)
            kind = row.get("kind")
            if tool_id and kind == "tool_begin":
                tool_begin[tool_id] = int(row.get("time_ns") or 0)
                tool_stage[tool_id] = str(row.get("stage") or "UNKNOWN")
            elif tool_id and kind == "tool_end":
                tool_end[tool_id] = int(row.get("time_ns") or 0)
        elif record == "task_summary":
            task_rows.append(row)

    commands = [item for item in trace.get("commands", []) if isinstance(item, dict)]
    command_by_tool: dict[int, dict[str, Any]] = {}
    for index, command in enumerate(commands):
        identifier = str(command.get("id") or "")
        if identifier:
            command_by_tool[stable_id(identifier)] = {
                "command_id": identifier,
                "command_index": index,
                "stage": str(command.get("stage") or "UNKNOWN"),
                "synchronous": not (
                    bool(command.get("async"))
                    or bool(command.get("asynchronous"))
                    or bool(command.get("background"))
                ),
            }
    selected_milestones = {
        str(item.get("command_id"))
        for item in (annotation or {}).get("selected_tool_milestones", [])
        if isinstance(item, dict) and item.get("command_id")
    }
    if not selected_milestones:
        selected_milestones = {
            str(item)
            for item in (annotation or {}).get("critical_commands", [])
            if isinstance(item, (str, int))
        }
    if not selected_milestones:
        selected_milestones = {
            str(item.get("id")) for item in commands if item.get("critical") and item.get("id")
        }

    begin_times = sorted(tool_begin.values())
    live_intervals: list[tuple[int, int]] = []
    runtime_by_stage: Counter[str] = Counter()
    wait_by_stage: Counter[str] = Counter()
    comm_counts: Counter[str] = Counter()
    outlived_tool = 0
    crossed_next_tool = 0
    max_tail_ns = 0
    outlived_runtime_ns = 0
    outliving_examples: list[dict[str, Any]] = []
    lifetimes_sec: list[float] = []
    task_intervals: list[dict[str, Any]] = []
    for task in task_rows:
        birth = int(task.get("birth_ns") or 0)
        exit_ns = int(task.get("exit_ns") or 0) or capture_end_ns
        if birth and exit_ns > birth:
            live_intervals.append((birth, exit_ns))
            lifetimes_sec.append((exit_ns - birth) / 1_000_000_000)
        tool_id = int(task.get("tool_id") or 0)
        stage = tool_stage.get(tool_id, "UNATTRIBUTED")
        runtime_ns = int(task.get("runtime_ns") or 0)
        runtime_by_stage[stage] += runtime_ns
        wait_by_stage[stage] += int(task.get("runnable_wait_ns") or 0)
        comm_counts[str(task.get("comm") or "unknown")] += 1
        if birth and exit_ns > birth:
            task_intervals.append(
                {
                    "birth_ns": birth,
                    "exit_ns": exit_ns,
                    "tool_id": tool_id,
                    "runtime_ns": runtime_ns,
                    "runnable_wait_ns": int(task.get("runnable_wait_ns") or 0),
                    "max_runnable_wait_ns": int(task.get("max_runnable_wait_ns") or 0),
                    "last_runnable_wait_ns": int(task.get("last_runnable_wait_ns") or 0),
                    "last_runnable_wait_end_ns": int(task.get("last_runnable_wait_end_ns") or 0),
                    "pid": int(task.get("pid") or 0),
                    "parent_pid": int(task.get("parent_pid") or 0),
                    "comm": str(task.get("comm") or "unknown"),
                }
            )
        end = tool_end.get(tool_id, 0)
        if end and exit_ns > end + 5_000_000:
            outlived_tool += 1
            max_tail_ns = max(max_tail_ns, exit_ns - end)
            outlived_runtime_ns += runtime_ns
            next_index = bisect.bisect_right(begin_times, end)
            crosses_next = next_index < len(begin_times) and exit_ns > begin_times[next_index]
            if crosses_next:
                crossed_next_tool += 1
            outliving_examples.append(
                {
                    "pid": int(task.get("pid") or 0),
                    "comm": str(task.get("comm") or "unknown"),
                    "stage": stage,
                    "tail_sec": (exit_ns - end) / 1_000_000_000,
                    "runtime_sec": runtime_ns / 1_000_000_000,
                    "crosses_next_tool": crosses_next,
                }
            )

    tool_windows: list[dict[str, Any]] = []
    cross_generation_windows: list[dict[str, Any]] = []
    join_windows: list[dict[str, Any]] = []
    for tool_id, begin_ns in sorted(tool_begin.items(), key=lambda item: item[1]):
        end_ns = tool_end.get(tool_id, 0)
        command = command_by_tool.get(tool_id, {})
        if not end_ns or end_ns <= begin_ns:
            continue
        command_index = int(command.get("command_index", -1))
        command_id = str(command.get("command_id") or f"tool-{tool_id}")
        stage = str(command.get("stage") or tool_stage.get(tool_id, "UNKNOWN"))
        owned = [item for item in task_intervals if int(item["tool_id"]) == tool_id]
        owned_intervals = [
            (max(begin_ns, int(item["birth_ns"])), min(end_ns, int(item["exit_ns"])))
            for item in owned
            if int(item["birth_ns"]) < end_ns and int(item["exit_ns"]) > begin_ns
        ]
        peak_live = max_live_tasks(owned_intervals)
        owned_runtime_ns = sum(int(item.get("runtime_ns") or 0) for item in owned)
        owned_wait_ns = sum(int(item.get("runnable_wait_ns") or 0) for item in owned)
        owned_bounds = activity_bounds(owned, begin_ns, end_ns)
        owned_max_wait_ns = max(
            [int(item.get("max_runnable_wait_ns") or 0) for item in owned], default=0
        )
        elapsed_ns = end_ns - begin_ns
        return_window_ns = min(50_000_000, max(5_000_000, elapsed_ns // 10))
        return_window_begin_ns = max(begin_ns, end_ns - return_window_ns)
        return_cohort = [
            item
            for item in owned
            if int(item["birth_ns"]) < end_ns
            and int(item["exit_ns"]) > return_window_begin_ns
        ]
        return_cohort_wait_ns = sum(
            int(item.get("runnable_wait_ns") or 0) for item in return_cohort
        )
        return_cohort_runtime_ns = sum(
            int(item.get("runtime_ns") or 0) for item in return_cohort
        )
        return_window_last_wait_ns = sum(
            int(item.get("last_runnable_wait_ns") or 0)
            for item in return_cohort
            if return_window_begin_ns
            <= int(item.get("last_runnable_wait_end_ns") or 0)
            <= end_ns
        )
        post_return_tail_sec = max(
            [
                max(0, int(item["exit_ns"]) - end_ns) / 1_000_000_000
                for item in owned
            ],
            default=0.0,
        )
        older: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for item in task_intervals:
            source = command_by_tool.get(int(item["tool_id"]))
            if source is None or command_index < 0:
                continue
            if int(source["command_index"]) >= command_index:
                continue
            if int(item["birth_ns"]) < end_ns and int(item["exit_ns"]) > begin_ns:
                older.append((item, source))
        older_tasks = [item for item, _ in older]
        older_bounds = activity_bounds(older_tasks, begin_ns, end_ns)
        overlap_task_ns = older_bounds["overlap_task_ns"]
        older_running_examples: list[dict[str, Any]] = []
        for item, source in older:
            bounds = activity_bounds([item], begin_ns, end_ns)
            if bounds["runtime_lower_ns"] <= 0:
                continue
            older_running_examples.append(
                {
                    "pid": int(item.get("pid") or 0),
                    "comm": str(item.get("comm") or "unknown"),
                    "source_command": str(source.get("command_id") or ""),
                    "runtime_lower_bound_sec": bounds["runtime_lower_ns"] / 1_000_000_000,
                    "overlap_sec": bounds["overlap_task_ns"] / 1_000_000_000,
                }
            )
        owned_waiting_examples: list[dict[str, Any]] = []
        for item in owned:
            bounds = activity_bounds([item], begin_ns, end_ns)
            if bounds["wait_lower_ns"] <= 0:
                continue
            owned_waiting_examples.append(
                {
                    "pid": int(item.get("pid") or 0),
                    "comm": str(item.get("comm") or "unknown"),
                    "wait_lower_bound_sec": bounds["wait_lower_ns"] / 1_000_000_000,
                    "overlap_sec": bounds["overlap_task_ns"] / 1_000_000_000,
                }
            )
        sources = sorted({str(source["command_id"]) for _, source in older})
        older_wait_ns = sum(int(item.get("runnable_wait_ns") or 0) for item, _ in older)
        older_runtime_ns = sum(int(item.get("runtime_ns") or 0) for item, _ in older)
        window = {
            "command_id": command_id,
            "command_index": command_index,
            "stage": stage,
            "synchronous": bool(command.get("synchronous", True)),
            "elapsed_sec": (end_ns - begin_ns) / 1_000_000_000,
            "owned_task_count": len(owned),
            "owned_peak_live_tasks": peak_live,
            "owned_generation_depth": generation_depth(owned),
            "owned_runtime_sec": owned_runtime_ns / 1_000_000_000,
            "owned_runnable_wait_sec": owned_wait_ns / 1_000_000_000,
            "owned_runnable_wait_fraction": (
                owned_wait_ns / (owned_runtime_ns + owned_wait_ns)
                if owned_runtime_ns + owned_wait_ns else 0.0
            ),
            "owned_runtime_in_window_lower_bound_sec": (
                owned_bounds["runtime_lower_ns"] / 1_000_000_000
            ),
            "owned_runtime_in_window_upper_bound_sec": (
                owned_bounds["runtime_upper_ns"] / 1_000_000_000
            ),
            "owned_runnable_wait_in_window_lower_bound_sec": (
                owned_bounds["wait_lower_ns"] / 1_000_000_000
            ),
            "owned_runnable_wait_in_window_upper_bound_sec": (
                owned_bounds["wait_upper_ns"] / 1_000_000_000
            ),
            "owned_waiting_processes": sorted(
                owned_waiting_examples,
                key=lambda item: float(item["wait_lower_bound_sec"]),
                reverse=True,
            )[:20],
            "owned_max_runnable_wait_ms": owned_max_wait_ns / 1_000_000,
            "return_window_sec": return_window_ns / 1_000_000_000,
            "return_cohort_task_count": len(return_cohort),
            "return_cohort_runtime_sec": return_cohort_runtime_ns / 1_000_000_000,
            "return_cohort_lifetime_runnable_wait_sec": (
                return_cohort_wait_ns / 1_000_000_000
            ),
            "return_window_observed_last_wait_lower_bound_sec": (
                return_window_last_wait_ns / 1_000_000_000
            ),
            "post_return_tail_sec": post_return_tail_sec,
            "older_generation_live_tasks": len(older),
            "older_generation_source_commands": sources,
            "older_generation_overlap_task_sec": overlap_task_ns / 1_000_000_000,
            "older_generation_lifetime_runtime_sec": older_runtime_ns / 1_000_000_000,
            "older_generation_lifetime_runnable_wait_sec": older_wait_ns / 1_000_000_000,
            "older_generation_runtime_in_window_lower_bound_sec": (
                older_bounds["runtime_lower_ns"] / 1_000_000_000
            ),
            "older_generation_runtime_in_window_upper_bound_sec": (
                older_bounds["runtime_upper_ns"] / 1_000_000_000
            ),
            "older_generation_runnable_wait_in_window_lower_bound_sec": (
                older_bounds["wait_lower_ns"] / 1_000_000_000
            ),
            "older_generation_runnable_wait_in_window_upper_bound_sec": (
                older_bounds["wait_upper_ns"] / 1_000_000_000
            ),
            "older_generation_running_processes": sorted(
                older_running_examples,
                key=lambda item: float(item["runtime_lower_bound_sec"]),
                reverse=True,
            )[:20],
            "selected_tool_milestone": command_id in selected_milestones,
            "critical": command_id in selected_milestones,
        }
        tool_windows.append(window)
        if older:
            cross_generation_windows.append(dict(window))
        if peak_live >= 4:
            join_windows.append(dict(window))

    milestone_windows = [item for item in tool_windows if item["selected_tool_milestone"]]
    max_join_tail_sec = max(
        [float(window["post_return_tail_sec"]) for window in join_windows], default=0.0
    )

    return {
        "raw_capture_available": True,
        "task_rows": len(task_rows),
        "max_live_tasks": max_live_tasks(live_intervals),
        "p50_task_lifetime_sec": percentile(lifetimes_sec, 0.50),
        "p95_task_lifetime_sec": percentile(lifetimes_sec, 0.95),
        "p99_task_lifetime_sec": percentile(lifetimes_sec, 0.99),
        "tasks_outliving_tool_return": outlived_tool,
        "tasks_crossing_next_tool": crossed_next_tool,
        "longest_post_tool_tail_sec": max_tail_ns / 1_000_000_000,
        "outliving_task_runtime_sec": outlived_runtime_ns / 1_000_000_000,
        "tool_windows": tool_windows,
        "cross_generation_windows": cross_generation_windows,
        "cross_generation_window_count": len(cross_generation_windows),
        "max_cross_generation_tasks": max(
            [int(item["older_generation_live_tasks"]) for item in cross_generation_windows],
            default=0,
        ),
        "cross_generation_overlap_task_seconds": sum(
            float(item["older_generation_overlap_task_sec"])
            for item in cross_generation_windows
        ),
        "join_windows": join_windows,
        "join_window_count": len(join_windows),
        "max_tool_peak_live_tasks": max(
            [int(item["owned_peak_live_tasks"]) for item in tool_windows], default=0
        ),
        "selected_milestone_tool_task_count": sum(
            int(item["owned_task_count"]) for item in milestone_windows
        ),
        "selected_milestone_tool_peak_live_tasks": max(
            [int(item["owned_peak_live_tasks"]) for item in milestone_windows], default=0
        ),
        "selected_milestone_tool_completion_tail_sec": max(
            [float(item["post_return_tail_sec"]) for item in milestone_windows], default=0.0
        ),
        # 兼容已经发布的数据读取脚本；这些字段不再被解释为关键路径。
        "critical_tool_task_count": sum(
            int(item["owned_task_count"]) for item in milestone_windows
        ),
        "critical_tool_peak_live_tasks": max(
            [int(item["owned_peak_live_tasks"]) for item in milestone_windows], default=0
        ),
        "critical_tool_completion_tail_sec": max(
            [float(item["post_return_tail_sec"]) for item in milestone_windows], default=0.0
        ),
        "max_join_tail_sec": max_join_tail_sec,
        "longest_outliving_tasks": sorted(
            outliving_examples, key=lambda item: float(item["tail_sec"]), reverse=True
        )[:20],
        "runtime_by_stage_sec": {
            stage: value / 1_000_000_000 for stage, value in sorted(runtime_by_stage.items())
        },
        "runnable_wait_by_stage_sec": {
            stage: value / 1_000_000_000 for stage, value in sorted(wait_by_stage.items())
        },
        "most_common_processes": [
            {"comm": name, "tasks": count} for name, count in comm_counts.most_common(12)
        ],
    }


def classify(profile: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    elapsed = float(profile["replay_elapsed_sec"])
    task_count = int(profile["kernel"]["task_count"])
    effective_parallelism = float(profile["kernel"]["effective_parallelism"])
    runnable_wait = float(profile["kernel"]["runnable_wait_sec"])
    wait_fraction = float(profile["kernel"]["runnable_wait_fraction"])
    raw = profile["kernel"].get("raw", {})
    actual_stages = profile.get("actual_stage_seconds", {})
    if elapsed >= 30:
        tags.append("长时间运行")
    if task_count >= 1000:
        tags.append("高进程扩展")
    if effective_parallelism >= 1.5:
        tags.append("并行执行")
    if runnable_wait >= 1.0 and wait_fraction >= 0.03:
        tags.append("可运行队列竞争")
    if int(profile["trace"]["explicit_async_commands"]) > 0:
        tags.append("显式异步")
    if int(raw.get("tasks_crossing_next_tool") or 0) > 0:
        tags.append("跨工具存活")
    if int(raw.get("cross_generation_window_count") or 0) > 0:
        tags.append("代次重叠")
    if int(raw.get("max_tool_peak_live_tasks") or 0) >= 4:
        tags.append("并行汇合")
    dominant = max(actual_stages, key=actual_stages.get) if actual_stages else ""
    if dominant == "TEST" and actual_stages[dominant] >= elapsed * 0.4:
        tags.append("测试主导")
    elif dominant == "BUILD" and actual_stages[dominant] >= elapsed * 0.4:
        tags.append("构建主导")
    elif dominant == "INSPECT" and actual_stages[dominant] >= elapsed * 0.4:
        tags.append("检索主导")
    elif dominant == "EDIT" and actual_stages[dominant] >= elapsed * 0.4:
        tags.append("编辑主导")
    elif dominant:
        tags.append(f"{STAGE_NAMES.get(dominant, dominant)}主导")
    return tags


def result_directories(result_root: pathlib.Path) -> dict[str, pathlib.Path]:
    """兼容单任务目录和统一运行程序写出的套件目录。"""
    found: dict[str, pathlib.Path] = {}
    summary_path = result_root / "结果汇总.json"
    if summary_path.is_file():
        summary = read_json(summary_path)
        for row in summary.get("workflows", []):
            if not isinstance(row, dict):
                continue
            instance = str(row.get("workflow") or "")
            relative = str(row.get("result") or "")
            if instance and relative:
                found[instance] = (result_root / relative).parent
    for path in result_root.glob("*/result.json"):
        result = read_json(path)
        cases = result.get("cases") if isinstance(result.get("cases"), list) else []
        if not cases or not isinstance(cases[0], dict):
            continue
        if len(cases) != 1:
            raise ValueError(f'执行画像需要单任务结果，不能把并发套件的总 CPU 时间归给第一个任务：{path}')
        instance = str(cases[0].get("instance_id") or "")
        if instance:
            found.setdefault(instance, path.parent)
    return found


def build_profile(
    task: dict[str, Any],
    result_root: pathlib.Path,
    directories: dict[str, pathlib.Path],
) -> dict[str, Any]:
    instance = str(task["instance_id"])
    trace = read_json(ROOT / str(task["trace_file"]))
    result_dir = directories.get(instance, result_root / instance)
    result_path = result_dir / "result.json"
    if not result_path.is_file():
        return {
            "instance_id": instance,
            "status": "missing",
            "trace": trace_metrics(trace),
        }
    result = read_json(result_path)
    cases = result.get('cases', [])
    if len(cases) != 1 or cases[0].get('instance_id') != instance:
        raise ValueError(f'结果必须只包含所请求的单个任务：{result_path}')
    case = cases[0]
    kernel = result.get("kernel_capture", {})
    elapsed = float(case.get("elapsed_sec") or 0)
    runtime_sec = int(kernel.get("runtime_ns") or 0) / 1_000_000_000
    wait_sec = int(kernel.get("runnable_wait_ns") or 0) / 1_000_000_000
    raw_path = result_dir / "kernel-capture.jsonl"
    annotation_path = ROOT / "annotations" / "formal" / f"{instance}.annotation.json"
    annotation = read_json(annotation_path) if annotation_path.is_file() else None
    raw = (
        raw_capture_metrics(raw_path, trace, annotation)
        if raw_path.is_file()
        else {"raw_capture_available": False}
    )
    event_name = case.get('retained', {}).get('events')
    events_path = result_dir / event_name if event_name else None
    timeouts = [event for event in read_jsonl(events_path)
                if event.get('record') == 'command_end' and event.get('timed_out')] if events_path else []
    timeout_ids = {event['command_id'] for event in timeouts}
    for group in ('tool_windows', 'cross_generation_windows', 'join_windows'):
        for window in raw.get(group, []):
            window['timed_out'] = window['command_id'] in timeout_ids if events_path else None
    provenance = {
        'result_file': result_path.resolve().relative_to(result_root.resolve()).as_posix(),
        'result_file_scope': 'Relative to the external measurement result root; raw results are not embedded in the benchmark package.',
        'result_sha256': file_sha256(result_path),
        'trace_sha256_at_analysis': file_sha256(ROOT / str(task['trace_file'])),
        'trace_hash_scope': 'Hash of trace read by this analysis; use the experiment input inventory to verify it was frozen during execution.',
        'timing_semantics': result.get('experiment', {}).get('timing_semantics', 'legacy-execution-start'),
        'scheduler': result.get('scheduler'),
        'kernel': result.get('environment', {}).get('kernel'),
        'cpu_set': result.get('environment', {}).get('cpu_set'),
        'container_images': result.get('environment', {}).get('container_images', []),
    }
    if raw_path.is_file():
        provenance['raw_capture_sha256'] = file_sha256(raw_path)
    if events_path:
        provenance['events_sha256'] = file_sha256(events_path)
    profile: dict[str, Any] = {
        "instance_id": instance,
        "trace_file": str(task["trace_file"]),
        "status": str(result.get("status") or "unknown"),
        "source_framework": str(trace.get("source_task", {}).get("framework") or "unknown"),
        "repository": str(trace.get("source_task", {}).get("repository") or "unknown"),
        "replay_elapsed_sec": elapsed,
        "workflow_completion_sec": float(case.get('workflow_completion_sec', elapsed)),
        "measurement_provenance": provenance,
        "timeout_observation_available": events_path is not None,
        "timed_out_commands": [{key: event[key] for key in ('command_id', 'command_index')}
                               for event in timeouts],
        "selected_tool_milestone_sec": float(case.get("selected_tool_milestone_sec") or case.get("critical_sec") or 0),
        "selected_tool_milestone_semantics": "heuristic_not_critical_path",
        "critical_metric_status": "deprecated_heuristic_alias",
        "critical_sec": float(case.get("critical_sec") or 0),
        "actual_stage_seconds": dict(case.get("stage_seconds", {})),
        "trace": trace_metrics(trace),
        "kernel": {
            "collector": kernel.get("collector"),
            "complete": kernel.get("complete"),
            "dropped_events": int(kernel.get("dropped_events") or 0),
            "task_count": int(kernel.get("task_count") or 0),
            "forks": sum(int(item.get("forks") or 0) for item in kernel.get("sessions", [])),
            "execs": sum(int(item.get("execs") or 0) for item in kernel.get("sessions", [])),
            "runtime_sec": runtime_sec,
            "runnable_wait_sec": wait_sec,
            "runnable_wait_fraction": wait_sec / (runtime_sec + wait_sec) if runtime_sec + wait_sec else 0.0,
            "max_runnable_wait_ms": int(kernel.get("max_runnable_wait_ns") or 0) / 1_000_000,
            "effective_parallelism": runtime_sec / elapsed if elapsed else 0.0,
            "wakeups": int(kernel.get("wakeups") or 0),
            "switches": int(kernel.get("switches") or 0),
            "migrations": int(kernel.get("migrations") or 0),
            "raw": raw,
        },
    }
    profile["behavior_tags"] = classify(profile)
    return profile


def execution_relations(profile: dict[str, Any], measurement_id: str) -> dict[str, Any]:
    raw = profile.get("kernel", {}).get("raw", {})
    causal = [
        {
            "command_id": str(item["command_id"]),
            "command_index": int(item["command_index"]),
            "stage": str(item["stage"]),
            "source_commands": list(item.get("older_generation_source_commands", [])),
            "observed_live_tasks": int(item.get("older_generation_live_tasks") or 0),
            "observed_overlap_task_sec": float(
                item.get("older_generation_overlap_task_sec") or 0
            ),
            "observed_runtime_lower_bound_sec": float(
                item.get("older_generation_runtime_in_window_lower_bound_sec") or 0
            ),
            "observed_wait_lower_bound_sec": float(
                item.get("older_generation_runnable_wait_in_window_lower_bound_sec") or 0
            ),
        }
        for item in raw.get("cross_generation_windows", [])
        if not item.get('timed_out')
    ]
    joins = [
        {
            "command_id": str(item["command_id"]),
            "command_index": int(item["command_index"]),
            "stage": str(item["stage"]),
            "observed_task_count": int(item.get("owned_task_count") or 0),
            "observed_peak_live_tasks": int(item.get("owned_peak_live_tasks") or 0),
            "observed_post_return_tail_sec": float(item.get("post_return_tail_sec") or 0),
        }
        for item in raw.get("join_windows", [])
        if not item.get('timed_out')
    ]
    return {
        "schema": "harness_replaybench.execution_relations.v1",
        "instance_id": str(profile["instance_id"]),
        "trace_file": str(profile["trace_file"]),
        "measurement_id": measurement_id,
        "measurement_provenance": profile.get('measurement_provenance', {}),
        "timed_out_commands": profile.get('timed_out_commands', []),
        "reference_scheduler": "scx_simple",
        "capture": "C/eBPF",
        "task_generation_rule": "子进程继承创建者所属的工具代次，进程执行新程序时保持代次不变",
        "causal_windows": causal,
        "join_windows": joins,
        "critical_commands": [
            str(item["command_id"])
            for item in raw.get("tool_windows", [])
            if item.get("selected_tool_milestone") and not item.get('timed_out')
        ],
        "selected_tool_milestones": [
            str(item["command_id"])
            for item in raw.get("tool_windows", [])
            if item.get("selected_tool_milestone") and not item.get('timed_out')
        ],
        "notes": [
            "已观测到超时终止的命令保留在原始画像中，但不作为正常返回的关系窗口或里程碑。",
            "本文件由 scx_simple 下的单任务内核画像离线生成，只用于套件选择和运行后统计。",
            "本文件不会传给在线被测调度器。",
            "存活重叠表示生命区间相交，不表示进程在整个区间持续占用处理器。",
            "critical_commands 是早期兼容字段，表示启发式工具里程碑，不是工作流关键路径。",
            "return_cohort_lifetime_runnable_wait_sec 是返回前仍存活任务的生命周期累计等待，不是尾部区间内等待的精确积分。",
            "return_window_observed_last_wait_lower_bound_sec 只累计返回窗口内结束的每任务最后一次可运行等待，是尾部等待的可观测下界。",
        ],
    }


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# HarnessReplayBench 1.0 执行行为画像",
        "",
        "画像由正式轨迹语义、单任务回放结果和 C/eBPF 任务统计共同生成。任务分类只使用执行结构，不读取任何被测调度器的性能结果。",
        "",
        "| 任务 | 耗时（秒） | 进程数 | 有效并行度 | 可运行等待（秒） | 工具内峰值 | 代次重叠窗口 | 行为标签 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    profiles = [item for item in payload["profiles"] if item.get("status") == "pass"]
    for item in sorted(profiles, key=lambda value: float(value["replay_elapsed_sec"]), reverse=True):
        kernel = item["kernel"]
        raw = kernel.get("raw", {})
        lines.append(
            "| {instance} | {elapsed:.3f} | {tasks} | {parallel:.2f} | {wait:.3f} | {live} | {cross} | {tags} |".format(
                instance=item["instance_id"],
                elapsed=float(item["replay_elapsed_sec"]),
                tasks=int(kernel["task_count"]),
                parallel=float(kernel["effective_parallelism"]),
                wait=float(kernel["runnable_wait_sec"]),
                live=raw.get("max_tool_peak_live_tasks", "—"),
                cross=raw.get("cross_generation_window_count", "—"),
                tags="、".join(item.get("behavior_tags", [])) or "常规顺序执行",
            )
        )
    lines.extend(
        [
            "",
            "## 解释",
            "",
            "- 有效并行度是累计 CPU 运行时间除以回放墙钟时间，用于区分并行计算与等待型任务。",
            "- 代次重叠窗口表示一个工具执行期间，前序工具创建的后代进程仍然存活。",
            "- 工具内峰值是同一工具代次中进程生命区间的峰值，不等同于同时占用 CPU 的进程数。",
            "- 行为标签使用固定阈值生成，用于构建测试套件，而不是用于挑选对某个调度器有利的任务。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 HarnessReplayBench 执行行为画像")
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--output", default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"))
    parser.add_argument("--markdown", default=str(ROOT / "HarnessReplayBench-1.0-执行行为画像.md"))
    parser.add_argument("--measurement-id", default="HarnessReplayBench 1.0 单任务执行画像")
    parser.add_argument(
        "--relations-root",
        default=str(ROOT / "annotations" / "execution-relations"),
    )
    args = parser.parse_args()

    result_root = pathlib.Path(args.result_root).resolve()
    dataset = read_json(ROOT / "datasets" / "HarnessReplayBench-1.0-任务索引.json")
    all_tasks = [item for item in dataset.get("tasks", []) if isinstance(item, dict)]
    selected_tasks = [item for item in all_tasks if item.get("included_in_suites", True)]
    excluded_tasks = [item for item in all_tasks if not item.get("included_in_suites", True)]
    directories = result_directories(result_root)
    profiles = [build_profile(task, result_root, directories) for task in selected_tasks]
    relations_root = pathlib.Path(args.relations_root)
    relations_root.mkdir(parents=True, exist_ok=True)
    for old in relations_root.glob("*.relations.json"):
        old.unlink()
    for profile in profiles:
        if profile.get("status") != "pass" or not profile.get("kernel", {}).get("raw", {}).get(
            "raw_capture_available"
        ):
            continue
        relation_path = relations_root / f"{profile['instance_id']}.relations.json"
        relation_path.write_text(
            json.dumps(
                execution_relations(profile, args.measurement_id),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        try:
            relation_reference = relation_path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            relation_reference = str(relation_path.resolve())
        profile["execution_relations_file"] = relation_reference
    tags: Counter[str] = Counter(
        tag for profile in profiles for tag in profile.get("behavior_tags", [])
    )
    payload = {
        "schema": "harness_replaybench.behavior_profiles.v1",
        "version": "1.0",
        "method": "正式轨迹语义与单任务 C/eBPF 执行统计",
        "scheduler": "scx_simple",
        "cpu_set": "0-7",
        "measurement_id": args.measurement_id,
        "summary": {
            "formal_tasks": len(all_tasks),
            "profiled_tasks": len(profiles),
            "excluded_tasks": len(excluded_tasks),
            "passed": sum(item.get("status") == "pass" for item in profiles),
            "with_raw_capture": sum(
                bool(item.get("kernel", {}).get("raw", {}).get("raw_capture_available"))
                for item in profiles
            ),
            "with_execution_relations": sum(
                bool(item.get("execution_relations_file")) for item in profiles
            ),
            "behavior_tag_counts": dict(sorted(tags.items())),
        },
        "excluded": [
            {
                "instance_id": str(item.get("instance_id") or ""),
                "exclusion": item.get("exclusion", {}),
            }
            for item in excluded_tasks
        ],
        "profiles": profiles,
    }
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = pathlib.Path(args.markdown)
    markdown.parent.mkdir(parents=True, exist_ok=True)
    markdown.write_text(markdown_report(payload), encoding="utf-8")
    print(json.dumps({"output": str(output), "markdown": str(markdown), **payload["summary"]}, ensure_ascii=False))
    return 0 if payload["summary"]["passed"] == len(profiles) else 1


if __name__ == "__main__":
    raise SystemExit(main())

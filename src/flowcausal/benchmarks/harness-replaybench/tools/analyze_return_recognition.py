#!/usr/bin/env python3
"""离线核对候选调度器的返回任务识别结果。

本程序不参与在线调度。它把 C 控制程序输出的等待/提权事件，与回放器的
工具开始结束时间及 C/eBPF 任务生命周期记录进行交叉核对。
"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import defaultdict
from typing import Any, Iterable


WAIT_LINK = 10
ROLE_CHANGE = 9
RETURN_REASON = 4
WAIT_TARGET = 1 << 2
RETURN_ROOT = 1 << 6


def json_lines(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def command_windows(path: pathlib.Path) -> dict[int, tuple[int, int]]:
    begins: dict[int, int] = {}
    ends: dict[int, int] = {}
    for row in json_lines(path):
        index = row.get("command_index")
        if not isinstance(index, int):
            continue
        if row.get("record") == "command_begin":
            begins[index] = int(row.get("time_ns") or 0)
        elif row.get("record") == "command_end":
            ends[index] = int(row.get("time_ns") or 0)
    return {
        index: (begin, ends[index])
        for index, begin in begins.items()
        if index in ends and begin <= ends[index]
    }


def case_event_files(case_dirs: list[pathlib.Path]) -> dict[str, pathlib.Path]:
    files: dict[str, pathlib.Path] = {}
    for case_dir in case_dirs:
        path = case_dir / "events.jsonl"
        if not path.exists():
            continue
        for row in json_lines(path):
            instance = row.get("instance_id")
            if isinstance(instance, str) and instance:
                files[instance] = path
                break
    return files


def load_control_events(case_dirs: list[pathlib.Path]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for case_dir in case_dirs:
        log = case_dir / "flowd.log"
        if not log.exists():
            continue
        for row in json_lines(log):
            kind = int(row.get("kind") or 0)
            if kind not in {WAIT_LINK, ROLE_CHANGE}:
                continue
            key = (
                row.get("time_ns"), kind, row.get("session_id"),
                row.get("sequence"), row.get("pid"), row.get("related_pid"),
                row.get("reason"), row.get("task_flags"),
            )
            unique[key] = row
    return sorted(unique.values(), key=lambda row: int(row.get("time_ns") or 0))


def load_tasks(path: pathlib.Path) -> dict[tuple[int, int], list[dict[str, Any]]]:
    tasks: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in json_lines(path):
        if row.get("record") != "task_summary":
            continue
        key = (int(row.get("session_id") or 0), int(row.get("pid") or 0))
        tasks[key].append(row)
    return tasks


def task_at(
    tasks: dict[tuple[int, int], list[dict[str, Any]]],
    session_id: int,
    pid: int,
    timestamp: int,
) -> dict[str, Any] | None:
    candidates = tasks.get((session_id, pid), [])
    for task in candidates:
        birth = int(task.get("birth_ns") or 0)
        exit_time = int(task.get("exit_ns") or 0)
        if birth <= timestamp and (not exit_time or timestamp <= exit_time):
            return task
    return None


def has_wait_ancestor(
    *, session_id: int, sequence: int, pid: int,
    wait_targets: set[tuple[int, int, int]],
    tasks: dict[tuple[int, int], list[dict[str, Any]]],
    timestamp: int,
) -> bool:
    current = pid
    for _ in range(13):
        if (session_id, sequence, current) in wait_targets:
            return True
        task = task_at(tasks, session_id, current, timestamp)
        if not task:
            return False
        parent = int(task.get("parent_pid") or 0)
        if not parent or parent == current:
            return False
        current = parent
    return False


def analyze(trial: pathlib.Path) -> dict[str, Any]:
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    case_dirs = sorted((trial / "cases").glob("*"))
    controls = result.get("flowcausal_control", {}).get("sessions", [])
    instance_by_session = {
        int(item.get("stats", {}).get("session_id") or 0): str(item.get("instance_id") or "")
        for item in controls
        if isinstance(item, dict) and isinstance(item.get("stats"), dict)
    }
    event_file_by_instance = case_event_files(case_dirs)
    windows_by_session = {
        session_id: command_windows(event_file_by_instance[instance])
        for session_id, instance in instance_by_session.items()
        if instance in event_file_by_instance
    }
    events = load_control_events(case_dirs)
    tasks = load_tasks(trial / "kernel-capture.jsonl")
    wait_targets = {
        (
            int(row.get("session_id") or 0),
            int(row.get("sequence") or 0),
            int(row.get("related_pid") or 0),
        )
        for row in events
        if int(row.get("kind") or 0) == WAIT_LINK and int(row.get("related_pid") or 0)
    }
    promotions = [
        row for row in events
        if int(row.get("kind") or 0) == ROLE_CHANGE
        and int(row.get("reason") or 0) == RETURN_REASON
    ]
    per_session: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "promotions": 0,
            "registered_return_roots": 0,
            "observed_wait_targets": 0,
            "inferred_tail_members": 0,
            "inside_tool_window": 0,
            "linked_to_wait_target": 0,
            "alive_when_selected": 0,
            "completed_by_tool_return": 0,
            "fully_correlated": 0,
        }
    )
    unique_promoted: dict[int, set[tuple[int, int]]] = defaultdict(set)
    unique_correlated: dict[int, set[tuple[int, int]]] = defaultdict(set)
    mismatches: list[dict[str, Any]] = []
    for row in promotions:
        session_id = int(row.get("session_id") or 0)
        sequence = int(row.get("sequence") or 0)
        pid = int(row.get("pid") or 0)
        timestamp = int(row.get("time_ns") or 0)
        flags = int(row.get("task_flags") or 0)
        index = (sequence - 1) // 2 if sequence > 0 and sequence % 2 else -1
        window = windows_by_session.get(session_id, {}).get(index)
        task = task_at(tasks, session_id, pid, timestamp)
        registered_root = bool(flags & RETURN_ROOT)
        observed_target = bool(flags & WAIT_TARGET) and not registered_root
        inside = bool(window and window[0] <= timestamp <= window[1])
        linked = bool(flags & RETURN_ROOT) or has_wait_ancestor(
            session_id=session_id,
            sequence=sequence,
            pid=pid,
            wait_targets=wait_targets,
            tasks=tasks,
            timestamp=timestamp,
        )
        alive = bool(
            task
            and int(task.get("birth_ns") or 0) <= timestamp
            and (not int(task.get("exit_ns") or 0) or timestamp <= int(task.get("exit_ns") or 0))
        )
        completed = bool(
            window and task and int(task.get("exit_ns") or 0)
            and int(task.get("exit_ns") or 0) <= window[1] + 5_000_000
        )
        fully = inside and linked and alive and completed
        summary = per_session[session_id]
        summary["promotions"] += 1
        unique_promoted[session_id].add((sequence, pid))
        if registered_root:
            summary["registered_return_roots"] += 1
        elif observed_target:
            summary["observed_wait_targets"] += 1
        else:
            summary["inferred_tail_members"] += 1
        summary["inside_tool_window"] += int(inside)
        summary["linked_to_wait_target"] += int(linked)
        summary["alive_when_selected"] += int(alive)
        summary["completed_by_tool_return"] += int(completed)
        summary["fully_correlated"] += int(fully)
        if fully:
            unique_correlated[session_id].add((sequence, pid))
        if not fully:
            mismatches.append({
                "session_id": session_id,
                "instance_id": instance_by_session.get(session_id, ""),
                "sequence": sequence,
                "command_index": index,
                "pid": pid,
                "comm": row.get("comm", ""),
                "inside_tool_window": inside,
                "linked_to_wait_target": linked,
                "alive_when_selected": alive,
                "completed_by_tool_return": completed,
            })
    sessions = []
    for session_id, counts in sorted(per_session.items()):
        total = counts["promotions"]
        sessions.append({
            "session_id": session_id,
            "instance_id": instance_by_session.get(session_id, ""),
            **counts,
            "unique_promoted_tasks": len(unique_promoted[session_id]),
            "unique_correlated_tasks": len(unique_correlated[session_id]),
            "correlation_percent": counts["fully_correlated"] / total * 100.0 if total else None,
        })
    total = sum(item["promotions"] for item in sessions)
    correlated = sum(item["fully_correlated"] for item in sessions)
    return {
        "schema": "flowcausal.return_recognition_analysis.v1",
        "trial": str(trial),
        "wait_link_events": len([row for row in events if int(row.get("kind") or 0) == WAIT_LINK]),
        "unique_wait_targets": len(wait_targets),
        "promotions": total,
        "unique_promoted_tasks": sum(item["unique_promoted_tasks"] for item in sessions),
        "fully_correlated": correlated,
        "unique_correlated_tasks": sum(item["unique_correlated_tasks"] for item in sessions),
        "correlation_percent": correlated / total * 100.0 if total else None,
        "sessions": sessions,
        "mismatches": mismatches,
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal 返回任务群识别核对",
        "",
        f"等待关系事件：{report['wait_link_events']}；唯一等待目标：{report['unique_wait_targets']}；"
        f"短时提前事件：{report['promotions']}，涉及唯一任务：{report['unique_promoted_tasks']}。",
        "",
        "完全对应要求同时满足：发生在工具执行窗口内、属于已登记的工具根或连续等待链、选择时仍存活、并在该工具返回时完成。",
        "",
        "| 任务 | 短时提前事件 | 唯一任务 | 已登记工具根事件 | 等待目标事件 | 推断普通后代 | 完全对应事件 | 对应比例 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report["sessions"]:
        percent = item["correlation_percent"]
        lines.append(
            f"| `{item['instance_id']}` | {item['promotions']} | {item['unique_promoted_tasks']} | "
            f"{item['registered_return_roots']} | "
            f"{item['observed_wait_targets']} | {item['inferred_tail_members']} | "
            f"{item['fully_correlated']} | "
            f"{percent:.1f}% |" if percent is not None else
            f"| `{item['instance_id']}` | 0 | 0 | 0 | 0 | 0 | 0 | — |"
        )
    lines.extend([
        "",
        f"总对应比例：{report['correlation_percent']:.1f}%" if report["correlation_percent"] is not None else "总对应比例：—",
        "",
        "该核对用于识别正确性，不作为性能结果。",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 FlowCausal 返回任务群识别")
    parser.add_argument("trial", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    trial = args.trial.resolve()
    report = analyze(trial)
    output = args.output.resolve() if args.output else trial / "返回任务群识别核对.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

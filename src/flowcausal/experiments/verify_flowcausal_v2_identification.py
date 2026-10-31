#!/usr/bin/env python3
"""用同一次运行的 FlowCausal 事件与独立 C/eBPF 记录核对任务识别。"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter, defaultdict
from typing import Any


WAIT_LINK = 10
ROLE_CHANGE = 9
RETURN_COHORT = 4
TASK_RETURN_ROOT = 1 << 6
TASK_PROGRESS_GROUP = 1 << 17


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def read_json_lines(path: pathlib.Path):
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text.startswith("{"):
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"无法解析 {path}:{number}") from error
            if isinstance(value, dict):
                yield value


def event_key(event: dict[str, Any]) -> tuple[int, ...]:
    return tuple(
        int(event.get(name) or 0)
        for name in (
            "time_ns",
            "kind",
            "session_id",
            "tool_id",
            "sequence",
            "pid",
            "related_pid",
            "task_flags",
        )
    )


def is_descendant(
    session_id: int,
    child_pid: int,
    ancestor_pid: int,
    parents: dict[tuple[int, int], int],
) -> bool:
    current = child_pid
    visited: set[int] = set()
    for _ in range(64):
        if current == ancestor_pid:
            return True
        if current in visited:
            return False
        visited.add(current)
        current = parents.get((session_id, current), 0)
        if not current:
            return False
    return False


def percentage(numerator: int, denominator: int) -> float | None:
    return numerator / denominator * 100.0 if denominator else None


def display_ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 FlowCausal v2 任务识别")
    parser.add_argument("--result-root", required=True)
    parser.add_argument("--output-json")
    parser.add_argument("--output-markdown")
    args = parser.parse_args()

    root = pathlib.Path(args.result_root).resolve()
    result = read_json(root / "result.json")
    capture_path = root / "kernel-capture.jsonl"
    if result.get("status") != "pass" or not capture_path.is_file():
        raise SystemExit("需要一次通过且保留原始 C/eBPF 记录的运行")

    sessions = {
        int(item["session_id"]): str(item["instance_id"])
        for item in result.get("kernel_capture", {}).get("session_ids", [])
    }
    parents: dict[tuple[int, int], int] = {}
    present: set[tuple[int, int]] = set()
    birth: dict[tuple[int, int], int] = {}
    exit_time: dict[tuple[int, int], int] = {}
    runtime: dict[tuple[int, int], int] = defaultdict(int)
    runnable_wait: dict[tuple[int, int], int] = defaultdict(int)
    namespace_children: dict[tuple[int, int, int], list[int]] = defaultdict(list)
    independent_wait_events: list[dict[str, Any]] = []
    independent_wait_end_events: list[dict[str, Any]] = []
    independent_wait_blocked_events: list[dict[str, Any]] = []

    for record in read_json_lines(capture_path):
        session_id = int(record.get("session_id") or 0)
        pid = int(record.get("pid") or 0)
        if not session_id or not pid or session_id not in sessions:
            continue
        key = (session_id, pid)
        present.add(key)
        parent_pid = int(record.get("parent_pid") or 0)
        if parent_pid and key not in parents:
            parents[key] = parent_pid
        if record.get("record") == "task_summary":
            birth[key] = int(record.get("birth_ns") or 0)
            exit_time[key] = int(record.get("exit_ns") or 0)
            runtime[key] = int(record.get("runtime_ns") or 0)
            runnable_wait[key] = int(record.get("runnable_wait_ns") or 0)
            namespace_pid = int(record.get("namespace_pid") or 0)
            if namespace_pid and parent_pid:
                namespace_children[(session_id, parent_pid, namespace_pid)].append(pid)
        if record.get("record") == "kernel_event" and record.get("kind") == "wait":
            independent_wait_events.append(record)
        if record.get("record") == "kernel_event" and record.get("kind") == "wait_end":
            independent_wait_end_events.append(record)
        if record.get("record") == "kernel_event" and record.get("kind") == "wait_blocked":
            independent_wait_blocked_events.append(record)

    flow_events: dict[tuple[int, ...], dict[str, Any]] = {}
    parsed_event_lines = 0
    for path in sorted((root / "cases").glob("*/flowd.log")):
        for event in read_json_lines(path):
            if int(event.get("kind") or 0) not in {WAIT_LINK, ROLE_CHANGE}:
                continue
            parsed_event_lines += 1
            flow_events[event_key(event)] = event

    wait_events = [
        event for event in flow_events.values() if int(event.get("kind") or 0) == WAIT_LINK
    ]
    promotion_events = [
        event
        for event in flow_events.values()
        if int(event.get("kind") or 0) == ROLE_CHANGE
        and int(event.get("reason") or 0) == RETURN_COHORT
    ]
    group_promotions = [
        event
        for event in promotion_events
        if int(event.get("task_flags") or 0) & TASK_PROGRESS_GROUP
    ]
    root_promotions = [
        event
        for event in promotion_events
        if int(event.get("task_flags") or 0) & TASK_RETURN_ROOT
    ]

    def flow_wait_pair(event: dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(event.get("session_id") or 0),
            int(event.get("pid") or 0),
            int(event.get("related_pid") or 0),
        )

    def independent_wait_pair(event: dict[str, Any]) -> tuple[int, int, int]:
        return (
            int(event.get("session_id") or 0),
            int(event.get("pid") or 0),
            int(event.get("value") or 0),
        )

    flow_wait_pairs = Counter(
        flow_wait_pair(event)
        for event in wait_events
        if flow_wait_pair(event)[2] > 0
    )
    independent_explicit_wait_pairs = Counter(
        independent_wait_pair(event)
        for event in independent_wait_events
        if independent_wait_pair(event)[2] > 0
    )
    matched_wait_events = sum(
        min(count, independent_explicit_wait_pairs.get(pair, 0))
        for pair, count in flow_wait_pairs.items()
    )
    matched_distinct_wait_pairs = len(
        set(flow_wait_pairs) & set(independent_explicit_wait_pairs)
    )
    flow_waiter_keys = {(session_id, waiter_pid) for session_id, waiter_pid, _ in flow_wait_pairs}

    wait_cycles: list[dict[str, Any]] = []
    pending_waits: dict[tuple[int, int], dict[str, Any]] = {}
    for event in sorted(
        [
            *independent_wait_events,
            *independent_wait_blocked_events,
            *independent_wait_end_events,
        ],
        key=lambda item: int(item.get("time_ns") or 0),
    ):
        key = (int(event.get("session_id") or 0), int(event.get("pid") or 0))
        if event.get("kind") == "wait":
            pending_waits.setdefault(key, {**event, "blocked": False})
            continue
        if event.get("kind") == "wait_blocked":
            if key in pending_waits:
                pending_waits[key]["blocked"] = True
            continue
        begin = pending_waits.pop(key, None)
        if begin is None:
            continue
        returned_namespace_pid = int(event.get("value") or 0)
        returned_pid = 0
        if returned_namespace_pid > 0:
            candidates = namespace_children.get(
                (key[0], key[1], returned_namespace_pid), []
            )
            event_time = int(event.get("time_ns") or 0)
            eligible = [
                candidate
                for candidate in candidates
                if birth.get((key[0], candidate), 0) <= event_time
                and (
                    not exit_time.get((key[0], candidate), 0)
                    or exit_time[(key[0], candidate)] <= event_time + 1_000_000
                )
            ]
            if eligible:
                returned_pid = max(
                    eligible,
                    key=lambda candidate: (
                        exit_time.get((key[0], candidate), 0),
                        birth.get((key[0], candidate), 0),
                    ),
                )
        wait_cycles.append(
            {
                "session_id": key[0],
                "waiter_pid": key[1],
                "begin_ns": int(begin.get("time_ns") or 0),
                "end_ns": int(event.get("time_ns") or 0),
                "selector_pid": int(begin.get("value") or 0),
                "blocked": bool(begin.get("blocked")),
                "returned_namespace_pid": returned_namespace_pid,
                "returned_pid": returned_pid,
            }
        )

    cycles_by_waiter: dict[tuple[int, int], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, cycle in enumerate(wait_cycles):
        cycles_by_waiter[(cycle["session_id"], cycle["waiter_pid"])].append(
            (index, cycle)
        )

    flow_targets_by_cycle: dict[int, set[int]] = defaultdict(set)
    flow_events_in_cycle = 0
    selector_compatible_events = 0
    boundary_tolerance_ns = 1_000_000
    for event in wait_events:
        session_id, waiter_pid, target_pid = flow_wait_pair(event)
        event_time = int(event.get("time_ns") or 0)
        matching = [
            (index, cycle)
            for index, cycle in cycles_by_waiter.get((session_id, waiter_pid), [])
            if cycle["begin_ns"] - boundary_tolerance_ns
            <= event_time
            <= cycle["end_ns"] + boundary_tolerance_ns
        ]
        if not matching:
            continue
        index, cycle = min(
            matching,
            key=lambda item: max(
                item[1]["begin_ns"] - event_time,
                event_time - item[1]["end_ns"],
                0,
            ),
        )
        flow_events_in_cycle += 1
        if cycle["selector_pid"] <= 0 or cycle["selector_pid"] == target_pid:
            selector_compatible_events += 1
        flow_targets_by_cycle[index].add(target_pid)

    scoped_resolved_cycles = [
        (index, cycle)
        for index, cycle in enumerate(wait_cycles)
        if (cycle["session_id"], cycle["waiter_pid"]) in flow_waiter_keys
        and cycle["blocked"]
        and cycle["returned_pid"] > 0
    ]
    actionable_return_cycles = [
        (index, cycle)
        for index, cycle in scoped_resolved_cycles
        if exit_time.get((cycle["session_id"], cycle["returned_pid"]), 0)
        >= cycle["begin_ns"]
    ]
    covered_actionable_return_cycles = sum(
        cycle["returned_pid"] in flow_targets_by_cycle.get(index, set())
        for index, cycle in actionable_return_cycles
    )
    independent_has_explicit_targets = bool(independent_explicit_wait_pairs)
    independent_wait_summary = {
        "events": len(independent_wait_events),
        "end_events": len(independent_wait_end_events),
        "complete_cycles": len(wait_cycles),
        "blocking_cycles": sum(cycle["blocked"] for cycle in wait_cycles),
        "wait_any_cycles": sum(cycle["selector_pid"] <= 0 for cycle in wait_cycles),
        "explicit_target_cycles": sum(cycle["selector_pid"] > 0 for cycle in wait_cycles),
        "successful_cycles": sum(
            cycle["returned_namespace_pid"] > 0 for cycle in wait_cycles
        ),
        "resolved_return_cycles": sum(cycle["returned_pid"] > 0 for cycle in wait_cycles),
        "explicit_target_events": sum(independent_explicit_wait_pairs.values()),
        "wait_any_events": sum(
            1 for event in independent_wait_events if int(event.get("value") or 0) <= 0
        ),
        "distinct_explicit_pairs": len(independent_explicit_wait_pairs),
        "flow_explicit_events": sum(flow_wait_pairs.values()),
        "flow_distinct_explicit_pairs": len(flow_wait_pairs),
        "matched_flow_events": matched_wait_events,
        "matched_flow_events_percent": (
            percentage(matched_wait_events, sum(flow_wait_pairs.values()))
            if independent_has_explicit_targets
            else None
        ),
        "matched_distinct_pairs": matched_distinct_wait_pairs,
        "matched_distinct_pairs_percent": (
            percentage(matched_distinct_wait_pairs, len(flow_wait_pairs))
            if independent_has_explicit_targets
            else None
        ),
        "flow_events_in_complete_cycle": flow_events_in_cycle,
        "flow_events_in_complete_cycle_percent": percentage(
            flow_events_in_cycle, len(wait_events)
        ),
        "selector_compatible_events": selector_compatible_events,
        "selector_compatible_events_percent": percentage(
            selector_compatible_events, flow_events_in_cycle
        ),
        "scoped_resolved_blocking_cycles": len(scoped_resolved_cycles),
        "already_finished_before_wait_cycles": (
            len(scoped_resolved_cycles) - len(actionable_return_cycles)
        ),
        "actionable_return_cycles": len(actionable_return_cycles),
        "covered_actionable_return_cycles": covered_actionable_return_cycles,
        "covered_actionable_return_cycles_percent": percentage(
            covered_actionable_return_cycles, len(actionable_return_cycles)
        ),
    }

    wait_targets_by_session: dict[int, set[int]] = defaultdict(set)
    for event in wait_events:
        wait_targets_by_session[int(event.get("session_id") or 0)].add(
            int(event.get("related_pid") or 0)
        )

    def summarize_waits(events: list[dict[str, Any]]) -> dict[str, Any]:
        counts = defaultdict(int)
        for event in events:
            session_id = int(event.get("session_id") or 0)
            waiter_pid = int(event.get("pid") or 0)
            target_pid = int(event.get("related_pid") or 0)
            target = (session_id, target_pid)
            waiter = (session_id, waiter_pid)
            counts["events"] += 1
            counts["known_session"] += session_id in sessions
            counts["waiter_present"] += waiter in present
            counts["target_present"] += target in present
            counts["direct_child"] += parents.get(target) == waiter_pid
            counts["descendant"] += is_descendant(
                session_id, target_pid, waiter_pid, parents
            )
            counts["target_runtime_positive"] += runtime[target] > 0
            counts["target_runnable_wait_positive"] += runnable_wait[target] > 0
            event_time = int(event.get("time_ns") or 0)
            counts["event_inside_target_lifetime"] += (
                bool(birth[target])
                and birth[target] <= event_time
                and (not exit_time[target] or event_time <= exit_time[target])
            )
        total = counts["events"]
        return {
            **counts,
            "known_session_percent": percentage(counts["known_session"], total),
            "waiter_present_percent": percentage(counts["waiter_present"], total),
            "target_present_percent": percentage(counts["target_present"], total),
            "direct_child_percent": percentage(counts["direct_child"], total),
            "descendant_percent": percentage(counts["descendant"], total),
            "target_runtime_positive_percent": percentage(
                counts["target_runtime_positive"], total
            ),
            "target_runnable_wait_positive_percent": percentage(
                counts["target_runnable_wait_positive"], total
            ),
            "event_inside_target_lifetime_percent": percentage(
                counts["event_inside_target_lifetime"], total
            ),
        }

    def summarize_promotions(events: list[dict[str, Any]]) -> dict[str, Any]:
        counts = defaultdict(int)
        for event in events:
            session_id = int(event.get("session_id") or 0)
            pid = int(event.get("pid") or 0)
            key = (session_id, pid)
            counts["events"] += 1
            counts["known_session"] += session_id in sessions
            counts["task_present"] += key in present
            counts["runtime_positive"] += runtime[key] > 0
            counts["runnable_wait_positive"] += runnable_wait[key] > 0
            counts["in_observed_wait_tree"] += any(
                is_descendant(session_id, pid, target_pid, parents)
                for target_pid in wait_targets_by_session[session_id]
            )
        total = counts["events"]
        return {
            **counts,
            "known_session_percent": percentage(counts["known_session"], total),
            "task_present_percent": percentage(counts["task_present"], total),
            "runtime_positive_percent": percentage(counts["runtime_positive"], total),
            "runnable_wait_positive_percent": percentage(
                counts["runnable_wait_positive"], total
            ),
            "in_observed_wait_tree_percent": percentage(
                counts["in_observed_wait_tree"], total
            ),
        }

    wait_summary = summarize_waits(wait_events)
    promotion_summary = summarize_promotions(promotion_events)
    group_summary = summarize_promotions(group_promotions)

    stats_by_session = {
        int(item.get("stats", {}).get("session_id") or 0): item.get("stats", {})
        for item in result.get("flowcausal_control", {}).get("sessions", [])
    }
    reported = {
        "wait_links": sum(int(stats.get("wait_links") or 0) for stats in stats_by_session.values()),
        "progress_group_promotions": sum(
            int(stats.get("progress_group_promotions") or 0)
            for stats in stats_by_session.values()
        ),
        "return_root_promotions": sum(
            int(stats.get("return_root_promotions") or 0)
            for stats in stats_by_session.values()
        ),
        "dropped_events": int(result.get("kernel_capture", {}).get("dropped_events") or 0),
    }

    payload = {
        "schema": "flowcausal.v2_identification_check.v1",
        "result_root": str(root),
        "suite_id": result.get("suite_id"),
        "sessions": sessions,
        "independent_capture": {
            "collector": result.get("kernel_capture", {}).get("collector"),
            "tasks": len(present),
            "raw_file": str(capture_path.relative_to(root)),
            "waits": independent_wait_summary,
        },
        "flow_event_lines": parsed_event_lines,
        "unique_flow_events": len(flow_events),
        "wait_links": wait_summary,
        "promotions": promotion_summary,
        "progress_group_promotions": group_summary,
        "return_root_promotion_events": len(root_promotions),
        "reported_counters": reported,
        "scope": (
            "该检查验证同一次运行中的会话归属、父子来源、任务生命周期、"
            "实际运行、可运行等待和阻塞式进程等待的一致性。独立采集器记录"
            "全部已登记任务的阻塞式进程等待；由于第二版有意只接受明确工具"
            "根进程和成组推进任务发起的等待，总量差异不解释为识别遗漏。"
        ),
    }

    output_json = pathlib.Path(args.output_json) if args.output_json else root / "识别核对.json"
    output_markdown = (
        pathlib.Path(args.output_markdown)
        if args.output_markdown
        else root / "识别核对.md"
    )
    output_json.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    lines = [
        "# FlowCausal v2 任务识别核对",
        "",
        f"测试套件：`{result.get('suite_id')}`。候选调度器事件和独立 C/eBPF 记录来自同一次运行。",
        "",
        "| 核对项 | 数量 | 与独立记录一致 |",
        "|---|---:|---:|",
        f"| 阻塞等待关系：等待进程存在 | {wait_summary['events']} | {display_ratio(wait_summary['waiter_present_percent'])} |",
        f"| 阻塞等待关系：被等待进程存在 | {wait_summary['events']} | {display_ratio(wait_summary['target_present_percent'])} |",
        f"| 阻塞等待关系：被等待进程属于等待进程后代 | {wait_summary['events']} | {display_ratio(wait_summary['descendant_percent'])} |",
        f"| 阻塞等待关系：事件位于被等待进程生命周期内 | {wait_summary['events']} | {display_ratio(wait_summary['event_inside_target_lifetime_percent'])} |",
        f"| 阻塞等待关系：位于独立记录的完整等待区间 | {wait_summary['events']} | {display_ratio(independent_wait_summary['flow_events_in_complete_cycle_percent'])} |",
        f"| 区间内候选关系符合等待目标范围 | {independent_wait_summary['flow_events_in_complete_cycle']} | {display_ratio(independent_wait_summary['selector_compatible_events_percent'])} |",
        f"| 等待开始后才结束的返回子进程被候选组包含 | {independent_wait_summary['actionable_return_cycles']} | {display_ratio(independent_wait_summary['covered_actionable_return_cycles_percent'])} |",
        f"| 全部短时推进：任务存在 | {promotion_summary['events']} | {display_ratio(promotion_summary['task_present_percent'])} |",
        f"| 全部短时推进：任务实际运行 | {promotion_summary['events']} | {display_ratio(promotion_summary['runtime_positive_percent'])} |",
        f"| 全部短时推进：任务经历处理器排队 | {promotion_summary['events']} | {display_ratio(promotion_summary['runnable_wait_positive_percent'])} |",
        f"| 成组推进：任务属于已观察等待子树 | {group_summary['events']} | {display_ratio(group_summary['in_observed_wait_tree_percent'])} |",
        "",
        f"内核统计报告 {reported['wait_links']} 条等待关系、{reported['progress_group_promotions']} 次成组推进和 {reported['return_root_promotions']} 次工具根进程推进；独立采集器记录 {independent_wait_summary['complete_cycles']} 个完整等待区间，其中 {independent_wait_summary['blocking_cycles']} 个确实发生睡眠，采集事件丢失数为 {reported['dropped_events']}。",
        "",
        f"在已解析返回进程的相关阻塞等待中，有 {independent_wait_summary['already_finished_before_wait_cycles']} 次等待开始前子进程已经结束，不存在可由处理器调度缩短的执行段，因此不计入候选覆盖率。",
        "",
        "本检查同时核对会话归属、进程来源、生命周期、处理器排队和阻塞式进程等待。第二版有意忽略普通工作进程自己的嵌套等待，因此只报告候选关系的独立确认比例，以及真正包含可调度执行段的返回任务覆盖率，不把全部等待事件总量当作目标。",
        "",
    ]
    output_markdown.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(output_json), "wait_events": len(wait_events), "promotion_events": len(promotion_events)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

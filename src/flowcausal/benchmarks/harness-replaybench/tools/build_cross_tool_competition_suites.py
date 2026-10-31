#!/usr/bin/env python3
"""只依据 scx_simple 的 C/eBPF 画像生成三类跨工具竞争套件。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"
OPPORTUNITY_PATH = ROOT / "datasets" / "HarnessReplayBench-1.0-调度机会画像.json"
# 该任务在组合回放中无法恢复轨迹所要求的 matplotlib 导入环境；这是
# 基线正确性问题，不是调度性能筛选，因此不进入三类正式组合。
COMPOSITION_EXCLUSIONS = {"matplotlib__matplotlib-23314"}


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def raw(profile: dict[str, Any], field: str) -> float:
    return float(profile.get("kernel", {}).get("raw", {}).get(field) or 0)


def effective_parallelism(profile: dict[str, Any]) -> float:
    return float(profile.get("kernel", {}).get("effective_parallelism") or 0)


def support_tasks(
    profiles: list[dict[str, Any]], excluded: set[str], count: int
) -> list[str]:
    candidates = [
        item
        for item in profiles
        if str(item.get("instance_id") or "") not in excluded
        and str(item.get("instance_id") or "") not in COMPOSITION_EXCLUSIONS
        and item.get("status") == "pass"
        and 10.0 <= float(item.get("replay_elapsed_sec") or 0) <= 60.0
        and effective_parallelism(item) >= 1.5
        and raw(item, "cross_generation_window_count") == 0
    ]
    candidates.sort(
        key=lambda item: (
            -effective_parallelism(item),
            -float(item.get("kernel", {}).get("runtime_sec") or 0),
            str(item["instance_id"]),
        )
    )
    return [str(item["instance_id"]) for item in candidates[:count]]


def turnover_support_tasks(
    profiles: list[dict[str, Any]], excluded: set[str], count: int
) -> list[str]:
    candidates = [
        item
        for item in profiles
        if str(item.get("instance_id") or "") not in excluded
        and str(item.get("instance_id") or "") not in COMPOSITION_EXCLUSIONS
        and item.get("status") == "pass"
        and 10.0 <= float(item.get("replay_elapsed_sec") or 0) <= 60.0
        and int(item.get("kernel", {}).get("task_count") or 0) >= 500
        and raw(item, "cross_generation_window_count") == 0
    ]
    candidates.sort(
        key=lambda item: (
            -int(item.get("kernel", {}).get("task_count") or 0),
            -float(item.get("kernel", {}).get("runtime_sec") or 0),
            str(item["instance_id"]),
        )
    )
    return [str(item["instance_id"]) for item in candidates[:count]]


def window_score(window: dict[str, Any]) -> tuple[float, float, float]:
    return (
        float(window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0),
        float(window.get("older_generation_runtime_in_window_lower_bound_sec") or 0),
        float(window.get("elapsed_sec") or 0),
    )


def write_suite(
    short_id: str,
    description: str,
    basis: str,
    instances: list[str],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    task_set_id = f"hrb1-{short_id}-{len(instances)}"
    suite_id = f"hrb1-{short_id}-concurrent-tool"
    task_set = {
        "schema": "harness_replaybench.task_set.v1",
        "task_set_id": task_set_id,
        "description": description,
        "selection_basis": basis + "选择过程未读取候选调度器的性能结果。",
        "selection_evidence": evidence,
        "instances": instances,
    }
    task_set_path = ROOT / "tasksets" / "v1" / f"{short_id}.json"
    suite = {
        "schema": "harness_replaybench.suite_definition.v1",
        "suite_id": suite_id,
        "description": description + "所有任务同时启动。",
        "benchmark_layer": "real-tool-composition",
        "task_set_file": task_set_path.relative_to(ROOT).as_posix(),
        "release_profile_file": "release-profiles/v1/concurrent.json",
        "replay_mode": "tool-execution",
        "cpu_set": "0-7",
    }
    write_json(task_set_path, task_set)
    write_json(ROOT / "suite-definitions" / "v1" / f"{suite_id}.json", suite)
    return {
        "class_id": short_id,
        "suite_id": suite_id,
        "task_set_id": task_set_id,
        "instances": instances,
        "selection_evidence": evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="由 scx_simple 的 C/eBPF 画像生成三类跨工具竞争套件"
    )
    parser.add_argument("--profiles", default=str(PROFILE_PATH))
    parser.add_argument("--opportunities", default=str(OPPORTUNITY_PATH))
    parser.add_argument(
        "--report",
        default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-三类跨工具竞争.json"),
    )
    args = parser.parse_args()

    profile_payload = read_json(pathlib.Path(args.profiles))
    opportunity_payload = read_json(pathlib.Path(args.opportunities))
    profiles = [
        item
        for item in profile_payload.get("profiles", [])
        if isinstance(item, dict) and item.get("status") == "pass"
    ]
    windows = [
        item
        for item in opportunity_payload.get("tool_windows", [])
        if isinstance(item, dict)
    ]

    return_candidates = [
        item
        for item in windows
        if item.get("target_cross_tool_window")
        and int(item.get("return_cohort_task_count") or 0) >= 2
    ]
    if not return_candidates:
        raise RuntimeError("没有找到返回任务群与前序工具竞争的基线窗口")
    return_window = max(return_candidates, key=window_score)
    return_anchor = str(return_window["instance_id"])

    build_candidates = [
        item
        for item in windows
        if item.get("confirmed_cross_tool_contention")
        and str(item.get("stage") or "") == "BUILD"
    ]
    if not build_candidates:
        raise RuntimeError("没有找到前序工具与后续构建竞争的基线窗口")
    build_window = max(build_candidates, key=window_score)
    build_anchor = str(build_window["instance_id"])

    followup_candidates = [
        item
        for item in windows
        if item.get("target_cross_tool_window")
        and str(item.get("instance_id") or "") == return_anchor
        and str(item.get("command_id") or "")
        != str(return_window.get("command_id") or "")
        and int(item.get("return_cohort_task_count") or 0) <= 1
    ]
    if not followup_candidates:
        raise RuntimeError("没有找到前序计算后代与短后续工具竞争的基线窗口")
    followup_window = max(
        followup_candidates,
        key=lambda item: (
            float(item.get("older_generation_runtime_in_window_lower_bound_sec") or 0),
            float(item.get("owned_runnable_wait_in_window_lower_bound_sec") or 0),
        ),
    )

    return_support = support_tasks(profiles, {return_anchor}, 3)
    build_support = support_tasks(profiles, {build_anchor}, 3)
    followup_support = turnover_support_tasks(
        profiles, {return_anchor, *return_support}, 3
    )
    if len(return_support) < 3 or len(build_support) < 3 or len(followup_support) < 3:
        raise RuntimeError("并行支持任务不足")

    classes = [
        write_suite(
            "cross-tool-return-group",
            "前序数据处理后代与后续返回任务群竞争的真实工具组合。",
            "锚点要求 scx_simple 单任务 C/eBPF 画像同时确认前序进程运行、当前任务等待处理器和至少两个返回成员；其余任务按有效并行度选择。",
            [return_anchor, *return_support],
            {
                "anchor_instance": return_anchor,
                "command_id": return_window.get("command_id"),
                "stage": return_window.get("stage"),
                "older_runtime_lower_bound_sec": return_window.get(
                    "older_generation_runtime_in_window_lower_bound_sec"
                ),
                "current_wait_lower_bound_sec": return_window.get(
                    "owned_runnable_wait_in_window_lower_bound_sec"
                ),
                "return_cohort_tasks": return_window.get("return_cohort_task_count"),
            },
        ),
        write_suite(
            "cross-tool-short-followup",
            "前序数据处理后代与后续短检查竞争的真实工具组合。",
            "锚点要求 scx_simple 单任务 C/eBPF 画像确认前序进程在短后续工具窗口内运行、当前工具等待处理器且返回成员为单进程；其余任务按进程创建数量选择。",
            [return_anchor, *followup_support],
            {
                "anchor_instance": return_anchor,
                "command_id": followup_window.get("command_id"),
                "stage": followup_window.get("stage"),
                "older_runtime_lower_bound_sec": followup_window.get(
                    "older_generation_runtime_in_window_lower_bound_sec"
                ),
                "current_wait_lower_bound_sec": followup_window.get(
                    "owned_runnable_wait_in_window_lower_bound_sec"
                ),
                "return_cohort_tasks": followup_window.get("return_cohort_task_count"),
            },
        ),
        write_suite(
            "cross-tool-build-followup",
            "前序测试残留进程与后续构建竞争的真实工具组合。",
            "锚点要求 scx_simple 单任务 C/eBPF 画像在构建窗口内同时确认前序进程运行和当前任务等待处理器；其余任务按有效并行度选择。",
            [build_anchor, *build_support],
            {
                "anchor_instance": build_anchor,
                "command_id": build_window.get("command_id"),
                "stage": build_window.get("stage"),
                "older_runtime_lower_bound_sec": build_window.get(
                    "older_generation_runtime_in_window_lower_bound_sec"
                ),
                "current_wait_lower_bound_sec": build_window.get(
                    "owned_runnable_wait_in_window_lower_bound_sec"
                ),
                "older_live_tasks": build_window.get("older_generation_live_tasks"),
            },
        ),
    ]

    report = {
        "schema": "harness_replaybench.cross_tool_competition_classes.v1",
        "reference_scheduler": "scx_simple",
        "capture": "C/eBPF",
        "selection_inputs": [
            pathlib.Path(args.profiles).resolve().relative_to(ROOT.resolve()).as_posix(),
            pathlib.Path(args.opportunities).resolve().relative_to(ROOT.resolve()).as_posix(),
        ],
        "candidate_scheduler_results_used": False,
        "classes": classes,
    }
    write_json(pathlib.Path(args.report), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

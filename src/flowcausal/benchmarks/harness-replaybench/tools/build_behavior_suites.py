#!/usr/bin/env python3
"""依据执行结构画像生成任务集合和对应测试套件定义。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any, Callable


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUSIONS = ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def raw(profile: dict[str, Any], field: str) -> float:
    return float(profile.get("kernel", {}).get("raw", {}).get(field) or 0)


def kernel(profile: dict[str, Any], field: str) -> float:
    return float(profile.get("kernel", {}).get(field) or 0)


def bounded_duration(profile: dict[str, Any]) -> bool:
    """机制比较保留真实长任务，但避免单条超长轨迹吞没整轮实验。"""
    return float(profile.get("replay_elapsed_sec") or 0) <= 300.0


def select(
    profiles: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
    key: Callable[[dict[str, Any]], tuple[Any, ...]],
    limit: int,
) -> list[str]:
    candidates = [item for item in profiles if predicate(item)]
    candidates.sort(key=key)
    return [str(item["instance_id"]) for item in candidates[:limit]]


def main() -> int:
    parser = argparse.ArgumentParser(description="由 C/eBPF 行为画像生成 HarnessReplayBench 测试套件")
    parser.add_argument("--profiles", default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"))
    parser.add_argument("--limit", type=int, default=4)
    args = parser.parse_args()
    if args.limit < 2:
        raise SystemExit("--limit 至少为 2")
    payload = read_json(pathlib.Path(args.profiles))
    exclusion_payload = read_json(EXCLUSIONS) if EXCLUSIONS.is_file() else {"items": []}
    exclusions = {
        str(item.get("instance_id") or "")
        for item in exclusion_payload.get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    }
    profiles = [
        item
        for item in payload.get("profiles", [])
        if isinstance(item, dict)
        and item.get("status") == "pass"
        and str(item.get("instance_id") or "") not in exclusions
    ]

    causal_overlap = select(
        profiles,
        lambda item: bounded_duration(item) and raw(item, "cross_generation_window_count") > 0,
        lambda item: (
            -raw(item, "cross_generation_overlap_task_seconds"),
            -raw(item, "max_cross_generation_tasks"),
            str(item["instance_id"]),
        ),
        args.limit,
    )
    join_frontier = select(
        profiles,
        lambda item: bounded_duration(item) and raw(item, "max_tool_peak_live_tasks") >= 8,
        lambda item: (
            -raw(item, "critical_tool_peak_live_tasks"),
            -raw(item, "max_tool_peak_live_tasks"),
            str(item["instance_id"]),
        ),
        args.limit,
    )
    causal_mixed: list[str] = []
    for instance in [*causal_overlap[:2], *join_frontier[:2]]:
        if instance not in causal_mixed:
            causal_mixed.append(instance)

    definitions: list[tuple[str, str, str, list[str]]] = [
        (
            "behavior-process-fanout",
            "高进程扩展",
            "选择 C/eBPF 观测到至少一千个内核任务的回放，按任务数从高到低排列。",
            select(
                profiles,
                lambda item: bounded_duration(item) and kernel(item, "task_count") >= 1000,
                lambda item: (-kernel(item, "task_count"), str(item["instance_id"])),
                args.limit,
            ),
        ),
        (
            "behavior-parallel",
            "并行执行",
            "选择累计 CPU 运行时间与墙钟时间之比不低于 1.15 的回放，按有效并行度从高到低排列。",
            select(
                profiles,
                lambda item: bounded_duration(item) and kernel(item, "effective_parallelism") >= 1.15,
                lambda item: (-kernel(item, "effective_parallelism"), str(item["instance_id"])),
                args.limit,
            ),
        ),
        (
            "behavior-runnable-contention",
            "可运行队列竞争",
            "选择累计可运行等待不少于一秒且占运行与等待总时间至少百分之二的回放，按等待时间从高到低排列。",
            select(
                profiles,
                lambda item: bounded_duration(item)
                and kernel(item, "runnable_wait_sec") >= 1.0
                and kernel(item, "runnable_wait_fraction") >= 0.02,
                lambda item: (-kernel(item, "runnable_wait_sec"), str(item["instance_id"])),
                args.limit,
            ),
        ),
        (
            "behavior-cross-tool-survival",
            "跨工具存活",
            "选择工具产生的进程在工具返回后继续存活并跨过下一工具开始时刻的真实回放，按跨工具任务数从高到低排列。",
            select(
                profiles,
                lambda item: bounded_duration(item) and raw(item, "tasks_crossing_next_tool") > 0,
                lambda item: (-raw(item, "tasks_crossing_next_tool"), str(item["instance_id"])),
                args.limit,
            ),
        ),
        (
            "behavior-causal-overlap",
            "真实代次重叠",
            "选择当前工具执行期间仍有前序工具后代存活的回放，按存活区间与当前工具区间的累计交叠时间排序。",
            causal_overlap,
        ),
        (
            "behavior-join-frontier",
            "并行汇合",
            "选择单个工具代次内至少八个进程同时存活的回放，优先保留关键命令内部并行峰值更高的任务。",
            join_frontier,
        ),
        (
            "behavior-causal-mixed",
            "因果执行混合",
            "稳定组合真实代次重叠和并行汇合任务，用于同时观察跨工具后代推进与工具内部并行完成。",
            causal_mixed,
        ),
        (
            "behavior-long-running",
            "长时间运行",
            "选择单任务工具执行回放时间不少于三十秒的任务，按持续时间从高到低排列。",
            select(
                profiles,
                lambda item: 30.0 <= float(item.get("replay_elapsed_sec") or 0) <= 300.0,
                lambda item: (-float(item.get("replay_elapsed_sec") or 0), str(item["instance_id"])),
                args.limit,
            ),
        ),
        (
            "behavior-low-overlap",
            "低重叠参考",
            "选择无显式异步、无跨工具存活、有效并行度低于 1.15 且持续一至三十秒的回放，按任务编号稳定排序。",
            select(
                profiles,
                lambda item: int(item.get("trace", {}).get("explicit_async_commands") or 0) == 0
                and raw(item, "tasks_crossing_next_tool") == 0
                and kernel(item, "effective_parallelism") < 1.15
                and 1.0 <= float(item.get("replay_elapsed_sec") or 0) < 30.0,
                lambda item: (str(item["instance_id"]),),
                args.limit,
            ),
        ),
    ]

    written: list[str] = []
    for short_id, description, basis, instances in definitions:
        if len(instances) < 2:
            continue
        task_set_id = f"hrb1-{short_id}-{len(instances)}"
        task_set_path = ROOT / "tasksets" / "v1" / f"{short_id}.json"
        write_json(
            task_set_path,
            {
                "schema": "harness_replaybench.task_set.v1",
                "task_set_id": task_set_id,
                "description": f"依据 HarnessReplayBench 1.0 单任务执行画像形成的{description}任务集合。",
                "selection_basis": basis + "选择过程不读取任何被测调度器的性能结果。",
                "instances": instances,
            },
        )
        suite_id = f"hrb1-{short_id}-concurrent-tool"
        suite_path = ROOT / "suite-definitions" / "v1" / f"{suite_id}.json"
        write_json(
            suite_path,
            {
                "schema": "harness_replaybench.suite_definition.v1",
                "suite_id": suite_id,
                "description": f"{description}任务的工具执行同时启动套件。",
                "benchmark_layer": "real-tool-composition",
                "task_set_file": task_set_path.relative_to(ROOT).as_posix(),
                "release_profile_file": "release-profiles/v1/concurrent.json",
                "replay_mode": "tool-execution",
                "cpu_set": "0-7",
            },
        )
        written.append(suite_id)

    print(json.dumps({"behavior_suites": len(written), "suite_ids": written}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

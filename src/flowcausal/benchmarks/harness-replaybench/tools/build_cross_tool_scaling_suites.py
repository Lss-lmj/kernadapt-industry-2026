#!/usr/bin/env python3
"""为已确认的跨工具竞争组合生成并发规模与处理器数量实验。"""
from __future__ import annotations

import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLASSES = ROOT / "datasets" / "HarnessReplayBench-1.0-三类跨工具竞争.json"
PROFILES = ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"
REPORT = ROOT / "datasets" / "HarnessReplayBench-1.0-并发与处理器缩放.json"
TARGET_CLASSES = {"cross-tool-return-group", "cross-tool-build-followup"}
EXCLUDED = {"matplotlib__matplotlib-23314"}


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


def effective_parallelism(profile: dict[str, Any]) -> float:
    return float(profile.get("kernel", {}).get("effective_parallelism") or 0)


def raw(profile: dict[str, Any], field: str) -> float:
    return float(profile.get("kernel", {}).get("raw", {}).get(field) or 0)


def additional_instances(
    profiles: list[dict[str, Any]], current: list[str], count: int
) -> list[str]:
    excluded = {*current, *EXCLUDED}
    candidates = [
        item
        for item in profiles
        if item.get("status") == "pass"
        and str(item.get("instance_id") or "") not in excluded
        and 10.0 <= float(item.get("replay_elapsed_sec") or 0) <= 60.0
        and raw(item, "cross_generation_window_count") == 0
    ]
    candidates.sort(
        key=lambda item: (
            -effective_parallelism(item),
            -float(item.get("kernel", {}).get("runtime_sec") or 0),
            str(item.get("instance_id") or ""),
        )
    )
    selected = [str(item["instance_id"]) for item in candidates[:count]]
    if len(selected) != count:
        raise RuntimeError(f"可运行的扩展任务不足：需要 {count}，只有 {len(selected)}")
    return selected


def write_task_set(
    class_id: str, instances: list[str], evidence: dict[str, Any]
) -> pathlib.Path:
    path = ROOT / "tasksets" / "v1" / f"{class_id}-8.json"
    write_json(
        path,
        {
            "schema": "harness_replaybench.task_set.v1",
            "task_set_id": f"hrb1-{class_id}-8",
            "description": "从四任务跨工具竞争组合扩展到八任务并发。",
            "selection_basis": (
                "保留原组合，新增任务只按 scx_simple 的 C/eBPF 行为画像中"
                "的有效并行度和运行时间排序；未读取候选调度器性能结果。"
            ),
            "selection_evidence": evidence,
            "instances": instances,
        },
    )
    return path


def write_suite(
    suite_id: str,
    description: str,
    task_set_path: pathlib.Path,
    cpu_set: str,
) -> pathlib.Path:
    path = ROOT / "suite-definitions" / "v1" / f"{suite_id}.json"
    task_set = read_json(task_set_path)
    write_json(
        path,
        {
            "schema": "harness_replaybench.suite_definition.v1",
            "suite_id": suite_id,
            "description": description,
            "benchmark_layer": "real-tool-composition",
            "task_set_file": task_set_path.relative_to(ROOT).as_posix(),
            "release_profile_file": "release-profiles/v1/concurrent.json",
            "replay_mode": "tool-execution",
            "cpu_set": cpu_set,
        },
    )
    return path


def main() -> int:
    classes = read_json(CLASSES)
    profiles = [
        item
        for item in read_json(PROFILES).get("profiles", [])
        if isinstance(item, dict)
    ]
    experiments: list[dict[str, Any]] = []
    for item in classes.get("classes", []):
        if not isinstance(item, dict):
            continue
        class_id = str(item.get("class_id") or "")
        if class_id not in TARGET_CLASSES:
            continue
        current = [str(value) for value in item.get("instances", [])]
        if len(current) != 4:
            raise RuntimeError(f"{class_id} 的基础组合不是四任务")
        current_task_set = ROOT / "tasksets" / "v1" / f"{class_id}.json"
        extras = additional_instances(profiles, current, 4)
        expanded = [*current, *extras]
        expanded_task_set = write_task_set(
            class_id,
            expanded,
            {
                "base_instances": current,
                "additional_instances": extras,
                "reference_scheduler": "scx_simple",
                "capture": "C/eBPF",
                "candidate_scheduler_results_used": False,
            },
        )
        for processors in (4, 16):
            suffix = f"cpu{processors}"
            suite_id = f"hrb1-{class_id}-{suffix}-concurrent-tool"
            write_suite(
                suite_id,
                f"四任务跨工具竞争组合使用 {processors} 个处理器同时启动。",
                current_task_set,
                f"0-{processors - 1}",
            )
            experiments.append(
                {
                    "class_id": class_id,
                    "dimension": "processors",
                    "level": processors,
                    "suite_id": suite_id,
                    "instances": current,
                    "cpu_set": f"0-{processors - 1}",
                }
            )
        suite_id = f"hrb1-{class_id}-8-concurrent-tool"
        write_suite(
            suite_id,
            "八任务跨工具竞争组合在八个处理器上同时启动。",
            expanded_task_set,
            "0-7",
        )
        experiments.extend(
            [
                {
                    "class_id": class_id,
                    "dimension": "concurrency",
                    "level": 4,
                    "suite_id": item["suite_id"],
                    "instances": current,
                    "cpu_set": "0-7",
                },
                {
                    "class_id": class_id,
                    "dimension": "concurrency",
                    "level": 8,
                    "suite_id": suite_id,
                    "instances": expanded,
                    "cpu_set": "0-7",
                },
                {
                    "class_id": class_id,
                    "dimension": "processors",
                    "level": 8,
                    "suite_id": item["suite_id"],
                    "instances": current,
                    "cpu_set": "0-7",
                },
            ]
        )
    write_json(
        REPORT,
        {
            "schema": "harness_replaybench.cross_tool_scaling.v1",
            "reference_scheduler": "scx_simple",
            "capture": "C/eBPF",
            "candidate_scheduler_results_used": False,
            "selection_inputs": [
                CLASSES.relative_to(ROOT).as_posix(),
                PROFILES.relative_to(ROOT).as_posix(),
            ],
            "experiments": sorted(
                experiments,
                key=lambda value: (
                    value["class_id"],
                    value["dimension"],
                    value["level"],
                ),
            ),
        },
    )
    print(json.dumps({"experiments": len(experiments)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

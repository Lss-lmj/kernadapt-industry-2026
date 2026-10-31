#!/usr/bin/env python3
"""根据正式轨迹和来源任务生成 HarnessReplayBench 1.0 数据索引。"""
from __future__ import annotations

import argparse
import json
import pathlib
from collections import Counter
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUSIONS = ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"文件不是 JSON 对象：{path}")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 HarnessReplayBench 1.0 数据索引")
    parser.add_argument("--require-count", type=int, default=0)
    parser.add_argument("--output", default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-任务索引.json"))
    args = parser.parse_args()

    exclusion_payload = read_json(EXCLUSIONS) if EXCLUSIONS.is_file() else {"items": []}
    exclusions = {
        str(item.get("instance_id") or ""): dict(item)
        for item in exclusion_payload.get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    }
    sources: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for path in sorted((ROOT / "corpus").rglob("*.json")):
        value = read_json(path)
        instance = str(value.get("instance_id") or "")
        if instance in sources:
            raise SystemExit(f"来源任务编号重复：{instance}")
        sources[instance] = (path, value)

    tasks: list[dict[str, Any]] = []
    stages: Counter[str] = Counter()
    frameworks: Counter[str] = Counter()
    collectors: Counter[str] = Counter()
    replay_modes: Counter[str] = Counter()
    repositories: set[str] = set()
    recorded_tool_duration_ns = 0
    instances: set[str] = set()
    for trace_path in sorted((ROOT / "traces" / "formal").glob("*.json")):
        trace = read_json(trace_path)
        if trace.get("schema") != "harness_replaybench.trace.v2":
            continue
        instance = str(trace.get("instance_id") or "")
        if instance in instances:
            raise SystemExit(f"正式轨迹任务编号重复：{instance}")
        instances.add(instance)
        source_path, source = sources.get(instance, (pathlib.Path(), {}))
        if not source:
            raise SystemExit(f"正式轨迹缺少来源任务：{instance}")
        command_stages = Counter(str(item.get("stage") or "") for item in trace.get("commands", []) if isinstance(item, dict))
        task_tool_duration_ns = sum(
            int(item.get("timing", {}).get("tool_duration_ns") or 0)
            for item in trace.get("commands", [])
            if isinstance(item, dict) and isinstance(item.get("timing"), dict)
        )
        recorded_tool_duration_ns += task_tool_duration_ns
        stages.update(command_stages)
        framework = str(trace.get("source_task", {}).get("framework") or "")
        collector = str(trace.get("capture", {}).get("online_collector") or "")
        modes = [str(item) for item in trace.get("replay", {}).get("modes", [])]
        frameworks[framework] += 1
        collectors[collector] += 1
        replay_modes.update(modes)
        repository = str(source.get("repository") or "")
        repositories.add(repository)
        tasks.append(
            {
                "instance_id": instance,
                "repository": repository,
                "category": str(source.get("category") or ""),
                "framework": framework,
                "commands": sum(command_stages.values()),
                "stage_counts": dict(sorted(command_stages.items())),
                "replay_modes": modes,
                "workspace_kind": str(trace.get("workspace", {}).get("kind") or ""),
                "online_collector": collector,
                "recorded_tool_duration_sec": task_tool_duration_ns / 1_000_000_000,
                "trace_file": trace_path.relative_to(ROOT).as_posix(),
                "source_task_file": source_path.relative_to(ROOT).as_posix(),
                "included_in_suites": instance not in exclusions,
                **(
                    {"exclusion": exclusions[instance]}
                    if instance in exclusions
                    else {}
                ),
            }
        )

    if args.require_count and len(tasks) != args.require_count:
        raise SystemExit(f"正式任务为 {len(tasks)} 个，不等于要求的 {args.require_count} 个")
    payload = {
        "schema": "harness_replaybench.dataset_index.v1",
        "benchmark_version": "1.0",
        "summary": {
            "tasks": len(tasks),
            "included_in_suites": sum(item["included_in_suites"] for item in tasks),
            "excluded_from_suites": sum(not item["included_in_suites"] for item in tasks),
            "repositories": len(repositories),
            "commands": sum(stages.values()),
            "recorded_tool_duration_sec": recorded_tool_duration_ns / 1_000_000_000,
            "frameworks": dict(sorted(frameworks.items())),
            "online_collectors": dict(sorted(collectors.items())),
            "replay_modes": dict(sorted(replay_modes.items())),
            "stage_counts": dict(sorted(stages.items())),
        },
        "tasks": tasks,
    }
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

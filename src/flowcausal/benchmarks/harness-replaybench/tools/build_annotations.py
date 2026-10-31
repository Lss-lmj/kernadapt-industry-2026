#!/usr/bin/env python3
"""从 HarnessReplayBench 1.0 轨迹生成独立的离线行为标注。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]


def command_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if isinstance(item, (str, int))]
    return []


def annotate(trace: dict[str, Any], trace_path: pathlib.Path) -> dict[str, Any]:
    commands = [item for item in trace.get("commands", []) if isinstance(item, dict)]
    selected = [item for item in commands if item.get("critical")]
    selection_method = "轨迹导入阶段的启发式工具里程碑"
    if not selected:
        candidates = [item for item in commands if item.get("stage") not in {"PACKAGE_BACKGROUND", "RETRY_PROBE"}]
        tests = [item for item in candidates if item.get("stage") == "TEST"]
        selected = tests[-1:] or candidates[-1:]
        selection_method = "离线选择最后一个测试工具，否则选择最后一个前台工具"
    selected_ids = [str(item["id"]) for item in selected]
    milestones = []
    for item in selected:
        frontier = item.get("frontier_label") if isinstance(item.get("frontier_label"), dict) else {}
        milestones.append(
            {
                "command_id": str(item["id"]),
                "kind": "selected_tool_milestone",
                "source": str(frontier.get("source") or "offline-heuristic"),
                "policy": str(frontier.get("critical_policy") or selection_method),
                "confidence": "heuristic",
                "is_workflow_completion": False,
                "is_critical_path_measurement": False,
            }
        )

    joins: list[dict[str, Any]] = []
    for command in commands:
        dependencies: list[str] = []
        for key in ("depends_on", "wait_for", "wait_for_commands"):
            dependencies.extend(command_ids(command.get(key)))
        if dependencies:
            joins.append(
                {
                    "command_id": str(command.get("id") or ""),
                    "predecessors": sorted(set(dependencies)),
                }
            )
    background = [
        str(item["id"])
        for item in commands
        if bool(item.get("async"))
        or bool(item.get("asynchronous"))
        or bool(item.get("background"))
    ]
    command_positions = {
        str(item.get("id") or ""): index
        for index, item in enumerate(commands)
        if item.get("id") is not None
    }
    critical_positions = [
        command_positions[identifier]
        for identifier in selected_ids
        if identifier in command_positions
    ]
    last_critical_position = max(critical_positions, default=-1)
    post_critical = [
        str(item["id"])
        for index, item in enumerate(commands)
        if index > last_critical_position and item.get("id") is not None
    ]
    artifact_commands = [
        str(item["id"])
        for item in commands
        if item.get("stage") == "PACKAGE_BACKGROUND" and item.get("id") is not None
    ]
    try:
        rel = trace_path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        rel = trace_path.name
    return {
        "schema": "harness_replaybench.annotation.v1",
        "instance_id": str(trace.get("instance_id") or trace_path.stem),
        "trace_file": rel,
        "method": selection_method,
        "workflow_completion": {
            "kind": "all_replayed_tools_returned",
            "source": "runner_workload_end",
            "confidence": "observed",
        },
        "selected_tool_milestones": milestones,
        "synchronous_tool_commands": [
            str(item["id"])
            for item in commands
            if item.get("id") is not None
            and not bool(item.get("async") or item.get("asynchronous") or item.get("background"))
        ],
        "critical_commands": selected_ids,
        "join_points": joins,
        "background_commands": background,
        "post_critical_commands": post_critical,
        "artifact_commands": artifact_commands,
        "notes": [
            "异步执行、关键完成点之后的命令和产物整理分别记录，三者不再混为一类。",
            "critical_commands 仅为兼容旧结果保留，其内容是启发式工具里程碑，不是工作流关键路径。",
            "完整本地工作流完成时间以运行器 workload_end 为准；同步工具从发起到返回的时间是智能体可见的本地阻塞时间。",
            "该文件只用于运行后的行为分析和理想方法，不提供给在线被测系统。",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 HarnessReplayBench 离线行为标注")
    parser.add_argument("--trace-root", default=str(ROOT / "traces" / "formal"))
    parser.add_argument("--output-root", default=str(ROOT / "annotations" / "formal"))
    args = parser.parse_args()
    trace_root = pathlib.Path(args.trace_root).resolve()
    output_root = pathlib.Path(args.output_root).resolve()
    outputs: list[str] = []
    for path in sorted(trace_root.glob("*.json")):
        trace = json.loads(path.read_text(encoding="utf-8"))
        if trace.get("schema") != "harness_replaybench.trace.v2":
            continue
        payload = annotate(trace, path)
        output = output_root / f"{payload['instance_id']}.annotation.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        outputs.append(output.as_posix())
    print(json.dumps({"annotations": len(outputs), "outputs": outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

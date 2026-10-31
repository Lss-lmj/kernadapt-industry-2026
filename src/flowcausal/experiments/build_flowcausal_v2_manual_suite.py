#!/usr/bin/env python3
"""为 V2 人工指定路线生成独立实验清单，不修改正式轨迹。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


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


def select_commands(opportunities: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """每个工作流选择可运行等待下界最大的强候选同步工具。"""
    selected: dict[str, dict[str, Any]] = {}
    for window in opportunities.get("tool_windows", []):
        if not isinstance(window, dict):
            continue
        instance = str(window.get("instance_id") or "")
        if (
            not instance
            or not bool(window.get("synchronous", True))
            or str(window.get("opportunity_level") or "") != "强候选"
        ):
            continue
        score = float(window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0)
        current = selected.get(instance)
        if current is None or score > float(current["score"]):
            selected[instance] = {
                "command_id": str(window.get("command_id") or ""),
                "stage": str(window.get("stage") or ""),
                "score": score,
                "elapsed_sec": float(window.get("elapsed_sec") or 0),
                "confirmed_cross_tool_contention": bool(
                    window.get("confirmed_cross_tool_contention", False)
                ),
            }
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 V2 人工指定路线实验清单")
    parser.add_argument("--base-manifest", required=True)
    parser.add_argument("--opportunities", required=True)
    parser.add_argument("--output-root", required=True)
    args = parser.parse_args()

    base_path = pathlib.Path(args.base_manifest).resolve()
    base = read_json(base_path)
    selected = select_commands(read_json(pathlib.Path(args.opportunities).resolve()))
    output = pathlib.Path(args.output_root).resolve()
    traces = output / "traces"
    cases: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []

    for case in base.get("cases", []):
        if not isinstance(case, dict):
            continue
        instance = str(case.get("instance_id") or "")
        choice = selected.get(instance)
        if choice is None:
            raise RuntimeError(f"工作流没有可用的人工核对候选：{instance}")
        trace_path = pathlib.Path(str(case["trace_file"]))
        if not trace_path.is_absolute():
            trace_path = base_path.parents[3] / trace_path
            if not trace_path.is_file():
                trace_path = pathlib.Path(__file__).resolve().parents[1] / "benchmarks" / "harness-replaybench" / str(case["trace_file"])
        trace = read_json(trace_path.resolve())
        matched = 0
        command_text = ""
        for command in trace.get("commands", []):
            if not isinstance(command, dict) or str(command.get("id") or "") != choice["command_id"]:
                continue
            command["frontier_label"] = {
                "source": "manual-profile-review",
                "critical_policy": (
                    "在独立 scx_simple 单任务记录中逐工具核对，选择可运行等待下界最大的强候选同步工具"
                ),
            }
            command_text = str(command.get("normalized_cmd") or command.get("cmd") or "")
            matched += 1
        if matched != 1:
            raise RuntimeError(
                f"{instance} 的人工候选匹配数量为 {matched}：{choice['command_id']}"
            )
        derived_trace = traces / trace_path.name
        write_json(derived_trace, trace)
        derived_case = dict(case)
        derived_case["trace_file"] = str(derived_trace)
        cases.append(derived_case)
        records.append(
            {
                "instance_id": instance,
                **choice,
                "command": command_text,
                "source_trace": str(trace_path.resolve()),
                "derived_trace": str(derived_trace),
            }
        )

    manifest = dict(base)
    # 调度器无关的工作负载身份保持不变，便于复用同一组通用基线。
    # 人工标注属于对照方法的输入，单独记录在 comparison_variant 和
    # manual_policy_selection 中，不改写正式轨迹与工作负载编号。
    manifest["suite_id"] = str(base.get("suite_id") or "suite")
    manifest["comparison_variant"] = "manual-policy"
    manifest["description"] = (
        "与原组合相同；每个工作流仅把独立 scx_simple 行为记录中人工核对的一个同步工具标给人工指定路线。"
    )
    manifest["cases"] = cases
    manifest["manual_policy_selection"] = {
        "source": "independent-scx-simple-single-workflow-profiles",
        "rule": "逐工作流核对强候选同步工具，选择窗口内可运行等待下界最大者",
        "uses_flowcausal_performance": False,
        "records": records,
    }
    write_json(output / "manifest.json", manifest)
    write_json(
        output / "人工指定记录.json",
        {
            "schema": "flowcausal.v2.manual-policy-selection.v1",
            "base_manifest": str(base_path),
            "records": records,
        },
    )
    print(json.dumps({"manifest": str(output / "manifest.json"), "selected": len(records)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

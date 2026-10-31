#!/usr/bin/env python3
"""由真实 C/eBPF 采集记录生成同步工具返回测试套件。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILES = ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"
EXCLUSIONS = ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
OUTPUT = ROOT / "datasets" / "HarnessReplayBench-1.0-同步工具调度测试任务.json"
SIZES = (4, 8, 12, 16)


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


def strong_window(window: dict[str, Any]) -> bool:
    return (
        float(window.get("elapsed_sec") or 0) >= 0.1
        and float(window.get("owned_runnable_wait_sec") or 0) >= 0.05
        and float(window.get("owned_runnable_wait_fraction") or 0) >= 0.02
        and (
            int(window.get("owned_peak_live_tasks") or 0) >= 4
            or int(window.get("return_cohort_task_count") or 0) >= 2
        )
    )


def formal_traces() -> dict[str, tuple[pathlib.Path, dict[str, Any]]]:
    traces: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for path in sorted((ROOT / "traces" / "formal").glob("*.json")):
        trace = read_json(path)
        if trace.get("schema") != "harness_replaybench.trace.v2":
            continue
        instance = str(trace.get("instance_id") or "")
        if not instance:
            continue
        if instance in traces:
            raise RuntimeError(f"正式轨迹中出现重复任务：{instance}")
        traces[instance] = (path, trace)
    return traces


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成只含真实 C/eBPF 源轨迹的同步工具返回测试套件"
    )
    parser.add_argument("--max-source-workflow-sec", type=float, default=60.0)
    args = parser.parse_args()
    if args.max_source_workflow_sec <= 0:
        raise SystemExit("--max-source-workflow-sec 必须大于零")

    traces = formal_traces()
    profile_payload = read_json(PROFILES)
    profiles = {
        str(item.get("instance_id") or ""): item
        for item in profile_payload.get("profiles", [])
        if isinstance(item, dict)
    }
    exclusion_payload = read_json(EXCLUSIONS)
    exclusions = {
        str(item.get("instance_id") or "")
        for item in exclusion_payload.get("items", [])
        if isinstance(item, dict)
    }

    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    for instance, (trace_path, trace) in traces.items():
        if instance in exclusions:
            rejected.append({"instance_id": instance, "reason": "已列入回放排除项"})
            continue
        if trace.get("capture", {}).get("online_collector") != "C/eBPF":
            rejected.append({"instance_id": instance, "reason": "源轨迹不是 C/eBPF 采集"})
            continue
        if any(bool(command.get("async")) for command in trace.get("commands", [])):
            rejected.append({"instance_id": instance, "reason": "含显式异步工具命令"})
            continue
        profile = profiles.get(instance)
        if not profile or profile.get("status") != "pass":
            rejected.append({"instance_id": instance, "reason": "缺少通过的单任务行为画像"})
            continue
        kernel = profile.get("kernel") if isinstance(profile.get("kernel"), dict) else {}
        if kernel.get("collector") != "C/eBPF" or not kernel.get("complete"):
            rejected.append({"instance_id": instance, "reason": "单任务内核画像不完整"})
            continue
        workflow_sec = float(profile.get("workflow_completion_sec") or 0)
        if workflow_sec <= 0 or workflow_sec > args.max_source_workflow_sec:
            rejected.append({"instance_id": instance, "reason": "单任务执行时间超出测试上限"})
            continue
        windows = [
            item
            for item in kernel.get("raw", {}).get("tool_windows", [])
            if isinstance(item, dict)
        ]
        strong = [item for item in windows if strong_window(item)]
        eligible.append(
            {
                "instance_id": instance,
                "trace_file": trace_path.relative_to(ROOT).as_posix(),
                "source_framework": str(trace.get("source_task", {}).get("framework") or ""),
                "source_workflow_sec": workflow_sec,
                "cpu_runtime_sec": float(kernel.get("runtime_sec") or 0),
                "effective_parallelism": float(kernel.get("effective_parallelism") or 0),
                "runnable_wait_sec": float(kernel.get("runnable_wait_sec") or 0),
                "strong_window_count": len(strong),
                "strong_window_runnable_wait_sec": sum(
                    float(item.get("owned_runnable_wait_sec") or 0) for item in strong
                ),
                "return_cohort_wait_sec": sum(
                    float(item.get("return_cohort_lifetime_runnable_wait_sec") or 0)
                    for item in windows
                ),
            }
        )

    # 先选择在同步工具内部确实出现过返回前任务等待的记录；其余位置按
    # CPU 运行时间补齐，使同时启动时自然形成处理器竞争。这里不读取
    # FlowCausal 或其他候选调度器的任何结果。
    eligible.sort(
        key=lambda item: (
            int(item["strong_window_count"]) == 0,
            -float(item["strong_window_runnable_wait_sec"]),
            -float(item["cpu_runtime_sec"]),
            str(item["instance_id"]),
        )
    )
    if len(eligible) < max(SIZES):
        raise SystemExit(
            f"符合条件的真实 C/eBPF 任务只有 {len(eligible)} 个，少于 {max(SIZES)} 个"
        )

    for size in SIZES:
        instances = [str(item["instance_id"]) for item in eligible[:size]]
        task_set = ROOT / "tasksets" / "v1" / f"synchronous-return-{size}.json"
        suite = (
            ROOT
            / "suite-definitions"
            / "v1"
            / f"hrb1-synchronous-return-{size}-concurrent-tool.json"
        )
        write_json(
            task_set,
            {
                "schema": "harness_replaybench.task_set.v1",
                "task_set_id": f"hrb1-synchronous-return-{size}",
                "description": f"{size} 个真实采集的同步工具任务，用于观察智能体等待工具返回时的处理器调度。",
                "selection_basis": "只使用 scx_simple 下的真实 C/eBPF 源轨迹及其单任务画像；先按同步工具内部的可运行等待量排序，再按 CPU 运行时间补齐；不读取候选调度器结果。",
                "instances": instances,
            },
        )
        write_json(
            suite,
            {
                "schema": "harness_replaybench.suite_definition.v1",
                "suite_id": f"hrb1-synchronous-return-{size}-concurrent-tool",
                "description": f"{size} 个真实智能体同步工具执行任务同时启动，固定使用 CPU 0–7。",
                "benchmark_layer": "general-workloads",
                "task_set_file": task_set.relative_to(ROOT).as_posix(),
                "release_profile_file": "release-profiles/v1/concurrent.json",
                "replay_mode": "tool-execution",
                "cpu_set": "0-7",
            },
        )

    payload = {
        "schema": "harness_replaybench.synchronous_return_selection.v1",
        "version": "1.0",
        "reference_scheduler": "scx_simple",
        "source_capture": "C/eBPF",
        "source_cpu_set": "0-7",
        "maximum_source_workflow_sec": args.max_source_workflow_sec,
        "selection_rule": "同步工具中有可运行等待的任务优先，其余按单任务 CPU 运行时间补齐；只使用基线数据。",
        "suite_sizes": list(SIZES),
        "eligible": eligible,
        "rejected": sorted(rejected, key=lambda item: item["instance_id"]),
    }
    write_json(OUTPUT, payload)
    print(
        json.dumps(
            {
                "output": str(OUTPUT),
                "eligible": len(eligible),
                "suite_sizes": list(SIZES),
                "largest_suite": [item["instance_id"] for item in eligible[: max(SIZES)]],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

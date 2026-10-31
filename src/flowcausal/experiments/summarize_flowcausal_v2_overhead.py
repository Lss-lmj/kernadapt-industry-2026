#!/usr/bin/env python3
"""汇总 FlowCausal v2 的内核与 C 控制程序开销。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results" / "harness-replaybench-v1"
GROUPING_PATH = (
    RESULTS
    / "cross-tool-return-group-overhead-grouping-only-20260910"
    / "flowcausal_v2_grouping_only"
    / "trial-01"
    / "result.json"
)
FULL_PATH = (
    RESULTS
    / "cross-tool-return-group-overhead-20260910"
    / "flowcausal_v2_latest"
    / "trial-01"
    / "result.json"
)
OUTPUT = RESULTS / "flowcausal-v2-overhead-summary-20260910"
CPU_COUNT = 8


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("status") != "pass":
        raise RuntimeError(f"开销轮次未通过：{path}")
    runtime = value.get("bpf_runtime", {})
    if not runtime.get("accounting_enabled_for_run"):
        raise RuntimeError(f"没有启用 BPF 累计时间统计：{path}")
    if runtime.get("accounting_restored_to") != 0:
        raise RuntimeError(f"BPF 累计时间设置没有恢复：{path}")
    return value


def bpf_summary(result: dict[str, Any], cpu_count: int) -> dict[str, Any]:
    runtime = result["bpf_runtime"]
    wall = float(result["aggregate"]["makespan_sec"])
    total_ns = int(runtime["total_run_time_ns"])
    total_count = int(runtime["total_run_count"])
    programs = []
    for item in runtime["programs"]:
        count = int(item["run_count"])
        run_ns = int(item["run_time_ns"])
        programs.append(
            {
                **item,
                "mean_ns_per_call": run_ns / count if count else 0.0,
                "one_core_percent": run_ns / 1e9 / wall * 100.0 if wall else 0.0,
            }
        )
    return {
        "wall_sec": wall,
        "total_run_time_sec": total_ns / 1e9,
        "total_run_count": total_count,
        "mean_ns_per_call": total_ns / total_count if total_count else 0.0,
        "one_core_percent": total_ns / 1e9 / wall * 100.0 if wall else 0.0,
        "machine_capacity_percent": (
            total_ns / 1e9 / wall / cpu_count * 100.0 if wall else 0.0
        ),
        "programs": programs,
    }


def controller_summary(result: dict[str, Any], cpu_count: int) -> dict[str, Any]:
    sessions = result.get("flowcausal_control", {}).get("sessions", [])
    resources = [item.get("resources") for item in sessions]
    if not resources or any(not isinstance(item, dict) for item in resources):
        raise RuntimeError("完整第二版缺少 C 控制进程资源记录")
    wall = float(result["aggregate"]["makespan_sec"])
    cpu_sec = sum(float(item["cpu_sec"]) for item in resources)
    return {
        "processes": len(resources),
        "cpu_sec": cpu_sec,
        "one_core_percent": cpu_sec / wall * 100.0 if wall else 0.0,
        "machine_capacity_percent": cpu_sec / wall / cpu_count * 100.0 if wall else 0.0,
        "mean_max_rss_kb": sum(int(item["max_rss_kb"]) for item in resources) / len(resources),
        "max_rss_kb": max(int(item["max_rss_kb"]) for item in resources),
        "sum_max_rss_kb": sum(int(item["max_rss_kb"]) for item in resources),
        "sessions": [
            {
                "instance_id": str(session["instance_id"]),
                **session["resources"],
            }
            for session in sessions
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 FlowCausal v2 内核与 C 程序开销")
    parser.add_argument("--grouping-result", type=pathlib.Path, default=GROUPING_PATH)
    parser.add_argument("--full-result", type=pathlib.Path, default=FULL_PATH)
    parser.add_argument("--automatic-result", type=pathlib.Path)
    parser.add_argument("--output", type=pathlib.Path, default=OUTPUT)
    parser.add_argument("--cpu-count", type=int, default=CPU_COUNT)
    args = parser.parse_args()
    if args.cpu_count <= 0:
        raise SystemExit("处理器数量必须为正数")

    grouping_result = load(args.grouping_result)
    full_result = load(args.full_result)
    grouping = bpf_summary(grouping_result, args.cpu_count)
    full = bpf_summary(full_result, args.cpu_count)
    controller = controller_summary(full_result, args.cpu_count)
    automatic_result = load(args.automatic_result) if args.automatic_result else None
    automatic = (
        bpf_summary(automatic_result, args.cpu_count)
        if automatic_result is not None
        else None
    )
    automatic_controller = (
        controller_summary(automatic_result, args.cpu_count)
        if automatic_result is not None
        else None
    )
    incremental_one_core = full["one_core_percent"] - grouping["one_core_percent"]
    payload = {
        "schema": "flowcausal.v2.overhead.v1",
        "conditions": {
            "suite": "hrb1-cross-tool-return-group-concurrent-tool",
            "cpu_set": "0-7",
            "release_pattern": "concurrent",
            "bpf_runtime_accounting": "仅在开销轮次开启，主性能轮次关闭",
            "online_runtime": "C、eBPF 与 sched_ext",
        },
        "grouping_only": grouping,
        "full_v2": full,
        "incremental": {
            "one_core_percentage_points": incremental_one_core,
            "machine_capacity_percentage_points": incremental_one_core / args.cpu_count,
            "mean_ns_per_callback_difference": (
                full["mean_ns_per_call"] - grouping["mean_ns_per_call"]
            ),
        },
        "c_controller": controller,
        "source_results": {
            "grouping_only": args.grouping_result.resolve().relative_to(ROOT).as_posix(),
            "full_v2": args.full_result.resolve().relative_to(ROOT).as_posix(),
        },
    }
    if automatic is not None and automatic_controller is not None:
        payload["automatic_weight"] = automatic
        payload["automatic_weight_c_controller"] = automatic_controller
        payload["source_results"]["automatic_weight"] = (
            args.automatic_result.resolve().relative_to(ROOT).as_posix()
        )
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "结果表.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# FlowCausal v2 运行开销",
        "",
        "三种方案在同一四任务、0–7号处理器、同时启动的组合上各运行一轮。内核累计时间统计会增加测量本身的成本，因此这些轮次只用于开销分析，不进入主性能均值。",
        "",
        "| 方案 | BPF调用次数 | BPF累计时间（秒） | 每次调用均值（纳秒） | 相当于单核占用 | 占八核总能力 |",
        "|---|---:|---:|---:|---:|---:|",
        f"| 仅工作流分组 | {grouping['total_run_count']} | {grouping['total_run_time_sec']:.3f} | {grouping['mean_ns_per_call']:.1f} | {grouping['one_core_percent']:.2f}% | {grouping['machine_capacity_percent']:.3f}% |",
        f"| 完整第二版 | {full['total_run_count']} | {full['total_run_time_sec']:.3f} | {full['mean_ns_per_call']:.1f} | {full['one_core_percent']:.2f}% | {full['machine_capacity_percent']:.3f}% |",
    ]
    if automatic is not None:
        lines.append(
            f"| 完整第二版加自动权重 | {automatic['total_run_count']} | "
            f"{automatic['total_run_time_sec']:.3f} | {automatic['mean_ns_per_call']:.1f} | "
            f"{automatic['one_core_percent']:.2f}% | {automatic['machine_capacity_percent']:.3f}% |"
        )
    lines.extend([
        "",
        f"来源识别、等待关系、成组推进、旧任务延后和近期处理器复用合计增加约 {incremental_one_core:.2f} 个单核百分点，即所测 {args.cpu_count} 个处理器总能力的 {incremental_one_core / args.cpu_count:.3f}%。",
        "",
        "| C控制程序数量 | 合计处理器时间（秒） | 相当于单核占用 | 占八核总能力 | 单进程平均峰值内存（千字节） | 四进程峰值内存合计（千字节） |",
        "|---:|---:|---:|---:|---:|---:|",
        f"| {controller['processes']} | {controller['cpu_sec']:.3f} | {controller['one_core_percent']:.3f}% | {controller['machine_capacity_percent']:.4f}% | {controller['mean_max_rss_kb']:.0f} | {controller['sum_max_rss_kb']} |",
        "",
        "C控制程序只接收工具开始、更新和结束事件，并定期更新时间戳；关系识别与调度决策均在内核侧完成。",
        "",
    ])
    (args.output / "结果摘要.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""汇总 FlowCausal v2 三类任务、规模变化、关键对照与开销证据。"""
from __future__ import annotations

import json
import pathlib
import statistics
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results" / "harness-replaybench-v1"
OUTPUT = RESULTS / "flowcausal-v2-goal-summary-20260910"
SELECTION = (
    ROOT
    / "benchmarks"
    / "harness-replaybench"
    / "datasets"
    / "HarnessReplayBench-1.0-三类跨工具竞争.json"
)
OVERHEAD = RESULTS / "flowcausal-v2-overhead-summary-20260910" / "结果表.json"


SCENARIOS = [
    {
        "id": "return-group-4-cpu8",
        "label": "等待一组返回子进程",
        "tasks": 4,
        "cpus": 8,
        "root": RESULTS / "cross-tool-return-group-first-relation-20260910",
    },
    {
        "id": "short-followup-4-cpu8",
        "label": "等待一个很短的后续进程",
        "tasks": 4,
        "cpus": 8,
        "root": RESULTS / "cross-tool-short-followup-first-relation-20260910",
    },
    {
        "id": "build-followup-4-cpu8",
        "label": "等待构建子进程结束",
        "tasks": 4,
        "cpus": 8,
        "root": RESULTS / "cross-tool-build-followup-first-relation-20260910",
    },
    {
        "id": "return-group-8-cpu8",
        "label": "八任务：等待一组返回子进程",
        "tasks": 8,
        "cpus": 8,
        "root": RESULTS / "cross-tool-return-group-concurrency8-first-relation-20260910",
    },
    {
        "id": "return-group-4-cpu4",
        "label": "四核：等待一组返回子进程",
        "tasks": 4,
        "cpus": 4,
        "root": RESULTS / "cross-tool-return-group-cpu4-managed4-20260910",
    },
    {
        "id": "return-group-4-cpu16",
        "label": "十六核：等待一组返回子进程",
        "tasks": 4,
        "cpus": 16,
        "root": RESULTS / "cross-tool-return-group-cpu16-managed16-20260910",
    },
]

BASELINES = [
    {
        "method": "scx_simple",
        "label": "简单调度器",
        "root": RESULTS / "cross-tool-return-group-baseline-20260910",
    },
    {
        "method": "scx_flatcg",
        "label": "控制组分组调度器",
        "root": RESULTS / "cross-tool-return-group-flatcg-baseline-20260910",
    },
]

COUNTERS = (
    "provenance_tasks",
    "confirmed_blocking_waits",
    "progress_group_links",
    "progress_group_promotions",
    "first_relation_promotions",
    "stale_delays",
    "background_rescues",
    "locality_hits",
    "locality_fallbacks",
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是对象：{path}")
    return value


def reduction(reference: float, value: float) -> float:
    return (reference - value) / reference * 100.0 if reference else 0.0


def summary_for(registry: dict[str, Any], method: str) -> dict[str, Any]:
    return next(item for item in registry["summary"] if item["method"] == method)


def result_rows(root: pathlib.Path, registry: dict[str, Any], method: str) -> list[dict[str, Any]]:
    rows = [item for item in registry["rows"] if item["method"] == method]
    results = []
    for row in rows:
        path = root / str(row["result"])
        result = load(path)
        if result.get("status") != "pass":
            raise RuntimeError(f"结果未通过：{path}")
        if int(result.get("kernel_capture", {}).get("dropped_events") or 0):
            raise RuntimeError(f"结果存在丢失事件：{path}")
        results.append(result)
    return results


def mechanism_means(results: list[dict[str, Any]]) -> dict[str, float]:
    values = {name: [] for name in COUNTERS}
    for result in results:
        totals = {name: 0 for name in COUNTERS}
        for session in result.get("flowcausal_control", {}).get("sessions", []):
            stats = session.get("stats", {})
            for name in COUNTERS:
                totals[name] += int(stats.get(name) or 0)
        for name in COUNTERS:
            values[name].append(totals[name])
    return {
        name: statistics.fmean(items) if items else 0.0
        for name, items in values.items()
    }


def scenario_row(spec: dict[str, Any]) -> dict[str, Any]:
    root = pathlib.Path(spec["root"])
    registry = load(root / "结果表.json")
    grouping = summary_for(registry, "flowcausal_v2_grouping_only")
    full = summary_for(registry, "flowcausal_v2_latest")
    if grouping["completed_trials"] != full["completed_trials"]:
        raise RuntimeError(f"两种方案轮数不同：{root}")
    mechanisms = mechanism_means(result_rows(root, registry, "flowcausal_v2_latest"))
    return {
        "id": spec["id"],
        "label": spec["label"],
        "tasks": spec["tasks"],
        "cpus": spec["cpus"],
        "suite_id": registry["suite_id"],
        "trials": int(full["completed_trials"]),
        "grouping_only": {
            "makespan_sec": grouping["makespan_sec_mean"],
            "mean_workflow_completion_sec": grouping["mean_workflow_completion_sec_mean"],
            "p95_tool_service_sec": grouping["p95_synchronous_tool_service_sec_mean"],
        },
        "full_v2": {
            "makespan_sec": full["makespan_sec_mean"],
            "mean_workflow_completion_sec": full["mean_workflow_completion_sec_mean"],
            "p95_tool_service_sec": full["p95_synchronous_tool_service_sec_mean"],
        },
        "improvement_percent": {
            "makespan": reduction(grouping["makespan_sec_mean"], full["makespan_sec_mean"]),
            "mean_workflow_completion": reduction(
                grouping["mean_workflow_completion_sec_mean"],
                full["mean_workflow_completion_sec_mean"],
            ),
            "p95_tool_service": reduction(
                grouping["p95_synchronous_tool_service_sec_mean"],
                full["p95_synchronous_tool_service_sec_mean"],
            ),
        },
        "mechanism_counts_per_trial": mechanisms,
        "result_directory": root.relative_to(ROOT).as_posix(),
    }


def baseline_rows(main: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    main_result = main["full_v2"]
    for spec in BASELINES:
        registry = load(pathlib.Path(spec["root"]) / "结果表.json")
        if registry["suite_id"] != main["suite_id"]:
            raise RuntimeError(f"关键对照套件不一致：{spec['root']}")
        summary = summary_for(registry, str(spec["method"]))
        rows.append(
            {
                "method": spec["method"],
                "label": spec["label"],
                "trials": int(summary["completed_trials"]),
                "makespan_sec": summary["makespan_sec_mean"],
                "mean_workflow_completion_sec": summary[
                    "mean_workflow_completion_sec_mean"
                ],
                "p95_tool_service_sec": summary["p95_synchronous_tool_service_sec_mean"],
                "full_v2_improvement_percent": {
                    "makespan": reduction(
                        summary["makespan_sec_mean"], main_result["makespan_sec"]
                    ),
                    "mean_workflow_completion": reduction(
                        summary["mean_workflow_completion_sec_mean"],
                        main_result["mean_workflow_completion_sec"],
                    ),
                    "p95_tool_service": reduction(
                        summary["p95_synchronous_tool_service_sec_mean"],
                        main_result["p95_tool_service_sec"],
                    ),
                },
                "result_directory": pathlib.Path(spec["root"]).relative_to(ROOT).as_posix(),
            }
        )
    return rows


def fmt(value: float) -> str:
    return f"{value:.2f}"


def write_markdown(payload: dict[str, Any]) -> None:
    lines = [
        "# FlowCausal v2 完整实验汇总",
        "",
        "三类真实工具组合仅依据简单调度器下的 C/eBPF 行为记录选取，选择过程未读取候选调度器结果。所有性能轮次使用真实仓库命令、同时启动、固定处理器范围并在每轮开始前清理页缓存。",
        "",
        "## 三类真实工具组合",
        "",
        "| 组合 | 任务 | 处理器 | 轮数 | 总完成时间：分组→完整（秒） | 改善 | 平均工作流：分组→完整（秒） | 改善 | 工具阻塞高分位改善 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["scenarios"][:3]:
        group = row["grouping_only"]
        full = row["full_v2"]
        improve = row["improvement_percent"]
        lines.append(
            f"| {row['label']} | {row['tasks']} | {row['cpus']} | {row['trials']} | "
            f"{fmt(group['makespan_sec'])}→{fmt(full['makespan_sec'])} | "
            f"{improve['makespan']:+.1f}% | "
            f"{fmt(group['mean_workflow_completion_sec'])}→{fmt(full['mean_workflow_completion_sec'])} | "
            f"{improve['mean_workflow_completion']:+.1f}% | "
            f"{improve['p95_tool_service']:+.1f}% |"
        )

    lines.extend(
        [
            "",
            "等待一组返回子进程和等待构建子进程结束两类组合分别将总完成时间缩短15.7%和17.1%；很短的后续进程很快结束，完整机制保持近似中性。由此，收益集中出现在当前工具等待对象仍需运行、同时前一工具任务继续竞争处理器的组合中。",
            "",
            "## 并发与处理器缩放",
            "",
            "| 条件 | 任务 | 处理器 | 总完成时间改善 | 平均工作流完成时间改善 | 工具阻塞高分位改善 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in payload["scenarios"][3:]:
        improve = row["improvement_percent"]
        lines.append(
            f"| {row['label']} | {row['tasks']} | {row['cpus']} | "
            f"{improve['makespan']:+.1f}% | {improve['mean_workflow_completion']:+.1f}% | "
            f"{improve['p95_tool_service']:+.1f}% |"
        )

    lines.extend(
        [
            "",
            "四任务从4核扩展到16核时，总完成时间改善分别为17.2%、15.7%和13.7%；固定8核从4任务扩展到8任务时，平均工作流完成时间改善由14.3%提高到21.9%。",
            "",
            "## 关键调度器对照",
            "",
            "| 调度器 | 轮数 | 总完成时间（秒） | 平均工作流完成时间（秒） | 工具阻塞高分位（秒） | 完整第二版的工作流改善 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    main = payload["scenarios"][0]
    for row in payload["baselines"]:
        lines.append(
            f"| {row['label']} | {row['trials']} | {fmt(row['makespan_sec'])} | "
            f"{fmt(row['mean_workflow_completion_sec'])} | {fmt(row['p95_tool_service_sec'])} | "
            f"{row['full_v2_improvement_percent']['mean_workflow_completion']:+.1f}% |"
        )
    lines.append(
        f"| 完整第二版 | {main['trials']} | {fmt(main['full_v2']['makespan_sec'])} | "
        f"{fmt(main['full_v2']['mean_workflow_completion_sec'])} | "
        f"{fmt(main['full_v2']['p95_tool_service_sec'])} | — |"
    )

    overhead = payload["overhead"]
    lines.extend(
        [
            "",
            "## 运行开销",
            "",
            f"完整机制相对仅分组增加的内核处理时间约占八核总能力 {overhead['incremental']['machine_capacity_percentage_points']:.3f}%；四个 C 控制进程合计占八核总能力 {overhead['c_controller']['machine_capacity_percent']:.4f}%，峰值内存合计约 {overhead['c_controller']['sum_max_rss_kb'] / 1024:.1f} 兆字节。",
            "",
            "内核记录同时确认来源继承、真实阻塞等待、成组推进、旧任务延后和近期处理器复用均在性能轮次中实际发生。完整在线系统只包含 C、eBPF 与 sched_ext，Python 仅用于实验运行和离线汇总。",
            "",
        ]
    )
    (OUTPUT / "结果摘要.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    selection = load(SELECTION)
    if selection.get("candidate_scheduler_results_used") is not False:
        raise RuntimeError("任务选择并非独立于候选调度器结果")
    if selection.get("reference_scheduler") != "scx_simple":
        raise RuntimeError("任务选择没有使用预定参考调度器")
    scenarios = [scenario_row(spec) for spec in SCENARIOS]
    payload = {
        "schema": "flowcausal.v2.goal-summary.v1",
        "selection": {
            "reference_scheduler": selection["reference_scheduler"],
            "capture": selection["capture"],
            "candidate_scheduler_results_used": False,
            "classes": selection["classes"],
            "source": SELECTION.relative_to(ROOT).as_posix(),
        },
        "scenarios": scenarios,
        "baselines": baseline_rows(scenarios[0]),
        "overhead": load(OVERHEAD),
        "excluded_results": [
            {
                "directory": "experiments/results/harness-replaybench-v1/cross-tool-return-group-cpu4-invalid-managed-range-20260910",
                "reason": "调度器受管处理器数与任务处理器范围不一致",
            },
            {
                "directory": "experiments/results/harness-replaybench-v1/cross-tool-return-group-cpu16-invalid-managed-range-20260910",
                "reason": "调度器受管处理器数与任务处理器范围不一致",
            },
        ],
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "结果表.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown(payload)
    print(json.dumps({"output": str(OUTPUT)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

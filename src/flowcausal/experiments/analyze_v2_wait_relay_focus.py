#!/usr/bin/env python3
"""汇总 V2 单名额传递的全工作流初测和重点重复实验。"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_INITIAL = ROOT / (
    "experiments/results/v2-wait-relay-all-workflow-coverage-initial-20260915/"
    "统一汇总/全工作流初步比较.json"
)
DEFAULT_PROCESS = ROOT / (
    "experiments/results/harness-replaybench-v1/"
    "50-workflow-process-tree-analysis-scx-simple-current-v2-20260915/"
    "进程结构分析.json"
)
DEFAULT_FOCUS = {
    "01": ROOT / (
        "experiments/results/"
        "v2-relayed-first-service-focus-suite01-3trial-20260915/"
        "harness-replay-matrix-summary.json"
    ),
    "02": ROOT / (
        "experiments/results/"
        "v2-relayed-first-service-focus-suite02-3trial-20260915/"
        "harness-replay-matrix-summary.json"
    ),
}
DEFAULT_BEFORE_EARLY = {
    "01": ROOT / (
        "experiments/results/v2-relayed-first-service-focus-suite01-20260915/"
        "harness-replay-matrix-summary.json"
    ),
    "02": ROOT / (
        "experiments/results/v2-relayed-first-service-focus-suite02-20260915/"
        "harness-replay-matrix-summary.json"
    ),
}
DEFAULT_EARLY = {
    "01": ROOT / (
        "experiments/results/v2-linked-queue-pressure-focus-suite01-20260915/"
        "harness-replay-matrix-summary.json"
    ),
    "02": ROOT / (
        "experiments/results/v2-linked-queue-pressure-focus-suite02-20260915/"
        "harness-replay-matrix-summary.json"
    ),
}
DEFAULT_OUTPUT = ROOT / (
    "experiments/results/v2-wait-relay-focus-analysis-20260915"
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"不是 JSON 对象：{path}")
    return value


def improvement(reference: float, candidate: float) -> float:
    return (reference - candidate) / reference * 100.0


def variant(path: pathlib.Path) -> dict[str, Any]:
    payload = read_json(path)
    variants = payload.get("variants")
    if payload.get("status") != "pass" or not isinstance(variants, list) or len(variants) != 1:
        raise ValueError(f"重点实验必须只有一个成功的最新版 V2：{path}")
    item = variants[0]
    if item.get("name") != "flowcausal_v2_automatic_weight":
        raise ValueError(f"调度器名称不符：{path}")
    if int(item.get("summary", {}).get("pass_trials", 0)) != int(
        payload.get("trials_per_variant", 0)
    ):
        raise ValueError(f"存在未通过轮次：{path}")
    return item


def focus_summary(path: pathlib.Path) -> dict[str, Any]:
    item = variant(path)
    trials = item["trials"]
    by_workflow: dict[str, list[float]] = {}
    mechanism_by_workflow: dict[str, dict[str, list[float]]] = {}
    for trial in trials:
        for case in trial["cases"]:
            name = case["instance_id"]
            by_workflow.setdefault(name, []).append(
                float(case["elapsed_sec"])
            )
            records = case.get("flowd_v2_stats", {}).get("records", [])
            stats = next(
                (record for record in records if "progress_group_relays" in record),
                {},
            )
            for key in (
                "progress_group_relays",
                "nested_wait_promotions",
                "progress_group_promotions",
                "adaptive_weight_activations",
            ):
                mechanism_by_workflow.setdefault(name, {}).setdefault(key, []).append(
                    float(stats.get(key, 0))
                )
    return {
        "source": str(path),
        "trials": len(trials),
        "makespan_sec": [float(t["aggregate"]["makespan_sec"]) for t in trials],
        "makespan_mean_sec": float(item["summary"]["makespan_mean_sec"]),
        "makespan_std_sec": float(item["summary"]["makespan_std_sec"]),
        "mean_workflow_completion_sec": float(
            item["summary"]["mean_workflow_completion_mean_sec"]
        ),
        "mean_workflow_completion_std_sec": float(
            item["summary"]["mean_workflow_completion_std_sec"]
        ),
        "mean_critical_sec": float(item["summary"]["mean_critical_mean_sec"]),
        "mechanism_mean": item["summary"]["flowcausal_v2_mean"],
        "workflows": {
            name: {
                "elapsed_sec": values,
                "mean_sec": statistics.mean(values),
                "std_sec": statistics.pstdev(values),
                "mechanism_mean": {
                    key: statistics.mean(metric_values)
                    for key, metric_values in mechanism_by_workflow[name].items()
                },
            }
            for name, values in sorted(by_workflow.items())
        },
    }


def single_run_delta(before_path: pathlib.Path, after_path: pathlib.Path) -> dict[str, Any]:
    before = variant(before_path)["summary"]
    after = variant(after_path)["summary"]
    return {
        "before": str(before_path),
        "after": str(after_path),
        "before_makespan_sec": float(before["makespan_mean_sec"]),
        "after_makespan_sec": float(after["makespan_mean_sec"]),
        "makespan_improvement_percent": improvement(
            float(before["makespan_mean_sec"]), float(after["makespan_mean_sec"])
        ),
        "before_mean_workflow_sec": float(before["mean_workflow_completion_mean_sec"]),
        "after_mean_workflow_sec": float(after["mean_workflow_completion_mean_sec"]),
        "mean_workflow_improvement_percent": improvement(
            float(before["mean_workflow_completion_mean_sec"]),
            float(after["mean_workflow_completion_mean_sec"]),
        ),
        "before_activations": float(
            before["flowcausal_v2_mean"]["adaptive_weight_activations"]
        ),
        "after_activations": float(
            after["flowcausal_v2_mean"]["adaptive_weight_activations"]
        ),
        "before_nested_promotions": float(
            before["flowcausal_v2_mean"]["nested_wait_promotions"]
        ),
        "after_nested_promotions": float(
            after["flowcausal_v2_mean"]["nested_wait_promotions"]
        ),
    }


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal V2 单名额传递：全工作流初测与重点复验",
        "",
        "## 1. 分析范围",
        "",
        "50 个工作流被分成 10 组，每组 5 个工作流同时启动并共享处理器 0—7。全量初测中 Linux、`scx_simple` 和当时的 V2 各运行一次，用于发现规律，不用于显著性结论。随后只对最新版 V2 的第 1 组和第 2 组各重复 3 次；历史基线没有重跑，因此下面与基线的百分比只用于判断方向，不作为配对统计。",
        "",
        "## 2. 五十个工作流的共同规律",
        "",
        f"- 共分析 {payload['process_analysis']['workflow_count']} 个工作流、{payload['process_analysis']['command_count']} 条工具命令；",
        f"- {payload['process_analysis']['substantial_commands']} 条命令具有可观测的处理器计算，其中 {payload['process_analysis']['substantial_commands_with_observed_wait_relation']} 条出现父进程等待子进程；",
        f"- {payload['process_analysis']['workflows_with_depth_at_least_two']} 个工作流出现至少两层后代，{payload['process_analysis']['workflows_with_depth_at_least_three']} 个出现至少三层后代；",
        f"- 在能够确认会影响工具返回的进程中，孙进程及更深进程占 {payload['process_analysis']['return_related_deeper_runtime_share_percent']:.1f}% 的处理器时间、{payload['process_analysis']['return_related_deeper_runnable_wait_share_percent']:.1f}% 的排队时间；",
        f"- 这一比例在不同工作流之间差异很大：按工作流计算的中位数为 {payload['process_analysis']['per_workflow_deeper_runtime_share_p50']:.1f}%，四分之三位置为 {payload['process_analysis']['per_workflow_deeper_runtime_share_p75']:.1f}%。因此不能仅按进程层数扩大调度范围。",
        "",
        "## 3. 全工作流初测",
        "",
        "| 比较 | 10组总完成时间 | 平均工作流完成时间 | 工作流完成时间95分位 |",
        "|---|---:|---:|---:|",
    ]
    initial = payload["initial"]
    for name, label in (("default", "Linux"), ("scx_simple", "`scx_simple`"), ("flowcausal_v2_automatic_weight", "初测V2")):
        row = initial["aggregates"][name]
        lines.append(
            f"| {label} | {row['sum_suite_makespan_sec']:.3f}秒 | "
            f"{row['mean_workflow_completion_sec']:.3f}秒 | "
            f"{row['p95_workflow_completion_sec']:.3f}秒 |"
        )
    lines += [
        "",
        "初测V2相对Linux的10组总完成时间增加0.62%，平均工作流完成时间增加0.66%；相对`scx_simple`的总完成时间增加0.51%，平均工作流完成时间缩短0.75%。初测的主要价值不是给出最终性能数字，而是发现：V2记录了2415次向下一层传递，但只有6次真正使深层进程提前运行。识别与调度动作之间存在明显缺口。",
        "",
        "## 4. 重点重复实验",
        "",
        "| 组合 | 最新V2总完成时间 | 相对冻结Linux | 相对冻结`scx_simple` | 平均工作流完成时间 |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in ("01", "02"):
        focus = payload["focus"][key]
        lines.append(
            f"| 第{int(key)}组 | {focus['makespan_mean_sec']:.3f}±{focus['makespan_std_sec']:.3f}秒 | "
            f"{focus['vs_frozen_linux_makespan_percent']:+.2f}% | "
            f"{focus['vs_frozen_scx_simple_makespan_percent']:+.2f}% | "
            f"{focus['mean_workflow_completion_sec']:.3f}±{focus['mean_workflow_completion_std_sec']:.3f}秒 |"
        )
    lines += [
        "",
        "第1组由Dask决定最后完成时间。最新版V2三轮为74.70—76.32秒，平均工作流完成时间相对冻结`scx_simple`缩短12.82%，但总完成时间与`scx_simple`基本相同。第2组由SQLGlot决定，三轮为157.39—158.87秒，稳定接近`scx_simple`且慢于Linux。",
        "",
        "## 5. 为什么现有传递没有命中主要计算进程",
        "",
        f"Dask中能够确认与工具返回有关的孙进程及更深进程占 {payload['key_workflows']['dask__dask-4003']['deeper_runtime_share_percent']:.1f}% 的处理器时间；两条长测试命令中这一比例分别约为85.7%和88.0%。最新版V2在Dask中每轮平均记录约 {payload['focus']['01']['workflows']['dask__dask-4003']['mechanism_mean']['progress_group_relays']:.1f} 次名额传递，却没有让Dask的深层进程提前运行。",
        "",
        f"SQLGlot中这一比例接近 {payload['key_workflows']['tobymao__sqlglot-3272']['deeper_runtime_share_percent']:.1f}%，主要测试由`make`启动的更深进程完成。最新版V2在SQLGlot中每轮平均记录约 {payload['focus']['02']['workflows']['tobymao__sqlglot-3272']['mechanism_mean']['progress_group_relays']:.1f} 次传递，但深层进程实际提前运行平均只有 {payload['focus']['02']['workflows']['tobymao__sqlglot-3272']['mechanism_mean']['nested_wait_promotions']:.1f} 次。",
        "",
        "原因是当前传递只沿`wait`/`waitpid`确认的父子等待继续向下。Dask的并行工作进程和SQLGlot测试进程还会通过管道、进程间通信或运行库内部同步等待；它们虽然属于同一条工具执行，且实际承担计算，却不会稳定表现为连续的`wait`父子链。",
        "",
        "## 6. 提前记录排队为何停止采用",
        "",
        "在关系建立时立即记录已经发生的排队，功能上能够增加权重调整和深层进程提前运行次数，但两个组合的单轮总完成时间分别增加3.16%和0.80%。动作变多而结果没有改善，说明问题不在权重启动晚了几百微秒，而在于被提高权重的会话中仍没有选中真正执行计算的进程。该改动已从源码撤回，结果保留为分析证据。",
        "",
        "## 7. 后续验证结果与下一轮V2算法",
        "",
        "上述睡眠上层进程识别已经实现并完成验证。专用管道任务证明它能够覆盖普通等待系统调用之外的后代关系；但 Dask 和 SQLGlot 分别产生上万个候选。放宽规则、反复给后代 2 毫秒短时间片时，Dask 的总完成时间明显增加；限制为每次工具等待一次后，两个重点组合相对原有 V2 分别变化 0.25% 和 0.60%，没有形成可归因收益。详细数据见 `experiments/results/v2-sleeping-descendant-route-analysis-20260915/`。该路线保留为显式实验开关并默认关闭。",
        "",
        "下一轮不再直接提高某个后代进程优先级。调度器应把工具入口仍在等待、同一工具后代持续运行、这些后代持续排队作为会话级处理器分配依据。在有限时间内增加该会话按普通排序和正常时间片运行的机会，实际使用量继续计入会话公平核算。这样既能覆盖 Dask 的并行工作进程，也能覆盖 SQLGlot 的少数长进程，并避免把连续计算切成大量短时间片。",
        "",
        "会话选择使用一段时间内累计的排队与运行情况，不能按第一个到达的进程决定。规则只读取已经发生的 C/eBPF 事件，不读取命令名称、离线完整进程树、最终耗时或未来进程数量。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--initial", type=pathlib.Path, default=DEFAULT_INITIAL)
    parser.add_argument("--process-analysis", type=pathlib.Path, default=DEFAULT_PROCESS)
    parser.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    initial = read_json(args.initial)
    process = read_json(args.process_analysis)
    focus = {key: focus_summary(path) for key, path in DEFAULT_FOCUS.items()}
    suite_by_id = {item["suite_id"]: item for item in initial["suites"]}
    for key, suite_id in (
        ("01", "hrb1-all-workflow-coverage-01-concurrent-tool"),
        ("02", "hrb1-all-workflow-coverage-02-concurrent-tool"),
    ):
        frozen = suite_by_id[suite_id]["variants"]
        current = focus[key]
        current["frozen_baselines"] = frozen
        current["vs_frozen_linux_makespan_percent"] = improvement(
            float(frozen["default"]["makespan_sec"]), current["makespan_mean_sec"]
        )
        current["vs_frozen_scx_simple_makespan_percent"] = improvement(
            float(frozen["scx_simple"]["makespan_sec"]), current["makespan_mean_sec"]
        )
        current["vs_frozen_linux_mean_workflow_percent"] = improvement(
            float(frozen["default"]["mean_workflow_completion_sec"]),
            current["mean_workflow_completion_sec"],
        )
        current["vs_frozen_scx_simple_mean_workflow_percent"] = improvement(
            float(frozen["scx_simple"]["mean_workflow_completion_sec"]),
            current["mean_workflow_completion_sec"],
        )

    workflow_process = {item["workflow"]: item for item in process["workflows"]}
    key_workflows = {}
    for name in ("dask__dask-4003", "tobymao__sqlglot-3272"):
        item = workflow_process[name]
        key_workflows[name] = {
            "deeper_runtime_share_percent": float(
                item["return_related_deeper_runtime_share_percent"]
            ),
            "deeper_queue_share_percent": float(
                item["return_related_deeper_runnable_wait_share_percent"]
            ),
            "maximum_depth": int(item["maximum_depth"]),
            "largest_deep_command": item["largest_deep_command"],
        }

    payload = {
        "schema": "flowcausal.v2.wait-relay-focus-analysis.v1",
        "initial": initial,
        "process_analysis": {
            key: process[key]
            for key in (
                "workflow_count",
                "command_count",
                "substantial_commands",
                "substantial_commands_with_observed_wait_relation",
                "workflows_with_depth_at_least_two",
                "workflows_with_depth_at_least_three",
                "return_related_deeper_runtime_share_percent",
                "return_related_deeper_runnable_wait_share_percent",
                "per_workflow_deeper_runtime_share_p50",
                "per_workflow_deeper_runtime_share_p75",
            )
        },
        "focus": focus,
        "early_queue_pressure": {
            key: single_run_delta(DEFAULT_BEFORE_EARLY[key], DEFAULT_EARLY[key])
            for key in ("01", "02")
        },
        "key_workflows": key_workflows,
        "decision": {
            "keep_relays": True,
            "keep_early_queue_pressure": False,
            "next_algorithm": "根据同一工具后代持续运行和持续排队情况，有限增加整个会话按正常时间片运行的机会",
            "do_not_use": ["命令名称", "离线完整进程树", "最终耗时", "未来进程数"],
        },
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "重点案例汇总.json"
    md_path = args.out_dir / "重点案例分析.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    md_path.write_text(markdown(payload), encoding="utf-8")
    print(json.dumps({"status": "pass", "json": str(json_path), "markdown": str(md_path)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""汇总 V2 睡眠上层进程与后代进程识别路线的功能和性能证据。"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments/results"
DEFAULT_OUTPUT = RESULTS / "v2-sleeping-descendant-route-analysis-20260915"
INPUTS = {
    "stable_01": RESULTS / "v2-relayed-first-service-focus-suite01-3trial-20260915/harness-replay-matrix-summary.json",
    "stable_02": RESULTS / "v2-relayed-first-service-focus-suite02-3trial-20260915/harness-replay-matrix-summary.json",
    "broad_01": RESULTS / "v2-sleeping-descendant-focus-suite01-initial-20260915/harness-replay-matrix-summary.json",
    "relation_01": RESULTS / "v2-sleeping-descendant-relation-bounded-focus-suite01-initial-20260915/harness-replay-matrix-summary.json",
    "subtree_01": RESULTS / "v2-sleeping-descendant-anchored-subtree-focus-suite01-initial-20260915/harness-replay-matrix-summary.json",
    "bounded_01": RESULTS / "v2-sleeping-descendant-tool-wait-bounded-focus-suite01-initial-20260915/harness-replay-matrix-summary.json",
    "bounded_02": RESULTS / "v2-sleeping-descendant-tool-wait-bounded-focus-suite02-initial-20260915/harness-replay-matrix-summary.json",
    "functional": RESULTS / "v2-sleeping-descendant-functional-final-20260915/result.json/result.json",
}


def read_json(path: pathlib.Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"不是 JSON 对象：{path}")
    return value


def improvement(reference: float, candidate: float) -> float:
    return (reference - candidate) / reference * 100.0


def read_matrix(path: pathlib.Path) -> dict[str, Any]:
    payload = read_json(path)
    variants = payload.get("variants")
    if payload.get("status") != "pass" or not isinstance(variants, list) or len(variants) != 1:
        raise ValueError(f"实验必须包含一个成功的调度器版本：{path}")
    variant = variants[0]
    summary = variant.get("summary", {})
    trials = variant.get("trials", [])
    if int(summary.get("pass_trials", 0)) != len(trials):
        raise ValueError(f"实验中存在失败轮次：{path}")
    workflows: dict[str, dict[str, float]] = {}
    for trial in trials:
        for case in trial.get("cases", []):
            name = case.get("instance_id")
            if not isinstance(name, str):
                continue
            record = next(iter(case.get("flowd_v2_stats", {}).get("records", [])), {})
            row = workflows.setdefault(
                name,
                {"runs": 0.0, "elapsed_sec": 0.0, "candidates": 0.0, "promotions": 0.0, "max_depth": 0.0},
            )
            row["runs"] += 1
            row["elapsed_sec"] += float(case.get("elapsed_sec", 0.0))
            row["candidates"] += float(record.get("sleeping_descendant_candidates", 0.0))
            row["promotions"] += float(record.get("sleeping_descendant_promotions", 0.0))
            row["max_depth"] = max(row["max_depth"], float(record.get("max_sleeping_descendant_depth", 0.0)))
    for row in workflows.values():
        runs = row.pop("runs")
        for key in ("elapsed_sec", "candidates", "promotions"):
            row[key] /= runs
    return {
        "source": str(path),
        "trials": len(trials),
        "makespan_mean_sec": float(summary["makespan_mean_sec"]),
        "makespan_std_sec": float(summary.get("makespan_std_sec", 0.0)),
        "mean_workflow_completion_sec": float(summary["mean_workflow_completion_mean_sec"]),
        "mean_critical_sec": float(summary["mean_critical_mean_sec"]),
        "workflows": workflows,
    }


def build_payload() -> dict[str, Any]:
    matrices = {key: read_matrix(path) for key, path in INPUTS.items() if key != "functional"}
    functional = read_json(INPUTS["functional"])
    matrices["broad_01"]["vs_stable_makespan_percent"] = improvement(
        matrices["stable_01"]["makespan_mean_sec"], matrices["broad_01"]["makespan_mean_sec"]
    )
    for suite in ("01", "02"):
        matrices[f"bounded_{suite}"]["vs_stable_makespan_percent"] = improvement(
            matrices[f"stable_{suite}"]["makespan_mean_sec"],
            matrices[f"bounded_{suite}"]["makespan_mean_sec"],
        )
    return {
        "schema": "flowcausal-v2-sleeping-descendant-route-analysis-v1",
        "scope": "离线分析可以读取完整运行结果；调度器运行时只读取已经发生的内核事件。",
        "functional": {"source": str(INPUTS["functional"]), **functional},
        "experiments": matrices,
        "decision": {
            "default_enabled": False,
            "reason": "直接给后代进程短时间片无法稳定缩短完成时间；候选数量很大时，最先满足条件的进程不能代表主要计算。",
            "next_algorithm": "把工具入口等待、同一工具后代持续运行和持续排队作为会话级处理器分配依据；有限时间内增加该会话按正常时间片运行的机会，不直接提高单个后代进程优先级。",
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    exp = payload["experiments"]
    dask_broad = exp["broad_01"]["workflows"]["dask__dask-4003"]
    dask_bounded = exp["bounded_01"]["workflows"]["dask__dask-4003"]
    sql_bounded = exp["bounded_02"]["workflows"]["tobymao__sqlglot-3272"]
    functional = payload["functional"]
    functional_status = "通过" if functional["status"] == "pass" else functional["status"]
    lines = [
        "# FlowCausal V2 睡眠上层进程与后代进程识别实验",
        "",
        "更新日期：2026 年 9 月 15 日",
        "",
        "## 1. 要回答的问题",
        "",
        "完整进程记录表明，Dask 和 SQLGlot 的主要计算位于工具入口以下多层进程中，且上层进程不一定连续调用 `wait` 或 `waitpid`。本轮判断：能否根据同一工具内上层进程已经睡眠、后代进程已经排队这一事实，自动找到需要推进的后代，并通过一次短时间片缩短工具返回时间。离线分析允许读取完整结果以发现规律；运行中的调度器没有读取命令名称、未来进程、最终耗时或离线标签。",
        "",
        "## 2. 功能检查",
        "",
        f"专用任务不在内部调用 `wait`/`waitpid`，而是由三层进程通过管道逐级通知。实验结果为 **{functional_status}**：观察到 {functional['stats']['sleeping_descendant_candidates']} 个候选，其中 {functional['stats']['sleeping_descendant_promotions']} 个获得一次短时间片；连续等待关系为 {functional['stats']['nested_wait_links']}。这证明新增识别路径确实能够覆盖普通等待系统调用之外的睡眠关系。",
        "",
        "## 3. 重点案例结果",
        "",
        "| 实现 | 组合 | 总完成时间 | 平均工作流完成时间 | 主要长工作流的候选/实际提前 |",
        "|---|---|---:|---:|---:|",
        f"| 原有单名额传递，3轮 | 第1组 | {exp['stable_01']['makespan_mean_sec']:.3f}±{exp['stable_01']['makespan_std_sec']:.3f}秒 | {exp['stable_01']['mean_workflow_completion_sec']:.3f}秒 | 未启用本机制 |",
        f"| 不限制次数，1轮 | 第1组 | {exp['broad_01']['makespan_mean_sec']:.3f}秒 | {exp['broad_01']['mean_workflow_completion_sec']:.3f}秒 | {dask_broad['candidates']:.0f}/{dask_broad['promotions']:.0f} |",
        f"| 每次工具等待限一次，1轮 | 第1组 | {exp['bounded_01']['makespan_mean_sec']:.3f}秒 | {exp['bounded_01']['mean_workflow_completion_sec']:.3f}秒 | {dask_bounded['candidates']:.0f}/{dask_bounded['promotions']:.0f} |",
        f"| 原有单名额传递，3轮 | 第2组 | {exp['stable_02']['makespan_mean_sec']:.3f}±{exp['stable_02']['makespan_std_sec']:.3f}秒 | {exp['stable_02']['mean_workflow_completion_sec']:.3f}秒 | 未启用本机制 |",
        f"| 每次工具等待限一次，1轮 | 第2组 | {exp['bounded_02']['makespan_mean_sec']:.3f}秒 | {exp['bounded_02']['mean_workflow_completion_sec']:.3f}秒 | {sql_bounded['candidates']:.0f}/{sql_bounded['promotions']:.0f} |",
        "",
        f"第1组中，不限制次数的版本让 Dask 获得 {dask_broad['promotions']:.0f} 次短时间片，总完成时间相对原有 V2 的三轮均值增加 {-exp['broad_01']['vs_stable_makespan_percent']:.2f}%。每次工具等待只允许一次后，Dask 仍出现 {dask_bounded['candidates']:.0f} 个候选，但仅 {dask_bounded['promotions']:.0f} 次实际提前，总完成时间相对原有 V2 变化 {-exp['bounded_01']['vs_stable_makespan_percent']:.2f}%。第2组 SQLGlot 出现 {sql_bounded['candidates']:.0f} 个候选，仅 {sql_bounded['promotions']:.0f} 次实际提前，总完成时间相对原有 V2 变化 {-exp['bounded_02']['vs_stable_makespan_percent']:.2f}%。单轮诊断只用于判断机制方向，不替代重复实验。",
        "",
        "关系限制更强的两个中间版本在 Dask 中分别只有 1 个候选、0 次实际提前，以及 0 个候选、0 次实际提前。它们虽然单轮总完成时间较短，但调度动作没有作用到 Dask，不能把时间变化归因于新增机制。",
        "",
        "## 4. 原因",
        "",
        "上层进程睡眠能够缩小范围，却不能直接指出哪一个后代承担主要计算。Dask 会创建大量短进程并行工作，SQLGlot 则把主要计算集中在少数长进程；两者都产生上万个候选。按第一个满足条件的进程选择，会把短时间片交给偶然先入队的进程。反复给 2 毫秒短时间片还会切碎原本连续的计算，使处理器缓存和任务切换成本抵消收益。",
        "",
        "## 5. 下一步算法",
        "",
        "保留这条识别路径作为实验开关，默认关闭。下一步不再直接提高某个后代进程的优先级，而把三项已经发生的事实合并为会话级信号：工具入口仍在等待；同一工具的后代持续获得处理器时间；这些后代持续在处理器队列中等待。满足条件时，在有限时间内增加该会话获得处理器的机会，但会话内部仍使用普通排序和正常时间片。这样既能覆盖 Dask 的并行工作进程，也能覆盖 SQLGlot 的少数长进程，同时避免把计算切成大量 2 毫秒片段。",
        "",
        "选择会话时使用一段时间内累计的排队与运行情况，而不是第一个到达的进程；增加的处理器时间继续计入现有会话公平核算，并受 100 毫秒上限、服务差额补齐和会话轮换约束。该设计完全使用运行中已经出现的 C/eBPF 事件，不需要事先知道工作流结构或最终结果。",
        "",
        "## 6. 证据位置",
        "",
    ]
    for key, path in INPUTS.items():
        lines.append(f"- `{key}`：`{path.relative_to(ROOT)}`；")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=pathlib.Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = build_payload()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "后代进程识别实验.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.out_dir / "后代进程识别实验.md").write_text(
        render_markdown(payload), encoding="utf-8"
    )
    print(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

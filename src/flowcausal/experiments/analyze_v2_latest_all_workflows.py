#!/usr/bin/env python3
"""把当前稳定 V2 的 50 工作流初测与冻结基线放入同一份结果。"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def finite(value: object) -> float:
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise RuntimeError(f"缺少有效数值：{value!r}")
    return float(value)


def improvement(reference: float, current: float) -> float:
    return (reference - current) / reference * 100.0


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def read_latest(root: pathlib.Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    suites: dict[str, dict[str, Any]] = {}
    workflows: dict[str, dict[str, Any]] = {}
    for index in range(1, 11):
        path = root / f"suite-{index:02d}" / "harness-replay-matrix-summary.json"
        summary = load(path)
        if summary.get("status") != "pass":
            raise RuntimeError(f"测试未通过：{path}")
        variants = summary.get("variants")
        if not isinstance(variants, list) or len(variants) != 1:
            raise RuntimeError(f"最新实现必须恰好运行一次：{path}")
        trials = variants[0].get("trials")
        if not isinstance(trials, list) or len(trials) != 1:
            raise RuntimeError(f"最新实现必须恰好运行一轮：{path}")
        trial = trials[0]
        if trial.get("status") != "pass":
            raise RuntimeError(f"工作流组合未通过：{path}")
        suite_id = str(trial.get("suite_id") or "")
        aggregate = trial.get("aggregate")
        cases = trial.get("cases")
        if not suite_id or not isinstance(aggregate, dict) or not isinstance(cases, list):
            raise RuntimeError(f"结果结构不完整：{path}")
        if len(cases) != 5 or any(case.get("returncode") != 0 for case in cases):
            raise RuntimeError(f"结果不含五个成功工作流：{path}")
        suites[suite_id] = {
            "summary": str(path.resolve()),
            "makespan_sec": finite(aggregate.get("makespan_sec")),
            "mean_workflow_completion_sec": finite(
                aggregate.get("mean_workflow_completion_sec")
            ),
            "p95_workflow_completion_sec": finite(
                aggregate.get("p95_workflow_completion_sec")
            ),
            "mean_critical_sec": finite(aggregate.get("mean_critical_sec")),
            "p95_critical_sec": finite(aggregate.get("p95_critical_sec")),
        }
        for case in cases:
            name = str(case.get("instance_id") or "")
            parsed = case.get("parsed")
            if not name or not isinstance(parsed, dict):
                raise RuntimeError(f"工作流结果不完整：{path}")
            if name in workflows:
                raise RuntimeError(f"工作流重复：{name}")
            records = (case.get("flowd_v2_stats") or {}).get("records") or []
            stats = records[-1] if records and isinstance(records[-1], dict) else {}
            workflows[name] = {
                "suite_id": suite_id,
                "elapsed_sec": finite(case.get("elapsed_sec")),
                "critical_sec": finite(parsed.get("critical_sec")),
                "confirmed_blocking_waits": int(stats.get("confirmed_blocking_waits") or 0),
                "progress_group_relays": int(stats.get("progress_group_relays") or 0),
                "return_promotions": int(stats.get("return_promotions") or 0),
                "first_relation_promotions": int(stats.get("first_relation_promotions") or 0),
                "parallel_width_limits": int(stats.get("parallel_width_limits") or 0),
                "parallel_width_borrows": int(stats.get("parallel_width_borrows") or 0),
                "provenance_tasks": int(stats.get("provenance_tasks") or 0),
                "locality_hits": int(stats.get("locality_hits") or 0),
                "locality_fallbacks": int(stats.get("locality_fallbacks") or 0),
                "stale_delays": int(stats.get("stale_delays") or 0),
            }
    if len(suites) != 10 or len(workflows) != 50:
        raise RuntimeError(f"覆盖不完整：{len(suites)} 个组合、{len(workflows)} 个工作流")
    return suites, workflows


def aggregate(suites: dict[str, dict[str, Any]], workflows: dict[str, dict[str, Any]]) -> dict[str, float]:
    elapsed = [finite(row["elapsed_sec"]) for row in workflows.values()]
    critical = [finite(row["critical_sec"]) for row in workflows.values()]
    makespans = [finite(row["makespan_sec"]) for row in suites.values()]
    return {
        "sum_suite_makespan_sec": sum(makespans),
        "mean_suite_makespan_sec": statistics.fmean(makespans),
        "mean_workflow_completion_sec": statistics.fmean(elapsed),
        "p95_workflow_completion_sec": percentile(elapsed, 0.95),
        "mean_critical_sec": statistics.fmean(critical),
        "p95_critical_sec": percentile(critical, 0.95),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", type=pathlib.Path, required=True)
    parser.add_argument("--stable", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    frozen = load(args.frozen)
    frozen_workflows = {
        str(row["workflow"]): row for row in frozen.get("workflows", [])
        if isinstance(row, dict) and row.get("workflow")
    }
    current_suites, current_workflows = read_latest(args.stable)
    if set(frozen_workflows) != set(current_workflows):
        raise RuntimeError("冻结结果与当前稳定版本的工作流集合不同")

    aggregates = {
        "linux": dict(frozen["aggregates"]["default"]),
        "scx_simple": dict(frozen["aggregates"]["scx_simple"]),
        "previous_v2": dict(frozen["aggregates"]["flowcausal_v2_automatic_weight"]),
        "current_v2": aggregate(current_suites, current_workflows),
    }
    comparisons: dict[str, dict[str, float]] = {}
    for reference in ("linux", "scx_simple", "previous_v2"):
        comparisons[f"current_vs_{reference}"] = {
            key: improvement(finite(aggregates[reference][key]), finite(aggregates["current_v2"][key]))
            for key in aggregates["current_v2"]
        }

    workflow_rows: list[dict[str, Any]] = []
    for name in sorted(current_workflows):
        old = frozen_workflows[name]
        current = current_workflows[name]
        row = {
            "workflow": name,
            "suite_id": current["suite_id"],
            "linux_sec": finite(old["default"]["elapsed_sec"]),
            "scx_simple_sec": finite(old["scx_simple"]["elapsed_sec"]),
            "previous_v2_sec": finite(old["flowcausal_v2_automatic_weight"]["elapsed_sec"]),
            "current_v2_sec": finite(current["elapsed_sec"]),
            "confirmed_blocking_waits": current["confirmed_blocking_waits"],
            "progress_group_relays": current["progress_group_relays"],
            "return_promotions": current["return_promotions"],
            "first_relation_promotions": current["first_relation_promotions"],
            "parallel_width_limits": current["parallel_width_limits"],
            "parallel_width_borrows": current["parallel_width_borrows"],
            "provenance_tasks": current["provenance_tasks"],
            "locality_hits": current["locality_hits"],
            "locality_fallbacks": current["locality_fallbacks"],
            "stale_delays": current["stale_delays"],
            "process_structure": dict(old.get("process_structure") or {}),
        }
        for reference in ("linux", "scx_simple", "previous_v2"):
            row[f"current_vs_{reference}_percent"] = improvement(
                finite(row[f"{reference}_sec"]), finite(row["current_v2_sec"])
            )
        workflow_rows.append(row)

    result = {
        "schema": "flowcausal.v2.latest-all-workflow.v1",
        "scope": {
            "suite_count": 10,
            "workflow_count": 50,
            "current_trials_per_suite": 1,
            "cpus": "0-7",
            "start_method": "concurrent",
            "interpretation": "最新实现的单轮全覆盖初测，用于发现规律和选择复验对象",
        },
        "sources": {
            "frozen": str(args.frozen.resolve()),
            "stable": str(args.stable.resolve()),
        },
        "aggregates": aggregates,
        "comparisons": comparisons,
        "suites": current_suites,
        "workflows": workflow_rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "最新V2全工作流比较.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    labels = {
        "linux": "Linux 原生调度器",
        "scx_simple": "scx_simple",
        "previous_v2": "此前 V2",
        "current_v2": "当前稳定 V2",
    }
    lines = [
        "# FlowCausal V2 最新全工作流比较\n",
        "最新实现把 50 个工作流分为 10 组，每组 5 个同时启动；每个工作流恰好出现一次。基线来自同一任务、同一 0—7 号处理器设置的冻结记录，当前结果为单轮初测，用于发现规律和选择复验对象。\n",
        "## 总体结果\n",
        "| 调度方式 | 10 组总用时（秒） | 平均工作流完成时间（秒） | 工作流完成时间 95 分位（秒） | 平均关键命令时间（秒） |",
        "|---|---:|---:|---:|---:|",
    ]
    for key in labels:
        row = aggregates[key]
        lines.append(
            f"| {labels[key]} | {row['sum_suite_makespan_sec']:.3f} | "
            f"{row['mean_workflow_completion_sec']:.3f} | "
            f"{row['p95_workflow_completion_sec']:.3f} | {row['mean_critical_sec']:.3f} |"
        )
    lines.extend(["\n## 当前实现的变化\n", "正数表示当前实现缩短了时间。\n",
                  "| 对照 | 10 组总用时 | 平均工作流完成时间 | 工作流完成时间 95 分位 | 平均关键命令时间 |",
                  "|---|---:|---:|---:|---:|"])
    for reference in ("linux", "scx_simple", "previous_v2"):
        row = comparisons[f"current_vs_{reference}"]
        lines.append(
            f"| {labels[reference]} | {row['sum_suite_makespan_sec']:.2f}% | "
            f"{row['mean_workflow_completion_sec']:.2f}% | "
            f"{row['p95_workflow_completion_sec']:.2f}% | {row['mean_critical_sec']:.2f}% |"
        )
    lines.extend(["\n## 十个并发组合\n", "| 组合 | 当前稳定 V2（秒） |", "|---|---:|"])
    for suite_id, row in result["suites"].items():
        lines.append(f"| {suite_id} | {row['makespan_sec']:.3f} |")
    lines.extend(["\n## 逐工作流结果\n", "| 工作流 | Linux | scx_simple | 此前 V2 | 当前稳定 V2 | 相对 Linux | 相对 scx_simple | 相对此前 V2 |", "|---|---:|---:|---:|---:|---:|---:|---:|"])
    for row in sorted(workflow_rows, key=lambda item: item["current_vs_previous_v2_percent"], reverse=True):
        lines.append(
            f"| {row['workflow']} | {row['linux_sec']:.3f} | {row['scx_simple_sec']:.3f} | "
            f"{row['previous_v2_sec']:.3f} | {row['current_v2_sec']:.3f} | "
            f"{row['current_vs_linux_percent']:.2f}% | {row['current_vs_scx_simple_percent']:.2f}% | "
            f"{row['current_vs_previous_v2_percent']:.2f}% |"
        )
    md_path = args.output / "最新V2全工作流比较.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

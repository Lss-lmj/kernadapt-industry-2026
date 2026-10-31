#!/usr/bin/env python3
"""比较同机逐工作流运行的 scx_simple 与 FlowCausal v2 结果。"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def rows_by_workflow(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    summary = read_json(root / "结果汇总.json")
    rows = summary.get("workflows")
    if not isinstance(rows, list):
        raise RuntimeError(f"汇总缺少工作流记录：{root}")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("workflow"):
            continue
        result[str(row["workflow"])] = row
    return result


def number(row: dict[str, Any], name: str) -> float | None:
    value = row.get(name)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def improvement(reference: float | None, current: float | None) -> float | None:
    if reference is None or current is None or reference <= 0:
        return None
    return (reference - current) / reference * 100.0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def format_value(value: object, digits: int = 2) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{float(value):.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="比较 FlowCausal v2 的 50 个工作流")
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--flowcausal", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    reference = rows_by_workflow(args.reference)
    flowcausal = rows_by_workflow(args.flowcausal)
    names = sorted(set(reference) | set(flowcausal))
    rows: list[dict[str, Any]] = []
    for name in names:
        base = reference.get(name, {})
        current = flowcausal.get(name, {})
        base_elapsed = number(base, "elapsed_sec")
        current_elapsed = number(current, "elapsed_sec")
        base_critical = number(base, "critical_sec")
        current_critical = number(current, "critical_sec")
        base_tool = number(base, "synchronous_tool_p95_sec")
        current_tool = number(current, "synchronous_tool_p95_sec")
        rows.append(
            {
                "workflow": name,
                "reference_status": base.get("status", "missing"),
                "flowcausal_status": current.get("status", "missing"),
                "reference_elapsed_sec": base_elapsed,
                "flowcausal_elapsed_sec": current_elapsed,
                "elapsed_improvement_percent": improvement(base_elapsed, current_elapsed),
                "reference_critical_sec": base_critical,
                "flowcausal_critical_sec": current_critical,
                "critical_improvement_percent": improvement(base_critical, current_critical),
                "reference_tool_p95_sec": base_tool,
                "flowcausal_tool_p95_sec": current_tool,
                "tool_p95_improvement_percent": improvement(base_tool, current_tool),
                "return_promotions": current.get("return_promotions", 0),
                "interactive_runs": current.get("interactive_runs", 0),
                "stale_delays": current.get("stale_delays", 0),
                "locality_hits": current.get("locality_hits", 0),
                "parallel_width_limits": current.get("parallel_width_limits", 0),
            }
        )

    comparable = [
        row for row in rows
        if row["reference_status"] == "pass"
        and row["flowcausal_status"] == "pass"
        and row["elapsed_improvement_percent"] is not None
    ]
    elapsed_changes = [float(row["elapsed_improvement_percent"]) for row in comparable]
    critical_changes = [
        float(row["critical_improvement_percent"])
        for row in comparable if row["critical_improvement_percent"] is not None
    ]
    tool_changes = [
        float(row["tool_p95_improvement_percent"])
        for row in comparable if row["tool_p95_improvement_percent"] is not None
    ]
    ratios = [
        float(row["flowcausal_elapsed_sec"]) / float(row["reference_elapsed_sec"])
        for row in comparable
        if float(row["reference_elapsed_sec"]) > 0
        and float(row["flowcausal_elapsed_sec"]) > 0
    ]
    geometric_improvement = (
        (1.0 - math.exp(statistics.fmean(math.log(value) for value in ratios))) * 100.0
        if ratios else None
    )
    result = {
        "schema": "flowcausal.v2.all-workflows-comparison.v1",
        "reference": str(args.reference.resolve()),
        "flowcausal": str(args.flowcausal.resolve()),
        "workflow_count": len(names),
        "comparable_workflows": len(comparable),
        "reference_passed": sum(row["reference_status"] == "pass" for row in rows),
        "flowcausal_passed": sum(row["flowcausal_status"] == "pass" for row in rows),
        "elapsed": {
            "geometric_mean_improvement_percent": geometric_improvement,
            "median_improvement_percent": statistics.median(elapsed_changes) if elapsed_changes else None,
            "p05_improvement_percent": percentile(elapsed_changes, 0.05),
            "p95_improvement_percent": percentile(elapsed_changes, 0.95),
            "improved_count": sum(value > 0 for value in elapsed_changes),
            "within_two_percent_count": sum(abs(value) <= 2 for value in elapsed_changes),
            "regressed_over_five_percent_count": sum(value < -5 for value in elapsed_changes),
        },
        "critical": {
            "median_improvement_percent": statistics.median(critical_changes) if critical_changes else None,
            "improved_count": sum(value > 0 for value in critical_changes),
        },
        "synchronous_tool_p95": {
            "median_improvement_percent": statistics.median(tool_changes) if tool_changes else None,
            "improved_count": sum(value > 0 for value in tool_changes),
        },
        "workflows": rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    ranked = sorted(
        comparable,
        key=lambda row: float(row["elapsed_improvement_percent"]),
        reverse=True,
    )
    lines = [
        "# FlowCausal v2 全工作流逐项对比\n",
        f"可比较工作流：{len(comparable)}/{len(names)}。",
        f"总完成时间几何平均变化：{format_value(geometric_improvement)}%。",
        f"总完成时间中位变化：{format_value(result['elapsed']['median_improvement_percent'])}%。",
        f"完成时间改善的工作流：{result['elapsed']['improved_count']} 个；变化在正负 2% 内：{result['elapsed']['within_two_percent_count']} 个；增加超过 5%：{result['elapsed']['regressed_over_five_percent_count']} 个。\n",
        "| 工作流 | 总完成时间：参考（秒） | 总完成时间：V2（秒） | 变化 | 关键完成时间变化 | 同步工具 P95 变化 | 推进次数 | 短交互运行 | 后台延后 | 近期处理器复用 | 并行度限制 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ranked:
        lines.append(
            "| {workflow} | {base} | {current} | {elapsed}% | {critical}% | "
            "{tool}% | {promotions} | {interactive} | {background} | {locality} | {width} |".format(
                workflow=row["workflow"],
                base=format_value(row["reference_elapsed_sec"], 3),
                current=format_value(row["flowcausal_elapsed_sec"], 3),
                elapsed=format_value(row["elapsed_improvement_percent"]),
                critical=format_value(row["critical_improvement_percent"]),
                tool=format_value(row["tool_p95_improvement_percent"]),
                promotions=row["return_promotions"],
                interactive=row["interactive_runs"],
                background=row["stale_delays"],
                locality=row["locality_hits"],
                width=row["parallel_width_limits"],
            )
        )
    (args.output / "结果摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if len(comparable) == len(names) == 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())

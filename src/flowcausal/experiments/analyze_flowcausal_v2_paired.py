#!/usr/bin/env python3
"""对同一批次的 scx_simple 与 FlowCausal v2 结果做配对统计。"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any

from scipy.stats import t


METRICS = (
    ("makespan_sec", "总完成时间"),
    ("mean_workflow_completion_sec", "平均工作流完成时间"),
    ("p95_synchronous_tool_service_sec", "工具阻塞时间 95 分位"),
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"结果不是 JSON 对象：{path}")
    return value


def rows_for(payload: dict[str, Any], method: str) -> list[dict[str, Any]]:
    rows = [row for row in payload.get("rows", []) if row.get("method") == method]
    if not rows:
        raise SystemExit(f"没有找到方法：{method}")
    return rows


def summarize(values: list[float]) -> dict[str, float]:
    count = len(values)
    mean = statistics.fmean(values)
    stdev = statistics.stdev(values) if count > 1 else 0.0
    margin = (
        float(t.ppf(0.975, count - 1)) * stdev / math.sqrt(count)
        if count > 1
        else 0.0
    )
    return {
        "count": count,
        "mean": mean,
        "sample_stdev": stdev,
        "confidence_95_low": mean - margin,
        "confidence_95_high": mean + margin,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="计算 FlowCausal v2 的配对置信区间")
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--reference", default="scx_simple")
    parser.add_argument("--candidate", default="flowcausal_v2")
    args = parser.parse_args()

    payload = load(args.input)
    reference = rows_for(payload, args.reference)
    candidate = rows_for(payload, args.candidate)
    if len(reference) != len(candidate):
        raise SystemExit(
            f"配对数量不一致：{args.reference}={len(reference)}，"
            f"{args.candidate}={len(candidate)}"
        )

    metrics: dict[str, Any] = {}
    for field, label in METRICS:
        reductions = []
        absolute_differences = []
        pairs = []
        for index, (base, value) in enumerate(zip(reference, candidate, strict=True), 1):
            base_value = float(base[field])
            candidate_value = float(value[field])
            reduction = (base_value - candidate_value) / base_value * 100.0
            reductions.append(reduction)
            absolute_differences.append(base_value - candidate_value)
            pairs.append(
                {
                    "pair": index,
                    "reference": base_value,
                    "candidate": candidate_value,
                    "reduction_percent": reduction,
                    "reference_result": base.get("result"),
                    "candidate_result": value.get("result"),
                }
            )
        metrics[field] = {
            "label": label,
            "reference": summarize([float(row[field]) for row in reference]),
            "candidate": summarize([float(row[field]) for row in candidate]),
            "paired_absolute_difference": summarize(absolute_differences),
            "paired_reduction_percent": summarize(reductions),
            "pairs": pairs,
        }

    result = {
        "schema": "flowcausal.v2.paired-statistics.v1",
        "source": str(args.input.resolve()),
        "reference": args.reference,
        "candidate": args.candidate,
        "pair_count": len(reference),
        "confidence_method": "two-sided Student t interval over paired reductions",
        "metrics": metrics,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "配对统计.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal v2 十轮配对统计",
        "",
        f"参考方法：`{args.reference}`；候选方法：`{args.candidate}`；配对数：{len(reference)}。",
        "每一对按结果表中的相同批次顺序对应。区间为每轮相对变化的双侧 95% Student t 置信区间；正数表示候选方法缩短时间。",
        "",
        "| 指标 | 参考均值（秒） | V2 均值（秒） | 配对改善均值 | 95% 置信区间 |",
        "|---|---:|---:|---:|---:|",
    ]
    for field, _ in METRICS:
        item = metrics[field]
        reduction = item["paired_reduction_percent"]
        lines.append(
            f"| {item['label']} | {item['reference']['mean']:.3f} | "
            f"{item['candidate']['mean']:.3f} | {reduction['mean']:.2f}% | "
            f"[{reduction['confidence_95_low']:.2f}%, "
            f"{reduction['confidence_95_high']:.2f}%] |"
        )
    (args.output / "配对统计.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output / "配对统计.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

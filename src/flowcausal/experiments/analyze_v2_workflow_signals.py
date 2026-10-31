#!/usr/bin/env python3
"""分析 50 个工作流的运行时信号与完成时间变化，不参与在线调度。"""

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


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    dx = [value - mx for value in xs]
    dy = [value - my for value in ys]
    denominator = math.sqrt(sum(value * value for value in dx) * sum(value * value for value in dy))
    return sum(a * b for a, b in zip(dx, dy)) / denominator if denominator else None


def ranks(values: list[float]) -> list[float]:
    ordered = sorted((value, index) for index, value in enumerate(values))
    result = [0.0] * len(values)
    start = 0
    while start < len(ordered):
        end = start + 1
        while end < len(ordered) and ordered[end][0] == ordered[start][0]:
            end += 1
        rank = (start + end - 1) / 2.0 + 1.0
        for _, index in ordered[start:end]:
            result[index] = rank
        start = end
    return result


def spearman(xs: list[float], ys: list[float]) -> float | None:
    return pearson(ranks(xs), ranks(ys))


def numeric(row: dict[str, Any], path: tuple[str, ...]) -> float | None:
    value: Any = row
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) and math.isfinite(float(value)) else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    source = load(args.input)
    rows = [row for row in source.get("workflows", []) if isinstance(row, dict)]
    if len(rows) != 50:
        raise RuntimeError(f"需要 50 个工作流，实际为 {len(rows)}")

    features = {
        "阻塞等待次数": ("confirmed_blocking_waits",),
        "等待关系向下传递次数": ("progress_group_relays",),
        "提前运行次数": ("return_promotions",),
        "首次等待关系提前次数": ("first_relation_promotions",),
        "并行度限制次数": ("parallel_width_limits",),
        "空闲处理器借用次数": ("parallel_width_borrows",),
        "记录的相关进程数": ("provenance_tasks",),
        "近期处理器复用成功次数": ("locality_hits",),
        "近期处理器复用失败次数": ("locality_fallbacks",),
        "最大进程层数": ("process_structure", "maximum_depth"),
        "深层进程处理器时间占比": ("process_structure", "return_related_deeper_runtime_share_percent"),
        "深层进程排队时间占比": ("process_structure", "return_related_deeper_runnable_wait_share_percent"),
    }
    outcomes = {
        "相对 Linux 的完成时间变化": ("current_vs_linux_percent",),
        "相对 scx_simple 的完成时间变化": ("current_vs_scx_simple_percent",),
    }
    correlations: list[dict[str, Any]] = []
    for feature_name, feature_path in features.items():
        for outcome_name, outcome_path in outcomes.items():
            pairs = [
                (numeric(row, feature_path), numeric(row, outcome_path))
                for row in rows
            ]
            usable = [(x, y) for x, y in pairs if x is not None and y is not None]
            xs = [x for x, _ in usable]
            ys = [y for _, y in usable]
            correlations.append({
                "feature": feature_name,
                "outcome": outcome_name,
                "count": len(usable),
                "pearson": pearson(xs, ys),
                "spearman": spearman(xs, ys),
            })

    groups: dict[str, dict[str, Any]] = {}
    definitions = {
        "发生提前运行": lambda row: float(row.get("return_promotions") or 0) > 0,
        "没有提前运行": lambda row: float(row.get("return_promotions") or 0) == 0,
        "深层进程处理器时间占比至少10%": lambda row: float((row.get("process_structure") or {}).get("return_related_deeper_runtime_share_percent") or 0) >= 10,
        "深层进程处理器时间占比低于10%": lambda row: float((row.get("process_structure") or {}).get("return_related_deeper_runtime_share_percent") or 0) < 10,
    }
    for name, predicate in definitions.items():
        selected = [row for row in rows if predicate(row)]
        linux = [float(row["current_vs_linux_percent"]) for row in selected]
        simple = [float(row["current_vs_scx_simple_percent"]) for row in selected]
        groups[name] = {
            "count": len(selected),
            "mean_vs_linux_percent": statistics.fmean(linux),
            "median_vs_linux_percent": statistics.median(linux),
            "mean_vs_scx_simple_percent": statistics.fmean(simple),
            "median_vs_scx_simple_percent": statistics.median(simple),
        }

    strongest = sorted(
        correlations,
        key=lambda row: abs(float(row["spearman"] or 0)),
        reverse=True,
    )
    result = {
        "schema": "flowcausal.v2.workflow-signals.v1",
        "input": str(args.input.resolve()),
        "workflow_count": len(rows),
        "correlations": correlations,
        "groups": groups,
        "strongest_rank_correlations": strongest[:10],
        "interpretation": "离线分析只用于发现共同规律；在线调度不得读取工作流名称、未来运行时间或本表结果。",
    }
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "工作流信号与性能关系.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    lines = [
        "# 工作流运行时信号与性能关系\n",
        "本分析把 50 个工作流已经发生的内核事件与完成时间变化对应起来，用于判断哪些规律值得写入调度器。它不参与在线运行，也不向调度器提供工作流名称、未来进程数或预先测得的完成时间。\n",
        "## 最强的秩相关关系\n",
        "相关系数接近 1 或 -1 才表示稳定的单调关系；接近 0 表示该信号不能单独决定是否加速。\n",
        "| 运行时信号 | 比较对象 | 工作流数 | 皮尔逊相关系数 | 斯皮尔曼秩相关系数 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in strongest:
        lines.append(
            f"| {row['feature']} | {row['outcome']} | {row['count']} | "
            f"{float(row['pearson'] or 0):.3f} | {float(row['spearman'] or 0):.3f} |"
        )
    lines.extend(["\n## 分组结果\n", "| 工作流条件 | 数量 | 相对 Linux 平均变化 | 相对 Linux 中位数变化 | 相对 scx_simple 平均变化 | 相对 scx_simple 中位数变化 |", "|---|---:|---:|---:|---:|---:|"])
    for name, row in groups.items():
        lines.append(
            f"| {name} | {row['count']} | {row['mean_vs_linux_percent']:.2f}% | "
            f"{row['median_vs_linux_percent']:.2f}% | {row['mean_vs_scx_simple_percent']:.2f}% | "
            f"{row['median_vs_scx_simple_percent']:.2f}% |"
        )
    lines.extend([
        "\n## 结论\n",
        "单个计数若与完成时间变化只有弱相关，就不能直接变成提高权重的规则。下一轮算法只能采用同时具备明确因果位置和在线可观测性的信号，并先在对应的加速与变慢案例上验证。",
    ])
    md_path = args.output / "工作流信号与性能关系.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

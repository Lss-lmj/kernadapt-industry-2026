#!/usr/bin/env python3
"""汇总工具代次与并行汇合套件的统一参考结果。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


SUITES = {
    "behavior-causal-overlap": "真实代次重叠",
    "behavior-join-frontier": "并行汇合",
    "behavior-causal-mixed": "因果执行混合",
}

METRICS = (
    "makespan_sec_mean",
    "mean_critical_sec_mean",
    "mean_causal_window_sec_mean",
    "p95_causal_window_sec_mean",
    "mean_join_window_sec_mean",
    "p95_join_window_sec_mean",
    "runnable_wait_sec_mean",
    "migrations_mean",
)


def reduction(reference: float, value: float) -> float | None:
    return (reference - value) / reference * 100.0 if reference > 0 else None


def number(value: Any) -> str:
    return "—" if value is None else f"{float(value):.3f}"


def percent(value: Any) -> str:
    return "—" if value is None else f"{float(value):+.2f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总执行关系套件的参考结果")
    parser.add_argument("--result-root", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.result_root).resolve()

    suites: list[dict[str, Any]] = []
    for prefix, display_name in SUITES.items():
        candidates = sorted(root.glob(f"{prefix}-3trial-*/结果表.json"))
        if len(candidates) != 1:
            raise SystemExit(f"{prefix} 应当恰好有一份三轮结果，实际为 {len(candidates)}")
        source = candidates[0]
        data = json.loads(source.read_text(encoding="utf-8"))
        methods = {
            str(item.get("method")): item
            for item in data.get("summary", [])
            if isinstance(item, dict)
        }
        required = {"scx_simple", "flowcausal_v2"}
        if not required.issubset(methods):
            raise SystemExit(f"{source} 方法集合不完整：{sorted(methods)}")
        if any(int(item.get("completed_trials") or 0) != 3 for item in methods.values()):
            raise SystemExit(f"{source} 没有完成每种方法三轮运行")
        if any(int(item.get("dropped_events") or 0) != 0 for item in methods.values()):
            raise SystemExit(f"{source} 存在 C/eBPF 事件丢失")

        reference = methods["scx_simple"]
        rows = []
        method_order = [
            "scx_simple",
            *sorted(set(methods) - {"scx_simple", "flowcausal_v2"}),
            "flowcausal_v2",
        ]
        for method_name in method_order:
            item = methods[method_name]
            row = {
                "method": method_name,
                "completed_trials": int(item["completed_trials"]),
                "dropped_events": int(item["dropped_events"]),
            }
            for metric in METRICS:
                value = float(item.get(metric) or 0.0)
                row[metric] = value
                row[f"{metric}_reduction_percent"] = reduction(
                    float(reference.get(metric) or 0.0), value
                )
            rows.append(row)
        suites.append(
            {
                "suite": prefix,
                "display_name": display_name,
                "source": source.relative_to(root).as_posix(),
                "methods": rows,
            }
        )

    output = {
        "schema": "harness_replaybench.execution_relation_comparison.v1",
        "baseline": "scx_simple",
        "trials_per_method": 3,
        "cpu_set": "0-7",
        "release_pattern": "concurrent",
        "replay_mode": "tool-execution",
        "suites": suites,
    }
    (root / "工具代次与汇合统一结果.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# 工具代次与并行汇合统一结果",
        "",
        "每种方法在每个套件上运行三轮；处理器范围为 0–7，同时启动，工具执行重放。正百分比表示相对 `scx_simple` 耗时缩短。",
        "",
        "| 套件 | 方法 | 总完成时间 | 平均关键完成时间 | 平均代次重叠窗口 | 95 分位代次重叠窗口 | 平均并行汇合窗口 | 95 分位并行汇合窗口 | 总完成时间变化 | 关键完成时间变化 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for suite in suites:
        for row in suite["methods"]:
            lines.append(
                "| {suite} | {method} | {makespan} | {critical} | {causal} | {causal95} | {join} | {join95} | {makespan_change} | {critical_change} |".format(
                    suite=suite["display_name"],
                    method=row["method"],
                    makespan=number(row["makespan_sec_mean"]),
                    critical=number(row["mean_critical_sec_mean"]),
                    causal=number(row["mean_causal_window_sec_mean"] or None),
                    causal95=number(row["p95_causal_window_sec_mean"] or None),
                    join=number(row["mean_join_window_sec_mean"] or None),
                    join95=number(row["p95_join_window_sec_mean"] or None),
                    makespan_change=percent(row["makespan_sec_mean_reduction_percent"]),
                    critical_change=percent(row["mean_critical_sec_mean_reduction_percent"]),
                )
            )
    (root / "工具代次与汇合统一结果.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "suites": len(suites),
        "rows": sum(len(item["methods"]) for item in suites),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

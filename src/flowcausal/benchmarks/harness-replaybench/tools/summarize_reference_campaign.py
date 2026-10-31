#!/usr/bin/env python3
"""把多个参考方法结果目录合并为一份可阅读、可更新的总表。"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
from collections import defaultdict
from typing import Any


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def reduction(reference: float, value: float) -> float:
    return (reference - value) / reference * 100.0 if reference > 0 else 0.0


def suite_summary(path: pathlib.Path) -> dict[str, Any]:
    registry = read_json(path / "结果表.json")
    methods = [str(item["name"]) for item in registry.get("methods", [])]
    requested = int(registry.get("trials_requested") or 0)
    aggregate = {str(item["method"]): item for item in registry.get("summary", [])}
    per_case: dict[str, dict[str, list[dict[str, float]]]] = defaultdict(lambda: defaultdict(list))
    flow_stats: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for method in methods:
        for trial in range(1, requested + 1):
            result_path = path / method / f"trial-{trial:02d}" / "result.json"
            if not result_path.is_file():
                continue
            result = read_json(result_path)
            if result.get("status") != "pass":
                continue
            for case in result.get("cases", []):
                if not isinstance(case, dict):
                    continue
                per_case[str(case.get("instance_id") or "")][method].append(
                    {
                        "elapsed_sec": float(case.get("elapsed_sec") or 0),
                        "critical_sec": float(case.get("critical_sec") or 0),
                    }
                )
            for session in result.get("flowcausal_control", {}).get("sessions", []):
                if isinstance(session, dict):
                    flow_stats[str(session.get("instance_id") or "")].append(
                        dict(session.get("stats") or {})
                    )

    cases: list[dict[str, Any]] = []
    for instance, by_method in sorted(per_case.items()):
        rows: list[dict[str, Any]] = []
        reference_rows = by_method.get("scx_simple", [])
        reference_elapsed = statistics.fmean(item["elapsed_sec"] for item in reference_rows)
        reference_critical = statistics.fmean(item["critical_sec"] for item in reference_rows)
        for method in methods:
            values = by_method.get(method, [])
            if not values:
                continue
            elapsed = statistics.fmean(item["elapsed_sec"] for item in values)
            critical = statistics.fmean(item["critical_sec"] for item in values)
            rows.append(
                {
                    "method": method,
                    "trials": len(values),
                    "elapsed_sec_mean": elapsed,
                    "critical_sec_mean": critical,
                    "elapsed_reduction_percent": reduction(reference_elapsed, elapsed),
                    "critical_reduction_percent": reduction(reference_critical, critical),
                }
            )
        flow = flow_stats.get(instance, [])
        cases.append(
            {
                "instance_id": instance,
                "methods": rows,
                "flowcausal": {
                    "trials": len(flow),
                    "tail_promotions_mean": statistics.fmean(
                        float(item.get("tail_promotions") or 0) for item in flow
                    ) if flow else 0.0,
                    "budget_limited_mean": statistics.fmean(
                        float(item.get("budget_limited") or 0) for item in flow
                    ) if flow else 0.0,
                    "wait_sec_mean": statistics.fmean(
                        float(item.get("wait_ns") or 0) / 1_000_000_000 for item in flow
                    ) if flow else 0.0,
                },
            }
        )
    return {
        "suite": path.name,
        "source": path.as_posix(),
        "trials_requested": requested,
        "failures": list(registry.get("failures", [])),
        "aggregate": [aggregate[method] for method in methods if method in aggregate],
        "cases": cases,
    }


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HarnessReplayBench 1.0 参考方法总表",
        "",
        "所有百分比均相对同一套件中的 `scx_simple`，正数表示耗时缩短。每个方法采用三轮交错顺序运行。",
        "",
    ]
    for suite in payload["suites"]:
        lines.extend(
            [
                f"## {suite['suite']}",
                "",
                "| 方法 | 轮次 | 总完成时间（秒） | 标准差 | 平均关键完成时间（秒） | 总完成时间变化 | 关键完成时间变化 |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for item in suite["aggregate"]:
            relative = item.get("relative_to_scx_simple_percent", {})
            lines.append(
                "| {method} | {trials} | {makespan:.3f} | {stdev:.3f} | {critical:.3f} | {mg:+.2f}% | {cg:+.2f}% |".format(
                    method=item["method"],
                    trials=item["completed_trials"],
                    makespan=float(item["makespan_sec_mean"]),
                    stdev=float(item["makespan_sec_stdev"]),
                    critical=float(item["mean_critical_sec_mean"]),
                    mg=float(relative.get("makespan_reduction") or 0),
                    cg=float(relative.get("mean_critical_reduction") or 0),
                )
            )
        lines.extend(
            [
                "",
                "### 逐任务关键完成时间",
                "",
                "| 任务 | 方法 | 平均耗时（秒） | 平均关键完成时间（秒） | 关键完成时间变化 |",
                "|---|---|---:|---:|---:|",
            ]
        )
        for case in suite["cases"]:
            for item in case["methods"]:
                lines.append(
                    "| {instance} | {method} | {elapsed:.3f} | {critical:.3f} | {gain:+.2f}% |".format(
                        instance=case["instance_id"],
                        method=item["method"],
                        elapsed=float(item["elapsed_sec_mean"]),
                        critical=float(item["critical_sec_mean"]),
                        gain=float(item["critical_reduction_percent"]),
                    )
                )
        flow_rows = [case for case in suite["cases"] if case["flowcausal"]["trials"]]
        if flow_rows:
            lines.extend(
                [
                    "",
                    "### FlowCausal 机制计数",
                    "",
                    "| 任务 | 尾部推进次数 | 预算受限次数 | 会话累计等待（秒） |",
                    "|---|---:|---:|---:|",
                ]
            )
            for case in flow_rows:
                item = case["flowcausal"]
                lines.append(
                    f"| {case['instance_id']} | {item['tail_promotions_mean']:.2f} | "
                    f"{item['budget_limited_mean']:.2f} | {item['wait_sec_mean']:.3f} |"
                )
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 HarnessReplayBench 参考方法实验")
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown", required=True)
    args = parser.parse_args()
    root = pathlib.Path(args.root).resolve()
    suites = [suite_summary(path) for path in sorted(root.iterdir()) if (path / "结果表.json").is_file()]
    payload = {
        "schema": "harness_replaybench.reference_campaign.v1",
        "suites": suites,
        "summary": {
            "suites": len(suites),
            "runs": sum(
                int(item.get("completed_trials") or 0)
                for suite in suites
                for item in suite["aggregate"]
            ),
            "failures": sum(len(suite["failures"]) for suite in suites),
        },
    }
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report = pathlib.Path(args.markdown).resolve()
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(markdown(payload), encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if payload["summary"]["failures"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

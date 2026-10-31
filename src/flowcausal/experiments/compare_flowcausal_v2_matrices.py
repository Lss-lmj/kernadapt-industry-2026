#!/usr/bin/env python3
"""将最新版 FlowCausal v2 与已经保存的同条件实验作比较。

两组结果可能来自不同时间，因此按独立样本计算置信区间，不能把编号
相同的轮次当作配对实验。这样每个调度器版本只需进行一次符合要求的
实验，同时不会夸大分别采集的数据之间的统计联系。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any

from scipy.stats import t

from analyze_flowcausal_v2_final_matrix import (
    METRICS,
    aggregate_value,
    case_value,
    load,
    summary,
    validate,
)


CONTROL_FIELDS = (
    "cleanup_success_artifacts",
    "copy_keep_build_artifacts",
    "drop_caches_before_trial",
    "pace_scale",
    "replay_mode",
    "taskset",
)


def variant_trials(payload: dict[str, Any], name: str) -> list[dict[str, Any]]:
    names = {
        str(item.get("name") or "")
        for item in payload.get("variants", [])
        if isinstance(item, dict) and item.get("name")
    }
    if name not in names:
        raise SystemExit(f"结果中没有方法：{name}")
    # A matrix may also contain a fully recorded but invalid method (for
    # example a scheduler detached by the watchdog).  Validate the requested
    # method strictly without allowing an unrelated method to invalidate it.
    variants = validate(payload, names - {name})
    if name not in variants:
        raise SystemExit(f"结果中没有方法：{name}")
    return variants[name]


def common_trial_facts(trials: list[dict[str, Any]]) -> dict[str, Any]:
    first = trials[0]
    facts = {
        "suite_id": first.get("suite_id"),
        "taskset": first.get("taskset"),
        "replay_mode": first.get("replay_mode"),
        "instances": sorted(str(item.get("instance_id")) for item in first["cases"]),
    }
    for trial in trials[1:]:
        current = {
            "suite_id": trial.get("suite_id"),
            "taskset": trial.get("taskset"),
            "replay_mode": trial.get("replay_mode"),
            "instances": sorted(str(item.get("instance_id")) for item in trial["cases"]),
        }
        if current != facts:
            raise SystemExit("同一种方法的轮次使用了不同任务或运行条件")
    return facts


def check_compatible(
    candidate_payload: dict[str, Any],
    candidate_trials: list[dict[str, Any]],
    reference_payload: dict[str, Any],
    reference_trials: list[dict[str, Any]],
) -> dict[str, Any]:
    candidate_facts = common_trial_facts(candidate_trials)
    reference_facts = common_trial_facts(reference_trials)
    if candidate_facts != reference_facts:
        raise SystemExit(
            "候选结果与历史结果的任务或运行条件不同：\n"
            f"候选={candidate_facts}\n历史={reference_facts}"
        )
    candidate_control = candidate_payload.get("experiment_control", {})
    reference_control = reference_payload.get("experiment_control", {})
    mismatches = {
        field: {
            "candidate": candidate_control.get(field),
            "reference": reference_control.get(field),
        }
        for field in CONTROL_FIELDS
        if candidate_control.get(field) != reference_control.get(field)
    }
    if mismatches:
        raise SystemExit(f"候选结果与历史结果的实验条件不同：{mismatches}")
    return candidate_facts


def independent_reduction(
    reference_values: list[float],
    candidate_values: list[float],
) -> dict[str, float | int]:
    reference_mean = statistics.fmean(reference_values)
    candidate_mean = statistics.fmean(candidate_values)
    reduction = (reference_mean - candidate_mean) / reference_mean * 100.0
    reference_variance = (
        statistics.variance(reference_values) if len(reference_values) > 1 else 0.0
    )
    candidate_variance = (
        statistics.variance(candidate_values) if len(candidate_values) > 1 else 0.0
    )
    reference_term = reference_variance / len(reference_values)
    candidate_term = candidate_variance / len(candidate_values)
    standard_error = math.sqrt(reference_term + candidate_term)
    denominator = 0.0
    if len(reference_values) > 1:
        denominator += reference_term**2 / (len(reference_values) - 1)
    if len(candidate_values) > 1:
        denominator += candidate_term**2 / (len(candidate_values) - 1)
    degrees_of_freedom = (
        (reference_term + candidate_term) ** 2 / denominator
        if denominator > 0
        else 1.0
    )
    margin = float(t.ppf(0.975, degrees_of_freedom)) * standard_error
    difference = reference_mean - candidate_mean
    return {
        "reference_count": len(reference_values),
        "candidate_count": len(candidate_values),
        "reduction_percent": reduction,
        "welch_degrees_of_freedom": degrees_of_freedom,
        "welch_95_low": (difference - margin) / reference_mean * 100.0,
        "welch_95_high": (difference + margin) / reference_mean * 100.0,
    }


def method_statistics(trials: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        field: summary([aggregate_value(trial, field) for trial in trials])
        for field, _ in METRICS
    }


def workflow_statistics(trials: list[dict[str, Any]]) -> dict[str, Any]:
    values: dict[str, dict[str, list[float]]] = {}
    for trial in trials:
        for case in trial["cases"]:
            instance = str(case.get("instance_id") or "")
            row = values.setdefault(
                instance, {"workflow_completion_sec": [], "critical_sec": []}
            )
            for field in row:
                row[field].append(case_value(case, field))
    return {
        instance: {field: summary(items) for field, items in fields.items()}
        for instance, fields in sorted(values.items())
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="将最新版 FlowCausal v2 与同条件历史结果作独立样本比较"
    )
    parser.add_argument("--candidate-input", required=True, type=pathlib.Path)
    parser.add_argument("--candidate", default="flowcausal_v2_automatic_weight")
    parser.add_argument("--reference-input", required=True, type=pathlib.Path)
    parser.add_argument(
        "--reference", action="append", required=True, dest="references"
    )
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    candidate_payload = load(args.candidate_input)
    reference_payload = load(args.reference_input)
    candidate_trials = variant_trials(candidate_payload, args.candidate)

    methods = {args.candidate: method_statistics(candidate_trials)}
    workflows = {args.candidate: workflow_statistics(candidate_trials)}
    comparisons: dict[str, Any] = {}
    facts: dict[str, Any] | None = None
    for reference in args.references:
        reference_trials = variant_trials(reference_payload, reference)
        current_facts = check_compatible(
            candidate_payload,
            candidate_trials,
            reference_payload,
            reference_trials,
        )
        facts = facts or current_facts
        methods[reference] = method_statistics(reference_trials)
        workflows[reference] = workflow_statistics(reference_trials)
        comparisons[reference] = {}
        for field, _ in METRICS:
            comparisons[reference][field] = independent_reduction(
                [aggregate_value(trial, field) for trial in reference_trials],
                [aggregate_value(trial, field) for trial in candidate_trials],
            )

    result = {
        "schema": "flowcausal.v2.independent-matrix-comparison.v1",
        "candidate_source": str(args.candidate_input.resolve()),
        "reference_source": str(args.reference_input.resolve()),
        "candidate": args.candidate,
        "references": args.references,
        "comparison_kind": "independent_samples",
        "conditions": facts,
        "methods": methods,
        "candidate_reduction_percent": comparisons,
        "workflows": workflows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "统计结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal v2 最新实现与历史同条件结果比较",
        "",
        "候选方法没有与历史方法重新交错运行，因此这里按独立样本比较，"
        "不会把轮次编号相同误写成配对实验。所有任务、处理器范围、执行方式、"
        "页缓存处理和产物清理条件均已核对一致。",
        "",
        "| 方法 | 轮数 | 最后完成（秒） | 平均工作流（秒） | 工作流 95 分位（秒） | 同步工具 95 分位（秒） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name in [*args.references, args.candidate]:
        stats = methods[name]
        count = stats[METRICS[0][0]]["count"]
        values = [
            f"{stats[field]['mean']:.3f}±{stats[field]['sample_stdev']:.3f}"
            for field, _ in METRICS[:4]
        ]
        lines.append(f"| `{name}` | {count} | " + " | ".join(values) + " |")

    lines.extend([
        "",
        f"## `{args.candidate}` 相对历史方法的变化",
        "",
        "正数表示最新版缩短时间。区间使用两组独立轮次的 Welch t 方法计算，只描述当前机器上的运行波动。",
        "",
        "| 历史方法 | 最后完成 | 平均工作流 | 工作流 95 分位 | 同步工具 95 分位 |",
        "|---|---:|---:|---:|---:|",
    ])
    for name in args.references:
        values = []
        for field, _ in METRICS[:4]:
            item = comparisons[name][field]
            values.append(
                f"{item['reduction_percent']:.2f}% "
                f"[{item['welch_95_low']:.2f}%, {item['welch_95_high']:.2f}%]"
            )
        lines.append(f"| `{name}` | " + " | ".join(values) + " |")
    (args.output / "统计结果.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    print(args.output / "统计结果.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""汇总 V2 在 50 个工作流并发组合上的初步比较结果。"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any


EXPECTED_VARIANTS = (
    "default",
    "scx_simple",
    "flowcausal_v2_automatic_weight",
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def number(value: object) -> float | None:
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


def fmt(value: object, digits: int = 2) -> str:
    value = number(value)
    return "—" if value is None else f"{value:.{digits}f}"


def last_stats(case: dict[str, Any]) -> dict[str, Any]:
    stats = case.get("flowd_v2_stats")
    if not isinstance(stats, dict):
        return {}
    records = stats.get("records")
    if not isinstance(records, list) or not records:
        return {}
    record = records[-1]
    return record if isinstance(record, dict) else {}


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 V2 的 50 工作流初步并发测试")
    parser.add_argument(
        "--summary",
        type=pathlib.Path,
        action="append",
        required=True,
        help="可重复传入 10 个 harness-replay-matrix-summary.json",
    )
    parser.add_argument("--process-analysis", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    process_analysis = read_json(args.process_analysis)
    process_rows = process_analysis.get("workflows")
    if not isinstance(process_rows, list):
        raise RuntimeError("进程结构分析缺少 workflows")
    process_by_workflow = {
        str(row["workflow"]): row
        for row in process_rows
        if isinstance(row, dict) and row.get("workflow")
    }

    suites: list[dict[str, Any]] = []
    workflow_rows: list[dict[str, Any]] = []
    seen_suites: set[str] = set()
    seen_workflows: set[str] = set()
    variant_elapsed: dict[str, list[float]] = {name: [] for name in EXPECTED_VARIANTS}
    variant_critical: dict[str, list[float]] = {name: [] for name in EXPECTED_VARIANTS}
    variant_makespans: dict[str, list[float]] = {name: [] for name in EXPECTED_VARIANTS}

    for summary_path in args.summary:
        summary = read_json(summary_path)
        if summary.get("status") != "pass":
            raise RuntimeError(f"实验未通过：{summary_path}")
        variants_value = summary.get("variants")
        if not isinstance(variants_value, list):
            raise RuntimeError(f"实验缺少 variants：{summary_path}")
        variants = {
            str(value.get("name")): value
            for value in variants_value
            if isinstance(value, dict) and value.get("name")
        }
        if set(variants) != set(EXPECTED_VARIANTS):
            raise RuntimeError(
                f"调度方式不完整：{summary_path}，实际为 {sorted(variants)}"
            )

        trials: dict[str, dict[str, Any]] = {}
        cases: dict[str, dict[str, dict[str, Any]]] = {}
        suite_id = ""
        for variant_name in EXPECTED_VARIANTS:
            trial_values = variants[variant_name].get("trials")
            if not isinstance(trial_values, list) or len(trial_values) != 1:
                raise RuntimeError(f"{summary_path} 的 {variant_name} 不是恰好一轮")
            trial = trial_values[0]
            if not isinstance(trial, dict) or trial.get("status") != "pass":
                raise RuntimeError(f"{summary_path} 的 {variant_name} 未通过")
            current_suite_id = str(trial.get("suite_id", ""))
            if not current_suite_id:
                raise RuntimeError(f"{summary_path} 缺少 suite_id")
            if suite_id and current_suite_id != suite_id:
                raise RuntimeError(f"{summary_path} 内部 suite_id 不一致")
            suite_id = current_suite_id
            trials[variant_name] = trial
            case_values = trial.get("cases")
            if not isinstance(case_values, list):
                raise RuntimeError(f"{summary_path} 的 {variant_name} 缺少 cases")
            case_map = {
                str(case["instance_id"]): case
                for case in case_values
                if isinstance(case, dict) and case.get("instance_id")
            }
            if len(case_map) != 5 or any(
                case.get("returncode") != 0 for case in case_map.values()
            ):
                raise RuntimeError(f"{summary_path} 的 {variant_name} 不含 5 个成功工作流")
            cases[variant_name] = case_map

        if suite_id in seen_suites:
            raise RuntimeError(f"重复的测试组合：{suite_id}")
        seen_suites.add(suite_id)
        names = set(cases[EXPECTED_VARIANTS[0]])
        if any(set(cases[name]) != names for name in EXPECTED_VARIANTS[1:]):
            raise RuntimeError(f"{summary_path} 的三种调度方式工作流不一致")
        duplicates = names & seen_workflows
        if duplicates:
            raise RuntimeError(f"工作流在多个组合中重复：{sorted(duplicates)}")
        seen_workflows.update(names)

        suite_row: dict[str, Any] = {
            "suite_id": suite_id,
            "summary": str(summary_path.resolve()),
            "workflows": sorted(names),
            "variants": {},
        }
        for variant_name in EXPECTED_VARIANTS:
            aggregate = trials[variant_name].get("aggregate", {})
            makespan = number(aggregate.get("makespan_sec"))
            mean_workflow = number(aggregate.get("mean_workflow_completion_sec"))
            p95_critical = number(aggregate.get("p95_critical_sec"))
            if makespan is None or mean_workflow is None or p95_critical is None:
                raise RuntimeError(f"{summary_path} 的 {variant_name} 缺少时间指标")
            variant_makespans[variant_name].append(makespan)
            suite_row["variants"][variant_name] = {
                "makespan_sec": makespan,
                "mean_workflow_completion_sec": mean_workflow,
                "p95_critical_sec": p95_critical,
            }
        flow_trial_stats = trials["flowcausal_v2_automatic_weight"].get(
            "flowcausal_scheduler", {}
        )
        suite_row["flowcausal_scheduler"] = flow_trial_stats
        for reference in ("default", "scx_simple"):
            suite_row[f"v2_vs_{reference}"] = {
                metric: improvement(
                    suite_row["variants"][reference][metric],
                    suite_row["variants"]["flowcausal_v2_automatic_weight"][metric],
                )
                for metric in (
                    "makespan_sec",
                    "mean_workflow_completion_sec",
                    "p95_critical_sec",
                )
            }
        suites.append(suite_row)

        for workflow in sorted(names):
            row: dict[str, Any] = {"workflow": workflow, "suite_id": suite_id}
            for variant_name in EXPECTED_VARIANTS:
                case = cases[variant_name][workflow]
                elapsed = number(case.get("elapsed_sec"))
                parsed = case.get("parsed")
                critical = number(parsed.get("critical_sec")) if isinstance(parsed, dict) else None
                if elapsed is None or critical is None:
                    raise RuntimeError(f"{summary_path} 的 {workflow} 缺少时间指标")
                row[variant_name] = {
                    "elapsed_sec": elapsed,
                    "critical_sec": critical,
                }
                variant_elapsed[variant_name].append(elapsed)
                variant_critical[variant_name].append(critical)
                if variant_name == "flowcausal_v2_automatic_weight":
                    stats = last_stats(case)
                    row[variant_name]["progress_group_relays"] = stats.get(
                        "progress_group_relays", 0
                    )
                    row[variant_name]["max_progress_relay_depth"] = stats.get(
                        "max_progress_relay_depth", 0
                    )
                    row[variant_name]["confirmed_blocking_waits"] = stats.get(
                        "confirmed_blocking_waits", 0
                    )
            for reference in ("default", "scx_simple"):
                row[f"v2_vs_{reference}"] = {
                    metric: improvement(
                        row[reference][metric],
                        row["flowcausal_v2_automatic_weight"][metric],
                    )
                    for metric in ("elapsed_sec", "critical_sec")
                }
            process = process_by_workflow.get(workflow, {})
            row["process_structure"] = {
                key: process.get(key)
                for key in (
                    "maximum_depth",
                    "return_related_deeper_runtime_share_percent",
                    "return_related_deeper_runnable_wait_share_percent",
                    "return_related_runtime_sec",
                    "return_related_runnable_wait_sec",
                    "deep_meaningful_commands",
                )
            }
            workflow_rows.append(row)

    if len(seen_suites) != 10 or len(seen_workflows) != 50:
        raise RuntimeError(
            f"覆盖不完整：{len(seen_suites)} 个组合、{len(seen_workflows)} 个工作流"
        )
    missing_analysis = seen_workflows - set(process_by_workflow)
    if missing_analysis:
        raise RuntimeError(f"缺少进程结构分析：{sorted(missing_analysis)}")

    aggregates: dict[str, Any] = {}
    for variant_name in EXPECTED_VARIANTS:
        aggregates[variant_name] = {
            "sum_suite_makespan_sec": sum(variant_makespans[variant_name]),
            "mean_suite_makespan_sec": statistics.fmean(variant_makespans[variant_name]),
            "mean_workflow_completion_sec": statistics.fmean(variant_elapsed[variant_name]),
            "p95_workflow_completion_sec": percentile(variant_elapsed[variant_name], 0.95),
            "mean_critical_sec": statistics.fmean(variant_critical[variant_name]),
            "p95_critical_sec": percentile(variant_critical[variant_name], 0.95),
        }

    comparisons: dict[str, Any] = {}
    for reference in ("default", "scx_simple"):
        per_workflow = [
            row[f"v2_vs_{reference}"]["elapsed_sec"] for row in workflow_rows
        ]
        values = [float(value) for value in per_workflow if value is not None]
        comparisons[f"v2_vs_{reference}"] = {
            metric: improvement(
                aggregates[reference][metric],
                aggregates["flowcausal_v2_automatic_weight"][metric],
            )
            for metric in aggregates[reference]
        } | {
            "workflow_improved_count": sum(value > 0 for value in values),
            "workflow_improved_over_five_percent_count": sum(value > 5 for value in values),
            "workflow_regressed_over_five_percent_count": sum(value < -5 for value in values),
            "workflow_elapsed_median_improvement_percent": statistics.median(values),
        }

    result = {
        "schema": "flowcausal.v2.all-workflow-competition-initial.v1",
        "scope": {
            "suite_count": len(suites),
            "workflow_count": len(workflow_rows),
            "trials_per_variant_per_suite": 1,
            "cpus": "0-7",
            "start_method": "concurrent",
            "interpretation": "用于发现规律的单轮初测，不用于显著性结论",
        },
        "process_analysis": str(args.process_analysis.resolve()),
        "aggregates": aggregates,
        "comparisons": comparisons,
        "suites": sorted(suites, key=lambda row: row["suite_id"]),
        "workflows": sorted(workflow_rows, key=lambda row: row["workflow"]),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    json_path = args.output / "全工作流初步比较.json"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal V2 全工作流初步比较\n",
        "本轮把 50 个工作流分为 10 组，每组 5 个并发启动；每个工作流恰好出现一次。每种调度方式只运行一轮，因此本结果用于发现规律和选择重点复测对象，不作为统计显著性结论。\n",
        "## 总体结果\n",
        "| 调度方式 | 10 组总用时（秒） | 平均工作流完成时间（秒） | 工作流完成时间 P95（秒） | 平均关键命令时间（秒） | 关键命令时间 P95（秒） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    labels = {
        "default": "Linux 原生调度器",
        "scx_simple": "scx_simple",
        "flowcausal_v2_automatic_weight": "FlowCausal V2",
    }
    for variant_name in EXPECTED_VARIANTS:
        row = aggregates[variant_name]
        lines.append(
            f"| {labels[variant_name]} | {fmt(row['sum_suite_makespan_sec'], 3)} | "
            f"{fmt(row['mean_workflow_completion_sec'], 3)} | "
            f"{fmt(row['p95_workflow_completion_sec'], 3)} | "
            f"{fmt(row['mean_critical_sec'], 3)} | {fmt(row['p95_critical_sec'], 3)} |"
        )
    lines.extend(
        [
            "\n## 与基线相比\n",
            "正数表示 FlowCausal V2 缩短了时间，负数表示时间增加。\n",
            "| 基线 | 10 组总用时变化 | 平均工作流完成时间变化 | 工作流完成时间 P95 变化 | 平均关键命令时间变化 | 关键命令时间 P95 变化 | 改善工作流数 | 改善超过 5% | 增加超过 5% |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for reference, label in (("default", "Linux 原生调度器"), ("scx_simple", "scx_simple")):
        row = comparisons[f"v2_vs_{reference}"]
        lines.append(
            f"| {label} | {fmt(row['sum_suite_makespan_sec'])}% | "
            f"{fmt(row['mean_workflow_completion_sec'])}% | "
            f"{fmt(row['p95_workflow_completion_sec'])}% | "
            f"{fmt(row['mean_critical_sec'])}% | {fmt(row['p95_critical_sec'])}% | "
            f"{row['workflow_improved_count']} | "
            f"{row['workflow_improved_over_five_percent_count']} | "
            f"{row['workflow_regressed_over_five_percent_count']} |"
        )

    lines.extend(
        [
            "\n## 各并发组合\n",
            "| 组合 | Linux 总用时 | scx_simple 总用时 | V2 总用时 | V2 相对 Linux | V2 相对 scx_simple |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for suite in sorted(suites, key=lambda row: row["suite_id"]):
        variants = suite["variants"]
        lines.append(
            f"| {suite['suite_id']} | {fmt(variants['default']['makespan_sec'], 3)} | "
            f"{fmt(variants['scx_simple']['makespan_sec'], 3)} | "
            f"{fmt(variants['flowcausal_v2_automatic_weight']['makespan_sec'], 3)} | "
            f"{fmt(suite['v2_vs_default']['makespan_sec'])}% | "
            f"{fmt(suite['v2_vs_scx_simple']['makespan_sec'])}% |"
        )

    lines.extend(
        [
            "\n## 逐工作流结果\n",
            "| 工作流 | 所在组合 | Linux 完成时间 | scx_simple 完成时间 | V2 完成时间 | V2 相对 Linux | V2 相对 scx_simple | 等待关系向下传递次数 | 离线观测最大进程层数 |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    ranked = sorted(
        workflow_rows,
        key=lambda row: float(row["v2_vs_default"]["elapsed_sec"] or 0),
        reverse=True,
    )
    for row in ranked:
        lines.append(
            f"| {row['workflow']} | {row['suite_id']} | "
            f"{fmt(row['default']['elapsed_sec'], 3)} | "
            f"{fmt(row['scx_simple']['elapsed_sec'], 3)} | "
            f"{fmt(row['flowcausal_v2_automatic_weight']['elapsed_sec'], 3)} | "
            f"{fmt(row['v2_vs_default']['elapsed_sec'])}% | "
            f"{fmt(row['v2_vs_scx_simple']['elapsed_sec'])}% | "
            f"{row['flowcausal_v2_automatic_weight']['progress_group_relays']} | "
            f"{row['process_structure']['maximum_depth']} |"
        )

    markdown_path = args.output / "全工作流初步比较.md"
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(markdown_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

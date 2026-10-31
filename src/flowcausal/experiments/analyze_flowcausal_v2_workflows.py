#!/usr/bin/env python3
"""逐工作流汇总 V2 的正确性、额外开销、识别结果和处理器竞争。"""

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


def finite(value: object) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return float(value)
    return None


def improvement(reference: float | None, current: float | None) -> float | None:
    if reference is None or current is None or reference <= 0:
        return None
    return (reference - current) / reference * 100.0


def single_case(result: dict[str, Any]) -> dict[str, Any]:
    cases = result.get("cases")
    return cases[0] if isinstance(cases, list) and cases and isinstance(cases[0], dict) else {}


def first_session_stats(result: dict[str, Any]) -> dict[str, Any]:
    control = result.get("flowcausal_control")
    sessions = control.get("sessions") if isinstance(control, dict) else None
    if not isinstance(sessions, list) or not sessions or not isinstance(sessions[0], dict):
        return {}
    stats = sessions[0].get("stats")
    return stats if isinstance(stats, dict) else {}


def comparison_conditions(result: dict[str, Any]) -> dict[str, Any]:
    """收集会影响单工作流时间可比性的条件。

    调度器名称和启动命令本来就应不同，因此不列入。结果缺少其中
    任一条件时，也不得自动生成性能改善数字。
    """
    experiment = result.get("experiment")
    environment = result.get("environment")
    experiment = experiment if isinstance(experiment, dict) else {}
    environment = environment if isinstance(environment, dict) else {}
    return {
        "benchmark_layer": result.get("benchmark_layer"),
        "replay_mode": result.get("replay_mode"),
        "manifest": result.get("manifest"),
        "page_cache_cleared": experiment.get("page_cache_cleared"),
        "release_pattern": experiment.get("release_pattern"),
        "container_pids_limit": experiment.get("container_pids_limit"),
        "cpu_set": environment.get("cpu_set"),
        "kernel": environment.get("kernel"),
        "cpu_model": environment.get("cpu_model"),
        "smt_active": environment.get("smt_active"),
        "scaling_governors": environment.get("scaling_governors"),
    }


def command_outcomes(result_path: pathlib.Path | None) -> list[tuple[int, int]] | None:
    """读取每条原始命令的实际退出状态。

    基准允许某些命令失败，两轮因此可能都显示成功，却在不同位置
    提前退出。性能比较必须排除这种实际工作量不同的情况。
    """
    if result_path is None or not result_path.is_file():
        return None
    event_files = sorted((result_path.parent / "cases").glob("*/events.jsonl"))
    if not event_files:
        return None
    outcomes: list[tuple[int, int]] = []
    try:
        for event_file in event_files:
            for line in event_file.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if event.get("record") != "command_end":
                    continue
                index = event.get("command_index")
                # returncode 是本次回放的实际退出状态；
                # observed_returncode 是来源轨迹中记录的期望值。
                returncode = event.get("returncode")
                if not isinstance(index, int) or not isinstance(returncode, int):
                    return None
                outcomes.append((index, returncode))
    except (OSError, json.JSONDecodeError):
        return None
    return outcomes


def condition_differences(
    reference: dict[str, Any],
    current: dict[str, Any],
    reference_path: pathlib.Path | None,
    current_path: pathlib.Path | None,
) -> list[dict[str, Any]]:
    base = comparison_conditions(reference)
    flow = comparison_conditions(current)
    differences: list[dict[str, Any]] = []
    for name in base:
        if base[name] is None or flow[name] is None or base[name] != flow[name]:
            differences.append(
                {"condition": name, "reference": base[name], "flowcausal": flow[name]}
            )
    base_outcomes = command_outcomes(reference_path)
    flow_outcomes = command_outcomes(current_path)
    if base_outcomes is None or flow_outcomes is None or base_outcomes != flow_outcomes:
        mismatch_indices: list[int] = []
        if base_outcomes is not None and flow_outcomes is not None:
            base_by_index = dict(base_outcomes)
            flow_by_index = dict(flow_outcomes)
            mismatch_indices = sorted(
                index for index in set(base_by_index) | set(flow_by_index)
                if base_by_index.get(index) != flow_by_index.get(index)
            )
        differences.append(
            {
                "condition": "command_outcomes",
                "reference_command_count": len(base_outcomes) if base_outcomes is not None else None,
                "flowcausal_command_count": len(flow_outcomes) if flow_outcomes is not None else None,
                "mismatch_indices": mismatch_indices,
            }
        )
    return differences


def result_files(root: pathlib.Path) -> dict[str, pathlib.Path]:
    summary_path = root / "结果汇总.json"
    if summary_path.is_file():
        summary = read_json(summary_path)
        rows = summary.get("workflows")
        if isinstance(rows, list):
            selected: dict[str, pathlib.Path] = {}
            for row in rows:
                if not isinstance(row, dict) or not row.get("workflow") or not row.get("result"):
                    continue
                path = root / str(row["result"])
                if path.is_file():
                    selected[str(row["workflow"])] = path
            if selected:
                return selected
    found: dict[str, pathlib.Path] = {}
    for path in sorted(root.glob("*/result.json")):
        result = read_json(path)
        case = single_case(result)
        name = str(case.get("instance_id") or "")
        if name:
            found[name] = path
    return found


def integer(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def fmt(value: object, digits: int = 2) -> str:
    return "—" if not isinstance(value, (int, float)) else f"{float(value):.{digits}f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="分析 FlowCausal v2 的全部单工作流")
    parser.add_argument("--reference", type=pathlib.Path, required=True)
    parser.add_argument("--flowcausal", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()

    reference = result_files(args.reference)
    flowcausal = result_files(args.flowcausal)
    names = sorted(set(reference) | set(flowcausal))
    rows: list[dict[str, Any]] = []
    for name in names:
        base_result = read_json(reference[name]) if name in reference else {}
        flow_result = read_json(flowcausal[name]) if name in flowcausal else {}
        base_case = single_case(base_result)
        flow_case = single_case(flow_result)
        base_capture = base_result.get("kernel_capture")
        flow_capture = flow_result.get("kernel_capture")
        base_capture = base_capture if isinstance(base_capture, dict) else {}
        flow_capture = flow_capture if isinstance(flow_capture, dict) else {}
        stats = first_session_stats(flow_result)
        base_elapsed = finite(base_case.get("elapsed_sec"))
        flow_elapsed = finite(flow_case.get("elapsed_sec"))
        differences = condition_differences(
            base_result,
            flow_result,
            reference.get(name),
            flowcausal.get(name),
        )
        performance_comparable = (
            base_result.get("status") == "pass"
            and flow_result.get("status") == "pass"
            and base_elapsed is not None
            and flow_elapsed is not None
            and not differences
        )
        base_runtime = integer(base_capture.get("runtime_ns")) / 1_000_000_000
        flow_runtime = integer(flow_capture.get("runtime_ns")) / 1_000_000_000
        base_wait = integer(base_capture.get("runnable_wait_ns")) / 1_000_000_000
        flow_wait = integer(flow_capture.get("runnable_wait_ns")) / 1_000_000_000
        average_cores = base_runtime / base_elapsed if base_elapsed and base_elapsed > 0 else None
        wait_per_runtime = base_wait / base_runtime if base_runtime > 0 else None
        rows.append(
            {
                "workflow": name,
                "reference_status": base_result.get("status", "missing"),
                "flowcausal_status": flow_result.get("status", "missing"),
                "reference_elapsed_sec": base_elapsed,
                "flowcausal_elapsed_sec": flow_elapsed,
                "performance_comparable": performance_comparable,
                "comparison_condition_differences": differences,
                "elapsed_improvement_percent": (
                    improvement(base_elapsed, flow_elapsed) if performance_comparable else None
                ),
                "reference_cpu_runtime_sec": base_runtime,
                "flowcausal_cpu_runtime_sec": flow_runtime,
                "reference_average_busy_cores": average_cores,
                "reference_runnable_wait_sec": base_wait,
                "flowcausal_runnable_wait_sec": flow_wait,
                "reference_wait_per_runtime": wait_per_runtime,
                "reference_task_count": integer(base_capture.get("task_count")),
                "reference_max_runnable_wait_ms": integer(base_capture.get("max_runnable_wait_ns")) / 1_000_000,
                "confirmed_blocking_waits": integer(stats.get("confirmed_blocking_waits")),
                "progress_group_links": integer(stats.get("progress_group_links")),
                "return_promotions": integer(stats.get("return_promotions")),
                "interactive_runs": integer(stats.get("interactive_runs")),
                "background_delays": integer(stats.get("stale_delays")),
                "locality_hits": integer(stats.get("locality_hits")),
                "parallel_width_limits": integer(stats.get("parallel_width_limits")),
                "dropped_events": integer(flow_capture.get("dropped_events")),
                "reference_result": str(reference.get(name, "")),
                "flowcausal_result": str(flowcausal.get(name, "")),
            }
        )

    comparable = [
        row for row in rows
        if row["performance_comparable"]
    ]
    changes = [float(row["elapsed_improvement_percent"]) for row in comparable]
    result = {
        "schema": "flowcausal.v2.workflow-analysis.v1",
        "reference": str(args.reference.resolve()),
        "flowcausal": str(args.flowcausal.resolve()),
        "workflow_count": len(names),
        "comparable_workflows": len(comparable),
        "performance_comparison_valid": len(comparable) == len(names) and len(names) > 0,
        "condition_mismatch_workflows": [
            row["workflow"] for row in rows
            if row["comparison_condition_differences"]
        ],
        "all_commands_passed": all(
            row["reference_status"] == "pass" and row["flowcausal_status"] == "pass"
            for row in rows
        ),
        "zero_event_loss": all(row["dropped_events"] == 0 for row in rows),
        "elapsed_median_improvement_percent": statistics.median(changes) if changes else None,
        "elapsed_improved_count": sum(value > 0 for value in changes),
        "elapsed_within_two_percent_count": sum(abs(value) <= 2 for value in changes),
        "relation_observed_count": sum(row["confirmed_blocking_waits"] > 0 for row in rows),
        "promotion_observed_count": sum(row["return_promotions"] > 0 for row in rows),
        "interactive_observed_count": sum(row["interactive_runs"] > 0 for row in rows),
        "background_observed_count": sum(row["background_delays"] > 0 for row in rows),
        "locality_observed_count": sum(row["locality_hits"] > 0 for row in rows),
        "parallel_width_observed_count": sum(row["parallel_width_limits"] > 0 for row in rows),
        "workflows": rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "逐工作流分析.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal v2 逐工作流分析\n",
        "单工作流结果用于检查命令正确性、识别覆盖和额外开销。只有命令、缓存、处理器范围、内核和运行方式一致时才计算时间变化。由于没有工作流之间的处理器竞争，不把单次完成时间变化解释为主要调度收益。",
        "",
        f"- 可比较工作流：{len(comparable)}/{len(names)}；",
        f"- 性能比较是否有效：{'\u662f' if result['performance_comparison_valid'] else '\u5426'}；",
        f"- 完成时间变化中位数：{fmt(result['elapsed_median_improvement_percent'])}%；",
        f"- 变化在正负 2% 内：{result['elapsed_within_two_percent_count']} 个；",
        f"- 观察到阻塞等待关系：{result['relation_observed_count']} 个；",
        f"- 实际执行过关键推进：{result['promotion_observed_count']} 个；",
        f"- 实际执行过短交互：{result['interactive_observed_count']} 个；",
        f"- 实际执行过旧任务延后：{result['background_observed_count']} 个；",
        f"- 实际复用近期处理器：{result['locality_observed_count']} 个；",
        f"- 实际限制过并行度：{result['parallel_width_observed_count']} 个。\n",
        "| 工作流 | 参考时间（秒） | V2 时间（秒） | 变化 | 参考平均使用处理器数 | 参考可运行等待/运行时间 | 阻塞等待 | 关键推进 | 短交互 | 旧任务延后 | 近期处理器复用 | 并行度限制 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {workflow} | {base} | {flow} | {change}% | {cores} | {wait} | {blocked} | {promoted} | {interactive} | {background} | {locality} | {width} |".format(
                workflow=row["workflow"],
                base=fmt(row["reference_elapsed_sec"], 3),
                flow=fmt(row["flowcausal_elapsed_sec"], 3),
                change=(
                    fmt(row["elapsed_improvement_percent"])
                    if row["performance_comparable"] else "不可比较"
                ),
                cores=fmt(row["reference_average_busy_cores"]),
                wait=fmt(row["reference_wait_per_runtime"], 3),
                blocked=row["confirmed_blocking_waits"],
                promoted=row["return_promotions"],
                interactive=row["interactive_runs"],
                background=row["background_delays"],
                locality=row["locality_hits"],
                width=row["parallel_width_limits"],
            )
        )
    (args.output / "逐工作流分析.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output)
    return 0 if len(comparable) == len(names) == 50 else 1


if __name__ == "__main__":
    raise SystemExit(main())

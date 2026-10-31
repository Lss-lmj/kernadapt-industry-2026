#!/usr/bin/env python3
"""对 FlowCausal v2 同批交错实验进行正确性核对和配对统计。

这个程序只读取 matrix runner 已生成的 JSON。它不删除样本，
不根据结果挑选轮次；任一轮执行失败、结果检查失败或页缓存清理
失败都会使整个分析失败。
"""

from __future__ import annotations

import argparse
import json
import math
import pathlib
import statistics
from typing import Any

from scipy.stats import t


METRICS = (
    ("makespan_sec", "最后一个工作流的完成时间"),
    ("mean_workflow_completion_sec", "平均工作流完成时间"),
    ("p95_workflow_completion_sec", "工作流完成时间 95 分位"),
    ("p95_synchronous_tool_service_sec", "同步工具耗时 95 分位"),
    ("mean_critical_sec", "平均主要工具序列完成时间"),
    ("p95_critical_sec", "主要工具序列完成时间 95 分位"),
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"结果不是 JSON 对象：{path}")
    return value


def summary(values: list[float]) -> dict[str, float | int]:
    count = len(values)
    if not count:
        raise SystemExit("没有可统计样本")
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


def trial_number(trial: dict[str, Any]) -> int:
    value = trial.get("trial")
    if not isinstance(value, int):
        raise SystemExit("轮次编号缺失")
    return value


def validate_external_scheduler_lifecycle(name: str, trial: dict[str, Any]) -> None:
    """Reject timings collected after an external sched_ext scheduler detached.

    Some upstream scheduler loaders return status 0 even when the kernel
    watchdog detaches their BPF scheduler.  Workload correctness alone cannot
    detect that the remaining commands silently ran under Linux.
    """

    if trial.get("kind") != "scx":
        return
    external = trial.get("external_scx")
    manifest = external.get("manifest") if isinstance(external, dict) else None
    if not isinstance(manifest, dict):
        raise SystemExit(f"{name} 缺少外部调度器运行记录")
    if manifest.get("error"):
        raise SystemExit(f"{name} 的外部调度器运行失败：{manifest['error']}")
    if (
        "scheduler_returncode_after_workload" in manifest
        and manifest.get("scheduler_returncode_after_workload") is not None
    ):
        raise SystemExit(f"{name} 的调度器在工作流结束前退出")
    state = manifest.get("sched_ext_state_after_workload")
    if state is not None and state != "enabled":
        raise SystemExit(f"{name} 的调度器未保持到工作流结束：{state!r}")

    out_dir = pathlib.Path(str(external.get("out_dir") or ""))
    log_path = out_dir / "scheduler.log"
    if not log_path.is_file():
        raise SystemExit(f"{name} 缺少调度器日志：{log_path}")
    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    if "triggered exit kind" in log_text:
        raise SystemExit(f"{name} 的调度器被内核提前退出：{log_path}")
    exit_lines = [line.strip() for line in log_text.splitlines() if line.startswith("EXIT:")]
    if exit_lines and exit_lines[-1] != "EXIT: unregistered from user space":
        raise SystemExit(f"{name} 的调度器异常退出：{exit_lines[-1]}")


def validate(
    payload: dict[str, Any], excluded_variants: set[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    excluded = excluded_variants or set()
    if payload.get("status") != "pass" and not excluded:
        raise SystemExit(f"整体实验未通过：{payload.get('status')}")
    expected = int(payload.get("trials_per_variant") or 0)
    if expected < 1:
        raise SystemExit("无效的每种方法运行次数")
    result: dict[str, list[dict[str, Any]]] = {}
    seen_names: set[str] = set()
    for variant in payload.get("variants", []):
        if not isinstance(variant, dict):
            raise SystemExit("方法记录格式错误")
        name = str(variant.get("name") or "")
        if name:
            seen_names.add(name)
        if name in excluded:
            continue
        trials = variant.get("trials")
        if not name or not isinstance(trials, list) or len(trials) != expected:
            raise SystemExit(f"{name or '<unnamed>'} 轮次不完整")
        selected: list[dict[str, Any]] = []
        for trial in trials:
            if not isinstance(trial, dict) or trial.get("status") != "pass":
                raise SystemExit(f"{name} 有未通过轮次")
            fidelity = trial.get("fidelity")
            if not isinstance(fidelity, dict) or fidelity.get("status") != "pass":
                raise SystemExit(f"{name} 有结果一致性检查未通过的轮次")
            cache = trial.get("cache_control")
            if (
                not isinstance(cache, dict)
                or not cache.get("enabled")
                or cache.get("sync_returncode") != 0
                or cache.get("drop_caches_returncode") != 0
            ):
                raise SystemExit(f"{name} 有页缓存清理未成功的轮次")
            cases = trial.get("cases")
            if not isinstance(cases, list) or not cases:
                raise SystemExit(f"{name} 缺少逐工作流结果")
            if any(not isinstance(case, dict) or case.get("returncode") != 0 for case in cases):
                raise SystemExit(f"{name} 有工作流异常退出")
            validate_external_scheduler_lifecycle(name, trial)
            selected.append(trial)
        result[name] = sorted(selected, key=trial_number)
    unknown = excluded - seen_names
    if unknown:
        raise SystemExit(f"要求排除的方法不存在：{', '.join(sorted(unknown))}")
    if not result:
        raise SystemExit("排除后没有可分析的方法")
    return result


def iter_log_records(path: pathlib.Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise SystemExit(f"工作流日志不存在：{path}")
    decoder = json.JSONDecoder()
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        for offset in reversed(
            [index for index, character in enumerate(line) if character == "{"]
        ):
            try:
                item, end = decoder.raw_decode(line, offset)
            except json.JSONDecodeError:
                continue
            if line[end:].strip() or not isinstance(item, dict):
                continue
            records.append(item)
            break
    return records


def percentile_nearest_rank(values: list[float], percent: float) -> float:
    if not values:
        raise SystemExit("没有可统计的同步工具记录")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * percent / 100.0) - 1)]


def synchronous_tool_p95(trial: dict[str, Any]) -> float:
    values: list[float] = []
    for case in trial["cases"]:
        run_dir = pathlib.Path(str(case.get("run_dir") or ""))
        records = iter_log_records(run_dir / "workload.log")
        endings = [
            record for record in records
            if record.get("type") == "agent_stage_end"
        ]
        parsed = case.get("parsed")
        expected = int(parsed.get("command_count") or 0) if isinstance(parsed, dict) else 0
        if len(endings) != expected:
            raise SystemExit(
                f"{case.get('instance_id')} 的工具结束记录不完整："
                f"预期 {expected}，实际 {len(endings)}"
            )
        for record in endings:
            if record.get("asynchronous"):
                continue
            value = record.get("elapsed_sec")
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise SystemExit(f"同步工具记录缺少有效耗时：{case.get('instance_id')}")
            values.append(float(value))
    return percentile_nearest_rank(values, 95.0)


def aggregate_value(trial: dict[str, Any], field: str) -> float:
    if field == "p95_synchronous_tool_service_sec":
        return synchronous_tool_p95(trial)
    aggregate = trial.get("aggregate")
    value = aggregate.get(field) if isinstance(aggregate, dict) else None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SystemExit(f"缺少指标 {field}")
    return float(value)


def case_value(case: dict[str, Any], field: str) -> float:
    if field == "workflow_completion_sec":
        value = case.get("elapsed_sec")
    else:
        parsed = case.get("parsed")
        value = parsed.get("critical_sec") if isinstance(parsed, dict) else None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise SystemExit(f"逐工作流结果缺少 {field}")
    return float(value)


def reduction(reference: float, candidate: float) -> float:
    return (reference - candidate) / reference * 100.0


def main() -> int:
    parser = argparse.ArgumentParser(description="分析 FlowCausal v2 最终交错实验")
    parser.add_argument("--input", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    parser.add_argument("--candidate", default="flowcausal_v2_automatic_weight")
    parser.add_argument(
        "--exclude-variant",
        action="append",
        default=[],
        help="exclude a complete method after recording the reason in the experiment report",
    )
    args = parser.parse_args()

    payload = load(args.input)
    excluded_variants = set(args.exclude_variant)
    variants = validate(payload, excluded_variants)
    if args.candidate not in variants:
        raise SystemExit(f"没有候选方法：{args.candidate}")
    candidate_trials = variants[args.candidate]

    method_stats: dict[str, Any] = {}
    for name, trials in variants.items():
        method_stats[name] = {
            field: summary([aggregate_value(trial, field) for trial in trials])
            for field, _ in METRICS
        }

    comparisons: dict[str, Any] = {}
    for name, trials in variants.items():
        if name == args.candidate:
            continue
        if [trial_number(item) for item in trials] != [
            trial_number(item) for item in candidate_trials
        ]:
            raise SystemExit(f"{name} 与 {args.candidate} 不能按轮次配对")
        comparisons[name] = {}
        for field, _ in METRICS:
            paired = [
                reduction(aggregate_value(base, field), aggregate_value(value, field))
                for base, value in zip(trials, candidate_trials, strict=True)
            ]
            comparisons[name][field] = summary(paired)

    workflows: dict[str, Any] = {}
    for name, trials in variants.items():
        by_workflow: dict[str, dict[str, list[float]]] = {}
        for trial in trials:
            for case in trial["cases"]:
                instance = str(case.get("instance_id") or "")
                if not instance:
                    raise SystemExit(f"{name} 存在没有名称的工作流")
                row = by_workflow.setdefault(
                    instance,
                    {"workflow_completion_sec": [], "critical_sec": []},
                )
                for field in row:
                    row[field].append(case_value(case, field))
        workflows[name] = {
            instance: {field: summary(values) for field, values in fields.items()}
            for instance, fields in sorted(by_workflow.items())
        }

    result = {
        "schema": "flowcausal.v2.final-interleaved-analysis.v1",
        "source": str(args.input.resolve()),
        "candidate": args.candidate,
        "excluded_variants": sorted(excluded_variants),
        "trials_per_method": int(payload["trials_per_variant"]),
        "validation": {
            "all_trials_pass": True,
            "all_fidelity_checks_pass": True,
            "all_page_cache_clears_pass": True,
            "all_workflows_exit_zero": True,
        },
        "methods": method_stats,
        "candidate_paired_reduction_percent": comparisons,
        "workflows": workflows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "统计结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal v2 同批交错实验统计",
        "",
        f"每种方法 {result['trials_per_method']} 轮；全部运行、结果一致性和页缓存清理检查通过。",
        "表中“±”后为样本标准差。",
        "",
        "| 方法 | 最后完成（秒） | 平均工作流（秒） | 工作流 95 分位（秒） | 同步工具 95 分位（秒） | 平均主要工具序列（秒） | 主要工具序列 95 分位（秒） |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name in variants:
        fields = method_stats[name]
        values = []
        for field, _ in METRICS:
            item = fields[field]
            values.append(f"{item['mean']:.3f}±{item['sample_stdev']:.3f}")
        lines.append(f"| `{name}` | " + " | ".join(values) + " |")

    lines.extend([
        "",
        f"## `{args.candidate}` 的配对变化",
        "",
        f"正数表示 `{args.candidate}` 缩短时间；区间为各轮相对变化的双侧 95% Student t 置信区间。",
        "",
        "| 参考方法 | 最后完成 | 平均工作流 | 工作流 95 分位 | 同步工具 95 分位 | 平均主要工具序列 | 主要工具序列 95 分位 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ])
    for name in variants:
        if name == args.candidate:
            continue
        values = []
        for field, _ in METRICS:
            item = comparisons[name][field]
            values.append(
                f"{item['mean']:.2f}% [{item['confidence_95_low']:.2f}%, {item['confidence_95_high']:.2f}%]"
            )
        lines.append(f"| `{name}` | " + " | ".join(values) + " |")
    (args.output / "统计结果.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(args.output / "统计结果.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

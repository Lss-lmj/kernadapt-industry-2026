#!/usr/bin/env python3
"""把 FlowCausal v2 的一次正式对比固化为唯一结果表。"""
from __future__ import annotations

import csv
import json
import pathlib
import statistics
from collections import defaultdict
from typing import Any


REPO = pathlib.Path(__file__).resolve().parents[1]
RESULTS = REPO / "experiments" / "results" / "harness-replaybench-v1"
REFERENCE = RESULTS / "scheduling-opportunity-v2-reference-3trial-20260910"
OUTPUT = RESULTS / "flowcausal-v2-final-comparison-20260910"

METHODS = [
    {
        "id": "scx_simple",
        "label": "scx_simple",
        "root": REFERENCE / "scx_simple",
        "trials": (1, 2, 3),
    },
    {
        "id": "scx_flatcg",
        "label": "scx_flatcg",
        "root": REFERENCE / "scx_flatcg",
        "trials": (1, 2, 3),
    },
    {
        "id": "scx_flowcausal_v2_grouping_only",
        "label": "scx_flowcausal_v2 --grouping-only",
        "root": REFERENCE / "flowcausal_v2_grouping_only",
        "trials": (2, 3, 4),
    },
    {
        "id": "scx_flowcausal_v2",
        "label": "scx_flowcausal_v2",
		"root": REFERENCE / "flowcausal_v2",
        "trials": (1, 2, 3),
    },
]

DVC_WINDOWS = (
	"trajectory[172].tool_calls[0]",
	"trajectory[176].tool_calls[0]",
)

MECHANISM_COUNTERS = (
    "provenance_tasks",
    "wait_events",
    "confirmed_blocking_waits",
    "ignored_immediate_waits",
    "wait_links",
    "progress_group_links",
    "progress_group_promotions",
    "stale_delays",
    "background_rescues",
    "locality_hits",
    "locality_fallbacks",
    "bias_ns",
)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"结果不是 JSON 对象：{path}")
    return payload


def mean(values: list[float]) -> float:
    return statistics.fmean(values)


def sample_std(values: list[float]) -> float:
    return statistics.stdev(values) if len(values) > 1 else 0.0


def summarize(values: list[float]) -> dict[str, float]:
    return {"mean": mean(values), "sample_std": sample_std(values)}


def reduction(reference: float, value: float) -> float:
    return (reference - value) / reference * 100.0 if reference else 0.0


def result_path(method: dict[str, Any], trial: int) -> pathlib.Path:
    return pathlib.Path(method["root"]) / f"trial-{trial:02d}" / "result.json"


def dvc_window_values(result: dict[str, Any]) -> dict[str, float]:
    for case in result.get("cases", []):
        if case.get("instance_id") != "iterative__dvc-1494":
            continue
        return {
            str(window["command_id"]): float(window["elapsed_sec"])
            for window in case.get("causal_windows", [])
            if window.get("command_id") in DVC_WINDOWS
        }
    return {}


def mechanism_counts(result: dict[str, Any]) -> dict[str, int]:
    total = {name: 0 for name in MECHANISM_COUNTERS}
    for session in result.get("flowcausal_control", {}).get("sessions", []):
        stats = session.get("stats", {}) if isinstance(session, dict) else {}
        for name in MECHANISM_COUNTERS:
            total[name] += int(stats.get(name) or 0)
    return total


def check_run(result: dict[str, Any], path: pathlib.Path) -> None:
    if result.get("status") != "pass":
        raise RuntimeError(f"正式轮次未通过：{path}")
    if result.get("suite_id") != "hrb1-scheduling-opportunity-concurrent-tool":
        raise RuntimeError(f"正式轮次套件不一致：{path}")
    experiment = result.get("experiment", {})
    if experiment.get("release_pattern") != "concurrent" or not experiment.get(
        "page_cache_cleared"
    ):
        raise RuntimeError(f"正式轮次启动或缓存条件不一致：{path}")
    if result.get("environment", {}).get("cpu_set") != "0-7":
        raise RuntimeError(f"正式轮次处理器范围不一致：{path}")
    if int(result.get("aggregate", {}).get("unexpected_failure_commands") or 0):
        raise RuntimeError(f"正式轮次存在非预期命令失败：{path}")
    if len(result.get("cases", [])) != 4 or any(
        case.get("status") != "pass" for case in result.get("cases", [])
    ):
        raise RuntimeError(f"正式轮次任务状态不完整：{path}")
    sched_ext = result.get("tested_system", {})
    if (
        sched_ext.get("sched_ext_before", {}).get("state") != "disabled"
        or sched_ext.get("sched_ext_during", {}).get("state") != "enabled"
        or sched_ext.get("sched_ext_after", {}).get("state") != "disabled"
    ):
        raise RuntimeError(f"正式轮次调度器状态不一致：{path}")
    if int(result.get("kernel_capture", {}).get("dropped_events") or 0):
        raise RuntimeError(f"正式轮次存在丢失事件：{path}")


def collect() -> list[dict[str, Any]]:
    raw_rows: list[dict[str, Any]] = []
    for method in METHODS:
        for trial in method["trials"]:
            path = result_path(method, trial)
            result = read_json(path)
            check_run(result, path)
            aggregate = result["aggregate"]
            capture = result.get("kernel_capture", {})
            windows = dvc_window_values(result)
            control = mechanism_counts(result)
            raw_rows.append(
                {
                    "method": method["label"],
                    "trial": trial,
                    "makespan_sec": float(aggregate["makespan_sec"]),
                    "mean_workflow_completion_sec": float(
                        aggregate["mean_workflow_completion_sec"]
                    ),
                    "p95_workflow_completion_sec": float(
                        aggregate["p95_workflow_completion_sec"]
                    ),
                    "mean_synchronous_tool_service_sec": float(
                        aggregate["mean_synchronous_tool_service_sec"]
                    ),
                    "p95_synchronous_tool_service_sec": float(
                        aggregate["p95_synchronous_tool_service_sec"]
                    ),
                    "runtime_sec": float(capture.get("runtime_ns") or 0) / 1e9,
                    "runnable_wait_sec": float(capture.get("runnable_wait_ns") or 0) / 1e9,
                    "migrations": int(capture.get("migrations") or 0),
                    "switches": int(capture.get("switches") or 0),
                    "dvc_window_172_sec": windows[DVC_WINDOWS[0]],
                    "dvc_window_176_sec": windows[DVC_WINDOWS[1]],
                    **{f"mechanism_{name}": value for name, value in control.items()},
                    "result": path.relative_to(REPO).as_posix(),
                }
            )
    return raw_rows


def aggregate(raw_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_method: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in raw_rows:
        by_method[str(row["method"])].append(row)

    metrics = (
        "makespan_sec",
        "mean_workflow_completion_sec",
        "p95_workflow_completion_sec",
        "mean_synchronous_tool_service_sec",
        "p95_synchronous_tool_service_sec",
        "runtime_sec",
        "runnable_wait_sec",
        "migrations",
        "switches",
    )
    rows: list[dict[str, Any]] = []
    for method in METHODS:
        label = str(method["label"])
        values = by_method[label]
        row: dict[str, Any] = {
            "method": label,
            "trials": len(values),
            "metrics": {
                metric: summarize([float(item[metric]) for item in values]) for metric in metrics
            },
            "result_directory": pathlib.Path(method["root"]).relative_to(REPO).as_posix(),
        }
        combined_windows = [
            float(item[key])
            for item in values
            for key in ("dvc_window_172_sec", "dvc_window_176_sec")
        ]
        row["dvc_preselected_windows"] = {
            "combined": summarize(combined_windows),
            DVC_WINDOWS[0]: summarize([float(item["dvc_window_172_sec"]) for item in values]),
            DVC_WINDOWS[1]: summarize([float(item["dvc_window_176_sec"]) for item in values]),
        }
        row["mechanism_counts_per_trial"] = {
            name: summarize([float(item[f"mechanism_{name}"]) for item in values])
            for name in MECHANISM_COUNTERS
        }
        rows.append(row)

    reference = rows[0]
    reference_windows = reference["dvc_preselected_windows"]["combined"]["mean"]
    for row in rows:
        row["relative_to_scx_simple_percent"] = {
            metric.removesuffix("_sec"): reduction(
                reference["metrics"][metric]["mean"], row["metrics"][metric]["mean"]
            )
            for metric in metrics[:5]
        }
        row["relative_to_scx_simple_percent"]["dvc_preselected_windows"] = reduction(
            reference_windows, row["dvc_preselected_windows"]["combined"]["mean"]
        )

    case_values: dict[tuple[str, str], list[float]] = defaultdict(list)
    for method, result in zip(
        [method for method in METHODS for _ in method["trials"]],
        [read_json(result_path(method, trial)) for method in METHODS for trial in method["trials"]],
        strict=True,
    ):
        for case in result["cases"]:
            case_values[(str(method["label"]), str(case["instance_id"]))].append(
                float(case["workflow_completion_sec"])
            )
    cases: list[dict[str, Any]] = []
    instances = sorted({instance for _, instance in case_values})
    for instance in instances:
        reference_mean = mean(case_values[(str(METHODS[0]["label"]), instance)])
        for method in METHODS:
            label = str(method["label"])
            summary = summarize(case_values[(label, instance)])
            cases.append(
                {
                    "instance_id": instance,
                    "method": label,
                    "workflow_completion_sec": summary,
                    "relative_to_scx_simple_percent": reduction(reference_mean, summary["mean"]),
                }
            )
    return rows, cases


def fmt(metric: dict[str, float]) -> str:
    return f"{metric['mean']:.3f} ± {metric['sample_std']:.3f}"


def write_markdown(payload: dict[str, Any]) -> None:
    full = next(row for row in payload["methods"] if row["method"] == "scx_flowcausal_v2")
    relative = full["relative_to_scx_simple_percent"]
    lines = [
        "# FlowCausal v2 正式对比结果",
        "",
        "实验使用同一四任务真实工具组合，固定在 0–7 号处理器上同时启动。每种方法三轮，每轮清理页缓存；命令返回状态全部符合来源轨迹，C/eBPF 采集没有丢失事件。正数表示相对 `scx_simple` 缩短。",
        "",
        "## 整体结果",
        "",
        "| 调度器 | 轮数 | 总完成时间（秒） | 平均工作流完成时间（秒） | 工作流完成时间 P95（秒） | 同步工具服务 P95（秒） | 平均工作流变化 | 结果目录 |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in payload["methods"]:
        metrics = row["metrics"]
        relative = row["relative_to_scx_simple_percent"]
        lines.append(
            f"| `{row['method']}` | {row['trials']} | {fmt(metrics['makespan_sec'])} | "
            f"{fmt(metrics['mean_workflow_completion_sec'])} | "
            f"{fmt(metrics['p95_workflow_completion_sec'])} | "
            f"{fmt(metrics['p95_synchronous_tool_service_sec'])} | "
            f"{relative['mean_workflow_completion']:+.2f}% | `{row['result_directory']}` |"
        )

    lines.extend(
        [
            "",
            f"完整 FlowCausal v2 相对 `scx_simple` 将总完成时间缩短 {relative['makespan']:.2f}%，"
            f"平均工作流完成时间缩短 {relative['mean_workflow_completion']:.2f}%，"
            f"工作流完成时间 P95 缩短 {relative['p95_workflow_completion']:.2f}%，"
            f"同步工具服务 P95 缩短 {relative['p95_synchronous_tool_service']:.2f}%。",
            "",
            "## 分任务工作流完成时间",
            "",
            "| 任务 | 调度器 | 时间（秒） | 相对 scx_simple 变化 |",
            "|---|---|---:|---:|",
        ]
    )
    for row in payload["cases"]:
        lines.append(
            f"| `{row['instance_id']}` | `{row['method']}` | "
            f"{fmt(row['workflow_completion_sec'])} | "
            f"{row['relative_to_scx_simple_percent']:+.2f}% |"
        )

    lines.extend(
        [
            "",
            "## DVC 中两个预先选定的观测窗口",
            "",
            "这两个窗口是在查看 FlowCausal 结果之前，仅依据 `scx_simple` 的 C/eBPF 记录选出的。正确位置是 `trajectory[172].tool_calls[0]` 和 `trajectory[176].tool_calls[0]`。它们用于检查调度动作是否落在前后工具发生竞争的时段，不单独承担性能结论。",
            "",
            "| 调度器 | 两窗口合并均值（毫秒） | 相对 scx_simple 变化 |",
            "|---|---:|---:|",
        ]
    )
    for row in payload["methods"]:
        window = row["dvc_preselected_windows"]["combined"]
        lines.append(
            f"| `{row['method']}` | {window['mean'] * 1000:.3f} ± "
            f"{window['sample_std'] * 1000:.3f} | "
            f"{row['relative_to_scx_simple_percent']['dvc_preselected_windows']:+.2f}% |"
        )

    counters = full["mechanism_counts_per_trial"]
    lines.extend(
        [
            "",
            "## 完整 FlowCausal v2 每轮平均动作",
            "",
            "| 来源任务记录 | 进入等待函数 | 确实睡眠 | 立即返回 | 等待关系 | 纳入推进组 | 实际短时推进 | 旧任务延后 | 最长等待后恢复 | 近期处理器命中 | 处理器选择回退 |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            f"| {counters['provenance_tasks']['mean']:.1f} | "
            f"{counters['wait_events']['mean']:.1f} | "
            f"{counters['confirmed_blocking_waits']['mean']:.1f} | "
            f"{counters['ignored_immediate_waits']['mean']:.1f} | "
            f"{counters['wait_links']['mean']:.1f} | "
            f"{counters['progress_group_links']['mean']:.1f} | "
            f"{counters['progress_group_promotions']['mean']:.1f} | "
            f"{counters['stale_delays']['mean']:.1f} | "
            f"{counters['background_rescues']['mean']:.1f} | "
            f"{counters['locality_hits']['mean']:.1f} | "
            f"{counters['locality_fallbacks']['mean']:.1f} |",
        ]
    )

    lines.extend(
        [
            "",
            "## 机制解释",
            "",
            "`scx_flowcausal_v2 --grouping-only` 将不同工作流分别排队，并把调度范围限定为实验使用的八个处理器。完整 FlowCausal v2 在此基础上使用进程来源、阻塞等待、成组推进、旧任务延后和近期处理器复用；内核统计用于确认这些动作在真实工具执行中实际发生。",
            "",
            "因此，当前证据支持两项结论：工作流分组改善多沙箱并发下的整体完成时间；完整第二版在相同组合上同时改善工作流完成时间和同步工具服务尾部。两个观测窗口只用于机制定位，不再作为单独的加速主张。",
            "",
        ]
    )
    (OUTPUT / "结果摘要.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    raw_rows = collect()
    methods, cases = aggregate(raw_rows)
    payload = {
        "schema": "harness_replaybench.flowcausal_v2_final_comparison.v1",
        "suite_id": "hrb1-scheduling-opportunity-concurrent-tool",
        "manifest": "benchmarks/harness-replaybench/manifests/v1/mixed/hrb1-scheduling-opportunity-concurrent-tool.json",
        "conditions": {
            "cpu_set": "0-7",
            "release_pattern": "concurrent",
            "trials_per_method": 3,
            "page_cache": "每轮清理",
            "online_runtime": "C + eBPF + sched_ext，无 Python",
        },
        "methods": methods,
        "cases": cases,
        "preselected_dvc_windows": list(DVC_WINDOWS),
    }
    (OUTPUT / "结果表.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    with (OUTPUT / "逐轮结果.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(raw_rows[0]))
        writer.writeheader()
        writer.writerows(raw_rows)
    write_markdown(payload)
    print(json.dumps({"output": str(OUTPUT), "methods": len(methods), "runs": len(raw_rows)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

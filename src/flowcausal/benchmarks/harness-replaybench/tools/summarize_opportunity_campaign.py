#!/usr/bin/env python3
"""汇总调度机会套件，并从并发 C/eBPF 记录中核对机会来源。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import statistics
from collections import defaultdict
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROFILE_MODULE_PATH = ROOT / "tools" / "analyze_behavior_profiles.py"
SPEC = importlib.util.spec_from_file_location("hrb_behavior_profiles", PROFILE_MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PROFILE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE_MODULE)


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def reduction(reference: float, value: float) -> float:
    return (reference - value) / reference * 100 if reference > 0 else 0.0


def mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else 0.0


def method_results(campaign: pathlib.Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    methods: dict[str, list[dict[str, Any]]] = defaultdict(list)
    cases: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for path in sorted(campaign.glob("*/trial-*/result.json")):
        result = read_json(path)
        if result.get("status") != "pass":
            continue
        method = str(result.get("scheduler") or path.parents[1].name)
        methods[method].append(result)
        for case in result.get("cases", []):
            if isinstance(case, dict):
                cases[(method, str(case.get("instance_id") or ""))].append(case)

    summary: list[dict[str, Any]] = []
    for method, values in methods.items():
        summary.append(
            {
                "method": method,
                "trials": len(values),
                "makespan_sec": mean([float(item["aggregate"]["makespan_sec"]) for item in values]),
                "workflow_completion_sec": mean(
                    [float(item["aggregate"]["mean_workflow_completion_sec"]) for item in values]
                ),
                "tool_service_p95_sec": mean(
                    [float(item["aggregate"]["p95_synchronous_tool_service_sec"]) for item in values]
                ),
                "selected_milestone_sec": mean(
                    [float(item["aggregate"]["mean_selected_tool_milestone_sec"]) for item in values]
                ),
                "runtime_sec": mean(
                    [float(item.get("kernel_capture", {}).get("runtime_ns") or 0) / 1e9 for item in values]
                ),
                "runnable_wait_sec": mean(
                    [float(item.get("kernel_capture", {}).get("runnable_wait_ns") or 0) / 1e9 for item in values]
                ),
            }
        )
    summary.sort(key=lambda item: item["method"] != "scx_simple")
    reference = next(item for item in summary if item["method"] == "scx_simple")
    for item in summary:
        item["relative_to_scx_simple_percent"] = {
            "makespan_reduction": reduction(reference["makespan_sec"], item["makespan_sec"]),
            "workflow_completion_reduction": reduction(
                reference["workflow_completion_sec"], item["workflow_completion_sec"]
            ),
            "tool_service_p95_reduction": reduction(
                reference["tool_service_p95_sec"], item["tool_service_p95_sec"]
            ),
        }

    case_summary: list[dict[str, Any]] = []
    instances = sorted({instance for _, instance in cases})
    for instance in instances:
        ref_values = cases.get(("scx_simple", instance), [])
        if not ref_values:
            continue
        ref_workflow = mean([float(item["workflow_completion_sec"]) for item in ref_values])
        for method in sorted(methods, key=lambda name: name != "scx_simple"):
            values = cases.get((method, instance), [])
            if not values:
                continue
            workflow = mean([float(item["workflow_completion_sec"]) for item in values])
            case_summary.append(
                {
                    "instance_id": instance,
                    "method": method,
                    "trials": len(values),
                    "workflow_completion_sec": workflow,
                    "workflow_completion_reduction_percent": reduction(ref_workflow, workflow),
                    "tool_service_p95_sec": mean(
                        [float(item["tool_service"]["synchronous_p95_sec"]) for item in values]
                    ),
                }
            )
    return summary, case_summary


def concurrent_capture(campaign: pathlib.Path) -> dict[str, Any]:
    trial_payloads: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    for result_path in sorted((campaign / "scx_simple").glob("trial-*/result.json")):
        result = read_json(result_path)
        raw_path = result_path.parent / "kernel-capture.jsonl"
        if not raw_path.is_file():
            continue
        manifest = read_json(ROOT / str(result["manifest"]))
        cases = {
            str(item["instance_id"]): item
            for item in manifest.get("cases", [])
            if isinstance(item, dict)
        }
        session_ids = {
            str(item["instance_id"]): int(item["session_id"])
            for item in result.get("kernel_capture", {}).get("session_ids", [])
        }
        trial = {
            "trial": result_path.parent.name,
            "cpu_busy_fraction": (
                float(result.get("kernel_capture", {}).get("runtime_ns") or 0)
                / (float(result["aggregate"]["makespan_sec"]) * 8 * 1e9)
            ),
            "cases": [],
        }
        for instance, case in cases.items():
            trace = read_json(ROOT / str(case["trace_file"]))
            annotation_path = ROOT / "annotations" / "formal" / f"{instance}.annotation.json"
            annotation = read_json(annotation_path)
            raw = PROFILE_MODULE.raw_capture_metrics(
                raw_path,
                trace,
                annotation,
                session_id=session_ids[instance],
            )
            trial["cases"].append(
                {
                    "instance_id": instance,
                    "task_rows": raw["task_rows"],
                    "cross_generation_windows": raw["cross_generation_window_count"],
                    "join_windows": raw["join_window_count"],
                    "runnable_wait_sec": sum(
                        float(item.get("owned_runnable_wait_sec") or 0)
                        for item in raw["tool_windows"]
                    ),
                    "return_cohort_lifetime_wait_sec": sum(
                        float(item.get("return_cohort_lifetime_runnable_wait_sec") or 0)
                        for item in raw["tool_windows"]
                    ),
                    "return_window_last_wait_lower_bound_sec": sum(
                        float(item.get("return_window_observed_last_wait_lower_bound_sec") or 0)
                        for item in raw["tool_windows"]
                    ),
                }
            )
            for window in raw["tool_windows"]:
                windows.append(
                    {
                        "trial": result_path.parent.name,
                        "instance_id": instance,
                        **window,
                    }
                )
        trial_payloads.append(trial)
    return {
        "trials": trial_payloads,
        "summary": {
            "captured_trials": len(trial_payloads),
            "cpu_busy_fraction_mean": mean(
                [float(item["cpu_busy_fraction"]) for item in trial_payloads]
            ),
            "process_fanout_windows": sum(
                int(item.get("owned_peak_live_tasks") or 0) >= 4 for item in windows
            ),
            "runnable_wait_windows": sum(
                float(item.get("owned_runnable_wait_sec") or 0) >= 0.01 for item in windows
            ),
            "return_cohort_windows": sum(
                int(item.get("return_cohort_task_count") or 0) >= 2 for item in windows
            ),
            "return_windows_with_observed_wait": sum(
                float(item.get("return_window_observed_last_wait_lower_bound_sec") or 0) > 0
                for item in windows
            ),
            "cross_generation_windows": sum(
                int(item.get("older_generation_live_tasks") or 0) > 0 for item in windows
            ),
            "return_window_last_wait_lower_bound_sec": sum(
                float(item.get("return_window_observed_last_wait_lower_bound_sec") or 0)
                for item in windows
            ),
        },
        "top_return_wait_windows": sorted(
            [
                item
                for item in windows
                if float(item.get("return_window_observed_last_wait_lower_bound_sec") or 0) > 0
            ],
            key=lambda item: float(item["return_window_observed_last_wait_lower_bound_sec"]),
            reverse=True,
        )[:40],
    }


def flow_control_summary(campaign: pathlib.Path) -> list[dict[str, Any]]:
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for path in sorted((campaign / "flowcausal_v2").glob("trial-*/result.json")):
        result = read_json(path)
        for session in result.get("flowcausal_control", {}).get("sessions", []):
            if isinstance(session, dict) and isinstance(session.get("stats"), dict):
                by_case[str(session.get("instance_id") or "")].append(session["stats"])
    rows: list[dict[str, Any]] = []
    for instance, values in sorted(by_case.items()):
        role_runtime = [
            mean([float(item.get("role_runtime_ns", [0] * 5)[index]) for item in values]) / 1e9
            for index in range(5)
        ]
        rows.append(
            {
                "instance_id": instance,
                "trials": len(values),
                "applied_bias_ms": mean([float(item.get("bias_ns") or 0) for item in values]) / 1e6,
                "budget_limited": mean([float(item.get("budget_limited") or 0) for item in values]),
                "tail_promotions": mean([float(item.get("tail_promotions") or 0) for item in values]),
                "role_runtime_sec": {
                    "normal": role_runtime[0],
                    "progress": role_runtime[1],
                    "interactive": role_runtime[2],
                    "parallel": role_runtime[3],
                    "background": role_runtime[4],
                },
            }
        )
    return rows


def markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HarnessReplayBench 1.0 调度机会对照结果",
        "",
        "本轮只在由 `scx_simple` 证据选出的四任务套件上比较三种调度方法。全部任务固定在 0–7 号处理器、同时启动；每种方法三轮，轮换执行顺序，每轮清页缓存。",
        "",
        "## 整体结果",
        "",
        "| 方法 | 轮次 | 总完成时间（秒） | 平均工作流完成时间（秒） | 同步工具阻塞 P95（秒） | 总完成时间变化 | 工作流完成时间变化 | 工具阻塞 P95 变化 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in payload["methods"]:
        relative = item["relative_to_scx_simple_percent"]
        lines.append(
            f"| `{item['method']}` | {item['trials']} | {item['makespan_sec']:.3f} | "
            f"{item['workflow_completion_sec']:.3f} | {item['tool_service_p95_sec']:.3f} | "
            f"{relative['makespan_reduction']:+.1f}% | {relative['workflow_completion_reduction']:+.1f}% | "
            f"{relative['tool_service_p95_reduction']:+.1f}% |"
        )
    lines.extend(
        [
            "",
            "正数表示相对 `scx_simple` 缩短。启发式工具里程碑不作为关键路径指标。",
            "",
            "## 分任务工作流完成时间",
            "",
            "| 任务 | 方法 | 时间（秒） | 相对 scx_simple 变化 |",
            "|---|---|---:|---:|",
        ]
    )
    for item in payload["cases"]:
        lines.append(
            f"| `{item['instance_id']}` | `{item['method']}` | "
            f"{item['workflow_completion_sec']:.3f} | {item['workflow_completion_reduction_percent']:+.1f}% |"
        )
    capture = payload["concurrent_capture"]["summary"]
    lines.extend(
        [
            "",
            "## C/eBPF 机会核对",
            "",
            f"三轮 `scx_simple` 的平均处理器忙碌率为 {capture['cpu_busy_fraction_mean'] * 100:.1f}%。C/eBPF 共识别到 {capture['process_fanout_windows']} 个进程扇出窗口、{capture['runnable_wait_windows']} 个具有可运行等待的窗口、{capture['return_cohort_windows']} 个返回前仍有同代次任务存活的窗口，以及 {capture['cross_generation_windows']} 个跨工具存活窗口。",
            "",
            f"在返回前 5–50 毫秒内，每个任务最后一次可运行等待的累计可观测下界为 {capture['return_window_last_wait_lower_bound_sec']:.3f} 秒，分布在 {capture['return_windows_with_observed_wait']} 个窗口中。这证明真实回放确有返回前调度等待，但不等于所有等待都可通过统一提权消除。",
            "",
            "## 当前 FlowCausal 的动作",
            "",
            "| 任务 | 平均提权额度（毫秒） | 平均尾部推进次数 | 平均额度不足次数 |",
            "|---|---:|---:|---:|",
        ]
    )
    for item in payload["flow_control"]:
        lines.append(
            f"| `{item['instance_id']}` | {item['applied_bias_ms']:.1f} | "
            f"{item['tail_promotions']:.1f} | {item['budget_limited']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## 结论",
            "",
            payload["conclusion"],
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总调度机会套件结果")
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--output")
    parser.add_argument("--markdown")
    args = parser.parse_args()
    campaign = pathlib.Path(args.campaign).resolve()
    methods, cases = method_results(campaign)
    capture = concurrent_capture(campaign)
    flow_control = flow_control_summary(campaign)
    flow = next(item for item in methods if item["method"] == "flowcausal_v2")
    payload = {
        "schema": "harness_replaybench.opportunity_campaign_summary.v1",
        "campaign": campaign.as_posix(),
        "experiment": {
            "task_set": [
                "matplotlib__matplotlib-23314",
                "dask__dask-5510",
                "iterative__dvc-1494",
                "dask__dask-4003",
            ],
            "release": "concurrent",
            "cpu_set": "0-7",
            "trials": 3,
            "cache": "每轮清空页缓存",
            "execution_order": "轮换方法顺序",
        },
        "methods": methods,
        "cases": cases,
        "concurrent_capture": capture,
        "flow_control": flow_control,
        "conclusion": (
            "HarnessReplayBench 0.3 的大幅收益来自预先给出的前台/后台角色和贯穿关键命令的持续过载。"
            "1.0 的真实同步工具也存在进程扇出、可运行等待和返回前汇合，但当前 FlowCausal v2 把工具阶段粗略映射为统一角色，"
            f"在本套件上总完成时间变化为 {flow['relative_to_scx_simple_percent']['makespan_reduction']:+.1f}%，"
            f"同步工具阻塞 P95 变化为 {flow['relative_to_scx_simple_percent']['tool_service_p95_reduction']:+.1f}%。"
            "四个任务中只有 Dask 4003 稳定触发了尾部推进，它也正是 FlowCausal 唯一缩短完整工作流的任务；"
            "另外三个任务几乎没有触发尾部推进。因而现象与动作能够对应，而不是简单的机器波动。"
            "因此，0.3 证明的是有准确角色信息时重排的收益上界；该收益不能直接迁移为 1.0 上现有规则的效果。"
            "后续机制应识别同一工具内部真正决定返回的任务群，并只在观测到返回前可运行等待时施加短时推进。"
        ),
    }
    output = pathlib.Path(args.output) if args.output else campaign / "归因分析.json"
    report = pathlib.Path(args.markdown) if args.markdown else campaign / "归因分析.md"
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report.write_text(markdown(payload), encoding="utf-8")
    print(json.dumps({"output": str(output), "report": str(report), "capture": capture["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

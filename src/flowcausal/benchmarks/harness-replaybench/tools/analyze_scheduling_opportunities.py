#!/usr/bin/env python3
"""从 scx_simple 的 C/eBPF 画像与并发回放中提取可调度机会。"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import statistics
from collections import defaultdict
from typing import Any, Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
EXCLUSIONS = ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def read_jsonl(path: pathlib.Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def command_times_from_result(path: pathlib.Path, result: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if result.get("scheduler") != "scx_simple" or result.get("status") != "pass":
        return rows
    for case in result.get("cases", []):
        if not isinstance(case, dict):
            continue
        retained = case.get("retained") if isinstance(case.get("retained"), dict) else {}
        event_name = str(retained.get("events") or "")
        candidates = []
        if event_name:
            candidates.append(path.parent / event_name)
        instance = str(case.get("instance_id") or "")
        candidates.extend(sorted((path.parent / "cases").glob(f"*-{instance}/events.jsonl")))
        events_path = next((item for item in candidates if item.is_file()), None)
        if events_path is None:
            continue
        for event in read_jsonl(events_path):
            if event.get("record") != "command_end" or event.get('timed_out'):
                continue
            rows.append(
                {
                    "instance_id": instance,
                    "command_id": str(event.get("command_id") or ""),
                    "elapsed_sec": max(0, int(event.get("elapsed_ns") or 0)) / 1_000_000_000,
                    "result": path.as_posix(),
                }
            )
    return rows


def evidence_tags(window: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    if window.get('timed_out'):
        tags.append('超时终止')
    if int(window.get("owned_peak_live_tasks") or 0) >= 4:
        tags.append("进程扇出")
    if (
        float(window.get("owned_runnable_wait_sec") or 0) >= 0.01
        and float(window.get("owned_runnable_wait_fraction") or 0) >= 0.01
    ):
        tags.append("可运行队列等待")
    if (
        int(window.get("return_cohort_task_count") or 0) >= 2
        and float(window.get("return_cohort_lifetime_runnable_wait_sec") or 0) >= 0.005
    ):
        tags.append("返回前汇合任务群")
    if int(window.get("older_generation_live_tasks") or 0) > 0:
        tags.append("前序工具进程仍然存活")
    if float(window.get("older_generation_runtime_in_window_lower_bound_sec") or 0) > 0:
        tags.append("前序工具进程在当前窗口内运行")
    if float(window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0) > 0:
        tags.append("当前工具进程在窗口内等待处理器")
    return tags


def opportunity_level(window: dict[str, Any]) -> str:
    if window.get('timed_out'):
        return '超时终止，不用于正常完成性能比较'
    wait = float(window.get("owned_runnable_wait_sec") or 0)
    fraction = float(window.get("owned_runnable_wait_fraction") or 0)
    peak = int(window.get("owned_peak_live_tasks") or 0)
    cohort = int(window.get("return_cohort_task_count") or 0)
    elapsed = float(window.get("elapsed_sec") or 0)
    if elapsed >= 0.1 and wait >= 0.05 and fraction >= 0.02 and (peak >= 4 or cohort >= 2):
        return "强候选"
    if elapsed >= 0.05 and wait >= 0.01 and (peak >= 2 or cohort >= 2):
        return "一般候选"
    return "低机会"


def confirmed_cross_tool_contention(window: dict[str, Any]) -> bool:
    """确认两类活动出现在同一工具窗口；不证明逐时刻重叠或因果阻塞。"""
    return (
        not window.get('timed_out')
        and bool(window.get("synchronous", True))
        and float(window.get("elapsed_sec") or 0) >= 0.05
        and int(window.get("older_generation_live_tasks") or 0) > 0
        and float(window.get("older_generation_overlap_task_sec") or 0) >= 0.05
        and float(window.get("older_generation_runtime_in_window_lower_bound_sec") or 0)
        >= 0.005
        and float(window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0)
        >= 0.005
    )


def target_cross_tool_window(window: dict[str, Any]) -> bool:
    """选择前序运行量与当前等待量相对窗口长度均不可忽略的窗口。"""
    elapsed = float(window.get("elapsed_sec") or 0)
    if not confirmed_cross_tool_contention(window) or elapsed <= 0:
        return False
    old_runtime = float(
        window.get("older_generation_runtime_in_window_lower_bound_sec") or 0
    )
    current_wait = float(
        window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0
    )
    return old_runtime / elapsed >= 0.05 and current_wait / elapsed >= 0.01


def write_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# HarnessReplayBench 1.0 真实调度空间",
        "",
        "本报告只使用 `scx_simple` 下的 C/eBPF 单任务记录和并发对照，不读取任何候选调度器的结果。它用于确认前序工具留下的进程是否确实在当前同步工具执行期间运行，以及当前工具进程是否在这一期间等待过处理器。",
        "",
        "## 指标语义",
        "",
        "- 完整工作流完成时间：本地回放的全部工具返回；这是端到端主指标。",
        "- 同步工具阻塞时间：智能体发起同步工具到工具返回的本地墙钟时间。",
        "- 窗口内运行时间下界：根据每个进程的生命周期和累计运行时间计算。即使把运行时间尽可能安排到当前窗口之外，仍无法放完的部分，才计入下界。",
        "- 窗口内等待时间下界：采用相同方法计算，只统计可以确认发生在当前工具窗口内的可运行等待。",
        "- 跨工具存活：前序工具后代的生命区间与当前工具窗口相交；它只证明存活重叠，不直接证明正在占用处理器。",
        "- 启发式工具里程碑：兼容旧轨迹中的选中测试命令，不再称为关键路径。",
        "",
        "## 总览",
        "",
        f"- 已画像任务：{summary['profiled_tasks']} 个；工具窗口：{summary['tool_windows']} 个。",
        f"- 强候选工具窗口：{summary['strong_candidate_windows']} 个，分布在 {summary['tasks_with_strong_candidates']} 个任务中。",
        f"- 已确认前序进程运行与当前同步工具等待同时出现的窗口：{summary['confirmed_cross_tool_contention_windows']} 个，分布在 {summary['tasks_with_confirmed_cross_tool_contention']} 个任务中。",
        f"- 其中前序运行量不低于窗口长度 5%、当前等待量不低于窗口长度 1% 的研究窗口：{summary['target_cross_tool_windows']} 个，分布在 {summary['tasks_with_target_cross_tool_windows']} 个任务中。",
        f"- 在已有并发对照中可匹配的工具窗口：{summary['matched_concurrent_windows']} 个。",
        f"- 并发下服务时间显著膨胀的工具窗口：{summary['inflated_windows']} 个。",
        "",
        "## 调度机会最高的任务",
        "",
        "| 任务 | 研究窗口 | 已确认窗口 | 前序进程窗口内运行下界（任务秒） | 当前工具窗口内等待下界（任务秒） | 并发增加时间（秒） |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in payload["task_ranking"][:20]:
        lines.append(
            f"| `{item['instance_id']}` | {item['target_cross_tool_windows']} | "
            f"{item['confirmed_cross_tool_contention_windows']} | "
            f"{item['older_runtime_in_window_lower_bound_sec']:.3f} | "
            f"{item['owned_wait_in_window_lower_bound_sec']:.3f} | "
            f"{item['concurrent_added_service_sec']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## 解释边界",
            "",
            "报告把“存活重叠”“同一工具期间记录到两类活动”和“达到研究量级”分开。研究窗口除要求两类活动下界同时存在，还要求前序运行量不低于窗口长度的 5%，当前等待量不低于窗口长度的 1%。这些比例在分析前固定，且不读取候选调度器结果。这些累计下界不证明两类活动发生在同一时刻，也不证明前序进程导致了当前进程等待。该证据仍不自动说明前序进程可以任意延后；可延后性需要结合工具语义或受控机制实验继续核对。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 HarnessReplayBench 调度机会画像")
    parser.add_argument(
        "--profiles",
        default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"),
    )
    parser.add_argument("--comparison-root", action="append", default=[])
    parser.add_argument(
        "--output",
        default=str(ROOT / "datasets" / "HarnessReplayBench-1.0-调度机会画像.json"),
    )
    parser.add_argument(
        "--markdown",
        default=str(ROOT / "HarnessReplayBench-1.0-调度机会画像.md"),
    )
    parser.add_argument("--suite-size", type=int, default=4)
    parser.add_argument('--no-update-suites', action='store_true',
                        help='只更新分析结果，保留已发布的任务组合和套件定义')
    args = parser.parse_args()

    profiles = read_json(pathlib.Path(args.profiles))
    exclusion_payload = read_json(EXCLUSIONS) if EXCLUSIONS.is_file() else {"items": []}
    exclusions = {
        str(item.get("instance_id") or "")
        for item in exclusion_payload.get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    }
    tools: list[dict[str, Any]] = []
    baseline: dict[tuple[str, str], float] = {}
    for profile in profiles.get("profiles", []):
        if (
            not isinstance(profile, dict)
            or profile.get("status") != "pass"
            or str(profile.get("instance_id") or "") in exclusions
        ):
            continue
        instance = str(profile["instance_id"])
        for window in profile.get("kernel", {}).get("raw", {}).get("tool_windows", []):
            if not isinstance(window, dict):
                continue
            row = {**window, "instance_id": instance}
            row["evidence"] = evidence_tags(row)
            row["opportunity_level"] = opportunity_level(row)
            row["confirmed_cross_tool_contention"] = confirmed_cross_tool_contention(row)
            row["target_cross_tool_window"] = target_cross_tool_window(row)
            row["single_elapsed_sec"] = float(row.get("elapsed_sec") or 0)
            tools.append(row)
            if not row.get('timed_out'):
                baseline[(instance, str(row.get("command_id") or ""))] = row["single_elapsed_sec"]

    concurrent: dict[tuple[str, str], list[float]] = defaultdict(list)
    observed_results: set[str] = set()
    comparison_artifacts = []
    for root_name in args.comparison_root:
        root = pathlib.Path(root_name).resolve()
        paths = [root] if root.is_file() else sorted(root.rglob("result.json"))
        for path in paths:
            try:
                result = read_json(path)
            except (OSError, json.JSONDecodeError, RuntimeError):
                continue
            rows = command_times_from_result(path, result)
            if rows:
                suite_id = str(result.get("suite_id") or result.get("manifest") or "unknown-suite")
                scheduler = str(result.get("scheduler") or "unknown-scheduler")
                observed_results.add(f"{suite_id}/{scheduler}/{path.parent.name}")
                events = []
                for case in result.get('cases', []):
                    event_name = case.get('retained', {}).get('events')
                    if event_name:
                        event_path = path.parent / event_name
                        events.append(dict(instance_id=case.get('instance_id'),
                                           events_sha256=hashlib.sha256(event_path.read_bytes()).hexdigest()))
                comparison_artifacts.append(dict(suite_id=suite_id, scheduler=scheduler,
                    result_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), events=events))
            for row in rows:
                key = (row["instance_id"], row["command_id"])
                if key in baseline:
                    concurrent[key].append(float(row["elapsed_sec"]))

    inflated = 0
    matched = 0
    by_task: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "strong_candidate_windows": 0,
            "owned_runnable_wait_sec": 0.0,
            "return_cohort_wait_sec": 0.0,
            "concurrent_added_service_sec": 0.0,
            "matched_windows": 0,
            "inflated_windows": 0,
            "confirmed_cross_tool_contention_windows": 0,
            "older_runtime_in_window_lower_bound_sec": 0.0,
            "owned_wait_in_window_lower_bound_sec": 0.0,
            "target_cross_tool_windows": 0,
        }
    )
    for row in tools:
        instance = str(row["instance_id"])
        item = by_task[instance]
        if row["opportunity_level"] == "强候选":
            item["strong_candidate_windows"] += 1
        if row["confirmed_cross_tool_contention"]:
            item["confirmed_cross_tool_contention_windows"] += 1
            item["older_runtime_in_window_lower_bound_sec"] += float(
                row.get("older_generation_runtime_in_window_lower_bound_sec") or 0
            )
            item["owned_wait_in_window_lower_bound_sec"] += float(
                row.get("owned_runnable_wait_in_window_lower_bound_sec") or 0
            )
        if row["target_cross_tool_window"]:
            item["target_cross_tool_windows"] += 1
        item["owned_runnable_wait_sec"] += float(row.get("owned_runnable_wait_sec") or 0)
        item["return_cohort_wait_sec"] += float(
            row.get("return_cohort_lifetime_runnable_wait_sec") or 0
        )
        values = concurrent.get((instance, str(row.get("command_id") or "")), [])
        if values:
            matched += 1
            item["matched_windows"] += 1
            concurrent_median = statistics.median(values)
            added = concurrent_median - float(row["single_elapsed_sec"])
            row["concurrent_observations"] = len(values)
            row["concurrent_median_sec"] = concurrent_median
            row["concurrent_added_service_sec"] = added
            row["concurrent_inflation_percent"] = (
                added / float(row["single_elapsed_sec"]) * 100
                if float(row["single_elapsed_sec"]) > 0 else None
            )
            if added >= 0.02 and row["concurrent_inflation_percent"] is not None and row["concurrent_inflation_percent"] >= 10:
                inflated += 1
                item["inflated_windows"] += 1
                item["concurrent_added_service_sec"] += added

    task_ranking = [
        {"instance_id": instance, **values}
        for instance, values in by_task.items()
    ]
    task_ranking.sort(
        key=lambda item: (
            -int(item["target_cross_tool_windows"]),
            -int(item["confirmed_cross_tool_contention_windows"]),
            -float(item["concurrent_added_service_sec"]),
            -int(item["strong_candidate_windows"]),
            -float(item["return_cohort_wait_sec"]),
            str(item["instance_id"]),
        )
    )
    anchor_instances = [
        str(item["instance_id"])
        for item in task_ranking
        if int(item["target_cross_tool_windows"]) > 0
    ][: args.suite_size]
    profile_metrics = {
        str(profile.get("instance_id") or ""): {
            "elapsed_sec": float(profile.get("replay_elapsed_sec") or 0),
            "runtime_sec": float(profile.get("kernel", {}).get("runtime_sec") or 0),
        }
        for profile in profiles.get("profiles", [])
        if isinstance(profile, dict) and profile.get("status") == "pass"
    }
    support_instances = sorted(
        (
            instance
            for instance, values in profile_metrics.items()
            if instance not in exclusions
            and instance not in anchor_instances
            and 0 < float(values["elapsed_sec"]) <= 60.0
        ),
        key=lambda instance: (-float(profile_metrics[instance]["runtime_sec"]), instance),
    )
    suite_instances = list(anchor_instances)
    for instance in support_instances:
        if len(suite_instances) >= args.suite_size:
            break
        suite_instances.append(instance)

    payload = {
        "schema": "harness_replaybench.scheduling_opportunities.v1",
        "version": "1.0",
        "reference_scheduler": "scx_simple",
        "capture": "C/eBPF",
        "comparison_results": sorted(observed_results),
        "comparison_artifacts": comparison_artifacts,
        "comparison_artifact_scope": "Checksums identify individual external run records and command events; comparison_results is a legacy suite label list, not a unique run inventory.",
        "profile_measurement_id": profiles.get('measurement_id'),
        "suite_definitions_updated": not args.no_update_suites,
        "metric_semantics": {
            "workflow_completion": "全部本地回放工具返回",
            "synchronous_tool_service": "同步工具发起到返回的本地墙钟时间",
            "selected_tool_milestone": "启发式选中的工具返回点，不是关键路径",
            "return_cohort": "返回前最后 5–50 毫秒仍存活的同工具代次任务",
            "window_activity_lower_bound": "由进程生命周期与累计运行、等待时间推导出的窗口内严格下界",
        },
        "summary": {
            "profiled_tasks": len(by_task),
            "tool_windows": len(tools),
            "strong_candidate_windows": sum(row["opportunity_level"] == "强候选" for row in tools),
            "tasks_with_strong_candidates": sum(
                int(item["strong_candidate_windows"]) > 0 for item in task_ranking
            ),
            "confirmed_cross_tool_contention_windows": sum(
                bool(row["confirmed_cross_tool_contention"]) for row in tools
            ),
            "tasks_with_confirmed_cross_tool_contention": sum(
                int(item["confirmed_cross_tool_contention_windows"]) > 0
                for item in task_ranking
            ),
            "target_cross_tool_windows": sum(
                bool(row["target_cross_tool_window"]) for row in tools
            ),
            "tasks_with_target_cross_tool_windows": sum(
                int(item["target_cross_tool_windows"]) > 0 for item in task_ranking
            ),
            "matched_concurrent_windows": matched,
            "inflated_windows": inflated,
        },
        "selected_anchor_instances": anchor_instances,
        "selected_support_instances": [
            instance for instance in suite_instances if instance not in anchor_instances
        ],
        "selected_suite_instances": suite_instances,
        "task_ranking": task_ranking,
        "tool_windows": sorted(
            tools,
            key=lambda item: (
                not bool(item["target_cross_tool_window"]),
                not bool(item["confirmed_cross_tool_contention"]),
                item["opportunity_level"] != "强候选",
                -float(item.get("concurrent_added_service_sec") or 0),
                -float(item.get("owned_runnable_wait_sec") or 0),
            ),
        ),
        "evidence_scope": "Activity lower bounds in the same tool window; not pointwise temporal overlap or a causal blocking proof. Legacy contention field names retain this limited meaning.",
        "notes": [
            "套件选择只读取 scx_simple，不读取任何候选调度器结果。",
            "前序进程运行下界和当前同步工具等待下界在同一工具窗口内均达到阈值，才列为进一步核对的候选。",
            "研究窗口进一步要求前序运行下界与当前等待下界相对窗口长度达到固定比例。",
            "并发服务时间增加用于确认混部影响；存活重叠不自动解释为依赖关系或可延后工作。",
        ],
    }
    output = pathlib.Path(args.output)
    report = pathlib.Path(args.markdown)
    write_json(output, payload)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(markdown(payload), encoding="utf-8")

    if anchor_instances and not args.no_update_suites:
        anchor_task_set = ROOT / "tasksets" / "v1" / "scheduling-opportunity-anchor.json"
        anchor_suite = ROOT / "suite-definitions" / "v1" / "hrb1-scheduling-opportunity-anchor-tool.json"
        write_json(
            anchor_task_set,
            {
                "schema": "harness_replaybench.task_set.v1",
                "task_set_id": f"hrb1-scheduling-opportunity-anchor-{len(anchor_instances)}",
                "description": "基线内核记录中达到研究量级的真实跨工具执行重叠任务。",
                "selection_basis": "前序进程窗口内运行下界不低于窗口长度的 5%，当前同步工具窗口内等待下界不低于窗口长度的 1%；只使用 scx_simple 数据。",
                "instances": anchor_instances,
            },
        )
        write_json(
            anchor_suite,
            {
                "schema": "harness_replaybench.suite_definition.v1",
                "suite_id": "hrb1-scheduling-opportunity-anchor-tool",
                "description": "单独运行真实跨工具执行重叠任务，用于核对任务内部的调度空间。",
                "benchmark_layer": "real-tool-composition",
                "task_set_file": anchor_task_set.relative_to(ROOT).as_posix(),
                "release_profile_file": "release-profiles/v1/single.json",
                "replay_mode": "tool-execution",
                "cpu_set": "0-7",
            },
        )

    if len(suite_instances) >= 2 and anchor_instances and not args.no_update_suites:
        task_set = ROOT / "tasksets" / "v1" / "scheduling-opportunity.json"
        suite = ROOT / "suite-definitions" / "v1" / "hrb1-scheduling-opportunity-concurrent-tool.json"
        write_json(
            task_set,
            {
                "schema": "harness_replaybench.task_set.v1",
                "task_set_id": f"hrb1-scheduling-opportunity-{len(suite_instances)}",
                "description": "达到研究量级的真实跨工具执行重叠任务与真实处理器密集任务的组合。",
                "selection_basis": "先依据 scx_simple 的窗口活动下界选择研究任务，再按单任务累计 CPU 运行时间选择不超过 60 秒的并发任务；未读取候选调度器结果。",
                "instances": suite_instances,
            },
        )
        write_json(
            suite,
            {
                "schema": "harness_replaybench.suite_definition.v1",
                "suite_id": "hrb1-scheduling-opportunity-concurrent-tool",
                "description": "在多个真实智能体任务并发时观察任务内部跨工具执行重叠。",
                "benchmark_layer": "real-tool-composition",
                "task_set_file": task_set.relative_to(ROOT).as_posix(),
                "release_profile_file": "release-profiles/v1/concurrent.json",
                "replay_mode": "tool-execution",
                "cpu_set": "0-7",
            },
        )

    print(json.dumps({"output": str(output), "suite_instances": suite_instances, **payload["summary"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""从一次 scx_simple 并发采集中核对前后工具是否同时竞争处理器。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from analyze_behavior_profiles import raw_capture_metrics, read_json


ROOT = pathlib.Path(__file__).resolve().parents[1]


def confirmed(window: dict[str, Any]) -> bool:
    return (
        bool(window.get("synchronous", True))
        and float(window.get("elapsed_sec") or 0) >= 0.05
        and int(window.get("older_generation_live_tasks") or 0) > 0
        and float(window.get("older_generation_overlap_task_sec") or 0) >= 0.05
        and float(window.get("older_generation_runtime_in_window_lower_bound_sec") or 0)
        >= 0.005
        and float(window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0)
        >= 0.005
    )


def evidence_row(instance: str, window: dict[str, Any]) -> dict[str, Any]:
    return {
        "instance_id": instance,
        "command_id": str(window.get("command_id") or ""),
        "stage": str(window.get("stage") or "UNKNOWN"),
        "elapsed_sec": float(window.get("elapsed_sec") or 0),
        "older_generation_live_tasks": int(
            window.get("older_generation_live_tasks") or 0
        ),
        "older_generation_runtime_lower_bound_sec": float(
            window.get("older_generation_runtime_in_window_lower_bound_sec") or 0
        ),
        "current_generation_wait_lower_bound_sec": float(
            window.get("owned_runnable_wait_in_window_lower_bound_sec") or 0
        ),
        "return_cohort_tasks": int(window.get("return_cohort_task_count") or 0),
        "confirmed": confirmed(window),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# {report['suite_id']} 基线竞争核对",
        "",
        "本报告只读取 `scx_simple` 的 C/eBPF 原始记录，不读取 FlowCausal 结果。确认条件要求前序工具进程在当前同步工具窗口内确实运行，同时当前工具进程确实等待处理器。",
        "",
        f"- 平均使用处理器数：{report['average_busy_cpus']:.2f}；",
        f"- 已确认窗口：{report['confirmed_windows']} 个；",
        f"- 采集事件丢失：{report['dropped_events']}；",
        f"- 结论：{'存在可调整的前后工具处理器竞争' if report['status'] == 'confirmed' else '当前组合尚未确认前后工具处理器竞争'}。",
        "",
        "| 任务 | 当前工具 | 阶段 | 窗口（秒） | 前序进程运行下界（任务秒） | 当前进程等待下界（任务秒） | 返回成员 | 确认 |",
        "|---|---|---|---:|---:|---:|---:|---|",
    ]
    for row in report["windows"]:
        display = dict(row)
        display["confirmed"] = "是" if row["confirmed"] else "否"
        lines.append(
            "| `{instance_id}` | `{command_id}` | {stage} | {elapsed_sec:.3f} | "
            "{older_generation_runtime_lower_bound_sec:.3f} | "
            "{current_generation_wait_lower_bound_sec:.3f} | "
            "{return_cohort_tasks} | {confirmed} |".format(
                **display
            )
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 scx_simple 并发采集中的跨工具竞争")
    parser.add_argument("--result", required=True)
    parser.add_argument("--output")
    parser.add_argument("--markdown")
    args = parser.parse_args()

    result_path = pathlib.Path(args.result).resolve()
    result = read_json(result_path)
    if result.get("scheduler") != "scx_simple" or result.get("status") != "pass":
        raise SystemExit("输入必须是成功的 scx_simple 结果")
    capture = result_path.parent / "kernel-capture.jsonl"
    if not capture.is_file():
        raise SystemExit(f"缺少原始内核记录：{capture}")
    manifest = read_json(ROOT / str(result.get("manifest") or ""))
    case_by_instance = {
        str(item.get("instance_id") or ""): item
        for item in manifest.get("cases", [])
        if isinstance(item, dict)
    }
    session_by_instance = {
        str(item.get("instance_id") or ""): int(item.get("session_id") or 0)
        for item in result.get("kernel_capture", {}).get("session_ids", [])
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    for instance, case in case_by_instance.items():
        session_id = session_by_instance.get(instance)
        trace_path = ROOT / str(case.get("trace_file") or "")
        if not session_id or not trace_path.is_file():
            continue
        trace = read_json(trace_path)
        annotation_path = ROOT / "annotations" / "formal" / f"{instance}.annotation.json"
        annotation = read_json(annotation_path) if annotation_path.is_file() else None
        metrics = raw_capture_metrics(capture, trace, annotation, session_id=session_id)
        for window in metrics.get("cross_generation_windows", []):
            if isinstance(window, dict):
                rows.append(evidence_row(instance, window))
    rows.sort(
        key=lambda item: (
            not item["confirmed"],
            -item["current_generation_wait_lower_bound_sec"],
            -item["older_generation_runtime_lower_bound_sec"],
        )
    )
    aggregate = result.get("aggregate", {})
    makespan = float(aggregate.get("makespan_sec") or 0)
    runtime = float(result.get("kernel_capture", {}).get("runtime_ns") or 0) / 1e9
    count = sum(bool(row["confirmed"]) for row in rows)
    report = {
        "schema": "harness_replaybench.concurrent_cross_tool_evidence.v1",
        "suite_id": str(result.get("suite_id") or ""),
        "reference_scheduler": "scx_simple",
        "capture": "C/eBPF",
        "candidate_scheduler_results_used": False,
        "result": result_path.as_posix(),
        "status": "confirmed" if count else "not-confirmed",
        "makespan_sec": makespan,
        "runtime_sec": runtime,
        "average_busy_cpus": runtime / makespan if makespan else 0.0,
        "dropped_events": int(
            result.get("kernel_capture", {}).get("dropped_events") or 0
        ),
        "confirmed_windows": count,
        "windows": rows,
    }
    output = pathlib.Path(args.output) if args.output else result_path.parent / "跨工具竞争.json"
    report_md = pathlib.Path(args.markdown) if args.markdown else result_path.parent / "跨工具竞争.md"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_md.write_text(markdown(report), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("suite_id", "status", "average_busy_cpus", "confirmed_windows", "dropped_events")}, ensure_ascii=False))
    return 0 if report["status"] == "confirmed" and not report["dropped_events"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

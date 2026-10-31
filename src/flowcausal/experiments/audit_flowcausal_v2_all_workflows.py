#!/usr/bin/env python3
"""合并逐工作流结果，并执行 FlowCausal v2 的严格完整性检查。"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "benchmarks" / "harness-replaybench" / "manifests" / "v1" / "single"


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def result_key(result: dict[str, Any]) -> str:
    cases = result.get("cases")
    if not isinstance(cases, list) or len(cases) != 1 or not isinstance(cases[0], dict):
        raise RuntimeError("逐工作流结果必须恰好包含一个任务")
    return str(cases[0].get("instance_id") or "")


def collect(roots: list[pathlib.Path]) -> dict[str, pathlib.Path]:
    selected: dict[str, pathlib.Path] = {}
    for root in roots:
        for path in sorted(root.glob("*/result.json")):
            key = result_key(load(path))
            if not key:
                raise RuntimeError(f"结果缺少任务名称：{path}")
            selected[key] = path
    return selected


def check_one(path: pathlib.Path) -> tuple[list[str], dict[str, int | str]]:
    result = load(path)
    name = result_key(result)
    case = result["cases"][0]
    problems: list[str] = []
    command_count = int(case.get("command_count") or 0)

    def require(condition: bool, message: str) -> None:
        if not condition:
            problems.append(message)

    require(result.get("status") == "pass", "工作流状态不是 pass")
    require(case.get("status") == "pass", "任务状态不是 pass")
    require(case.get("returncode") == 0, "任务入口返回状态不是 0")
    require(not case.get("failure"), "任务记录了失败原因")
    require(command_count > 0, "没有命令记录")
    require(case.get("completed_commands") == command_count, "命令没有全部完成")
    require(
        case.get("command_returncode_matches") == command_count,
        "存在与规范化轨迹不一致的命令返回状态",
    )
    require(case.get("unexpected_failure_commands") == 0, "存在非预期失败命令")

    capture = result.get("kernel_capture")
    require(isinstance(capture, dict), "缺少内核采集结果")
    if isinstance(capture, dict):
        require(capture.get("complete") is True, "内核采集不完整")
        require(capture.get("dropped_events") == 0, "内核事件存在丢失")
        require(capture.get("raw_retained") is False, "成功结果仍保留大体积原始采集")

    experiment = result.get("experiment")
    require(isinstance(experiment, dict), "缺少实验条件")
    if isinstance(experiment, dict):
        require(experiment.get("page_cache_cleared") is True, "未清理文件页缓存")
        require(experiment.get("success_artifacts_compacted") is True, "成功结果未精简")

    tested = result.get("tested_system")
    require(isinstance(tested, dict), "缺少被测调度器状态")
    if isinstance(tested, dict):
        require(
            tested.get("sched_ext_during", {}).get("ops") == "flowcausal_v2",
            "运行期间加载的不是 flowcausal_v2",
        )
        require(
            tested.get("sched_ext_after", {}).get("state") == "disabled",
            "运行结束后 sched_ext 没有卸载",
        )

    control = result.get("flowcausal_control")
    sessions = control.get("sessions") if isinstance(control, dict) else None
    require(isinstance(sessions, list) and len(sessions) == 1, "C 控制程序会话记录不完整")
    stats: dict[str, Any] = {}
    if isinstance(sessions, list) and len(sessions) == 1 and isinstance(sessions[0], dict):
        stats = sessions[0].get("stats") if isinstance(sessions[0].get("stats"), dict) else {}
        activations = int(stats.get("adaptive_weight_activations") or 0)
        deactivations = int(stats.get("adaptive_weight_deactivations") or 0)
        require(activations == deactivations, "自动权重提高和恢复次数不一致")
        log_value = sessions[0].get("log")
        log_path = path.parent / str(log_value or "")
        require(bool(log_value) and log_path.is_file(), "缺少 C 控制程序日志")
        if log_path.is_file():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            records = [
                tuple(map(int, match.groups()))
                for match in re.finditer(
                    r"flowd tool .* active=(\d) root_pid=(\d+) scope_only=(\d)", text
                )
            ]
            scope_starts = sum(active == 1 and scope == 1 for active, _, scope in records)
            root_starts = sum(
                active == 1 and root_pid > 0 and scope == 0
                for active, root_pid, scope in records
            )
            endings = sum(active == 0 and root_pid == 0 and scope == 0 for active, root_pid, scope in records)
            require(scope_starts == command_count, "工具执行范围开始次数与命令数不一致")
            require(root_starts == command_count, "工具根进程开始次数与命令数不一致")
            require(endings == command_count, "工具结束次数与命令数不一致")
            require("flowd closed session=" in text, "C 控制程序没有关闭会话")

    case_root = path.parent / "cases"
    require(not any(case_root.glob("*/work")), "成功结果仍保留可重建工作目录")
    return problems, {
        "workflow": name,
        "commands": command_count,
        "allowed_failure_commands": int(case.get("allowed_failure_commands") or 0),
        "return_promotions": int(stats.get("return_promotions") or 0),
        "automatic_weight_activations": int(stats.get("adaptive_weight_activations") or 0),
        "automatic_weight_deactivations": int(stats.get("adaptive_weight_deactivations") or 0),
        "automatic_weight_timeouts": int(stats.get("automatic_weight_timeouts") or 0),
        "automatic_weight_condition_releases": int(stats.get("automatic_weight_condition_releases") or 0),
        "automatic_weight_pressure_waits": int(stats.get("automatic_weight_pressure_waits") or 0),
        "automatic_weight_handoffs": int(stats.get("automatic_weight_handoffs") or 0),
        "automatic_weight_owner_change_releases": int(stats.get("automatic_weight_owner_change_releases") or 0),
        "automatic_weight_max_active_ns": int(stats.get("automatic_weight_max_active_ns") or 0),
        "automatic_weight_service_releases": int(stats.get("automatic_weight_service_releases") or 0),
        "automatic_weight_planned_ns": int(stats.get("automatic_weight_planned_ns") or 0),
        "automatic_weight_service_ns": int(stats.get("automatic_weight_service_ns") or 0),
        "result": str(path.resolve().relative_to(ROOT)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="严格核对 FlowCausal v2 的 50 个工作流")
    parser.add_argument("--base", required=True, type=pathlib.Path)
    parser.add_argument("--override", action="append", default=[], type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()

    selected = collect([args.base, *args.override])
    expected = {
        load(path)["cases"][0]["instance_id"]
        for path in sorted(MANIFESTS.glob("*-tool.json"))
    }
    missing = sorted(expected - selected.keys())
    extra = sorted(selected.keys() - expected)
    if missing or extra:
        raise SystemExit(f"任务集合不一致：missing={missing}, extra={extra}")

    rows: list[dict[str, int | str]] = []
    failures: dict[str, list[str]] = {}
    for name in sorted(expected):
        problems, row = check_one(selected[name])
        rows.append(row)
        if problems:
            failures[name] = problems

    coverage = {
        "workflows_with_return_promotions": sum(int(row["return_promotions"]) > 0 for row in rows),
        "workflows_with_automatic_weight": sum(int(row["automatic_weight_activations"]) > 0 for row in rows),
        "return_promotions": sum(int(row["return_promotions"]) for row in rows),
        "automatic_weight_activations": sum(int(row["automatic_weight_activations"]) for row in rows),
        "automatic_weight_deactivations": sum(int(row["automatic_weight_deactivations"]) for row in rows),
        "automatic_weight_pressure_waits": sum(int(row["automatic_weight_pressure_waits"]) for row in rows),
        "automatic_weight_timeouts": sum(int(row["automatic_weight_timeouts"]) for row in rows),
        "automatic_weight_condition_releases": sum(int(row["automatic_weight_condition_releases"]) for row in rows),
        "automatic_weight_handoffs": sum(int(row["automatic_weight_handoffs"]) for row in rows),
        "automatic_weight_owner_change_releases": sum(int(row["automatic_weight_owner_change_releases"]) for row in rows),
        "automatic_weight_max_active_ns": max(int(row["automatic_weight_max_active_ns"]) for row in rows),
        "automatic_weight_service_releases": sum(int(row["automatic_weight_service_releases"]) for row in rows),
        "automatic_weight_planned_ns": sum(int(row["automatic_weight_planned_ns"]) for row in rows),
        "automatic_weight_service_ns": sum(int(row["automatic_weight_service_ns"]) for row in rows),
    }
    payload = {
        "schema": "flowcausal.v2.all-workflows-audit.v1",
        "status": "pass" if not failures else "fail",
        "expected_workflows": len(expected),
        "passed_workflows": len(expected) - len(failures),
        "failed_workflows": failures,
        "commands": sum(int(row["commands"]) for row in rows),
        "allowed_failure_commands": sum(int(row["allowed_failure_commands"]) for row in rows),
        "all_recorded_returncodes_match": not failures,
        "all_kernel_captures_complete": not failures,
        "dropped_kernel_events": 0 if not failures else None,
        "mechanism_coverage": coverage,
        "workflows": rows,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "完整核对.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    lines = [
        "# FlowCausal v2 全部工作流完整核对",
        "",
        f"- 状态：`{payload['status']}`；",
        f"- 通过：{payload['passed_workflows']}/{payload['expected_workflows']}；",
        f"- 工具命令：{payload['commands']}；",
        f"- 已记录返回状态是否全部一致：{'是' if payload['all_recorded_returncodes_match'] else '否'}；",
        f"- 内核事件丢失：{payload['dropped_kernel_events']}；",
        f"- 触发等待关系处理的工作流：{coverage['workflows_with_return_promotions']}/{len(expected)}；",
        f"- 触发自动权重的工作流：{coverage['workflows_with_automatic_weight']}/{len(expected)}。",
        "",
    ]
    if failures:
        lines.extend(["## 未通过项目", ""])
        for name, problems in failures.items():
            lines.append(f"- `{name}`：{'；'.join(problems)}")
        lines.append("")
    (args.output / "完整核对.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({key: payload[key] for key in ("status", "passed_workflows", "expected_workflows", "commands", "mechanism_coverage")}, ensure_ascii=False))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

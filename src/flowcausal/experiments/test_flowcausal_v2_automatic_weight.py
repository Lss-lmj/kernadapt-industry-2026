#!/usr/bin/env python3
"""核对自动权重只依赖运行时等待与排队，不依赖工具标签。"""

from __future__ import annotations

import argparse
import json
import pathlib
import tempfile

from test_flowcausal_v2_dynamic_weight import run_once
from test_flowcausal_v2_lifecycle import SCX_STATE


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 V2 自动工作流权重")
    parser.add_argument("--output", type=pathlib.Path)
    parser.add_argument("--cpu-operations", type=int, default=4000)
    args = parser.parse_args()
    if args.cpu_operations < 1:
        raise SystemExit("工作量必须为正数")
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")

    output = args.output or pathlib.Path(
        tempfile.mkdtemp(prefix="flowcausal-v2-automatic-weight-")
    )
    result = run_once(output, mode="automatic", operations=args.cpu_operations)
    stats = list(result["session_stats"])
    activations = [int(item["adaptive_weight_activations"]) for item in stats]
    deactivations = [int(item["adaptive_weight_deactivations"]) for item in stats]
    peaks = [int(item["adaptive_weight_peak"]) for item in stats]
    pressure_waits = [int(item["automatic_weight_pressure_waits"]) for item in stats]
    max_active_ns = [int(item["automatic_weight_max_active_ns"]) for item in stats]
    timeouts = [int(item["automatic_weight_timeouts"]) for item in stats]
    condition_releases = [
        int(item["automatic_weight_condition_releases"]) for item in stats
    ]
    handoffs = [int(item["automatic_weight_handoffs"]) for item in stats]
    owner_change_releases = [
        int(item["automatic_weight_owner_change_releases"]) for item in stats
    ]
    service_releases = [
        int(item["automatic_weight_service_releases"]) for item in stats
    ]
    planned_ns = [int(item["automatic_weight_planned_ns"]) for item in stats]
    service_ns = [int(item["automatic_weight_service_ns"]) for item in stats]
    checks = {
        "四个未标记为同步的工具均正常结束": not any(result["returncodes"]),
        "至少一个会话依据实际等待和排队自动获得额外权重": sum(activations) > 0,
        "自动选择不要求预先指定某个会话": sum(value > 0 for value in activations) >= 2,
        "额外权重在等待结束后全部归还": sum(activations) == sum(deactivations),
        "最高权重符合一次提高一倍的规则": max(peaks, default=0) == 200,
        "每次真实阻塞等待最多提高一次权重": all(
            activation <= pressure
            for activation, pressure in zip(activations, pressure_waits)
        ),
        "最长一次提高权重没有超过允许的调度误差": max(
            max_active_ns, default=0
        ) <= 200_000_000,
        "额外权重具有按实际处理器时间停止的路径": sum(service_releases) > 0,
        "计划补偿量和实际取得的处理器时间均被记录": (
            sum(planned_ns) > 0 and sum(service_ns) > 0
        ),
        "每次恢复均记录了明确原因": (
            sum(service_releases) + sum(timeouts) + sum(condition_releases)
            + sum(owner_change_releases)
            == sum(deactivations)
        ),
    }
    payload = {
        "schema": "flowcausal.v2.automatic-weight-functional-check.v1",
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "adaptive_weight_activations": activations,
        "adaptive_weight_deactivations": deactivations,
        "adaptive_weight_peaks": peaks,
        "automatic_weight_pressure_waits": pressure_waits,
        "automatic_weight_max_active_ns": max_active_ns,
        "automatic_weight_timeouts": timeouts,
        "automatic_weight_condition_releases": condition_releases,
        "automatic_weight_handoffs": handoffs,
        "automatic_weight_owner_change_releases": owner_change_releases,
        "automatic_weight_service_releases": service_releases,
        "automatic_weight_planned_ns": planned_ns,
        "automatic_weight_service_ns": service_ns,
        "result": result,
    }
    (output / "自动权重功能核对.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({key: payload[key] for key in (
        "status", "checks", "adaptive_weight_activations",
        "adaptive_weight_deactivations", "adaptive_weight_peaks",
        "automatic_weight_pressure_waits", "automatic_weight_max_active_ns",
        "automatic_weight_timeouts", "automatic_weight_condition_releases",
        "automatic_weight_handoffs",
        "automatic_weight_owner_change_releases",
        "automatic_weight_service_releases", "automatic_weight_planned_ns",
        "automatic_weight_service_ns",
    )}, ensure_ascii=False))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

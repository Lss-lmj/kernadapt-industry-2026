#!/usr/bin/env python3
"""逐一运行 HarnessReplayBench 1.0 的全部单工作流并持续写出汇总。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import statistics
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BENCHMARK = ROOT / "benchmarks" / "harness-replaybench"
ADAPTER = BENCHMARK / "adapters" / "run_scheduler.py"
MANIFESTS = BENCHMARK / "manifests" / "v1" / "single"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"不是 JSON 对象：{path}")
    return value


def write_json(path: pathlib.Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def scheduler_command(args: argparse.Namespace, manifest: pathlib.Path,
                      output: pathlib.Path) -> list[str]:
    command = [
        sys.executable,
        str(ADAPTER),
        "--scheduler", args.method,
        "--manifest", str(manifest),
        "--output", str(output),
        "--ready-timeout", str(args.ready_timeout),
    ]
    if args.binary:
        command.extend(["--binary", str(pathlib.Path(args.binary).resolve())])
    if args.tool_controller:
        command.extend(
            ["--tool-controller", str(pathlib.Path(args.tool_controller).resolve())]
        )
    if args.drop_caches:
        command.append("--drop-caches")
    if not args.kernel_capture:
        command.append("--no-kernel-capture")
    if args.keep_kernel_capture:
        command.append("--keep-kernel-capture")
    if args.scheduler_arg:
        command.append("--")
        command.extend(args.scheduler_arg)
    return command


def run_scheduler(command: list[str]) -> tuple[int, bool]:
    """运行一个任务；中断时清理适配器及其调度器、容器子进程。"""
    process = subprocess.Popen(command, start_new_session=True)
    try:
        return process.wait(), False
    except KeyboardInterrupt:
        for requested_signal, timeout in (
            (signal.SIGINT, 10),
            (signal.SIGTERM, 5),
            (signal.SIGKILL, 2),
        ):
            if process.poll() is not None:
                break
            try:
                os.killpg(process.pid, requested_signal)
            except ProcessLookupError:
                break
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                continue
        return 130, True


def summarize(output_root: pathlib.Path, method: str,
              manifests: list[pathlib.Path], started_at: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    expected = {path.stem for path in manifests}
    for directory in sorted(output_root.iterdir() if output_root.exists() else []):
        result_path = directory / "result.json"
        if directory.name not in expected or not result_path.is_file():
            continue
        result = read_json(result_path)
        cases = result.get("cases") if isinstance(result.get("cases"), list) else []
        case = cases[0] if cases and isinstance(cases[0], dict) else {}
        aggregate = result.get("aggregate") if isinstance(result.get("aggregate"), dict) else {}
        control = result.get("flowcausal_control")
        stats: dict[str, Any] = {}
        if isinstance(control, dict):
            sessions = control.get("sessions")
            if isinstance(sessions, list) and sessions and isinstance(sessions[0], dict):
                candidate = sessions[0].get("stats")
                if isinstance(candidate, dict):
                    stats = candidate
        rows.append(
            {
                "workflow": str(case.get("instance_id") or directory.name),
                "suite_id": str(result.get("suite_id") or directory.name),
                "status": str(result.get("status") or "unknown"),
                "elapsed_sec": case.get("elapsed_sec"),
                "workflow_completion_sec": case.get("workflow_completion_sec"),
                "synchronous_tool_p95_sec": (
                    case.get("tool_service", {}).get("synchronous_p95_sec")
                    if isinstance(case.get("tool_service"), dict) else None
                ),
                "critical_sec": case.get("critical_sec"),
                "cpu_runtime_sec": (
                    float(result.get("kernel_capture", {}).get("runtime_ns") or 0)
                    / 1_000_000_000
                    if isinstance(result.get("kernel_capture"), dict) else None
                ),
                "runnable_wait_sec": (
                    float(result.get("kernel_capture", {}).get("runnable_wait_ns") or 0)
                    / 1_000_000_000
                    if isinstance(result.get("kernel_capture"), dict) else None
                ),
                "return_promotions": int(stats.get("return_promotions") or 0),
                "interactive_runs": int(stats.get("interactive_runs") or 0),
                "stale_delays": int(stats.get("stale_delays") or 0),
                "locality_hits": int(stats.get("locality_hits") or 0),
                "locality_fallbacks": int(stats.get("locality_fallbacks") or 0),
                "parallel_width_limits": int(stats.get("parallel_width_limits") or 0),
                "automatic_weight_activations": int(
                    stats.get("adaptive_weight_activations") or 0
                ),
                "automatic_weight_deactivations": int(
                    stats.get("adaptive_weight_deactivations") or 0
                ),
                "automatic_weight_pressure_waits": int(
                    stats.get("automatic_weight_pressure_waits") or 0
                ),
                "automatic_weight_timeouts": int(
                    stats.get("automatic_weight_timeouts") or 0
                ),
                "automatic_weight_condition_releases": int(
                    stats.get("automatic_weight_condition_releases") or 0
                ),
                "automatic_weight_handoffs": int(
                    stats.get("automatic_weight_handoffs") or 0
                ),
                "automatic_weight_owner_change_releases": int(
                    stats.get("automatic_weight_owner_change_releases") or 0
                ),
                "automatic_weight_max_active_ns": int(
                    stats.get("automatic_weight_max_active_ns") or 0
                ),
                "automatic_weight_service_releases": int(
                    stats.get("automatic_weight_service_releases") or 0
                ),
                "automatic_weight_planned_ns": int(
                    stats.get("automatic_weight_planned_ns") or 0
                ),
                "automatic_weight_service_ns": int(
                    stats.get("automatic_weight_service_ns") or 0
                ),
                "result": str(result_path.relative_to(output_root)),
            }
        )
    elapsed_values = [
        float(row["elapsed_sec"]) for row in rows
        if isinstance(row.get("elapsed_sec"), (int, float))
    ]
    return {
        "schema": "flowcausal.v2.all-workflows.v1",
        "method": method,
        "expected_workflows": len(manifests),
        "completed_workflows": len(rows),
        "passed_workflows": sum(row["status"] == "pass" for row in rows),
        "failed_workflows": [row["workflow"] for row in rows if row["status"] != "pass"],
        "elapsed_wall_sec": time.monotonic() - started_at,
        "workflow_elapsed_sec": {
            "sum": sum(elapsed_values),
            "median": statistics.median(elapsed_values) if elapsed_values else None,
            "maximum": max(elapsed_values) if elapsed_values else None,
        },
        "mechanism_coverage": {
            "workflows_with_return_promotions": sum(
                int(row["return_promotions"]) > 0 for row in rows
            ),
            "workflows_with_automatic_weight": sum(
                int(row["automatic_weight_activations"]) > 0 for row in rows
            ),
            "return_promotions": sum(int(row["return_promotions"]) for row in rows),
            "automatic_weight_activations": sum(
                int(row["automatic_weight_activations"]) for row in rows
            ),
            "automatic_weight_deactivations": sum(
                int(row["automatic_weight_deactivations"]) for row in rows
            ),
            "automatic_weight_pressure_waits": sum(
                int(row["automatic_weight_pressure_waits"]) for row in rows
            ),
            "automatic_weight_timeouts": sum(
                int(row["automatic_weight_timeouts"]) for row in rows
            ),
            "automatic_weight_condition_releases": sum(
                int(row["automatic_weight_condition_releases"]) for row in rows
            ),
            "automatic_weight_handoffs": sum(
                int(row["automatic_weight_handoffs"]) for row in rows
            ),
            "automatic_weight_owner_change_releases": sum(
                int(row["automatic_weight_owner_change_releases"])
                for row in rows
            ),
            "automatic_weight_max_active_ns": max(
                (int(row["automatic_weight_max_active_ns"]) for row in rows),
                default=0,
            ),
            "automatic_weight_service_releases": sum(
                int(row["automatic_weight_service_releases"]) for row in rows
            ),
            "automatic_weight_planned_ns": sum(
                int(row["automatic_weight_planned_ns"]) for row in rows
            ),
            "automatic_weight_service_ns": sum(
                int(row["automatic_weight_service_ns"]) for row in rows
            ),
        },
        "workflows": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="逐一验证 FlowCausal v2 的全部工作流")
    parser.add_argument("--method", required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--binary")
    parser.add_argument("--tool-controller")
    parser.add_argument("--scheduler-arg", action="append", default=[])
    parser.add_argument("--ready-timeout", type=float, default=120.0)
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument(
        "--kernel-capture",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="记录 C/eBPF 进程与调度事件；逐工作流分析默认开启",
    )
    parser.add_argument(
        "--keep-kernel-capture",
        action="store_true",
        help="成功后仍保留逐任务内核记录，用于重新选择并发组合",
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        help="只运行指定来源任务，可重复使用",
    )
    parser.add_argument("--start-at", type=int, default=0)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    manifests = sorted(MANIFESTS.glob("*-tool.json"))
    if len(manifests) != 50:
        raise SystemExit(f"预期 50 个单工作流，实际 {len(manifests)} 个")
    if args.workflow:
        wanted = set(args.workflow)
        manifests = [
            path for path in manifests
            if any(name in path.stem for name in wanted)
        ]
        missing = [name for name in args.workflow if not any(name in path.stem for path in manifests)]
        if missing:
            raise SystemExit(f"找不到指定工作流：{missing}")
    selected = manifests[args.start_at:]
    if args.limit is not None:
        selected = selected[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    failures = 0
    interrupted = False
    display_total = len(manifests)
    for index, manifest in enumerate(selected, start=args.start_at + 1):
        output = args.output / manifest.stem
        result_path = output / "result.json"
        if result_path.is_file() and read_json(result_path).get("status") == "pass":
            print(f"[{index:02d}/{display_total}] 已有合格结果：{manifest.stem}", flush=True)
            continue
        if output.exists() and any(output.iterdir()):
            print(f"[{index:02d}/{display_total}] 结果目录不为空，保留现场并跳过：{output}", flush=True)
            failures += 1
            continue
        print(f"[{index:02d}/{display_total}] 开始：{manifest.stem}", flush=True)
        returncode, interrupted = run_scheduler(
            scheduler_command(args, manifest, output)
        )
        if returncode:
            failures += 1
            print(f"[{index:02d}/{display_total}] 失败，退出状态 {returncode}", flush=True)
        else:
            print(f"[{index:02d}/{display_total}] 完成", flush=True)
        write_json(
            args.output / "结果汇总.json",
            summarize(args.output, args.method, manifests, started_at),
        )
        if interrupted:
            print("收到中断信号，已清理当前任务及其子进程。", flush=True)
            break
    summary = summarize(args.output, args.method, manifests, started_at)
    write_json(args.output / "结果汇总.json", summary)
    print(json.dumps({key: summary[key] for key in (
        "expected_workflows", "completed_workflows", "passed_workflows",
        "failed_workflows", "elapsed_wall_sec",
    )}, ensure_ascii=False), flush=True)
    return 130 if interrupted else (1 if failures else 0)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""在同一组合上交错运行关键调度基线，并维护一份结果表。"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import pathlib
import random
import shutil
import statistics
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters" / "run_scheduler.py"
RUN_SUITE = ROOT / "runners" / "run_suite.py"
REPLAY_CASE = ROOT / "runners" / "replay_case.py"


def environment_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve immutable images and stable host settings before reusing results."""
    sys.path.insert(0, str(ROOT / 'runners'))
    from run_suite import image_record, prepare_image, system_record
    names = sorted({case['container_image'] for case in payload['cases'] if case.get('container_image')})
    images = []
    for name in names:
        prepare_image(name)
        images.append(image_record(name))
    host = system_record(str(payload.get('cpu_set') or ''))
    fields = ('hostname', 'kernel', 'machine', 'cpu_model', 'logical_cpus', 'cpu_set',
              'online_cpus', 'selected_cpu_topology', 'scaling_governors',
              'intel_pstate_no_turbo', 'smt_active', 'numa_nodes')
    return dict(host={key:host.get(key) for key in fields}, images=images)


@dataclass(frozen=True)
class Method:
    name: str
    binary: str


def parse_method(text: str) -> Method:
    name, separator, binary = text.partition("=")
    if not name.strip():
        raise argparse.ArgumentTypeError("方法名称不能为空")
    if not separator and name != "linux":
        raise argparse.ArgumentTypeError("非 Linux 方法使用 名称=可执行文件")
    return Method(name.strip(), binary.strip())


def parse_method_arg(text: str) -> tuple[str, str]:
    name, separator, value = text.partition("=")
    if not separator or not name.strip() or not value.strip():
        raise argparse.ArgumentTypeError("方法参数使用 方法名称=参数")
    return name.strip(), value.strip()


def is_flowcausal_v2(method: Method) -> bool:
    """识别第二版调度器，不依赖实验表中采用的显示名称。"""
    return (
        "flowcausal_v2" in method.name
        or pathlib.Path(method.binary).name.startswith("scx_flowcausal_v2")
    )


def option_value(arguments: list[str], option: str) -> str | None:
    """读取只允许出现一次、且使用独立参数值的调度器选项。"""
    found: list[str] = []
    for index, argument in enumerate(arguments):
        if argument != option:
            continue
        if index + 1 >= len(arguments):
            raise ValueError(f"{option} 缺少参数值")
        found.append(arguments[index + 1])
    if len(found) > 1:
        raise ValueError(f"{option} 重复出现")
    return found[0] if found else None


def validate_managed_cpus(
    manifest_payload: dict[str, Any],
    methods: list[Method],
    method_args: dict[str, list[str]],
) -> None:
    """确保第二版调度器和任务使用完全相同的连续处理器范围。"""
    task_cpus = str(manifest_payload.get("cpu_set") or "").strip()
    if not task_cpus:
        raise ValueError("套件清单缺少 cpu_set")
    for method in methods:
        if not is_flowcausal_v2(method):
            continue
        configured = option_value(method_args[method.name], "--managed-cpus")
        scheduler_cpus = configured or "0-7"
        if scheduler_cpus != task_cpus:
            raise ValueError(
                f"{method.name} 管理的处理器范围为 {scheduler_cpus}，"
                f"但任务使用 {task_cpus}；请传入 "
                f"--method-arg {method.name}=--managed-cpus "
                f"--method-arg {method.name}={task_cpus}"
            )


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) * percent + 99) // 100) - 1))
    return ordered[index]


def read_result(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def package_path(text: str) -> pathlib.Path:
    path = pathlib.Path(text)
    return path.resolve() if path.is_absolute() else (ROOT / path).resolve()


def update_file_digest(digest: Any, label: str, path: pathlib.Path) -> None:
    """把文件名和内容同时纳入一次实验的输入摘要。"""
    digest.update(label.encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    digest.update(b"\0")


def manifest_input_digest(
    manifest: pathlib.Path, payload: dict[str, Any]
) -> str:
    """计算套件清单以及它直接引用的数据文件的内容摘要。"""
    digest = hashlib.blake2b(digest_size=32)
    update_file_digest(digest, "manifest", manifest)
    referenced: set[pathlib.Path] = set()
    for field in ("release_profile_file", "task_set_file"):
        value = payload.get(field)
        if isinstance(value, str) and value:
            referenced.add(package_path(value))
    for case in payload.get("cases", []):
        if not isinstance(case, dict):
            continue
        for field in ("trace_file", "execution_relations_file"):
            value = case.get(field)
            if isinstance(value, str) and value:
                referenced.add(package_path(value))
    for path in sorted(referenced, key=lambda item: str(item)):
        if not path.is_file():
            raise SystemExit(f"套件引用的输入文件不存在：{path}")
        update_file_digest(digest, str(path), path)
    return digest.hexdigest()


def executable_digest(path_text: str) -> str:
    if not path_text:
        return "linux"
    path = pathlib.Path(path_text).resolve()
    if not path.is_file():
        raise SystemExit(f"调度器或控制程序不存在：{path}")
    digest = hashlib.blake2b(digest_size=32)
    update_file_digest(digest, path.name, path)
    return digest.hexdigest()


def method_input_identity(
    manifest_digest: str,
    method: Method,
    arguments: list[str],
    uses_flowd: bool,
    flowd_binary: str | None,
    flowd_arguments: list[str],
    drop_caches: bool,
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runner_digest = hashlib.blake2b(digest_size=32)
    runtime_scripts = sorted(path for path in (ROOT / 'runners').iterdir()
                             if path.is_file() and path.suffix in {'.py', '.sh'})
    for path in (ADAPTER, *runtime_scripts):
        update_file_digest(runner_digest, path.name, path)
    capture = {}
    for name in ('hrb_capture', 'hrb_mark'):
        path = ROOT / 'capture' / 'build' / name
        capture[name] = executable_digest(str(path)) if path.is_file() else 'not-built'
    record = {
        "schema": "harness_replaybench.input_identity.v2",
        "manifest_digest": manifest_digest,
        "method": method.name,
        "scheduler_digest": executable_digest(method.binary),
        "scheduler_arguments": list(arguments),
        "tool_controller_digest": (
            executable_digest(flowd_binary or "") if uses_flowd else "unused"
        ),
        "tool_controller_arguments": list(flowd_arguments) if uses_flowd else [],
        "drop_caches": bool(drop_caches),
        "runner_digest": runner_digest.hexdigest(),
        'capture_binaries': capture,
        'environment': environment,
    }
    encoded = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    record["digest"] = hashlib.blake2b(encoded, digest_size=32).hexdigest()
    return record


def trial_number(path: pathlib.Path) -> int | None:
    """从 trial-NN 目录读取原始轮次编号。"""
    name = path.parent.name
    prefix = "trial-"
    if not name.startswith(prefix):
        return None
    try:
        return int(name[len(prefix) :])
    except ValueError:
        return None


def passing_result_paths(method_root: pathlib.Path) -> list[pathlib.Path]:
    """按原始轮次返回已有的成功结果，失败现场不参与统计。"""
    found: list[tuple[int, pathlib.Path]] = []
    for path in method_root.glob("trial-*/result.json"):
        number = trial_number(path)
        result = read_result(path)
        if number is None or not result or result.get("status") != "pass":
            continue
        found.append((number, path))
    return [path for _, path in sorted(found)]


def compatible_result_paths(
    method_root: pathlib.Path,
    expected_identity: dict[str, Any],
    suite_id: str,
    allow_legacy: bool,
) -> list[pathlib.Path]:
    """只返回与当前输入一致的成功结果。"""
    found: list[pathlib.Path] = []
    for path in passing_result_paths(method_root):
        result = read_result(path)
        if not result:
            continue
        identity = result.get("input_identity")
        if isinstance(identity, dict):
            if identity.get("digest") == expected_identity.get("digest"):
                found.append(path)
            continue
        if allow_legacy and str(result.get("suite_id") or "") == suite_id:
            found.append(path)
    return found


def next_trial_number(method_root: pathlib.Path) -> int:
    numbers: list[int] = []
    for trial_dir in method_root.glob("trial-*"):
        try:
            numbers.append(int(trial_dir.name.removeprefix("trial-")))
        except ValueError:
            continue
    return max(numbers, default=0) + 1


def mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def stdev(values: list[float]) -> float | None:
    return statistics.stdev(values) if len(values) > 1 else (0.0 if values else None)


def lower_is_better(reference: float | None, value: float | None) -> float | None:
    if reference is None or value is None or reference <= 0:
        return None
    return (reference - value) / reference * 100.0


def higher_is_better(reference: float | None, value: float | None) -> float | None:
    if reference is None or value is None or reference <= 0:
        return None
    return (value - reference) / reference * 100.0


def display_number(value: Any, digits: int = 3) -> str:
    return "" if value is None else f"{float(value):.{digits}f}"


def display_percent(value: Any, digits: int = 1) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}%"


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 HarnessReplayBench 关键基线")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--method", action="append", type=parse_method, default=[])
    parser.add_argument(
        "--method-arg",
        action="append",
        type=parse_method_arg,
        default=[],
        help="只传给指定调度器的参数，格式为 方法名称=参数，可重复使用",
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--seed",
        type=int,
        default=20260911,
        help="随机安排各轮方法顺序时使用的种子",
    )
    parser.add_argument(
        "--reference-method",
        help="计算相对变化时使用的方法；省略时优先使用 scx_simple，否则使用首个方法",
    )
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--existing-results",
        action="append",
        type=parse_method_arg,
        default=[],
        help="复用已有方法结果，格式为 方法名称=结果根目录；不会重复运行",
    )
    parser.add_argument(
        "--reuse-legacy-existing",
        action="store_true",
        help="显式复用没有输入内容摘要、但套件编号一致的历史结果",
    )
    parser.add_argument(
        "--keep-kernel-capture-method",
        action="append",
        default=[],
        help="保留逐任务 C/eBPF 原始记录的方法名称，可重复使用",
    )
    parser.add_argument("--tool-controller", "--flowd-v2", dest="flowd_v2", help="可选的会话工具事件控制程序")
    parser.add_argument(
        "--tool-controller-method", "--flowd-method",
        dest="flowd_method",
        action="append",
        default=[],
        help="需要启动 C 控制程序的方法名称，可重复使用",
    )
    parser.add_argument(
        "--tool-controller-arg",
        dest="flowd_v2_args",
        action="append",
        default=[],
        help="传给 C 工具事件控制程序的参数，可重复使用",
    )
    parser.add_argument(
        "--measure-controller-resources",
        action="store_true",
        help="记录 C 控制进程的处理器时间和峰值内存",
    )
    parser.add_argument(
        "--measure-bpf-runtime",
        action="store_true",
        help="记录调度器 BPF 程序的累计执行时间和调用次数",
    )
    args = parser.parse_args()
    if args.trials < 1:
        raise SystemExit("--trials 必须大于零")
    methods: list[Method] = args.method or [Method("linux", "")]
    if len({item.name for item in methods}) != len(methods):
        raise SystemExit("方法名称重复")
    method_names = {item.name for item in methods}
    reference_method = args.reference_method or (
        "scx_simple" if "scx_simple" in method_names else methods[0].name
    )
    if reference_method not in method_names:
        raise SystemExit(f"参考方法不存在：{reference_method}")
    method_args: dict[str, list[str]] = {name: [] for name in method_names}
    for name, value in args.method_arg:
        if name not in method_names:
            raise SystemExit(f"方法参数对应的方法不存在：{name}")
        method_args[name].append(value)
    flowd_methods = set(args.flowd_method)
    if flowd_methods and not args.flowd_v2:
        raise SystemExit("指定 --flowd-method 时必须同时指定 --flowd-v2")
    unknown_flowd_methods = flowd_methods - {item.name for item in methods}
    if unknown_flowd_methods:
        raise SystemExit(f"C 控制程序对应的方法不存在：{sorted(unknown_flowd_methods)}")
    keep_capture_methods = set(args.keep_kernel_capture_method)
    unknown_capture_methods = keep_capture_methods - {item.name for item in methods}
    if unknown_capture_methods:
        raise SystemExit(f"需要保留内核记录的方法不存在：{sorted(unknown_capture_methods)}")
    manifest = pathlib.Path(args.manifest).resolve()
    manifest_payload = read_result(manifest)
    if not manifest_payload:
        raise SystemExit(f"无法读取套件清单：{manifest}")
    try:
        validate_managed_cpus(manifest_payload, methods, method_args)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    suite_id = str(manifest_payload.get("suite_id") or "")
    benchmark_layer = str(manifest_payload.get("benchmark_layer") or "")
    if benchmark_layer not in {
        "mechanism-validation",
        "real-tool-composition",
        "general-workloads",
    }:
        raise SystemExit(f"套件缺少有效实验层次：{manifest}")
    output_root = pathlib.Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_digest = manifest_input_digest(manifest, manifest_payload)
    environment = environment_identity(manifest_payload)
    input_identities = {
        method.name: method_input_identity(
            manifest_digest,
            method,
            method_args[method.name],
            method.name in flowd_methods,
            args.flowd_v2,
            args.flowd_v2_args,
            args.drop_caches,
            {**environment, 'measurement': {
                'bpf_runtime': args.measure_bpf_runtime,
                'controller_resources': args.measure_controller_resources,
                'keep_kernel_capture': method.name in keep_capture_methods,
            }},
        )
        for method in methods
    }
    existing_roots: dict[str, pathlib.Path] = {}
    for name, value in args.existing_results:
        if name not in method_names:
            raise SystemExit(f"已有结果对应的方法不存在：{name}")
        root = pathlib.Path(value).resolve()
        compatible = compatible_result_paths(
            root,
            input_identities[name],
            suite_id,
            allow_legacy=args.reuse_legacy_existing,
        )
        if not compatible:
            detail = "；如确认历史输入一致，可显式使用 --reuse-legacy-existing"
            raise SystemExit(f"已有结果中没有输入一致的成功轮次：{name}={root}{detail}")
        existing_roots[name] = root
    failures: list[str] = []
    executed_runs: list[str] = []
    reused_runs: list[str] = []
    passing_by_method: dict[str, list[pathlib.Path]] = {}
    for method in methods:
        expected_identity = input_identities[method.name]
        paths = [] if args.force else compatible_result_paths(
            existing_roots.get(method.name, output_root / method.name),
            expected_identity,
            suite_id,
            allow_legacy=(
                args.reuse_legacy_existing and method.name in existing_roots
            ),
        )
        if not args.force and method.name in existing_roots:
            paths.extend(
                compatible_result_paths(
                    output_root / method.name,
                    expected_identity,
                    suite_id,
                    allow_legacy=False,
                )
            )
        passing_by_method[method.name] = paths

    rng = random.Random(args.seed)
    trial_orders: list[list[str]] = []
    for trial in range(args.trials):
        order = list(methods)
        rng.shuffle(order)
        trial_orders.append([method.name for method in order])
        for method in order:
            if not args.force and trial < len(passing_by_method[method.name]):
                path = passing_by_method[method.name][trial]
                print(
                    f"跳过已完成结果：{method.name} {path.parent.name}",
                    flush=True,
                )
                reused_runs.append(f"{method.name}/{path.parent.name}")
                continue
            trial_id = trial + 1 if args.force else next_trial_number(
                output_root / method.name
            )
            output = output_root / method.name / f"trial-{trial_id:02d}"
            old = read_result(output / "result.json")
            if old and old.get("status") == "pass" and not args.force:
                print(f"跳过已完成结果：{method.name} 第 {trial_id} 轮", flush=True)
                reused_runs.append(f"{method.name}/trial-{trial_id:02d}")
                continue
            if output.exists() and any(output.iterdir()):
                if not args.force:
                    failures.append(
                        f"{method.name}/trial-{trial_id:02d}: 目录已有失败现场"
                    )
                    continue
                shutil.rmtree(output)
            command = [
                sys.executable,
                str(ADAPTER),
                "--scheduler", method.name,
                "--manifest", str(manifest),
                "--output", str(output),
            ]
            if method.binary:
                command.extend(["--binary", str(pathlib.Path(method.binary).resolve())])
            if method.name in flowd_methods:
                command.extend(["--flowd-v2", str(pathlib.Path(args.flowd_v2).resolve())])
                command.extend(f"--tool-controller-arg={value}" for value in args.flowd_v2_args)
                if args.measure_controller_resources:
                    command.append("--measure-controller-resources")
            if args.measure_bpf_runtime:
                command.append("--measure-bpf-runtime")
            if args.drop_caches:
                command.append("--drop-caches")
            if method.name in keep_capture_methods:
                command.append("--keep-kernel-capture")
            if method_args[method.name]:
                command.append("--")
                command.extend(method_args[method.name])
            print(f"运行 {method.name} 第 {trial_id} 轮", flush=True)
            executed_runs.append(f"{method.name}/trial-{trial_id:02d}")
            try:
                completed = subprocess.run(command, cwd=ROOT, check=False)
            except KeyboardInterrupt:
                print("收到中止信号，停止后续轮次", file=sys.stderr, flush=True)
                return 130
            if completed.returncode != 0:
                failures.append(
                    f"{method.name}/trial-{trial_id:02d}: 退出状态 {completed.returncode}"
                )
            else:
                result_path = output / "result.json"
                result = read_result(result_path)
                if not result or result.get("status") != "pass":
                    failures.append(
                        f"{method.name}/trial-{trial_id:02d}: 缺少成功结果"
                    )
                    continue
                result["input_identity"] = input_identities[method.name]
                result_path.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
                    + "\n",
                    encoding="utf-8",
                )
                passing_by_method[method.name].append(output / "result.json")

    rows: list[dict[str, Any]] = []
    summary: list[dict[str, Any]] = []
    for method in methods:
        method_results: list[dict[str, Any]] = []
        for path in passing_by_method[method.name][: args.trials]:
            result = read_result(path)
            if not result or result.get("status") != "pass":
                continue
            trial = trial_number(path)
            if trial is None:
                continue
            aggregate = result.get("aggregate") if isinstance(result.get("aggregate"), dict) else {}
            row = {
                "suite_id": suite_id,
                "benchmark_layer": benchmark_layer,
                "method": method.name,
                "trial": trial,
                "makespan_sec": float(aggregate.get("makespan_sec") or 0),
                "mean_elapsed_sec": float(aggregate.get("mean_elapsed_sec") or 0),
                "p95_elapsed_sec": float(aggregate.get("p95_elapsed_sec") or 0),
                "mean_workflow_completion_sec": float(aggregate.get("mean_workflow_completion_sec") or 0),
                "p95_workflow_completion_sec": float(aggregate.get("p95_workflow_completion_sec") or 0),
                "mean_selected_tool_milestone_sec": float(aggregate.get("mean_selected_tool_milestone_sec") or 0),
                "p95_selected_tool_milestone_sec": float(aggregate.get("p95_selected_tool_milestone_sec") or 0),
                "mean_synchronous_tool_service_sec": float(aggregate.get("mean_synchronous_tool_service_sec") or 0),
                "p95_synchronous_tool_service_sec": float(aggregate.get("p95_synchronous_tool_service_sec") or 0),
                "mean_critical_sec": float(aggregate.get("mean_critical_sec") or 0),
                "p95_critical_sec": float(aggregate.get("p95_critical_sec") or 0),
                "causal_window_count": int(aggregate.get("causal_window_count") or 0),
                "mean_causal_window_sec": float(aggregate.get("mean_causal_window_sec") or 0),
                "p95_causal_window_sec": float(aggregate.get("p95_causal_window_sec") or 0),
                "join_window_count": int(aggregate.get("join_window_count") or 0),
                "mean_join_window_sec": float(aggregate.get("mean_join_window_sec") or 0),
                "p95_join_window_sec": float(aggregate.get("p95_join_window_sec") or 0),
                "sessions_per_sec": float(aggregate.get("sessions_per_sec") or 0),
                "runtime_sec": float(result.get("kernel_capture", {}).get("runtime_ns") or 0) / 1_000_000_000,
                "runnable_wait_sec": float(result.get("kernel_capture", {}).get("runnable_wait_ns") or 0) / 1_000_000_000,
                "switches": int(result.get("kernel_capture", {}).get("switches") or 0),
                "migrations": int(result.get("kernel_capture", {}).get("migrations") or 0),
                "dropped_events": int(result.get("kernel_capture", {}).get("dropped_events") or 0),
                "result": (
                    path.relative_to(output_root).as_posix()
                    if path.is_relative_to(output_root)
                    else str(path)
                ),
            }
            row["average_busy_cpus"] = (
                row["runtime_sec"] / row["makespan_sec"]
                if row["makespan_sec"] > 0
                else 0.0
            )
            rows.append(row)
            method_results.append(row)
        if method_results:
            item: dict[str, Any] = {"method": method.name, "completed_trials": len(method_results)}
            for field in (
                "makespan_sec",
                "mean_elapsed_sec",
                "p95_elapsed_sec",
                "mean_workflow_completion_sec",
                "p95_workflow_completion_sec",
                "mean_selected_tool_milestone_sec",
                "p95_selected_tool_milestone_sec",
                "mean_synchronous_tool_service_sec",
                "p95_synchronous_tool_service_sec",
                "mean_critical_sec",
                "p95_critical_sec",
                "mean_causal_window_sec",
                "p95_causal_window_sec",
                "mean_join_window_sec",
                "p95_join_window_sec",
                "sessions_per_sec",
                "runtime_sec",
                "runnable_wait_sec",
                "average_busy_cpus",
                "switches",
                "migrations",
            ):
                values = [float(row[field]) for row in method_results]
                item[f"{field}_mean"] = mean(values)
                item[f"{field}_median"] = median(values)
                item[f"{field}_stdev"] = stdev(values)
                item[f"{field}_p95"] = percentile(values, 95)
            item["dropped_events"] = sum(int(row["dropped_events"]) for row in method_results)
            item["causal_window_count"] = max(
                int(row["causal_window_count"]) for row in method_results
            )
            item["join_window_count"] = max(
                int(row["join_window_count"]) for row in method_results
            )
            summary.append(item)

    reference = next(
        (item for item in summary if item["method"] == reference_method), None
    )
    if reference is not None:
        for item in summary:
            relative = {
                "makespan_reduction": lower_is_better(reference["makespan_sec_mean"], item["makespan_sec_mean"]),
                "mean_elapsed_reduction": lower_is_better(
                    reference["mean_elapsed_sec_mean"], item["mean_elapsed_sec_mean"]
                ),
                "p95_elapsed_reduction": lower_is_better(
                    reference["p95_elapsed_sec_mean"], item["p95_elapsed_sec_mean"]
                ),
                "mean_workflow_completion_reduction": lower_is_better(
                    reference["mean_workflow_completion_sec_mean"],
                    item["mean_workflow_completion_sec_mean"],
                ),
                "p95_workflow_completion_reduction": lower_is_better(
                    reference["p95_workflow_completion_sec_mean"],
                    item["p95_workflow_completion_sec_mean"],
                ),
                "mean_synchronous_tool_service_reduction": lower_is_better(
                    reference["mean_synchronous_tool_service_sec_mean"],
                    item["mean_synchronous_tool_service_sec_mean"],
                ),
                "p95_synchronous_tool_service_reduction": lower_is_better(
                    reference["p95_synchronous_tool_service_sec_mean"],
                    item["p95_synchronous_tool_service_sec_mean"],
                ),
                "mean_critical_reduction": lower_is_better(
                    reference["mean_critical_sec_mean"], item["mean_critical_sec_mean"]
                ),
                "p95_critical_reduction": lower_is_better(
                    reference["p95_critical_sec_mean"], item["p95_critical_sec_mean"]
                ),
                "mean_causal_window_reduction": lower_is_better(
                    reference["mean_causal_window_sec_mean"],
                    item["mean_causal_window_sec_mean"],
                ),
                "p95_causal_window_reduction": lower_is_better(
                    reference["p95_causal_window_sec_mean"],
                    item["p95_causal_window_sec_mean"],
                ),
                "mean_join_window_reduction": lower_is_better(
                    reference["mean_join_window_sec_mean"],
                    item["mean_join_window_sec_mean"],
                ),
                "p95_join_window_reduction": lower_is_better(
                    reference["p95_join_window_sec_mean"],
                    item["p95_join_window_sec_mean"],
                ),
                "throughput_increase": higher_is_better(
                    reference["sessions_per_sec_mean"], item["sessions_per_sec_mean"]
                ),
            }
            item["relative_to_reference_percent"] = relative
            if reference_method == "scx_simple":
                item["relative_to_scx_simple_percent"] = relative
    registry = {
        "schema": "harness_replaybench.reference_results.v1",
        "manifest": str(manifest),
        "suite_id": suite_id,
        "benchmark_layer": benchmark_layer,
        "reference_method": reference_method,
        "trials_requested": args.trials,
        "execution_order": "randomized-method-order-with-completed-results-reused",
        "execution_order_seed": args.seed,
        "trial_method_order": trial_orders,
        "methods": [{"name": item.name, "binary": item.binary} for item in methods],
        "tool_controller": {
            "program": str(pathlib.Path(args.flowd_v2).resolve()) if args.flowd_v2 else "",
            "methods": sorted(flowd_methods),
            "args": list(args.flowd_v2_args),
            "resources_measured": args.measure_controller_resources,
        },
        "scheduler_args": method_args,
        "bpf_runtime_measured": args.measure_bpf_runtime,
        "kept_kernel_capture_methods": sorted(keep_capture_methods),
        "executed_runs": executed_runs,
        "reused_runs": reused_runs,
        "existing_result_roots": {
            name: str(path) for name, path in sorted(existing_roots.items())
        },
        "input_identities": input_identities,
        "legacy_existing_results_allowed": bool(args.reuse_legacy_existing),
        "rows": rows,
        "summary": summary,
        "failures": failures,
    }
    (output_root / "结果表.json").write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    csv_fields = [
        "suite_id",
        "benchmark_layer",
        "method",
        "trial",
        "makespan_sec",
        "mean_elapsed_sec",
        "p95_elapsed_sec",
        "mean_workflow_completion_sec",
        "p95_workflow_completion_sec",
        "mean_selected_tool_milestone_sec",
        "p95_selected_tool_milestone_sec",
        "mean_synchronous_tool_service_sec",
        "p95_synchronous_tool_service_sec",
        "mean_critical_sec",
        "p95_critical_sec",
        "causal_window_count",
        "mean_causal_window_sec",
        "p95_causal_window_sec",
        "join_window_count",
        "mean_join_window_sec",
        "p95_join_window_sec",
        "sessions_per_sec",
        "runtime_sec",
        "runnable_wait_sec",
        "average_busy_cpus",
        "switches",
        "migrations",
        "dropped_events",
        "result",
    ]
    with (output_root / "逐轮结果.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = [
        "# HarnessReplayBench 1.0 参考结果",
        "",
        f"固定组合：`{manifest}`",
        f"实验层次：`{benchmark_layer}`",
        "",
        f"已有的合规结果不重复运行；新增方法按相同输入、处理器范围和缓存条件追加。百分比均相对 `{reference_method}`，正数表示改善。",
        "",
        "完整本地工作流完成时间与单任务耗时相同；旧字段中的“关键时间”只是启发式工具里程碑，以下不再把它作为关键路径结论。",
        "",
        "| 方法 | 完成轮次 | 总完成时间（秒） | 平均工作流完成时间（秒） | 工具阻塞 P95（秒） | 平均使用处理器数 | 总完成时间改善 | 工作流完成时间改善 | 工具阻塞 P95 改善 | 事件丢失 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary:
        relative = item.get("relative_to_reference_percent", {})
        lines.append(
            "| {method} | {trials} | {makespan} | {workflow} | {tool_p95} | {busy_cpus} | {makespan_gain} | {workflow_gain} | {tool_gain} | {dropped} |".format(
                method=item["method"],
                trials=item["completed_trials"],
                makespan=display_number(item["makespan_sec_mean"]),
                workflow=display_number(item["mean_workflow_completion_sec_mean"]),
                tool_p95=display_number(item["p95_synchronous_tool_service_sec_mean"]),
                busy_cpus=display_number(item["average_busy_cpus_mean"], 2),
                makespan_gain=display_percent(relative.get("makespan_reduction")),
                workflow_gain=display_percent(relative.get("mean_workflow_completion_reduction")),
                tool_gain=display_percent(relative.get("p95_synchronous_tool_service_reduction")),
                dropped=item["dropped_events"],
            )
        )
    (output_root / "结果摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps({"results": len(rows), "failures": len(failures)}, ensure_ascii=False))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""逐个运行 HarnessReplayBench 1.0 正式任务，记录可执行性并清理新拉取的成功镜像。"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters" / "run_scheduler.py"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def image_exists(image: str) -> bool:
    return bool(image) and subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def remove_image(image: str) -> None:
    subprocess.run(
        ["docker", "image", "rm", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def archive_failure(output_root: pathlib.Path, output: pathlib.Path, instance: str) -> str:
    root = output_root / "失败记录" / instance
    root.mkdir(parents=True, exist_ok=True)
    number = 1
    while (root / f"第{number:02d}次").exists():
        number += 1
    destination = root / f"第{number:02d}次"
    shutil.move(str(output), destination)
    return destination.relative_to(output_root).as_posix()


def remove_output(output: pathlib.Path, output_root: pathlib.Path) -> None:
    """只清理本轮结果根目录之下的指定任务目录。"""
    output.resolve().relative_to(output_root.resolve())
    try:
        shutil.rmtree(output)
        return
    except PermissionError:
        pass
    result = subprocess.run(
        ["sudo", "-n", "rm", "-rf", "--", str(output.resolve())],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 or output.exists():
        raise RuntimeError(f"无法清理任务结果 {output}：{result.stderr.strip()}")


def timing_fields(manifest: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    case = manifest["cases"][0]
    trace_path = ROOT / str(case["trace_file"])
    trace = read_json(trace_path)
    recorded_ns = sum(
        int(command.get("timing", {}).get("tool_duration_ns") or 0)
        for command in trace.get("commands", [])
        if isinstance(command, dict)
    )
    replay_sec = 0.0
    if isinstance(payload.get("cases"), list) and payload["cases"]:
        replay_sec = float(payload["cases"][0].get("elapsed_sec") or 0)
    recorded_sec = recorded_ns / 1_000_000_000
    ratio = replay_sec / recorded_sec if recorded_sec > 0 else None
    return {
        "recorded_tool_sec": recorded_sec,
        "replay_elapsed_sec": replay_sec,
        "replay_to_recorded_ratio": ratio,
        "timing_outlier": ratio is not None and (ratio > 5.0 or ratio < 0.2),
        "dropped_events": int(payload.get("kernel_capture", {}).get("dropped_events") or 0),
    }


def write_registry(output_root: pathlib.Path, scheduler: str, rows: list[dict[str, Any]]) -> None:
    (output_root / "可执行性记录.json").write_text(
        json.dumps(
            {
                "schema": "harness_replaybench.runnable_validation.v1",
                "scheduler": scheduler,
                "rows": rows,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 HarnessReplayBench 1.0 任务可执行性")
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--scheduler", default="scx_simple")
    parser.add_argument("--binary")
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--instance",
        action="append",
        default=[],
        help="只运行指定任务编号，可重复使用",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--keep-kernel-capture",
        action="store_true",
        help="成功任务仍保留逐任务C/eBPF记录，供执行结构分析使用",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="保留原失败现场并重新运行尚未通过的任务",
    )
    args = parser.parse_args()
    if args.scheduler != "linux" and not args.binary:
        raise SystemExit("非 Linux 方法必须提供 --binary")
    manifests = sorted((ROOT / "manifests" / "v1" / "single").glob("*-tool.json"))
    if args.instance:
        wanted = set(args.instance)
        manifests = [
            path
            for path in manifests
            if str(read_json(path)["cases"][0]["instance_id"]) in wanted
        ]
        found = {str(read_json(path)["cases"][0]["instance_id"]) for path in manifests}
        missing = sorted(wanted - found)
        if missing:
            raise SystemExit(f"没有找到单任务套件：{', '.join(missing)}")
    manifests = manifests[args.start : args.start + args.limit if args.limit else None]
    output_root = pathlib.Path(args.output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    registry_path = output_root / "可执行性记录.json"
    rows: list[dict[str, Any]] = []
    if args.instance and registry_path.is_file():
        previous_registry = read_json(registry_path)
        rows = [
            dict(item)
            for item in previous_registry.get("rows", [])
            if isinstance(item, dict)
        ]

    def record(row: dict[str, Any]) -> None:
        nonlocal rows
        instance_id = str(row.get("instance_id") or "")
        rows = [item for item in rows if str(item.get("instance_id") or "") != instance_id]
        rows.append(row)
        rows.sort(key=lambda item: str(item.get("instance_id") or ""))
        write_registry(output_root, args.scheduler, rows)

    failed = False

    for position, manifest_path in enumerate(manifests, args.start + 1):
        manifest = read_json(manifest_path)
        case = manifest["cases"][0]
        instance = str(case["instance_id"])
        output = output_root / instance
        old = read_json(output / "result.json") if (output / "result.json").is_file() else None
        if old and old.get("status") == "pass" and not args.force:
            record(
                {
                    "instance_id": instance,
                    "status": "pass",
                    "result": f"{instance}/result.json",
                    "reused": True,
                    **timing_fields(manifest, old),
                }
            )
            print(f"[{position}] 跳过已通过任务 {instance}", flush=True)
            continue
        previous_failure = ""
        if output.exists() and any(output.iterdir()):
            if args.force:
                remove_output(output, output_root)
            elif args.retry_failures:
                previous_failure = archive_failure(output_root, output, instance)
            else:
                raise SystemExit(f"已有失败现场，请先处理或使用新的输出目录：{output}")
        image = str(case.get("container_image") or "")
        image_preexisting = image_exists(image)
        command = [
            sys.executable,
            str(ADAPTER),
            "--scheduler", args.scheduler,
            "--manifest", str(manifest_path),
            "--output", str(output),
        ]
        if args.binary:
            command.extend(["--binary", str(pathlib.Path(args.binary).resolve())])
        if args.keep_kernel_capture:
            command.append("--keep-kernel-capture")
        started = time.monotonic()
        print(f"[{position}] 运行 {instance}", flush=True)
        result = subprocess.run(command, cwd=ROOT, check=False)
        payload = read_json(output / "result.json") if (output / "result.json").is_file() else {}
        status = "pass" if result.returncode == 0 and payload.get("status") == "pass" else "fail"
        row = {
            "instance_id": instance,
            "status": status,
            "returncode": result.returncode,
            "elapsed_sec": time.monotonic() - started,
            "commands": int(payload.get("cases", [{}])[0].get("command_count") or 0) if payload.get("cases") else 0,
            "result": f"{instance}/result.json" if payload else "",
            "reused": False,
            "previous_failure_scene": previous_failure,
            **timing_fields(manifest, payload),
        }
        record(row)
        if status == "pass" and image and not image_preexisting:
            remove_image(image)
        if status != "pass":
            failed = True
        print(f"[{position}] {instance}: {status}，{row['elapsed_sec']:.1f} 秒", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

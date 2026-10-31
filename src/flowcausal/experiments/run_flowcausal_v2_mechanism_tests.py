#!/usr/bin/env python3
"""顺序运行 V2 机制测试，只保存结果和失败日志。"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import time


ROOT = pathlib.Path(__file__).resolve().parents[1]
TESTS = (
    ("协议与并发工具", "test-protocol-v2"),
    ("会话内四类处理", "test-roles-v2"),
    ("生命周期与恢复", "test-lifecycle-v2"),
    ("动态并行度", "test-parallel-width-v2"),
    ("映射容量不足", "test-capacity-v2"),
    ("处理器时间上限", "test-quota-v2"),
    ("会话公平与资源边界", "test-resources-v2"),
    ("短交互延迟", "test-latency-v2"),
    ("后台任务让路与恢复", "test-background-v2"),
    ("普通任务公平", "test-normal-v2"),
)


def find_result(stdout: str) -> pathlib.Path | None:
    for line in reversed(stdout.splitlines()):
        raw = line.strip()
        if not raw.startswith("/tmp/"):
            continue
        candidate = pathlib.Path(raw)
        try:
            if not candidate.is_dir():
                continue
        except OSError:
            continue
        for name in ("结果.json", "result.json"):
            result = candidate / name
            if result.is_file():
                return result
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 FlowCausal v2 机制测试")
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    failed = False
    for index, (name, target) in enumerate(TESTS, start=1):
        print(f"[{index:02d}/{len(TESTS)}] {name}", flush=True)
        begin = time.monotonic()
        completed = subprocess.run(
            ["make", "-C", str(ROOT / "src"), target],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        duration = time.monotonic() - begin
        source = find_result(completed.stdout)
        target_dir = args.output / f"{index:02d}-{target}"
        target_dir.mkdir(parents=True, exist_ok=True)
        status = "通过" if completed.returncode == 0 and source is not None else "失败"
        row = {
            "name": name,
            "target": target,
            "status": status,
            "returncode": completed.returncode,
            "duration_sec": duration,
            "source": str(source) if source else None,
        }
        if source is not None:
            shutil.copy2(source, target_dir / "结果.json")
        if status == "失败":
            failed = True
            (target_dir / "运行日志.txt").write_text(completed.stdout, encoding="utf-8")
        rows.append(row)
        (args.output / "结果汇总.json").write_text(
            json.dumps({"tests": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{status}，{duration:.1f} 秒", flush=True)
        if failed:
            break
    summary = {
        "schema": "flowcausal.v2.mechanism-tests.v1",
        "status": "失败" if failed else "通过",
        "test_count": len(rows),
        "passed_count": sum(row["status"] == "通过" for row in rows),
        "tests": rows,
    }
    (args.output / "结果汇总.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# FlowCausal v2 机制测试汇总\n",
        "| 测试 | 结果 | 时间（秒） |",
        "|---|---:|---:|",
        *(
            f"| {row['name']} | {row['status']} | {row['duration_sec']:.1f} |"
            for row in rows
        ),
    ]
    (args.output / "结果摘要.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

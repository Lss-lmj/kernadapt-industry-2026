#!/usr/bin/env python3
"""从已采集的原始文件重新生成 HarnessReplayBench 1.0 轨迹，不再次调用模型。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
from typing import Any

from jsonschema import Draft202012Validator

try:
    from datasets import load_dataset
except ModuleNotFoundError:  # 离线重建时使用数据包内来源任务元数据
    load_dataset = None


ROOT = pathlib.Path(__file__).resolve().parents[3]
BENCHMARK = ROOT / "benchmarks" / "harness-replaybench"
DEFAULT_TASKS = BENCHMARK / "datasets" / "HarnessReplayBench-1.0-真实采集任务.json"
DEFAULT_CAPTURE = ROOT / ".local" / "hrb-v1-capture"
NORMALIZER = ROOT / "experiments" / "normalize_agent_trace.py"
BUILDER = BENCHMARK / "tools" / "build_trace_v2.py"


def run(command: list[str]) -> None:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"命令失败：{' '.join(command[:3])}\n{result.stderr[-2000:]}")


def sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return value.replace(str(ROOT), "<workspace>/project").replace("/home/iscas", "<home>")
    return value


def complete_capture(path: pathlib.Path) -> bool:
    if not path.is_file():
        return False
    last: dict[str, Any] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict) and value.get("record") == "capture_end":
                last = value
    return bool(last) and int(last.get("returncode") or 0) == 0


def selected_rows(task_plan: dict[str, Any], wanted: set[str]) -> dict[str, dict[str, Any]]:
    if load_dataset is not None:
        dataset = load_dataset(
            task_plan.get("source_dataset", "nebius/SWE-rebench"),
            split=task_plan.get("source_split", "test"),
        )
        return {
            str(row["instance_id"]): dict(row)
            for row in dataset
            if str(row.get("instance_id")) in wanted
        }
    rows: dict[str, dict[str, Any]] = {}
    for instance in wanted:
        path = BENCHMARK / "corpus" / "agentcgroup-overlap" / f"{instance}.json"
        if not path.is_file():
            raise RuntimeError(f"缺少离线来源任务元数据：{path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["repo"] = value.get("repository", "")
        value["image_name"] = value.get("container_image", "")
        rows[instance] = value
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="重新生成 HarnessReplayBench 1.0 轨迹")
    parser.add_argument("--task-list", default=str(DEFAULT_TASKS))
    parser.add_argument("--capture-root", default=str(DEFAULT_CAPTURE))
    parser.add_argument("--model", default=os.environ.get("FLOWCAUSAL_MINI_MODEL", "openai/DeepSeek-V4-Flash-0731"))
    args = parser.parse_args()

    task_plan = json.loads(pathlib.Path(args.task_list).read_text(encoding="utf-8"))
    tasks = [item for item in task_plan.get("tasks", []) if isinstance(item, dict)]
    wanted = {str(item["instance_id"]) for item in tasks}
    rows = selected_rows(task_plan, wanted)
    capture_root = pathlib.Path(args.capture_root).resolve()
    state_path = capture_root / "采集状态.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else {}
    completed = {
        str(item.get("instance_id"))
        for item in state.get("tasks", [])
        if isinstance(item, dict) and item.get("status") == "pass"
    }
    schema = json.loads((BENCHMARK / "schemas" / "trace-v2.schema.json").read_text(encoding="utf-8"))
    built: list[str] = []
    skipped: list[str] = []
    for task in tasks:
        instance = str(task["instance_id"])
        row = rows.get(instance)
        old = next(
            (item for item in state.get("tasks", []) if isinstance(item, dict) and item.get("instance_id") == instance),
            {"instance_id": instance},
        )
        run_dir = capture_root / "runs" / instance
        raw = run_dir / f"{instance}.traj"
        capture = run_dir / f"{instance}.capture.jsonl"
        if row is None or not raw.is_file() or not complete_capture(capture):
            skipped.append(instance)
            continue
        rebuild_dir = capture_root / "rebuild" / instance
        rebuild_dir.mkdir(parents=True, exist_ok=True)
        normalized = rebuild_dir / f"{instance}.normalized-v1.json"
        problem = rebuild_dir / f"{instance}.problem.txt"
        problem.write_text(str(row.get("problem_statement") or ""), encoding="utf-8")
        run(
            [
                os.sys.executable,
                str(NORMALIZER),
                "--input", str(raw),
                "--output", str(normalized),
                "--format", "mini-swe-agent",
                "--source", "hrb-v1-real-capture",
                "--instance-id", instance,
                "--agent-repo-root", "/testbed",
            ]
        )
        output = BENCHMARK / "traces" / "formal" / f"{instance}.mini-swe-agent.hrb-trace-v2.json"
        image = str(row.get("image_name") or task.get("container_image") or "")
        run(
            [
                os.sys.executable,
                str(BUILDER),
                "--raw", str(raw),
                "--normalized", str(normalized),
                "--capture", str(capture),
                "--output", str(output),
                "--instance-id", instance,
                "--framework", "mini-swe-agent",
                "--repository", str(row.get("repo") or task.get("repository") or ""),
                "--base-commit", str(row.get("base_commit") or ""),
                "--problem-file", str(problem),
                "--dataset", str(task_plan.get("source_dataset") or "nebius/SWE-rebench"),
                "--split", str(task_plan.get("source_split") or "test"),
                "--model", args.model,
                "--container-image", image,
                "--container-repo-dir", "/testbed",
                "--capture-scheduler", str(old.get("capture_scheduler") or "unspecified"),
                "--capture-cpu-set", str(old.get("cpu_set") or ""),
            ]
        )
        value = json.loads(output.read_text(encoding="utf-8"))
        Draft202012Validator(schema).validate(value)
        source_dir = BENCHMARK / "source-artifacts" / "formal"
        source_dir.mkdir(parents=True, exist_ok=True)
        source_trace = source_dir / f"{instance}.traj"
        source_capture = source_dir / f"{instance}.capture.jsonl"
        source_trace.write_text(
            json.dumps(sanitize_value(json.loads(raw.read_text(encoding="utf-8"))), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        shutil.copy2(capture, source_capture)
        normalized.unlink(missing_ok=True)
        problem.unlink(missing_ok=True)
        old.update(
            {
                "status": "pass",
                "returncode": 0,
                "commands": len(value.get("commands", [])),
                "tasks": int(value.get("session_summary", {}).get("tasks") or 0),
                "dropped_events": int(value.get("capture", {}).get("dropped_events") or 0),
                "final_trace": str(output.relative_to(ROOT)),
                "source_trace": str(source_trace.relative_to(ROOT)),
                "source_capture": str(source_capture.relative_to(ROOT)),
            }
        )
        if old not in state.get("tasks", []):
            state.setdefault("tasks", []).append(old)
        completed.add(instance)
        built.append(instance)
    if state_path.is_file():
        state["summary"] = {
            "completed": sum(item.get("status") == "pass" for item in state.get("tasks", [])),
            "failed": sum(item.get("status") != "pass" for item in state.get("tasks", [])),
            "planned": len(tasks),
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"built": built, "skipped": skipped}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

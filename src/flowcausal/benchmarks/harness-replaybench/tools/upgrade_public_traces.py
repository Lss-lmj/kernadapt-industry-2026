#!/usr/bin/env python3
"""把公开智能体命令轨迹升级为 HarnessReplayBench 1.0 格式。"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
from typing import Any

from jsonschema import Draft202012Validator

try:
    from datasets import load_dataset
except ModuleNotFoundError:  # 离线重建时可复用数据包内已经保存的来源任务元数据
    load_dataset = None


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = ROOT.parents[1]
BUILDER = ROOT / "tools" / "build_trace_v2.py"
DEFAULT_SELECTION = ROOT / "datasets" / "HarnessReplayBench-1.0-公开轨迹任务.json"


def find_v1(instance: str) -> pathlib.Path:
    matches = [
        path
        for path in (ROOT / "traces" / "upstream").glob(f"{instance}.*.json")
        if json.loads(path.read_text(encoding="utf-8")).get("schema") == "harness_replaybench.trace.v1"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{instance} 的旧轨迹数量不是一：{len(matches)}")
    return matches[0]


def raw_path(trace: dict[str, Any]) -> pathlib.Path:
    provenance = trace.get("provenance", {})
    text = str(provenance.get("source_artifact") or provenance.get("input_file") or "")
    path = ROOT / text
    if not path.is_file():
        raise RuntimeError(f"找不到来源记录：{text}")
    return path


def selected_rows(selection: dict[str, Any], wanted: set[str]) -> dict[str, dict[str, Any]]:
    if load_dataset is not None:
        return {
            str(row["instance_id"]): dict(row)
            for row in load_dataset(selection["source_dataset"], split=selection["source_split"])
            if str(row.get("instance_id")) in wanted
        }
    rows: dict[str, dict[str, Any]] = {}
    for instance in wanted:
        path = ROOT / "corpus" / "openhands" / f"{instance}.json"
        if not path.is_file():
            raise RuntimeError(f"缺少离线来源任务元数据：{path}")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["repo"] = value.get("repository", "")
        value["image_name"] = value.get("container_image", "")
        rows[instance] = value
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="升级 HarnessReplayBench 公开轨迹")
    parser.add_argument("--selection", default=str(DEFAULT_SELECTION))
    parser.add_argument(
        "--instance",
        action="append",
        default=[],
        help="只重建指定任务，可重复使用；省略时重建清单中的全部任务",
    )
    args = parser.parse_args()
    selection = json.loads(pathlib.Path(args.selection).read_text(encoding="utf-8"))
    tasks = [item for item in selection.get("tasks", []) if isinstance(item, dict)]
    if args.instance:
        wanted_instances = set(args.instance)
        tasks = [
            item for item in tasks
            if str(item.get("instance_id") or "") in wanted_instances
        ]
        missing = wanted_instances - {
            str(item.get("instance_id") or "") for item in tasks
        }
        if missing:
            raise RuntimeError(f"公开轨迹清单中没有这些任务：{sorted(missing)}")
    wanted = {str(item["instance_id"]) for item in tasks}
    rows = selected_rows(selection, wanted)
    trace_schema = json.loads((ROOT / "schemas" / "trace-v2.schema.json").read_text(encoding="utf-8"))
    task_schema = json.loads((ROOT / "schemas" / "source-task.schema.json").read_text(encoding="utf-8"))
    work = PROJECT / ".local" / "hrb-v1-public-upgrade"
    outputs: list[str] = []
    for task in tasks:
        instance = str(task["instance_id"])
        row = rows.get(instance)
        if row is None or not row.get("image_name"):
            raise RuntimeError(f"{instance} 没有可恢复容器")
        old_path = find_v1(instance)
        old = json.loads(old_path.read_text(encoding="utf-8"))
        raw = raw_path(old)
        problem = work / f"{instance}.problem.txt"
        problem.parent.mkdir(parents=True, exist_ok=True)
        problem.write_text(str(row.get("problem_statement") or ""), encoding="utf-8")
        output = ROOT / "traces" / "formal" / f"{instance}.openhands.hrb-trace-v2.json"
        command = [
            os.sys.executable,
            str(BUILDER),
            "--raw", str(raw),
            "--normalized", str(old_path),
            "--output", str(output),
            "--instance-id", instance,
            "--framework", str(task["framework"]),
            "--repository", str(row.get("repo") or ""),
            "--base-commit", str(row.get("base_commit") or ""),
            "--problem-file", str(problem),
            "--dataset", str(selection["source_dataset"]),
            "--split", str(selection["source_split"]),
            "--model", "public-upstream-trajectory",
            "--container-image", str(row["image_name"]),
            "--container-repo-dir", "/testbed",
            "--replay-repo-subdir", str(task.get("replay_repo_subdir") or ""),
            "--capture-scheduler", "not-recorded",
            "--capture-cpu-set", "",
        ]
        for identifier in task.get("allow_failure_command_ids", []):
            command.extend(["--allow-failure-command", str(identifier)])
        result = subprocess.run(command, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"升级 {instance} 失败：{result.stderr[-2000:]}")
        Draft202012Validator(trace_schema).validate(json.loads(output.read_text(encoding="utf-8")))
        source_task = {
            "schema": "harness_replaybench.source_task.v1",
            "instance_id": instance,
            "repository": str(row.get("repo") or ""),
            "base_commit": str(row.get("base_commit") or ""),
            "dataset": str(selection["source_dataset"]),
            "split": str(selection["source_split"]),
            "category": str(task.get("category") or ""),
            "difficulty": "",
            "problem_statement": str(row.get("problem_statement") or ""),
            "test_patch": str(row.get("test_patch") or ""),
            "container_image": str(row["image_name"]),
            "source_revision": str(old.get("created_at") or "public-dataset"),
        }
        Draft202012Validator(task_schema).validate(source_task)
        task_output = ROOT / "corpus" / "openhands" / f"{instance}.json"
        task_output.parent.mkdir(parents=True, exist_ok=True)
        task_output.write_text(json.dumps(source_task, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        problem.unlink(missing_ok=True)
        outputs.append(output.relative_to(ROOT).as_posix())
    print(json.dumps({"traces": len(outputs), "outputs": outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

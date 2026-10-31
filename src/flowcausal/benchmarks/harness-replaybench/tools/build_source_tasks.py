#!/usr/bin/env python3
"""把公开数据集行整理为 HarnessReplayBench 1.0 来源任务文件。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

from datasets import load_dataset
from jsonschema import Draft202012Validator


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_TASKS = ROOT / "datasets" / "HarnessReplayBench-1.0-真实采集任务.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 HarnessReplayBench 来源任务")
    parser.add_argument("--task-list", default=str(DEFAULT_TASKS))
    parser.add_argument("--output-root", default=str(ROOT / "corpus" / "agentcgroup-overlap"))
    args = parser.parse_args()
    plan = json.loads(pathlib.Path(args.task_list).read_text(encoding="utf-8"))
    tasks = [item for item in plan.get("tasks", []) if isinstance(item, dict)]
    wanted = {str(item["instance_id"]) for item in tasks}
    rows = {
        str(row["instance_id"]): dict(row)
        for row in load_dataset(plan.get("source_dataset", "nebius/SWE-rebench"), split=plan.get("source_split", "test"))
        if str(row.get("instance_id")) in wanted
    }
    schema = json.loads((ROOT / "schemas" / "source-task.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)
    output_root = pathlib.Path(args.output_root).resolve()
    created: list[str] = []
    for task in tasks:
        instance = str(task["instance_id"])
        row = rows.get(instance)
        if row is None:
            raise SystemExit(f"公开数据集中缺少任务：{instance}")
        value: dict[str, Any] = {
            "schema": "harness_replaybench.source_task.v1",
            "instance_id": instance,
            "repository": str(row.get("repo") or task.get("repository") or ""),
            "base_commit": str(row.get("base_commit") or ""),
            "dataset": str(plan.get("source_dataset") or "nebius/SWE-rebench"),
            "split": str(plan.get("source_split") or "test"),
            "category": str(task.get("category") or ""),
            "difficulty": str(task.get("difficulty") or ""),
            "selection_reason": str(task.get("selection_reason") or ""),
            "problem_statement": str(row.get("problem_statement") or ""),
            "test_patch": str(row.get("test_patch") or ""),
            "container_image": str(row.get("image_name") or ""),
            "source_revision": str(plan.get("agentcgroup_source_revision") or ""),
        }
        validator.validate(value)
        output = output_root / f"{instance}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        created.append(output.relative_to(ROOT).as_posix())
    print(json.dumps({"source_tasks": len(created), "files": created}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

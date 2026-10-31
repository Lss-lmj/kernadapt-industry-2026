#!/usr/bin/env python3
"""把公开 SWE-agent、本地 mini-swe-agent 和冻结仓库轨迹升级为 HarnessReplayBench 1.0 格式。"""
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
except ModuleNotFoundError:  # 离线重建时使用数据包内来源任务元数据
    load_dataset = None


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROJECT = ROOT.parents[1]
BUILDER = ROOT / "tools" / "build_trace_v2.py"
DEFAULT_SELECTION = ROOT / "datasets" / "HarnessReplayBench-1.0-跨框架轨迹任务.json"


def read_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"文件不是 JSON 对象：{path}")
    return value


def find_v1(instance: str) -> pathlib.Path:
    matches: list[pathlib.Path] = []
    for path in (ROOT / "traces").rglob(f"{instance}.*.json"):
        value = read_json(path)
        if value.get("schema") == "harness_replaybench.trace.v1":
            matches.append(path)
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


def rows_by_instance(dataset: str, split: str, wanted: set[str]) -> dict[str, dict[str, Any]]:
    if load_dataset is not None:
        return {
            str(row["instance_id"]): dict(row)
            for row in load_dataset(dataset, split=split)
            if str(row.get("instance_id") or "") in wanted
        }
    rows: dict[str, dict[str, Any]] = {}
    for instance in wanted:
        path = ROOT / "corpus" / "cross-framework" / f"{instance}.json"
        if not path.is_file():
            raise RuntimeError(f"缺少离线来源任务元数据：{path}")
        value = read_json(path)
        value["repo"] = value.get("repository", "")
        value["image_name"] = value.get("container_image", "")
        rows[instance] = value
    return rows


def public_model(raw: dict[str, Any]) -> str:
    row = raw.get("row") if isinstance(raw.get("row"), dict) else {}
    return str(row.get("model_name") or "public-upstream-trajectory")


def mini_model(raw: dict[str, Any]) -> str:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    model = config.get("model") if isinstance(config.get("model"), dict) else {}
    return str(model.get("model_name") or "recorded-mini-swe-agent")


def marshmallow_metadata(raw: dict[str, Any]) -> tuple[str, str]:
    replay = raw.get("replay_config") if isinstance(raw.get("replay_config"), dict) else {}
    problem = replay.get("problem_statement") if isinstance(replay.get("problem_statement"), dict) else {}
    agent = replay.get("agent") if isinstance(replay.get("agent"), dict) else {}
    model = agent.get("model") if isinstance(agent.get("model"), dict) else {}
    return str(problem.get("text") or ""), str(model.get("name") or "recorded-swe-agent")


def main() -> int:
    parser = argparse.ArgumentParser(description="升级 HarnessReplayBench 跨框架轨迹")
    parser.add_argument("--selection", default=str(DEFAULT_SELECTION))
    args = parser.parse_args()
    selection = read_json(pathlib.Path(args.selection).resolve())
    tasks = [item for item in selection.get("tasks", []) if isinstance(item, dict)]
    public_ids = {str(item["instance_id"]) for item in tasks if item.get("source_kind") == "swe-agent-dataset"}
    mini_ids = {str(item["instance_id"]) for item in tasks if item.get("source_kind") == "local-mini"}
    rebench = rows_by_instance("nebius/SWE-rebench", "test", public_ids)
    lite = rows_by_instance("princeton-nlp/SWE-bench_Lite", "test", mini_ids)
    trace_schema = read_json(ROOT / "schemas" / "trace-v2.schema.json")
    task_schema = read_json(ROOT / "schemas" / "source-task.schema.json")
    work = PROJECT / ".local" / "hrb-v1-cross-framework-upgrade"
    work.mkdir(parents=True, exist_ok=True)
    outputs: list[str] = []

    for task in tasks:
        instance = str(task["instance_id"])
        source_kind = str(task.get("source_kind") or "")
        if source_kind == "formal-mini-capture":
            output = ROOT / str(task.get("trace") or "")
            raw = ROOT / str(task.get("raw") or "")
            capture = ROOT / str(task.get("capture") or "")
            if not output.is_file() or not raw.is_file() or not capture.is_file():
                raise RuntimeError(f"{instance} 的重新采集文件不完整")
            trace = read_json(output)
            Draft202012Validator(trace_schema).validate(trace)
            if trace.get("capture", {}).get("online_collector") != "C/eBPF":
                raise RuntimeError(f"{instance} 不是 C/eBPF 重新采集轨迹")
            outputs.append(output.relative_to(ROOT).as_posix())
            continue
        old_path = ROOT / str(task["normalized"]) if task.get("normalized") else find_v1(instance)
        old = read_json(old_path)
        raw = ROOT / str(task["raw"]) if task.get("raw") else raw_path(old)
        raw_data = read_json(raw)

        if source_kind == "swe-agent-dataset":
            row = rebench.get(instance)
            if row is None or not row.get("image_name"):
                raise RuntimeError(f"{instance} 没有可恢复容器")
            repository = str(row.get("repo") or "")
            base_commit = str(row.get("base_commit") or "")
            problem_statement = str(row.get("problem_statement") or "")
            test_patch = str(row.get("test_patch") or "")
            image = str(row["image_name"])
            dataset = "nebius/SWE-rebench"
            split = "test"
            model = public_model(raw_data)
            repo_dir = ""
        elif source_kind == "local-mini":
            row = lite.get(instance)
            if row is None:
                raise RuntimeError(f"{instance} 不在 SWE-bench Lite 中")
            repository = str(task.get("repository") or row.get("repo") or "")
            base_commit = str(task.get("base_commit") or row.get("base_commit") or "")
            problem_statement = str(row.get("problem_statement") or "")
            test_patch = str(row.get("test_patch") or "")
            image = str(task.get("container_image") or "")
            dataset = "princeton-nlp/SWE-bench_Lite"
            split = "test"
            model = mini_model(raw_data)
            repo_dir = ""
        elif source_kind == "packaged-repository":
            repository = str(task.get("repository") or "")
            base_commit = str(task.get("base_commit") or "")
            problem_statement, model = marshmallow_metadata(raw_data)
            test_patch = ""
            image = str(task.get("container_image") or "")
            dataset = "SWE-agent published trajectory"
            split = "function-calling"
            repo_dir = str(task.get("repo_dir") or "")
            if not repo_dir or not (ROOT / repo_dir).is_dir():
                raise RuntimeError(f"{instance} 的冻结仓库不存在：{repo_dir}")
        else:
            raise RuntimeError(f"{instance} 的来源类型未知：{source_kind}")

        problem = work / f"{instance}.problem.txt"
        problem.write_text(problem_statement, encoding="utf-8")
        suffix = str(task["framework"]).lower().replace("-", "_")
        output = ROOT / "traces" / "formal" / f"{instance}.{suffix}.hrb-trace-v2.json"
        command = [
            os.sys.executable,
            str(BUILDER),
            "--raw", str(raw),
            "--normalized", str(old_path),
            "--output", str(output),
            "--instance-id", instance,
            "--framework", str(task["framework"]),
            "--repository", repository,
            "--base-commit", base_commit,
            "--problem-file", str(problem),
            "--dataset", dataset,
            "--split", split,
            "--model", model,
            "--container-image", image,
            "--container-repo-dir", "/testbed",
            "--repo-dir", repo_dir,
            "--capture-scheduler", "not-recorded",
            "--capture-cpu-set", "",
        ]
        result = subprocess.run(command, cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(f"升级 {instance} 失败：{result.stderr[-2000:]}")
        trace = read_json(output)
        Draft202012Validator(trace_schema).validate(trace)

        source_task = {
            "schema": "harness_replaybench.source_task.v1",
            "instance_id": instance,
            "repository": repository,
            "base_commit": base_commit,
            "dataset": dataset,
            "split": split,
            "category": str(task.get("category") or ""),
            "difficulty": "",
            "problem_statement": problem_statement,
            "test_patch": test_patch,
            "container_image": image,
            "source_revision": str(old.get("created_at") or raw_data.get("downloaded_at") or "published-trajectory"),
        }
        Draft202012Validator(task_schema).validate(source_task)
        task_output = ROOT / "corpus" / "cross-framework" / f"{instance}.json"
        task_output.parent.mkdir(parents=True, exist_ok=True)
        task_output.write_text(json.dumps(source_task, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        problem.unlink(missing_ok=True)
        outputs.append(output.relative_to(ROOT).as_posix())

    print(json.dumps({"traces": len(outputs), "outputs": outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

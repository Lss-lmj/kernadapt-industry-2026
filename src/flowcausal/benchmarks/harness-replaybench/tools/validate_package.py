#!/usr/bin/env python3
"""Offline validator for the HarnessReplayBench package.

This script performs structural checks only. It does not execute benchmark
workloads, Docker containers, schedulers, or long-running tests.
"""
from __future__ import annotations

import json
import hashlib
import pathlib
import re
import sys
from typing import Any

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover - reported by CLI
    Draft202012Validator = None  # type: ignore[assignment]


BENCHMARK_ROOT = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN_SUBSTRINGS = (
    "/home/" + "iscas/",
    "Flow" + "Causal",
    "flow" + "causal",
    "FLOW" + "CAUSAL",
    "Flow" + "Causal/experiments/",
    "experiments/" + "results/",
    "experiments/" + "harness_replay",
    "flow" + "causal.agent_trace",
    "flow" + "causal.harness",
    "flow" + "causal.repo_stage",
)
EXCLUDED_PACKAGE_TREES = {
    ("derived",),
    ("repos", "matplotlib__matplotlib-23314", ".pytest_cache"),
    ("repos", "matplotlib__matplotlib-23314", "build"),
    ("repos", "matplotlib__matplotlib-23314", "tmp"),
    ("repos", "sympy__sympy-21171", ".pytest_cache"),
}


def is_excluded_package_file(path: pathlib.Path) -> bool:
    parts = path.relative_to(BENCHMARK_ROOT).parts
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    if parts[:2] == ("capture", "build"):
        return True
    if path.suffix == ".pyc":
        return True
    return any(
        parts[: len(prefix)] == prefix for prefix in EXCLUDED_PACKAGE_TREES
    )


def load_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - CLI guard
        raise SystemExit(f"invalid JSON: {path}: {exc}") from exc


def validate_schema(path: pathlib.Path, schema_name: str) -> list[str]:
    if Draft202012Validator is None:
        return ["jsonschema is required to validate the first-release package"]
    schema = load_json(BENCHMARK_ROOT / "schemas" / schema_name)
    value = load_json(path)
    return [
        f"{path.relative_to(BENCHMARK_ROOT)} schema error at {'/'.join(map(str, error.path))}: {error.message}"
        for error in Draft202012Validator(schema).iter_errors(value)
    ]


def walk_strings(obj: Any) -> list[str]:
    out: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            out.append(str(key))
            out.extend(walk_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(walk_strings(item))
    elif isinstance(obj, str):
        out.append(obj)
    return out


def check_no_forbidden_refs(path: pathlib.Path) -> list[str]:
    problems: list[str] = []
    if path.suffix == ".json":
        strings = walk_strings(load_json(path))
        for text in strings:
            for bad in FORBIDDEN_SUBSTRINGS:
                if bad in text:
                    problems.append(f"{path.relative_to(BENCHMARK_ROOT)} contains forbidden reference {bad!r}: {text}")
    elif path.suffix in {".md", ".txt", ".traj"} or path.name in {"VERSION"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for bad in FORBIDDEN_SUBSTRINGS:
            if bad in text:
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} contains forbidden reference {bad!r}")
    return problems


def check_trace(path: pathlib.Path) -> list[str]:
    data = load_json(path)
    problems: list[str] = []
    schema = data.get("schema")
    if schema not in {"harness_replaybench.trace.v1", "harness_replaybench.trace.v2"}:
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong schema: {data.get('schema')!r}")
    commands = data.get("commands")
    if not isinstance(commands, list) or not commands:
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has no commands")
        return problems
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    replay_repo_subdir = str(workspace.get("replay_repo_subdir") or "").strip("/")
    repeated_repo_prefix = (
        re.compile(
            re.escape(f"./{replay_repo_subdir}")
            + r"(?=/|$|[\s'\"`;|&<>()\[\]{}])"
        )
        if replay_repo_subdir
        else None
    )
    for idx, cmd in enumerate(commands):
        if not isinstance(cmd, dict):
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} command {idx} is not an object")
            continue
        for key in ("id", "stage", "cmd", "cwd"):
            if key not in cmd:
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} command {idx} missing {key}")
        if schema == "harness_replaybench.trace.v2":
            normalized = str(cmd.get("normalized_cmd") or "")
            if not normalized.strip():
                problems.append(
                    f"{path.relative_to(BENCHMARK_ROOT)} command {idx} missing normalized_cmd"
                )
            elif repeated_repo_prefix is not None and repeated_repo_prefix.search(normalized):
                problems.append(
                    f"{path.relative_to(BENCHMARK_ROOT)} command {idx} repeats replay repository directory"
                )
            timing = cmd.get("timing")
            if not isinstance(timing, dict):
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} command {idx} missing timing")
            else:
                for key in ("tool_duration_ns",):
                    if not isinstance(timing.get(key), int) or timing.get(key, -1) < 0:
                        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} command {idx} has invalid timing.{key}")
                duration_ns = timing.get("tool_duration_ns")
                if (
                    cmd.get("observed_returncode") == -1
                    and isinstance(duration_ns, int)
                    and duration_ns >= 55_000_000_000
                    and not isinstance(cmd.get("timeout_sec"), (int, float))
                ):
                    problems.append(
                        f"{path.relative_to(BENCHMARK_ROOT)} command {idx} "
                        "was terminated after a long source run but has no replay timeout"
                    )
            if not isinstance(cmd.get("observed_returncode"), int) and not cmd.get("allow_failure"):
                problems.append(
                    f"{path.relative_to(BENCHMARK_ROOT)} command {idx} has no source return code and must remain non-blocking"
                )
    if schema == "harness_replaybench.trace.v2":
        problems.extend(validate_schema(path, "trace-v2.schema.json"))
        replay = data.get("replay")
        if not isinstance(replay, dict) or replay.get("primary_mode") != "tool-execution":
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} must use tool-execution as primary replay")
        modes = replay.get("modes", []) if isinstance(replay, dict) else []
        if modes != ["tool-execution"]:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} must only support tool-execution replay")
    return problems


def check_manifest(path: pathlib.Path) -> list[str]:
    data = load_json(path)
    problems: list[str] = []
    schema = data.get("schema")
    if schema not in {"harness_replaybench.manifest.v1", "harness_replaybench.manifest.v2"}:
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong schema: {data.get('schema')!r}")
    if schema == "harness_replaybench.manifest.v2" and data.get("replay_mode") != "tool-execution":
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has invalid replay_mode")
    if schema == "harness_replaybench.manifest.v2":
        problems.extend(validate_schema(path, "manifest-v2.schema.json"))
    if data.get("path_base") != "benchmark_root":
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} path_base must be benchmark_root")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has no cases")
        return problems
    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} case {idx} is not an object")
            continue
        trace = case.get("trace_file")
        repo = case.get("repo_dir")
        container_repo = case.get("container_repo_dir")
        if not isinstance(trace, str) or not trace:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} case {idx} missing trace_file")
        else:
            trace_path = BENCHMARK_ROOT / trace
            if not trace_path.is_file():
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} case {idx} trace missing: {trace}")
        relations = case.get("execution_relations_file")
        if relations and not (BENCHMARK_ROOT / str(relations)).is_file():
            problems.append(
                f"{path.relative_to(BENCHMARK_ROOT)} case {idx} execution relations missing: {relations}"
            )
        if isinstance(repo, str) and repo:
            repo_path = BENCHMARK_ROOT / repo
            if not repo_path.exists():
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} case {idx} repo missing: {repo}")
        elif not (
            isinstance(case.get("container_image"), str)
            and case.get("container_image")
            and isinstance(container_repo, str)
            and container_repo
        ):
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} case {idx} has no workspace")
    return problems


def check_suite_index() -> list[str]:
    path = BENCHMARK_ROOT / "suite-index.json"
    problems: list[str] = []
    data = load_json(path)
    if data.get("schema") != "harness_replaybench.suite_index.v1":
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong schema: {data.get('schema')!r}")
    suites = data.get("suites")
    if not isinstance(suites, list) or not suites:
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has no suites")
        return problems
    indexed: set[str] = set()
    for idx, suite in enumerate(suites):
        if not isinstance(suite, dict):
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} suite {idx} is not an object")
            continue
        manifest = suite.get("manifest")
        if not isinstance(manifest, str) or not manifest:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} suite {idx} missing manifest")
            continue
        indexed.add(manifest)
        if not (BENCHMARK_ROOT / manifest).is_file():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} suite {idx} manifest missing: {manifest}")
    actual = {
        manifest_path.relative_to(BENCHMARK_ROOT).as_posix()
        for manifest_path in (BENCHMARK_ROOT / "manifests").glob("*.json")
    }
    for manifest in sorted(actual - indexed):
        problems.append(f"suite-index.json does not list manifest: {manifest}")
    return problems


def check_v1_suite_collection() -> list[str]:
    root = BENCHMARK_ROOT / "manifests" / "v1"
    index_path = root / "index.json"
    if not index_path.is_file():
        return ["manifests/v1/index.json is missing"]
    data = load_json(index_path)
    problems: list[str] = []
    if data.get("schema") != "harness_replaybench.v1_suite_collection.v1":
        problems.append(f"manifests/v1/index.json has wrong schema: {data.get('schema')!r}")
    suites = data.get("suites")
    if not isinstance(suites, list) or not suites:
        return [*problems, "manifests/v1/index.json has no suites"]
    indexed = {str(item) for item in suites}
    for rel in sorted(indexed):
        path = BENCHMARK_ROOT / rel
        if not path.is_file():
            problems.append(f"manifests/v1/index.json lists missing manifest: {rel}")
    actual = {
        path.relative_to(BENCHMARK_ROOT).as_posix()
        for path in root.rglob("*.json")
        if path != index_path
    }
    for rel in sorted(actual - indexed):
        problems.append(f"manifests/v1/index.json does not list manifest: {rel}")
    for rel in sorted(indexed - actual):
        problems.append(f"manifests/v1/index.json lists absent manifest: {rel}")
    formal_count = sum(
        load_json(path).get("schema") == "harness_replaybench.trace.v2"
        for path in (BENCHMARK_ROOT / "traces" / "formal").glob("*.json")
    )
    if data.get("trace_count") != formal_count:
        problems.append(
            f"manifests/v1/index.json trace_count is {data.get('trace_count')!r}, expected {formal_count}"
        )
    exclusions_path = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
    excluded = {
        str(item.get("instance_id") or "")
        for item in load_json(exclusions_path).get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    } if exclusions_path.is_file() else set()
    if data.get("included_trace_count") != formal_count - len(excluded):
        problems.append(
            "manifests/v1/index.json included_trace_count does not match formal traces minus exclusions"
        )
    if data.get("excluded_trace_count") != len(excluded):
        problems.append("manifests/v1/index.json excluded_trace_count is inconsistent")
    return problems


def check_dataset_index() -> list[str]:
    path = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-任务索引.json"
    if not path.is_file():
        return ["datasets/HarnessReplayBench-1.0-任务索引.json is missing"]
    data = load_json(path)
    problems: list[str] = []
    if data.get("schema") != "harness_replaybench.dataset_index.v1":
        problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json has wrong schema: {data.get('schema')!r}")
    tasks = data.get("tasks")
    if not isinstance(tasks, list):
        return [*problems, "datasets/HarnessReplayBench-1.0-任务索引.json tasks must be a list"]
    instances: set[str] = set()
    for number, task in enumerate(tasks):
        if not isinstance(task, dict):
            problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json tasks[{number}] is not an object")
            continue
        instance = str(task.get("instance_id") or "")
        if not instance:
            problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json tasks[{number}] has no instance_id")
        elif instance in instances:
            problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json repeats instance_id: {instance}")
        instances.add(instance)
        for field in ("trace_file", "source_task_file"):
            rel = str(task.get(field) or "")
            if not rel or not (BENCHMARK_ROOT / rel).is_file():
                problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json {instance} has missing {field}: {rel}")
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    if summary.get("tasks") != len(tasks):
        problems.append(
            f"datasets/HarnessReplayBench-1.0-任务索引.json summary.tasks is {summary.get('tasks')!r}, expected {len(tasks)}"
        )
    formal_instances = {
        str(load_json(trace).get("instance_id") or "")
        for trace in (BENCHMARK_ROOT / "traces" / "formal").glob("*.json")
        if load_json(trace).get("schema") == "harness_replaybench.trace.v2"
    }
    for instance in sorted(formal_instances - instances):
        problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json does not list formal trace: {instance}")
    for instance in sorted(instances - formal_instances):
        problems.append(f"datasets/HarnessReplayBench-1.0-任务索引.json lists absent formal trace: {instance}")
    return problems


def check_composable_suites() -> list[str]:
    """检查任务集合、启动方式与物化套件之间的一致性。"""
    problems: list[str] = []
    dataset = load_json(BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-任务索引.json")
    known_instances = {
        str(item.get("instance_id") or "")
        for item in dataset.get("tasks", [])
        if isinstance(item, dict)
    }
    exclusions_path = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
    excluded_instances = {
        str(item.get("instance_id") or "")
        for item in load_json(exclusions_path).get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    } if exclusions_path.is_file() else set()
    for instance in sorted(excluded_instances - known_instances):
        problems.append(f"datasets/HarnessReplayBench-1.0-回放排除项.json lists unknown task: {instance}")
    trace_modes: dict[str, set[str]] = {}
    for path in (BENCHMARK_ROOT / "traces" / "formal").glob("*.json"):
        trace = load_json(path)
        trace_modes[str(trace.get("instance_id") or "")] = {
            str(item) for item in trace.get("replay", {}).get("modes", [])
        }

    task_sets: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for path in sorted((BENCHMARK_ROOT / "tasksets" / "v1").glob("*.json")):
        problems.extend(validate_schema(path, "task-set.schema.json"))
        value = load_json(path)
        task_set_id = str(value.get("task_set_id") or "")
        if task_set_id in task_sets:
            problems.append(f"tasksets/v1 repeats task_set_id: {task_set_id}")
        task_sets[task_set_id] = (path, value)
        for instance in value.get("instances", []):
            if instance not in known_instances:
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} lists unknown task: {instance}")
            if instance in excluded_instances:
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} lists excluded task: {instance}")

    profiles: dict[str, tuple[pathlib.Path, dict[str, Any]]] = {}
    for path in sorted((BENCHMARK_ROOT / "release-profiles" / "v1").glob("*.json")):
        problems.extend(validate_schema(path, "release-profile.schema.json"))
        value = load_json(path)
        profile_id = str(value.get("release_profile_id") or "")
        if profile_id in profiles:
            problems.append(f"release-profiles/v1 repeats release_profile_id: {profile_id}")
        profiles[profile_id] = (path, value)

    valid_layers = {"mechanism-validation", "real-tool-composition", "general-workloads"}
    suite_ids: set[str] = set()
    expected_layer_paths: dict[str, set[str]] = {name: set() for name in valid_layers}
    for path in sorted((BENCHMARK_ROOT / "suite-definitions" / "v1").glob("*.json")):
        problems.extend(validate_schema(path, "suite-definition.schema.json"))
        value = load_json(path)
        suite_id = str(value.get("suite_id") or "")
        benchmark_layer = str(value.get("benchmark_layer") or "")
        if benchmark_layer not in valid_layers:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has unknown benchmark_layer: {benchmark_layer!r}")
        if suite_id in suite_ids:
            problems.append(f"suite-definitions/v1 repeats suite_id: {suite_id}")
        suite_ids.add(suite_id)
        task_set_path = BENCHMARK_ROOT / str(value.get("task_set_file") or "")
        profile_path = BENCHMARK_ROOT / str(value.get("release_profile_file") or "")
        if not task_set_path.is_file():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} task set is missing: {value.get('task_set_file')}")
            continue
        if not profile_path.is_file():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} release profile is missing: {value.get('release_profile_file')}")
            continue
        task_set = load_json(task_set_path)
        profile = load_json(profile_path)
        replay_mode = str(value.get("replay_mode") or "")
        instances = [str(item) for item in task_set.get("instances", [])]
        for instance in instances:
            if replay_mode not in trace_modes.get(instance, set()):
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)} task {instance} does not support {replay_mode}")
        materialized_path = BENCHMARK_ROOT / "manifests" / "v1" / "mixed" / f"{suite_id}.json"
        if not materialized_path.is_file():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has no materialized manifest")
            continue
        materialized = load_json(materialized_path)
        actual_instances = [str(item.get("instance_id") or "") for item in materialized.get("cases", [])]
        if actual_instances != instances:
            problems.append(f"{materialized_path.relative_to(BENCHMARK_ROOT)} task order differs from task set")
        expected_metadata = {
            "benchmark_layer": benchmark_layer,
            "task_set_id": task_set.get("task_set_id"),
            "task_set_file": task_set_path.relative_to(BENCHMARK_ROOT).as_posix(),
            "release_profile_id": profile.get("release_profile_id"),
            "release_profile_file": profile_path.relative_to(BENCHMARK_ROOT).as_posix(),
            "release_pattern": profile.get("pattern"),
            "replay_mode": replay_mode,
        }
        for field, expected in expected_metadata.items():
            if materialized.get(field) != expected:
                problems.append(
                    f"{materialized_path.relative_to(BENCHMARK_ROOT)} {field} is "
                    f"{materialized.get(field)!r}, expected {expected!r}"
                )
        if benchmark_layer in expected_layer_paths:
            expected_layer_paths[benchmark_layer].add(
                materialized_path.relative_to(BENCHMARK_ROOT).as_posix()
            )
        offsets = [int(item.get("start_offset_ms") or 0) for item in materialized.get("cases", [])]
        pattern = profile.get("pattern")
        if pattern in {"single", "concurrent"}:
            expected_offsets = [0] * len(instances)
        elif pattern == "fixed-offset":
            expected_offsets = [index * int(profile["offset_ms"]) for index in range(len(instances))]
        else:
            expected_offsets = offsets
        if offsets != expected_offsets:
            problems.append(f"{materialized_path.relative_to(BENCHMARK_ROOT)} has wrong start offsets")

    actual_materialized = {
        path.stem for path in (BENCHMARK_ROOT / "manifests" / "v1" / "mixed").glob("*.json")
    }
    for suite_id in sorted(actual_materialized - suite_ids):
        problems.append(f"manifests/v1/mixed/{suite_id}.json has no suite definition")

    for path in sorted((BENCHMARK_ROOT / "manifests" / "v1" / "single").glob("*.json")):
        problems.extend(validate_schema(path, "manifest-v2.schema.json"))
        value = load_json(path)
        if value.get("benchmark_layer") != "general-workloads":
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} must belong to general-workloads")
        expected_layer_paths["general-workloads"].add(path.relative_to(BENCHMARK_ROOT).as_posix())

    for legacy_path in sorted((BENCHMARK_ROOT / "manifests").glob("*.json")):
        value = load_json(legacy_path)
        benchmark_layer = str(value.get("benchmark_layer") or "")
        if benchmark_layer not in valid_layers:
            problems.append(f"{legacy_path.relative_to(BENCHMARK_ROOT)} has unknown benchmark_layer")
            continue
        expected_layer_paths[benchmark_layer].add(
            legacy_path.relative_to(BENCHMARK_ROOT).as_posix()
        )

    layer_index_path = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-测试层次.json"
    if not layer_index_path.is_file():
        problems.append("datasets/HarnessReplayBench-1.0-测试层次.json is missing")
    else:
        layer_index = load_json(layer_index_path)
        indexed_layers = layer_index.get("layers") if isinstance(layer_index.get("layers"), dict) else {}
        if set(indexed_layers) != valid_layers:
            problems.append("datasets/HarnessReplayBench-1.0-测试层次.json has wrong layer names")
        for layer in sorted(valid_layers):
            actual_paths = {str(item) for item in indexed_layers.get(layer, [])}
            if actual_paths != expected_layer_paths[layer]:
                problems.append(
                    f"datasets/HarnessReplayBench-1.0-测试层次.json {layer} differs from materialized suites"
                )
    return problems


def check_package_manifest() -> list[str]:
    path = BENCHMARK_ROOT / "PACKAGE_MANIFEST.json"
    problems: list[str] = []
    data = load_json(path)
    version = (BENCHMARK_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if data.get("schema") != "harness_replaybench.package_manifest.v1":
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong schema: {data.get('schema')!r}")
    if data.get("version") != version:
        problems.append(
            f"{path.relative_to(BENCHMARK_ROOT)} version is {data.get('version')!r}, expected {version!r}"
        )
    files = data.get("files")
    if not isinstance(files, list):
        problems.append(f"{path.relative_to(BENCHMARK_ROOT)} files must be a list")
        return problems
    indexed: set[str] = set()
    for idx, item in enumerate(files):
        if not isinstance(item, dict):
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} files[{idx}] is not an object")
            continue
        rel = item.get("path")
        if not isinstance(rel, str) or not rel:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} files[{idx}] missing path")
            continue
        indexed.add(rel)
        target = BENCHMARK_ROOT / rel
        if not target.is_file():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} lists missing file: {rel}")
            continue
        if item.get("bytes") != target.stat().st_size:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong byte count for: {rel}")
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if item.get("sha256") != digest.hexdigest():
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong content digest for: {rel}")
    actual = {
        file_path.relative_to(BENCHMARK_ROOT).as_posix()
        for file_path in BENCHMARK_ROOT.rglob("*")
        if file_path.is_file()
        and file_path.name != "PACKAGE_MANIFEST.json"
        and not is_excluded_package_file(file_path)
    }
    for rel in sorted(actual - indexed):
        problems.append(f"PACKAGE_MANIFEST.json does not list file: {rel}")
    for rel in sorted(indexed - actual):
        problems.append(f"PACKAGE_MANIFEST.json lists absent file: {rel}")
    return problems


def check_release_shape() -> list[str]:
    problems: list[str] = []
    version = (BENCHMARK_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if version != "1.0.0":
        problems.append(f"VERSION is {version!r}, expected '1.0.0'")
    dataset = load_json(BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-任务索引.json")
    summary = dataset.get("summary") if isinstance(dataset.get("summary"), dict) else {}
    formal = list((BENCHMARK_ROOT / "traces" / "formal").glob("*.json"))
    annotations = list((BENCHMARK_ROOT / "annotations" / "formal").glob("*.json"))
    execution_relations = list(
        (BENCHMARK_ROOT / "annotations" / "execution-relations").glob("*.json")
    )
    single_tool = list((BENCHMARK_ROOT / "manifests" / "v1" / "single").glob("*-tool.json"))
    mixed = list((BENCHMARK_ROOT / "manifests" / "v1" / "mixed").glob("*.json"))
    definitions = list((BENCHMARK_ROOT / "suite-definitions" / "v1").glob("*.json"))
    formal_traces = [load_json(path) for path in formal]
    expected_tasks = len(formal_traces)
    expected_repositories = len(
        {
            str(trace.get("source_task", {}).get("repository") or "")
            for trace in formal_traces
        }
    )
    expected_commands = sum(len(trace.get("commands", [])) for trace in formal_traces)
    expected_captures = sum(
        trace.get("capture", {}).get("online_collector") == "C/eBPF" for trace in formal_traces
    )
    exclusions_path = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
    excluded_instances = {
        str(item.get("instance_id") or "")
        for item in load_json(exclusions_path).get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    } if exclusions_path.is_file() else set()
    if expected_tasks < 36:
        problems.append(f"formal task count is {expected_tasks}, expected at least 36")
    expected_summary = {
        "tasks": expected_tasks,
        "repositories": expected_repositories,
        "commands": expected_commands,
    }
    for key, value in expected_summary.items():
        if summary.get(key) != value:
            problems.append(f"dataset summary {key} is {summary.get(key)!r}, expected {value}")
    counts = {
        "formal annotations": (len(annotations), expected_tasks),
        "single tool suites": (len(single_tool), expected_tasks - len(excluded_instances)),
        "mixed suites": (len(mixed), len(definitions)),
    }
    for label, (actual, wanted) in counts.items():
        if actual != wanted:
            problems.append(f"{label} count is {actual}, expected {wanted}")
    relation_instances = {
        str(load_json(path).get("instance_id") or "") for path in execution_relations
    }
    formal_instances = {
        str(trace.get("instance_id") or "") for trace in formal_traces
    }
    included_instances = formal_instances - excluded_instances
    for instance in sorted(included_instances - relation_instances):
        problems.append(f"execution relations are missing for included task: {instance}")
    for instance in sorted(relation_instances - formal_instances):
        problems.append(f"execution relations list unknown task: {instance}")
    captures = list((BENCHMARK_ROOT / "source-artifacts" / "formal").glob("*.capture.jsonl"))
    if len(captures) != expected_captures:
        problems.append(f"formal capture count is {len(captures)}, expected {expected_captures}")
    for path in captures:
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        ends = [item for item in rows if item.get("record") == "capture_end"]
        sessions = [item for item in rows if item.get("record") == "session_summary"]
        if len(ends) != 1 or int(ends[0].get("returncode") or 0) != 0:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has invalid capture end")
        if len(sessions) != 1 or int(sessions[0].get("dropped_events") or 0) != 0:
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has incomplete session summary")
    for required in (
        "capture/Makefile",
        "capture/bpf/hrb_capture.bpf.c",
        "capture/user/hrb_capture.c",
        "capture/user/hrb_mark.c",
        "runners/replay_case.py",
        "runners/run_suite.py",
        "runners/launch_replay.sh",
        "adapters/run_scheduler.py",
    ):
        if not (BENCHMARK_ROOT / required).is_file():
            problems.append(f"required release file is missing: {required}")
    return problems


def main() -> int:
    problems: list[str] = []
    for path in BENCHMARK_ROOT.rglob("*"):
        if (
            "repos" in path.relative_to(BENCHMARK_ROOT).parts
            or is_excluded_package_file(path)
        ):
            continue
        if path.is_file():
            problems.extend(check_no_forbidden_refs(path))
    for path in sorted((BENCHMARK_ROOT / "traces").rglob("*.json")):
        problems.extend(check_trace(path))
    for path in sorted((BENCHMARK_ROOT / "manifests").rglob("*.json")):
        if path == BENCHMARK_ROOT / "manifests" / "v1" / "index.json":
            continue
        problems.extend(check_manifest(path))
    for path in sorted((BENCHMARK_ROOT / "corpus").rglob("*.json")):
        problems.extend(validate_schema(path, "source-task.schema.json"))
    for path in sorted((BENCHMARK_ROOT / "annotations" / "formal").glob("*.json")):
        problems.extend(validate_schema(path, "annotation.schema.json"))
    for path in sorted((BENCHMARK_ROOT / "annotations" / "execution-relations").glob("*.json")):
        problems.extend(validate_schema(path, "execution-relations.schema.json"))
    behavior_profiles = BENCHMARK_ROOT / "datasets" / "HarnessReplayBench-1.0-行为画像.json"
    if behavior_profiles.is_file():
        problems.extend(validate_schema(behavior_profiles, "behavior-profiles.schema.json"))
    capture_schema = load_json(BENCHMARK_ROOT / "schemas" / "capture.schema.json")
    capture_validator = Draft202012Validator(capture_schema) if Draft202012Validator is not None else None
    for path in sorted((BENCHMARK_ROOT / "source-artifacts" / "formal").glob("*.capture.jsonl")):
        if capture_validator is None:
            problems.append("jsonschema is required to validate capture records")
            break
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)}:{number} invalid JSON: {exc}")
                continue
            for error in capture_validator.iter_errors(value):
                problems.append(f"{path.relative_to(BENCHMARK_ROOT)}:{number} schema error: {error.message}")
    for path in sorted((BENCHMARK_ROOT / "repo-stage-specs").glob("*.json")):
        data = load_json(path)
        if data.get("schema") != "harness_replaybench.repo_stage_spec.v1":
            problems.append(f"{path.relative_to(BENCHMARK_ROOT)} has wrong schema: {data.get('schema')!r}")
    problems.extend(check_suite_index())
    problems.extend(check_v1_suite_collection())
    problems.extend(check_dataset_index())
    problems.extend(check_composable_suites())
    problems.extend(check_release_shape())
    problems.extend(check_package_manifest())
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        return 1
    print(f"HarnessReplayBench package OK: {BENCHMARK_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

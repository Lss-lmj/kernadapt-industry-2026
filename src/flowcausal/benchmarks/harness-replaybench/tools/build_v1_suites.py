#!/usr/bin/env python3
"""由任务集合、启动方式和重放方式组合生成 HarnessReplayBench 1.0 测试套件。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_SUITE_ROOT = ROOT / "suite-definitions" / "v1"
EXCLUSIONS = ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
LAYER_INDEX = ROOT / "datasets" / "HarnessReplayBench-1.0-测试层次.json"
BENCHMARK_LAYERS = {
    "mechanism-validation": "机制验证",
    "real-tool-composition": "真实工具组合",
    "general-workloads": "通用任务",
}
LEGACY_LAYER_SUITES = {
    "mechanism-validation": [
        "manifests/causal-overlap-3harness-concurrent.json",
        "manifests/repo-stage-3harness-concurrent.json",
        "manifests/repo-stage-3harness.json",
        "manifests/single-cxx-rust-build-heavy.json",
        "manifests/single-package-log-artifact-heavy.json",
        "manifests/single-python-test-straggler.json",
    ],
    "real-tool-composition": [],
    "general-workloads": [
        "manifests/formal-mix-2harness.json",
        "manifests/formal-mix-4harness.json",
        "manifests/formal-mix-8harness-concurrent.json",
        "manifests/formal-mix-8harness-docker-local.json",
        "manifests/formal-mix-8harness.json",
        "manifests/single-JeffLIrion__python-androidtv-351.json",
        "manifests/single-PlasmaFAIR__sdf-xarray-24.json",
        "manifests/single-django__django-14608.json",
        "manifests/single-matplotlib__matplotlib-23314.json",
        "manifests/single-tianocore__edk2-pytool-library-372.json",
    ],
}
CONTAINER_PYTHON = "auto"
PUBLIC_TRACE_PATH = "/opt/conda/envs/testbed/bin:/opt/conda/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def case_from_trace(path: pathlib.Path) -> dict[str, Any]:
    trace = json.loads(path.read_text(encoding="utf-8"))
    source = trace.get("source_task", {})
    workspace = trace.get("workspace", {})
    case: dict[str, Any] = {
        "instance_id": str(trace["instance_id"]),
        "trace_file": path.relative_to(ROOT).as_posix(),
        "start_offset_ms": 0,
        "output_checks": trace.get("replay", {}).get("output_checks", []),
    }
    relations = ROOT / "annotations" / "execution-relations" / f"{trace['instance_id']}.relations.json"
    if relations.is_file():
        case["execution_relations_file"] = relations.relative_to(ROOT).as_posix()
    repair_file = ROOT/'environments/replay-images.json'
    repairs = json.loads(repair_file.read_text()) if repair_file.exists() else {}
    image = str(repairs.get(trace['instance_id'], {}).get('container_image')
                or source.get("container_image") or "")
    container_repo = str(workspace.get("container_repo_dir") or "")
    if image and container_repo:
        command_environment = workspace.get("command_environment", {})
        variables = command_environment.get("env", {}) if isinstance(command_environment, dict) else {}
        if not variables:
            # OpenHands/SWE-agent 公开轨迹没有保存命令环境，但配套
            # SWE-rebench 镜像把任务依赖冻结在 testbed 环境中。只有这类
            # 缺失环境记录的轨迹使用镜像回退；本项目重新采集的轨迹始终
            # 按记录恢复 BASH_ENV、PATH 等实际环境。
            variables = {"PATH": PUBLIC_TRACE_PATH}
        case.update(
            {
                "container_image": image,
                "container_repo_dir": container_repo,
                "container_python": CONTAINER_PYTHON,
                # 这里必须复用真实智能体执行工具时的环境。强制选择
                # testbed Python 会绕过轨迹中由智能体完成的依赖安装，
                # 从而改变进程树、处理器用量和命令耗时。
                "container_env": [f"{key}={value}" for key, value in sorted(variables.items())],
                "docker_user": "root",
            }
        )
    else:
        repo_dir = str(workspace.get("repo_dir") or "")
        if not repo_dir:
            raise SystemExit(f"{path} 没有可运行工作区")
        case["repo_dir"] = repo_dir
    return case


def manifest(
    suite_id: str,
    cases: list[dict[str, Any]],
    *,
    benchmark_layer: str,
    replay_mode: str,
    release_pattern: str,
    description: str,
    task_set_id: str,
    release_profile_id: str,
    task_set_file: str | None = None,
    release_profile_file: str | None = None,
    cpu_set: str = "0-7",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "harness_replaybench.manifest.v2",
        "suite_id": suite_id,
        "description": description,
        "benchmark_layer": benchmark_layer,
        "path_base": "benchmark_root",
        "replay_mode": replay_mode,
        "release_pattern": release_pattern,
        "cpu_set": cpu_set,
        "task_set_id": task_set_id,
        "release_profile_id": release_profile_id,
        "copy_keep_git": True,
        "cleanup_success_artifacts": True,
        "cases": cases,
    }
    if task_set_file:
        value["task_set_file"] = task_set_file
    if release_profile_file:
        value["release_profile_file"] = release_profile_file
    return value


def root_relative(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def release_cases(cases: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    pattern = str(profile["pattern"])
    if pattern == "single" and len(cases) != 1:
        raise SystemExit("single 启动方式只能用于一个任务")
    if pattern in {"single", "concurrent"}:
        return [dict(item, start_offset_ms=0) for item in cases]
    if pattern == "fixed-offset":
        step = int(profile["offset_ms"])
        return [dict(item, start_offset_ms=index * step) for index, item in enumerate(cases)]
    raise SystemExit(f"未知启动方式：{pattern}")


def load_suite_definition(path: pathlib.Path, by_instance: dict[str, dict[str, Any]]) -> dict[str, Any]:
    definition = json.loads(path.read_text(encoding="utf-8"))
    task_set_path = ROOT / str(definition["task_set_file"])
    profile_path = ROOT / str(definition["release_profile_file"])
    task_set = json.loads(task_set_path.read_text(encoding="utf-8"))
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    missing = [instance for instance in task_set["instances"] if instance not in by_instance]
    if missing:
        raise SystemExit(f"{path} 引用了不存在的任务：{', '.join(missing)}")
    selected = [dict(by_instance[instance]) for instance in task_set["instances"]]
    replay_mode = str(definition["replay_mode"])
    for case in selected:
        trace = json.loads((ROOT / str(case["trace_file"])).read_text(encoding="utf-8"))
        if replay_mode not in trace.get("replay", {}).get("modes", []):
            raise SystemExit(f"{path} 的 {case['instance_id']} 不支持 {replay_mode}")
    return manifest(
        str(definition["suite_id"]),
        release_cases(selected, profile),
        benchmark_layer=str(definition["benchmark_layer"]),
        replay_mode=replay_mode,
        release_pattern=str(profile["pattern"]),
        description=str(definition.get("description") or task_set.get("description") or ""),
        task_set_id=str(task_set["task_set_id"]),
        release_profile_id=str(profile["release_profile_id"]),
        task_set_file=root_relative(task_set_path),
        release_profile_file=root_relative(profile_path),
        cpu_set=str(definition.get("cpu_set") or "0-7"),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="组合生成 HarnessReplayBench 1.0 测试套件")
    parser.add_argument("--trace-root", default=str(ROOT / "traces" / "formal"))
    parser.add_argument("--output-root", default=str(ROOT / "manifests" / "v1"))
    parser.add_argument("--suite-root", default=str(DEFAULT_SUITE_ROOT))
    parser.add_argument("--require-count", type=int, default=0)
    args = parser.parse_args()

    trace_paths = sorted(pathlib.Path(args.trace_root).resolve().glob("*.json"))
    all_cases = [case_from_trace(path) for path in trace_paths if json.loads(path.read_text()).get("schema") == "harness_replaybench.trace.v2"]
    if args.require_count and len(all_cases) != args.require_count:
        raise SystemExit(f"HarnessReplayBench 1.0 轨迹为 {len(all_cases)} 条，不等于要求的 {args.require_count} 条")
    exclusion_payload = json.loads(EXCLUSIONS.read_text(encoding="utf-8")) if EXCLUSIONS.is_file() else {"items": []}
    exclusions = {
        str(item.get("instance_id") or "")
        for item in exclusion_payload.get("items", [])
        if isinstance(item, dict) and item.get("instance_id")
    }
    known = {str(item["instance_id"]) for item in all_cases}
    unknown_exclusions = sorted(exclusions - known)
    if unknown_exclusions:
        raise SystemExit(f"回放排除项引用了不存在的任务：{', '.join(unknown_exclusions)}")
    cases = [item for item in all_cases if str(item["instance_id"]) not in exclusions]
    by_instance = {str(item["instance_id"]): item for item in cases}
    output_root = pathlib.Path(args.output_root).resolve()
    for directory in (output_root / "single", output_root / "mixed"):
        if directory.is_dir():
            for old in directory.glob("*.json"):
                old.unlink()
    created: list[pathlib.Path] = []

    for case in cases:
        instance = str(case["instance_id"])
        value = manifest(
            f"hrb1-single-{instance}-tool",
            [dict(case)],
            benchmark_layer="general-workloads",
            replay_mode="tool-execution",
            release_pattern="single",
            description=f"{instance} 的单任务工具执行回放。",
            task_set_id=f"single-{instance}",
            release_profile_id="single",
            release_profile_file="release-profiles/v1/single.json",
        )
        path = output_root / "single" / f"{value['suite_id']}.json"
        write_json(path, value)
        created.append(path)

    for definition_path in sorted(pathlib.Path(args.suite_root).resolve().glob("*.json")):
        value = load_suite_definition(definition_path, by_instance)
        path = output_root / "mixed" / f"{value['suite_id']}.json"
        write_json(path, value)
        created.append(path)

    layers: dict[str, list[str]] = {name: [] for name in BENCHMARK_LAYERS}
    for path in sorted(created):
        value = json.loads(path.read_text(encoding="utf-8"))
        layer = str(value["benchmark_layer"])
        if layer not in layers:
            raise SystemExit(f"{path} 使用未知测试层次：{layer}")
        layers[layer].append(path.relative_to(ROOT).as_posix())
    for layer, relative_paths in LEGACY_LAYER_SUITES.items():
        for relative in relative_paths:
            if not (ROOT / relative).is_file():
                raise SystemExit(f"旧套件不存在：{relative}")
            value = json.loads((ROOT / relative).read_text(encoding="utf-8"))
            if value.get("benchmark_layer") != layer:
                raise SystemExit(f"{relative} 的测试层次与生成器不一致")
            layers[layer].append(relative)

    layer_index = {
        "schema": "harness_replaybench.suite_layers.v1",
        "version": "1.0",
        "descriptions": {
            "mechanism-validation": "隔离验证调度机制是否能改变前序任务与当前同步工具的执行先后。",
            "real-tool-composition": "从真实智能体工具执行记录中按可观测行为选择并组合任务。",
            "general-workloads": "覆盖单任务、不同并发规模、同时启动和错峰启动。",
        },
        "layers": layers,
    }
    write_json(LAYER_INDEX, layer_index)

    index = {
        "schema": "harness_replaybench.v1_suite_collection.v1",
        "version": "1.0",
        "cpu_set": "0-7",
        "trace_count": len(all_cases),
        "included_trace_count": len(cases),
        "excluded_trace_count": len(exclusions),
        "suite_definition_root": root_relative(pathlib.Path(args.suite_root)),
        "layer_index": root_relative(LAYER_INDEX),
        "layers": layers,
        "suites": [path.relative_to(ROOT).as_posix() for path in sorted(created)],
    }
    write_json(output_root / "index.json", index)
    print(json.dumps({"traces": len(all_cases), "included": len(cases), "excluded": len(exclusions), "suites": len(created), "index": str(output_root / "index.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import json
import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LAYERS = {"mechanism-validation", "real-tool-composition", "general-workloads"}


def load(path: pathlib.Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


class SuiteCompositionTests(unittest.TestCase):
    def test_recorded_source_environment_is_preserved(self) -> None:
        source_paths = sorted(
            path
            for path in (ROOT / "source-artifacts").rglob("*")
            if path.is_file()
        )
        by_name: dict[str, pathlib.Path] = {}
        for path in source_paths:
            self.assertNotIn(path.name, by_name, f"来源记录文件名重复：{path.name}")
            by_name[path.name] = path

        checked = 0
        for trace_path in sorted((ROOT / "traces" / "formal").glob("*.json")):
            trace = load(trace_path)
            source_name = str(trace["capture"]["source_trajectory"])
            self.assertIn(source_name, by_name, f"{trace_path}: 找不到来源记录")
            source = load(by_name[source_name])
            source_environment = (
                source.get("info", {}).get("config", {}).get("environment", {})
            )
            recorded: dict[str, object] = {}
            interpreter = source_environment.get("interpreter")
            variables = source_environment.get("env")
            if isinstance(interpreter, list) and interpreter:
                recorded["interpreter"] = interpreter
            if isinstance(variables, dict):
                recorded["env"] = variables
            if not recorded:
                continue
            self.assertEqual(trace["workspace"].get("command_environment"), recorded)
            checked += 1
        self.assertEqual(checked, 30)

    def test_formal_commands_are_relative_to_repository_root(self) -> None:
        traces = sorted((ROOT / "traces" / "formal").glob("*.json"))
        self.assertEqual(len(traces), 50)
        for path in traces:
            trace = load(path)
            subdir = str(trace.get("workspace", {}).get("replay_repo_subdir") or "")
            prefix = re.compile(
                re.escape(f"./{subdir}")
                + r"(?=/|$|[\s'\"`;|&<>()\[\]{}])"
            ) if subdir else None
            for command in trace.get("commands", []):
                normalized = str(command.get("normalized_cmd") or "")
                self.assertTrue(normalized.strip(), f"{path}: 缺少可重放命令")
                if prefix is not None:
                    self.assertIsNone(
                        prefix.search(normalized),
                        f"{path}: 可重放命令重复保留仓库目录",
                    )

    def test_excluded_tasks_never_enter_materialized_suites(self) -> None:
        excluded = {
            str(item["instance_id"])
            for item in load(
                ROOT / "datasets" / "HarnessReplayBench-1.0-回放排除项.json"
            )["items"]
        }
        self.assertEqual(len(excluded), 0)
        for manifest_path in sorted((ROOT / "manifests" / "v1").rglob("*.json")):
            if manifest_path.name == "index.json":
                continue
            manifest = load(manifest_path)
            instances = {
                str(item["instance_id"]) for item in manifest.get("cases", [])
            }
            self.assertFalse(instances & excluded, manifest_path)

        index = load(ROOT / "manifests" / "v1" / "index.json")
        self.assertEqual(index["trace_count"], 50)
        self.assertEqual(index["included_trace_count"], 50)
        self.assertEqual(index["excluded_trace_count"], 0)

    def test_every_definition_has_matching_materialized_manifest(self) -> None:
        definitions = sorted((ROOT / "suite-definitions" / "v1").glob("*.json"))
        self.assertGreater(len(definitions), 0)
        for definition_path in definitions:
            definition = load(definition_path)
            task_set = load(ROOT / str(definition["task_set_file"]))
            release = load(ROOT / str(definition["release_profile_file"]))
            manifest = load(
                ROOT / "manifests" / "v1" / "mixed" / f"{definition['suite_id']}.json"
            )
            self.assertEqual(manifest["task_set_id"], task_set["task_set_id"])
            self.assertEqual(manifest["release_profile_id"], release["release_profile_id"])
            self.assertEqual(manifest["replay_mode"], definition["replay_mode"])
            self.assertIn(definition["benchmark_layer"], LAYERS)
            self.assertEqual(manifest["benchmark_layer"], definition["benchmark_layer"])
            self.assertEqual(
                [item["instance_id"] for item in manifest["cases"]],
                task_set["instances"],
            )

    def test_same_task_set_can_use_multiple_release_profiles(self) -> None:
        concurrent = load(ROOT / "manifests" / "v1" / "mixed" / "hrb1-8-concurrent-tool.json")
        offset = load(ROOT / "manifests" / "v1" / "mixed" / "hrb1-8-offset-tool.json")
        self.assertEqual(concurrent["task_set_id"], offset["task_set_id"])
        self.assertEqual(
            [item["instance_id"] for item in concurrent["cases"]],
            [item["instance_id"] for item in offset["cases"]],
        )
        self.assertEqual([item["start_offset_ms"] for item in concurrent["cases"]], [0] * 8)
        self.assertEqual([item["start_offset_ms"] for item in offset["cases"]], list(range(0, 2000, 250)))

    def test_synchronous_return_suites_are_nested_real_captures(self) -> None:
        previous: list[str] = []
        for size in (4, 8, 12, 16):
            task_set = load(ROOT / "tasksets" / "v1" / f"synchronous-return-{size}.json")
            instances = [str(item) for item in task_set["instances"]]
            self.assertEqual(len(instances), size)
            self.assertEqual(instances[: len(previous)], previous)
            previous = instances
            manifest = load(
                ROOT
                / "manifests"
                / "v1"
                / "mixed"
                / f"hrb1-synchronous-return-{size}-concurrent-tool.json"
            )
            self.assertEqual(manifest["cpu_set"], "0-7")
            self.assertEqual([int(item["start_offset_ms"]) for item in manifest["cases"]], [0] * size)
            for case in manifest["cases"]:
                trace = load(ROOT / str(case["trace_file"]))
                self.assertEqual(trace["capture"]["online_collector"], "C/eBPF")
                self.assertFalse(any(bool(command.get("async")) for command in trace["commands"]))
                source_env = trace["workspace"]["command_environment"]["env"]
                self.assertEqual(case["container_env"], [
                    f"{key}={value}" for key, value in sorted(source_env.items())
                ])
                self.assertIn("BASH_ENV=/root/.bashrc", case["container_env"])

    def test_suite_layers_cover_every_executable_suite_once(self) -> None:
        layer_index = load(ROOT / "datasets" / "HarnessReplayBench-1.0-测试层次.json")
        self.assertEqual(set(layer_index["layers"]), LAYERS)
        grouped = [
            str(path)
            for layer in LAYERS
            for path in layer_index["layers"][layer]
        ]
        self.assertEqual(len(grouped), len(set(grouped)))

        generated = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "manifests" / "v1").rglob("*.json")
            if path.name != "index.json"
        }
        legacy_manifests = {
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / "manifests").glob("*.json")
        }
        legacy = "manifests/causal-overlap-3harness-concurrent.json"
        self.assertEqual(set(grouped), generated | legacy_manifests)
        self.assertIn(legacy, layer_index["layers"]["mechanism-validation"])
        self.assertIn(
            "manifests/v1/mixed/hrb1-scheduling-opportunity-concurrent-tool.json",
            layer_index["layers"]["real-tool-composition"],
        )
        self.assertIn(
            "manifests/v1/mixed/hrb1-8-concurrent-tool.json",
            layer_index["layers"]["general-workloads"],
        )
        for relative in generated:
            manifest = load(ROOT / relative)
            self.assertIn(relative, layer_index["layers"][manifest["benchmark_layer"]])
        for relative in legacy_manifests:
            manifest = load(ROOT / relative)
            self.assertIn(relative, layer_index["layers"][manifest["benchmark_layer"]])

    def test_command_environment_uses_the_correct_source(self) -> None:
        public_manifest = load(
            ROOT
            / "manifests"
            / "v1"
            / "single"
            / "hrb1-single-tianocore__edk2-pytool-library-372-tool.json"
        )
        self.assertEqual(
            public_manifest["cases"][0]["container_env"],
            [
                "PATH=/opt/conda/envs/testbed/bin:/opt/conda/bin:/usr/local/sbin:"
                "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
            ],
        )

        captured_manifest = load(
            ROOT
            / "manifests"
            / "v1"
            / "single"
            / "hrb1-single-dask__dask-4003-tool.json"
        )
        captured_env = captured_manifest["cases"][0]["container_env"]
        self.assertIn("BASH_ENV=/root/.bashrc", captured_env)
        self.assertFalse(any(item.startswith("PATH=") for item in captured_env))

        django_manifest = load(
            ROOT
            / "manifests"
            / "v1"
            / "single"
            / "hrb1-single-django__django-14608-tool.json"
        )
        django_env = django_manifest["cases"][0]["container_env"]
        self.assertIn("BASH_ENV=/root/.bashrc", django_env)
        self.assertFalse(any(item.startswith("PATH=") for item in django_env))


if __name__ == "__main__":
    unittest.main()

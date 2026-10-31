#!/usr/bin/env python3
"""检查参考实验不会复用输入已经变化的旧结果。"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "run_reference_baselines.py"
SPEC = importlib.util.spec_from_file_location("run_reference_baselines", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class ReferenceResultIdentityTest(unittest.TestCase):
    def test_image_and_kernel_changes_invalidate_reuse(self) -> None:
        def identity(image, kernel):
            return MODULE.method_input_identity('manifest', MODULE.Method('linux',''),
                [], False, None, [], True,
                {'images':[{'id':image}], 'host':{'kernel':kernel}})['digest']
        before = identity('sha256:first', 'kernel-a')
        self.assertNotEqual(before, identity('sha256:second', 'kernel-a'))
        self.assertNotEqual(before, identity('sha256:first', 'kernel-b'))
        self.assertEqual(before, identity('sha256:first', 'kernel-a'))

    def write_json(self, path: pathlib.Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def test_trace_change_changes_manifest_digest(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            root = pathlib.Path(text)
            trace = root / "trace.json"
            relation = root / "relation.json"
            manifest = root / "manifest.json"
            self.write_json(trace, {"commands": [{"cmd": "true"}]})
            self.write_json(relation, {"relations": []})
            payload = {
                "cases": [
                    {
                        "trace_file": str(trace),
                        "execution_relations_file": str(relation),
                    }
                ]
            }
            self.write_json(manifest, payload)
            before = MODULE.manifest_input_digest(manifest, payload)
            self.write_json(trace, {"commands": [{"cmd": "printf changed"}]})
            after = MODULE.manifest_input_digest(manifest, payload)
            self.assertNotEqual(before, after)

    def test_result_reuse_requires_matching_identity(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            root = pathlib.Path(text)
            result_path = root / "trial-01" / "result.json"
            expected = {"digest": "current"}
            self.write_json(
                result_path,
                {
                    "status": "pass",
                    "suite_id": "suite-a",
                    "input_identity": {"digest": "old"},
                },
            )
            self.assertEqual(
                MODULE.compatible_result_paths(root, expected, "suite-a", False),
                [],
            )
            self.write_json(
                result_path,
                {
                    "status": "pass",
                    "suite_id": "suite-a",
                    "input_identity": expected,
                },
            )
            self.assertEqual(
                MODULE.compatible_result_paths(root, expected, "suite-a", False),
                [result_path],
            )

    def test_legacy_result_needs_explicit_permission_and_same_suite(self) -> None:
        with tempfile.TemporaryDirectory() as text:
            root = pathlib.Path(text)
            result_path = root / "trial-01" / "result.json"
            self.write_json(result_path, {"status": "pass", "suite_id": "suite-a"})
            self.assertEqual(
                MODULE.compatible_result_paths(
                    root, {"digest": "current"}, "suite-a", False
                ),
                [],
            )
            self.assertEqual(
                MODULE.compatible_result_paths(
                    root, {"digest": "current"}, "suite-b", True
                ),
                [],
            )
            self.assertEqual(
                MODULE.compatible_result_paths(
                    root, {"digest": "current"}, "suite-a", True
                ),
                [result_path],
            )


if __name__ == "__main__":
    unittest.main()

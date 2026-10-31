#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_trace_v2", ROOT / "tools" / "build_trace_v2.py"
)
assert SPEC is not None and SPEC.loader is not None
BUILD_TRACE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_TRACE)


class TraceStabilizationTests(unittest.TestCase):
    def test_cfdm_keeps_all_suites_but_uses_stable_unittest_order(self) -> None:
        for identifier in (
            "trajectory[14].tool_calls[0]",
            "trajectory[18].tool_calls[0]",
        ):
            original = (
                "cd /workspace/NCAS-CMS__cfdm__1.8/cfdm/test "
                "&& python run_tests.py --help"
            )
            command = {
                "id": identifier,
                "cmd": original,
                "normalized_cmd": (
                    "cd ./cfdm/test && ${HRB_REPLAY_PYTHON:-python3} "
                    "run_tests.py --help"
                ),
            }
            BUILD_TRACE.stabilize_recorded_artifacts(
                "NCAS-CMS__cfdm-114", command
            )
            self.assertEqual(command["cmd"], original)
            self.assertEqual(command["replay_cmd"], command["normalized_cmd"])
            self.assertIn("run_cfdm_tests.py", command["replay_cmd"])
            self.assertIn("三个测试组", command["replay_adjustment_reason"])

    def test_dvc_temporary_repository_keeps_the_tested_source_tree(self) -> None:
        create = {
            "id": "messages[42].tool_calls[0]",
            "cmd": "export PYTHONPATH=/testbed",
            "normalized_cmd": "cd /tmp/new-repo\nexport PYTHONPATH=.\npython -m dvc",
        }
        BUILD_TRACE.stabilize_recorded_artifacts("iterative__dvc-777", create)
        self.assertIn(
            'export PYTHONPATH="${HRB_REPLAY_REPO}"',
            create["normalized_cmd"],
        )

        add_stubs = {
            "id": "messages[57].tool_calls[0]",
            "cmd": "true",
            "normalized_cmd": (
                "sed -i 's|export PYTHONPATH=.|export PYTHONPATH=/tmp/stubs:.|' "
                "/tmp/repro_local_ext.sh && /tmp/repro_local_ext.sh"
            ),
        }
        BUILD_TRACE.stabilize_recorded_artifacts("iterative__dvc-777", add_stubs)
        self.assertIn(
            "export PYTHONPATH=/tmp/stubs:${HRB_REPLAY_REPO}",
            add_stubs["normalized_cmd"],
        )
        self.assertIn(
            "HRB_REPLAY_PYTHON=/opt/conda/bin/python3 /tmp/repro_local_ext.sh",
            add_stubs["normalized_cmd"],
        )
        self.assertIn(
            "前一条 pip 命令所属",
            add_stubs["metadata"]["replay_python_environment_adjustment"],
        )

        internal = {
            "id": "messages[62].tool_calls[0]",
            "cmd": "export PYTHONPATH=/tmp/stubs:/testbed",
            "normalized_cmd": (
                "cd /tmp/new-repo\nexport PYTHONPATH=/tmp/stubs:.\npython -m dvc"
            ),
        }
        BUILD_TRACE.stabilize_recorded_artifacts("iterative__dvc-777", internal)
        self.assertIn(
            "export PYTHONPATH=/tmp/stubs:${HRB_REPLAY_REPO}",
            internal["normalized_cmd"],
        )

    def test_nested_repository_prefix_is_removed_from_replay_command(self) -> None:
        original = (
            "cd ./owner__project__1.0 && "
            "python -c \"open('./owner__project__1.0/tests/test_one.py')\""
        )
        normalized = BUILD_TRACE.canonicalize_replay_command(
            original, "owner__project__1.0"
        )
        self.assertEqual(
            normalized,
            "cd . && python -c \"open('./tests/test_one.py')\"",
        )

    def test_repository_prefix_rewrite_respects_path_boundary(self) -> None:
        original = "test -d ./owner__project__1.0-extra"
        self.assertEqual(
            BUILD_TRACE.canonicalize_replay_command(
                original, "owner__project__1.0"
            ),
            original,
        )

    def test_dask_stdin_program_keeps_process_workers_without_spawn_restart(self) -> None:
        command = {
            "id": "messages[45].tool_calls[0]",
            "cmd": "cd /testbed && python - <<'PY'\nprint('probe')\nPY",
            "normalized_cmd": (
                "cd . && ${HRB_REPLAY_PYTHON:-python3} - <<'PY'\n"
                "print('probe')\nPY"
            ),
        }
        BUILD_TRACE.stabilize_recorded_artifacts("dask__dask-11725", command)
        self.assertIn(
            "DASK_DISTRIBUTED__WORKER__MULTIPROCESSING_METHOD=fork python -",
            command["cmd"],
        )
        self.assertIn(
            "DASK_DISTRIBUTED__WORKER__MULTIPROCESSING_METHOD=fork "
            "${HRB_REPLAY_PYTHON:-python3} -",
            command["normalized_cmd"],
        )
        self.assertIn("仍创建两个独立工作进程", command["metadata"]["replay_process_start_adjustment"])

    def test_tox_environment_probe_uses_the_recorded_search_path(self) -> None:
        command = {
            "id": "messages[34].tool_calls[0]",
            "normalized_cmd": (
                "cd . && ${HRB_REPLAY_PYTHON:-python3} -V && "
                "pip list 2>/dev/null | head -40 && "
                "which ${HRB_REPLAY_PYTHON:-python3} pip tox"
            ),
        }
        BUILD_TRACE.stabilize_recorded_artifacts("tox-dev__tox-2207", command)
        self.assertIn("export PATH=/opt/conda/bin:", command["normalized_cmd"])
        self.assertIn("which python pip tox", command["normalized_cmd"])
        self.assertIn("来源输出表明", command["metadata"]["replay_environment_adjustment"])

    def test_sanic_test_output_no_longer_uses_a_live_pipe(self) -> None:
        for identifier, lines in (
            ("messages[144].tool_calls[0]", "80"),
            ("messages[194].tool_calls[0]", "20"),
        ):
            original = (
                "cd /testbed && python -m pytest tests/test_prepare.py "
                f"tests/test_cli.py -q 2>&1 | tail -{lines}"
            )
            command = {
                "id": identifier,
                "cmd": original,
                "normalized_cmd": (
                    "cd . && ${HRB_REPLAY_PYTHON:-python3} -m pytest "
                    "tests/test_prepare.py tests/test_cli.py -q 2>&1 "
                    f"| tail -{lines}"
                ),
            }
            BUILD_TRACE.stabilize_recorded_artifacts("sanic-org__sanic-3015", command)
            for field in ("cmd", "normalized_cmd"):
                self.assertNotIn("2>&1 | tail", command[field])
                self.assertIn(f"tail -{lines} \"$_hrb_output\"", command[field])
                self.assertIn("exit \"$_hrb_status\"", command[field])
                self.assertIn("not test_server_run_with_repl", command[field])

    def test_unrelated_command_is_unchanged(self) -> None:
        command = {"id": "other", "cmd": "true"}
        BUILD_TRACE.stabilize_recorded_artifacts("other", command)
        self.assertEqual(command, {"id": "other", "cmd": "true"})


if __name__ == "__main__":
    unittest.main()

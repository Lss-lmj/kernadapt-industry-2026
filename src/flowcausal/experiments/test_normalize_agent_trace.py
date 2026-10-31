#!/usr/bin/env python3
"""Tests for normalizing real agent trajectory files into FlowCausal traces."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
NORMALIZER = HERE / "normalize_agent_trace.py"


def file_sha256(path: pathlib.Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


class NormalizeAgentTraceTests(unittest.TestCase):
    def run_normalizer(self, *args: str) -> dict:
        proc = subprocess.run(
            [sys.executable, str(NORMALIZER), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_swe_agent_traj_to_flowcausal_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "django__issue.traj"
            out = tmp / "trace.json"
            raw.write_text(
                json.dumps(
                    {
                        "instance_id": "django__issue",
                        "trajectory": [
                            {
                                "thought": "inspect",
                                "action": "open package/module.py",
                                "observation": "found",
                                "state": json.dumps({"working_dir": "/workspace/repo"}),
                            },
                            {
                                "thought": "edit",
                                "action": "git apply /tmp/fix.patch",
                                "observation": "ok",
                                "state": json.dumps({"working_dir": "/workspace/repo"}),
                            },
                            {
                                "thought": "validate",
                                "action": "pytest tests/test_target.py",
                                "observation": "passed",
                                "state": json.dumps({"working_dir": "/workspace/repo"}),
                            },
                            {"thought": "done", "action": "submit", "observation": "submitted"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = self.run_normalizer(
                "--input",
                str(raw),
                "--output",
                str(out),
                "--format",
                "swe-agent",
                "--source",
                "swe-agent-fixture",
                "--agent-repo-root",
                "/workspace/repo",
                "--created-at",
                "2026-07-19T00:00:00+00:00",
                "--emit-heuristic-frontier-labels",
            )

            self.assertEqual(summary["commands"], 3)
            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["schema"], "flowcausal.agent_trace.v1")
            self.assertEqual(trace["source"], "swe-agent-fixture")
            self.assertEqual(trace["instance_id"], "django__issue")
            self.assertEqual([cmd["stage"] for cmd in trace["commands"]], ["INSPECT", "EDIT", "TEST"])
            self.assertTrue(trace["commands"][0]["cmd"].startswith("${FLOWCAUSAL_REPLAY_PYTHON:-python3} - <<'PY'"))
            self.assertEqual(trace["commands"][0]["agent_action_kind"], "open")
            self.assertEqual(trace["commands"][0]["agent_action"], "open package/module.py")
            self.assertNotEqual(trace["commands"][0]["cmd"].strip(), "open package/module.py")
            self.assertTrue(trace["commands"][-1]["critical"])
            self.assertEqual(trace["commands"][-1]["frontier_label"]["source"], "flowcausal-normalizer-heuristic")
            self.assertEqual(trace["provenance"]["raw_events_or_steps"], 4)
            self.assertEqual(trace["provenance"]["normalized_commands"], 3)
            self.assertEqual(trace["provenance"]["input_sha256"], file_sha256(raw))

    def test_openhands_history_jsonl_to_flowcausal_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "output.jsonl"
            out = tmp / "trace.json"
            row = {
                "instance_id": "sympy__issue",
                "history": [
                    {"type": "MessageAction", "content": "plan"},
                    {
                        "action": {
                            "type": "CmdRunAction",
                            "args": {"command": "cd /workspace/repo && grep -R target sympy", "cwd": "/workspace/repo"},
                        }
                    },
                    {"type": "CmdRunAction", "command": "python3 -m py_compile sympy/core.py"},
                    {"action": "run", "args": {"command": "pytest sympy/core/tests/test_core.py"}},
                    {"type": "AgentFinishAction", "command": "exit"},
                ],
            }
            raw.write_text(json.dumps(row) + "\n", encoding="utf-8")

            self.run_normalizer(
                "--input",
                str(raw),
                "--output",
                str(out),
                "--format",
                "openhands",
                "--source",
                "openhands-fixture",
                "--instance-id",
                "sympy__issue",
                "--agent-repo-root",
                "/workspace/repo",
                "--created-at",
                "2026-07-19T00:00:00+00:00",
            )

            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["source_format"], "openhands")
            self.assertEqual(trace["instance_id"], "sympy__issue")
            self.assertEqual([cmd["stage"] for cmd in trace["commands"]], ["INSPECT", "BUILD", "TEST"])
            self.assertEqual(trace["commands"][0]["cwd"], ".")
            self.assertEqual(trace["commands"][0]["cmd"], "cd . && grep -R target sympy")
            self.assertTrue(trace["commands"][0]["allow_failure"])
            self.assertEqual(trace["commands"][0]["allow_failure_reason"], "inspect_search_probe")
            self.assertTrue(trace["commands"][-1]["critical"])
            self.assertEqual(trace["provenance"]["raw_rows"], 1)

    def test_openhands_chat_tool_calls_to_flowcausal_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "openhands-row.json"
            out = tmp / "trace.json"
            raw.write_text(
                json.dumps(
                    {
                        "schema": "flowcausal.raw_upstream.openhands_row.v1",
                        "row": {
                            "instance_id": "repo__issue",
                            "trajectory": [
                                {"role": "system", "content": "prompt"},
                                {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_view",
                                            "type": "function",
                                            "function": {
                                                "name": "str_replace_editor",
                                                "arguments": json.dumps(
                                                    {"command": "view", "path": "/workspace/repo/pkg/mod.py"}
                                                ),
                                            },
                                        }
                                    ],
                                },
                                {
                                    "role": "assistant",
                                    "tool_calls": [
                                        {
                                            "id": "call_bash",
                                            "type": "function",
                                            "function": {
                                                "name": "execute_bash",
                                                "arguments": json.dumps(
                                                    {"command": "cd /workspace/repo && python -m pytest tests/test_mod.py"}
                                                ),
                                            },
                                        }
                                    ],
                                },
                                {
                                    "role": "tool",
                                    "name": "execute_bash",
                                    "tool_call_id": "call_bash",
                                    "content": "[Command finished with exit code 1]",
                                },
                            ],
                        },
                    }
                ),
                encoding="utf-8",
            )

            summary = self.run_normalizer(
                "--input",
                str(raw),
                "--output",
                str(out),
                "--format",
                "openhands",
                "--source",
                "openhands-chat-fixture",
                "--agent-repo-root",
                "/workspace/repo",
                "--created-at",
                "2026-07-19T00:00:00+00:00",
            )

            self.assertEqual(summary["commands"], 2)
            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["instance_id"], "repo__issue")
            self.assertEqual([cmd["stage"] for cmd in trace["commands"]], ["INSPECT", "TEST"])
            self.assertEqual(trace["commands"][0]["openhands_tool"], "str_replace_editor")
            self.assertEqual(trace["commands"][0]["agent_action_kind"], "editor_view")
            self.assertIn("Path('./pkg/mod.py')", trace["commands"][0]["cmd"])
            self.assertEqual(trace["commands"][1]["openhands_tool"], "execute_bash")
            self.assertEqual(
                trace["commands"][1]["cmd"],
                "cd . && ${FLOWCAUSAL_REPLAY_PYTHON:-python3} -m pytest tests/test_mod.py",
            )
            self.assertEqual(trace["commands"][1]["agent_cmd"], "cd /workspace/repo && python -m pytest tests/test_mod.py")
            self.assertEqual(trace["commands"][1]["observed_returncode"], 1)
            self.assertTrue(trace["commands"][1]["allow_failure"])

    def test_mini_swe_agent_messages_tool_calls_to_flowcausal_trace(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "django__mini.traj"
            out = tmp / "trace.json"
            raw.write_text(
                json.dumps(
                    {
                        "trajectory_format": "mini-swe-agent-1.1",
                        "messages": [
                            {"role": "system", "content": "system prompt"},
                            {"role": "user", "content": "fix issue"},
                            {
                                "role": "assistant",
                                "content": "inspect and test",
                                "tool_calls": [
                                    {
                                        "id": "call_inspect",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": json.dumps(
                                                {
                                                    "command": "find /testbed/pkg -type f | head",
                                                    "cwd": "/testbed",
                                                }
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call_non_shell",
                                        "type": "function",
                                        "function": {
                                            "name": "python",
                                            "arguments": json.dumps({"command": "print('ignore')"}),
                                        },
                                    },
                                ],
                            },
                            {
                                "role": "assistant",
                                "content": "validate",
                                "tool_calls": [
                                    {
                                        "id": "call_test",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": json.dumps(
                                                {"command": "cd /testbed && pytest tests/test_target.py"}
                                            ),
                                        },
                                    },
                                    {
                                        "id": "call_submit",
                                        "type": "function",
                                        "function": {
                                            "name": "bash",
                                            "arguments": json.dumps({"command": "submit"}),
                                        },
                                    },
                                ],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": "call_test",
                                "content": "<returncode>1</returncode>\n<output>failed but agent continued</output>",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            summary = self.run_normalizer(
                "--input",
                str(raw),
                "--output",
                str(out),
                "--format",
                "auto",
                "--source",
                "mini-fixture",
                "--instance-id",
                "django__mini",
                "--agent-repo-root",
                "/testbed",
                "--created-at",
                "2026-07-19T00:00:00+00:00",
            )

            self.assertEqual(summary["format"], "mini-swe-agent")
            self.assertEqual(summary["commands"], 2)
            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["source_format"], "mini-swe-agent")
            self.assertEqual(trace["source"], "mini-fixture")
            self.assertEqual(trace["instance_id"], "django__mini")
            self.assertEqual(trace["commands"][0]["id"], "messages[2].tool_calls[0]")
            self.assertEqual(trace["commands"][0]["tool_call_id"], "call_inspect")
            self.assertEqual(trace["commands"][0]["cmd"], "find ./pkg -type f | head")
            self.assertEqual(trace["commands"][0]["agent_cmd"], "find /testbed/pkg -type f | head")
            self.assertEqual(trace["commands"][0]["cwd"], ".")
            self.assertEqual(trace["commands"][1]["cmd"], "cd . && pytest tests/test_target.py")
            self.assertEqual(trace["commands"][1]["observed_returncode"], 1)
            self.assertTrue(trace["commands"][1]["allow_failure"])
            self.assertEqual([cmd["stage"] for cmd in trace["commands"]], ["INSPECT", "TEST"])
            self.assertTrue(trace["commands"][1]["critical"])
            self.assertEqual(trace["provenance"]["raw_events_or_steps"], 4)
            self.assertEqual(trace["provenance"]["normalized_commands"], 2)
            self.assertEqual(trace["provenance"]["command_path_rewrite"], "agent_repo_root_to_relative")

    def test_flowcausal_input_is_rewritten_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            raw = tmp / "trace.json"
            out = tmp / "normalized.json"
            raw.write_text(
                json.dumps(
                    {
                        "source": "existing",
                        "commands": [
                            {"id": "inspect", "cmd": "find . -type f"},
                            {"id": "test", "cmd": "make test", "critical": True},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.run_normalizer(
                "--input",
                str(raw),
                "--output",
                str(out),
                "--format",
                "flowcausal",
                "--source",
                "existing-normalized",
            )

            trace = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(trace["source"], "existing-normalized")
            self.assertEqual(len(trace["commands"]), 2)
            self.assertEqual(trace["commands"][1]["stage"], "TEST")
            self.assertTrue(trace["commands"][1]["critical"])


if __name__ == "__main__":
    unittest.main()

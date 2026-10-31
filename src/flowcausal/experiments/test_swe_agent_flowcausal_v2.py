#!/usr/bin/env python3
from __future__ import annotations

import types
import unittest

try:
    from swe_agent_flowcausal_v2 import FlowCausalV2ActionHook
except ModuleNotFoundError as exc:
    if exc.name != "sweagent" and not str(exc.name).startswith("sweagent."):
        raise
    FlowCausalV2ActionHook = None


class FakeEnvironment:
    def __init__(self) -> None:
        self.commands: list[str] = []

    def communicate(self, command: str, **kwargs) -> str:
        self.commands.append(command)
        return ""


@unittest.skipUnless(FlowCausalV2ActionHook is not None, "需要安装 SWE-agent")
class SWEAgentFlowCausalV2Test(unittest.TestCase):
    def test_action_text_is_not_rewritten(self) -> None:
        env = FakeEnvironment()
        hook = FlowCausalV2ActionHook(env, 123, "/tmp/flow.sock")
        step = types.SimpleNamespace(action="cd /testbed && export VALUE=1")
        hook.on_action_started(step=step)
        self.assertEqual(step.action, "cd /testbed && export VALUE=1")
        self.assertTrue(hook.active)
        self.assertIn("--hold", env.commands[0])
        self.assertIn("--session-id 123", env.commands[0])
        hook.on_action_executed(step=step)
        self.assertFalse(hook.active)
        self.assertIn("kill -USR1", env.commands[1])


if __name__ == "__main__":
    unittest.main()

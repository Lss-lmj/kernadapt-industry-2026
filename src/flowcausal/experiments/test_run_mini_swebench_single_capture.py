#!/usr/bin/env python3
"""Tests for the mini-swe-agent SWE-bench single-instance capture adapter."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


HERE = pathlib.Path(__file__).resolve().parent
RUNNER = HERE / "run_mini_swebench_single_capture.py"


class MiniSwebenchSingleCaptureTests(unittest.TestCase):
    def test_fake_mini_extra_writes_trajectory_and_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            fake = tmp / "fake-mini-extra.py"
            traj = tmp / "runs" / "inst.traj"
            endpoint = tmp / "runs" / "inst.endpoint.json"
            fake.write_text(
                textwrap.dedent(
                    """
                    import json
                    import pathlib
                    import sys

                    args = sys.argv
                    out = pathlib.Path(args[args.index('--output') + 1])
                    inst = args[args.index('--instance') + 1]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps({
                        'instance_id': inst,
                        'trajectory': [{'action': 'pytest'}],
                    }))
                    """
                ),
                encoding="utf-8",
            )
            proc = subprocess.run(
                [
                    sys.executable,
                    str(RUNNER),
                    "--instance-id",
                    "django__django-1",
                    "--trajectory",
                    str(traj),
                    "--endpoint",
                    str(endpoint),
                    "--mini-extra",
                    f"{sys.executable} {fake}",
                    "--yolo",
                    "--cost-limit",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(traj.exists())
            payload = json.loads(endpoint.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "flowcausal.mini_swebench_single_endpoint.v1")
            self.assertEqual(payload["instance_id"], "django__django-1")
            self.assertIsNone(payload["resolved"])
            self.assertEqual(payload["harness_returncode"], 0)
            self.assertEqual(payload["provenance"]["trajectory_hint"]["trajectory_steps"], 1)


if __name__ == "__main__":
    unittest.main()

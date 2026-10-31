#!/usr/bin/env python3
"""Tests for one-instance real-agent run capture."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


HERE = pathlib.Path(__file__).resolve().parent
CAPTURE = HERE / "capture_real_agent_run.py"
VALIDATE = HERE / "validate_bundle_collection_manifest.py"


class CaptureRealAgentRunTests(unittest.TestCase):
    def run_capture(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(CAPTURE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return proc

    def test_runs_command_and_archives_raw_endpoint_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            work = tmp / "work"
            repo = tmp / "repo"
            out = tmp / "capture"
            work.mkdir()
            repo.mkdir()
            instance_id = "django__django-12345"
            command = textwrap.dedent(
                f"""
                import json
                from pathlib import Path
                Path('agent.traj').write_text(json.dumps({{
                    'instance_id': {instance_id!r},
                    'trajectory': [
                        {{'id': 'inspect', 'action': 'rg target .'}},
                        {{'id': 'test', 'action': 'pytest tests/test_target.py'}}
                    ]
                }}))
                Path('endpoint.json').write_text(json.dumps({{
                    'instance_id': {instance_id!r},
                    'resolved': True,
                    'elapsed_sec': 42.0
                }}))
                print('done')
                """
            )

            self.run_capture(
                "--out-dir",
                str(out),
                "--instance-id",
                instance_id,
                "--repo-dir",
                str(repo),
                "--raw-agent-output",
                "agent.traj",
                "--endpoint-result",
                "endpoint.json",
                "--raw-format",
                "swe-agent",
                "--source",
                "swe-agent",
                "--agent-repo-root",
                "/testbed",
                "--taskset",
                "0-1",
                "--work-dir",
                str(work),
                "--",
                sys.executable,
                "-c",
                command,
            )

            manifest = json.loads((out / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["defaults"]["source"], "swe-agent")
            self.assertEqual(manifest["defaults"]["agent_repo_root"], "/testbed")
            self.assertEqual(manifest["defaults"]["taskset"], "0-1")
            self.assertEqual(len(manifest["instances"]), 1)
            row = manifest["instances"][0]
            self.assertEqual(row["instance_id"], instance_id)
            self.assertEqual(row["raw_format"], "swe-agent")
            self.assertTrue(row["raw_agent_output"].endswith("raw/django__django-12345.traj"))
            self.assertTrue(row["full_harness_result"].endswith("endpoints/django__django-12345.full-harness.raw.json"))
            self.assertTrue((out / row["raw_agent_output"]).exists())
            self.assertTrue((out / row["full_harness_result"]).exists())
            self.assertTrue((out / "logs" / "django__django-12345.stdout.log").exists())
            report = json.loads((out / "capture-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "pass", report["warnings"])
            self.assertEqual(report["run"]["returncode"], 0)
            preflight = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATE),
                    "--manifest",
                    str(out / "real-agent-manifest.json"),
                    "--out-dir",
                    str(out),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            self.assertEqual(preflight.returncode, 0, preflight.stderr)
            preflight_report = json.loads(preflight.stdout)
            self.assertEqual(preflight_report["blockers"], [])

    def test_no_endpoint_result_uses_captured_metadata_but_stays_partial(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            out = tmp / "capture"
            repo.mkdir()
            raw = tmp / "agent.traj"
            raw.write_text(json.dumps({"instance_id": "sympy__sympy-99", "trajectory": [{"action": "pytest"}]}), encoding="utf-8")

            self.run_capture(
                "--out-dir",
                str(out),
                "--instance-id",
                "sympy__sympy-99",
                "--repo-dir",
                str(repo),
                "--raw-agent-output",
                str(raw),
                "--no-run",
            )

            manifest = json.loads((out / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["instances"]), 1)
            endpoint = out / manifest["instances"][0]["full_harness_result"]
            endpoint_payload = json.loads(endpoint.read_text(encoding="utf-8"))
            self.assertIsNone(endpoint_payload["resolved"])
            report = json.loads((out / "capture-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "partial")
            self.assertTrue(any("captured command metadata" in item for item in report["warnings"]))

    def test_system_profile_records_live_harness_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            work = tmp / "work"
            repo = tmp / "repo"
            out = tmp / "capture"
            work.mkdir()
            repo.mkdir()
            instance_id = "django__django-profile"
            command = textwrap.dedent(
                f"""
                import json
                import time
                from pathlib import Path
                Path('stage-state.json').write_text(json.dumps({{
                    'stage': 'TEST',
                    'phase': 'begin'
                }}))
                Path('agent.traj').write_text(json.dumps({{
                    'instance_id': {instance_id!r},
                    'trajectory': [{{'id': 'test', 'action': 'pytest'}}]
                }}))
                Path('endpoint.json').write_text(json.dumps({{
                    'instance_id': {instance_id!r},
                    'resolved': True,
                    'elapsed_sec': 1.0
                }}))
                time.sleep(0.15)
                """
            )

            self.run_capture(
                "--out-dir",
                str(out),
                "--instance-id",
                instance_id,
                "--repo-dir",
                str(repo),
                "--raw-agent-output",
                "agent.traj",
                "--endpoint-result",
                "endpoint.json",
                "--work-dir",
                str(work),
                "--system-profile",
                "--system-profile-interval-ms",
                "10",
                "--system-profile-stage-state-file",
                "stage-state.json",
                "--",
                sys.executable,
                "-c",
                command,
            )

            report = json.loads((out / "capture-report.json").read_text(encoding="utf-8"))
            profile = report["run"]["system_profile"]
            self.assertTrue(profile["enabled"])
            self.assertGreater(profile["samples"], 0)
            for key in ("system_profile_summary", "system_profile_samples", "system_profile_markdown"):
                path = pathlib.Path(report["artifacts"][key])
                self.assertTrue(path.exists(), key)
            summary = json.loads(pathlib.Path(report["artifacts"]["system_profile_summary"]).read_text(encoding="utf-8"))
            self.assertEqual(summary["schema"], "flowcausal.harness_replay_system_profile.v1")
            self.assertGreater(summary["sample_count"], 0)
            self.assertGreater(summary["stage_sample_counts"].get("TEST", 0), 0)
            self.assertEqual(summary["provenance"]["profile_scope"], "real_agent_harness_capture")
            self.assertEqual(summary["provenance"]["instance_id"], instance_id)
            self.assertTrue(summary["provenance"]["stage_state_file"].endswith("stage-state.json"))

    def test_missing_raw_blocks_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            out = tmp / "capture"
            repo.mkdir()

            self.run_capture(
                "--out-dir",
                str(out),
                "--instance-id",
                "astropy__astropy-1",
                "--repo-dir",
                str(repo),
                "--raw-agent-output",
                str(tmp / "missing.traj"),
                "--no-run",
                expect_ok=False,
            )

            manifest = json.loads((out / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["instances"], [])
            report = json.loads((out / "capture-report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "fail")
            self.assertTrue(any("raw_agent_output does not exist" in item for item in report["blockers"]))

    def test_multiple_captures_merge_manifest_by_instance_id(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "capture"
            repo = tmp / "repo"
            repo.mkdir()
            for instance_id in ("django__django-1", "sympy__sympy-2"):
                raw = tmp / f"{instance_id}.traj"
                raw.write_text(json.dumps({"instance_id": instance_id, "trajectory": [{"action": "pytest"}]}), encoding="utf-8")
                self.run_capture(
                    "--out-dir",
                    str(out),
                    "--instance-id",
                    instance_id,
                    "--repo-dir",
                    str(repo),
                    "--raw-agent-output",
                    str(raw),
                    "--resolved",
                    "false",
                    "--no-run",
                )

            manifest = json.loads((out / "real-agent-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual([row["instance_id"] for row in manifest["instances"]], ["django__django-1", "sympy__sympy-2"])
            self.assertIn("captures in the same --out-dir are merged", " ".join(manifest["notes"]))


if __name__ == "__main__":
    unittest.main()

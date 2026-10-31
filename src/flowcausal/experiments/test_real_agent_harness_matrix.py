#!/usr/bin/env python3
"""Tests for full real-agent harness endpoint matrix scaffolding."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


HERE = pathlib.Path(__file__).resolve().parent
MATRIX = HERE / "run_real_agent_harness_matrix.py"


class RealAgentHarnessMatrixTests(unittest.TestCase):
    def run_matrix(self, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
        proc = subprocess.run(
            [sys.executable, str(MATRIX), *args],
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

    def test_generate_only_live_variant_writes_runbook_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "matrix"
            repo = tmp / "repo"
            repo.mkdir()

            self.run_matrix(
                "--out-dir",
                str(out),
                "--instance-id",
                "django__django-1",
                "--repo-dir",
                str(repo),
                "--variant",
                "default_observed,flowcausal_width",
                "--system-profile",
                "--system-profile-interval-ms",
                "10",
                "--system-profile-stage-state-template",
                "{work_dir}/stage-state.json",
                "--command-template",
                "python fake_harness.py --instance {instance_id} --raw {raw_agent_output} --endpoint {endpoint_result}",
            )

            manifest = json.loads((out / "real-agent-harness-matrix-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "generated")
            self.assertEqual(manifest["variants"], ["default_observed", "flowcausal_width"])
            self.assertEqual(len(manifest["runs"]), 2)
            observed = manifest["runs"][0]
            flowcausal = manifest["runs"][1]
            self.assertEqual(observed["mode"], "live")
            self.assertIn(str(HERE / "run_live_sandbox.py"), observed["wrapped_command"])
            self.assertIn("--hint-dry-run", observed["wrapped_command"])
            self.assertIn("--collect-characterization", observed["wrapped_command"])
            self.assertIn("--system-profile", observed["capture_command"])
            self.assertIn("--system-profile-interval-ms", observed["capture_command"])
            self.assertIn("--system-profile-stage-state-file", observed["capture_command"])
            self.assertIn("--attach-scheduler", flowcausal["wrapped_command"])
            self.assertIn("--width-control", flowcausal["wrapped_command"])
            self.assertTrue(manifest["system_profile"]["enabled"])
            self.assertEqual(manifest["system_profile"]["interval_ms"], 10)
            self.assertEqual(manifest["system_profile"]["stage_state_template"], "{work_dir}/stage-state.json")
            script = (out / "run-harness-matrix.sh").read_text(encoding="utf-8")
            self.assertIn("capture_real_agent_run.py", script)
            self.assertFalse((out / "harness-matrix-summary.json").exists())

    def test_generated_run_script_can_require_api_env(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "matrix"
            repo = tmp / "repo"
            repo.mkdir()

            self.run_matrix(
                "--out-dir",
                str(out),
                "--instance-id",
                "django__django-1",
                "--repo-dir",
                str(repo),
                "--variant",
                "default",
                "--command-template",
                "python fake_harness.py --instance {instance_id} --raw {raw_agent_output} --endpoint {endpoint_result}",
                "--require-any-env",
                "OPENAI_API_KEY",
                "--require-any-env",
                "ANTHROPIC_API_KEY",
            )

            script = (out / "run-harness-matrix.sh").read_text(encoding="utf-8")
            self.assertIn("FLOWCAUSAL_HAS_REQUIRED_ENV=0", script)
            self.assertIn("set at least one API key before running this full-harness matrix", script)
            self.assertIn("OPENAI_API_KEY ANTHROPIC_API_KEY", script)

    def test_static_linux_baseline_variants_are_real_wrappers(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "matrix"
            repo = tmp / "repo"
            repo.mkdir()

            self.run_matrix(
                "--out-dir",
                str(out),
                "--instance-id",
                "django__django-1",
                "--repo-dir",
                str(repo),
                "--variant",
                "nice_priority,static_affinity,static_cgroup",
                "--taskset",
                "2-3",
                "--nice-value",
                "7",
                "--static-affinity-taskset",
                "5",
                "--static-cgroup-cpu-weight",
                "2000",
                "--static-cgroup-cpu-max",
                "50000 100000",
                "--command-template",
                "python fake_harness.py --instance {instance_id} --raw {raw_agent_output} --endpoint {endpoint_result}",
            )

            manifest = json.loads((out / "real-agent-harness-matrix-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["variants"], ["nice_priority", "static_affinity", "static_cgroup"])
            rows = {row["variant"]: row for row in manifest["runs"]}
            nice = rows["nice_priority"]["wrapped_command"]
            affinity = rows["static_affinity"]["wrapped_command"]
            cgroup = rows["static_cgroup"]["wrapped_command"]
            self.assertEqual(nice[:5], ["taskset", "-c", "2-3", "nice", "-n"])
            self.assertIn("7", nice)
            self.assertEqual(affinity[:3], ["taskset", "-c", "5"])
            self.assertNotEqual(affinity[:3], ["taskset", "-c", "2-3"])
            self.assertIn(str(HERE / "run_static_cgroup.py"), cgroup)
            self.assertIn("--cpu-weight", cgroup)
            self.assertIn("2000", cgroup)
            self.assertIn("--cpu-max", cgroup)
            self.assertIn("50000 100000", cgroup)
            self.assertEqual(manifest["static_baselines"]["static_affinity_taskset"], "5")

    def test_execute_default_variant_archives_endpoint_and_summarizes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            out = tmp / "matrix"
            repo = tmp / "repo"
            repo.mkdir()
            harness = tmp / "fake_harness.py"
            harness.write_text(
                textwrap.dedent(
                    """
                    import argparse
                    import json
                    import time
                    from pathlib import Path

                    parser = argparse.ArgumentParser()
                    parser.add_argument("--instance", required=True)
                    parser.add_argument("--raw", required=True)
                    parser.add_argument("--endpoint", required=True)
                    parser.add_argument("--variant", required=True)
                    parser.add_argument("--trial", type=int, required=True)
                    parser.add_argument("--stage_state", required=True)
                    args = parser.parse_args()
                    Path(args.raw).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.endpoint).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.stage_state).parent.mkdir(parents=True, exist_ok=True)
                    Path(args.stage_state).write_text(json.dumps({"stage": "TEST", "phase": "begin"}))
                    Path(args.raw).write_text(json.dumps({
                        "instance_id": args.instance,
                        "trajectory": [{"id": "test", "action": "pytest"}],
                    }))
                    Path(args.endpoint).write_text(json.dumps({
                        "instance_id": args.instance,
                        "resolved": True,
                        "elapsed_sec": 10.0 + args.trial,
                    }))
                    time.sleep(0.12)
                    print("fake harness done")
                    """
                ),
                encoding="utf-8",
            )

            self.run_matrix(
                "--out-dir",
                str(out),
                "--instance-id",
                "sympy__sympy-2",
                "--repo-dir",
                str(repo),
                "--variant",
                "default",
                "--trials",
                "2",
                "--execute",
                "--system-profile",
                "--system-profile-interval-ms",
                "10",
                "--system-profile-stage-state-template",
                "{work_dir}/stage-state.json",
                "--created-at",
                "2026-07-20T00:00:00+00:00",
                "--command-template",
                f"{sys.executable} {harness} --instance {{instance_id}} --raw {{raw_agent_output}} --endpoint {{endpoint_result}} --variant {{variant}} --trial {{trial}} --stage_state {{work_dir}}/stage-state.json",
            )

            summary = json.loads((out / "harness-matrix-summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "pass", summary)
            self.assertEqual(summary["aggregate"]["default"]["trials"], 2)
            self.assertEqual(summary["aggregate"]["default"]["passes"], 2)
            self.assertEqual(summary["aggregate"]["default"]["system_profile_enabled"], 2)
            self.assertEqual(summary["aggregate"]["default"]["system_profile_rows"], 2)
            self.assertAlmostEqual(summary["aggregate"]["default"]["elapsed_sec_mean"], 11.5)
            self.assertEqual(summary["aggregate"]["default"]["resolved_true"], 2)
            self.assertTrue(summary["trials"][0]["system_profile_summary"])
            self.assertTrue(pathlib.Path(summary["trials"][0]["system_profile_summary"]).exists())
            self.assertTrue(summary["trials"][0]["system_profile_enabled"])
            profile_summary = json.loads(pathlib.Path(summary["trials"][0]["system_profile_summary"]).read_text(encoding="utf-8"))
            self.assertGreater(profile_summary["stage_sample_counts"].get("TEST", 0), 0)
            first_capture = out / "runs" / "default" / "trial-01" / "capture"
            self.assertTrue((first_capture / "capture-report.json").exists())
            normalized = json.loads((first_capture / "normalized-endpoint.json").read_text(encoding="utf-8"))
            self.assertEqual(normalized["schema"], "flowcausal.full_harness_endpoint.v1")
            self.assertEqual(normalized["scheduler_variant"], "default")

    def test_unknown_variant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            repo = tmp / "repo"
            repo.mkdir()
            proc = self.run_matrix(
                "--out-dir",
                str(tmp / "matrix"),
                "--instance-id",
                "x__y-1",
                "--repo-dir",
                str(repo),
                "--variant",
                "invented",
                "--command-template",
                "true",
                expect_ok=False,
            )
            self.assertIn("unknown variant", proc.stderr)


if __name__ == "__main__":
    unittest.main()

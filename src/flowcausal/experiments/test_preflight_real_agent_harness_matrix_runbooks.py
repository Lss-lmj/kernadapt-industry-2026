#!/usr/bin/env python3
"""Tests for full-harness matrix runbook preflight."""

from __future__ import annotations

import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
PREFLIGHT = HERE / "preflight_real_agent_harness_matrix_runbooks.py"
REQUIRED_VARIANTS = [
    "default",
    "nice_priority",
    "static_affinity",
    "static_cgroup",
    "default_observed",
    "flowcausal_no_width",
    "flowcausal_width",
]


def write_executable(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def wrapped_command(variant: str) -> list[str]:
    base = ["python3", "harness.py"]
    if variant == "default":
        return ["taskset", "-c", "0-1", *base]
    if variant == "nice_priority":
        return ["taskset", "-c", "0-1", "nice", "-n", "-5", *base]
    if variant == "static_affinity":
        return ["taskset", "-c", "0", *base]
    if variant == "static_cgroup":
        return ["taskset", "-c", "0-1", sys.executable, str(HERE / "run_static_cgroup.py"), "--cpu-weight", "10000", "--", *base]
    if variant == "default_observed":
        return [sys.executable, str(HERE / "run_live_sandbox.py"), "--hint-dry-run", "--", *base]
    if variant == "flowcausal_no_width":
        return [sys.executable, str(HERE / "run_live_sandbox.py"), "--attach-scheduler", "--", *base]
    if variant == "flowcausal_width":
        return [sys.executable, str(HERE / "run_live_sandbox.py"), "--attach-scheduler", "--width-control", "--", *base]
    return base


def write_matrix_artifacts(root: pathlib.Path, *, missing_variant: str = "", script_guard: bool = True) -> pathlib.Path:
    matrix_dir = root / "matrices" / "django__django-1"
    runs = []
    variants = [variant for variant in REQUIRED_VARIANTS if variant != missing_variant]
    for variant in variants:
        for trial in range(1, 4):
            runs.append(
                {
                    "variant": variant,
                    "trial": trial,
                    "wrapped_command": wrapped_command(variant),
                    "capture_command": [sys.executable, str(HERE / "capture_real_agent_run.py")],
                }
            )
    manifest = {
        "schema": "flowcausal.real_agent_harness_matrix.v1",
        "status": "generated",
        "variants": variants,
        "trials": 3,
        "runs": runs,
    }
    matrix_dir.mkdir(parents=True)
    (matrix_dir / "real-agent-harness-matrix-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if script_guard:
        script = "\n".join(
            [
                "#!/usr/bin/env bash",
                "FLOWCAUSAL_HAS_REQUIRED_ENV=0",
                "for key in OPENAI_API_KEY ANTHROPIC_API_KEY OPENROUTER_API_KEY DEEPSEEK_API_KEY; do echo $key; done",
                "python3 harness.py",
                "",
            ]
        )
    else:
        script = "#!/usr/bin/env bash\npython3 harness.py\n"
    write_executable(matrix_dir / "run-harness-matrix.sh", script)
    plan = {
        "schema": "flowcausal.real_agent_harness_matrix_plan.v1",
        "status": "pass",
        "rows": [
            {
                "instance_id": "django__django-1",
                "matrix_out_dir": str(matrix_dir),
            }
        ],
    }
    plan_path = root / "harness-matrix-plan.json"
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return plan_path


class PreflightRealAgentHarnessMatrixRunbooksTests(unittest.TestCase):
    def run_preflight(self, *args: str, expect_ok: bool = True) -> dict:
        proc = subprocess.run(
            [sys.executable, str(PREFLIGHT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if expect_ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
        else:
            self.assertNotEqual(proc.returncode, 0)
        return json.loads(proc.stdout)

    def test_complete_runbook_preflight_passes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            plan = write_matrix_artifacts(root)
            output = root / "preflight.json"
            markdown = root / "preflight.md"

            report = self.run_preflight(
                "--plan",
                str(plan),
                "--root",
                str(root),
                "--min-instances",
                "1",
                "--output",
                str(output),
                "--markdown-output",
                str(markdown),
            )

            self.assertEqual(report["status"], "pass", report["blockers"])
            self.assertEqual(report["summary"]["row_pass"], 1)
            self.assertIn("static_cgroup uses run_static_cgroup.py wrapper", " ".join(report["rows"][0]["evidence"]))
            self.assertTrue(output.exists())
            self.assertIn("full-harness matrix runbook preflight", markdown.read_text(encoding="utf-8"))

    def test_missing_static_baseline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            plan = write_matrix_artifacts(root, missing_variant="static_cgroup")

            report = self.run_preflight("--plan", str(plan), "--root", str(root), "--min-instances", "1", expect_ok=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("missing required variant", " ".join(report["blockers"]))
            self.assertIn("static_cgroup", " ".join(report["blockers"]))

    def test_missing_api_guard_fails(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            root = pathlib.Path(raw_tmp)
            plan = write_matrix_artifacts(root, script_guard=False)

            report = self.run_preflight("--plan", str(plan), "--root", str(root), "--min-instances", "1", expect_ok=False)

            self.assertEqual(report["status"], "fail")
            self.assertIn("API-key guard", " ".join(report["blockers"]))


if __name__ == "__main__":
    unittest.main()

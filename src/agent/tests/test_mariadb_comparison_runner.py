#!/usr/bin/env python3
"""Local checks for same-batch MariaDB comparison runner artifacts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
RUNNER = KERNADAPT_ROOT / "agent" / "run_mariadb_comparison.py"


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "comparison"
        proc = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--threads",
                "64",
                "--run-s",
                "1",
                "--profile-s",
                "1",
                "--reps",
                "1",
                "--arms",
                "default",
                "--out",
                str(out),
                "--dry-run",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout
        batch_dirs = sorted(out.glob("batch_*"))
        assert batch_dirs, proc.stdout
        batch = batch_dirs[-1]
        ledger = json.loads((batch / "state_ledger.json").read_text(encoding="utf-8"))
        assert ledger["benchmark"]["restart_protocol"] == "restart_before_each_trial", ledger
        readiness = json.loads((batch / "batch_readiness.json").read_text(encoding="utf-8"))
        assert readiness["schema_version"] == "lockagent.same_batch_readiness.v0.1", readiness
        assert readiness["status"] == "ready", readiness
        assert any(check["name"] == "sudo_noninteractive_available" and check["severity"] == "info" for check in readiness["checks"]), readiness
        manifest = json.loads((batch / "batch_manifest.json").read_text(encoding="utf-8"))
        assert manifest["readiness"] == str(batch / "batch_readiness.json"), manifest
        assert manifest["readiness_status"] == "ready", manifest
        trial_dir = Path(manifest["trials"][0]["trial_dir"])
        profile = json.loads((trial_dir / "profile.json").read_text(encoding="utf-8"))
        assert profile["data_provenance"]["contains_oracle_best"] is False, profile
        execution_manifest = json.loads((trial_dir / "execution" / "manifest.json").read_text(encoding="utf-8"))
        assert execution_manifest["state_ledger"] == str(batch / "state_ledger.json"), execution_manifest
        guard = json.loads((trial_dir / "execution" / "guard_preflight.json").read_text(encoding="utf-8"))
        assert guard["verdict"] == "allow", guard

        online_out = Path(td) / "comparison_online"
        online = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--threads",
                "64",
                "--run-s",
                "1",
                "--profile-s",
                "1",
                "--reps",
                "1",
                "--arms",
                "lockagent_online",
                "--out",
                str(online_out),
                "--dry-run",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert online.returncode == 0, online.stdout
        online_batch = sorted(online_out.glob("batch_*"))[-1]
        online_manifest = json.loads((online_batch / "batch_manifest.json").read_text(encoding="utf-8"))
        assert online_manifest["kernadapt_memory_scope"] == "batch_local", online_manifest
        assert online_manifest["kernadapt_update_skills"] is False, online_manifest
        assert online_manifest["kernadapt_memory_root"] == str(online_batch / "kernadapt_memory"), online_manifest
        assert online_manifest["lockagent_memory_scope"] == "batch_local", online_manifest
        assert online_manifest["lockagent_update_skills"] is False, online_manifest
        trial = online_manifest["trials"][0]
        assert trial["arm"] == "kernadapt_online", trial
        assert trial["legacy_arm"] == "lockagent_online", trial
        assert trial["kernadapt_memory_scope"] == "batch_local", trial
        assert trial["kernadapt_update_skills"] is False, trial
        assert trial["lockagent_memory_scope"] == "batch_local", trial
        assert trial["lockagent_update_skills"] is False, trial
        online_run = Path(trial["trial_dir"])
        kernadapt_manifest = json.loads((online_run / "online_manifest.json").read_text(encoding="utf-8"))
        assert kernadapt_manifest["update_skills"] is False, kernadapt_manifest
        assert kernadapt_manifest["memory_isolation"]["mode"] == "explicit_paths", kernadapt_manifest
        assert str(online_batch / "kernadapt_memory" / "global_memory.jsonl") == kernadapt_manifest["global_memory"], kernadapt_manifest
        assert str(online_batch / "kernadapt_memory" / "systemic_overrides.json") == kernadapt_manifest["systemic_overrides"], kernadapt_manifest

        check_out = Path(td) / "comparison_check"
        check = subprocess.run(
            [
                sys.executable,
                str(RUNNER),
                "--threads",
                "64",
                "--run-s",
                "1",
                "--profile-s",
                "1",
                "--reps",
                "1",
                "--arms",
                "default",
                "fixed_locknest",
                "--out",
                str(check_out),
                "--check-only",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        check_batches = sorted(check_out.glob("batch_*"))
        assert check_batches, check.stdout
        check_batch = check_batches[-1]
        check_readiness = json.loads((check_batch / "batch_readiness.json").read_text(encoding="utf-8"))
        check_manifest = json.loads((check_batch / "batch_manifest.json").read_text(encoding="utf-8"))
        assert check_manifest["check_only"] is True, check_manifest
        assert check_manifest["trials"] == [], check_manifest
        expected_returncode = 0 if check_readiness["status"] == "ready" else 2
        assert check.returncode == expected_returncode, check.stdout
        if check_readiness["status"] == "ready":
            assert check_readiness["status"] == "ready", check_readiness
        else:
            assert check_readiness["status"] == "blocked", check_readiness
            assert check_readiness["blocking_reasons"], check_readiness

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

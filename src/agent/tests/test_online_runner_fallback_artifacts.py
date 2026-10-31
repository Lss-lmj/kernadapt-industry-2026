#!/usr/bin/env python3
"""Checks synthetic fallback artifacts for failed online runner paths."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "run_lockagent_online.py"
spec = importlib.util.spec_from_file_location("run_lockagent_online", MODULE_PATH)
assert spec and spec.loader
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        records = runner.materialize_failure_fallback_artifacts(
            run_id="run_test",
            workload="mariadb-sysbench",
            threads=64,
            db_name="sbtest",
            end_time="2026-06-27T00:00:00+0800",
            manifest={"returncode": 7},
            status_path=base / "execution" / "status.json",
            decision_path=base / "decision.json",
            profile_path=base / "profile.json",
            state_ledger_path=base / "state_ledger.json",
            fallback_path=base / "fallback_artifacts.json",
        )
        assert len(records) == 4, records
        fallback = load(base / "fallback_artifacts.json")
        assert fallback["created"] is True, fallback
        assert fallback["safe_for_performance_claims"] is False, fallback
        assert {item["artifact_type"] for item in fallback["records"]} == {
            "execution_status",
            "decision",
            "profile",
            "state_ledger",
        }, fallback

        status = load(base / "execution" / "status.json")
        assert status["synthetic_failure_fallback"] is True, status
        assert status["safe_for_performance_claims"] is False, status
        assert status["returncode"] == 7, status

        decision = load(base / "decision.json")
        assert decision["scheduler"] == "default", decision
        assert decision["data_provenance"]["synthetic_failure_fallback"] is True, decision
        assert decision["data_provenance"]["safe_for_policy_claims"] is False, decision

        profile = load(base / "profile.json")
        assert profile["data_provenance"]["contains_oracle_best"] is False, profile
        assert profile["data_provenance"]["synthetic_failure_fallback"] is True, profile
        assert profile["data_provenance"]["safe_for_policy_claims"] is False, profile

        ledger = load(base / "state_ledger.json")
        assert ledger["synthetic_failure_fallback"] is True, ledger
        assert ledger["safe_for_environment_claims"] is False, ledger
        assert ledger["database"]["collection_status"] == "unknown", ledger

        records = runner.materialize_failure_fallback_artifacts(
            run_id="run_test",
            workload="mariadb-sysbench",
            threads=64,
            db_name="sbtest",
            end_time="2026-06-27T00:00:01+0800",
            manifest={"returncode": 0},
            status_path=base / "execution" / "status.json",
            decision_path=base / "decision.json",
            profile_path=base / "profile.json",
            state_ledger_path=base / "state_ledger.json",
            fallback_path=base / "fallback_artifacts.json",
        )
        assert records == [], records
        fallback = load(base / "fallback_artifacts.json")
        assert fallback["created"] is False, fallback
        assert fallback["records"] == [], fallback
        assert fallback["safe_for_performance_claims"] is True, fallback

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

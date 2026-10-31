#!/usr/bin/env python3
"""Checks that online runner preflight uses the shared PolicyEngine."""

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


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def valid_decision() -> dict:
    return {
        "scheduler": "default",
        "confidence": 0.9,
        "rationale": "test",
        "evidence_used": ["profile.signals"],
        "expected_benefit": {"throughput": "baseline", "latency": "baseline", "fairness": "baseline"},
        "risks": ["test"],
        "validation_plan": ["parse metrics"],
        "rollback_conditions": ["benchmark failure"],
    }


def valid_profile() -> dict:
    return {
        "signals": {"lock_wait": 1.0},
        "candidate_schedulers": ["default", "scx_lockcoop"],
        "data_provenance": {"contains_oracle_best": False},
        "scheduler_skill_cards": [],
        "workload": {"name": "mariadb-sysbench", "family": "oltp_database"},
    }


def valid_ledger() -> dict:
    return {
        "database": {"collection_status": "ok"},
        "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"},
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        profile = base / "profile.json"
        decision = base / "decision.json"
        ledger = base / "ledger.json"
        write(profile, valid_profile())
        write(decision, valid_decision())
        write(ledger, valid_ledger())

        ok = runner.build_critic_preflight(profile, decision, ledger)
        assert ok["verdict"] == "allow", ok
        assert ok["enforcement_model"] == "PolicyEngine", ok
        assert ok["policy_engine"]["verdict"] == "allow", ok
        assert ok["policy_context"]["phase"] == "preflight", ok
        assert all(check.get("source") == "PolicyEngine" for check in ok["checks"]), ok
        assert any(check.get("name") == "rollback_required" for check in ok["checks"]), ok

        bad = valid_decision()
        bad["rollback_conditions"] = []
        write(decision, bad)
        missing_rollback = runner.build_critic_preflight(profile, decision, ledger)
        assert missing_rollback["verdict"] == "block", missing_rollback
        assert "missing_rollback_conditions" in missing_rollback["blocking_reasons"], missing_rollback
        rollback_check = next(check for check in missing_rollback["checks"] if check.get("name") == "rollback_required")
        assert rollback_check["category"] == "rollback", rollback_check
        assert rollback_check["evidence_source"] == "decision.rollback_conditions", rollback_check

        write(decision, valid_decision())
        oracle_profile = valid_profile()
        oracle_profile["data_provenance"] = {"contains_oracle_best": True}
        write(profile, oracle_profile)
        oracle = runner.build_critic_preflight(profile, decision, ledger)
        assert oracle["verdict"] == "block", oracle
        assert "profile_contains_oracle_best" in oracle["blocking_reasons"], oracle
        oracle_check = next(check for check in oracle["checks"] if check.get("name") == "no_oracle_leakage")
        assert oracle_check["category"] == "oracle_leakage", oracle_check
        assert oracle_check["source"] == "PolicyEngine", oracle_check

        scx_decision = valid_decision()
        scx_decision["scheduler"] = "scx_lockcoop"
        write(decision, scx_decision)
        no_signal_profile = valid_profile()
        no_signal_profile["signals"] = {}
        write(profile, no_signal_profile)
        deferred = runner.build_critic_preflight(profile, decision, ledger)
        assert deferred["verdict"] == "allow", deferred
        assert "non_default_scheduler_requires_lock_signals" in deferred["deferred_policy_reasons"], deferred
        lock_check = next(check for check in deferred["checks"] if check.get("name") == "lock_signal_required_for_scx")
        assert lock_check["source"] == "PolicyEngine", lock_check
        assert lock_check["severity"] == "block", lock_check

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

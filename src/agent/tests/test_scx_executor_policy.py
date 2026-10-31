#!/usr/bin/env python3
"""Local checks for scx_executor PolicyEngine enforcement."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "scx_executor.py"
spec = importlib.util.spec_from_file_location("scx_executor", MODULE_PATH)
assert spec and spec.loader
executor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(executor)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def decision(scheduler: str) -> dict:
    return {
        "scheduler": scheduler,
        "confidence": 0.8,
        "rationale": "test",
        "evidence_used": ["profile.signals"],
        "expected_benefit": {"throughput": "increase"},
        "risks": ["test"],
        "validation_plan": ["test"],
        "rollback_conditions": ["test"],
    }


def profile() -> dict:
    return {
        "signals": {"lock_wait": 1.0},
        "candidate_schedulers": ["default", "scx_lockcoop"],
        "data_provenance": {"contains_oracle_best": False},
    }


def ledger() -> dict:
    return {
        "database": {"collection_status": "ok"},
        "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"},
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        decision_path = base / "decision.json"
        profile_path = base / "profile.json"
        ledger_path = base / "ledger.json"
        out = base / "execution"
        write(decision_path, decision("scx_unknown"))
        write(profile_path, profile())
        write(ledger_path, ledger())

        rc = executor.main(
            [
                "--decision",
                str(decision_path),
                "--benchmark-cmd",
                "true",
                "--out",
                str(out),
                "--timeout-s",
                "1",
                "--dry-run",
                "--ledger",
                str(ledger_path),
                "--profile",
                str(profile_path),
            ]
        )
        assert rc == 2, rc
        guard = json.loads((out / "guard_preflight.json").read_text(encoding="utf-8"))
        assert guard["enforcement_model"] == "PolicyEngine", guard
        assert guard["verdict"] == "block", guard
        assert any(reason.startswith("scheduler_not_allowlisted") for reason in guard["reasons"]), guard
        status = json.loads((out / "status.json").read_text(encoding="utf-8"))
        assert status["status"] == "guard_blocked", status
        postflight = json.loads((out / "guard_postflight.json").read_text(encoding="utf-8"))
        assert postflight["enforcement_model"] == "PolicyEngine", postflight
        assert postflight["policy_context"]["phase"] == "postflight", postflight

        out_missing_ledger = base / "execution_missing_ledger"
        write(decision_path, decision("default"))
        rc = executor.main(
            [
                "--decision",
                str(decision_path),
                "--benchmark-cmd",
                "true",
                "--out",
                str(out_missing_ledger),
                "--timeout-s",
                "1",
                "--dry-run",
            ]
        )
        assert rc == 2, rc
        guard = json.loads((out_missing_ledger / "guard_preflight.json").read_text(encoding="utf-8"))
        assert "missing_state_ledger" in guard["reasons"], guard
        postflight = json.loads((out_missing_ledger / "guard_postflight.json").read_text(encoding="utf-8"))
        assert postflight["enforcement_model"] == "PolicyEngine", postflight
        assert postflight["policy_context"]["phase"] == "postflight", postflight

        executor.sched_ext_state = lambda: "disabled"
        write(decision_path, decision("default"))
        upstream_allow = base / "runtime_policy_preflight_allow.json"
        write(upstream_allow, {"verdict": "allow", "policy_engine": {"checks": []}})
        out_upstream_allow = base / "execution_upstream_allow"
        rc = executor.main(
            [
                "--decision",
                str(decision_path),
                "--benchmark-cmd",
                "true",
                "--out",
                str(out_upstream_allow),
                "--timeout-s",
                "1",
                "--dry-run",
                "--ledger",
                str(ledger_path),
                "--profile",
                str(profile_path),
                "--runtime-policy-preflight",
                str(upstream_allow),
            ]
        )
        assert rc == 0, rc
        manifest = json.loads((out_upstream_allow / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["policy_enforcement"]["mode"] == "consume_and_reexecute_policy_engine", manifest
        guard = json.loads((out_upstream_allow / "guard_preflight.json").read_text(encoding="utf-8"))
        assert guard["verdict"] == "allow", guard
        assert guard["policy_context"]["upstream_policy_required"] is True, guard
        assert guard["policy_context"]["upstream_policy_exists"] is True, guard
        assert guard["policy_context"]["upstream_policy_verdict"] == "allow", guard
        status = json.loads((out_upstream_allow / "status.json").read_text(encoding="utf-8"))
        assert status["policy_enforcement"]["mode"] == "consume_and_reexecute_policy_engine", status

        upstream_block = base / "runtime_policy_preflight_block.json"
        write(upstream_block, {"verdict": "block", "policy_engine": {"checks": []}})
        out_upstream_block = base / "execution_upstream_block"
        rc = executor.main(
            [
                "--decision",
                str(decision_path),
                "--benchmark-cmd",
                "true",
                "--out",
                str(out_upstream_block),
                "--timeout-s",
                "1",
                "--dry-run",
                "--ledger",
                str(ledger_path),
                "--profile",
                str(profile_path),
                "--runtime-policy-preflight",
                str(upstream_block),
            ]
        )
        assert rc == 2, rc
        guard = json.loads((out_upstream_block / "guard_preflight.json").read_text(encoding="utf-8"))
        assert "runtime_policy_preflight_not_allow:block" in guard["reasons"], guard
        upstream_check = next(check for check in guard["policy_engine"]["checks"] if check["name"] == "upstream_policy_preflight_allows")
        assert upstream_check["category"] == "runtime_authorization", upstream_check

        out_startup_failure = base / "execution_startup_failure"
        write(decision_path, decision("scx_lockcoop"))
        executor.SCX_BIN_DIR = base / "missing_scx_bin"
        rc = executor.main(
            [
                "--decision",
                str(decision_path),
                "--benchmark-cmd",
                "true",
                "--out",
                str(out_startup_failure),
                "--timeout-s",
                "1",
                "--ledger",
                str(ledger_path),
                "--profile",
                str(profile_path),
            ]
        )
        assert rc == 1, rc
        status = json.loads((out_startup_failure / "status.json").read_text(encoding="utf-8"))
        assert status["executor_failure_class"] == "scheduler_startup_failure", status
        assert "scx binary not executable" in status["executor_error"], status

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

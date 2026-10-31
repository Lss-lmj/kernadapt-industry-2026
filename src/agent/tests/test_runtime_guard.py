#!/usr/bin/env python3
"""Local checks for RuntimeGuard."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "runtime_guard.py"
spec = importlib.util.spec_from_file_location("runtime_guard", MODULE_PATH)
assert spec and spec.loader
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def decision(scheduler: str = "scx_lockcoop") -> dict:
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


def valid_profile() -> dict:
    return {
        "signals": {"lock_wait": 1.0, "hot_lock_fanin": 8},
        "candidate_schedulers": ["default", "scx_lockcoop"],
        "data_provenance": {"contains_oracle_best": False},
        "scheduler_skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
        "experience_card_ids": ["mariadb_oltp_lock_nest_positive"],
        "workload": {"name": "mariadb-sysbench", "family": "oltp_database"},
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        decision_path = base / "decision.json"
        ledger_path = base / "ledger.json"
        profile_path = base / "profile.json"
        manifest_path = base / "manifest.json"
        write(decision_path, decision())
        write(
            ledger_path,
            {
                "database": {"collection_status": "ok"},
                "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"},
            },
        )
        write(profile_path, valid_profile())
        write(manifest_path, {"benchmark_cmd": "true"})

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "allow", result
        assert result["enforcement_model"] == "PolicyEngine", result
        assert result["policy_context"]["schema_version"] == guard.POLICY_CONTEXT_SCHEMA, result
        assert result["policy_context"]["requires_executor"] is True, result
        assert result["policy_engine"]["verdict"] == "allow", result

        cited = decision()
        cited["evidence_used"] = [
            "mariadb_oltp_lock_nest_positive",
            "profile.signals",
            "long_term_memory.failure_counts",
        ]
        write(decision_path, cited)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "allow", result

        cited["evidence_used"] = ["unknown_freeform_evidence"]
        write(decision_path, cited)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "unresolved_evidence:unknown_freeform_evidence" in result["reasons"], result
        write(decision_path, decision())

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=True,
            intent="online",
        )
        assert result["verdict"] == "allow", result
        assert "dry_run_no_real_scx_load:scx_lockcoop" in result["warnings"], result
        dry_run_check = next(check for check in result["policy_engine"]["checks"] if check["name"] == "dry_run_records_no_side_effect")
        assert dry_run_check["severity"] == "warn", dry_run_check

        write(decision_path, decision("scx_unknown"))
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert any(reason.startswith("scheduler_not_allowlisted") for reason in result["reasons"]), result

        write(decision_path, decision("scx_lockcoop"))
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
            requires_human_approval=True,
            human_approved=False,
        )
        assert result["verdict"] == "block", result
        assert "human_approval_required" in result["reasons"], result

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
            requires_human_approval=True,
            human_approved=True,
        )
        assert result["verdict"] == "allow", result

        missing_manifest = base / "missing_manifest.json"
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=missing_manifest,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "missing_execution_manifest" in result["reasons"], result

        write(manifest_path, {"benchmark_cmd": "true", "scheduler": "scx_simple"})
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "manifest_scheduler_mismatch:scx_simple!=scx_lockcoop" in result["reasons"], result
        write(manifest_path, {"benchmark_cmd": "true"})

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="reproduction",
            environment_compatible=False,
        )
        assert result["verdict"] == "block", result
        assert "environment_compatibility_required" in result["reasons"], result
        assert "reproduction_intent_requires_environment_compatibility" in result["warnings"], result

        unknown_ledger = {
            "database": {"collection_status": "unknown"},
            "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"},
        }
        write(ledger_path, unknown_ledger)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert "database_state_unknown:unknown" in result["warnings"], result
        write(
            ledger_path,
            {
                "database": {"collection_status": "ok"},
                "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"},
            },
        )

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
            scheduler_disablelist=["scx_lockcoop"],
        )
        assert result["verdict"] == "block", result
        assert "scheduler_disabled_by_systemic_policy:scx_lockcoop" in result["reasons"], result

        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
            require_strict_critic=True,
            critic_verdict="block",
        )
        assert result["verdict"] == "block", result
        assert "strict_critic_preflight_required" in result["reasons"], result

        profile_without_lock_signals = valid_profile()
        profile_without_lock_signals["signals"] = {}
        write(profile_path, profile_without_lock_signals)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "non_default_scheduler_requires_lock_signals" in result["reasons"], result

        oracle_profile = valid_profile()
        oracle_profile["data_provenance"] = {"contains_oracle_best": True}
        write(profile_path, oracle_profile)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "profile_contains_oracle_best" in result["reasons"], result

        candidate_mismatch_profile = valid_profile()
        candidate_mismatch_profile["candidate_schedulers"] = ["default"]
        write(profile_path, candidate_mismatch_profile)
        result = guard.preflight(
            decision_path=decision_path,
            ledger_path=ledger_path,
            profile_path=profile_path,
            manifest_path=manifest_path,
            dry_run=False,
            intent="online",
        )
        assert result["verdict"] == "block", result
        assert "scheduler_not_in_profile_candidates:scx_lockcoop" in result["reasons"], result

        write(profile_path, valid_profile())

        status_path = base / "status.json"
        benchmark_log = base / "benchmark.log"
        write(status_path, {"returncode": 0, "final_sched_ext_state": "disabled"})
        benchmark_log.write_text("ok", encoding="utf-8")
        post = guard.postflight(status_path, benchmark_log)
        assert post["verdict"] == "allow", post
        assert post["enforcement_model"] == "PolicyEngine", post
        assert post["policy_context"]["benchmark_log_exists"] is True, post

        benchmark_log.unlink()
        post = guard.postflight(status_path, benchmark_log)
        assert post["verdict"] == "allow", post
        assert "missing_benchmark_log" in post["warnings"], post

        write(status_path, {"returncode": 0, "final_sched_ext_state": "enabled"})
        post = guard.postflight(status_path, None)
        assert post["verdict"] == "block", post
        assert "sched_ext_not_disabled:enabled" in post["reasons"], post

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

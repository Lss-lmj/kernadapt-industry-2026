#!/usr/bin/env python3
"""Local checks for Critic failure attribution."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "critic.py"
spec = importlib.util.spec_from_file_location("critic", MODULE_PATH)
assert spec and spec.loader
critic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(critic)


def write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        ledger = base / "ledger.json"
        profile = base / "profile.json"
        decision = base / "decision.json"
        status = base / "status.json"
        bench = base / "sysbench.log"
        write(ledger, {"database": {"collection_status": "ok"}})
        write(profile, {"signals": {}})
        write(
            decision,
            {
                "scheduler": "scx_lockcoop",
                "evidence_used": ["profile.signals"],
                "rollback_conditions": ["rollback"],
            },
        )
        write(status, {"status": "ok", "returncode": 0, "final_sched_ext_state": "disabled"})
        bench.write_text("transactions: 1 (1.00 per sec.)", encoding="utf-8")
        args = argparse.Namespace(
            run_id="run_test",
            ledger=ledger,
            profile=profile,
            decision=decision,
            status=status,
            benchmark_log=bench,
            preflight=None,
            postflight=None,
        )
        report, trajectory, failure = critic.build_reports(args)
        assert report["valid"] is True, report
        assert trajectory["status"] == "valid", trajectory
        assert failure["categories"] == [], failure

        write(status, {"status": "benchmark_failed", "returncode": 1, "final_sched_ext_state": "enabled"})
        report, trajectory, failure = critic.build_reports(args)
        assert report["valid"] is False, report
        assert "benchmark_failure" in failure["categories"], failure
        assert "rollback_failure" in failure["categories"], failure

        write(status, {"status": "paused_for_human_approval", "returncode": 2, "final_sched_ext_state": "disabled"})
        bench.unlink()
        report, trajectory, failure = critic.build_reports(args)
        assert report["valid"] is False, report
        assert "guard_blocked" in failure["categories"], failure
        assert "executor_failure" not in failure["categories"], failure
        assert "benchmark_failure" not in failure["categories"], failure

        write(
            status,
            {
                "status": "error: scx binary not executable",
                "returncode": 1,
                "executor_failure_class": "scheduler_startup_failure",
                "final_sched_ext_state": "disabled",
            },
        )
        bench.write_text("", encoding="utf-8")
        report, trajectory, failure = critic.build_reports(args)
        assert report["valid"] is False, report
        assert "executor_failure" in failure["categories"], failure
        assert "scheduler_startup_failure" in failure["categories"], failure
        assert trajectory["executor_failure_class"] == "scheduler_startup_failure", trajectory

        write(
            status,
            {
                "status": "timeout",
                "returncode": 124,
                "executor_failure_class": "benchmark_timeout",
                "final_sched_ext_state": "disabled",
            },
        )
        report, trajectory, failure = critic.build_reports(args)
        assert "benchmark_failure" in failure["categories"], failure
        assert "benchmark_timeout" in failure["invalid_reasons"], failure

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

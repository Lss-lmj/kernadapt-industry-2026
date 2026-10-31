#!/usr/bin/env python3
"""Small local checks for LockAgent LLM decision validation."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "llm_policy_agent.py"
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location("llm_policy_agent", MODULE_PATH)
assert spec and spec.loader
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


def valid_decision() -> dict:
    return {
        "scheduler": "scx_lockboost",
        "confidence": 0.75,
        "rationale": "Owner progress is likely useful while affinity risk is unclear.",
        "evidence_used": ["profile.signals.owner_preempt_score"],
        "expected_benefit": {
            "throughput": "increase",
            "latency": "decrease",
            "fairness": "unchanged",
        },
        "risks": ["May leave NUMA handoff gains untapped."],
        "validation_plan": ["Check throughput and p99 latency in probation window."],
        "rollback_conditions": ["Rollback if p99 latency increases by more than 20%."],
    }


def assert_raises(fn, message: str) -> None:
    try:
        fn()
    except Exception:
        return
    raise AssertionError(message)


def main() -> int:
    agent.validate_decision(valid_decision())

    bad_scheduler = valid_decision()
    bad_scheduler["scheduler"] = "scx_unknown"
    assert_raises(lambda: agent.validate_decision(bad_scheduler), "bad scheduler accepted")

    bad_confidence = valid_decision()
    bad_confidence["confidence"] = 1.5
    assert_raises(lambda: agent.validate_decision(bad_confidence), "bad confidence accepted")

    missing_rollback = valid_decision()
    del missing_rollback["rollback_conditions"]
    assert_raises(lambda: agent.validate_decision(missing_rollback), "missing field accepted")

    payload = agent.build_payload(
        {
            "workload": "mariadb-sysbench",
            "candidate_schedulers": ["default", "scx_lockcoop"],
            "scheduler_skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
        },
        {"cards": []},
        {
            "returned_count": 1,
            "negative_examples": [{"run_id": "run_bad", "scheduler": "scx_lockcoop"}],
            "policy_hints": {"avoid_schedulers_with_repeated_failures": {"scx_lockcoop": {"skill_misfire": 1}}},
        },
    )
    assert payload["long_term_memory"]["returned_count"] == 1, payload
    assert payload["long_term_memory"]["negative_examples"][0]["run_id"] == "run_bad", payload

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

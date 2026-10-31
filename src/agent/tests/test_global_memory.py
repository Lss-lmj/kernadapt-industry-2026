#!/usr/bin/env python3
"""Local checks for cross-run global memory."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "global_memory.py"
spec = importlib.util.spec_from_file_location("global_memory", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        run = base / "run_1"
        run.mkdir()
        write(run / "decision.json", {"scheduler": "scx_lockcoop", "confidence": 0.8})
        write(run / "failure_attribution.json", {"categories": ["skill_misfire"], "primary_category": "skill_misfire"})
        write(run / "trajectory_status.json", {"valid": False})
        write(run / "skill_curation.json", {"skill": "choose_locknest_for_hot_lock_oltp", "verdict": "candidate_rejected_for_this_context"})
        write(run / "self_evolution_plan.json", {"systemic_actions": [{"action": "require_strict_critic_preflight_before_guard"}], "meta_template": "critic_required_agent"})
        write(run / "execution" / "status.json", {"status": "benchmark_failed", "returncode": 1, "final_sched_ext_state": "disabled"})
        write(run / "online_manifest.json", {"workload": "mariadb-sysbench", "threads": 64})

        record = module.build_record(run)
        assert record["scheduler"] == "scx_lockcoop", record
        assert record["failure_categories"] == ["skill_misfire"], record
        memory = base / "global_memory.jsonl"
        module.append_record(memory, record)
        module.append_record(
            memory,
            {
                "schema_version": module.SCHEMA_VERSION,
                "timestamp": "later",
                "run_id": "run_2",
                "workload": "mariadb-sysbench",
                "scheduler": "default",
                "trajectory_valid": True,
                "failure_categories": [],
                "skill": "fallback_default_for_non_lock_bound",
                "skill_verdict": "validated",
            },
        )
        summary = module.summarize(module.read_jsonl(memory))
        assert summary["record_count"] == 2, summary
        assert summary["failure_counts"]["skill_misfire"] == 1, summary
        assert summary["scheduler_failures"]["scx_lockcoop"]["skill_misfire"] == 1, summary
        context = module.retrieve_policy_context(
            module.read_jsonl(memory),
            workload="mariadb-sysbench",
            scheduler="scx_lockcoop",
            limit=4,
        )
        assert context["returned_count"] == 2, context
        assert context["negative_examples"][0]["primary_failure"] == "skill_misfire", context
        assert context["positive_examples"][0]["scheduler"] == "default", context
        assert context["policy_hints"]["avoid_schedulers_with_repeated_failures"]["scx_lockcoop"]["skill_misfire"] == 1, context
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

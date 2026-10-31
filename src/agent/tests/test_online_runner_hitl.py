#!/usr/bin/env python3
"""Dry-run check for the online runner's HITL executor gate."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
RUNNER = KERNADAPT_ROOT / "agent" / "run_kernadapt_online.py"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td)
        decision = out / "scx_locknest_decision.json"
        decision.write_text(
            json.dumps(
                {
                    "scheduler": "scx_lockcoop",
                    "confidence": 0.8,
                    "rationale": "test non-default scheduler approval path",
                    "evidence_used": ["profile.signals"],
                    "expected_benefit": {"throughput": "increase", "latency": "decrease", "fairness": "unchanged"},
                    "risks": ["requires privileged scheduler load"],
                    "validation_plan": ["parse TPS and p95"],
                    "rollback_conditions": ["benchmark failure", "sched_ext not disabled"],
                }
            ),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [
                "python3",
                str(RUNNER),
                "--workload",
                "mariadb-sysbench",
                "--threads",
                "64",
                "--profile-s",
                "2",
                "--run-s",
                "5",
                "--out",
                str(out),
                "--dry-run",
                "--skip-llm",
                "--decision",
                str(decision),
                "--ignore-systemic-overrides",
                "--require-human-approval",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert proc.returncode == 2, proc.stdout
        run_dirs = sorted(path for path in out.glob("run_*") if path.is_dir())
        assert len(run_dirs) == 1, proc.stdout
        run = run_dirs[0]

        gate = load_json(run / "human_gate.json")
        assert gate["status"] == "waiting", gate
        assert gate["step_name"] == "scx_executor", gate

        status = load_json(run / "execution" / "status.json")
        assert status["status"] == "paused_for_human_approval", status
        assert status["final_sched_ext_state"] == "disabled", status

        fallback = load_json(run / "fallback_artifacts.json")
        assert fallback["created"] is False, fallback
        assert fallback["records"] == [], fallback
        assert fallback["safe_for_performance_claims"] is True, fallback

        guard_postflight = load_json(run / "execution" / "guard_postflight.json")
        assert guard_postflight["verdict"] == "allow", guard_postflight
        assert guard_postflight["enforcement_model"] == "PolicyEngine", guard_postflight
        assert guard_postflight["policy_context"]["phase"] == "postflight", guard_postflight
        assert any(
            check.get("name") == "final_sched_ext_disabled"
            and check.get("category") == "postflight_cleanup"
            for check in guard_postflight["policy_engine"]["checks"]
        ), guard_postflight

        scheduler = load_json(run / "agent_scheduler.json")
        assert any(
            item.get("event") == "task_queued" and item.get("task", {}).get("step_name") == "scx_executor"
            for item in scheduler.get("history", [])
        ), scheduler

        agent_run = load_json(run / "agent_run.json")
        assert agent_run["status"] == "failed", agent_run
        assert any(
            step.get("name") == "human_approval_gate" and step.get("status") == "paused"
            for step in agent_run.get("steps", [])
        ), agent_run
        assert any(
            step.get("name") == "runtime_policy_preflight" and step.get("metadata", {}).get("handoff_id")
            for step in agent_run.get("steps", [])
        ), agent_run

        artifact_store = load_json(run / "artifact_store.json")
        artifacts = artifact_store.get("artifacts", [])
        assert any(
            item.get("step_name") == "runtime_policy_preflight"
            and item.get("artifact_role") == "input"
            and item.get("consumer") == "RuntimeGuard"
            and item.get("required") is True
            and item.get("handoff_id")
            for item in artifacts
        ), artifact_store
        assert any(
            item.get("step_name") == "runtime_policy_preflight"
            and item.get("artifact_role") == "output"
            and item.get("producer") == "RuntimeGuard"
            and item.get("required") is True
            and item.get("sha256")
            for item in artifacts
        ), artifact_store
        assert any(
            item.get("step_name") == "human_approval_gate"
            and item.get("artifact_role") == "output"
            and item.get("producer") == "RuntimeGuard"
            for item in artifacts
        ), artifact_store
        assert any(
            item.get("artifact_type") == "runtime_parity"
            and item.get("producer") == "runtime_parity"
            and item.get("sha256")
            for item in artifacts
        ), artifact_store

        manifest = load_json(run / "online_manifest.json")
        assert manifest["memory_isolation"]["enabled"] is True, manifest
        assert manifest["memory_isolation"]["mode"] == "run_local_defaults", manifest
        assert manifest["global_memory"] == str(run / "memory" / "global_memory.jsonl"), manifest
        assert manifest["global_memory_summary"] == str(run / "memory" / "global_memory_summary.json"), manifest
        assert manifest["systemic_overrides"] == str(run / "memory" / "systemic_overrides.json"), manifest
        assert manifest["system_evolution_state"] == str(run / "memory" / "system_evolution_state.json"), manifest
        assert (run / "memory" / "global_memory.jsonl").exists(), manifest
        assert (run / "resume_plan.json").exists(), manifest
        assert (run / "resume_audit.json").exists(), manifest
        assert (run / "resume_result.json").exists(), manifest
        assert (run / "runtime_parity.json").exists(), manifest
        assert manifest["resume_plan"] == str(run / "resume_plan.json"), manifest
        assert manifest["resume_audit"] == str(run / "resume_audit.json"), manifest
        assert manifest["resume_result"] == str(run / "resume_result.json"), manifest
        assert manifest["runtime_parity"] == str(run / "runtime_parity.json"), manifest
        assert manifest["runtime_finalization"]["resume_status"] in {"ok", "failed", "waiting_for_human_approval"}, manifest
        assert manifest["runtime_finalization"]["runtime_parity_status"] in {"native_not_weaker", "native_gap_detected", "failed"}, manifest

        resume_plan = load_json(run / "resume_plan.json")
        assert isinstance(resume_plan.get("execution_order"), list), resume_plan
        assert isinstance(resume_plan.get("dependency_order"), list), resume_plan
        assert isinstance(resume_plan.get("artifact_records"), list), resume_plan

        resume_audit = load_json(run / "resume_audit.json")
        assert resume_audit["run_id"] == run.name, resume_audit
        assert "dependency_graph" in resume_audit, resume_audit

        parity = load_json(run / "runtime_parity.json")
        assert parity["run_dir"] == str(run), parity
        assert parity["summary"]["status"] in {"native_not_weaker", "native_gap_detected", "failed"}, parity

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local checks for applying Systemic/Meta self-evolution overrides."""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "run_lockagent_online.py"
spec = importlib.util.spec_from_file_location("run_lockagent_online", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["run_lockagent_online"] = module
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def decision(scheduler: str) -> dict:
    return {
        "scheduler": scheduler,
        "confidence": 0.8,
        "rationale": "test provided decision",
        "evidence_used": ["profile.signals"],
        "expected_benefit": {"throughput": "increase"},
        "risks": ["test"],
        "validation_plan": ["test"],
        "rollback_conditions": ["test"],
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        old_run = root / "run_20260101_000000"
        write(old_run / "failure_attribution.json", {"categories": ["scheduler_startup_failure"]})
        write(old_run / "decision.json", {"scheduler": "scx_lockcoop"})

        overrides = {
            "require_environment_compatibility": True,
            "require_strict_critic": True,
            "require_human_approval": True,
            "profile_s_multiplier": 3,
            "scheduler_disablelist": ["last_failed_scheduler", "scx_simple"],
            "meta_template": "critic_required_agent",
        }
        args = argparse.Namespace(profile_s=5, require_human_approval=False)
        application = module.apply_systemic_overrides(args, overrides, history_root=root)

        assert args.profile_s == 15, application
        assert args.require_human_approval is True, application
        assert args.systemic_overrides_applied["scheduler_disablelist"] == ["scx_lockcoop", "scx_simple"], application
        assert application["applied_count"] == 6, application
        fields = {item["field"] for item in application["applied"]}
        assert "profile_s_multiplier" in fields, application
        assert "scheduler_disablelist" in fields, application
        assert "meta_template" in fields, application
        assert application["profile_s"] == {"original": 5, "final": 15}, application
        assert application["execution_graph"]["role_sequence"] == [
            "ProfilerAgent",
            "ExecutorAgent",
            "ExecutorAgent",
            "ProfilerAgent",
            "PolicyAgent",
            "CriticAgent",
            "RuntimeGuard",
            "ExecutorAgent",
            "ExecutorAgent",
            "CriticAgent",
            "CriticAgent",
            "CuratorAgent",
            "CuratorAgent",
            "CuratorAgent",
            "CriticAgent",
            "CriticAgent",
        ], application
        assert [node["name"] for node in application["execution_graph"]["step_nodes"]] == [
            "state_ledger",
            "restart_mariadb_for_profile",
            "default_profile_window",
            "profile_builder",
            "llm_policy_agent",
            "critic_preflight",
            "runtime_policy_preflight",
            "restart_mariadb_for_execution",
            "scx_executor",
            "critic_postflight",
            "build_provenance",
            "skill_curator",
            "self_evolution_planner",
            "global_memory_append",
            "build_final_provenance",
            "causal_attribution",
        ], application
        assert module.runtime_template_policy("critic_required_agent") == {
            "template": "critic_required_agent",
            "requires_critic_preflight": True,
            "run_skill_curator": False,
            "run_causal_attribution": True,
            "run_counterfactual_planner": False,
            "run_skill_evolution_application": False,
            "mode": "critic_required_runtime_control",
        }

        normalized = module.normalize_systemic_overrides(
            {"scheduler_disablelist": ["last_failed_scheduler"]},
            history_root=root / "missing",
        )
        assert normalized["scheduler_disablelist"] == [], normalized

        empty_args = argparse.Namespace(profile_s=2, require_human_approval=False)
        empty = module.apply_systemic_overrides(empty_args, {}, history_root=root)
        assert empty["ignored"] is True, empty
        assert empty["applied_count"] == 0, empty
        assert empty_args.profile_s == 2, empty

        runner_root = root / "online_runs"
        history = runner_root / "run_20260101_000000"
        write(history / "failure_attribution.json", {"categories": ["scheduler_startup_failure"]})
        write(history / "decision.json", {"scheduler": "scx_lockcoop"})
        overrides_path = root / "systemic_overrides.json"
        write(
            overrides_path,
            {
                "require_environment_compatibility": True,
                "require_strict_critic": True,
                "profile_s_multiplier": 2,
                "scheduler_disablelist": ["last_failed_scheduler"],
                "meta_template": "curator_training_mode",
            },
        )
        provided_decision = root / "provided_decision.json"
        write(provided_decision, decision("scx_lockcoop"))
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lockagent_online.py"),
                "--workload",
                "mariadb-sysbench",
                "--threads",
                "64",
                "--profile-s",
                "2",
                "--run-s",
                "1",
                "--out",
                str(runner_root),
                "--dry-run",
                "--skip-llm",
                "--decision",
                str(provided_decision),
                "--systemic-overrides",
                str(overrides_path),
            ],
            cwd=ROOT.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert proc.returncode == 2, proc.stdout
        new_runs = sorted(path for path in runner_root.glob("run_*") if path.name != history.name)
        assert new_runs, proc.stdout
        run_dir = new_runs[-1]
        application = json.loads((run_dir / "systemic_override_application.json").read_text(encoding="utf-8"))
        assert application["profile_s"] == {"original": 2, "final": 4}, application
        assert application["memory_isolation"]["enabled"] is True, application
        assert application["memory_isolation"]["explicit"]["systemic_overrides"] is True, application
        assert application["memory_isolation"]["rewritten"]["global_memory"]["to"] == str(run_dir / "memory" / "global_memory.jsonl"), application
        assert "systemic_overrides" not in application["memory_isolation"]["rewritten"], application
        assert application["normalized_overrides"]["scheduler_disablelist"] == ["scx_lockcoop"], application
        assert application["normalized_overrides"]["require_environment_compatibility"] is True, application
        assert application["normalized_overrides"]["require_strict_critic"] is True, application
        assert application["meta_template"] == "curator_training_mode", application
        assert application["execution_graph"]["role_sequence"] == [
            "ProfilerAgent",
            "ExecutorAgent",
            "ExecutorAgent",
            "ProfilerAgent",
            "PolicyAgent",
            "CriticAgent",
            "RuntimeGuard",
            "ExecutorAgent",
            "ExecutorAgent",
            "CriticAgent",
            "CriticAgent",
            "CuratorAgent",
            "CuratorAgent",
            "CuratorAgent",
            "CriticAgent",
            "CriticAgent",
            "CriticAgent",
            "CuratorAgent",
            "CuratorAgent",
        ], application
        assert [node["name"] for node in application["execution_graph"]["step_nodes"]][-1] == "skill_evolution_application", application
        agent_run = json.loads((run_dir / "agent_run.json").read_text(encoding="utf-8"))
        assert agent_run["graph"]["step_nodes"] == application["execution_graph"]["step_nodes"], agent_run
        manifest = json.loads((run_dir / "online_manifest.json").read_text(encoding="utf-8"))
        assert manifest["profile_s"] == 4, manifest
        assert manifest["global_memory"] == str(run_dir / "memory" / "global_memory.jsonl"), manifest
        assert manifest["systemic_overrides"] == str(overrides_path), manifest
        assert manifest["memory_isolation"]["paths"]["systemic_overrides"] == str(overrides_path), manifest
        assert manifest["systemic_overrides_applied"]["scheduler_disablelist"] == ["scx_lockcoop"], manifest
        assert (run_dir / "resume_plan.json").exists(), manifest
        assert (run_dir / "resume_audit.json").exists(), manifest
        assert (run_dir / "resume_result.json").exists(), manifest
        assert (run_dir / "runtime_parity.json").exists(), manifest
        assert manifest["resume_plan"] == str(run_dir / "resume_plan.json"), manifest
        assert manifest["resume_audit"] == str(run_dir / "resume_audit.json"), manifest
        assert manifest["resume_result"] == str(run_dir / "resume_result.json"), manifest
        assert manifest["runtime_parity"] == str(run_dir / "runtime_parity.json"), manifest
        assert manifest["runtime_finalization"]["resume_status"] in {"ok", "failed", "nothing_to_resume"}, manifest
        assert manifest["runtime_finalization"]["runtime_parity_status"] in {"native_not_weaker", "native_gap_detected", "failed"}, manifest

        resume_plan = json.loads((run_dir / "resume_plan.json").read_text(encoding="utf-8"))
        assert isinstance(resume_plan.get("execution_order"), list), resume_plan
        assert isinstance(resume_plan.get("dependency_order"), list), resume_plan
        assert isinstance(resume_plan.get("artifact_records"), list), resume_plan

        resume_audit = json.loads((run_dir / "resume_audit.json").read_text(encoding="utf-8"))
        assert resume_audit["run_id"] == run_dir.name, resume_audit
        assert "dependency_graph" in resume_audit, resume_audit

        runtime_parity = json.loads((run_dir / "runtime_parity.json").read_text(encoding="utf-8"))
        assert runtime_parity["run_dir"] == str(run_dir), runtime_parity
        assert runtime_parity["summary"]["status"] in {"native_not_weaker", "native_gap_detected", "failed"}, runtime_parity
        policy = json.loads((run_dir / "runtime_policy_preflight.json").read_text(encoding="utf-8"))
        assert policy["verdict"] == "block", policy
        assert "scheduler_disabled_by_systemic_policy:scx_lockcoop" in policy["reasons"], policy
        scheduler = json.loads((run_dir / "agent_scheduler.json").read_text(encoding="utf-8"))
        assert not any(
            item.get("event") == "task_queued" and item.get("task", {}).get("step_name") == "scx_executor"
            for item in scheduler.get("history", [])
        ), scheduler
        assert not (run_dir / "human_gate.json").exists(), scheduler

        fast_root = root / "fast_online_runs"
        fast_overrides = root / "fast_systemic_overrides.json"
        write(fast_overrides, {"meta_template": "fast_path_single_agent"})
        fast_decision = root / "fast_decision.json"
        write(fast_decision, decision("default"))
        fast_proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / "run_lockagent_online.py"),
                "--workload",
                "mariadb-sysbench",
                "--threads",
                "64",
                "--profile-s",
                "1",
                "--run-s",
                "1",
                "--out",
                str(fast_root),
                "--dry-run",
                "--skip-llm",
                "--decision",
                str(fast_decision),
                "--systemic-overrides",
                str(fast_overrides),
            ],
            cwd=ROOT.parent,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert fast_proc.returncode == 0, fast_proc.stdout
        fast_run = sorted(path for path in fast_root.glob("run_*") if path.is_dir())[-1]
        fast_application = json.loads((fast_run / "systemic_override_application.json").read_text(encoding="utf-8"))
        assert fast_application["runtime_template_policy"]["mode"] == "fast_runtime_control", fast_application
        declared_names = [node["name"] for node in fast_application["execution_graph"]["step_nodes"]]
        assert "critic_preflight" not in declared_names, fast_application
        assert "skill_evolution_application" not in declared_names, fast_application
        fast_agent_run = json.loads((fast_run / "agent_run.json").read_text(encoding="utf-8"))
        skipped_steps = {
            step["name"]: step
            for step in fast_agent_run["steps"]
            if step.get("status") == "skipped"
        }
        for skipped in [
            "critic_preflight",
            "skill_curator",
            "causal_attribution",
            "counterfactual_planner",
            "skill_evolution_application",
        ]:
            assert skipped in skipped_steps, skipped_steps
            assert skipped_steps[skipped]["metadata"]["runtime_template_policy"] is True, skipped_steps[skipped]
        fast_skill = json.loads((fast_run / "skill_evolution_application.json").read_text(encoding="utf-8"))
        assert fast_skill["status"] == "skipped", fast_skill
        assert fast_skill["repository_mutated"] is False, fast_skill
        fast_counterfactual = json.loads((fast_run / "counterfactual_plan.json").read_text(encoding="utf-8"))
        assert fast_counterfactual["status"] == "skipped", fast_counterfactual
        assert fast_counterfactual["task_count"] == 0, fast_counterfactual

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

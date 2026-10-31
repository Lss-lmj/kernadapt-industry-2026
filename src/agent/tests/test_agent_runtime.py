#!/usr/bin/env python3
"""Local checks for the lightweight LockAgent runtime."""

from __future__ import annotations

import tempfile
import subprocess
import json
import os
from pathlib import Path
import importlib.util
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "agent_runtime.py"
spec = importlib.util.spec_from_file_location("agent_runtime", MODULE_PATH)
assert spec and spec.loader
runtime = importlib.util.module_from_spec(spec)
sys.modules["agent_runtime"] = runtime
spec.loader.exec_module(runtime)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        run = runtime.AgentRun(
            run_id="run_test",
            intent="online",
            artifact_root=base,
            environment_fingerprint={"kernel": "test", "workload": "mariadb-sysbench"},
        )

        assert (base / "agent_run.json").exists()
        assert (base / "runtime_trace.jsonl").exists()
        assert (base / "memory_store.json").exists()
        assert (base / "agent_scheduler.json").exists()
        assert (base / "durable_runtime.db").exists()
        assert (base / "durable_runtime_summary.json").exists()
        assert (base / "typed_event_bus.jsonl").exists()
        assert run.graph.edges == [
            (runtime.ROLE_PROFILER, runtime.ROLE_POLICY),
            (runtime.ROLE_POLICY, runtime.ROLE_CRITIC),
            (runtime.ROLE_CRITIC, runtime.ROLE_GUARD),
            (runtime.ROLE_GUARD, runtime.ROLE_EXECUTOR),
            (runtime.ROLE_EXECUTOR, runtime.ROLE_CRITIC),
            (runtime.ROLE_CRITIC, runtime.ROLE_CURATOR),
        ]
        assert run.graph.step_nodes, run.graph.to_dict()
        assert run.graph.step_edges, run.graph.to_dict()
        assert any(node["name"] == "scx_executor" for node in run.graph.step_nodes), run.graph.to_dict()

        decision_path = run.store.write_json(
            "decision.json",
            {
                "scheduler": "scx_lockcoop",
                "rollback_conditions": ["return to default"],
            },
            artifact_type="decision",
            producer="PolicyAgent",
        )
        assert decision_path.exists()
        assert (base / "artifact_store.json").exists()
        in_root_relative = base / "profile.json"
        in_root_relative.write_text(json.dumps({"profile": "ok"}), encoding="utf-8")
        relative_from_cwd = Path(os.path.relpath(in_root_relative, Path.cwd()))
        registered = run.store.register(
            relative_from_cwd,
            artifact_type="profile",
            producer="test",
        )
        assert registered.path == str(in_root_relative.resolve()), registered
        assert registered.path.count(str(base.resolve())) == 1, registered

        try:
            run.record_step(
                name="bad_profiler_scx",
                role=runtime.ROLE_PROFILER,
                required_capabilities=[runtime.CAP_LOAD_SCX],
                status="ok",
            )
            raise AssertionError("ProfilerAgent should not have CAP_LOAD_SCX")
        except runtime.CapabilityError:
            pass

        step = run.record_step(
            name="execute_scx",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_LOAD_SCX, runtime.CAP_ROOT_CONTROL, runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[decision_path],
            output_artifacts=[base / "execution" / "status.json"],
            status="ok",
            returncode=0,
        )
        assert step.capability_verdict["verdict"] == "allow"
        declared_step = run.record_step(
            name="scx_executor",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[base / "execution" / "status.json"],
            output_artifacts=[base / "execution" / "declared_status.json"],
            status="ok",
            returncode=0,
        )
        assert declared_step.metadata["execution_graph_declared"] is True, declared_step.metadata
        assert declared_step.metadata["execution_graph_node"].endswith(".scx_executor"), declared_step.metadata
        partial_run = runtime.AgentRun(
            run_id="run_scheduler_partial",
            intent="online",
            artifact_root=base / "scheduler_partial",
            environment_fingerprint={"kernel": "test"},
            graph=runtime.ExecutionGraph.from_step_template(
                [
                    ("first_step", runtime.ROLE_PROFILER),
                    ("second_step", runtime.ROLE_POLICY),
                ]
            ),
        )
        partial_artifact = base / "scheduler_partial" / "profile.json"
        partial_artifact.parent.mkdir(parents=True, exist_ok=True)
        partial_artifact.write_text(json.dumps({"profile": "ok"}), encoding="utf-8")
        partial_run.record_step(
            name="first_step",
            role=runtime.ROLE_PROFILER,
            required_capabilities=[runtime.CAP_BUILD_PROFILE],
            output_artifacts=[partial_artifact],
            status="ok",
        )
        partial_plan = partial_run.graph_scheduler_plan()
        assert partial_plan["summary"]["declared_step_count"] == 2, partial_plan
        assert partial_plan["summary"]["completed_step_count"] == 1, partial_plan
        assert partial_plan["summary"]["runnable_step_count"] == 1, partial_plan
        assert partial_plan["next_runnable_steps"][0]["name"] == "second_step", partial_plan
        partial_run.record_step(
            name="second_step",
            role=runtime.ROLE_POLICY,
            required_capabilities=[runtime.CAP_CALL_LLM],
            input_artifacts=[partial_artifact],
            status="ok",
        )
        dependency_plan = partial_run.graph_scheduler_plan()
        assert dependency_plan["summary"]["artifact_dependency_edge_count"] == 1, dependency_plan
        assert dependency_plan["artifact_dependency_edges"][0]["producer_declared_step_id"].endswith(".first_step"), dependency_plan
        assert dependency_plan["artifact_dependency_edges"][0]["consumer_declared_step_id"].endswith(".second_step"), dependency_plan
        failed_replay_run = runtime.AgentRun(
            run_id="run_scheduler_failed_replay",
            intent="online",
            artifact_root=base / "scheduler_failed_replay",
            environment_fingerprint={"kernel": "test"},
            graph=runtime.ExecutionGraph.from_step_template(
                [
                    ("profile_builder", runtime.ROLE_PROFILER),
                    ("llm_policy_agent", runtime.ROLE_POLICY),
                    ("critic_preflight", runtime.ROLE_CRITIC),
                    ("unrelated_curator", runtime.ROLE_CURATOR),
                ]
            ),
        )
        failed_profile = base / "scheduler_failed_replay" / "profile.json"
        failed_decision = base / "scheduler_failed_replay" / "decision.json"
        failed_profile.parent.mkdir(parents=True, exist_ok=True)
        failed_profile.write_text(json.dumps({"profile": "stale"}), encoding="utf-8")
        failed_decision.write_text(json.dumps({"scheduler": "scx_lockcoop"}), encoding="utf-8")
        failed_replay_run.record_step(
            name="profile_builder",
            role=runtime.ROLE_PROFILER,
            required_capabilities=[runtime.CAP_BUILD_PROFILE],
            output_artifacts=[failed_profile],
            status="failed",
            returncode=1,
        )
        failed_replay_run.record_step(
            name="llm_policy_agent",
            role=runtime.ROLE_POLICY,
            required_capabilities=[runtime.CAP_CALL_LLM],
            input_artifacts=[failed_profile],
            output_artifacts=[failed_decision],
            status="ok",
        )
        failed_replay_run.record_step(
            name="critic_preflight",
            role=runtime.ROLE_CRITIC,
            required_capabilities=[],
            input_artifacts=[failed_decision],
            status="ok",
        )
        failed_replay_run.record_step(
            name="unrelated_curator",
            role=runtime.ROLE_CURATOR,
            required_capabilities=[],
            status="ok",
        )
        replay_plan = failed_replay_run.graph_scheduler_plan()
        assert replay_plan["summary"]["failed_step_replay_candidate_count"] == 1, replay_plan
        candidate = replay_plan["failed_step_replay_candidates"][0]
        assert candidate["source_name"] == "profile_builder", candidate
        assert candidate["selection_policy"] == "artifact_dependency_subgraph", candidate
        assert any(step["name"] == "llm_policy_agent" for step in candidate["downstream_steps"]), candidate
        assert any(step["name"] == "critic_preflight" for step in candidate["downstream_steps"]), candidate
        assert all(step["name"] != "unrelated_curator" for step in candidate["downstream_steps"]), candidate
        artifact_store = json.loads((base / "artifact_store.json").read_text(encoding="utf-8"))
        typed_inputs = [
            item
            for item in artifact_store["artifacts"]
            if item.get("step_name") == "execute_scx" and item.get("artifact_role") == "input"
        ]
        typed_outputs = [
            item
            for item in artifact_store["artifacts"]
            if item.get("step_name") == "execute_scx" and item.get("artifact_role") == "output"
        ]
        assert typed_inputs and typed_inputs[0]["consumer"] == runtime.ROLE_EXECUTOR, artifact_store
        assert typed_inputs[0]["required"] is True and typed_inputs[0]["handoff_id"], typed_inputs
        assert typed_outputs and typed_outputs[0]["producer"] == runtime.ROLE_EXECUTOR, artifact_store
        assert typed_outputs[0]["required"] is True and typed_outputs[0]["handoff_id"], typed_outputs

        executed_payload = base / "executed_payload.json"

        def profiler_executor(step_obj: runtime.AgentStep) -> dict:
            executed_payload.write_text(json.dumps({"step": step_obj.name}), encoding="utf-8")
            return {"status": "ok", "returncode": 0, "output_artifacts": [executed_payload]}

        executed_step = run.execute_step(
            name="execute_step_profile",
            role=runtime.ROLE_PROFILER,
            required_capabilities=[runtime.CAP_BUILD_PROFILE],
            input_artifacts=[decision_path],
            output_artifacts=[executed_payload],
            executor=profiler_executor,
        )
        assert executed_step.status == "ok"
        assert executed_step.metadata["execution_graph_declared"] is False, executed_step.metadata
        assert executed_payload.exists()
        artifact_store = json.loads((base / "artifact_store.json").read_text(encoding="utf-8"))
        assert any(
            item.get("step_name") == "execute_step_profile"
            and item.get("artifact_role") == "input"
            and item.get("consumer") == runtime.ROLE_PROFILER
            for item in artifact_store["artifacts"]
        ), artifact_store
        assert any(
            item.get("step_name") == "execute_step_profile"
            and item.get("artifact_role") == "output"
            and item.get("producer") == runtime.ROLE_PROFILER
            and item.get("sha256")
            for item in artifact_store["artifacts"]
        ), artifact_store

        run.memory.add(namespace="skills", key="choose_affinity", value={"status": "candidate"}, source="test")
        assert run.memory.query(namespace="skills", key="choose_affinity")[0]["value"]["status"] == "candidate"

        task = run.submit_task(
            task_id="task-1",
            role=runtime.ROLE_POLICY,
            step_name="llm_policy_agent",
            payload={"profile": "profile.json"},
            priority=1,
        )
        assert task.status == "queued"
        queued = run.scheduler.pop_ready([runtime.ROLE_POLICY])
        assert queued is not None and queued.task_id == "task-1"
        assert queued.status == "running"
        assert queued.attempts == 1
        assert queued.lease_until is not None
        assert run.scheduler.pop_ready([runtime.ROLE_POLICY]) is None
        heartbeat = run.scheduler.heartbeat("task-1", worker_id=None, lease_s=30)
        assert heartbeat["ok"] is True, heartbeat
        run.scheduler.complete("task-1", "ok")

        leased_task = run.submit_task(
            task_id="task-lease",
            role=runtime.ROLE_POLICY,
            step_name="lease_policy",
            payload={"command": ["/bin/true"]},
            priority=1,
        )
        leased = run.scheduler.pop_ready([runtime.ROLE_POLICY], worker_id="worker-a", lease_s=60)
        assert leased is not None and leased.task_id == leased_task.task_id
        assert run.scheduler.pop_ready([runtime.ROLE_POLICY], worker_id="worker-b", lease_s=60) is None
        bad_heartbeat = run.scheduler.heartbeat("task-lease", worker_id="worker-b")
        assert bad_heartbeat["ok"] is False and bad_heartbeat["reason"] == "lease_not_owned", bad_heartbeat
        for task_item in run.scheduler._queue:
            if task_item.task_id == "task-lease":
                task_item.lease_until = 0
        assert run.scheduler.reclaim_expired_leases(now=1) == 1
        reclaimed = run.scheduler.pop_ready([runtime.ROLE_POLICY], worker_id="worker-b", lease_s=60)
        assert reclaimed is not None and reclaimed.task_id == "task-lease"
        assert reclaimed.attempts == 2
        run.scheduler.complete("task-lease", "ok")

        request = run.pause_for_human(
            step_name="scx_executor",
            reason="high-risk scheduler load",
            artifacts=[decision_path],
        )
        assert request["status"] == "waiting"
        assert run.status == "paused"
        approval = run.approve_human_gate(approved_by="tester", note="ok")
        assert approval["status"] == "approved"

        failed = run.record_step(
            name="replay_me",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[decision_path],
            output_artifacts=[base / "replay.out"],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        assert failed.status == "failed"

        engine = runtime.PolicyEngine()
        valid_policy_decision = {
            "scheduler": "scx_lockcoop",
            "confidence": 0.8,
            "rationale": "Lock-intensive OLTP profile cites affinity as a candidate.",
            "evidence_used": ["profile.signals.lock_wait", "skill.choose_locknest_for_hot_lock_oltp"],
            "expected_benefit": "Improve lock handoff locality.",
            "risks": ["May regress if profile is not lock-bound."],
            "validation_plan": ["same-batch default vs selected scheduler"],
            "rollback_conditions": ["return to default"],
        }
        verdict = engine.evaluate(
            {
                "decision": valid_policy_decision,
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "profile_contains_oracle_best": False,
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "allow", verdict
        for check in verdict["checks"]:
            assert check.get("category"), check
            assert check.get("evidence_source"), check
            assert check.get("remediation"), check
            assert "failure_category" in check, check

        verdict = engine.evaluate(
            {
                "decision": {**valid_policy_decision, "scheduler": "scx_unknown"},
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_unknown"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert any("scheduler_not_allowlisted" in reason for reason in verdict["blocking_reasons"]), verdict

        verdict = engine.evaluate(
            {
                "decision": {**valid_policy_decision, "confidence": 1.2},
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert any("confidence_out_of_range" in reason for reason in verdict["blocking_reasons"]), verdict

        verdict = engine.evaluate(
            {
                "decision": {**valid_policy_decision, "evidence_used": []},
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert "missing_evidence_used" in verdict["blocking_reasons"], verdict

        verdict = engine.evaluate(
            {
                "decision": valid_policy_decision,
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
                "requires_executor": True,
                "manifest_exists": True,
                "manifest_scheduler": "scx_simple",
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert "manifest_scheduler_mismatch:scx_simple!=scx_lockcoop" in verdict["blocking_reasons"], verdict

        verdict = engine.evaluate(
            {
                "decision": valid_policy_decision,
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert "non_default_scheduler_requires_lock_signals" in verdict["blocking_reasons"], verdict

        verdict = engine.evaluate(
            {
                "decision": valid_policy_decision,
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert "scheduler_not_in_profile_candidates:scx_lockcoop" in verdict["blocking_reasons"], verdict

        verdict = engine.evaluate(
            {
                "decision": {**valid_policy_decision, "evidence_used": ["oracle.best_scheduler"]},
                "ledger_exists": True,
                "profile_exists": True,
                "profile_signals": {"lock_wait": 1.0},
                "profile_candidate_schedulers": ["default", "scx_lockcoop"],
                "skill_cards": [{"name": "choose_locknest_for_hot_lock_oltp"}],
                "profile_contains_oracle_best": True,
                "dry_run": False,
            }
        )
        assert verdict["verdict"] == "block", verdict
        assert "profile_contains_oracle_best" in verdict["blocking_reasons"], verdict

        runtime.RuntimeReplayExecutor(base / "agent_run.json", dry_run=True).build_resume_plan(from_step="replay_me")
        run.finish(status="ok")
        assert (base / "openai_agents_trace.json").exists()
        assert (base / "langgraph_state.json").exists()
        assert (base / "trace_schema_alignment.json").exists()
        assert (base / "execution_graph_runtime.json").exists()
        assert (base / "graph_scheduler_plan.json").exists()
        observed_graph = json.loads((base / "execution_graph_runtime.json").read_text(encoding="utf-8"))
        assert observed_graph["observed_nodes"], observed_graph
        assert observed_graph["summary"]["observed_step_count"] >= 4, observed_graph
        assert observed_graph["summary"]["declared_step_coverage"] > 0, observed_graph
        assert observed_graph["observed_control_edges"], observed_graph
        assert observed_graph["observed_artifact_dependency_edges"], observed_graph
        scheduler_plan = json.loads((base / "graph_scheduler_plan.json").read_text(encoding="utf-8"))
        assert scheduler_plan["declared_steps"], scheduler_plan
        assert scheduler_plan["summary"]["declared_step_count"] >= 1, scheduler_plan
        assert scheduler_plan["summary"]["observed_declared_step_count"] >= 1, scheduler_plan
        artifact_store = json.loads((base / "artifact_store.json").read_text(encoding="utf-8"))
        assert any(item.get("artifact_type") == "graph_scheduler_plan" for item in artifact_store["artifacts"]), artifact_store
        durable_summary = json.loads((base / "durable_runtime_summary.json").read_text(encoding="utf-8"))
        assert durable_summary["checkpoint_count"] > 0, durable_summary
        assert durable_summary["step_count"] >= 2, durable_summary
        assert durable_summary["handoff_count"] >= 2, durable_summary
        assert durable_summary["task_count"] >= 2, durable_summary
        assert durable_summary["task_status_counts"].get("ok", 0) >= 2, durable_summary
        durable_tasks = run.durable.list_tasks(run.run_id)
        assert any(task["task_id"] == "task-1" and task["status"] == "ok" for task in durable_tasks), durable_tasks
        assert any(task["task_id"] == "task-lease" and task["attempts"] == 2 for task in durable_tasks), durable_tasks
        typed_events = [
            json.loads(line)
            for line in (base / "typed_event_bus.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(event["event_type"] == "handoff" for event in typed_events), typed_events
        assert any(event["event_type"] == "step_start" for event in typed_events), typed_events
        assert any(event["event_type"] == "human_gate_approved" for event in typed_events), typed_events
        alignment = json.loads((base / "trace_schema_alignment.json").read_text(encoding="utf-8"))
        assert alignment["alignment_score"] == 1.0, alignment
        assert all(check["passed"] for check in alignment["checks"].values()), alignment
        loaded = runtime.AgentRun.load(base / "agent_run.json")
        assert loaded.run_id == "run_test"
        assert loaded.status == "ok"
        assert any(item.name == "execute_scx" for item in loaded.steps)

        executor = runtime.RuntimeReplayExecutor(base / "agent_run.json", dry_run=True)
        result = executor.replay(from_step="replay_me")
        assert result["status"] == "ok", result
        assert result["replayed"][0]["step"] == "replay_me", result
        assert (base / "resume_result.json").exists()
        assert (base / "resume_plan.json").exists()
        assert (base / "resume_audit.json").exists()
        resume_plan = json.loads((base / "resume_plan.json").read_text(encoding="utf-8"))
        assert resume_plan["durable_resume_view"]["source"] == "durable_runtime.db", resume_plan
        assert any(step["name"] == "replay_me" for step in resume_plan["durable_resume_view"]["resumable_steps"]), resume_plan
        assert resume_plan["execution_order"], resume_plan
        assert resume_plan["dependency_order"], resume_plan
        assert resume_plan["artifact_records"], resume_plan
        assert resume_plan["resume_graph"]["execution_order"] == resume_plan["execution_order"], resume_plan
        assert resume_plan["resume_graph"]["artifact_records"] == resume_plan["artifact_records"], resume_plan
        resume_audit = json.loads((base / "resume_audit.json").read_text(encoding="utf-8"))
        assert resume_audit["status"] == "ok", resume_audit
        assert resume_audit["summary"]["replayed_count"] >= 1, resume_audit
        assert resume_audit["summary"]["dependency_node_count"] >= 2, resume_audit
        assert resume_audit["dependency_graph"]["resume_dependency_edges"], resume_audit

        dep_run = runtime.AgentRun(
            run_id="run_dependency_replay",
            intent="online",
            artifact_root=base / "dep",
            environment_fingerprint={"kernel": "test"},
        )
        dep_input = dep_run.store.write_text(
            "dep/input.txt",
            "input",
            artifact_type="test_input",
            producer="test",
        )
        dep_mid = base / "dep" / "mid.txt"
        dep_downstream = base / "dep" / "downstream.txt"
        dep_unrelated = base / "dep" / "unrelated.txt"
        dep_unrelated_input = dep_run.store.write_text(
            "dep/unrelated_input.txt",
            "unrelated",
            artifact_type="test_input",
            producer="test",
        )
        dep_run.record_step(
            name="producer_failed",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[dep_input],
            output_artifacts=[dep_mid],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        dep_run.record_step(
            name="consumer_ok",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[dep_mid],
            output_artifacts=[dep_downstream],
            metadata={"command": ["/bin/true"]},
            status="ok",
            returncode=0,
        )
        dep_run.record_step(
            name="unrelated_ok",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[dep_unrelated_input],
            output_artifacts=[dep_unrelated],
            metadata={"command": ["/bin/true"]},
            status="ok",
            returncode=0,
        )
        dep_replay = runtime.RuntimeReplayExecutor(dep_run.checkpoint_path, dry_run=True).replay(
            from_step="producer_failed",
            only_failed=False,
            dependency_replay=True,
        )
        replayed_steps = [item.get("step") for item in dep_replay["replayed"]]
        assert replayed_steps == ["producer_failed", "consumer_ok"], dep_replay
        assert dep_replay["selection_policy"] == "artifact_dependency_subgraph", dep_replay
        dep_plan = json.loads((base / "dep" / "resume_plan.json").read_text(encoding="utf-8"))
        assert dep_plan["selected_replay_indices"] == [0, 1], dep_plan
        assert dep_plan["skipped_unselected_steps"] == [
            {
                "index": 2,
                "name": "unrelated_ok",
                "reason": "outside_artifact_dependency_subgraph",
                "role": runtime.ROLE_EXECUTOR,
                "status": "ok",
            }
        ], dep_plan
        assert dep_plan["artifact_dependency_indices"] == {"0": [1]}, dep_plan
        assert dep_plan["resume_graph"]["downstream_artifact_dependency_edges"], dep_plan
        assert dep_plan["resume_graph"]["replay_selection_edges"], dep_plan
        assert dep_plan["resume_graph"]["skipped_unselected_step_count"] == 1, dep_plan
        assert all(step["input_hash_verdict"] in {"ok", "no_inputs", "pending_replay_output"} for step in dep_plan["planned_steps"]), dep_plan
        assert dep_plan["planned_steps"][1]["input_hash_verdict"] == "pending_replay_output", dep_plan
        assert all(step["side_effect_class"] == "benchmark" for step in dep_plan["planned_steps"]), dep_plan
        assert all(step["side_effect_safe"] is True for step in dep_plan["planned_steps"]), dep_plan
        assert all(step["replay_mode"] == "dry_run_replay" for step in dep_plan["planned_steps"]), dep_plan
        dep_audit = json.loads((base / "dep" / "resume_audit.json").read_text(encoding="utf-8"))
        assert dep_audit["dependency_replay"] is True, dep_audit
        assert dep_audit["selection_policy"] == "artifact_dependency_subgraph", dep_audit
        assert dep_audit["selected_replay_indices"] == [0, 1], dep_audit
        assert dep_audit["artifact_dependency_indices"] == {"0": [1]}, dep_audit
        assert dep_audit["execution_order"] == dep_plan["execution_order"], dep_audit
        assert dep_audit["dependency_order"] == dep_plan["dependency_order"], dep_audit
        assert dep_audit["artifact_records"] == dep_plan["artifact_records"], dep_audit
        assert dep_audit["summary"]["downstream_artifact_dependency_count"] >= 1, dep_audit
        assert dep_audit["summary"]["replay_selection_edge_count"] == 2, dep_audit
        assert dep_audit["summary"]["skipped_unselected_step_count"] == 1, dep_audit
        assert dep_audit["summary"]["input_hash_changed_count"] == 0, dep_audit
        assert dep_audit["summary"]["input_hash_missing_count"] == 0, dep_audit
        assert dep_audit["summary"]["side_effect_safe_count"] == 2, dep_audit
        assert dep_audit["summary"]["benchmark_replay_count"] == 2, dep_audit
        assert all(check["replay_mode"] == "dry_run_replay" for check in dep_audit["checks"]), dep_audit
        assert all(check["side_effect_class"] == "benchmark" for check in dep_audit["checks"]), dep_audit
        assert all(check["safe_to_replay"] is True for check in dep_audit["checks"]), dep_audit
        assert dep_audit["skipped_unselected_steps"] == dep_plan["skipped_unselected_steps"], dep_audit
        assert dep_audit["dependency_graph"]["downstream_artifact_dependency_edges"], dep_audit
        assert dep_audit["dependency_graph"]["replay_selection_edges"], dep_audit
        assert any(edge["to"] == "resume.step.0.producer_failed" for edge in dep_audit["dependency_graph"]["replay_selection_edges"]), dep_audit

        hash_run = runtime.AgentRun(
            run_id="run_hash_replay",
            intent="online",
            artifact_root=base / "hash",
            environment_fingerprint={"kernel": "test"},
        )
        hash_input = hash_run.store.write_text(
            "input.txt",
            "before",
            artifact_type="test_input",
            producer="test",
        )
        hash_output = base / "hash" / "output.txt"
        hash_run.record_step(
            name="benchmark_step",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            input_artifacts=[hash_input],
            output_artifacts=[hash_output],
            metadata={"command": ["/bin/true"]},
            status="failed",
            returncode=1,
        )
        Path(hash_input).write_text("after\n", encoding="utf-8")
        real_replay_executor = runtime.RuntimeReplayExecutor(hash_run.checkpoint_path, dry_run=False)
        hash_plan = real_replay_executor.build_resume_plan(from_step="benchmark_step")
        assert hash_plan["planned_steps"][0]["input_hash_verdict"] == "changed", hash_plan
        assert hash_plan["planned_steps"][0]["side_effect_class"] == "benchmark", hash_plan
        assert hash_plan["planned_steps"][0]["side_effect_safe"] is False, hash_plan
        hash_audit = real_replay_executor.build_resume_audit(
            plan=hash_plan,
            replayed=[],
            status="planned_only",
            from_step="benchmark_step",
            only_failed=True,
            drain_queue=False,
        )
        assert hash_audit["summary"]["input_hash_changed_count"] == 1, hash_audit
        assert hash_audit["summary"]["side_effect_unsafe_count"] == 1, hash_audit
        assert hash_audit["summary"]["benchmark_replay_count"] == 1, hash_audit
        assert hash_audit["checks"][0]["safe_to_replay"] is False, hash_audit
        assert hash_audit["checks"][0]["replay_mode"] == "real_replay", hash_audit

        relocated = base / "relocated_dep"
        subprocess.run(["cp", "-a", str(base / "dep"), str(relocated)], check=True)
        relocated_checkpoint = relocated / "agent_run.json"
        relocated_data = json.loads(relocated_checkpoint.read_text(encoding="utf-8"))
        relocated_data["status"] = "failed"
        for item in relocated_data["steps"]:
            if item["name"] == "producer_failed":
                item["status"] = "failed"
                item["returncode"] = 1
        relocated_checkpoint.write_text(json.dumps(relocated_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        relocated_loaded = runtime.AgentRun.load(relocated_checkpoint)
        assert relocated_loaded.artifact_root == relocated.resolve(), relocated_loaded.artifact_root
        assert relocated_loaded.environment_fingerprint["artifact_root_rebased"] is True, relocated_loaded.environment_fingerprint
        relocated_result = runtime.RuntimeReplayExecutor(relocated_checkpoint, dry_run=True).replay(
            from_step="producer_failed",
            only_failed=False,
            dependency_replay=True,
        )
        assert relocated_result["status"] == "ok", relocated_result
        assert str(relocated / "resume_plan.json") == relocated_result["resume_plan"], relocated_result
        assert (relocated / "resume_audit.json").exists(), relocated_result

        gate_run = runtime.AgentRun(
            run_id="run_gate",
            intent="online",
            artifact_root=base / "gate",
            environment_fingerprint={"kernel": "test"},
        )
        gate_request = gate_run.pause_for_human(
            step_name="scx_executor",
            reason="approval required",
            artifacts=[decision_path],
        )
        assert gate_request["status"] == "waiting"
        gate_run.record_step(
            name="runtime_policy_preflight",
            role=runtime.ROLE_GUARD,
            required_capabilities=[runtime.CAP_EVALUATE_POLICY],
            status="blocked",
            returncode=2,
        )
        gate_run.record_step(
            name="human_approval_gate",
            role=runtime.ROLE_GUARD,
            required_capabilities=[runtime.CAP_EVALUATE_POLICY],
            metadata={"requires_human_approval": True},
            status="paused",
            returncode=2,
        )
        gate_run.submit_task(
            task_id="queued-exec",
            role=runtime.ROLE_EXECUTOR,
            step_name="queued_executor",
            payload={
                "command": ["/bin/true"],
                "required_capabilities": [runtime.CAP_RUN_BENCHMARK],
                "input_artifacts": [],
                "output_artifacts": [str(base / "gate" / "queued.out")],
            },
            priority=1,
        )
        waiting = runtime.RuntimeReplayExecutor(base / "gate" / "agent_run.json", dry_run=True).replay(drain_queue=True)
        assert waiting["status"] == "waiting_for_human_approval", waiting
        gate_run = runtime.AgentRun.load(base / "gate" / "agent_run.json")
        gate_run.approve_human_gate(approved_by="tester")
        resumed = runtime.RuntimeReplayExecutor(base / "gate" / "agent_run.json", dry_run=True).replay(drain_queue=True)
        assert resumed["status"] == "ok", resumed
        assert any(item.get("reason") == "superseded_by_human_approval" for item in resumed["replayed"]), resumed
        assert any(item.get("approved_human_gate") for item in resumed["replayed"]), resumed
        assert any(item.get("task") == "queued-exec" for item in resumed["replayed"]), resumed
        gate_audit = json.loads((base / "gate" / "resume_audit.json").read_text(encoding="utf-8"))
        assert gate_audit["summary"]["queued_task_count"] == 1, gate_audit
        assert gate_audit["dependency_graph"]["resume_dependency_edges"], gate_audit

        subprocess.run(
            [
                "python3",
                str(MODULE_PATH),
                "submit-task",
                "--checkpoint",
                str(base / "agent_run.json"),
                "--task-id",
                "task-cli",
                "--role",
                runtime.ROLE_POLICY,
                "--step-name",
                "policy_cli",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "python3",
                str(MODULE_PATH),
                "pop-task",
                "--checkpoint",
                str(base / "agent_run.json"),
                "--role",
                runtime.ROLE_POLICY,
                "--worker-id",
                "cli-worker",
                "--lease-s",
                "60",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
        )

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

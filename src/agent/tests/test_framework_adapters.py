#!/usr/bin/env python3
"""Local checks for mainstream framework adapter exports."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME_PATH = ROOT / "agent_runtime.py"
ADAPTER_PATH = ROOT / "framework_adapters.py"

rt_spec = importlib.util.spec_from_file_location("agent_runtime", RUNTIME_PATH)
assert rt_spec and rt_spec.loader
runtime = importlib.util.module_from_spec(rt_spec)
sys.modules["agent_runtime"] = runtime
rt_spec.loader.exec_module(runtime)

adapter_spec = importlib.util.spec_from_file_location("framework_adapters", ADAPTER_PATH)
assert adapter_spec and adapter_spec.loader
adapters = importlib.util.module_from_spec(adapter_spec)
sys.modules["framework_adapters"] = adapters
adapter_spec.loader.exec_module(adapters)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        run = runtime.AgentRun(
            run_id="run_adapter",
            intent="online",
            artifact_root=base,
            environment_fingerprint={"kernel": "test"},
        )
        run.record_step(
            name="profile",
            role=runtime.ROLE_PROFILER,
            required_capabilities=[runtime.CAP_BUILD_PROFILE],
            output_artifacts=[base / "profile.json"],
            status="ok",
            returncode=0,
        )
        run.record_step(
            name="policy",
            role=runtime.ROLE_POLICY,
            required_capabilities=[runtime.CAP_CALL_LLM],
            input_artifacts=[base / "profile.json"],
            output_artifacts=[base / "decision.json"],
            status="ok",
            returncode=0,
        )
        run.submit_task(
            task_id="adapter-task",
            role=runtime.ROLE_EXECUTOR,
            step_name="execute",
            payload={"command": ["/bin/true"], "required_capabilities": [runtime.CAP_RUN_BENCHMARK]},
            priority=1,
        )
        runtime.RuntimeReplayExecutor(base / "agent_run.json", dry_run=True).build_resume_plan(drain_queue=True)
        run.finish(status="ok")

        bundle = adapters.build_bundle(base / "agent_run.json")
        assert bundle["coverage"]["status"] == "aligned", bundle
        assert bundle["coverage"]["coverage_score"] == 1.0, bundle
        assert bundle["coverage"]["contract_status"] == "aligned", bundle
        assert bundle["framework_contracts"]["status"] == "aligned", bundle
        assert bundle["framework_contracts"]["contract_score"] == 1.0, bundle
        contract_names = {check["name"] for check in bundle["framework_contracts"]["checks"]}
        assert "openai.handoffs_match_execution_graph" in contract_names, bundle
        assert "langgraph.observed_handoff_edges" in contract_names, bundle
        assert "langgraph.checkpoint" in contract_names, bundle
        assert "autogen.subscriptions_cover_graph_nodes" in contract_names, bundle
        assert "crewai.memory_paths" in contract_names, bundle
        assert bundle["openai_agents"]["handoffs"], bundle
        assert bundle["langgraph"]["state"]["checkpoint"].endswith("agent_run.json"), bundle
        assert bundle["langgraph"]["observed_graph"]["observed_handoff_edges"], bundle
        assert bundle["autogen_core"]["subscriptions"], bundle
        assert bundle["crewai"]["tasks"], bundle
        assert (base / "framework_adapter_bundle.json").exists(), bundle

        regression_run = runtime.AgentRun(
            run_id="run_observed_handoff",
            intent="online",
            artifact_root=base / "observed",
            environment_fingerprint={"kernel": "test"},
        )
        regression_run.record_step(
            name="default_profile_window",
            role=runtime.ROLE_EXECUTOR,
            required_capabilities=[runtime.CAP_RUN_BENCHMARK],
            output_artifacts=[base / "observed" / "default_profile_metrics.json"],
            status="ok",
            returncode=0,
        )
        regression_run.record_step(
            name="profile_builder",
            role=runtime.ROLE_PROFILER,
            required_capabilities=[runtime.CAP_BUILD_PROFILE],
            input_artifacts=[base / "observed" / "default_profile_metrics.json"],
            output_artifacts=[base / "observed" / "profile.json"],
            status="ok",
            returncode=0,
        )
        regression_run.finish(status="ok")

        regression_bundle = adapters.build_bundle(base / "observed" / "agent_run.json")
        failed = regression_bundle["framework_contracts"]["failed"]
        assert regression_bundle["framework_contracts"]["status"] == "aligned", regression_bundle
        assert not failed, regression_bundle
        observed_nodes = regression_bundle["langgraph"]["observed_graph"]["observed_nodes"]
        observed_role_edges = {
            (source["role"], target["role"])
            for source, target in zip(observed_nodes, observed_nodes[1:])
        }
        assert (runtime.ROLE_EXECUTOR, runtime.ROLE_PROFILER) in observed_role_edges, regression_bundle
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

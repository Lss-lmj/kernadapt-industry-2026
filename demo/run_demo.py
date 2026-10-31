#!/usr/bin/env python3
"""Run real control-plane code with explicitly synthetic inputs, without root or an API key."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/agent"))
from agent_runtime import AgentRun, CapabilityError, CAP_LOAD_SCX, ROLE_PROFILER
from runtime_guard import preflight


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "results/demo")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    events = []
    start = time.monotonic()

    def event(name, detail):
        record = {"elapsed_seconds": round(time.monotonic() - start, 3), "stage": name, "detail": detail}
        events.append(record)
        print(f"[{name}] {detail}", flush=True)

    event("模式", "本机控制面运行；合成策略输入；不加载调度器、不调用模型 API")
    profile = {"synthetic_fixture": True, "signals": {"lock_wait": 1.0, "hot_lock_fanin": 8}, "candidate_schedulers": ["default", "scx_lockcoop"], "data_provenance": {"contains_oracle_best": False}}
    ledger = {"synthetic_fixture": True, "database": {"collection_status": "ok"}, "benchmark": {"restart_protocol": "restart_before_profile_and_before_execution"}}
    decision = {"scheduler": "scx_lockcoop", "confidence": 0.8, "rationale": "Demo fixture for a lock-contention mechanism", "evidence_used": ["profile.signals"], "expected_benefit": {"throughput": "hypothesis_only"}, "risks": ["requires same-batch validation"], "validation_plan": ["same-batch comparison"], "rollback_conditions": ["return to default if validation fails"]}
    write(out / "profile.json", profile)
    write(out / "state_ledger.json", ledger)
    write(out / "manifest.json", {"benchmark_cmd": "true", "synthetic_fixture": True})
    verdicts = {}
    for name, patch, leak, expected in [
        ("valid_contract", {}, False, "allow"),
        ("unknown_scheduler", {"scheduler": "scx_unknown"}, False, "block"),
        ("oracle_leakage", {}, True, "block"),
        ("missing_rollback", {"rollback_conditions": []}, False, "block"),
    ]:
        current = dict(decision, **patch)
        profile["data_provenance"]["contains_oracle_best"] = leak
        write(out / "decision.json", current)
        write(out / "profile.json", profile)
        result = preflight(decision_path=out / "decision.json", ledger_path=out / "state_ledger.json", profile_path=out / "profile.json", manifest_path=out / "manifest.json", dry_run=False, intent="online")
        assert result["verdict"] == expected, (name, result)
        write(out / f"guard_{name}.json", result)
        verdicts[name] = result["verdict"]
        event("策略检查", f"{name}: {result['verdict']}")
    run = AgentRun(run_id="finals_offline_demo", intent="online", artifact_root=out / "runtime", environment_fingerprint={"mode": "synthetic_control_plane_fixture"})
    try:
        run.record_step(name="denied_profiler_load", role=ROLE_PROFILER, required_capabilities=[CAP_LOAD_SCX], status="ok")
    except CapabilityError:
        event("权限隔离", "ProfilerAgent 加载调度器的请求被拒绝")
    else:
        raise AssertionError("Unauthorized profiler was accepted")
    run.store.write_json("demo_result.json", {"verdicts": verdicts, "benchmark_claim": False}, artifact_type="demo", producer="finals_demo")
    assert (out / "runtime/durable_runtime.db").exists()
    assert (out / "runtime/artifact_store.json").exists()
    event("持久化", "已产生 SQLite 状态库、事件轨迹与产物索引")
    subprocess.run([sys.executable, "tests/verify_results.py"], cwd=ROOT, check=True)
    history = json.loads((ROOT / "tests/lockcoop/openeuler_node0_summary.json").read_text())
    event("历史实验核算", f"140 个记录样本；256 线程 vs default = {history['comparison_256_threads']['default']['throughput_ratio']:.2f}x")
    event("验证边界", "以上倍数来自归档 openEuler 实验，本次演示未重新执行内核性能实验")
    write(out / "summary.json", {"mode": "offline_control_plane", "real_kernel_load": False, "model_api_called": False, "verdicts": verdicts, "capability_rejection": True, "durable_artifacts": True, "historical_results_verified": True, "events": events})
    print("DEMO PASS", flush=True)


if __name__ == "__main__":
    main()

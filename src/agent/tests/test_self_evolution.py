#!/usr/bin/env python3
"""Local checks for systemic/meta self-evolution planning."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "self_evolution.py"
spec = importlib.util.spec_from_file_location("self_evolution", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        run = root / "run_1"
        run.mkdir()
        (run / "failure_attribution.json").write_text(
            json.dumps({"categories": ["policy_reasoning_failure", "environment_mismatch"]}),
            encoding="utf-8",
        )
        plan = module.build_plan(root)
        assert plan["run_count"] == 1, plan
        assert plan["meta_template"] == "critic_required_agent", plan
        actions = {item["action"] for item in plan["systemic_actions"]}
        assert "require_strict_critic_preflight_before_guard" in actions, plan
        assert "enable_environment_compatibility_gate" in actions, plan
        overrides = plan["systemic_overrides"]
        assert overrides["require_environment_compatibility"] is True, overrides
        assert overrides["require_strict_critic"] is True, overrides
        assert overrides["meta_template"] == "critic_required_agent", overrides
        state = plan["system_evolution_state"]
        assert state["failure_totals"]["policy_reasoning_failure"] == 1, state
        assert state["failure_totals"]["environment_mismatch"] == 1, state
        assert state["selected_meta_template"] == "critic_required_agent", state
        assert state["template_scores"]["critic_required_agent"] >= 2, state
        assert state["history"], state

        single = root / "single_run"
        single.mkdir()
        (single / "failure_attribution.json").write_text(
            json.dumps({"categories": ["environment_mismatch"]}),
            encoding="utf-8",
        )
        single_plan = module.build_plan(single)
        assert single_plan["run_count"] == 1, single_plan
        single_actions = {item["action"] for item in single_plan["systemic_actions"]}
        assert "enable_environment_compatibility_gate" in single_actions, single_plan
        assert single_plan["systemic_overrides"]["require_environment_compatibility"] is True, single_plan

        state_path = root / "system_evolution_state.json"
        out_path = root / "self_evolution_plan.json"
        overrides_path = root / "systemic_overrides.json"
        rc = module.main(
            [
                "--root",
                str(root),
                "--out",
                str(out_path),
                "--state",
                str(state_path),
                "--state-out",
                str(state_path),
                "--overrides-out",
                str(overrides_path),
            ]
        )
        assert rc == 0
        assert state_path.exists()
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["history"], persisted
        assert persisted["override_history"], persisted
        assert persisted["template_history"], persisted
        assert overrides_path.exists()

        clean_root = root / "clean"
        for idx in range(3):
            run_dir = clean_root / f"run_{idx}"
            run_dir.mkdir(parents=True)
            (run_dir / "failure_attribution.json").write_text(json.dumps({"categories": []}), encoding="utf-8")
        clean_plan = module.build_plan(clean_root, state=persisted)
        assert clean_plan["run_count"] == 3, clean_plan
        assert clean_plan["system_evolution_state"]["template_scores"]["fast_path_single_agent"] >= 1, clean_plan
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local checks for same-batch skill validation evidence."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "same_batch_validator.py"
spec = importlib.util.spec_from_file_location("same_batch_validator", MODULE_PATH)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules["same_batch_validator"] = module
spec.loader.exec_module(module)


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        summary = base / "batch_summary.json"
        write(
            summary,
            {
                "batch_id": "batch_test",
                "aggregate": [
                    {"arm": "default", "n_valid": 3, "tps_mean": 100.0, "lat_p95_ms_mean": 10.0},
                    {"arm": "fixed_locknest", "n_valid": 3, "tps_mean": 120.0, "lat_p95_ms_mean": 9.5},
                    {"arm": "lockagent_online", "n_valid": 1, "tps_mean": 118.0, "lat_p95_ms_mean": 9.8},
                    {"arm": "oracle_best", "n_valid": 1, "tps_mean": 125.0, "lat_p95_ms_mean": 9.0},
                ],
            },
        )
        positive = module.build_validation(
            summary_path=summary,
            skill="choose_locknest_for_hot_lock_oltp",
            scheduler="scx_lockcoop",
            control_arm="default",
            treatment_arm="fixed_locknest",
            min_improvement_pct=1.0,
            max_p95_regression_pct=5.0,
        )
        assert positive["trajectory_valid"] is True, positive
        assert positive["verdict"] == "positive_evidence", positive
        assert positive["validation"]["same_batch_control"] is True, positive
        assert positive["validation"]["counterfactual_default_checked"] is True, positive
        assert positive["validation"]["oracle_best_is_evaluation_only"] is True, positive
        assert positive["validation"]["improvement_pct"] == 20.0, positive
        assert positive["comparison_arms"]["arms"]["default"]["role"] == "default_control", positive
        assert positive["comparison_arms"]["arms"]["fixed_locknest"]["role"] == "fixed_scx_treatment", positive
        assert positive["comparison_arms"]["arms"]["lockagent_online"]["role"] == "lockagent_online", positive
        assert positive["comparison_arms"]["arms"]["oracle_best"]["role"] == "oracle_best_evaluation_only", positive

        write(
            summary,
            {
                "batch_id": "batch_bad",
                "aggregate": [
                    {"arm": "default", "n_valid": 1, "tps_mean": 100.0, "lat_p95_ms_mean": 10.0},
                    {"arm": "fixed_locknest", "n_valid": 0, "tps_mean": None, "lat_p95_ms_mean": None},
                ],
            },
        )
        negative = module.build_validation(
            summary_path=summary,
            skill="choose_locknest_for_hot_lock_oltp",
            scheduler="scx_lockcoop",
            control_arm="default",
            treatment_arm="fixed_locknest",
            min_improvement_pct=1.0,
            max_p95_regression_pct=5.0,
        )
        assert negative["trajectory_valid"] is False, negative
        assert negative["verdict"] == "negative_or_inconclusive_evidence", negative
        assert "comparison_invalid" in negative["failure_categories"], negative
        assert negative["validation"]["environment_compatible"] is False, negative
        assert "treatment_has_no_valid_trials" in negative["reasons"], negative

        write(
            summary,
            {
                "batch_id": "batch_regression",
                "aggregate": [
                    {"arm": "default", "n_valid": 1, "tps_mean": 100.0, "lat_p95_ms_mean": 10.0},
                    {"arm": "fixed_locknest", "n_valid": 1, "tps_mean": 90.0, "lat_p95_ms_mean": 10.0},
                ],
            },
        )
        regression = module.build_validation(
            summary_path=summary,
            skill="choose_locknest_for_hot_lock_oltp",
            scheduler="scx_lockcoop",
            control_arm="default",
            treatment_arm="fixed_locknest",
            min_improvement_pct=1.0,
            max_p95_regression_pct=5.0,
        )
        assert regression["trajectory_valid"] is False, regression
        assert regression["verdict"] == "candidate_rejected_for_this_context", regression
        assert regression["failure_categories"] == ["skill_misfire"], regression
        assert regression["validation"]["environment_compatible"] is True, regression
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Checks the PolicyEngine ownership audit."""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MODULE_PATH = ROOT / "policy_audit.py"
spec = importlib.util.spec_from_file_location("policy_audit", MODULE_PATH)
assert spec and spec.loader
policy_audit = importlib.util.module_from_spec(spec)
sys.modules["policy_audit"] = policy_audit
spec.loader.exec_module(policy_audit)


def main() -> int:
    report = policy_audit.run_audit()
    assert report["status"] == "ok", report
    rules = report["policy_rules"]
    assert rules["required_rules_passed"] is True, rules
    assert rules["metadata_complete"] is True, rules
    assert rules["missing_required_rules"] == [], rules
    assert rules["mismatched_required_rules"] == [], rules
    assert rules["missing_metadata_rules"] == [], rules
    assert rules["category_counts"]["scheduler_allowlist"] >= 1, rules
    assert rules["category_counts"]["rollback"] >= 1, rules
    assert rules["category_counts"]["oracle_leakage"] >= 1, rules
    assert rules["category_counts"]["environment_compatibility"] >= 1, rules
    assert rules["category_counts"]["manifest_consistency"] >= 1, rules
    assert rules["category_counts"]["postflight_cleanup"] >= 1, rules

    branch_audit = report["local_branch_audit"]
    assert branch_audit["branch_count"] > 0, branch_audit
    assert branch_audit["unclassified_count"] == 0, branch_audit
    assert branch_audit["classification_counts"]["policy_engine_owned"] >= 1, branch_audit
    assert branch_audit["classification_counts"]["executor_mechanics"] >= 1, branch_audit
    assert branch_audit["classification_counts"]["artifact_fallback"] >= 1, branch_audit

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "policy_audit.json"
        rc = policy_audit.main(["--out", str(out)])
        assert rc == 0, rc
        assert out.exists(), out
        assert "lockagent.policy_audit.v0.1" in out.read_text(encoding="utf-8"), out

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

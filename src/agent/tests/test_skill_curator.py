#!/usr/bin/env python3
"""Local checks for scheduler skill card schema."""

from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
MODULE_PATH = KERNADAPT_ROOT / "agent" / "skill_curator.py"
REPO = KERNADAPT_ROOT / "skills" / "scheduler_skill_cards.json"
spec = importlib.util.spec_from_file_location("skill_curator", MODULE_PATH)
assert spec and spec.loader
curator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(curator)


def main() -> int:
    result = curator.validate_repository(REPO)
    assert result["valid"] is True, result
    assert len(result["skills"]) >= 5

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        repo_copy = base / "skills.json"
        shutil.copy2(REPO, repo_copy)
        decision = base / "decision.json"
        critic = base / "critic.json"
        failure = base / "failure.json"
        trajectory = base / "trajectory.json"
        decision.write_text(json.dumps({"scheduler": "scx_lockcoop"}), encoding="utf-8")
        critic.write_text(json.dumps({"valid": False}), encoding="utf-8")
        failure.write_text(json.dumps({"categories": ["environment_mismatch"]}), encoding="utf-8")
        trajectory.write_text(json.dumps({"valid": False}), encoding="utf-8")
        curation = curator.curate_run(
            repository_path=repo_copy,
            decision_path=decision,
            critic_path=critic,
            failure_path=failure,
            trajectory_path=trajectory,
            run_dir=base,
        )
        assert curation["skill_curation"]["skill"] == "choose_locknest_for_hot_lock_oltp", curation
        assert curation["skill_curation"]["verdict"] == "environment_invalid", curation
        assert curation["rejected_hypothesis"]["rejected"] is True, curation

        update = curator.update_repository_with_evidence(
            repository_path=repo_copy,
            curation=curation["skill_curation"],
            promote_after=1,
            reject_after=1,
        )
        assert update["updated"] is True, update
        assert update["repository_mutated"] is True, update
        assert update["transition"] is None, update
        updated_repo = json.loads(repo_copy.read_text(encoding="utf-8"))
        skill = next(item for item in updated_repo["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        assert skill["status"] == "candidate", skill
        assert skill["known_failures"], skill
        assert skill["known_failures"][0]["evidence_source"] == str(base), skill
        assert skill["evidence"], skill

        failure.write_text(json.dumps({"categories": ["skill_misfire"]}), encoding="utf-8")
        trajectory.write_text(json.dumps({"valid": False}), encoding="utf-8")
        negative = curator.curate_run(
            repository_path=repo_copy,
            decision_path=decision,
            critic_path=critic,
            failure_path=failure,
            trajectory_path=trajectory,
            run_dir=base,
        )
        update = curator.update_repository_with_evidence(
            repository_path=repo_copy,
            curation=negative["skill_curation"],
            promote_after=99,
            reject_after=1,
        )
        assert update["transition"] == "candidate->rejected", update
        assert update["validation_evidence"]["failure_categories"] == ["skill_misfire"], update

        shutil.copy2(REPO, repo_copy)
        failure.write_text(json.dumps({"categories": []}), encoding="utf-8")
        trajectory.write_text(json.dumps({"valid": True}), encoding="utf-8")
        positive = curator.curate_run(
            repository_path=repo_copy,
            decision_path=decision,
            critic_path=critic,
            failure_path=failure,
            trajectory_path=trajectory,
            run_dir=base,
        )
        update = curator.update_repository_with_evidence(
            repository_path=repo_copy,
            curation=positive["skill_curation"],
            promote_after=1,
            reject_after=99,
        )
        assert update["transition"] is None, update
        assert update["blocked_reason"] == "promotion_requires_same_batch_validation", update

        shutil.copy2(REPO, repo_copy)
        critic.write_text(
            json.dumps(
                {
                    "valid": True,
                    "same_batch_control": True,
                    "counterfactual_default_checked": True,
                    "primary_metric": "tps",
                    "improvement_pct": 12.5,
                }
            ),
            encoding="utf-8",
        )
        validated = curator.curate_run(
            repository_path=repo_copy,
            decision_path=decision,
            critic_path=critic,
            failure_path=failure,
            trajectory_path=trajectory,
            run_dir=base,
        )
        update = curator.update_repository_with_evidence(
            repository_path=repo_copy,
            curation=validated["skill_curation"],
            promote_after=1,
            reject_after=99,
        )
        assert update["transition"] == "candidate->validated", update
        assert update["validation_gate"]["passed"] is True, update
        assert update["repository_mutated"] is True, update
        assert update["validation_evidence"]["primary_metric"] == "tps", update
        assert Path(update["evolution_ledger"]).exists(), update
        ledger_tail = Path(update["evolution_ledger"]).read_text(encoding="utf-8").splitlines()[-1]
        ledger_record = json.loads(ledger_tail)
        assert ledger_record["primary_metric"] == "tps", ledger_record
        assert ledger_record["improvement_pct"] == 12.5, ledger_record
        assert ledger_record["environment_compatible"] is True, ledger_record
        assert ledger_record["validation_evidence"]["validation_gate_passed"] is True, ledger_record

        shutil.copy2(REPO, repo_copy)
        run_a = base / "run_1"
        run_b = base / "run_2"
        run_a.mkdir(exist_ok=True)
        run_b.mkdir(exist_ok=True)
        positive_curation = dict(validated["skill_curation"])
        positive_curation["run_dir"] = str(run_a)
        (run_a / "skill_curation.json").write_text(json.dumps(positive_curation), encoding="utf-8")
        (run_a / "same_batch_validation.json").write_text(
            json.dumps(
                {
                    "trajectory_valid": True,
                    "validation": {
                        "environment_compatible": True,
                        "primary_metric": "tps",
                        "improvement_pct": 12.5,
                    },
                    "failure_categories": [],
                }
            ),
            encoding="utf-8",
        )
        blocked_curation = dict(positive["skill_curation"])
        blocked_curation["run_dir"] = str(run_b)
        (run_b / "skill_curation.json").write_text(json.dumps(blocked_curation), encoding="utf-8")
        dry_out = base / "dry_skill_evolution_application.json"
        dry = curator.apply_evolution(
            repository_path=repo_copy,
            root=base,
            out_path=dry_out,
            promote_after=1,
            reject_after=99,
            dry_run=True,
        )
        assert dry["dry_run"] is True, dry
        assert dry["applied_count"] == 2, dry
        assert dry["transition_count"] == 1, dry
        assert dry["transitions"][0]["transition"] == "candidate->validated", dry
        assert dry["transitions"][0]["repository_mutated"] is False, dry
        assert dry["applications"][0]["validation_evidence"]["same_batch_validation_path"], dry
        repo_after_dry = json.loads(repo_copy.read_text(encoding="utf-8"))
        dry_skill = next(item for item in repo_after_dry["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        assert dry_skill["status"] == "candidate", dry_skill

        applied_out = base / "skill_evolution_application.json"
        applied = curator.apply_evolution(
            repository_path=repo_copy,
            root=base,
            out_path=applied_out,
            promote_after=1,
            reject_after=99,
            dry_run=False,
        )
        assert applied["applied_count"] == 2, applied
        assert applied["transition_count"] == 1, applied
        assert applied["transitions"][0]["repository_mutated"] is True, applied
        assert applied["transitions"][0]["primary_metric"] == "tps", applied
        repo_after_apply = json.loads(repo_copy.read_text(encoding="utf-8"))
        applied_skill = next(item for item in repo_after_apply["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        assert applied_skill["status"] == "validated", applied_skill
        assert len(applied_skill["evidence"]) == 2, applied_skill

        shutil.copy2(REPO, repo_copy)
        negative_run = base / "run_3"
        negative_run.mkdir(exist_ok=True)
        negative_curation = dict(negative["skill_curation"])
        negative_curation["run_dir"] = str(negative_run)
        (negative_run / "skill_curation.json").write_text(json.dumps(negative_curation), encoding="utf-8")
        rejected = curator.apply_evolution(
            repository_path=repo_copy,
            root=negative_run,
            out_path=negative_run / "skill_evolution_application.json",
            promote_after=99,
            reject_after=1,
            dry_run=False,
        )
        assert rejected["transition_count"] == 1, rejected
        assert rejected["transitions"][0]["transition"] == "candidate->rejected", rejected

        shutil.copy2(REPO, repo_copy)
        env_invalid_run = base / "run_env_invalid"
        env_invalid_run.mkdir(exist_ok=True)
        env_invalid_curation = dict(negative["skill_curation"])
        env_invalid_curation["run_dir"] = str(env_invalid_run)
        env_invalid_curation["verdict"] = "negative_or_inconclusive_evidence"
        env_invalid_curation["failure_categories"] = ["comparison_invalid", "environment_mismatch"]
        env_invalid_curation["validation"] = {
            "same_batch_control": True,
            "counterfactual_default_checked": True,
            "environment_compatible": False,
            "primary_metric": "tps",
            "improvement_pct": None,
        }
        (env_invalid_run / "skill_curation.json").write_text(json.dumps(env_invalid_curation), encoding="utf-8")
        env_invalid = curator.apply_evolution(
            repository_path=repo_copy,
            root=env_invalid_run,
            out_path=env_invalid_run / "skill_evolution_application.json",
            promote_after=99,
            reject_after=1,
            dry_run=False,
        )
        assert env_invalid["transition_count"] == 0, env_invalid
        env_repo = json.loads(repo_copy.read_text(encoding="utf-8"))
        env_skill = next(item for item in env_repo["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        assert env_skill["status"] == "candidate", env_skill
        assert env_skill["known_failures"], env_skill

        shutil.copy2(REPO, repo_copy)
        repo_data = json.loads(repo_copy.read_text(encoding="utf-8"))
        affinity = next(item for item in repo_data["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        affinity["status"] = "validated"
        repo_copy.write_text(json.dumps(repo_data), encoding="utf-8")
        protected_negative = dict(negative["skill_curation"])
        protected_negative["run_dir"] = str(base / "validated_negative")
        update = curator.update_repository_with_evidence(
            repository_path=repo_copy,
            curation=protected_negative,
            promote_after=99,
            reject_after=1,
        )
        assert update["transition"] is None, update
        assert update["blocked_reason"] == "validated_skill_status_protected_append_known_failure_only", update
        assert update["protected_status"] is True, update
        protected_repo = json.loads(repo_copy.read_text(encoding="utf-8"))
        protected_skill = next(item for item in protected_repo["skills"] if item["name"] == "choose_locknest_for_hot_lock_oltp")
        assert protected_skill["status"] == "validated", protected_skill
        assert protected_skill["known_failures"], protected_skill
        assert protected_skill["known_failures"][0]["evidence_source"], protected_skill
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

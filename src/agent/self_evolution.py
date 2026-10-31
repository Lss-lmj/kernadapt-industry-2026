#!/usr/bin/env python3
"""Systemic and meta self-evolution planner for LockAgent runs."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any
import time


SYSTEMIC_RULES = [
    {
        "trigger": "profile_failure",
        "action": "increase_profile_window_or_add_second_sample",
        "component": "ProfilerAgent",
    },
    {
        "trigger": "policy_reasoning_failure",
        "action": "require_strict_critic_preflight_before_guard",
        "component": "CriticAgent",
    },
    {
        "trigger": "environment_mismatch",
        "action": "enable_environment_compatibility_gate",
        "component": "RuntimeGuard",
    },
    {
        "trigger": "scheduler_startup_failure",
        "action": "temporarily_disable_scheduler_for_environment",
        "component": "ExecutorAgent",
    },
    {
        "trigger": "rollback_failure",
        "action": "force_postflight_cleanup_and_human_review",
        "component": "RuntimeGuard",
    },
]

META_TEMPLATES = {
    "fast_path_single_agent": {
        "description": "Use when recent runs have low failure rate and no guard blocks.",
        "roles": ["ProfilerAgent", "PolicyAgent", "RuntimeGuard", "ExecutorAgent"],
    },
    "guarded_policy_agent": {
        "description": "Default template with policy proposal plus guard enforcement.",
        "roles": ["ProfilerAgent", "PolicyAgent", "RuntimeGuard", "ExecutorAgent", "CriticAgent"],
    },
    "critic_required_agent": {
        "description": "Use when policy or evidence failures recur.",
        "roles": ["ProfilerAgent", "PolicyAgent", "CriticAgent", "RuntimeGuard", "ExecutorAgent", "CriticAgent"],
    },
    "curator_training_mode": {
        "description": "Use when accumulating evidence for skill promotion/rejection.",
        "roles": ["ProfilerAgent", "PolicyAgent", "CriticAgent", "RuntimeGuard", "ExecutorAgent", "CriticAgent", "CuratorAgent"],
    },
}


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def collect_run_dirs(root: Path) -> list[Path]:
    # self_evolution.py is called before online_manifest.json is written in the
    # online runner's finally block, so a single-run root must also be detected
    # from the postflight artifacts.
    if (root / "failure_attribution.json").exists() or (root / "trajectory_status.json").exists():
        return [root]
    if (root / "online_manifest.json").exists():
        return [root]
    return sorted(path for path in root.glob("run_*") if path.is_dir())


def summarize_failures(run_dirs: list[Path]) -> Counter[str]:
    counter: Counter[str] = Counter()
    for run_dir in run_dirs:
        failure = load_json(run_dir / "failure_attribution.json")
        for category in (failure or {}).get("categories", []):
            counter[category] += 1
    return counter


def choose_template(failures: Counter[str], run_count: int, state: dict[str, Any] | None = None) -> str:
    if run_count == 0:
        return (state or {}).get("selected_meta_template") or "guarded_policy_agent"
    if failures["policy_reasoning_failure"] or failures["profile_failure"]:
        return "critic_required_agent"
    if failures["skill_misfire"] or failures["scheduler_regression"]:
        return "curator_training_mode"
    if sum(failures.values()) == 0 and run_count >= 3:
        return "fast_path_single_agent"
    state_template = (state or {}).get("selected_meta_template")
    if state_template in META_TEMPLATES and sum(failures.values()) == 0:
        return state_template
    return "guarded_policy_agent"


def load_state(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {
            "schema_version": "lockagent.system_evolution_state.v0.1",
            "history": [],
            "failure_totals": {},
            "template_scores": {name: 0 for name in META_TEMPLATES},
            "template_history": [],
            "override_history": [],
            "selected_meta_template": "guarded_policy_agent",
        }
    data = load_json(path)
    if not isinstance(data, dict):
        return load_state(None)
    data.setdefault("schema_version", "lockagent.system_evolution_state.v0.1")
    data.setdefault("history", [])
    data.setdefault("failure_totals", {})
    data.setdefault("template_scores", {name: 0 for name in META_TEMPLATES})
    data.setdefault("template_history", [])
    data.setdefault("override_history", [])
    data.setdefault("selected_meta_template", "guarded_policy_agent")
    for name in META_TEMPLATES:
        data["template_scores"].setdefault(name, 0)
    return data


def update_template_scores(
    scores: dict[str, int | float],
    *,
    failures: Counter[str],
    template: str,
    run_count: int,
) -> dict[str, int | float]:
    updated = dict(scores)
    total_failures = sum(failures.values())
    if run_count == 0:
        return updated
    if total_failures == 0:
        updated["fast_path_single_agent"] = updated.get("fast_path_single_agent", 0) + 1
        updated[template] = updated.get(template, 0) + 1
    if failures["policy_reasoning_failure"] or failures["profile_failure"]:
        updated["critic_required_agent"] = updated.get("critic_required_agent", 0) + 2
        updated["fast_path_single_agent"] = updated.get("fast_path_single_agent", 0) - 1
    if failures["skill_misfire"] or failures["scheduler_regression"]:
        updated["curator_training_mode"] = updated.get("curator_training_mode", 0) + 2
    if failures["environment_mismatch"] or failures["rollback_failure"]:
        updated["guarded_policy_agent"] = updated.get("guarded_policy_agent", 0) + 1
    if failures["scheduler_startup_failure"]:
        updated["guarded_policy_agent"] = updated.get("guarded_policy_agent", 0) + 1
        updated["fast_path_single_agent"] = updated.get("fast_path_single_agent", 0) - 1
    return updated


def select_template_from_scores(scores: dict[str, int | float], fallback: str) -> str:
    if fallback in {"critic_required_agent", "curator_training_mode"}:
        return fallback
    ranked = sorted(META_TEMPLATES, key=lambda name: (scores.get(name, 0), name), reverse=True)
    best = ranked[0] if ranked else fallback
    if scores.get(best, 0) <= 0:
        return fallback
    return best


def build_state_update(
    *,
    state: dict[str, Any],
    root: Path,
    run_dirs: list[Path],
    failures: Counter[str],
    actions: list[dict[str, Any]],
    template: str,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    failure_totals = Counter(state.get("failure_totals", {}))
    failure_totals.update(failures)
    scores = update_template_scores(
        state.get("template_scores", {}),
        failures=failures,
        template=template,
        run_count=len(run_dirs),
    )
    selected = select_template_from_scores(scores, template)
    record = {
        "timestamp": now,
        "root": str(root),
        "run_count": len(run_dirs),
        "failure_distribution": dict(failures),
        "systemic_actions": actions,
        "candidate_meta_template": template,
        "selected_meta_template": selected,
    }
    history = [*state.get("history", []), record][-100:]
    template_history = [
        *state.get("template_history", []),
        {
            "timestamp": now,
            "candidate": template,
            "selected": selected,
            "scores": scores,
            "reason": "score_selected" if selected != template else "current_window_selected",
        },
    ][-100:]
    override_history = [
        *state.get("override_history", []),
        {
            "timestamp": now,
            "root": str(root),
            "overrides": overrides,
            "action_count": len(actions),
        },
    ][-100:]
    return {
        "schema_version": "lockagent.system_evolution_state.v0.1",
        "updated_at": now,
        "selected_meta_template": selected,
        "failure_totals": dict(failure_totals),
        "template_scores": scores,
        "history": history,
        "template_history": template_history,
        "override_history": override_history,
        "last_root": str(root),
        "last_actions": actions,
        "last_overrides": overrides,
    }


def build_plan(root: Path, state: dict[str, Any] | None = None) -> dict[str, Any]:
    run_dirs = collect_run_dirs(root)
    failures = summarize_failures(run_dirs)
    actions = []
    for rule in SYSTEMIC_RULES:
        count = failures[rule["trigger"]]
        if count:
            actions.append({**rule, "count": count})
    template = choose_template(failures, len(run_dirs), state=state)
    overrides = build_overrides(actions, template)
    state_update = build_state_update(
        state=state or load_state(None),
        root=root,
        run_dirs=run_dirs,
        failures=failures,
        actions=actions,
        template=template,
        overrides=overrides,
    )
    selected_template = state_update.get("selected_meta_template", template)
    if selected_template in META_TEMPLATES:
        template = str(selected_template)
        overrides["meta_template"] = template
    return {
        "schema_version": "lockagent.self_evolution.v0.1",
        "run_count": len(run_dirs),
        "failure_distribution": dict(failures),
        "systemic_actions": actions,
        "meta_template": template,
        "meta_template_detail": META_TEMPLATES[template],
        "systemic_overrides": overrides,
        "system_evolution_state": state_update,
    }


def build_overrides(actions: list[dict[str, Any]], template: str) -> dict[str, Any]:
    overrides: dict[str, Any] = {
        "schema_version": "lockagent.systemic_overrides.v0.1",
        "generated_at": None,
        "meta_template": template,
        "require_environment_compatibility": False,
        "require_strict_critic": False,
        "require_human_approval": False,
        "profile_s_multiplier": 1,
        "scheduler_disablelist": [],
        "notes": [],
    }
    for action in actions:
        name = action.get("action")
        if name == "enable_environment_compatibility_gate":
            overrides["require_environment_compatibility"] = True
            overrides["notes"].append("environment_mismatch observed; block incompatible comparisons")
        elif name == "require_strict_critic_preflight_before_guard":
            overrides["require_strict_critic"] = True
            overrides["notes"].append("policy/profile failure observed; keep CriticAgent in preflight path")
        elif name == "increase_profile_window_or_add_second_sample":
            overrides["profile_s_multiplier"] = max(int(overrides["profile_s_multiplier"]), 2)
            overrides["notes"].append("profile failure observed; extend profiling window")
        elif name == "temporarily_disable_scheduler_for_environment":
            overrides["scheduler_disablelist"].append("last_failed_scheduler")
            overrides["notes"].append("scheduler startup failure observed; disable failed scheduler for this environment")
        elif name == "force_postflight_cleanup_and_human_review":
            overrides["require_human_approval"] = True
            overrides["notes"].append("rollback failure observed; require human approval before executor")
    return overrides


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--state", type=Path)
    ap.add_argument("--state-out", type=Path)
    ap.add_argument("--overrides-out", type=Path)
    args = ap.parse_args(argv)

    state = load_state(args.state)
    plan = build_plan(args.root, state=state)
    plan["systemic_overrides"]["generated_at"] = str(args.root)
    plan["system_evolution_state"]["last_overrides"]["generated_at"] = str(args.root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    state_out = args.state_out or args.state
    if state_out:
        state_out.parent.mkdir(parents=True, exist_ok=True)
        state_out.write_text(
            json.dumps(plan["system_evolution_state"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.overrides_out:
        args.overrides_out.parent.mkdir(parents=True, exist_ok=True)
        args.overrides_out.write_text(
            json.dumps(plan["systemic_overrides"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

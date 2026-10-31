#!/usr/bin/env python3
"""LLM-first scheduler policy selector for LockAgent.

The selector deliberately avoids workload-to-scheduler rules. It builds a
structured context, calls an OpenAI-compatible Chat Completions endpoint, and
validates that the model returned a safe JSON decision.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from global_memory import read_jsonl, retrieve_policy_context


DEFAULT_API_BASE = "https://api.openai.com/v1/chat/completions"

ALLOWED_SCHEDULERS = {
    "default",
    "scx_simple",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
    "scx_hybrid",
    "scx_toponest",
    "scx_cfl",
    "scx_lcas",
}

REQUIRED_DECISION_FIELDS = {
    "scheduler",
    "confidence",
    "rationale",
    "evidence_used",
    "expected_benefit",
    "risks",
    "validation_plan",
    "rollback_conditions",
}

SYSTEM_PROMPT = """\
You are LockAgent's scheduler-selection brain.

Your job is to choose one sched_ext scheduler for the next experiment window
from the allowed action space. You must reason from the provided profiling
signals and evidence cards. Do not assume benchmark labels are enough. Do not
use post-hoc throughput results from the target run. If evidence is ambiguous,
choose a conservative scheduler and explain what validation will decide.

Return only a single JSON object with this schema:
{
  "scheduler": "one allowed scheduler",
  "confidence": 0.0,
  "rationale": "short technical explanation",
  "evidence_used": ["card ids or profile signals"],
  "expected_benefit": {
    "throughput": "expected direction",
    "latency": "expected direction",
    "fairness": "expected direction"
  },
  "risks": ["specific risks"],
  "validation_plan": ["metrics to check during probation window"],
  "rollback_conditions": ["conditions that should trigger rollback"]
}

Safety constraints:
- Never choose scx_lockcoop solely because the workload is a database.
- Treat scx_lockboost as the owner-progress-only scheduler for contended lock
  holders before unlock.
- Treat scx_lockcoop as the handoff-locality-only scheduler without owner
  progress boosting; use it to isolate placement benefits or when boost risk is high.
- Treat scx_lockcoop as the composed owner-progress + handoff-locality scheduler.
- If live profiling shows default already has high throughput and low tail
  latency, treat historical LockNest wins as stale until revalidated; choosing
  default is allowed and preferred over a risky scx intervention.
- Treat zero-progress threads or low Jain fairness as a severe risk.
- Treat affinity_parallelism_risk=high_parallelism_collapse_risk as a strong
  warning against scx_lockcoop or scx_lockcoop unless multiple live lock
  signals are severe.
- Treat micro-jitter as monitor-layer evidence; enable or recommend it only
  for high synchronized recontention with short critical sections.
- If strong affinity may collapse parallelism, prefer a less constraining
  scheduler or require a short probation window.
"""


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def build_payload(profile: dict[str, Any], experience: dict[str, Any], memory_context: dict[str, Any] | None = None) -> dict[str, Any]:
    candidates = profile.get("candidate_schedulers") or sorted(ALLOWED_SCHEDULERS)
    disallowed = sorted(set(candidates) - ALLOWED_SCHEDULERS)
    if disallowed:
        raise ValueError(f"profile contains unsupported schedulers: {disallowed}")

    experience_cards = experience.get("cards", []) if isinstance(experience, dict) else []
    experience_card_ids = [
        str(card.get("id"))
        for card in experience_cards
        if isinstance(card, dict) and card.get("id")
    ]
    if experience_card_ids:
        profile = dict(profile)
        existing_ids = profile.get("experience_card_ids", [])
        profile["experience_card_ids"] = sorted(
            set(str(item) for item in existing_ids if item) | set(experience_card_ids)
        )

    return {
        "task": "select_sched_ext_scheduler_for_next_probation_window",
        "allowed_schedulers": sorted(candidates),
        "profile": profile,
        "experience_cards": experience,
        "scheduler_skill_cards": profile.get("scheduler_skill_cards", []),
        "long_term_memory": memory_context or {
            "schema_version": "lockagent.global_memory.v0.1",
            "returned_count": 0,
            "policy_hints": {},
            "negative_examples": [],
            "positive_examples": [],
        },
        "decision_contract": {
            "must_be_online_decision": True,
            "no_oracle_best_selection": True,
            "probation_window_required": True,
            "rollback_required": True,
            "runtime_guard_enforced": True,
        },
    }


def call_chat_completions(messages: list[dict[str, str]], args: argparse.Namespace) -> str:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        raise RuntimeError("LLM_API_KEY is not set")

    model = args.model or os.environ.get("LLM_MODEL")
    if not model:
        raise RuntimeError("LLM_MODEL is not set and --model was not provided")

    api_base = args.api_base or os.environ.get("LLM_API_BASE", DEFAULT_API_BASE)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": args.temperature,
    }
    if not args.no_response_format:
        body["response_format"] = {"type": "json_object"}

    req = urllib.request.Request(
        api_base,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=args.timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"LLM API HTTP {exc.code}: {detail}") from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"unexpected LLM API response: {dump_json(data)}") from exc


def parse_decision(text: str) -> dict[str, Any]:
    raw = strip_code_fence(text)
    try:
        decision = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM did not return valid JSON: {raw}") from exc
    if not isinstance(decision, dict):
        raise ValueError("LLM decision must be a JSON object")
    return decision


def validate_decision(decision: dict[str, Any]) -> None:
    missing = sorted(REQUIRED_DECISION_FIELDS - set(decision))
    if missing:
        raise ValueError(f"decision missing required fields: {missing}")

    scheduler = decision["scheduler"]
    if scheduler not in ALLOWED_SCHEDULERS:
        raise ValueError(f"unsupported scheduler: {scheduler}")

    confidence = decision["confidence"]
    if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
        raise ValueError("confidence must be a number in [0, 1]")

    for field in ("evidence_used", "risks", "validation_plan", "rollback_conditions"):
        if not isinstance(decision[field], list) or not decision[field]:
            raise ValueError(f"{field} must be a non-empty list")

    if not isinstance(decision["rationale"], str) or not decision["rationale"].strip():
        raise ValueError("rationale must be a non-empty string")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--profile", required=True, type=Path, help="Profiling window JSON")
    ap.add_argument("--experience", required=True, type=Path, help="Experience cards JSON")
    ap.add_argument("--out", type=Path, help="Write decision JSON to this path")
    ap.add_argument("--prompt-out", type=Path, help="Write the exact LLM messages JSON before calling the API")
    ap.add_argument("--memory", type=Path, help="Optional global memory JSONL for long-term policy context")
    ap.add_argument("--memory-out", type=Path, help="Write retrieved long-term memory context")
    ap.add_argument("--memory-limit", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="Print prompt payload without calling LLM")
    ap.add_argument("--model", help="LLM model name. Defaults to LLM_MODEL")
    ap.add_argument("--api-base", help="Chat Completions URL. Defaults to LLM_API_BASE")
    ap.add_argument("--temperature", type=float, default=0.1)
    ap.add_argument("--timeout-s", type=int, default=60)
    ap.add_argument("--no-response-format", action="store_true")
    args = ap.parse_args(argv)

    profile = load_json(args.profile)
    experience = load_json(args.experience)
    memory_context = None
    if args.memory:
        memory_context = retrieve_policy_context(
            read_jsonl(args.memory),
            workload=profile.get("workload"),
            limit=args.memory_limit,
        )
        if args.memory_out:
            args.memory_out.parent.mkdir(parents=True, exist_ok=True)
            args.memory_out.write_text(dump_json(memory_context) + "\n", encoding="utf-8")
    payload = build_payload(profile, experience, memory_context)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": dump_json(payload)},
    ]

    if args.prompt_out:
        args.prompt_out.parent.mkdir(parents=True, exist_ok=True)
        args.prompt_out.write_text(dump_json({"messages": messages}) + "\n", encoding="utf-8")

    if args.dry_run:
        print("# SYSTEM PROMPT")
        print(SYSTEM_PROMPT)
        print("# USER PAYLOAD")
        print(dump_json(payload))
        return 0

    content = call_chat_completions(messages, args)
    decision = parse_decision(content)
    validate_decision(decision)

    output = dump_json(decision)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        if os.environ.get("LOCKAGENT_DEBUG"):
            raise
        raise SystemExit(1)

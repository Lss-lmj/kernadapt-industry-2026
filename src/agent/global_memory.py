#!/usr/bin/env python3
"""Append-only cross-run memory for LockAgent experiments."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lockagent.global_memory.v0.1"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else None


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def append_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def filter_records(
    records: list[dict[str, Any]],
    *,
    workload: str | None = None,
    scheduler: str | None = None,
    failure: str | None = None,
    skill: str | None = None,
    valid: bool | None = None,
) -> list[dict[str, Any]]:
    result = records
    if workload:
        result = [record for record in result if record.get("workload") == workload]
    if scheduler:
        result = [record for record in result if record.get("scheduler") == scheduler]
    if failure:
        result = [record for record in result if failure in record.get("failure_categories", [])]
    if skill:
        result = [record for record in result if record.get("skill") == skill]
    if valid is not None:
        result = [record for record in result if bool(record.get("trajectory_valid")) is valid]
    return result


def record_score(record: dict[str, Any], *, workload: str | None, scheduler: str | None, skill: str | None) -> tuple[int, str]:
    score = 0
    if workload and record.get("workload") == workload:
        score += 4
    if scheduler and record.get("scheduler") == scheduler:
        score += 3
    if skill and record.get("skill") == skill:
        score += 2
    if record.get("trajectory_valid") is False:
        score += 1
    timestamp_value = str(record.get("timestamp") or "")
    return score, timestamp_value


def retrieve_policy_context(
    records: list[dict[str, Any]],
    *,
    workload: str | None = None,
    scheduler: str | None = None,
    skill: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    candidates = filter_records(records, workload=workload, scheduler=scheduler, skill=skill)
    if workload and len(candidates) < limit:
        seen = {id(record) for record in candidates}
        for record in filter_records(records, workload=workload):
            if id(record) not in seen:
                candidates.append(record)
                seen.add(id(record))
            if len(candidates) >= limit:
                break
    if not candidates:
        candidates = records
    ranked = sorted(
        candidates,
        key=lambda record: record_score(record, workload=workload, scheduler=scheduler, skill=skill),
        reverse=True,
    )[: max(limit, 0)]
    summary = summarize(ranked)
    negative_examples = [
        {
            "run_id": record.get("run_id"),
            "workload": record.get("workload"),
            "scheduler": record.get("scheduler"),
            "skill": record.get("skill"),
            "failures": record.get("failure_categories", []),
            "primary_failure": record.get("primary_failure"),
        }
        for record in ranked
        if record.get("failure_categories") or record.get("trajectory_valid") is False
    ]
    positive_examples = [
        {
            "run_id": record.get("run_id"),
            "workload": record.get("workload"),
            "scheduler": record.get("scheduler"),
            "skill": record.get("skill"),
            "skill_verdict": record.get("skill_verdict"),
        }
        for record in ranked
        if record.get("trajectory_valid") is True and not record.get("failure_categories")
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "query": {"workload": workload, "scheduler": scheduler, "skill": skill, "limit": limit},
        "record_count": len(records),
        "matched_count": len(candidates),
        "returned_count": len(ranked),
        "summary": summary,
        "negative_examples": negative_examples,
        "positive_examples": positive_examples,
        "records": ranked,
        "policy_hints": {
            "avoid_schedulers_with_repeated_failures": summary.get("scheduler_failures", {}),
            "skill_history": summary.get("skill_counts", {}),
            "failure_history": summary.get("failure_counts", {}),
        },
    }


def build_record(run_dir: Path) -> dict[str, Any]:
    decision = load_json(run_dir / "decision.json") or {}
    failure = load_json(run_dir / "failure_attribution.json") or {}
    trajectory = load_json(run_dir / "trajectory_status.json") or {}
    skill = load_json(run_dir / "skill_curation.json") or {}
    evolution = load_json(run_dir / "self_evolution_plan.json") or {}
    status = load_json(run_dir / "execution" / "status.json") or {}
    manifest = load_json(run_dir / "online_manifest.json") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp(),
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "workload": manifest.get("workload"),
        "threads": manifest.get("threads"),
        "scheduler": decision.get("scheduler"),
        "confidence": decision.get("confidence"),
        "trajectory_valid": trajectory.get("valid"),
        "failure_categories": failure.get("categories", []),
        "primary_failure": failure.get("primary_category"),
        "skill": skill.get("skill"),
        "skill_verdict": skill.get("verdict"),
        "systemic_actions": evolution.get("systemic_actions", []),
        "meta_template": evolution.get("meta_template"),
        "executor_status": status.get("status"),
        "executor_returncode": status.get("returncode"),
        "final_sched_ext_state": status.get("final_sched_ext_state"),
    }


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    failure_counts: dict[str, int] = {}
    skill_counts: dict[str, int] = {}
    scheduler_failures: dict[str, dict[str, int]] = {}
    for record in records:
        scheduler = str(record.get("scheduler") or "unknown")
        for category in record.get("failure_categories", []):
            failure_counts[category] = failure_counts.get(category, 0) + 1
            scheduler_failures.setdefault(scheduler, {})
            scheduler_failures[scheduler][category] = scheduler_failures[scheduler].get(category, 0) + 1
        skill = record.get("skill")
        if skill:
            skill_counts[str(skill)] = skill_counts.get(str(skill), 0) + 1
    return {
        "schema_version": SCHEMA_VERSION,
        "record_count": len(records),
        "failure_counts": failure_counts,
        "skill_counts": skill_counts,
        "scheduler_failures": scheduler_failures,
        "last_run": records[-1] if records else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    append = sub.add_parser("append-run")
    append.add_argument("--memory", type=Path, required=True)
    append.add_argument("--run-dir", type=Path, required=True)
    append.add_argument("--summary-out", type=Path)
    append.add_argument("--record-out", type=Path)
    summarize_cmd = sub.add_parser("summarize")
    summarize_cmd.add_argument("--memory", type=Path, required=True)
    summarize_cmd.add_argument("--out", type=Path, required=True)
    retrieve_cmd = sub.add_parser("retrieve")
    retrieve_cmd.add_argument("--memory", type=Path, required=True)
    retrieve_cmd.add_argument("--out", type=Path, required=True)
    retrieve_cmd.add_argument("--workload")
    retrieve_cmd.add_argument("--scheduler")
    retrieve_cmd.add_argument("--skill")
    retrieve_cmd.add_argument("--limit", type=int, default=8)
    args = ap.parse_args(argv)

    if args.cmd == "append-run":
        record = build_record(args.run_dir)
        append_record(args.memory, record)
        records = read_jsonl(args.memory)
        summary = summarize(records)
        if args.summary_out:
            args.summary_out.parent.mkdir(parents=True, exist_ok=True)
            args.summary_out.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if args.record_out:
            args.record_out.parent.mkdir(parents=True, exist_ok=True)
            args.record_out.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    records = read_jsonl(args.memory)
    if args.cmd == "retrieve":
        result = retrieve_policy_context(
            records,
            workload=args.workload,
            scheduler=args.scheduler,
            skill=args.skill,
            limit=args.limit,
        )
    else:
        result = summarize(records)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

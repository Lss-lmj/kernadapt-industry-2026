#!/usr/bin/env python3
"""Normalize full agent/harness endpoint results for FlowCausal audits.

FlowCausal separates two artifacts that are easy to accidentally conflate:

* a sandbox command trace used for repeatable scheduler replay; and
* the full agent/harness endpoint result that says whether the benchmark
  instance was solved, how long it took, and which raw file proves that.

SWE-bench, SWE-agent, and OpenHands evaluation outputs can be JSON, JSONL,
single-instance dictionaries, lists of per-instance rows, or summary objects
with ``resolved_ids``/``unresolved_ids``.  This helper converts those shapes into
a small stable schema consumed by ``audit_agent_trace_bundle.py``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import sys
from typing import Any


SCHEMA = "flowcausal.full_harness_endpoint.v1"
RESOLVED_KEYS = (
    "resolved",
    "is_resolved",
    "passed",
    "success",
    "succeeded",
    "test_passed",
    "patch_correct",
    "fail_to_pass_success",
    "pass_to_pass_success",
)
STATUS_KEYS = ("status", "outcome", "result", "resolution", "exit_status")
INSTANCE_KEYS = ("instance_id", "task_id", "problem_id", "sample_id", "id")
SECONDS_KEYS = (
    "elapsed_sec",
    "duration_sec",
    "wall_time_sec",
    "agent_elapsed_sec",
    "runtime_sec",
    "time_sec",
    "total_sec",
)
MILLISECONDS_KEYS = ("elapsed_ms", "duration_ms", "wall_time_ms", "runtime_ms", "time_ms")
CONTAINER_KEYS = (
    "results",
    "instances",
    "reports",
    "predictions",
    "runs",
    "tasks",
    "evaluation",
    "eval_results",
    "outputs",
)


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_or_jsonl(path: pathlib.Path) -> tuple[Any, str, int]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        return rows, "jsonl", len(rows)
    try:
        return json.loads(text), "json", 1
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def fnum(value: Any, default: float = -1.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def bool_from_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "pass", "passed", "success", "succeeded", "resolved", "yes", "submitted"}:
            return True
        if text in {"false", "fail", "failed", "error", "errored", "unresolved", "no", "timeout"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def status_to_resolved(status: str) -> bool | None:
    return bool_from_value(status)


def first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def first_bool(row: dict[str, Any], keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        if key in row:
            parsed = bool_from_value(row.get(key))
            if parsed is not None:
                return parsed
    for key in STATUS_KEYS:
        value = row.get(key)
        if isinstance(value, str):
            parsed = status_to_resolved(value)
            if parsed is not None:
                return parsed
    return None


def first_seconds(row: dict[str, Any]) -> float | None:
    for key in SECONDS_KEYS:
        value = fnum(row.get(key), default=-1.0)
        if value >= 0.0:
            return value
    for key in MILLISECONDS_KEYS:
        value = fnum(row.get(key), default=-1.0)
        if value >= 0.0:
            return value / 1000.0
    metrics = as_dict(row.get("metrics"))
    if metrics:
        nested = first_seconds(metrics)
        if nested is not None:
            return nested
    return None


def count_items(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return len(value)
    value_num = fnum(value, default=-1.0)
    if value_num >= 0:
        return int(value_num)
    return None


def summarize_tests(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    test_status = as_dict(row.get("test_status") or row.get("tests_status") or row.get("test_result"))
    for namespace, value in test_status.items():
        if not isinstance(namespace, str):
            continue
        data = as_dict(value)
        if not data:
            continue
        prefix = namespace.lower()
        for key in ("success", "passed", "failure", "failed", "error", "skipped"):
            count = count_items(data.get(key))
            if count is not None:
                out[f"{prefix}_{key}_count"] = count
    for key in (
        "pass_to_pass",
        "fail_to_pass",
        "tests_passed",
        "tests_failed",
        "num_tests_passed",
        "num_tests_failed",
    ):
        count = count_items(row.get(key))
        if count is not None:
            out[f"{key}_count"] = count
    return out


def looks_like_instance_key(key: str) -> bool:
    return "__" in key or "/" in key or key.count("-") >= 1


def candidate_from_row(row: dict[str, Any], *, instance_hint: str = "", source_path: str = "$") -> dict[str, Any] | None:
    instance_id = first_text(row, INSTANCE_KEYS) or instance_hint
    resolved = first_bool(row, RESOLVED_KEYS)
    elapsed_sec = first_seconds(row)
    status = first_text(row, STATUS_KEYS)
    tests = summarize_tests(row)
    if not instance_id and resolved is None and elapsed_sec is None and not tests:
        return None
    return {
        "instance_id": instance_id,
        "resolved": resolved,
        "elapsed_sec": elapsed_sec,
        "status": status,
        "tests": tests,
        "source_path": source_path,
    }


def candidates_from_id_sets(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    groups = (
        ("resolved_ids", True),
        ("resolved", True),
        ("completed_ids", None),
        ("unresolved_ids", False),
        ("failed_ids", False),
        ("error_ids", False),
        ("timeout_ids", False),
    )
    seen: dict[str, dict[str, Any]] = {}
    for key, resolved in groups:
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for instance in value:
            if not isinstance(instance, str) or not instance.strip():
                continue
            row = seen.setdefault(
                instance.strip(),
                {
                    "instance_id": instance.strip(),
                    "resolved": None,
                    "elapsed_sec": None,
                    "status": "",
                    "tests": {},
                    "source_path": f"$.{key}",
                },
            )
            if resolved is not None:
                row["resolved"] = resolved
                row["status"] = "resolved" if resolved else "unresolved"
    out.extend(seen.values())
    return out


def collect_candidates(data: Any, *, source_path: str = "$", instance_hint: str = "") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for index, item in enumerate(data):
            out.extend(collect_candidates(item, source_path=f"{source_path}[{index}]", instance_hint=instance_hint))
        return out

    if not isinstance(data, dict):
        return out

    if data.get("schema") == SCHEMA:
        candidate = candidate_from_row(data, source_path=source_path)
        return [candidate] if candidate is not None else []

    out.extend(candidates_from_id_sets(data))

    direct = candidate_from_row(data, instance_hint=instance_hint, source_path=source_path)
    if direct is not None and direct.get("instance_id"):
        out.append(direct)

    for key, value in data.items():
        if isinstance(value, dict):
            child_hint = key if looks_like_instance_key(key) else ""
            child_candidate = candidate_from_row(
                value,
                instance_hint=child_hint,
                source_path=f"{source_path}.{key}",
            )
            if child_candidate is not None and child_candidate.get("instance_id"):
                out.append(child_candidate)
            if key in CONTAINER_KEYS:
                out.extend(collect_candidates(value, source_path=f"{source_path}.{key}", instance_hint=instance_hint))
        elif isinstance(value, list) and key in CONTAINER_KEYS:
            out.extend(collect_candidates(value, source_path=f"{source_path}.{key}", instance_hint=instance_hint))
    return out


def merge_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    anonymous: list[dict[str, Any]] = []
    for candidate in candidates:
        instance_id = str(candidate.get("instance_id") or "")
        if not instance_id:
            anonymous.append(candidate)
            continue
        row = merged.setdefault(instance_id, {"instance_id": instance_id, "tests": {}, "source_paths": []})
        if row.get("resolved") is None and candidate.get("resolved") is not None:
            row["resolved"] = candidate.get("resolved")
        if row.get("elapsed_sec") is None and candidate.get("elapsed_sec") is not None:
            row["elapsed_sec"] = candidate.get("elapsed_sec")
        if not row.get("status") and candidate.get("status"):
            row["status"] = candidate.get("status")
        row.setdefault("tests", {}).update(as_dict(candidate.get("tests")))
        if candidate.get("source_path"):
            row.setdefault("source_paths", []).append(candidate.get("source_path"))
    return [*merged.values(), *anonymous]


def select_candidate(candidates: list[dict[str, Any]], instance_id: str) -> dict[str, Any]:
    merged = merge_candidates(candidates)
    if instance_id:
        matches = [row for row in merged if row.get("instance_id") == instance_id]
        if not matches:
            raise SystemExit(f"instance_id {instance_id!r} not found in full-harness result")
        return matches[0]
    nonempty = [row for row in merged if row.get("instance_id")]
    if len(nonempty) == 1:
        return nonempty[0]
    if len(merged) == 1:
        return merged[0]
    raise SystemExit(
        "full-harness result contains multiple candidate instances; pass --instance-id "
        f"(candidates: {', '.join(str(row.get('instance_id') or '<anonymous>') for row in merged[:8])})"
    )


def infer_format(data: Any, input_kind: str) -> str:
    if isinstance(data, dict):
        if data.get("schema") == SCHEMA:
            return "flowcausal"
        if any(key in data for key in ("resolved_ids", "unresolved_ids", "error_ids")):
            return "swe-bench"
        if any(key in data for key in ("history", "agent_controller", "openhands_version")):
            return "openhands"
        if any(key in data for key in ("trajectory", "info", "model_stats")):
            return "swe-agent"
    if input_kind == "jsonl":
        return "openhands"
    return "generic"


def normalize_endpoint(data: Any, input_kind: str, args: argparse.Namespace, input_path: pathlib.Path) -> dict[str, Any]:
    source_format = args.format if args.format != "auto" else infer_format(data, input_kind)
    candidates = collect_candidates(data)
    selected = select_candidate(candidates, args.instance_id or "")
    created_at = args.created_at or _dt.datetime.now(_dt.UTC).isoformat()
    selected_paths = as_list(selected.get("source_paths"))
    if isinstance(selected.get("source_path"), str):
        selected_paths.append(str(selected.get("source_path")))
    payload = {
        "schema": SCHEMA,
        "source": args.source or source_format,
        "source_format": source_format,
        "instance_id": selected.get("instance_id") or args.instance_id or "",
        "resolved": selected.get("resolved"),
        "elapsed_sec": selected.get("elapsed_sec"),
        "status": selected.get("status") or "",
        "scheduler_variant": args.scheduler_variant or "",
        "created_at": created_at,
        "tests": as_dict(selected.get("tests")),
        "provenance": {
            "input_file": str(input_path),
            "input_sha256": file_sha256(input_path),
            "input_kind": input_kind,
            "raw_rows": 1 if input_kind == "json" else len(as_list(data)),
            "candidate_count": len(merge_candidates(candidates)),
            "selected_source_paths": selected_paths,
            "normalizer": "flowcausal/experiments/normalize_full_harness_result.py",
            "notes": [
                "This file records a full agent/harness endpoint, not a replay-only scheduler outcome.",
                "It should be audited together with the raw agent output, normalized trace, and replay matrix.",
            ],
        },
    }
    if payload["resolved"] is None:
        payload["provenance"]["warnings"] = ["resolved status could not be inferred"]
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize SWE-bench/SWE-agent/OpenHands full-harness endpoint results",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="raw full-harness endpoint JSON/JSONL")
    parser.add_argument("--output", required=True, help="output flowcausal.full_harness_endpoint.v1 JSON")
    parser.add_argument(
        "--format",
        choices=["auto", "swe-bench", "swe-agent", "openhands", "flowcausal", "generic"],
        default="auto",
    )
    parser.add_argument("--source", help="logical source label stored in the normalized endpoint")
    parser.add_argument("--instance-id", help="select this benchmark instance when the input contains many rows")
    parser.add_argument("--scheduler-variant", help="optional scheduler/run label, e.g. default_taskset or flowcausal_width")
    parser.add_argument("--created-at", help="override creation timestamp for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input).resolve()
    output_path = pathlib.Path(args.output).resolve()
    data, input_kind, _raw_rows = read_json_or_jsonl(input_path)
    payload = normalize_endpoint(data, input_kind, args, input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(output_path),
                "source_format": payload["source_format"],
                "instance_id": payload["instance_id"],
                "resolved": payload["resolved"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Generate a manual/oracle frontier annotation template from a trace.

The output is intentionally not active by default: each candidate row has
``enabled: false`` and is ignored by ``annotate_frontier_labels.py`` until a
human/oracle pass changes the relevant rows to ``enabled: true``.  This keeps a
review scaffold from being mistaken for frontier-correctness evidence.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
from typing import Any


TRACE_SCHEMA = "flowcausal.agent_trace.v1"
TOOL_NAME = "flowcausal/experiments/generate_frontier_annotation_template.py"


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def command_source_id(command: dict[str, Any], index: int) -> str:
    return str(command.get("id") or command.get("source_id") or command.get("event_id") or index)


def is_foreground(command: dict[str, Any]) -> bool:
    return str(command.get("stage") or "").upper() not in {"PACKAGE_BACKGROUND"}


def candidate_indices(commands: list[dict[str, Any]], policy: str) -> list[int]:
    if policy == "all":
        return list(range(len(commands)))
    if policy == "foreground":
        return [index for index, command in enumerate(commands) if is_foreground(command)]
    if policy == "critical":
        return [index for index, command in enumerate(commands) if bool(command.get("critical"))]
    if policy == "test":
        return [index for index, command in enumerate(commands) if str(command.get("stage") or "").upper() == "TEST"]
    if policy == "frontier-labeled":
        return [index for index, command in enumerate(commands) if command.get("frontier_label") is not None]
    if policy == "critical-or-test":
        return [
            index
            for index, command in enumerate(commands)
            if bool(command.get("critical")) or str(command.get("stage") or "").upper() == "TEST"
        ]
    if policy == "last-critical-or-test":
        candidates = candidate_indices(commands, "critical-or-test")
        return candidates[-1:] if candidates else []
    raise SystemExit(f"unsupported candidate policy: {policy}")


def row_for_command(command: dict[str, Any], index: int, *, source: str, enabled: bool) -> dict[str, Any]:
    existing = command.get("frontier_label")
    row: dict[str, Any] = {
        "enabled": enabled,
        "command_index": index,
        "source_id": command_source_id(command, index),
        "stage": str(command.get("stage") or ""),
        "turn": command.get("turn", 0),
        "cwd": command.get("cwd", "."),
        "cmd": str(command.get("cmd") or command.get("command") or ""),
        "label": {
            "source": source,
            "role": "foreground-gate",
            "reason": "TODO: explain why this command gates the next agent-visible transition",
            "confidence": 100,
        },
    }
    if existing is not None:
        row["existing_frontier_label"] = existing
    return row


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate disabled manual/oracle frontier annotation candidates from a normalized trace",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="input flowcausal.agent_trace.v1 JSON")
    parser.add_argument("--output", required=True, help="annotation template JSON")
    parser.add_argument(
        "--candidate-policy",
        choices=(
            "all",
            "foreground",
            "critical",
            "test",
            "frontier-labeled",
            "critical-or-test",
            "last-critical-or-test",
        ),
        default="critical-or-test",
    )
    parser.add_argument("--source", choices=("manual", "oracle"), default="manual")
    parser.add_argument(
        "--enable-candidates",
        action="store_true",
        help="emit enabled=true rows; use only when a trusted oracle has already selected these candidates",
    )
    parser.add_argument("--created-at", help="override creation timestamp for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input).resolve()
    output_path = pathlib.Path(args.output).resolve()
    trace = as_dict(json.loads(input_path.read_text(encoding="utf-8")))
    if trace.get("schema") != TRACE_SCHEMA:
        raise SystemExit(f"input trace schema must be {TRACE_SCHEMA}")
    commands = [row for row in as_list(trace.get("commands")) if isinstance(row, dict)]
    if not commands:
        raise SystemExit("input trace has no commands")
    indices = candidate_indices(commands, args.candidate_policy)
    created_at = args.created_at or _dt.datetime.now(_dt.UTC).isoformat()
    labels = [
        row_for_command(commands[index], index, source=args.source, enabled=bool(args.enable_candidates))
        for index in indices
    ]
    payload = {
        "schema": "flowcausal.frontier_annotation_template.v1",
        "created_at": created_at,
        "instance_id": str(trace.get("instance_id") or ""),
        "candidate_policy": args.candidate_policy,
        "instructions": [
            "Review candidate rows and set enabled=true only for real manual/oracle frontier labels.",
            "Rows with enabled=false are ignored by annotate_frontier_labels.py.",
            "Edit label.reason if the default TODO text is not sufficient for artifact review.",
        ],
        "labels": labels,
        "provenance": {
            "tool": TOOL_NAME,
            "input_trace": str(input_path),
            "input_trace_sha256": file_sha256(input_path),
            "command_count": len(commands),
            "candidate_count": len(labels),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "candidate_count": len(labels)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

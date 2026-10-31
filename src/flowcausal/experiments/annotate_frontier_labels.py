#!/usr/bin/env python3
"""Attach manual/oracle frontier labels to a normalized FlowCausal trace.

``normalize_agent_trace.py`` can emit heuristic labels for convenience, but
EuroSys-strength frontier correctness needs labels that are explicitly produced
by a human annotation pass or a trusted oracle.  This helper applies those
labels to ``flowcausal.agent_trace.v1`` traces while preserving annotation
provenance and refusing ambiguous/accidental overwrites by default.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
from typing import Any


TRACE_SCHEMA = "flowcausal.agent_trace.v1"
MATCH_KEYS = {"command_index", "source_id", "id", "command_id", "cmd"}
TOOL_NAME = "flowcausal/experiments/annotate_frontier_labels.py"


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json_or_jsonl(path: pathlib.Path) -> Any:
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
        return rows
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def is_heuristic_label(label: Any) -> bool:
    return "heuristic" in json.dumps(label, ensure_ascii=False, sort_keys=True).lower()


def normalize_annotations(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict):
        for key in ("labels", "annotations", "frontier_labels"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]
    if not isinstance(data, list):
        raise SystemExit("annotation file must be a JSON object/list or JSONL records")
    out: list[dict[str, Any]] = []
    for index, row in enumerate(data):
        if not isinstance(row, dict):
            raise SystemExit(f"annotation[{index}] is not an object")
        if row.get("enabled") is False:
            continue
        out.append(row)
    return out


def command_source_id(command: dict[str, Any], index: int) -> str:
    return str(command.get("id") or command.get("source_id") or command.get("event_id") or index)


def command_matches(command: dict[str, Any], index: int, annotation: dict[str, Any]) -> bool:
    matched_any = False
    if annotation.get("command_index") is not None:
        matched_any = True
        try:
            if int(annotation["command_index"]) != index:
                return False
        except (TypeError, ValueError):
            raise SystemExit(f"invalid command_index in annotation: {annotation.get('command_index')!r}")
    source = str(annotation.get("source_id") or annotation.get("id") or annotation.get("command_id") or "")
    if source:
        matched_any = True
        if command_source_id(command, index) != source:
            return False
    cmd = str(annotation.get("cmd") or "").strip()
    if cmd:
        matched_any = True
        if str(command.get("cmd") or command.get("command") or "").strip() != cmd:
            return False
    if not matched_any:
        raise SystemExit("annotation must include at least one of command_index, source_id/id, or cmd")
    return True


def find_command(commands: list[dict[str, Any]], annotation: dict[str, Any]) -> int:
    matches = [index for index, command in enumerate(commands) if command_matches(command, index, annotation)]
    if not matches:
        raise SystemExit(f"annotation did not match any command: {annotation}")
    if len(matches) > 1:
        raise SystemExit(f"annotation matched multiple commands; add command_index/source_id: {annotation}")
    return matches[0]


def annotation_label(annotation: dict[str, Any], *, default_source: str, annotator: str) -> dict[str, Any]:
    raw_label = as_dict(annotation.get("label"))
    if raw_label:
        label = dict(raw_label)
    else:
        label = {key: value for key, value in annotation.items() if key not in MATCH_KEYS and key != "label"}
    label.setdefault("source", default_source)
    label.setdefault("role", "foreground-gate")
    if annotator:
        label.setdefault("annotator", annotator)
    if annotation.get("annotation_id") is not None:
        label.setdefault("annotation_id", annotation.get("annotation_id"))
    return label


def apply_annotations(trace: dict[str, Any], annotations: list[dict[str, Any]], args: argparse.Namespace) -> list[dict[str, Any]]:
    if trace.get("schema") != TRACE_SCHEMA:
        raise SystemExit(f"input trace schema must be {TRACE_SCHEMA}")
    commands = [row for row in as_list(trace.get("commands")) if isinstance(row, dict)]
    if not commands:
        raise SystemExit("input trace has no commands")

    applied: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for annotation in annotations:
        index = find_command(commands, annotation)
        if index in seen_indices:
            raise SystemExit(f"multiple annotations target command_index={index}")
        seen_indices.add(index)
        label = annotation_label(annotation, default_source=args.source, annotator=args.annotator)
        if is_heuristic_label(label) and not args.allow_heuristic_labels:
            raise SystemExit("annotation label appears heuristic; refusing to create oracle/manual evidence")

        command = commands[index]
        existing = command.get("frontier_label")
        if existing is not None:
            if args.replace_existing:
                pass
            elif args.replace_heuristic and is_heuristic_label(existing):
                pass
            else:
                raise SystemExit(
                    f"command_index={index} already has frontier_label; "
                    "use --replace-heuristic or --replace-existing explicitly"
                )
        command["frontier_label"] = label
        if args.mark_critical:
            command["critical"] = True
        applied.append(
            {
                "command_index": index,
                "source_id": command_source_id(command, index),
                "stage": command.get("stage", ""),
                "label_source": label.get("source", ""),
                "label_role": label.get("role", ""),
            }
        )
    return applied


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attach manual/oracle frontier labels to flowcausal.agent_trace.v1 traces",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="input normalized flowcausal.agent_trace.v1 JSON")
    parser.add_argument("--annotations", required=True, help="manual/oracle labels as JSON, JSONL, or {labels:[...]}")
    parser.add_argument("--output", required=True, help="output annotated trace JSON")
    parser.add_argument("--source", choices=("manual", "oracle"), default="manual", help="default label source")
    parser.add_argument("--annotator", default="", help="optional human/tool annotator name stored in each label")
    parser.add_argument("--mark-critical", action="store_true", help="also mark annotated commands as critical")
    parser.add_argument(
        "--replace-heuristic",
        action="store_true",
        help="allow replacing existing heuristic labels generated by the normalizer",
    )
    parser.add_argument(
        "--replace-existing",
        action="store_true",
        help="allow replacing any existing frontier_label; use sparingly and keep the annotation file",
    )
    parser.add_argument(
        "--allow-heuristic-labels",
        action="store_true",
        help="allow annotation labels whose source/reason contains heuristic; not valid for L5 oracle claims",
    )
    parser.add_argument("--created-at", help="override annotation timestamp for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input).resolve()
    annotations_path = pathlib.Path(args.annotations).resolve()
    output_path = pathlib.Path(args.output).resolve()
    trace = as_dict(read_json_or_jsonl(input_path))
    annotations = normalize_annotations(read_json_or_jsonl(annotations_path))
    if not annotations:
        raise SystemExit("annotation file contains no labels")
    applied = apply_annotations(trace, annotations, args)
    created_at = args.created_at or _dt.datetime.now(_dt.UTC).isoformat()

    provenance = as_dict(trace.get("provenance"))
    history = as_list(provenance.get("frontier_label_annotations"))
    history.append(
        {
            "tool": TOOL_NAME,
            "created_at": created_at,
            "input_trace": str(input_path),
            "input_trace_sha256": file_sha256(input_path),
            "annotation_file": str(annotations_path),
            "annotation_sha256": file_sha256(annotations_path),
            "source": args.source,
            "annotator": args.annotator,
            "replace_heuristic": bool(args.replace_heuristic),
            "replace_existing": bool(args.replace_existing),
            "mark_critical": bool(args.mark_critical),
            "annotated_count": len(applied),
            "applied": applied,
        }
    )
    provenance["frontier_label_annotations"] = history
    trace["provenance"] = provenance

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "annotated_count": len(applied)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

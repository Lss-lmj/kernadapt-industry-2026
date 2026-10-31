#!/usr/bin/env python3
"""Audit a real-agent trace evidence bundle for FlowCausal.

``audit_claim_gate.py`` answers whether a matrix result passes the scheduler
claim gate.  This helper answers a different question: whether a scheduler
matrix is actually tied to a real code-agent instance with enough provenance to
support a EuroSys-style claim.

The expected bundle is:

* raw SWE-agent/OpenHands/full-agent output;
* normalized ``flowcausal.agent_trace.v1`` trace;
* full-harness endpoint result for the same benchmark instance;
* FlowCausal replay matrix summary produced from that normalized trace.

The audit is intentionally conservative.  A replay-only result may pass the
matrix claim gate and still fail this bundle audit if it lacks full-agent
endpoint alignment or a frontier oracle/manual label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import shlex
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from audit_claim_gate import audit_matrix as audit_claim_gate_matrix  # noqa: E402


STATUS_PASS = "pass"
STATUS_PARTIAL = "partial"
STATUS_FAIL = "fail"
TRACE_SCHEMA = "flowcausal.agent_trace.v1"
FULL_HARNESS_SCHEMA = "flowcausal.full_harness_endpoint.v1"


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


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def gate(name: str, status: str, evidence: list[str] | None = None, blockers: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "evidence": evidence or [],
        "blockers": blockers or [],
    }


def flatten_dicts(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        out.append(value)
        for child in value.values():
            out.extend(flatten_dicts(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(flatten_dicts(child))
    return out


def first_text(obj: Any, keys: tuple[str, ...]) -> str:
    for row in flatten_dicts(obj):
        for key in keys:
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def first_bool(obj: Any, keys: tuple[str, ...]) -> bool | None:
    for row in flatten_dicts(obj):
        for key in keys:
            if key not in row:
                continue
            value = row.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"true", "pass", "passed", "success", "succeeded", "resolved", "yes"}:
                    return True
                if text in {"false", "fail", "failed", "error", "unresolved", "no"}:
                    return False
            if isinstance(value, (int, float)):
                return bool(value)
    return None


def first_number(obj: Any, keys: tuple[str, ...]) -> float:
    for row in flatten_dicts(obj):
        for key in keys:
            if key in row:
                value = fnum(row.get(key), default=-1.0)
                if value >= 0.0:
                    return value
    return -1.0


def endpoint_summary(full_harness: Any) -> dict[str, Any]:
    if full_harness is None:
        return {}
    if isinstance(full_harness, list) and len(full_harness) == 1:
        return endpoint_summary(full_harness[0])
    if isinstance(full_harness, dict) and full_harness.get("schema") == FULL_HARNESS_SCHEMA:
        return {
            "schema": full_harness.get("schema"),
            "source": full_harness.get("source"),
            "source_format": full_harness.get("source_format"),
            "instance_id": str(full_harness.get("instance_id") or ""),
            "resolved": full_harness.get("resolved") if isinstance(full_harness.get("resolved"), bool) else None,
            "elapsed_sec": fnum(full_harness.get("elapsed_sec"), default=-1.0),
            "status": str(full_harness.get("status") or ""),
            "scheduler_variant": str(full_harness.get("scheduler_variant") or ""),
        }
    return {
        "instance_id": first_text(full_harness, ("instance_id", "task_id", "id")),
        "resolved": first_bool(
            full_harness,
            (
                "resolved",
                "passed",
                "success",
                "succeeded",
                "test_passed",
                "PASS_TO_PASS",
                "FAIL_TO_PASS",
            ),
        ),
        "elapsed_sec": first_number(
            full_harness,
            (
                "elapsed_sec",
                "duration_sec",
                "wall_time_sec",
                "agent_elapsed_sec",
                "runtime_sec",
                "time_sec",
            ),
        ),
    }


def variant_name(variant: dict[str, Any]) -> str:
    return str(variant.get("name", ""))


def variants(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    return [row for row in as_list(matrix.get("variants")) if isinstance(row, dict)]


def is_baseline_variant(name: str) -> bool:
    text = name.lower()
    return text.startswith("default") or any(token in text for token in ("eevdf", "linux"))


def is_flowcausal_variant(name: str) -> bool:
    return name.lower().startswith("flowcausal")


def baseline_classes(matrix: dict[str, Any]) -> set[str]:
    classes: set[str] = set()
    for row in variants(matrix):
        name = variant_name(row).lower()
        if is_baseline_variant(name):
            classes.add("linux_default")
        if "cgroup" in name or "cpuweight" in name or "cpumax" in name:
            classes.add("static_cgroup")
        if "nice" in name or "priority" in name:
            classes.add("nice_priority")
        if "taskset" in name or "affinity" in name or "numactl" in name:
            classes.add("static_affinity")
        if "oracle" in name:
            classes.add("oracle_frontier")
        if is_flowcausal_variant(name):
            classes.add("flowcausal")
    return classes


def outcome_metric(row: dict[str, Any]) -> tuple[str, float] | None:
    agg = as_dict(row.get("aggregate"))
    for key in ("critical_mean_sec", "total_mean_sec"):
        value = fnum(agg.get(key), default=-1.0)
        if value > 0:
            return key, value
    return None


def best_flowcausal_improvement(matrix: dict[str, Any]) -> tuple[str, float, str, float, float] | None:
    baseline_rows = [row for row in variants(matrix) if is_baseline_variant(variant_name(row))]
    flow_rows = [row for row in variants(matrix) if is_flowcausal_variant(variant_name(row))]
    baseline_metrics = [(row, outcome_metric(row)) for row in baseline_rows]
    baseline_metrics = [(row, metric) for row, metric in baseline_metrics if metric is not None]
    if not baseline_metrics:
        return None
    base_row, (metric_name, base_value) = baseline_metrics[0]
    candidates: list[tuple[str, float, str, float, float]] = []
    for row in flow_rows:
        metric = outcome_metric(row)
        if metric is None or metric[0] != metric_name:
            continue
        value = metric[1]
        improvement = (base_value - value) / base_value if base_value > 0 else 0.0
        candidates.append((variant_name(row), improvement, metric_name, base_value, value))
    return max(candidates, key=lambda item: item[1]) if candidates else None


def claim_gate_by_name(matrix_audit: dict[str, Any], name: str) -> dict[str, Any]:
    for item in as_list(matrix_audit.get("gates")):
        if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
            return item
    return {}


def report_item_by_name(report: dict[str, Any], name: str) -> dict[str, Any]:
    for section in ("chains", "gates"):
        for item in as_list(report.get(section)):
            if isinstance(item, dict) and str(item.get("name", "")).lower() == name.lower():
                return item
    return {}


def report_item_status(report: dict[str, Any], name: str) -> str:
    return str(report_item_by_name(report, name).get("status") or "")


def any_claim_gate_status(matrix_audit: dict[str, Any], names: tuple[str, ...], status: str = STATUS_PASS) -> bool:
    for name in names:
        item = claim_gate_by_name(matrix_audit, name)
        if item.get("status") == status:
            return True
    return False


def all_claim_gate_status(matrix_audit: dict[str, Any], names: tuple[str, ...], status: str = STATUS_PASS) -> bool:
    return all(claim_gate_by_name(matrix_audit, name).get("status") == status for name in names)


def stage_records(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for variant in variants(matrix):
        for trial in as_list(variant.get("trials")):
            workload = as_dict(as_dict(trial).get("workload"))
            for record in as_list(workload.get("stage_records")):
                if isinstance(record, dict):
                    out.append(record)
    return out


def workload_begins(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for variant in variants(matrix):
        for trial in as_list(variant.get("trials")):
            workload = as_dict(as_dict(trial).get("workload"))
            begin = as_dict(workload.get("workload_begin"))
            if begin:
                out.append(begin)
    return out


def matrix_command_payloads(matrix: dict[str, Any]) -> list[Any]:
    payloads: list[Any] = []
    config = as_dict(matrix.get("config"))
    if config.get("command"):
        payloads.append(config.get("command"))
    for variant in variants(matrix):
        for trial in as_list(variant.get("trials")):
            manifest = as_dict(as_dict(trial).get("manifest"))
            for key in ("command", "inner_command"):
                if manifest.get(key):
                    payloads.append(manifest.get(key))
    return payloads


def payload_tokens(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, str):
        try:
            return shlex.split(payload)
        except ValueError:
            return payload.split()
    return []


def trace_file_args(payload: Any) -> list[str]:
    tokens = payload_tokens(payload)
    out: list[str] = []
    for idx, token in enumerate(tokens):
        if token == "--trace-file" and idx + 1 < len(tokens):
            out.append(tokens[idx + 1])
        elif token.startswith("--trace-file="):
            out.append(token.split("=", 1)[1])
    return out


def path_arg_matches(path_text: str, expected: pathlib.Path, *, base_dirs: list[pathlib.Path] | None = None) -> bool:
    if not path_text:
        return False
    if path_text == str(expected):
        return True
    raw_candidate = pathlib.Path(path_text).expanduser()
    candidates = [raw_candidate]
    if not raw_candidate.is_absolute():
        candidates = [pathlib.Path.cwd() / raw_candidate]
        for base in base_dirs or []:
            candidates.append(base / raw_candidate)
    for candidate in candidates:
        try:
            if candidate.resolve() == expected.resolve():
                return True
        except OSError:
            continue
    return False


def int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def selected_harness_trial(summary: dict[str, Any], *, variant: str, trial: int) -> dict[str, Any]:
    for row in as_list(summary.get("trials")):
        row_dict = as_dict(row)
        if str(row_dict.get("variant") or "") != variant:
            continue
        if int_or_none(row_dict.get("trial")) == trial:
            return row_dict
    return {}


def provenance_input_file(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    return str(as_dict(payload.get("provenance")).get("input_file") or "")


def matrix_references_trace(
    matrix: dict[str, Any],
    normalized_path: pathlib.Path,
    *,
    matrix_path: pathlib.Path,
) -> tuple[list[str], list[str]]:
    evidence: list[str] = []
    blockers: list[str] = []
    base_dirs = [parent for parent in matrix_path.resolve().parents]
    begin_paths = [
        str(row.get("trace_file"))
        for row in workload_begins(matrix)
        if isinstance(row.get("trace_file"), str) and row.get("trace_file")
    ]
    if begin_paths:
        begin_matches = [path_arg_matches(path, normalized_path, base_dirs=base_dirs) for path in begin_paths]
        if all(begin_matches):
            evidence.append("matrix workload_begin records reference the normalized trace")
        elif any(begin_matches):
            evidence.append("at least one matrix workload_begin record references the normalized trace")
            blockers.append("some matrix workload_begin trace_file values do not match --normalized-trace")
        else:
            blockers.append("matrix workload_begin trace_file does not match --normalized-trace")

    cli_trace_args: list[str] = []
    for payload in matrix_command_payloads(matrix):
        cli_trace_args.extend(trace_file_args(payload))
    if cli_trace_args:
        cli_matches = [path_arg_matches(path, normalized_path, base_dirs=base_dirs) for path in cli_trace_args]
        if all(cli_matches):
            evidence.append("matrix command/manifest --trace-file references the normalized trace")
        elif any(cli_matches):
            evidence.append("at least one matrix command/manifest --trace-file references the normalized trace")
            blockers.append("some matrix command/manifest --trace-file values do not match --normalized-trace")
        else:
            blockers.append("matrix command/manifest --trace-file does not match --normalized-trace")

    if not begin_paths and not cli_trace_args:
        blockers.append("matrix does not expose workload_begin.trace_file or command --trace-file provenance")
    return evidence, blockers


def audit_reproducibility(
    *,
    raw_path: pathlib.Path | None,
    normalized_path: pathlib.Path,
    full_harness_path: pathlib.Path | None,
    full_harness: Any | None,
    matrix_path: pathlib.Path,
    normalized: dict[str, Any],
    matrix: dict[str, Any],
    harness_matrix_summary_path: pathlib.Path | None = None,
    harness_matrix_summary: dict[str, Any] | None = None,
    harness_matrix_audit_path: pathlib.Path | None = None,
    harness_matrix_audit: dict[str, Any] | None = None,
    victim_log_path: pathlib.Path | None = None,
    live_dir_path: pathlib.Path | None = None,
    harness_matrix_source_variant: str = "",
    harness_matrix_source_trial: int | None = None,
) -> dict[str, Any]:
    evidence: list[str] = []
    blockers: list[str] = []
    if normalized.get("schema") == TRACE_SCHEMA:
        evidence.append(f"normalized trace schema={TRACE_SCHEMA}")
    else:
        blockers.append("normalized trace schema is not flowcausal.agent_trace.v1")

    commands = [row for row in as_list(normalized.get("commands")) if isinstance(row, dict)]
    if commands:
        evidence.append(f"normalized trace has {len(commands)} replayable command(s)")
    else:
        blockers.append("normalized trace has no replayable commands")

    if normalized.get("instance_id"):
        evidence.append(f"normalized instance_id={normalized.get('instance_id')}")
    else:
        blockers.append("normalized trace lacks instance_id")

    if raw_path is not None and raw_path.exists():
        evidence.append(f"raw agent output exists: {raw_path}")
    else:
        blockers.append("raw agent output path is missing")

    if normalized_path.exists():
        evidence.append(f"normalized trace exists: {normalized_path}")
    else:
        blockers.append("normalized trace path is missing")

    provenance = as_dict(normalized.get("provenance"))
    input_file = str(provenance.get("input_file", ""))
    if input_file:
        evidence.append(f"normalized provenance input_file={input_file}")
        if raw_path is not None and pathlib.Path(input_file).resolve() != raw_path.resolve():
            blockers.append("normalized provenance input_file does not match --raw-agent-output")
    else:
        blockers.append("normalized trace lacks provenance.input_file")

    input_sha256 = str(provenance.get("input_sha256", ""))
    if raw_path is not None and raw_path.exists():
        raw_sha256 = file_sha256(raw_path)
        if input_sha256:
            if input_sha256 == raw_sha256:
                evidence.append("normalized provenance input_sha256 matches raw agent output")
            else:
                blockers.append("normalized provenance input_sha256 does not match raw agent output")
        else:
            blockers.append("normalized trace lacks provenance.input_sha256")

    if full_harness_path is not None and full_harness_path.exists():
        evidence.append(f"full harness result exists: {full_harness_path}")
        endpoint = endpoint_summary(full_harness)
        if endpoint.get("schema") == FULL_HARNESS_SCHEMA:
            evidence.append(f"full harness endpoint schema={FULL_HARNESS_SCHEMA}")
            provenance = as_dict(as_dict(full_harness).get("provenance"))
            input_sha256 = str(provenance.get("input_sha256", ""))
            input_file = str(provenance.get("input_file", ""))
            if input_file:
                evidence.append(f"full harness provenance input_file={input_file}")
            else:
                blockers.append("normalized full harness endpoint lacks provenance.input_file")
            if input_sha256 and input_file:
                source_path = pathlib.Path(input_file)
                if source_path.exists():
                    if file_sha256(source_path) == input_sha256:
                        evidence.append("full harness provenance input_sha256 matches source file")
                    else:
                        blockers.append("full harness provenance input_sha256 does not match source file")
                else:
                    evidence.append("full harness provenance source file is not locally present for checksum replay")
            elif not input_sha256:
                blockers.append("normalized full harness endpoint lacks provenance.input_sha256")
    else:
        blockers.append("full harness endpoint result path is missing")

    if matrix_path.exists():
        evidence.append(f"matrix summary exists: {matrix_path}")
    else:
        blockers.append("matrix summary path is missing")

    if harness_matrix_summary_path is not None:
        if harness_matrix_summary_path.exists():
            evidence.append(f"full-harness matrix summary exists: {harness_matrix_summary_path}")
        else:
            blockers.append("full-harness matrix summary path is missing")

    if harness_matrix_audit_path is not None:
        if harness_matrix_audit_path.exists():
            evidence.append(f"full-harness matrix audit exists: {harness_matrix_audit_path}")
            if as_dict(harness_matrix_audit).get("schema") == "flowcausal.real_agent_harness_matrix_audit.v1":
                evidence.append("full-harness matrix audit schema is recognized")
            else:
                blockers.append("full-harness matrix audit schema is missing or unexpected")
        else:
            blockers.append("full-harness matrix audit path is missing")

    if victim_log_path is not None:
        if victim_log_path.exists():
            evidence.append(f"victim log exists: {victim_log_path}")
        else:
            blockers.append("victim log path is missing")

    if live_dir_path is not None:
        if live_dir_path.is_dir():
            evidence.append(f"live scheduler directory exists: {live_dir_path}")
        else:
            blockers.append("live scheduler directory path is missing")

    has_harness_matrix_artifact = harness_matrix_summary_path is not None or harness_matrix_audit_path is not None
    should_verify_harness_source = (
        has_harness_matrix_artifact
        or bool(harness_matrix_source_variant)
        or harness_matrix_source_trial is not None
    )
    if should_verify_harness_source:
        if not harness_matrix_source_variant:
            blockers.append("harness matrix source variant is missing")
        if harness_matrix_source_trial is None:
            blockers.append("harness matrix source trial is missing")
        if harness_matrix_summary_path is None or not harness_matrix_summary_path.exists():
            blockers.append("cannot verify harness source trial without harness matrix summary")
        elif harness_matrix_source_variant and harness_matrix_source_trial is not None:
            harness_summary = as_dict(harness_matrix_summary)
            selected = selected_harness_trial(
                harness_summary,
                variant=harness_matrix_source_variant,
                trial=harness_matrix_source_trial,
            )
            if not selected:
                blockers.append(
                    f"harness matrix selected trial not found: variant={harness_matrix_source_variant} trial={harness_matrix_source_trial}"
                )
            else:
                evidence.append(
                    f"harness matrix selected trial found: variant={harness_matrix_source_variant} trial={harness_matrix_source_trial}"
                )
                base_dirs = [parent for parent in harness_matrix_summary_path.resolve().parents]
                selected_raw = str(selected.get("raw_agent_output") or "")
                if raw_path is not None and selected_raw and path_arg_matches(selected_raw, raw_path, base_dirs=base_dirs):
                    evidence.append("harness selected trial raw_agent_output matches bundle raw")
                else:
                    blockers.append("harness selected trial raw_agent_output does not match bundle raw")

                selected_endpoint = str(selected.get("full_harness_result") or "")
                source_endpoint = provenance_input_file(full_harness)
                if selected_endpoint and source_endpoint and path_arg_matches(selected_endpoint, pathlib.Path(source_endpoint), base_dirs=base_dirs):
                    evidence.append("harness selected trial full_harness_result matches normalized endpoint provenance")
                else:
                    blockers.append("harness selected trial full_harness_result does not match normalized endpoint provenance")

                selected_victim = str(selected.get("victim_log") or "")
                if selected_victim or victim_log_path is not None:
                    if victim_log_path is not None and selected_victim and path_arg_matches(selected_victim, victim_log_path, base_dirs=base_dirs):
                        evidence.append("harness selected trial victim_log matches bundle victim log")
                    else:
                        blockers.append("harness selected trial victim_log does not match bundle victim log")

                selected_live = str(selected.get("live_dir") or "")
                if selected_live or live_dir_path is not None:
                    if live_dir_path is not None and selected_live and path_arg_matches(selected_live, live_dir_path, base_dirs=base_dirs):
                        evidence.append("harness selected trial live_dir matches bundle live dir")
                    else:
                        blockers.append("harness selected trial live_dir does not match bundle live dir")

    if matrix_path.exists():
        trace_evidence, trace_blockers = matrix_references_trace(
            matrix=matrix,
            normalized_path=normalized_path,
            matrix_path=matrix_path,
        )
        evidence.extend(trace_evidence)
        blockers.extend(trace_blockers)
        begins = workload_begins(matrix)
        if begins:
            begin_instance_ids = [
                str(row.get("instance_id"))
                for row in begins
                if isinstance(row.get("instance_id"), str) and row.get("instance_id")
            ]
            normalized_instance = str(normalized.get("instance_id") or "")
            if begin_instance_ids and normalized_instance:
                if all(instance_id == normalized_instance for instance_id in begin_instance_ids):
                    evidence.append("matrix workload_begin instance_id matches normalized trace")
                else:
                    blockers.append("matrix workload_begin instance_id does not match normalized trace")
            elif normalized_instance:
                blockers.append("matrix workload_begin lacks instance_id")

            begin_command_counts = [
                int(row.get("command_count"))
                for row in begins
                if isinstance(row.get("command_count"), int)
            ]
            if begin_command_counts:
                if all(count == len(commands) for count in begin_command_counts):
                    evidence.append("matrix workload_begin command_count matches normalized trace")
                else:
                    blockers.append("matrix workload_begin command_count does not match normalized trace")
            else:
                blockers.append("matrix workload_begin lacks command_count")

    if not blockers:
        return gate("reproducibility/artifact", STATUS_PASS, evidence, blockers)
    if len(evidence) >= 3:
        return gate("reproducibility/artifact", STATUS_PARTIAL, evidence, blockers)
    return gate("reproducibility/artifact", STATUS_FAIL, evidence, blockers)


def audit_frontier_correctness(normalized: dict[str, Any], matrix: dict[str, Any]) -> dict[str, Any]:
    evidence: list[str] = []
    blockers: list[str] = []
    commands = [row for row in as_list(normalized.get("commands")) if isinstance(row, dict)]
    annotation_history = as_list(as_dict(normalized.get("provenance")).get("frontier_label_annotations"))
    labels = [cmd for cmd in commands if "frontier_label" in cmd]
    oracle_labels = [
        cmd
        for cmd in labels
        if "heuristic" not in json.dumps(cmd.get("frontier_label"), ensure_ascii=False).lower()
    ]
    if labels:
        evidence.append(f"normalized trace contains {len(labels)} frontier_label command(s)")
    else:
        blockers.append("normalized trace has no frontier_label/manual-oracle labels")

    if oracle_labels:
        evidence.append(f"{len(oracle_labels)} frontier label(s) are not marked heuristic")
    elif labels:
        blockers.append("frontier labels are heuristic only; no manual/oracle correctness evidence")

    if annotation_history:
        evidence.append(f"frontier label annotation provenance has {len(annotation_history)} entry/entries")
    elif oracle_labels:
        blockers.append("frontier labels lack annotation provenance")

    records = stage_records(matrix)
    indexed_records = [row for row in records if row.get("command_index") is not None or row.get("source_id") is not None]
    if indexed_records:
        evidence.append(f"matrix stage_records preserve command identity for {len(indexed_records)} stage event(s)")
    else:
        blockers.append("matrix stage_records do not preserve command_index/source_id")

    if oracle_labels and indexed_records and annotation_history:
        return gate("frontier correctness", STATUS_PASS, evidence, blockers)
    if labels or indexed_records:
        return gate("frontier correctness", STATUS_PARTIAL, evidence, blockers)
    return gate("frontier correctness", STATUS_FAIL, evidence, blockers)


def audit_endpoint_improvement(
    normalized: dict[str, Any],
    full_harness: Any | None,
    matrix: dict[str, Any],
    matrix_audit: dict[str, Any],
    harness_matrix_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: list[str] = []
    hard_blockers: list[str] = []
    replay_blockers: list[str] = []
    norm_instance = str(normalized.get("instance_id", ""))
    endpoint = endpoint_summary(full_harness)
    if endpoint:
        evidence.append(
            "full harness endpoint present"
            + (f": resolved={endpoint.get('resolved')}" if endpoint.get("resolved") is not None else "")
        )
        if endpoint.get("schema") == FULL_HARNESS_SCHEMA:
            evidence.append("full harness endpoint uses normalized FlowCausal schema")
        endpoint_instance = str(endpoint.get("instance_id", ""))
        if norm_instance and endpoint_instance and norm_instance != endpoint_instance:
            hard_blockers.append(f"instance_id mismatch: normalized={norm_instance} full_harness={endpoint_instance}")
    else:
        hard_blockers.append("no full harness endpoint result")

    harness_endpoint = report_item_by_name(as_dict(harness_matrix_audit), "endpoint improvement")
    if harness_endpoint.get("status") == STATUS_PASS:
        evidence.append("full-harness matrix audit passes endpoint improvement")
    elif harness_endpoint:
        replay_blockers.extend(
            f"full-harness matrix audit: {item}" for item in as_list(harness_endpoint.get("blockers"))
        )

    improvement = best_flowcausal_improvement(matrix)
    if improvement is not None:
        variant, pct, metric, base, value = improvement
        evidence.append(f"best replay outcome {variant}: {metric} {base:.4g}->{value:.4g} ({pct*100:.2f}%)")
        if pct <= 0:
            replay_blockers.append("replay matrix does not improve endpoint metric")
    else:
        replay_blockers.append("no comparable default/FlowCausal replay endpoint metric")

    outcome_gate = claim_gate_by_name(matrix_audit, "stage/episode outcome")
    if outcome_gate.get("status") == STATUS_PASS:
        evidence.append("matrix claim gate passes stage/episode outcome")
    else:
        replay_blockers.append("matrix claim gate does not pass stage/episode outcome")

    if harness_endpoint.get("status") == STATUS_PASS and not hard_blockers:
        return gate("endpoint improvement", STATUS_PASS, evidence, [])

    blockers = [*hard_blockers, *replay_blockers]
    if not blockers:
        return gate("endpoint improvement", STATUS_PASS, evidence, blockers)
    if evidence:
        return gate("endpoint improvement", STATUS_PARTIAL, evidence, blockers)
    return gate("endpoint improvement", STATUS_FAIL, evidence, blockers)


def audit_scheduler_mechanism(matrix_audit: dict[str, Any]) -> dict[str, Any]:
    evidence: list[str] = []
    blockers: list[str] = []
    required = ("controller/BPF admission", "direct CPU mechanism")
    for name in required:
        item = claim_gate_by_name(matrix_audit, name)
        if item.get("status") == STATUS_PASS:
            evidence.append(f"matrix claim gate passes {name}")
        else:
            blockers.append(f"matrix claim gate does not pass {name}")
    if not blockers:
        return gate("scheduler mechanism", STATUS_PASS, evidence, blockers)
    if evidence:
        return gate("scheduler mechanism", STATUS_PARTIAL, evidence, blockers)
    return gate("scheduler mechanism", STATUS_FAIL, evidence, blockers)


def audit_safety(matrix_audit: dict[str, Any], harness_matrix_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    harness_item = report_item_by_name(as_dict(harness_matrix_audit), "safety/fairness")
    if harness_item.get("status") == STATUS_PASS:
        return gate("safety/fairness", STATUS_PASS, ["full-harness matrix audit passes safety/fairness"], [])
    if harness_item.get("status") == STATUS_FAIL:
        return gate(
            "safety/fairness",
            STATUS_FAIL,
            as_list(harness_item.get("evidence")),
            as_list(harness_item.get("blockers")) or ["full-harness matrix audit fails safety/fairness"],
        )
    if harness_item.get("status") == STATUS_PARTIAL:
        return gate(
            "safety/fairness",
            STATUS_PARTIAL,
            as_list(harness_item.get("evidence")),
            as_list(harness_item.get("blockers")) or ["full-harness matrix audit has partial safety/fairness evidence"],
        )
    item = claim_gate_by_name(matrix_audit, "victim/fairness safety")
    if item.get("status") == STATUS_PASS:
        return gate("safety/fairness", STATUS_PASS, ["matrix claim gate passes victim/fairness safety"], [])
    return gate(
        "safety/fairness",
        STATUS_FAIL,
        as_list(item.get("evidence")),
        as_list(item.get("blockers")) or ["matrix claim gate does not pass victim/fairness safety"],
    )


def audit_baselines(matrix: dict[str, Any], harness_matrix_audit: dict[str, Any] | None = None) -> dict[str, Any]:
    harness_item = report_item_by_name(as_dict(harness_matrix_audit), "baseline comparison")
    if harness_item.get("status") == STATUS_PASS:
        return gate("baseline comparison", STATUS_PASS, ["full-harness matrix audit passes baseline comparison"], [])
    classes = baseline_classes(matrix)
    evidence = [f"baseline classes present: {', '.join(sorted(classes))}"] if classes else []
    blockers: list[str] = []
    required_min = {"linux_default", "flowcausal"}
    for item in sorted(required_min - classes):
        blockers.append(f"missing required comparison class: {item}")

    submission_required = {"static_cgroup", "nice_priority", "static_affinity", "oracle_frontier"}
    missing_submission = sorted(submission_required - classes)
    if missing_submission:
        blockers.append("missing EuroSys-strength baseline(s): " + ", ".join(missing_submission))

    if not blockers:
        return gate("baseline comparison", STATUS_PASS, evidence, blockers)
    if required_min.issubset(classes):
        return gate("baseline comparison", STATUS_PARTIAL, evidence, blockers)
    return gate("baseline comparison", STATUS_FAIL, evidence, blockers)


def load_optional(path_text: str | None) -> tuple[pathlib.Path | None, Any | None]:
    if not path_text:
        return None, None
    path = pathlib.Path(path_text).resolve()
    if not path.exists():
        return path, None
    return path, read_json_or_jsonl(path)


def audit_bundle(
    *,
    raw_agent_output: str | None,
    normalized_trace: str,
    full_harness_result: str | None,
    matrix_summary: str,
    harness_matrix_summary: str | None = None,
    harness_matrix_audit: str | None = None,
    victim_log: str | None = None,
    live_dir: str | None = None,
    harness_matrix_source_variant: str = "",
    harness_matrix_source_trial: int | None = None,
) -> dict[str, Any]:
    raw_path, raw_data = load_optional(raw_agent_output)
    normalized_path = pathlib.Path(normalized_trace).resolve()
    matrix_path = pathlib.Path(matrix_summary).resolve()
    full_harness_path, full_harness = load_optional(full_harness_result)
    harness_matrix_summary_path, harness_matrix_summary_payload = load_optional(harness_matrix_summary)
    harness_matrix_audit_path, harness_matrix_audit_payload = load_optional(harness_matrix_audit)
    victim_log_path = pathlib.Path(victim_log).resolve() if victim_log else None
    live_dir_path = pathlib.Path(live_dir).resolve() if live_dir else None
    normalized = as_dict(read_json_or_jsonl(normalized_path))
    matrix = as_dict(read_json_or_jsonl(matrix_path))
    matrix_audit = as_dict(matrix.get("claim_gate_audit")) or audit_claim_gate_matrix(matrix)
    harness_matrix_audit_dict = as_dict(harness_matrix_audit_payload)

    gates = [
        audit_frontier_correctness(normalized, matrix),
        audit_endpoint_improvement(normalized, full_harness, matrix, matrix_audit, harness_matrix_audit_dict),
        audit_scheduler_mechanism(matrix_audit),
        audit_safety(matrix_audit, harness_matrix_audit_dict),
        audit_baselines(matrix, harness_matrix_audit_dict),
        audit_reproducibility(
            raw_path=raw_path,
            normalized_path=normalized_path,
            full_harness_path=full_harness_path,
            full_harness=full_harness,
            matrix_path=matrix_path,
            normalized=normalized,
            matrix=matrix,
            harness_matrix_summary_path=harness_matrix_summary_path,
            harness_matrix_summary=as_dict(harness_matrix_summary_payload),
            harness_matrix_audit_path=harness_matrix_audit_path,
            harness_matrix_audit=harness_matrix_audit_dict,
            victim_log_path=victim_log_path,
            live_dir_path=live_dir_path,
            harness_matrix_source_variant=harness_matrix_source_variant,
            harness_matrix_source_trial=harness_matrix_source_trial,
        ),
    ]

    pass_count = sum(1 for item in gates if item["status"] == STATUS_PASS)
    partial_count = sum(1 for item in gates if item["status"] == STATUS_PARTIAL)
    fail_count = sum(1 for item in gates if item["status"] == STATUS_FAIL)
    if fail_count == 0 and pass_count == len(gates):
        status = STATUS_PASS
        highest = "L5-ready evidence bundle"
    elif pass_count or partial_count:
        status = STATUS_PARTIAL
        highest = "partial trace-replay evidence; not L5"
    else:
        status = STATUS_FAIL
        highest = "insufficient real-agent evidence"

    return {
        "schema": "flowcausal.agent_trace_bundle_audit.v1",
        "status": status,
        "highest_supported_scope": highest,
        "gates": gates,
        "inputs": {
            "raw_agent_output": str(raw_path) if raw_path is not None else "",
            "normalized_trace": str(normalized_path),
            "full_harness_result": str(full_harness_path) if full_harness_path is not None else "",
            "matrix_summary": str(matrix_path),
            "harness_matrix_summary": str(harness_matrix_summary_path) if harness_matrix_summary_path is not None else "",
            "harness_matrix_audit": str(harness_matrix_audit_path) if harness_matrix_audit_path is not None else "",
            "victim_log": str(victim_log_path) if victim_log_path is not None else "",
            "live_dir": str(live_dir_path) if live_dir_path is not None else "",
            "harness_matrix_source_variant": harness_matrix_source_variant,
            "harness_matrix_source_trial": harness_matrix_source_trial if harness_matrix_source_trial is not None else "",
        },
        "normalized_trace": {
            "schema": normalized.get("schema"),
            "source": normalized.get("source"),
            "source_format": normalized.get("source_format"),
            "instance_id": normalized.get("instance_id"),
            "command_count": len(as_list(normalized.get("commands"))),
        },
        "full_harness_endpoint": endpoint_summary(full_harness),
        "matrix_claim_gate": {
            "highest_supported_level": matrix_audit.get("highest_supported_level"),
        },
        "harness_matrix_audit": {
            "status": harness_matrix_audit_dict.get("status", ""),
            "highest_supported_scope": harness_matrix_audit_dict.get("highest_supported_scope", ""),
        },
        "notes": [
            "pass means the bundle is structurally ready for a strong claim; it does not guarantee statistical significance",
            "heuristic frontier labels are treated as partial evidence, not oracle correctness",
        ],
    }


def markdown_escape(value: Any) -> str:
    text = str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def markdown_list(items: list[Any]) -> list[str]:
    if not items:
        return ["- none"]
    return [f"- {markdown_escape(item)}" for item in items]


def render_markdown_report(report: dict[str, Any]) -> str:
    """Render a compact human-readable audit report.

    The JSON output remains the machine-readable source of truth.  This report
    is deliberately conservative: it repeats blockers next to each gate so a
    partial bundle cannot be mistaken for endpoint evidence in paper writing.
    """

    lines: list[str] = [
        "# FlowCausal agent trace bundle audit",
        "",
        f"- status: `{markdown_escape(report.get('status', ''))}`",
        f"- highest supported scope: `{markdown_escape(report.get('highest_supported_scope', ''))}`",
        "",
        "## Inputs",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    for key, value in as_dict(report.get("inputs")).items():
        if value not in ("", None):
            lines.append(f"| `{markdown_escape(key)}` | `{markdown_escape(value)}` |")

    normalized = as_dict(report.get("normalized_trace"))
    endpoint = as_dict(report.get("full_harness_endpoint"))
    lines.extend(
        [
            "",
            "## Workload identity",
            "",
            "| Field | Value |",
            "| --- | --- |",
            f"| normalized instance | `{markdown_escape(normalized.get('instance_id', ''))}` |",
            f"| normalized command count | `{markdown_escape(normalized.get('command_count', ''))}` |",
            f"| endpoint instance | `{markdown_escape(endpoint.get('instance_id', ''))}` |",
            f"| endpoint resolved | `{markdown_escape(endpoint.get('resolved', ''))}` |",
            f"| endpoint elapsed_sec | `{markdown_escape(endpoint.get('elapsed_sec', ''))}` |",
            "",
            "## Gate summary",
            "",
            "| Gate | Status | Evidence count | Blocker count |",
            "| --- | --- | ---: | ---: |",
        ]
    )
    for item in as_list(report.get("gates")):
        gate_item = as_dict(item)
        lines.append(
            "| "
            f"{markdown_escape(gate_item.get('name', ''))} | "
            f"`{markdown_escape(gate_item.get('status', ''))}` | "
            f"{len(as_list(gate_item.get('evidence')))} | "
            f"{len(as_list(gate_item.get('blockers')))} |"
        )

    lines.extend(["", "## Gate details", ""])
    for item in as_list(report.get("gates")):
        gate_item = as_dict(item)
        name = markdown_escape(gate_item.get("name", ""))
        status = markdown_escape(gate_item.get("status", ""))
        lines.extend(
            [
                f"### {name}",
                "",
                f"status: `{status}`",
                "",
                "Evidence:",
                "",
                *markdown_list(as_list(gate_item.get("evidence"))),
                "",
                "Blockers:",
                "",
                *markdown_list(as_list(gate_item.get("blockers"))),
                "",
            ]
        )

    notes = as_list(report.get("notes"))
    if notes:
        lines.extend(["## Notes", ""])
        lines.extend(markdown_list(notes))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit raw/full-agent output, normalized trace, full harness endpoint, and replay matrix as one evidence bundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--raw-agent-output", help="original SWE-agent/OpenHands/full-agent output file")
    parser.add_argument("--normalized-trace", required=True, help="flowcausal.agent_trace.v1 JSON file")
    parser.add_argument("--full-harness-result", help="SWE-bench/OpenHands/SWE-agent endpoint result JSON/JSONL")
    parser.add_argument("--matrix-summary", required=True, help="FlowCausal replay matrix-summary.json")
    parser.add_argument("--harness-matrix-summary", help="full-harness matrix summary that selected this bundle's raw/endpoint trial")
    parser.add_argument("--harness-matrix-audit", help="audit_real_agent_harness_matrix.py JSON for the full-harness matrix")
    parser.add_argument("--victim-log", help="optional full-harness/live victim.log archived with the selected trial")
    parser.add_argument("--live-dir", help="optional live scheduler directory archived with the selected trial")
    parser.add_argument("--harness-matrix-source-variant", default="", help="variant of the selected full-harness matrix trial")
    parser.add_argument("--harness-matrix-source-trial", type=int, help="trial number of the selected full-harness matrix trial")
    parser.add_argument("--output", help="write audit JSON here instead of stdout only")
    parser.add_argument("--markdown-output", help="write a human-readable Markdown audit report here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = audit_bundle(
        raw_agent_output=args.raw_agent_output,
        normalized_trace=args.normalized_trace,
        full_harness_result=args.full_harness_result,
        matrix_summary=args.matrix_summary,
        harness_matrix_summary=args.harness_matrix_summary,
        harness_matrix_audit=args.harness_matrix_audit,
        victim_log=args.victim_log,
        live_dir=args.live_dir,
        harness_matrix_source_variant=args.harness_matrix_source_variant,
        harness_matrix_source_trial=args.harness_matrix_source_trial,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        markdown_output = pathlib.Path(args.markdown_output).resolve()
        markdown_output.parent.mkdir(parents=True, exist_ok=True)
        markdown_output.write_text(render_markdown_report(report), encoding="utf-8")
    print(text, end="")
    # ``partial`` is the common research-state outcome and should be easy to
    # archive in scripts.  A nonzero exit is reserved for malformed input that
    # raises before a report is produced.
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

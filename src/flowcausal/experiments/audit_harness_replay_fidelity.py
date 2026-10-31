#!/usr/bin/env python3
"""Audit whether a HarnessReplayBench replay followed its normalized trace.

This is a structural fidelity check.  It does not prove CPU/runtime fidelity
against the original live agent run; it verifies that a replay log executed the
same command identities, stages, and expected returncode semantics recorded in
``flowcausal.agent_trace.v1`` or the packaged
``harness_replaybench.trace.v1`` schema.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


TRACE_SCHEMAS = {"flowcausal.agent_trace.v1", "harness_replaybench.trace.v1"}
AUDIT_SCHEMA = "flowcausal.harness_replay_fidelity.v1"


def iter_json_records(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def load_trace(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") not in TRACE_SCHEMAS:
        expected = ", ".join(sorted(TRACE_SCHEMAS))
        raise SystemExit(f"trace schema is not one of {expected}: {path}")
    commands = data.get("commands")
    if not isinstance(commands, list):
        raise SystemExit(f"trace has no commands list: {path}")
    return data


def expected_returncode(command: dict[str, Any]) -> int | None:
    if "observed_returncode" not in command:
        return 0 if not command.get("allow_failure") else None
    try:
        value = int(command["observed_returncode"])
    except (TypeError, ValueError):
        return None
    # mini-swe-agent can use negative synthetic values around final submission;
    # those are provenance hints rather than POSIX shell return codes.
    if value < 0:
        return None
    return value


def int_field(record: dict[str, Any], key: str, default: int = 0) -> int:
    value = record.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def audit_trace_log(trace_path: pathlib.Path, log_path: pathlib.Path) -> dict[str, Any]:
    trace = load_trace(trace_path)
    commands = [cmd for cmd in trace["commands"] if isinstance(cmd, dict)]
    records = iter_json_records(log_path)
    stage_ends = [record for record in records if record.get("type") == "agent_stage_end"]
    metrics = [record for record in records if record.get("type") == "flowcausal_metric"]
    workload_ends = [record for record in records if record.get("type") == "workload_end"]

    blockers: list[str] = []
    warnings: list[str] = []
    mismatches: list[dict[str, Any]] = []
    if len(stage_ends) != len(commands):
        blockers.append(f"command count mismatch: trace={len(commands)} replay_stage_end={len(stage_ends)}")

    for index, command in enumerate(commands):
        if index >= len(stage_ends):
            mismatches.append({"index": index, "reason": "missing_replay_stage_end"})
            continue
        stage_end = stage_ends[index]
        expected_stage = str(command.get("stage") or "")
        actual_stage = str(stage_end.get("stage") or "")
        expected_source = str(command.get("id") or command.get("event_id") or index)
        actual_source = str(stage_end.get("source_id") or "")
        actual_index = int_field(stage_end, "command_index", -1)
        expected_rc = expected_returncode(command)
        actual_rc = int_field(stage_end, "returncode", 0)
        item: dict[str, Any] = {"index": index}
        failed = False
        if actual_index != index:
            item["command_index"] = {"expected": index, "actual": actual_index}
            failed = True
        if expected_stage != actual_stage:
            item["stage"] = {"expected": expected_stage, "actual": actual_stage}
            failed = True
        if expected_source != actual_source:
            item["source_id"] = {"expected": expected_source, "actual": actual_source}
            failed = True
        if expected_rc is not None and expected_rc != actual_rc:
            item["returncode"] = {"expected": expected_rc, "actual": actual_rc}
            failed = True
        if expected_rc is None and actual_rc != 0:
            warnings.append(f"command {index} returned {actual_rc} under allow_failure/unknown expected rc")
        if failed:
            mismatches.append(item)

    if mismatches:
        blockers.append(f"{len(mismatches)} command identity/stage/returncode mismatches")
    if not metrics:
        blockers.append("missing flowcausal_metric critical_sec")
    if not workload_ends:
        blockers.append("missing workload_end")
    elif int_field(workload_ends[-1], "returncode", 1) != 0:
        blockers.append(f"workload_end returncode is {workload_ends[-1].get('returncode')}")

    replay_nonzero = [
        {
            "index": int_field(record, "command_index", -1),
            "stage": record.get("stage"),
            "returncode": record.get("returncode"),
            "source_id": record.get("source_id"),
        }
        for record in stage_ends
        if int_field(record, "returncode", 0) != 0
    ]
    observed_nonzero = [
        {
            "index": idx,
            "stage": command.get("stage"),
            "observed_returncode": command.get("observed_returncode"),
            "source_id": command.get("id"),
        }
        for idx, command in enumerate(commands)
        if isinstance(command.get("observed_returncode"), int) and int(command["observed_returncode"]) > 0
    ]
    return {
        "trace_file": str(trace_path),
        "log_file": str(log_path),
        "instance_id": trace.get("instance_id", ""),
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "warnings": warnings,
        "command_count": len(commands),
        "stage_end_count": len(stage_ends),
        "critical_metric_count": len(metrics),
        "workload_returncode": workload_ends[-1].get("returncode") if workload_ends else None,
        "replay_nonzero": replay_nonzero,
        "observed_nonzero": observed_nonzero,
        "mismatches": mismatches,
    }


def audit_mix_summary(path: pathlib.Path) -> dict[str, Any]:
    summary = json.loads(path.read_text(encoding="utf-8"))
    cases = summary.get("cases")
    if not isinstance(cases, list):
        raise SystemExit(f"mix summary has no cases list: {path}")
    case_audits = []
    for case in cases:
        if not isinstance(case, dict):
            continue
        trace_file = case.get("trace_file")
        stdout = case.get("stdout")
        if not trace_file or not stdout:
            case_audits.append(
                {
                    "instance_id": case.get("instance_id", ""),
                    "status": "fail",
                    "blockers": ["case missing trace_file or stdout"],
                }
            )
            continue
        case_audits.append(audit_trace_log(pathlib.Path(str(trace_file)), pathlib.Path(str(stdout))))
    blockers = [
        f"{audit.get('instance_id')}: {blocker}"
        for audit in case_audits
        for blocker in audit.get("blockers", [])
    ]
    return {
        "schema": AUDIT_SCHEMA,
        "source": "mix-summary",
        "mix_summary": str(path),
        "status": "pass" if not blockers else "fail",
        "blockers": blockers,
        "case_count": len(case_audits),
        "case_audits": case_audits,
    }


def write_markdown(path: pathlib.Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Harness replay fidelity audit",
        "",
        f"- status: `{payload['status']}`",
        f"- case_count: {payload.get('case_count', 1)}",
        "",
    ]
    if payload.get("blockers"):
        lines.append("## Blockers")
        lines.append("")
        for blocker in payload["blockers"]:
            lines.append(f"- {blocker}")
        lines.append("")
    lines.extend(["| instance | status | commands | stage_end | nonzero replay | warnings |", "|---|---|---:|---:|---:|---:|"])
    audits = payload.get("case_audits") if "case_audits" in payload else [payload]
    for audit in audits:
        lines.append(
            f"| `{audit.get('instance_id', '')}` | `{audit.get('status', '')}` | "
            f"{audit.get('command_count', '')} | {audit.get('stage_end_count', '')} | "
            f"{len(audit.get('replay_nonzero', []))} | {len(audit.get('warnings', []))} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit structural fidelity of HarnessReplayBench replay logs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--trace", help="single normalized trace JSON")
    parser.add_argument("--log", help="single workload.log")
    parser.add_argument("--mix-summary", help="harness-replay-mix-summary.json")
    parser.add_argument("--output", required=True, help="audit JSON output")
    parser.add_argument("--markdown-output", help="optional Markdown output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mix_summary:
        payload = audit_mix_summary(pathlib.Path(args.mix_summary).resolve())
    else:
        if not args.trace or not args.log:
            raise SystemExit("either --mix-summary or both --trace and --log are required")
        single = audit_trace_log(pathlib.Path(args.trace).resolve(), pathlib.Path(args.log).resolve())
        payload = {"schema": AUDIT_SCHEMA, "source": "single-log", **single}
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        write_markdown(pathlib.Path(args.markdown_output).resolve(), payload)
    print(json.dumps({"status": payload["status"], "output": str(output)}, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

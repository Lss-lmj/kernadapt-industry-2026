#!/usr/bin/env python3
"""Summarize HarnessReplayBench traces, replay mixes, and replay matrices.

The structural fidelity audit answers whether a replay followed the same
command path.  This script answers a more basic paper-writing question: what
kind of harness workload are we actually studying?

It summarizes normalized traces, optional raw agent trajectories, and optional
HarnessReplayBench mix/matrix summaries without upgrading them into performance
claims.  Timing fields are reported only when the artifacts really contain
timestamps or command durations; otherwise the summary says so explicitly.
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
import hashlib
import json
import pathlib
import statistics
from typing import Any


SCHEMA = "flowcausal.harness_replaybench_summary.v1"
TIMESTAMP_KEYS = {
    "timestamp",
    "time",
    "time_sec",
    "created_at",
    "started_at",
    "start_time",
    "ended_at",
    "end_time",
    "finished_at",
    "completed_at",
    "ts",
}
STAGE_ORDER = [
    "INSPECT",
    "EDIT",
    "BUILD",
    "TEST",
    "RETRY_PROBE",
    "PACKAGE_BACKGROUND",
    "SHELL",
]


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def fnum(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def parse_timestamp(value: Any) -> float | None:
    number = fnum(value)
    if number is not None:
        # Accept seconds, milliseconds, microseconds, or nanoseconds.
        if number > 10_000_000_000_000_000:
            return number / 1_000_000_000.0
        if number > 10_000_000_000_000:
            return number / 1_000_000.0
        if number > 10_000_000_000:
            return number / 1_000.0
        return number
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.UTC)
    return parsed.timestamp()


def walk_timestamps(value: Any, out: list[float], *, depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in TIMESTAMP_KEYS:
                ts = parse_timestamp(item)
                if ts is not None:
                    out.append(ts)
            if isinstance(item, (dict, list)):
                walk_timestamps(item, out, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                walk_timestamps(item, out, depth=depth + 1)


def load_raw(path: pathlib.Path) -> Any:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
        return rows
    return json.loads(text)


def raw_timing_summary(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None:
        return {"raw_path": None, "timestamp_count": 0, "llm_timestamp_span_sec": None}
    if not path.exists():
        return {
            "raw_path": str(path),
            "raw_exists": False,
            "timestamp_count": 0,
            "llm_timestamp_span_sec": None,
        }
    data = load_raw(path)
    timestamps: list[float] = []
    walk_timestamps(data, timestamps)
    summary: dict[str, Any] = {
        "raw_path": str(path),
        "raw_exists": True,
        "raw_sha256": file_sha256(path),
        "timestamp_count": len(timestamps),
        "llm_timestamp_span_sec": None,
    }
    if timestamps:
        summary.update(
            {
                "timestamp_min_sec": min(timestamps),
                "timestamp_max_sec": max(timestamps),
                "llm_timestamp_span_sec": max(timestamps) - min(timestamps),
            }
        )
    return summary


def command_duration(record: dict[str, Any]) -> float | None:
    for key in ("duration_sec", "elapsed_sec"):
        value = fnum(record.get(key))
        if value is not None:
            return value
    start = parse_timestamp(record.get("start_time", record.get("started_at")))
    end = parse_timestamp(record.get("end_time", record.get("ended_at", record.get("finished_at"))))
    if start is not None and end is not None and end >= start:
        return end - start
    return None


def resolve_raw_path(trace_path: pathlib.Path, trace: dict[str, Any], raw_root: pathlib.Path | None) -> pathlib.Path | None:
    provenance = trace.get("provenance") if isinstance(trace.get("provenance"), dict) else {}
    text = provenance.get("input_file")
    if not isinstance(text, str) or not text:
        return None
    candidate = pathlib.Path(text)
    if candidate.exists():
        return candidate
    if raw_root is not None:
        rooted = raw_root / candidate.name
        if rooted.exists():
            return rooted
    rel = trace_path.parent / candidate
    if rel.exists():
        return rel
    return candidate


def sorted_counter(counter: collections.Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items(), key=lambda item: (-item[1], item[0])))


def summarize_trace(path: pathlib.Path, *, raw_root: pathlib.Path | None) -> dict[str, Any]:
    trace = read_json(path)
    if not isinstance(trace, dict) or not isinstance(trace.get("commands"), list):
        raise SystemExit(f"{path} must be a normalized trace object with commands")
    commands = [cmd for cmd in trace.get("commands", []) if isinstance(cmd, dict)]
    command_count = len(commands)
    stage_counts: collections.Counter[str] = collections.Counter()
    returncode_counts: collections.Counter[str] = collections.Counter()
    turn_values: set[str] = set()
    durations: list[float] = []
    for record in commands:
        stage_counts[str(record.get("stage", "UNKNOWN"))] += 1
        rc = record.get("observed_returncode", record.get("returncode", "missing"))
        returncode_counts[str(rc)] += 1
        if "turn" in record:
            turn_values.add(str(record.get("turn")))
        duration = command_duration(record)
        if duration is not None:
            durations.append(duration)

    raw_path = resolve_raw_path(path, trace, raw_root)
    raw_timing = raw_timing_summary(raw_path)
    tool_execution_sec = sum(durations) if durations else None
    llm_span = raw_timing.get("llm_timestamp_span_sec")
    tool_execution_share = None
    if isinstance(tool_execution_sec, (int, float)) and isinstance(llm_span, (int, float)) and llm_span > 0:
        tool_execution_share = float(tool_execution_sec) / float(llm_span)
    if raw_timing.get("timestamp_count", 0) <= 0:
        timing_status = "missing_timestamps"
    elif tool_execution_sec is None:
        timing_status = "missing_tool_durations"
    else:
        timing_status = "pass"

    nonzero = [
        record
        for record in commands
        if str(record.get("observed_returncode", record.get("returncode", 0))) not in {"0", "missing", "None"}
    ]
    provenance = trace.get("provenance") if isinstance(trace.get("provenance"), dict) else {}
    ratios = {
        f"{stage.lower()}_command_ratio": (stage_counts.get(stage, 0) / command_count if command_count else 0.0)
        for stage in STAGE_ORDER
    }
    summary = {
        "instance_id": trace.get("instance_id") or path.stem,
        "trace_file": str(path),
        "trace_sha256": file_sha256(path),
        "schema": trace.get("schema"),
        "source": trace.get("source"),
        "source_format": trace.get("source_format"),
        "command_count": command_count,
        "stage_counts": sorted_counter(stage_counts),
        "stage_ratios": ratios,
        "observed_returncode_counts": sorted_counter(returncode_counts),
        "observed_nonzero_command_count": len(nonzero),
        "allow_failure_count": sum(1 for record in commands if bool(record.get("allow_failure", False))),
        "critical_command_count": sum(1 for record in commands if bool(record.get("critical", False))),
        "frontier_label_count": sum(1 for record in commands if record.get("frontier_label")),
        "rewritten_command_count": sum(1 for record in commands if record.get("command_rewrite")),
        "turn_count": len(turn_values),
        "timing_status": timing_status,
        "tool_command_duration_count": len(durations),
        "tool_execution_sec": tool_execution_sec,
        "tool_execution_share_of_raw_span": tool_execution_share,
        "raw_timing": raw_timing,
        "provenance": {
            "input_file": provenance.get("input_file"),
            "input_sha256": provenance.get("input_sha256"),
            "critical_policy": provenance.get("critical_policy"),
            "normalizer": provenance.get("normalizer"),
        },
    }
    return summary


def merge_stage_seconds(cases: list[dict[str, Any]]) -> dict[str, float]:
    out: collections.Counter[str] = collections.Counter()
    for case in cases:
        parsed = case.get("parsed") if isinstance(case.get("parsed"), dict) else {}
        stage_seconds = parsed.get("stage_seconds") if isinstance(parsed.get("stage_seconds"), dict) else {}
        for stage, value in stage_seconds.items():
            number = fnum(value)
            if number is not None:
                out[str(stage)] += number
    return dict(sorted(out.items()))


def summarize_matrix(path: pathlib.Path) -> dict[str, Any]:
    matrix = read_json(path)
    if not isinstance(matrix, dict) or not isinstance(matrix.get("variants"), list):
        raise SystemExit(f"{path} must be a HarnessReplayBench matrix summary")
    variants: list[dict[str, Any]] = []
    for variant in matrix.get("variants", []):
        if not isinstance(variant, dict):
            continue
        stage_totals_by_trial: list[dict[str, float]] = []
        command_counts: list[float] = []
        for trial in variant.get("trials", []):
            if not isinstance(trial, dict) or trial.get("status") != "pass":
                continue
            cases = [case for case in trial.get("cases", []) if isinstance(case, dict)]
            stage_totals_by_trial.append(merge_stage_seconds(cases))
            for case in cases:
                parsed = case.get("parsed") if isinstance(case.get("parsed"), dict) else {}
                count = fnum(parsed.get("command_count"))
                if count is not None:
                    command_counts.append(count)
        all_stages = sorted({stage for row in stage_totals_by_trial for stage in row})
        stage_seconds_mean = {
            stage: mean([row.get(stage, 0.0) for row in stage_totals_by_trial])
            for stage in all_stages
        }
        variants.append(
            {
                "name": variant.get("name"),
                "kind": variant.get("kind"),
                "taskset": variant.get("taskset"),
                "nice": variant.get("nice"),
                "cgroup_cpu_weight": variant.get("cgroup_cpu_weight"),
                "cgroup_cpu_max": variant.get("cgroup_cpu_max"),
                "pass_trials": (variant.get("summary") or {}).get("pass_trials"),
                "trials": (variant.get("summary") or {}).get("trials"),
                "makespan_mean_sec": (variant.get("summary") or {}).get("makespan_mean_sec"),
                "mean_critical_mean_sec": (variant.get("summary") or {}).get("mean_critical_mean_sec"),
                "p95_critical_mean_sec": (variant.get("summary") or {}).get("p95_critical_mean_sec"),
                "comparison_to_baseline": variant.get("comparison_to_baseline", {}),
                "command_count_mean_per_case": mean(command_counts),
                "stage_seconds_mean_per_trial": stage_seconds_mean,
            }
        )
    return {
        "matrix_summary": str(path),
        "matrix_sha256": file_sha256(path),
        "status": matrix.get("status"),
        "trials_per_variant": matrix.get("trials_per_variant"),
        "variants": variants,
    }


def summarize_mix(path: pathlib.Path) -> dict[str, Any]:
    mix = read_json(path)
    if not isinstance(mix, dict) or not isinstance(mix.get("cases"), list):
        raise SystemExit(f"{path} must be a HarnessReplayBench mix summary")
    cases: list[dict[str, Any]] = []
    stage_totals: collections.Counter[str] = collections.Counter()
    command_counts: list[float] = []
    critical_values: list[float] = []
    profile_samples: list[float] = []
    profile_max_process: list[float] = []
    profile_unique_pids: list[float] = []
    for case in mix.get("cases", []):
        if not isinstance(case, dict):
            continue
        parsed = case.get("parsed") if isinstance(case.get("parsed"), dict) else {}
        profile = case.get("system_profile") if isinstance(case.get("system_profile"), dict) else {}
        for stage, value in (parsed.get("stage_seconds") if isinstance(parsed.get("stage_seconds"), dict) else {}).items():
            number = fnum(value)
            if number is not None:
                stage_totals[str(stage)] += number
        command_count = fnum(parsed.get("command_count"))
        if command_count is not None:
            command_counts.append(command_count)
        critical = fnum(parsed.get("critical_sec"))
        if critical is not None:
            critical_values.append(critical)
        samples = fnum(profile.get("samples"))
        if samples is not None:
            profile_samples.append(samples)
        max_process = fnum(profile.get("max_process_count"))
        if max_process is not None:
            profile_max_process.append(max_process)
        unique_pids = fnum(profile.get("unique_pids_seen"))
        if unique_pids is not None:
            profile_unique_pids.append(unique_pids)
        cases.append(
            {
                "instance_id": case.get("instance_id"),
                "returncode": case.get("returncode"),
                "command_count": parsed.get("command_count"),
                "critical_sec": parsed.get("critical_sec"),
                "stage_counts": parsed.get("stage_counts", {}),
                "stage_seconds": parsed.get("stage_seconds", {}),
                "system_profile_enabled": profile.get("enabled", False),
                "system_profile_samples": profile.get("samples", 0),
                "system_profile_max_process_count": profile.get("max_process_count", 0),
                "system_profile_unique_pids_seen": profile.get("unique_pids_seen", 0),
            }
        )
    return {
        "mix_summary": str(path),
        "mix_sha256": file_sha256(path),
        "status": mix.get("status"),
        "case_count": len(cases),
        "makespan_sec": mix.get("makespan_sec"),
        "aggregate": mix.get("aggregate", {}),
        "cases": cases,
        "command_count_mean_per_case": mean(command_counts),
        "critical_sec_mean_per_case": mean(critical_values),
        "stage_seconds_total": dict(sorted(stage_totals.items())),
        "system_profile_samples_mean_per_case": mean(profile_samples),
        "system_profile_max_process_count_max": max(profile_max_process) if profile_max_process else None,
        "system_profile_unique_pids_seen_mean": mean(profile_unique_pids),
    }


def write_markdown(path: pathlib.Path, payload: dict[str, Any]) -> None:
    lines: list[str] = [
        "# HarnessReplayBench workload summary",
        "",
        f"- schema: `{payload['schema']}`",
        f"- traces: {len(payload['traces'])}",
        f"- mixes: {len(payload['mixes'])}",
        f"- matrices: {len(payload['matrices'])}",
        "",
    ]
    if payload["traces"]:
        lines.extend(
            [
                "## Traces",
                "",
                "| instance | commands | stages | nonzero rc | allow-fail | timing | search/inspect | build | test | package |",
                "|---|---:|---|---:|---:|---|---:|---:|---:|---:|",
            ]
        )
        for row in payload["traces"]:
            ratios = row.get("stage_ratios", {})
            lines.append(
                "| `{instance}` | {commands} | {stages} | {nonzero} | {allow_fail} | `{timing}` | {inspect:.3f} | {build:.3f} | {test:.3f} | {package:.3f} |".format(
                    instance=row.get("instance_id"),
                    commands=row.get("command_count"),
                    stages=", ".join(f"{k}={v}" for k, v in row.get("stage_counts", {}).items()),
                    nonzero=row.get("observed_nonzero_command_count"),
                    allow_fail=row.get("allow_failure_count"),
                    timing=row.get("timing_status"),
                    inspect=float(ratios.get("inspect_command_ratio", 0.0)),
                    build=float(ratios.get("build_command_ratio", 0.0)),
                    test=float(ratios.get("test_command_ratio", 0.0)),
                    package=float(ratios.get("package_background_command_ratio", 0.0)),
                )
            )
        lines.append("")
    for mix in payload["mixes"]:
        lines.extend(
            [
                f"## Mix: `{mix['mix_summary']}`",
                "",
                f"- status: `{mix.get('status')}`",
                f"- cases: {mix.get('case_count')}",
                f"- makespan_sec: {mix.get('makespan_sec')}",
                "",
                "| instance | rc | commands | critical sec | sys samples | max proc | unique pids | stages |",
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for case in mix.get("cases", []):
            stages = ", ".join(f"{k}={v}" for k, v in sorted((case.get("stage_counts") or {}).items()))
            lines.append(
                "| `{instance}` | {rc} | {commands} | {critical} | {samples} | {max_proc} | {unique} | {stages} |".format(
                    instance=case.get("instance_id"),
                    rc=case.get("returncode"),
                    commands=case.get("command_count"),
                    critical=case.get("critical_sec"),
                    samples=case.get("system_profile_samples"),
                    max_proc=case.get("system_profile_max_process_count"),
                    unique=case.get("system_profile_unique_pids_seen"),
                    stages=stages,
                )
            )
        lines.append("")
    for matrix in payload["matrices"]:
        lines.extend(
            [
                f"## Matrix: `{matrix['matrix_summary']}`",
                "",
                f"- status: `{matrix.get('status')}`",
                "",
                "| variant | pass/trials | makespan | mean critical | p95 critical | Δ makespan | Δ mean critical | stage seconds mean/trial |",
                "|---|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for variant in matrix.get("variants", []):
            comp = variant.get("comparison_to_baseline", {})
            stage_seconds = variant.get("stage_seconds_mean_per_trial", {})
            stage_text = ", ".join(f"{k}={v:.3f}" for k, v in stage_seconds.items() if isinstance(v, (int, float)))
            lines.append(
                "| `{name}` | {pass_trials}/{trials} | {makespan} | {meancrit} | {p95} | {dm} | {dc} | {stage_text} |".format(
                    name=variant.get("name"),
                    pass_trials=variant.get("pass_trials"),
                    trials=variant.get("trials"),
                    makespan=variant.get("makespan_mean_sec"),
                    meancrit=variant.get("mean_critical_mean_sec"),
                    p95=variant.get("p95_critical_mean_sec"),
                    dm=comp.get("makespan_delta"),
                    dc=comp.get("mean_critical_delta"),
                    stage_text=stage_text,
                )
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize normalized HarnessReplayBench traces and matrix results",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--trace", action="append", default=[], help="normalized trace JSON; repeatable")
    parser.add_argument("--trace-dir", action="append", default=[], help="directory containing normalized trace JSON files")
    parser.add_argument("--raw-root", help="directory used to resolve raw provenance input_file basenames")
    parser.add_argument("--mix-summary", action="append", default=[], help="mix summary JSON; repeatable")
    parser.add_argument("--matrix-summary", action="append", default=[], help="matrix summary JSON; repeatable")
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    return parser.parse_args()


def collect_trace_paths(args: argparse.Namespace) -> list[pathlib.Path]:
    paths = [pathlib.Path(item) for item in args.trace]
    for directory in args.trace_dir:
        paths.extend(sorted(pathlib.Path(directory).glob("*.json")))
    unique: dict[str, pathlib.Path] = {}
    for path in paths:
        unique[str(path.resolve())] = path
    return list(unique.values())


def main() -> int:
    args = parse_args()
    raw_root = pathlib.Path(args.raw_root) if args.raw_root else None
    traces = [summarize_trace(path, raw_root=raw_root) for path in collect_trace_paths(args)]
    mixes = [summarize_mix(pathlib.Path(path)) for path in args.mix_summary]
    matrices = [summarize_matrix(pathlib.Path(path)) for path in args.matrix_summary]
    payload = {
        "schema": SCHEMA,
        "traces": traces,
        "mixes": mixes,
        "matrices": matrices,
    }
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        markdown = pathlib.Path(args.markdown_output)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(markdown, payload)
    print(json.dumps({"status": "pass", "output": str(output)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Aggregate HarnessReplayBench system profiles at matrix level.

``harness_replay_system_profile.py`` writes one ``system-profile-summary.json``
per replay case.  This script lifts those per-case S1 profiles into a matrix
summary so placement/overhead/locality comparisons do not rely on raw totals
that are biased by elapsed time or sample count.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import statistics
from typing import Any


SCHEMA = "flowcausal.harness_replay_profile_matrix_summary.v1"
CORE_METRICS = (
    "sched_wait_per_elapsed_sec",
    "sched_runtime_per_elapsed_sec",
    "sched_wait_per_sample_ns",
    "sched_runtime_per_sample_ns",
    "cpu_migration_per_elapsed_sec",
    "cpu_migration_per_sample",
    "unique_pids_per_elapsed_sec",
)


def load_json(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected JSON object in {path}")
    return data


def maybe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def divide(numerator: Any, denominator: Any) -> float | None:
    n = maybe_float(numerator)
    d = maybe_float(denominator)
    if n is None or d is None or d == 0:
        return None
    return n / d


def mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def pstdev(values: list[float]) -> float | None:
    return statistics.pstdev(values) if len(values) > 1 else (0.0 if values else None)


def p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int(0.95 * len(ordered) + 0.999999))
    return ordered[min(len(ordered) - 1, rank - 1)]


def relative_delta(base: float | None, value: float | None) -> float | None:
    if base is None or value is None or base == 0:
        return None
    return (value - base) / base


def profile_summary_path(case: dict[str, Any], matrix_dir: pathlib.Path) -> pathlib.Path | None:
    profile = case.get("system_profile")
    if not isinstance(profile, dict) or not profile.get("enabled"):
        return None
    value = profile.get("summary")
    if not value:
        return None
    path = pathlib.Path(str(value))
    if not path.is_absolute():
        path = matrix_dir / path
    return path


def case_identity(case: dict[str, Any]) -> str:
    return str(
        case.get("instance_id")
        or case.get("instance")
        or case.get("name")
        or pathlib.Path(str(case.get("run_dir") or "case")).name
    )


def extract_case_profile(
    *,
    variant: str,
    trial: int,
    case: dict[str, Any],
    summary_path: pathlib.Path,
) -> dict[str, Any]:
    summary = load_json(summary_path)
    aggregate = summary.get("aggregate_delta") if isinstance(summary.get("aggregate_delta"), dict) else {}
    sample_count = maybe_float(summary.get("sample_count")) or 0.0
    elapsed_sec = maybe_float(summary.get("sampled_elapsed_sec")) or 0.0
    wait_delta = maybe_float(aggregate.get("sched_wait_delta_ns")) or 0.0
    runtime_delta = maybe_float(aggregate.get("sched_runtime_delta_ns")) or 0.0
    migration_events = maybe_float(summary.get("cpu_migration_events")) or 0.0
    unique_pids = maybe_float(summary.get("unique_pids_seen")) or 0.0
    record = {
        "variant": variant,
        "trial": trial,
        "instance_id": case_identity(case),
        "run_dir": str(case.get("run_dir") or ""),
        "summary": str(summary_path),
        "profile_level": summary.get("profile_level", ""),
        "sample_count": int(sample_count),
        "sampled_elapsed_sec": elapsed_sec,
        "samples_with_schedstat": int(maybe_float(summary.get("samples_with_schedstat")) or 0),
        "max_process_count": int(maybe_float(summary.get("max_process_count")) or 0),
        "max_leaf_count": int(maybe_float(summary.get("max_leaf_count")) or 0),
        "unique_pids_seen": int(unique_pids),
        "sched_wait_delta_ns": wait_delta,
        "sched_runtime_delta_ns": runtime_delta,
        "sched_wait_per_elapsed_sec": divide(wait_delta, elapsed_sec),
        "sched_runtime_per_elapsed_sec": divide(runtime_delta, elapsed_sec),
        "sched_wait_per_sample_ns": divide(wait_delta, sample_count),
        "sched_runtime_per_sample_ns": divide(runtime_delta, sample_count),
        "cpu_migration_events": int(migration_events),
        "cpu_migration_per_elapsed_sec": divide(migration_events, elapsed_sec),
        "cpu_migration_per_sample": divide(migration_events, sample_count),
        "unique_pids_per_elapsed_sec": divide(unique_pids, elapsed_sec),
        "stage": {},
    }
    stage_summary = summary.get("stage_delta_summary")
    if isinstance(stage_summary, dict):
        for stage, stage_row in sorted(stage_summary.items()):
            if not isinstance(stage_row, dict):
                continue
            stage_delta = (
                stage_row.get("aggregate_delta")
                if isinstance(stage_row.get("aggregate_delta"), dict)
                else {}
            )
            stage_samples = maybe_float(stage_row.get("sample_count")) or 0.0
            stage_wait = maybe_float(stage_delta.get("sched_wait_delta_ns")) or 0.0
            stage_runtime = maybe_float(stage_delta.get("sched_runtime_delta_ns")) or 0.0
            record["stage"][str(stage)] = {
                "sample_count": int(stage_samples),
                "sched_wait_delta_ns": stage_wait,
                "sched_runtime_delta_ns": stage_runtime,
                "sched_wait_per_stage_sample_ns": divide(stage_wait, stage_samples),
                "sched_runtime_per_stage_sample_ns": divide(stage_runtime, stage_samples),
            }
    return record


def metric_values(records: list[dict[str, Any]], metric: str) -> list[float]:
    values: list[float] = []
    for record in records:
        value = maybe_float(record.get(metric))
        if value is not None:
            values.append(value)
    return values


def summarize_metric(records: list[dict[str, Any]], metric: str) -> dict[str, float | None]:
    values = metric_values(records, metric)
    return {
        "mean": mean(values),
        "std": pstdev(values),
        "p95": p95(values),
    }


def summarize_stage(records: list[dict[str, Any]]) -> dict[str, Any]:
    stages = sorted({stage for record in records for stage in record.get("stage", {})})
    out: dict[str, Any] = {}
    for stage in stages:
        stage_rows = [record["stage"][stage] for record in records if stage in record.get("stage", {})]
        wait_per_sample = [
            value
            for row in stage_rows
            if (value := maybe_float(row.get("sched_wait_per_stage_sample_ns"))) is not None
        ]
        runtime_per_sample = [
            value
            for row in stage_rows
            if (value := maybe_float(row.get("sched_runtime_per_stage_sample_ns"))) is not None
        ]
        out[stage] = {
            "profile_count": len(stage_rows),
            "sample_count_total": sum(int(row.get("sample_count") or 0) for row in stage_rows),
            "sched_wait_delta_ns_total": sum(float(row.get("sched_wait_delta_ns") or 0.0) for row in stage_rows),
            "sched_runtime_delta_ns_total": sum(float(row.get("sched_runtime_delta_ns") or 0.0) for row in stage_rows),
            "sched_wait_per_stage_sample_ns_mean": mean(wait_per_sample),
            "sched_runtime_per_stage_sample_ns_mean": mean(runtime_per_sample),
        }
    return out


def summarize_variant(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "profile_count": len(records),
        "sample_count_total": sum(int(record.get("sample_count") or 0) for record in records),
        "sampled_elapsed_sec_mean": mean(metric_values(records, "sampled_elapsed_sec")),
        "max_process_count_mean": mean(metric_values(records, "max_process_count")),
        "max_process_count_max": max(metric_values(records, "max_process_count"), default=None),
        "unique_pids_seen_mean": mean(metric_values(records, "unique_pids_seen")),
        "metrics": {metric: summarize_metric(records, metric) for metric in CORE_METRICS},
        "stage": summarize_stage(records),
    }


def add_variant_comparisons(variants: list[dict[str, Any]]) -> None:
    if not variants:
        return
    baseline = variants[0]
    baseline_metrics = baseline.get("summary", {}).get("metrics", {})
    baseline["comparison_to_baseline"] = {
        "baseline": baseline.get("name"),
        **{metric: {"mean_delta": 0.0} for metric in CORE_METRICS},
    }
    for variant in variants[1:]:
        metrics = variant.get("summary", {}).get("metrics", {})
        comparison: dict[str, Any] = {"baseline": baseline.get("name")}
        for metric in CORE_METRICS:
            comparison[metric] = {
                "mean_delta": relative_delta(
                    baseline_metrics.get(metric, {}).get("mean"),
                    metrics.get(metric, {}).get("mean"),
                )
            }
        variant["comparison_to_baseline"] = comparison


def summarize_matrix(matrix_summary_path: pathlib.Path) -> dict[str, Any]:
    matrix_summary_path = matrix_summary_path.resolve()
    matrix_dir = matrix_summary_path.parent
    matrix = load_json(matrix_summary_path)
    variants_out: list[dict[str, Any]] = []
    missing_profiles: list[dict[str, Any]] = []
    all_profiles: list[dict[str, Any]] = []
    for variant in matrix.get("variants", []):
        if not isinstance(variant, dict):
            continue
        variant_name = str(variant.get("name") or "unknown")
        variant_profiles: list[dict[str, Any]] = []
        for trial in variant.get("trials", []):
            if not isinstance(trial, dict):
                continue
            trial_id = int(trial.get("trial") or 0)
            for case in trial.get("cases", []):
                if not isinstance(case, dict):
                    continue
                path = profile_summary_path(case, matrix_dir)
                if path is None or not path.exists():
                    missing_profiles.append(
                        {
                            "variant": variant_name,
                            "trial": trial_id,
                            "instance_id": case_identity(case),
                            "expected_summary": str(path) if path else "",
                        }
                    )
                    continue
                profile = extract_case_profile(
                    variant=variant_name,
                    trial=trial_id,
                    case=case,
                    summary_path=path,
                )
                variant_profiles.append(profile)
                all_profiles.append(profile)
        variants_out.append(
            {
                "name": variant_name,
                "kind": variant.get("kind", ""),
                "summary": summarize_variant(variant_profiles),
                "profiles": variant_profiles,
            }
        )
    add_variant_comparisons(variants_out)
    return {
        "schema": SCHEMA,
        "matrix_summary": str(matrix_summary_path),
        "status": "pass" if not missing_profiles and all_profiles else ("partial" if all_profiles else "fail"),
        "profile_count": len(all_profiles),
        "missing_profiles": missing_profiles,
        "variants": variants_out,
        "claim_discipline": {
            "permitted": [
                "matrix-level normalization of replay-time system profiles",
                "per elapsed/per sample/per stage-sample comparison across variants",
                "diagnosing overhead, migration, locality, and wait-rate differences",
            ],
            "forbidden": [
                "live-vs-replay system fidelity without original/live profiles",
                "victim safety without victim latency data",
                "FlowCausal speedup without endpoint/critical-path and mechanism gates",
            ],
        },
    }


def write_csv(path: pathlib.Path, payload: dict[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    for variant in payload.get("variants", []):
        for profile in variant.get("profiles", []):
            rows.append(
                {
                    key: profile.get(key)
                    for key in (
                        "variant",
                        "trial",
                        "instance_id",
                        "sample_count",
                        "sampled_elapsed_sec",
                        "max_process_count",
                        "unique_pids_seen",
                        "sched_wait_per_elapsed_sec",
                        "sched_runtime_per_elapsed_sec",
                        "sched_wait_per_sample_ns",
                        "sched_runtime_per_sample_ns",
                        "cpu_migration_events",
                        "cpu_migration_per_elapsed_sec",
                        "cpu_migration_per_sample",
                    )
                }
            )
    fieldnames = [
        "variant",
        "trial",
        "instance_id",
        "sample_count",
        "sampled_elapsed_sec",
        "max_process_count",
        "unique_pids_seen",
        "sched_wait_per_elapsed_sec",
        "sched_runtime_per_elapsed_sec",
        "sched_wait_per_sample_ns",
        "sched_runtime_per_sample_ns",
        "cpu_migration_events",
        "cpu_migration_per_elapsed_sec",
        "cpu_migration_per_sample",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HarnessReplayBench system profile matrix summary",
        "",
        f"- status: `{payload.get('status')}`",
        f"- profiles: {payload.get('profile_count')}",
        f"- missing_profiles: {len(payload.get('missing_profiles', []))}",
        "",
        "## Variant normalized metrics",
        "",
        "| variant | profiles | wait/elapsed mean | wait/sample mean | migration/elapsed mean | migration/sample mean | max proc mean |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in payload.get("variants", []):
        summary = variant.get("summary", {})
        metrics = summary.get("metrics", {})
        lines.append(
            "| `{name}` | {profiles} | {wait_elapsed} | {wait_sample} | {mig_elapsed} | {mig_sample} | {max_proc} |".format(
                name=variant.get("name"),
                profiles=summary.get("profile_count"),
                wait_elapsed=metrics.get("sched_wait_per_elapsed_sec", {}).get("mean"),
                wait_sample=metrics.get("sched_wait_per_sample_ns", {}).get("mean"),
                mig_elapsed=metrics.get("cpu_migration_per_elapsed_sec", {}).get("mean"),
                mig_sample=metrics.get("cpu_migration_per_sample", {}).get("mean"),
                max_proc=summary.get("max_process_count_mean"),
            )
        )
    lines.extend(["", "## Stage wait per sample", "", "| variant | stage | profiles | samples | wait/stage-sample mean | runtime/stage-sample mean |", "|---|---|---:|---:|---:|---:|"])
    for variant in payload.get("variants", []):
        for stage, row in sorted(variant.get("summary", {}).get("stage", {}).items()):
            lines.append(
                "| `{variant}` | `{stage}` | {profiles} | {samples} | {wait} | {runtime} |".format(
                    variant=variant.get("name"),
                    stage=stage,
                    profiles=row.get("profile_count"),
                    samples=row.get("sample_count_total"),
                    wait=row.get("sched_wait_per_stage_sample_ns_mean"),
                    runtime=row.get("sched_runtime_per_stage_sample_ns_mean"),
                )
            )
    if payload.get("missing_profiles"):
        lines.extend(["", "## Missing profiles", ""])
        for row in payload["missing_profiles"]:
            lines.append(
                f"- `{row.get('variant')}` trial {row.get('trial')} `{row.get('instance_id')}`: {row.get('expected_summary')}"
            )
    lines.extend(
        [
            "",
            "This is normalized replay-time profile evidence. It is diagnostic only unless combined with fidelity, mechanism, safety, and baseline gates.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate system-profile-summary.json files referenced by a HarnessReplayBench matrix summary",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--matrix-summary", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output", default="")
    parser.add_argument("--csv-output", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    payload = summarize_matrix(pathlib.Path(args.matrix_summary))
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        markdown = pathlib.Path(args.markdown_output)
        markdown.parent.mkdir(parents=True, exist_ok=True)
        markdown.write_text(render_markdown(payload), encoding="utf-8")
    if args.csv_output:
        csv_path = pathlib.Path(args.csv_output)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_csv(csv_path, payload)
    print(json.dumps({"status": payload["status"], "output": str(output)}, sort_keys=True))
    return 0 if payload["status"] in {"pass", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

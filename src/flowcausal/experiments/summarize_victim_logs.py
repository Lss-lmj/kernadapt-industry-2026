#!/usr/bin/env python3
"""Aggregate periodic CPU victim logs referenced by a matrix summary."""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
from typing import Any


SCHEMA = "flowcausal.victim_latency_matrix_summary.v1"
LATENCY_METRICS = (
    "lateness_mean_ms",
    "lateness_p50_ms",
    "lateness_p95_ms",
    "lateness_p99_ms",
    "lateness_max_ms",
    "runtime_p95_ms",
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


def resolve_path(value: str, matrix_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = matrix_dir / path
    return path


def victim_log_path(trial: dict[str, Any], matrix_dir: pathlib.Path) -> pathlib.Path | None:
    live_wrapper = trial.get("live_wrapper") if isinstance(trial.get("live_wrapper"), dict) else {}
    if live_wrapper.get("victim_log"):
        return resolve_path(str(live_wrapper["victim_log"]), matrix_dir)
    flowcausal_live = trial.get("flowcausal_live") if isinstance(trial.get("flowcausal_live"), dict) else {}
    if flowcausal_live.get("victim_log"):
        return resolve_path(str(flowcausal_live["victim_log"]), matrix_dir)
    if flowcausal_live.get("out_dir"):
        return resolve_path(str(flowcausal_live["out_dir"]), matrix_dir) / "victim.log"
    run_dir = trial.get("run_dir")
    if run_dir:
        base = resolve_path(str(run_dir), matrix_dir)
        for dirname in ("live-sidecar", "flowcausal-live"):
            candidate = base / dirname / "victim.log"
            if candidate.exists():
                return candidate
    return None


def parse_victim_log(path: pathlib.Path) -> dict[str, Any]:
    summary: dict[str, Any] | None = None
    begin: dict[str, Any] | None = None
    parse_errors = 0
    line_count = 0
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line_count += 1
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "victim_begin":
            begin = record
        elif record.get("type") == "victim_summary":
            summary = record
    if summary is None:
        return {
            "status": "missing-summary",
            "path": str(path),
            "line_count": line_count,
            "parse_errors": parse_errors,
        }
    lateness = summary.get("lateness") if isinstance(summary.get("lateness"), dict) else {}
    runtime = summary.get("runtime") if isinstance(summary.get("runtime"), dict) else {}
    return {
        "status": "pass",
        "path": str(path),
        "line_count": line_count,
        "parse_errors": parse_errors,
        "workers": int(summary.get("workers") or (begin or {}).get("workers") or 0),
        "iterations": int(summary.get("iterations") or 0),
        "returncode": int(summary.get("returncode") or 0),
        "lateness_mean_ms": maybe_float(lateness.get("mean_ms")),
        "lateness_p50_ms": maybe_float(lateness.get("p50_ms")),
        "lateness_p95_ms": maybe_float(lateness.get("p95_ms")),
        "lateness_p99_ms": maybe_float(lateness.get("p99_ms")),
        "lateness_max_ms": maybe_float(lateness.get("max_ms")),
        "runtime_p95_ms": maybe_float(runtime.get("p95_ms")),
        "runtime_max_ms": maybe_float(runtime.get("max_ms")),
    }


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
        "max": max(values) if values else None,
    }


def summarize_variant(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "victim_runs": len(records),
        "iterations_mean": mean(metric_values(records, "iterations")),
        "metrics": {metric: summarize_metric(records, metric) for metric in LATENCY_METRICS},
    }


def add_variant_comparisons(variants: list[dict[str, Any]]) -> None:
    if not variants:
        return
    baseline = variants[0]
    baseline_metrics = baseline.get("summary", {}).get("metrics", {})
    baseline["comparison_to_baseline"] = {
        "baseline": baseline.get("name"),
        **{metric: {"mean_delta": 0.0} for metric in LATENCY_METRICS},
    }
    for variant in variants[1:]:
        metrics = variant.get("summary", {}).get("metrics", {})
        comparison: dict[str, Any] = {"baseline": baseline.get("name")}
        for metric in LATENCY_METRICS:
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
    missing_logs: list[dict[str, Any]] = []
    invalid_logs: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    for variant in matrix.get("variants", []):
        if not isinstance(variant, dict):
            continue
        variant_name = str(variant.get("name") or "unknown")
        records: list[dict[str, Any]] = []
        for trial in variant.get("trials", []):
            if not isinstance(trial, dict):
                continue
            trial_id = int(trial.get("trial") or 0)
            path = victim_log_path(trial, matrix_dir)
            if path is None or not path.exists():
                missing_logs.append(
                    {
                        "variant": variant_name,
                        "trial": trial_id,
                        "expected_log": str(path) if path else "",
                    }
                )
                continue
            parsed = parse_victim_log(path)
            parsed.update({"variant": variant_name, "trial": trial_id})
            if parsed.get("status") != "pass":
                invalid_logs.append(parsed)
                continue
            records.append(parsed)
            all_records.append(parsed)
        variants_out.append(
            {
                "name": variant_name,
                "kind": variant.get("kind", ""),
                "summary": summarize_variant(records),
                "victim_logs": records,
            }
        )
    add_variant_comparisons(variants_out)
    status = "pass"
    if missing_logs or invalid_logs:
        status = "partial" if all_records else "fail"
    if not all_records:
        status = "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "matrix_summary": str(matrix_summary_path),
        "victim_runs": len(all_records),
        "missing_logs": missing_logs,
        "invalid_logs": invalid_logs,
        "variants": variants_out,
        "claim_discipline": {
            "permitted": [
                "matrix-level victim release-lateness aggregation",
                "p95/p99/max sidecar latency comparison across variants",
            ],
            "forbidden": [
                "FlowCausal safety claim without comparable baseline victim runs",
                "overall scheduler speedup without active/makespan and mechanism gates",
            ],
        },
    }


def write_csv(path: pathlib.Path, payload: dict[str, Any]) -> None:
    fieldnames = [
        "variant",
        "trial",
        "workers",
        "iterations",
        "lateness_mean_ms",
        "lateness_p50_ms",
        "lateness_p95_ms",
        "lateness_p99_ms",
        "lateness_max_ms",
        "runtime_p95_ms",
        "runtime_max_ms",
        "path",
    ]
    rows: list[dict[str, Any]] = []
    for variant in payload.get("variants", []):
        for record in variant.get("victim_logs", []):
            rows.append({key: record.get(key) for key in fieldnames})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Victim latency matrix summary",
        "",
        f"- status: `{payload.get('status')}`",
        f"- victim_runs: {payload.get('victim_runs')}",
        f"- missing_logs: {len(payload.get('missing_logs', []))}",
        f"- invalid_logs: {len(payload.get('invalid_logs', []))}",
        "",
        "## Variant victim latency",
        "",
        "| variant | runs | p95 mean ms | p99 mean ms | max mean ms | p95 Δ | p99 Δ | max Δ |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in payload.get("variants", []):
        summary = variant.get("summary", {})
        metrics = summary.get("metrics", {})
        comp = variant.get("comparison_to_baseline", {})
        lines.append(
            "| `{name}` | {runs} | {p95} | {p99} | {max_ms} | {dp95} | {dp99} | {dmax} |".format(
                name=variant.get("name"),
                runs=summary.get("victim_runs"),
                p95=metrics.get("lateness_p95_ms", {}).get("mean"),
                p99=metrics.get("lateness_p99_ms", {}).get("mean"),
                max_ms=metrics.get("lateness_max_ms", {}).get("mean"),
                dp95=comp.get("lateness_p95_ms", {}).get("mean_delta"),
                dp99=comp.get("lateness_p99_ms", {}).get("mean_delta"),
                dmax=comp.get("lateness_max_ms", {}).get("mean_delta"),
            )
        )
    if payload.get("missing_logs"):
        lines.extend(["", "## Missing logs", ""])
        for row in payload["missing_logs"]:
            lines.append(f"- `{row.get('variant')}` trial {row.get('trial')}: {row.get('expected_log')}")
    if payload.get("invalid_logs"):
        lines.extend(["", "## Invalid logs", ""])
        for row in payload["invalid_logs"]:
            lines.append(f"- `{row.get('variant')}` trial {row.get('trial')}: {row.get('path')}")
    lines.extend(
        [
            "",
            "This table is a safety diagnostic. It is not a FlowCausal performance claim by itself.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate cpu_victim.py JSONL logs from a matrix summary",
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

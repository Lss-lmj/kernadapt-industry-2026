#!/usr/bin/env python3
"""Import externally produced full-harness endpoint rows into a matrix summary.

The online path, ``run_real_agent_harness_matrix.py``, executes SWE-agent /
OpenHands / SWE-bench harness commands itself.  In practice, full agent runs are
often produced elsewhere: on another machine with API keys, through an official
SWE-bench evaluator, or by OpenHands/SWE-agent CI.  This helper is the intake
bridge for those runs.

It does not execute an agent and it does not upgrade claims.  It only
normalizes endpoint JSON/JSONL files with ``normalize_full_harness_result.py``
and emits the same ``flowcausal.real_agent_harness_matrix.v1`` shape consumed by
``audit_real_agent_harness_matrix.py``.  Missing raw trajectories, capture
directories, or optional victim safety logs remain explicit blockers/partial
evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import pathlib
import statistics
from types import SimpleNamespace
from typing import Any

from normalize_full_harness_result import normalize_endpoint, read_json_or_jsonl
from validate_bundle_collection_manifest import safe_name


SCHEMA = "flowcausal.real_agent_harness_matrix.v1"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def fnum(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0.0 else None


def parse_bool(value: str) -> bool:
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "pass", "passed"}:
        return True
    if text in {"0", "false", "no", "n", "fail", "failed"}:
        return False
    raise argparse.ArgumentTypeError(f"not a boolean: {value!r}")


def parse_endpoint_spec(text: str) -> dict[str, str]:
    """Parse a repeated ``--endpoint`` spec.

    Format:

        variant=default,trial=1,path=/tmp/result.json,raw=/tmp/run.traj

    Required keys: ``variant`` and ``path``.  Optional keys: ``trial``, ``raw``,
    ``capture``, ``live``, ``victim``, ``status``, ``run_returncode``.
    """

    out: dict[str, str] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise argparse.ArgumentTypeError(f"--endpoint item must be key=value, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise argparse.ArgumentTypeError(f"--endpoint item has an empty key: {text!r}")
        out[key] = value.strip()
    for key in ("variant", "path"):
        if not out.get(key):
            raise argparse.ArgumentTypeError(f"--endpoint requires {key}=... in {text!r}")
    if "trial" in out:
        try:
            trial = int(out["trial"])
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"--endpoint trial must be an integer: {text!r}") from exc
        if trial <= 0:
            raise argparse.ArgumentTypeError(f"--endpoint trial must be positive: {text!r}")
    return out


def ensure_trial_numbers(specs: list[dict[str, str]]) -> list[dict[str, str]]:
    counters: dict[str, int] = {}
    out: list[dict[str, str]] = []
    for spec in specs:
        row = dict(spec)
        variant = row["variant"]
        if "trial" not in row or not row["trial"]:
            counters[variant] = counters.get(variant, 0) + 1
            row["trial"] = str(counters[variant])
        else:
            counters[variant] = max(counters.get(variant, 0), int(row["trial"]))
        out.append(row)
    return out


def normalize_endpoint_file(path: pathlib.Path, *, args: argparse.Namespace, variant: str) -> dict[str, Any]:
    data, input_kind, _rows = read_json_or_jsonl(path)
    ns = SimpleNamespace(
        format=args.format,
        source=args.source,
        instance_id=args.instance_id,
        scheduler_variant=variant,
        created_at=args.created_at,
    )
    return normalize_endpoint(data, input_kind, ns, path)


def path_or_empty(value: str | None) -> str:
    if not value:
        return ""
    return str(pathlib.Path(value).expanduser().resolve())


def victim_path_for_spec(spec: dict[str, str], live_dir: pathlib.Path) -> str:
    if spec.get("victim"):
        return path_or_empty(spec.get("victim"))
    if str(live_dir) and str(live_dir) != ".":
        return str((live_dir / "victim.log").expanduser().resolve())
    return ""


def trial_row(
    spec: dict[str, str],
    *,
    args: argparse.Namespace,
    out_dir: pathlib.Path,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[str]]:
    variant = spec["variant"]
    trial = int(spec["trial"])
    safe_variant = safe_name(variant)
    trial_dir = out_dir / "runs" / safe_variant / f"trial-{trial:02d}"
    capture_dir = pathlib.Path(spec["capture"]).expanduser().resolve() if spec.get("capture") else trial_dir / "imported-capture"
    live_dir = pathlib.Path(spec["live"]).expanduser().resolve() if spec.get("live") else pathlib.Path("")
    endpoint_path = pathlib.Path(spec["path"]).expanduser().resolve()
    raw_path_text = path_or_empty(spec.get("raw"))
    victim_path_text = victim_path_for_spec(spec, live_dir)
    warnings: list[str] = []
    blockers: list[str] = []
    normalized: dict[str, Any] | None = None

    capture_dir.mkdir(parents=True, exist_ok=True)
    if not endpoint_path.exists():
        blockers.append(f"endpoint path does not exist: {endpoint_path}")
    else:
        try:
            normalized = normalize_endpoint_file(endpoint_path, args=args, variant=variant)
            normalized_path = trial_dir / "normalized-endpoint.json"
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            normalized_path.write_text(
                json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        except SystemExit as exc:
            blockers.append(f"endpoint normalization failed: {exc}")

    if not raw_path_text:
        warnings.append("raw_agent_output path missing; artifact audit will remain partial")
    elif not pathlib.Path(raw_path_text).exists():
        blockers.append(f"raw_agent_output path does not exist: {raw_path_text}")

    if not spec.get("capture"):
        warnings.append("capture_dir was synthesized for imported endpoint metadata")
    if victim_path_text and not pathlib.Path(victim_path_text).exists():
        blockers.append(f"victim_log path does not exist: {victim_path_text}")
    run_returncode = int(spec.get("run_returncode") or 0)
    status = spec.get("status") or ("pass" if not blockers else "partial")
    if run_returncode != 0:
        blockers.append(f"run_returncode is nonzero: {run_returncode}")
        if status == "pass":
            status = "partial"

    capture_report = {
        "schema": "flowcausal.imported_capture.v1",
        "status": status,
        "instance_id": args.instance_id,
        "scheduler_variant": variant,
        "trial": trial,
        "artifacts": {
            "raw_agent_output": raw_path_text,
            "full_harness_result": str(endpoint_path),
            "victim_log": victim_path_text,
            "live_dir": str(live_dir) if str(live_dir) != "." else "",
        },
        "run": {"returncode": run_returncode},
        "warnings": warnings,
        "blockers": blockers,
        "notes": [
            "This capture report was synthesized by import_real_agent_harness_matrix.py.",
            "It records imported full-harness endpoint metadata; it is not proof that the harness was executed on this machine.",
        ],
    }
    (capture_dir / "capture-report.json").write_text(
        json.dumps(capture_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    row = {
        "variant": variant,
        "trial": trial,
        "status": status,
        "capture_dir": str(capture_dir),
        "live_dir": str(live_dir) if str(live_dir) != "." else "",
        "victim_log": victim_path_text,
        "raw_agent_output": raw_path_text,
        "full_harness_result": str(endpoint_path),
        "resolved": normalized.get("resolved") if normalized else None,
        "elapsed_sec": normalized.get("elapsed_sec") if normalized else None,
        "run_returncode": run_returncode,
        "blockers": blockers,
        "warnings": warnings,
    }
    return row, normalized, blockers


def aggregate_trials(trials: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for variant in sorted({str(row.get("variant") or "") for row in trials if row.get("variant")}):
        rows = [row for row in trials if row.get("variant") == variant]
        elapsed = [value for value in (fnum(row.get("elapsed_sec")) for row in rows) if value is not None]
        resolved = [row.get("resolved") for row in rows if row.get("resolved") is not None]
        aggregate: dict[str, Any] = {
            "trials": len(rows),
            "passes": sum(1 for row in rows if row.get("status") == "pass"),
            "endpoint_rows": len(elapsed),
            "resolved_true": sum(1 for value in resolved if value is True),
            "resolved_false": sum(1 for value in resolved if value is False),
            "resolved_unknown": len(rows) - len(resolved),
        }
        if elapsed:
            aggregate.update(
                {
                    "elapsed_sec_mean": statistics.mean(elapsed),
                    "elapsed_sec_min": min(elapsed),
                    "elapsed_sec_max": max(elapsed),
                    "elapsed_sec_std": statistics.pstdev(elapsed) if len(elapsed) > 1 else 0.0,
                }
            )
        out[variant] = aggregate
    return out


def comparisons_for(aggregate: dict[str, dict[str, Any]], baseline: str) -> dict[str, dict[str, Any]]:
    baseline_elapsed = fnum(as_dict(aggregate.get(baseline)).get("elapsed_sec_mean"))
    out: dict[str, dict[str, Any]] = {}
    for variant, row in aggregate.items():
        variant_elapsed = fnum(row.get("elapsed_sec_mean"))
        comparison: dict[str, Any] = {"baseline_variant": baseline}
        if baseline_elapsed is not None and baseline_elapsed > 0 and variant_elapsed is not None:
            comparison["elapsed_delta_sec"] = variant_elapsed - baseline_elapsed
            comparison["elapsed_delta_pct"] = (variant_elapsed - baseline_elapsed) * 100.0 / baseline_elapsed
            comparison["speedup_x"] = baseline_elapsed / variant_elapsed if variant_elapsed > 0 else None
        else:
            comparison["status"] = "partial"
            comparison["reason"] = "missing comparable elapsed_sec"
        out[variant] = comparison
    return out


def write_summary_files(out_dir: pathlib.Path, summary: dict[str, Any]) -> None:
    (out_dir / "harness-matrix-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (out_dir / "harness-matrix-summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "variant",
                "trials",
                "passes",
                "endpoint_rows",
                "resolved_true",
                "resolved_false",
                "resolved_unknown",
                "elapsed_sec_mean",
                "elapsed_delta_pct",
                "speedup_x",
            ],
        )
        writer.writeheader()
        for variant, aggregate in sorted(as_dict(summary.get("aggregate")).items()):
            comparison = as_dict(as_dict(summary.get("comparisons")).get(variant))
            writer.writerow(
                {
                    "variant": variant,
                    "trials": aggregate.get("trials", 0),
                    "passes": aggregate.get("passes", 0),
                    "endpoint_rows": aggregate.get("endpoint_rows", 0),
                    "resolved_true": aggregate.get("resolved_true", 0),
                    "resolved_false": aggregate.get("resolved_false", 0),
                    "resolved_unknown": aggregate.get("resolved_unknown", 0),
                    "elapsed_sec_mean": aggregate.get("elapsed_sec_mean", ""),
                    "elapsed_delta_pct": comparison.get("elapsed_delta_pct", ""),
                    "speedup_x": comparison.get("speedup_x", ""),
                }
            )
    lines = [
        "# Imported real-agent harness matrix summary",
        "",
        f"Status: `{summary.get('status')}`",
        "",
        "| variant | trials | passes | resolved T/F/? | mean elapsed (s) | delta vs baseline | speedup |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for variant, aggregate in sorted(as_dict(summary.get("aggregate")).items()):
        comparison = as_dict(as_dict(summary.get("comparisons")).get(variant))
        lines.append(
            "| {variant} | {trials} | {passes} | {rt}/{rf}/{ru} | {elapsed} | {delta} | {speedup} |".format(
                variant=variant,
                trials=aggregate.get("trials", 0),
                passes=aggregate.get("passes", 0),
                rt=aggregate.get("resolved_true", 0),
                rf=aggregate.get("resolved_false", 0),
                ru=aggregate.get("resolved_unknown", 0),
                elapsed=f"{aggregate.get('elapsed_sec_mean'):.3f}" if isinstance(aggregate.get("elapsed_sec_mean"), (int, float)) else "",
                delta=f"{comparison.get('elapsed_delta_pct'):.1f}%" if isinstance(comparison.get("elapsed_delta_pct"), (int, float)) else "",
                speedup=f"{comparison.get('speedup_x'):.3f}x" if isinstance(comparison.get("speedup_x"), (int, float)) else "",
            )
        )
    if summary.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        lines.extend(f"- {item}" for item in summary["blockers"])
    if summary.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {item}" for item in summary["warnings"])
    (out_dir / "harness-matrix-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_summary(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    specs = ensure_trial_numbers([parse_endpoint_spec(text) for text in args.endpoint])
    trials: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for spec in specs:
        row, _normalized, row_blockers = trial_row(spec, args=args, out_dir=out_dir)
        trials.append(row)
        blockers.extend(f"{row['variant']} trial {row['trial']}: {item}" for item in row_blockers)
        warnings.extend(f"{row['variant']} trial {row['trial']}: {item}" for item in row.get("warnings", []))
    aggregate = aggregate_trials(trials)
    comparisons = comparisons_for(aggregate, args.baseline_variant)
    missing_baseline = args.baseline_variant not in aggregate
    if missing_baseline:
        blockers.append(f"baseline variant missing: {args.baseline_variant}")
    status = "pass" if trials and not blockers and all(row.get("status") == "pass" for row in trials) else "partial" if trials else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "instance_id": args.instance_id,
        "baseline_variant": args.baseline_variant,
        "source": args.source,
        "trials": sorted(trials, key=lambda row: (str(row.get("variant")), int(row.get("trial") or 0))),
        "aggregate": aggregate,
        "comparisons": comparisons,
        "warnings": warnings,
        "blockers": blockers,
        "notes": [
            "This summary imports externally produced full-harness endpoint artifacts; it does not execute an agent.",
            "Run audit_real_agent_harness_matrix.py before using it as endpoint evidence.",
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import external SWE-agent/OpenHands/SWE-bench endpoint rows into a FlowCausal harness matrix summary",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--baseline-variant", default="default")
    parser.add_argument("--endpoint", action="append", required=True, help="key=value spec: variant=...,trial=...,path=...,raw=...,capture=...,live=...,victim=...")
    parser.add_argument(
        "--format",
        choices=["auto", "swe-bench", "swe-agent", "openhands", "flowcausal", "generic"],
        default="auto",
    )
    parser.add_argument("--source", default="external-full-harness")
    parser.add_argument("--created-at", default="")
    parser.add_argument("--allow-partial", action="store_true", help="exit 0 even when imported rows are partial")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = build_summary(args)
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    write_summary_files(out_dir, summary)
    result = {
        "summary": str(out_dir / "harness-matrix-summary.json"),
        "status": summary.get("status"),
        "trials": len(summary.get("trials", [])),
        "blockers": len(summary.get("blockers", [])),
    }
    print(json.dumps(result, sort_keys=True))
    if summary.get("status") != "pass" and not args.allow_partial:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

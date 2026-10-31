#!/usr/bin/env python3
"""Compare two HarnessReplayBench system profiles.

``harness_replay_system_profile.py`` captures replay-time process-tree and
schedstat evidence.  This audit compares two summaries captured with the same
schema: typically a reference/original/live harness profile and a replay
profile.  It is deliberately separate from structural fidelity and scheduler
mechanism audits.

Default pass/fail gates are conservative and focus on shape:

* both profiles exist and have samples;
* max process width and unique pid count are within relative tolerance;
* foreground stage distribution is close enough;
* total runtime is within relative tolerance.

Runqueue wait and migration are reported but not strict by default because they
can be very noisy on short smoke runs.  Use stricter thresholds later when
profiles are collected under controlled repeated trials.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
from typing import Any


PROFILE_SCHEMA = "flowcausal.harness_replay_system_profile.v1"
AUDIT_SCHEMA = "flowcausal.harness_replay_system_fidelity.v1"
BACKGROUND_STAGES = {"SHELL", "UNKNOWN"}


def read_json(path: pathlib.Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def relative_delta(reference: float, replay: float) -> float | None:
    if reference == 0:
        if replay == 0:
            return 0.0
        return None
    return (replay - reference) / reference


def abs_relative_delta(reference: float, replay: float) -> float:
    delta = relative_delta(reference, replay)
    if delta is None:
        return float("inf")
    return abs(delta)


def stage_counts(profile: dict[str, Any], *, include_background: bool) -> dict[str, float]:
    raw = profile.get("stage_sample_counts")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for stage, value in raw.items():
        stage_text = str(stage)
        if not include_background and stage_text in BACKGROUND_STAGES:
            continue
        out[stage_text] = fnum(value)
    return out


def normalize_counts(counts: dict[str, float]) -> dict[str, float]:
    total = sum(value for value in counts.values() if value > 0)
    if total <= 0:
        return {stage: 0.0 for stage in counts}
    return {stage: max(0.0, value) / total for stage, value in counts.items()}


def total_variation_distance(left: dict[str, float], right: dict[str, float]) -> float:
    stages = sorted(set(left) | set(right))
    left_n = normalize_counts(left)
    right_n = normalize_counts(right)
    return 0.5 * sum(abs(left_n.get(stage, 0.0) - right_n.get(stage, 0.0)) for stage in stages)


def aggregate(profile: dict[str, Any], key: str) -> float:
    raw = profile.get("aggregate_delta") if isinstance(profile.get("aggregate_delta"), dict) else {}
    return fnum(raw.get(key))


def scalar_check(
    name: str,
    reference: float,
    replay: float,
    *,
    tolerance: float,
    strict: bool = True,
) -> dict[str, Any]:
    abs_delta = abs_relative_delta(reference, replay)
    passed = abs_delta <= tolerance
    return {
        "name": name,
        "reference": reference,
        "replay": replay,
        "relative_delta": relative_delta(reference, replay),
        "abs_relative_delta": None if math.isinf(abs_delta) else abs_delta,
        "tolerance": tolerance,
        "strict": strict,
        "status": "pass" if passed else ("fail" if strict else "warn"),
    }


def compare_profiles(
    reference: dict[str, Any],
    replay: dict[str, Any],
    *,
    args: argparse.Namespace,
) -> dict[str, Any]:
    blockers: list[str] = []
    warnings: list[str] = []
    if reference.get("schema") != PROFILE_SCHEMA:
        blockers.append("reference profile schema mismatch")
    if replay.get("schema") != PROFILE_SCHEMA:
        blockers.append("replay profile schema mismatch")
    if int(reference.get("sample_count") or 0) <= 0:
        blockers.append("reference profile has no samples")
    if int(replay.get("sample_count") or 0) <= 0:
        blockers.append("replay profile has no samples")

    checks: list[dict[str, Any]] = [
        scalar_check(
            "max_process_count",
            fnum(reference.get("max_process_count")),
            fnum(replay.get("max_process_count")),
            tolerance=args.process_tolerance,
        ),
        scalar_check(
            "max_leaf_count",
            fnum(reference.get("max_leaf_count")),
            fnum(replay.get("max_leaf_count")),
            tolerance=args.process_tolerance,
        ),
        scalar_check(
            "unique_pids_seen",
            fnum(reference.get("unique_pids_seen")),
            fnum(replay.get("unique_pids_seen")),
            tolerance=args.unique_pid_tolerance,
        ),
        scalar_check(
            "sched_runtime_delta_ns",
            aggregate(reference, "sched_runtime_delta_ns"),
            aggregate(replay, "sched_runtime_delta_ns"),
            tolerance=args.runtime_tolerance,
        ),
        scalar_check(
            "sched_wait_delta_ns",
            aggregate(reference, "sched_wait_delta_ns"),
            aggregate(replay, "sched_wait_delta_ns"),
            tolerance=args.wait_tolerance,
            strict=args.strict_wait,
        ),
        scalar_check(
            "cpu_migration_events",
            fnum(reference.get("cpu_migration_events")),
            fnum(replay.get("cpu_migration_events")),
            tolerance=args.migration_tolerance,
            strict=args.strict_migration,
        ),
    ]
    for check in checks:
        if check["status"] == "fail":
            blockers.append(
                f"{check['name']} outside tolerance: abs_delta={check['abs_relative_delta']} tolerance={check['tolerance']}"
            )
        elif check["status"] == "warn":
            warnings.append(
                f"{check['name']} outside non-strict tolerance: abs_delta={check['abs_relative_delta']} tolerance={check['tolerance']}"
            )

    ref_stage_counts = stage_counts(reference, include_background=args.include_background_stages)
    replay_stage_counts = stage_counts(replay, include_background=args.include_background_stages)
    stage_tvd = total_variation_distance(ref_stage_counts, replay_stage_counts)
    stage_status = "pass" if stage_tvd <= args.stage_tvd_tolerance else "fail"
    if stage_status == "fail":
        blockers.append(f"stage distribution TVD {stage_tvd:.4f} exceeds {args.stage_tvd_tolerance}")
    missing_replay_stages = [
        stage
        for stage, count in sorted(ref_stage_counts.items())
        if count > 0 and replay_stage_counts.get(stage, 0.0) <= 0
    ]
    if missing_replay_stages:
        blockers.append(f"replay missing reference stages: {missing_replay_stages}")

    status = "pass" if not blockers else "fail"
    fidelity_level = (
        "S3: system profile comparison pass"
        if status == "pass"
        else "S2: comparable profiles collected; comparison not passed"
    )
    return {
        "schema": AUDIT_SCHEMA,
        "status": status,
        "fidelity_level": fidelity_level,
        "blockers": blockers,
        "warnings": warnings,
        "reference": {
            "profile_level": reference.get("profile_level"),
            "sample_count": reference.get("sample_count"),
            "provenance": reference.get("provenance", {}),
        },
        "replay": {
            "profile_level": replay.get("profile_level"),
            "sample_count": replay.get("sample_count"),
            "provenance": replay.get("provenance", {}),
        },
        "checks": checks,
        "stage_distribution": {
            "include_background_stages": bool(args.include_background_stages),
            "reference_counts": ref_stage_counts,
            "replay_counts": replay_stage_counts,
            "total_variation_distance": stage_tvd,
            "tolerance": args.stage_tvd_tolerance,
            "status": stage_status,
            "missing_replay_stages": missing_replay_stages,
        },
        "claim_discipline": {
            "permitted": [
                "profile-level comparison of process shape, stage mix, runtime/wait, and migration proxy",
                "S3 only means profile comparison passed under configured thresholds",
            ],
            "forbidden": [
                "scheduler speedup",
                "frontier correctness",
                "victim safety",
                "live-agent fidelity unless the reference profile provenance is an original/live harness run",
            ],
            "next_evidence_needed": [
                "collect reference profiles from original/live harness captures",
                "run repeated-trial profile comparisons instead of relying on a single smoke",
                "combine with structural fidelity, mechanism, and safety audits",
            ],
        },
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# HarnessReplayBench system fidelity audit",
        "",
        f"- status: `{payload['status']}`",
        f"- fidelity_level: `{payload['fidelity_level']}`",
        "",
    ]
    if payload.get("blockers"):
        lines.extend(["## Blockers", ""])
        for blocker in payload["blockers"]:
            lines.append(f"- {blocker}")
        lines.append("")
    if payload.get("warnings"):
        lines.extend(["## Warnings", ""])
        for warning in payload["warnings"]:
            lines.append(f"- {warning}")
        lines.append("")
    lines.extend(
        [
            "## Scalar checks",
            "",
            "| metric | reference | replay | relative delta | tolerance | strict | status |",
            "|---|---:|---:|---:|---:|---|---|",
        ]
    )
    for check in payload.get("checks", []):
        lines.append(
            f"| `{check['name']}` | {check['reference']} | {check['replay']} | "
            f"{check['relative_delta']} | {check['tolerance']} | {check['strict']} | `{check['status']}` |"
        )
    stage = payload.get("stage_distribution", {})
    lines.extend(
        [
            "",
            "## Stage distribution",
            "",
            f"- total_variation_distance: {stage.get('total_variation_distance')}",
            f"- tolerance: {stage.get('tolerance')}",
            f"- status: `{stage.get('status')}`",
            "",
            "This audit compares system profile summaries. It does not prove scheduling speedup or frontier correctness.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare two HarnessReplayBench system profile summaries",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--reference-profile", required=True, help="reference/live/original system-profile-summary.json")
    parser.add_argument("--replay-profile", required=True, help="replay system-profile-summary.json")
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    parser.add_argument("--process-tolerance", type=float, default=0.50)
    parser.add_argument("--unique-pid-tolerance", type=float, default=0.60)
    parser.add_argument("--runtime-tolerance", type=float, default=0.60)
    parser.add_argument("--wait-tolerance", type=float, default=1.00)
    parser.add_argument("--migration-tolerance", type=float, default=1.00)
    parser.add_argument("--stage-tvd-tolerance", type=float, default=0.35)
    parser.add_argument("--include-background-stages", action="store_true")
    parser.add_argument("--strict-wait", action="store_true")
    parser.add_argument("--strict-migration", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reference = read_json(pathlib.Path(args.reference_profile).resolve())
    replay = read_json(pathlib.Path(args.replay_profile).resolve())
    payload = compare_profiles(reference, replay, args=args)
    output = pathlib.Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        pathlib.Path(args.markdown_output).resolve().write_text(render_markdown(payload), encoding="utf-8")
    print(json.dumps({"status": payload["status"], "output": str(output)}, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Audit a full real-agent harness endpoint matrix.

``run_real_agent_harness_matrix.py`` can run the complete SWE-agent/OpenHands/
SWE-bench harness under Linux default and FlowCausal variants.  This auditor
checks whether that endpoint matrix is strong enough to support the *endpoint
improvement* part of the FlowCausal story.

It deliberately does not upgrade replay/frontier/mechanism claims.  A passing
report here says: the same real benchmark instance was run under comparable
full-harness scheduler variants, the endpoint artifacts are archived, and the
selected FlowCausal variant improves resolved-rate or elapsed endpoint time
without losing correctness.

When live-mode trials include ``run_live_sandbox.py --victim-command``, this
auditor can also read the per-trial ``victim.log`` JSONL stream and gate the
co-tenant safety/fairness chain on victim p99/max release-lateness slowdown.
Frontier correctness and direct scheduler mechanism still require the
trace/bundle/claim-gate audits.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


SCHEMA = "flowcausal.real_agent_harness_matrix_audit.v1"
MATRIX_SCHEMA = "flowcausal.real_agent_harness_matrix.v1"
STATUS_ORDER = {"pass": 3, "partial": 2, "fail": 1, "missing": 0}


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def norm_status(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in STATUS_ORDER else "missing"


def read_json(path_text: str) -> dict[str, Any]:
    path = pathlib.Path(path_text).expanduser().resolve()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON report is not an object: {path}")
    payload["_path"] = str(path)
    return payload


def fnum(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0.0 else None


def ffloat(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def inum(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def gate(name: str, status: str, evidence: list[str] | None = None, blockers: list[str] | None = None, next_actions: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": norm_status(status),
        "evidence": evidence or [],
        "blockers": blockers or [],
        "next_actions": next_actions or [],
    }


def variant_names(summary: dict[str, Any]) -> list[str]:
    names = set(str(row.get("variant") or "") for row in as_list(summary.get("trials")) if isinstance(row, dict))
    names.update(str(name) for name in as_dict(summary.get("aggregate")))
    return sorted(name for name in names if name)


def is_flowcausal_variant(name: str) -> bool:
    return name.lower().startswith("flowcausal")


def select_flowcausal_variant(summary: dict[str, Any], requested: str = "") -> str:
    if requested:
        return requested
    comparisons = as_dict(summary.get("comparisons"))
    candidates: list[tuple[str, float, int]] = []
    for name in variant_names(summary):
        if not is_flowcausal_variant(name):
            continue
        delta = ffloat(as_dict(comparisons.get(name)).get("elapsed_delta_pct"))
        aggregate = as_dict(as_dict(summary.get("aggregate")).get(name))
        resolved_true = int(aggregate.get("resolved_true") or 0)
        if delta is not None:
            candidates.append((name, delta, resolved_true))
        else:
            # Put variants with no elapsed comparison after comparable variants,
            # but still prefer ones that solved more trials.
            candidates.append((name, 1_000_000.0, resolved_true))
    if not candidates:
        return ""
    return sorted(candidates, key=lambda item: (item[1], -item[2], item[0]))[0][0]


def trials_for(summary: dict[str, Any], variant: str) -> list[dict[str, Any]]:
    return [row for row in as_list(summary.get("trials")) if isinstance(row, dict) and str(row.get("variant") or "") == variant]


def aggregate_for(summary: dict[str, Any], variant: str) -> dict[str, Any]:
    return as_dict(as_dict(summary.get("aggregate")).get(variant))


def check_path(path_text: str) -> tuple[bool, str]:
    if not path_text:
        return False, "<missing>"
    path = pathlib.Path(path_text).expanduser()
    return path.exists(), str(path)


def execution_gate(summary: dict[str, Any], *, baseline: str, flow: str, min_trials: int) -> dict[str, Any]:
    blockers: list[str] = []
    evidence: list[str] = [f"matrix_status={summary.get('status', '')}", f"baseline={baseline}", f"flowcausal={flow}"]
    if summary.get("schema") not in {MATRIX_SCHEMA, None}:
        blockers.append(f"unexpected summary schema: {summary.get('schema')}")
    for variant in (baseline, flow):
        rows = trials_for(summary, variant)
        aggregate = aggregate_for(summary, variant)
        evidence.append(f"{variant}.trials={len(rows)}")
        evidence.append(f"{variant}.passes={aggregate.get('passes', 0)}")
        if len(rows) < min_trials:
            blockers.append(f"{variant} trial count {len(rows)} < required {min_trials}")
        bad = [row for row in rows if row.get("status") != "pass" or row.get("run_returncode") not in (0, None)]
        if bad:
            blockers.append(f"{variant} has {len(bad)} non-pass or nonzero-returncode trial(s)")
    if not flow:
        blockers.append("no FlowCausal variant found")
    status = "pass" if not blockers else "partial" if any(trials_for(summary, item) for item in (baseline, flow)) else "fail"
    return gate(
        "full-harness execution",
        status,
        evidence=evidence,
        blockers=blockers,
        next_actions=[] if status == "pass" else ["rerun the complete harness matrix until baseline and FlowCausal trials all pass"],
    )


def comparability_gate(summary: dict[str, Any], *, baseline: str, flow: str, min_trials: int) -> dict[str, Any]:
    blockers: list[str] = []
    evidence: list[str] = []
    for variant in (baseline, flow):
        aggregate = aggregate_for(summary, variant)
        endpoint_rows = int(aggregate.get("endpoint_rows") or 0)
        unknown = int(aggregate.get("resolved_unknown") or 0)
        evidence.extend(
            [
                f"{variant}.endpoint_rows={endpoint_rows}",
                f"{variant}.resolved_true={aggregate.get('resolved_true', 0)}",
                f"{variant}.resolved_false={aggregate.get('resolved_false', 0)}",
                f"{variant}.resolved_unknown={unknown}",
                f"{variant}.elapsed_sec_mean={aggregate.get('elapsed_sec_mean', '')}",
            ]
        )
        if endpoint_rows < min_trials:
            blockers.append(f"{variant} endpoint rows {endpoint_rows} < required {min_trials}")
        if unknown:
            blockers.append(f"{variant} has {unknown} unresolved/unknown endpoint row(s)")
        if fnum(aggregate.get("elapsed_sec_mean")) is None:
            blockers.append(f"{variant} lacks comparable elapsed_sec_mean")
    status = "pass" if not blockers else "partial"
    return gate(
        "endpoint comparability",
        status,
        evidence=evidence,
        blockers=blockers,
        next_actions=[] if status == "pass" else ["provide framework-produced endpoint JSON for every full-harness trial"],
    )


def improvement_gate(summary: dict[str, Any], *, baseline: str, flow: str, min_improvement_pct: float) -> dict[str, Any]:
    base = aggregate_for(summary, baseline)
    cand = aggregate_for(summary, flow)
    blockers: list[str] = []
    evidence: list[str] = []
    base_resolved = int(base.get("resolved_true") or 0)
    flow_resolved = int(cand.get("resolved_true") or 0)
    base_false = int(base.get("resolved_false") or 0)
    flow_false = int(cand.get("resolved_false") or 0)
    base_elapsed = fnum(base.get("elapsed_sec_mean"))
    flow_elapsed = fnum(cand.get("elapsed_sec_mean"))
    comparison = as_dict(as_dict(summary.get("comparisons")).get(flow))
    delta_pct = ffloat(comparison.get("elapsed_delta_pct"))
    if delta_pct is None and base_elapsed is not None and flow_elapsed is not None and base_elapsed > 0:
        delta_pct = (flow_elapsed - base_elapsed) * 100.0 / base_elapsed
    evidence.extend(
        [
            f"{baseline}.resolved_true={base_resolved}",
            f"{flow}.resolved_true={flow_resolved}",
            f"{baseline}.resolved_false={base_false}",
            f"{flow}.resolved_false={flow_false}",
            f"{baseline}.elapsed_sec_mean={base_elapsed}",
            f"{flow}.elapsed_sec_mean={flow_elapsed}",
            f"elapsed_delta_pct={delta_pct}",
            f"required_elapsed_improvement_pct={min_improvement_pct}",
        ]
    )
    if flow_resolved < base_resolved:
        blockers.append(f"{flow} solves fewer endpoint trials than {baseline}: {flow_resolved} < {base_resolved}")
        status = "fail"
    elif flow_resolved > base_resolved:
        status = "pass"
        evidence.append("resolved-rate improvement is sufficient endpoint evidence")
    elif delta_pct is None:
        blockers.append("no comparable elapsed endpoint delta")
        status = "partial"
    elif delta_pct <= -abs(min_improvement_pct):
        status = "pass"
        evidence.append("elapsed endpoint time improves without reducing resolved count")
    elif delta_pct <= 0:
        blockers.append(f"elapsed improvement {-delta_pct:.2f}% < required {abs(min_improvement_pct):.2f}%")
        status = "partial"
    else:
        blockers.append(f"{flow} is slower than {baseline} by {delta_pct:.2f}%")
        status = "fail"
    return gate(
        "endpoint improvement",
        status,
        evidence=evidence,
        blockers=blockers,
        next_actions=[] if status == "pass" else ["collect more full-harness trials or revise the scheduling policy if endpoint does not improve"],
    )


def variant_coverage_gate(summary: dict[str, Any], *, baseline: str, flow: str) -> dict[str, Any]:
    names = variant_names(summary)
    blockers: list[str] = []
    if baseline not in names:
        blockers.append(f"baseline variant missing: {baseline}")
    if not flow:
        blockers.append("FlowCausal variant missing")
    if flow and flow not in names:
        blockers.append(f"selected FlowCausal variant missing: {flow}")
    has_observed = "default_observed" in names
    if not has_observed:
        blockers.append("default_observed variant is absent; scheduler observation overhead is not separated")
    has_no_width = "flowcausal_no_width" in names
    if not has_no_width:
        blockers.append("flowcausal_no_width variant is absent; width-control ablation is not separated")
    hard_blockers = [item for item in blockers if "absent" not in item and "not separated" not in item]
    status = "pass" if not blockers else "partial" if not hard_blockers else "fail"
    return gate(
        "baseline comparison",
        status,
        evidence=[f"variants={','.join(names)}"],
        blockers=blockers,
        next_actions=[] if status == "pass" else ["include Linux default, observed default, FlowCausal no-width, and FlowCausal width endpoint rows"],
    )


def artifact_gate(summary: dict[str, Any], *, baseline: str, flow: str, check_paths: bool) -> dict[str, Any]:
    blockers: list[str] = []
    evidence: list[str] = [f"summary={summary.get('_path', '')}"]
    weak_endpoint = 0
    for variant in (baseline, flow):
        for row in trials_for(summary, variant):
            prefix = f"{variant}/trial-{row.get('trial')}"
            raw = str(row.get("raw_agent_output") or "")
            endpoint = str(row.get("full_harness_result") or "")
            capture = str(row.get("capture_dir") or "")
            if "captured-endpoint" in endpoint:
                weak_endpoint += 1
            for label, path_text in (("raw_agent_output", raw), ("full_harness_result", endpoint), ("capture_dir", capture)):
                if not path_text:
                    blockers.append(f"{prefix}: missing {label} path")
                    continue
                if check_paths:
                    exists, path = check_path(path_text)
                    if not exists:
                        blockers.append(f"{prefix}: {label} path does not exist: {path}")
            warnings = " ".join(str(item) for item in as_list(row.get("warnings")))
            if "captured command metadata" in warnings or "resolved=null" in warnings:
                weak_endpoint += 1
    if weak_endpoint:
        blockers.append(f"{weak_endpoint} trial(s) use captured metadata or unresolved endpoint instead of framework-produced endpoint")
    status = "pass" if not blockers else "partial"
    return gate(
        "reproducibility/artifact",
        status,
        evidence=evidence,
        blockers=blockers[:20],
        next_actions=[] if status == "pass" else ["archive raw trajectory, full-harness endpoint JSON, capture report, and live scheduler logs for every trial"],
    )


def victim_log_candidates(row: dict[str, Any]) -> list[pathlib.Path]:
    """Return likely victim.log paths for a trial row.

    Native harness rows store ``live_dir`` for live-mode variants.  Imported or
    older rows may only have ``capture_dir``; for those we also try the sibling
    ``live/victim.log`` path.
    """

    candidates: list[pathlib.Path] = []
    seen: set[str] = set()

    def add(path_value: Any) -> None:
        text = str(path_value or "").strip()
        if not text:
            return
        path = pathlib.Path(text).expanduser()
        key = str(path)
        if key not in seen:
            seen.add(key)
            candidates.append(path)

    add(row.get("victim_log"))
    live_dir = str(row.get("live_dir") or "").strip()
    if live_dir:
        add(pathlib.Path(live_dir) / "victim.log")
    capture_dir = str(row.get("capture_dir") or "").strip()
    if capture_dir:
        capture_path = pathlib.Path(capture_dir)
        add(capture_path / "victim.log")
        add(capture_path.parent / "live" / "victim.log")
    trial_dir = str(row.get("trial_dir") or "").strip()
    if trial_dir:
        add(pathlib.Path(trial_dir) / "live" / "victim.log")
    return candidates


def read_victim_summary(path: pathlib.Path) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    blockers: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        return None, [f"victim log does not exist: {path}"], warnings
    latest: dict[str, Any] | None = None
    invalid_lines = 0
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return None, [f"failed to read victim log {path}: {exc}"], warnings
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            invalid_lines += 1
            continue
        if isinstance(item, dict) and item.get("type") == "victim_summary":
            latest = item
    if invalid_lines:
        warnings.append(f"{path}: ignored {invalid_lines} non-JSON victim log line(s)")
    if latest is None:
        blockers.append(f"victim_summary not found in {path}")
    return latest, blockers, warnings


def victim_metric_from_row(row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    prefix = f"{row.get('variant', '')}/trial-{row.get('trial')}"
    candidates = victim_log_candidates(row)
    if not candidates:
        return None, [], [f"{prefix}: no victim log path recorded"]
    path = next((item for item in candidates if item.exists()), candidates[0])
    if not path.exists():
        return None, [], [f"{prefix}: victim log missing at {path}"]
    summary, blockers, warnings = read_victim_summary(path)
    blockers = [f"{prefix}: {item}" for item in blockers]
    warnings = [f"{prefix}: {item}" for item in warnings]
    if summary is None:
        return None, blockers, warnings
    lateness = as_dict(summary.get("lateness"))
    p99_ms = fnum(lateness.get("p99_ms"))
    max_ms = fnum(lateness.get("max_ms"))
    iterations = inum(summary.get("iterations"))
    returncode = inum(summary.get("returncode"))
    metric_blockers: list[str] = []
    if p99_ms is None:
        metric_blockers.append("victim_summary lacks lateness.p99_ms")
    if max_ms is None:
        metric_blockers.append("victim_summary lacks lateness.max_ms")
    if iterations is None or iterations <= 0:
        metric_blockers.append(f"victim iterations not positive: {summary.get('iterations')}")
    if returncode not in (0, None):
        metric_blockers.append(f"victim returncode is nonzero: {summary.get('returncode')}")
    if metric_blockers:
        return None, [f"{prefix}: {item}" for item in metric_blockers], warnings
    return (
        {
            "variant": row.get("variant", ""),
            "trial": row.get("trial"),
            "path": str(path),
            "p99_ms": p99_ms,
            "max_ms": max_ms,
            "iterations": iterations,
            "returncode": returncode,
        },
        blockers,
        warnings,
    )


def collect_victim_metrics(summary: dict[str, Any], variant: str) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    metrics: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for row in trials_for(summary, variant):
        metric, row_blockers, row_warnings = victim_metric_from_row(row)
        if metric is not None:
            metrics.append(metric)
        blockers.extend(row_blockers)
        warnings.extend(row_warnings)
    return metrics, blockers, warnings


def mean_metric(metrics: list[dict[str, Any]], key: str) -> float | None:
    values = [float(item[key]) for item in metrics if fnum(item.get(key)) is not None]
    return sum(values) / len(values) if values else None


def slowdown_pct(baseline_value: float | None, flow_value: float | None) -> float | None:
    if baseline_value is None or flow_value is None:
        return None
    if baseline_value == 0.0:
        return 0.0 if flow_value == 0.0 else None
    return (flow_value - baseline_value) * 100.0 / baseline_value


def victim_safety_gate(
    summary: dict[str, Any],
    *,
    baseline: str,
    flow: str,
    min_trials: int,
    safety_baseline_variant: str,
    safety_flowcausal_variant: str,
    max_p99_slowdown_pct: float,
    max_max_slowdown_pct: float,
    max_p99_ms: float,
    max_max_ms: float,
) -> dict[str, Any]:
    requested_baseline = safety_baseline_variant or baseline
    safety_flow = safety_flowcausal_variant or flow
    baseline_metrics, baseline_blockers, baseline_warnings = collect_victim_metrics(summary, requested_baseline)
    safety_baseline = requested_baseline
    fallback_used = ""
    if not safety_baseline_variant and not baseline_metrics and requested_baseline != "default_observed":
        fallback_metrics, fallback_blockers, fallback_warnings = collect_victim_metrics(summary, "default_observed")
        if fallback_metrics:
            safety_baseline = "default_observed"
            baseline_metrics = fallback_metrics
            baseline_blockers = fallback_blockers
            baseline_warnings = fallback_warnings
            fallback_used = "default_observed"

    flow_metrics, flow_blockers, flow_warnings = collect_victim_metrics(summary, safety_flow)
    baseline_p99 = mean_metric(baseline_metrics, "p99_ms")
    flow_p99 = mean_metric(flow_metrics, "p99_ms")
    baseline_max = mean_metric(baseline_metrics, "max_ms")
    flow_max = mean_metric(flow_metrics, "max_ms")
    p99_delta = slowdown_pct(baseline_p99, flow_p99)
    max_delta = slowdown_pct(baseline_max, flow_max)

    evidence = [
        f"safety_baseline={safety_baseline}",
        f"safety_flowcausal={safety_flow}",
        f"{safety_baseline}.victim_trials={len(baseline_metrics)}",
        f"{safety_flow}.victim_trials={len(flow_metrics)}",
        f"{safety_baseline}.victim_p99_ms_mean={baseline_p99}",
        f"{safety_flow}.victim_p99_ms_mean={flow_p99}",
        f"victim_p99_slowdown_pct={p99_delta}",
        f"allowed_victim_p99_slowdown_pct={max_p99_slowdown_pct}",
        f"{safety_baseline}.victim_max_ms_mean={baseline_max}",
        f"{safety_flow}.victim_max_ms_mean={flow_max}",
        f"victim_max_slowdown_pct={max_delta}",
        f"allowed_victim_max_slowdown_pct={max_max_slowdown_pct}",
    ]
    if fallback_used:
        evidence.append(f"safety_baseline_fallback={fallback_used}")
    for metric in baseline_metrics[:3] + flow_metrics[:3]:
        evidence.append(f"victim_log={metric.get('path')}")
    if baseline_warnings or flow_warnings:
        evidence.extend(f"warning={item}" for item in (baseline_warnings + flow_warnings)[:8])

    blockers = [*baseline_blockers, *flow_blockers]
    if not baseline_metrics:
        blockers.append(f"{safety_baseline} has no parsed victim_summary rows")
    if not flow_metrics:
        blockers.append(f"{safety_flow} has no parsed victim_summary rows")
    if baseline_metrics and len(baseline_metrics) < min_trials:
        blockers.append(f"{safety_baseline} victim trial count {len(baseline_metrics)} < required {min_trials}")
    if flow_metrics and len(flow_metrics) < min_trials:
        blockers.append(f"{safety_flow} victim trial count {len(flow_metrics)} < required {min_trials}")

    failures: list[str] = []
    if p99_delta is None and baseline_metrics and flow_metrics:
        blockers.append("cannot compute victim p99 slowdown percentage because baseline p99 is zero or missing")
    elif p99_delta is not None and p99_delta > max_p99_slowdown_pct:
        failures.append(f"victim p99 slowdown {p99_delta:.2f}% > allowed {max_p99_slowdown_pct:.2f}%")
    if max_delta is None and baseline_metrics and flow_metrics:
        blockers.append("cannot compute victim max slowdown percentage because baseline max is zero or missing")
    elif max_delta is not None and max_delta > max_max_slowdown_pct:
        failures.append(f"victim max slowdown {max_delta:.2f}% > allowed {max_max_slowdown_pct:.2f}%")
    if max_p99_ms > 0.0 and flow_p99 is not None and flow_p99 > max_p99_ms:
        failures.append(f"victim p99 {flow_p99:.3f}ms > absolute cap {max_p99_ms:.3f}ms")
    if max_max_ms > 0.0 and flow_max is not None and flow_max > max_max_ms:
        failures.append(f"victim max {flow_max:.3f}ms > absolute cap {max_max_ms:.3f}ms")

    if not baseline_metrics and not flow_metrics:
        status = "missing"
    elif failures:
        status = "fail"
    elif blockers:
        status = "partial"
    else:
        status = "pass"

    return gate(
        "safety/fairness",
        status,
        evidence=evidence,
        blockers=[*failures, *blockers][:30],
        next_actions=[] if status == "pass" else ["run live full-harness trials with --victim-command for baseline and FlowCausal variants"],
    )


def chain_rows(endpoint: dict[str, Any], coverage: dict[str, Any], artifact: dict[str, Any], execution: dict[str, Any], safety: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        gate(
            "frontier correctness",
            "missing",
            blockers=["full-harness endpoint matrix does not prove selected frontier correctness"],
            next_actions=["add manual/oracle frontier annotations and audit the replay bundle"],
        ),
        endpoint,
        gate(
            "scheduler mechanism",
            "partial" if execution.get("status") == "pass" else "missing",
            evidence=["full-harness FlowCausal variants were executed"] if execution.get("status") == "pass" else [],
            blockers=["endpoint matrix alone does not prove direct CPU/frontier-wait mechanism"],
            next_actions=["run trace replay/claim-gate audit with controller, flowmon, and schedstat evidence"],
        ),
        safety,
        coverage,
        artifact,
    ]


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    summary = read_json(args.summary)
    baseline = args.baseline_variant or str(summary.get("baseline_variant") or "default")
    flow = select_flowcausal_variant(summary, args.flowcausal_variant)
    execution = execution_gate(summary, baseline=baseline, flow=flow, min_trials=args.min_trials)
    comparable = comparability_gate(summary, baseline=baseline, flow=flow, min_trials=args.min_trials)
    endpoint = improvement_gate(summary, baseline=baseline, flow=flow, min_improvement_pct=args.min_improvement_pct)
    coverage = variant_coverage_gate(summary, baseline=baseline, flow=flow)
    artifact = artifact_gate(summary, baseline=baseline, flow=flow, check_paths=not args.no_path_check)
    safety = victim_safety_gate(
        summary,
        baseline=baseline,
        flow=flow,
        min_trials=args.min_trials,
        safety_baseline_variant=args.safety_baseline_variant,
        safety_flowcausal_variant=args.safety_flowcausal_variant,
        max_p99_slowdown_pct=args.max_victim_p99_slowdown_pct,
        max_max_slowdown_pct=args.max_victim_max_slowdown_pct,
        max_p99_ms=args.max_victim_p99_ms,
        max_max_ms=args.max_victim_max_ms,
    )
    gates = [execution, comparable, endpoint, coverage, artifact]
    if args.require_victim_safety:
        gates.append(safety)
    if all(row["status"] == "pass" for row in gates):
        status = "pass"
        highest = "full-harness endpoint-improvement evidence"
        if args.require_victim_safety:
            highest = "full-harness endpoint-improvement evidence with victim safety/fairness"
    elif any(row["status"] in {"pass", "partial"} for row in gates):
        status = "partial"
        highest = "partial full-harness endpoint evidence"
    else:
        status = "fail"
        highest = "not endpoint-ready"
    blockers: list[str] = []
    for row in gates:
        blockers.extend(str(item) for item in as_list(row.get("blockers")))
    chains = chain_rows(endpoint, coverage, artifact, execution, safety)
    return {
        "schema": SCHEMA,
        "status": status,
        "highest_supported_scope": highest,
        "summary": {
            "input_summary": summary.get("_path", ""),
            "instance_id": summary.get("instance_id", ""),
            "baseline_variant": baseline,
            "flowcausal_variant": flow,
            "min_trials": args.min_trials,
            "min_improvement_pct": args.min_improvement_pct,
            "require_victim_safety": bool(args.require_victim_safety),
        },
        "gates": gates,
        "chains": chains,
        "blockers": blockers[:40],
        "notes": [
            "This audit only covers full-harness endpoint evidence.",
            "A EuroSys-ready claim still needs frontier correctness, direct scheduler mechanism, safety/fairness, and replay/artifact audits.",
        ],
    }


def write_markdown(report: dict[str, Any], path_text: str) -> None:
    path = pathlib.Path(path_text).expanduser().resolve()
    lines = [
        "# FlowCausal full-harness endpoint matrix audit",
        "",
        f"Status: `{report.get('status')}`",
        "",
        f"Highest supported scope: `{report.get('highest_supported_scope')}`",
        "",
        "## Gates",
        "",
        "| gate | status | evidence | blockers |",
        "| --- | --- | --- | --- |",
    ]
    for row in as_list(report.get("gates")):
        lines.append(
            "| {name} | {status} | {evidence} | {blockers} |".format(
                name=row.get("name", ""),
                status=row.get("status", ""),
                evidence="<br>".join(str(item) for item in as_list(row.get("evidence"))),
                blockers="<br>".join(str(item) for item in as_list(row.get("blockers"))),
            )
        )
    lines.extend(["", "## Six-chain interpretation", "", "| chain | status | next action |", "| --- | --- | --- |"])
    for row in as_list(report.get("chains")):
        lines.append(
            "| {name} | {status} | {actions} |".format(
                name=row.get("name", ""),
                status=row.get("status", ""),
                actions="<br>".join(str(item) for item in as_list(row.get("next_actions"))),
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit FlowCausal full real-agent harness endpoint matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--summary", required=True, help="harness-matrix-summary.json")
    parser.add_argument("--baseline-variant", default="")
    parser.add_argument("--flowcausal-variant", default="", help="default: best flowcausal* variant by elapsed delta")
    parser.add_argument("--min-trials", type=int, default=3)
    parser.add_argument("--min-improvement-pct", type=float, default=2.0)
    parser.add_argument("--no-path-check", action="store_true", help="do not require archived trial paths to exist on this machine")
    parser.add_argument("--require-victim-safety", action="store_true", help="make victim safety/fairness a top-level audit gate")
    parser.add_argument("--safety-baseline-variant", default="", help="variant used as the victim-safety baseline; default uses --baseline-variant, falling back to default_observed if it has victim logs")
    parser.add_argument("--safety-flowcausal-variant", default="", help="FlowCausal variant used for victim-safety comparison; default uses selected --flowcausal-variant")
    parser.add_argument("--max-victim-p99-slowdown-pct", type=float, default=25.0)
    parser.add_argument("--max-victim-max-slowdown-pct", type=float, default=50.0)
    parser.add_argument("--max-victim-p99-ms", type=float, default=0.0, help="optional absolute p99 lateness cap; 0 disables")
    parser.add_argument("--max-victim-max-ms", type=float, default=0.0, help="optional absolute max lateness cap; 0 disables")
    parser.add_argument("--output", help="write JSON audit report")
    parser.add_argument("--markdown-output", help="write Markdown audit report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.min_trials <= 0:
        raise SystemExit("--min-trials must be positive")
    if args.max_victim_p99_slowdown_pct < 0 or args.max_victim_max_slowdown_pct < 0:
        raise SystemExit("victim slowdown thresholds must be non-negative")
    if args.max_victim_p99_ms < 0 or args.max_victim_max_ms < 0:
        raise SystemExit("victim absolute latency caps must be non-negative")
    report = summarize(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        write_markdown(report, args.markdown_output)
    print(text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Audit FlowCausal matrix summaries against the paper claim gate.

The paper-level rule is stricter than "the workload got faster": a result must
first show a CPU-side frontier problem, then prove that controller hints reached
BPF, then move a direct mechanism metric, then improve stage/episode outcome,
and finally pass victim/fairness checks.  This helper maps existing
``matrix-summary.json`` files onto that five-gate structure.

The audit is intentionally conservative.  If evidence is present but not tied to
a comparable default/oracle mechanism baseline, the gate is marked ``partial``
and the highest supported level remains L1.
"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


STATUS_PASS = "pass"
STATUS_PARTIAL = "partial"
STATUS_FAIL = "fail"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_path(obj: dict[str, Any], *path: str) -> Any:
    cur: Any = obj
    for item in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(item)
    return cur


def variant_name(variant: dict[str, Any]) -> str:
    return str(variant.get("name", ""))


def is_baseline(variant: dict[str, Any]) -> bool:
    name = variant_name(variant).lower()
    kind = str(variant.get("kind", "")).lower()
    return kind in {"default", "baseline"} or name.startswith("default")


def is_flowcausal(variant: dict[str, Any]) -> bool:
    return not is_baseline(variant) and variant_name(variant).lower().startswith("flowcausal")


def is_shared_only_control(variant: dict[str, Any]) -> bool:
    name = variant_name(variant).lower()
    return bool(variant.get("hint_dry_run")) or "shared_only" in name or "shared-only" in name


def is_optimization_candidate(variant: dict[str, Any]) -> bool:
    return is_flowcausal(variant) and not is_shared_only_control(variant)


def aggregate(variant: dict[str, Any]) -> dict[str, Any]:
    return as_dict(variant.get("aggregate"))


def flowmon(variant: dict[str, Any]) -> dict[str, Any]:
    return as_dict(aggregate(variant).get("flowmon_mean"))


def characterization(variant: dict[str, Any]) -> dict[str, Any]:
    return as_dict(aggregate(variant).get("characterization_mean"))


def controller(variant: dict[str, Any]) -> dict[str, Any]:
    return as_dict(aggregate(variant).get("controller"))


def victim(variant: dict[str, Any]) -> dict[str, Any]:
    return as_dict(aggregate(variant).get("victim_mean"))


def has_victim_evidence(variant: dict[str, Any]) -> bool:
    data = victim(variant)
    if not data:
        return False
    if fnum(data.get("iterations")) > 0:
        return True
    return any(
        fnum(data.get(key)) > 0
        for key in ("lateness_mean_ms", "lateness_p95_ms", "lateness_p99_ms", "lateness_max_ms")
    )


def trial_count(variant: dict[str, Any]) -> int:
    status = as_dict(aggregate(variant).get("trial_status"))
    if fnum(status.get("total")) > 0:
        return int(fnum(status.get("total")))
    trials = variant.get("trials")
    if isinstance(trials, list):
        return len(trials)
    if fnum(trials) > 0:
        return int(fnum(trials))
    summary = as_dict(aggregate(variant).get("total_summary"))
    if fnum(summary.get("n")) > 0:
        return int(fnum(summary.get("n")))
    return 0


def successful_trial_count(variant: dict[str, Any]) -> int:
    status = as_dict(aggregate(variant).get("trial_status"))
    if fnum(status.get("successful")) > 0:
        return int(fnum(status.get("successful")))
    total = trial_count(variant)
    failed = int(fnum(status.get("failed")))
    return max(0, total - failed)


def outcome_metric(variant: dict[str, Any]) -> tuple[str, float] | None:
    agg = aggregate(variant)
    critical = fnum(agg.get("critical_mean_sec"))
    if critical > 0:
        return "critical_mean_sec", critical

    total = fnum(agg.get("total_mean_sec"))
    if total > 0:
        return "total_mean_sec", total

    test16 = as_dict(agg.get("test16_summary"))
    for key in (
        "task_completion_ms_mean",
        "end_to_end_ms_mean",
        "critical_path_ms_mean",
    ):
        metric = as_dict(test16.get(key))
        mean = fnum(metric.get("mean"))
        if mean > 0:
            return f"test16_{key}", mean
    return None


def direct_mechanism_metric(variant: dict[str, Any]) -> tuple[str, float] | None:
    char = characterization(variant)
    normalized_wait = fnum(char.get("frontier_wait_per_frontier_sample_ns"))
    if normalized_wait > 0:
        return "schedstat_frontier_wait_per_frontier_sample_ns", normalized_wait
    char_wait = fnum(char.get("frontier_sched_wait_delta_ns"))
    frontier_samples = fnum(char.get("samples_with_frontier"))
    if char_wait > 0 and frontier_samples > 0:
        return "schedstat_frontier_wait_per_frontier_sample_ns", char_wait / frontier_samples
    char_wait = fnum(char.get("frontier_sched_wait_delta_ns"))
    if char_wait > 0:
        return "schedstat_frontier_wait_ns", char_wait

    fm = flowmon(variant)
    for key in (
        "frontier_runnable_wait_ns",
        "global_shared_wait_pressure_wait_ns",
        "frontier_runtime_ns",
    ):
        value = fnum(fm.get(key))
        if value > 0:
            return f"bpf_{key}", value
    return None


def gate(name: str, status: str, evidence: list[str] | None = None, blockers: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "evidence": evidence or [],
        "blockers": blockers or [],
    }


def audit_workload_gate(variants: list[dict[str, Any]]) -> dict[str, Any]:
    baselines = [v for v in variants if is_baseline(v)]
    flow_variants = [v for v in variants if is_optimization_candidate(v)]
    evidence: list[str] = []
    blockers: list[str] = []

    if baselines and outcome_metric(baselines[0]):
        metric, value = outcome_metric(baselines[0]) or ("", 0.0)
        evidence.append(f"baseline outcome metric present: {metric}={value:.4g}")
    else:
        blockers.append("no default/taskset baseline outcome metric")

    wait_variants = []
    for v in flow_variants:
        flow_wait = fnum(flowmon(v).get("frontier_runnable_wait_ns"))
        char_wait = fnum(characterization(v).get("frontier_sched_wait_delta_ns"))
        wait = max(flow_wait, char_wait)
        if wait > 0:
            wait_variants.append((variant_name(v), wait))
    if wait_variants:
        name, value = max(wait_variants, key=lambda item: item[1])
        evidence.append(f"CPU-side selected-frontier wait observed in {name}: {value:.0f} ns")
    elif flow_variants:
        blockers.append("no positive selected-frontier runnable-wait counter")
    else:
        blockers.append("no FlowCausal variant")

    if not blockers:
        return gate("workload characterization", STATUS_PASS, evidence, blockers)
    if evidence:
        return gate("workload characterization", STATUS_PARTIAL, evidence, blockers)
    return gate("workload characterization", STATUS_FAIL, evidence, blockers)


def audit_admission_gate(variants: list[dict[str, Any]]) -> dict[str, Any]:
    evidence: list[str] = []
    blockers: list[str] = []
    flow_variants = [v for v in variants if is_optimization_candidate(v)]

    controller_rows = [
        (variant_name(v), fnum(controller(v).get("frontier_records")))
        for v in flow_variants
        if fnum(controller(v).get("frontier_records")) > 0
    ]
    bpf_rows = []
    for v in flow_variants:
        fm = flowmon(v)
        routed = (
            fnum(fm.get("frontier_enqueues"))
            + fnum(fm.get("fallback_enqueues"))
            + fnum(fm.get("width_held_enqueues"))
        )
        if routed > 0:
            bpf_rows.append((variant_name(v), routed))

    if controller_rows:
        name, value = max(controller_rows, key=lambda item: item[1])
        evidence.append(f"controller selected frontiers in {name}: {value:.0f} records")
    else:
        blockers.append("no controller frontier records")

    if bpf_rows:
        name, value = max(bpf_rows, key=lambda item: item[1])
        evidence.append(f"BPF routed marked tasks in {name}: {value:.1f} enqueues")
    else:
        blockers.append("no BPF frontier/fallback/width-held routing counters")

    if not blockers:
        return gate("controller/BPF admission", STATUS_PASS, evidence, blockers)
    if evidence:
        return gate("controller/BPF admission", STATUS_PARTIAL, evidence, blockers)
    return gate("controller/BPF admission", STATUS_FAIL, evidence, blockers)


def audit_mechanism_gate(variants: list[dict[str, Any]]) -> dict[str, Any]:
    flow_variants = [v for v in variants if is_optimization_candidate(v)]
    baselines = [v for v in variants if is_baseline(v)]
    metrics = [
        (variant_name(v), *(direct_mechanism_metric(v) or ("", 0.0)))
        for v in flow_variants
        if direct_mechanism_metric(v) is not None
    ]
    baseline_metrics = [
        (variant_name(v), *(direct_mechanism_metric(v) or ("", 0.0)))
        for v in baselines
        if direct_mechanism_metric(v) is not None
    ]
    evidence: list[str] = []
    blockers: list[str] = []

    if not metrics:
        return gate(
            "direct CPU mechanism",
            STATUS_FAIL,
            [],
            ["no positive direct CPU mechanism metric in FlowCausal variants"],
        )

    by_metric: dict[str, list[tuple[str, float]]] = {}
    for name, metric, value in metrics:
        by_metric.setdefault(metric, []).append((name, value))
    baseline_by_metric: dict[str, list[tuple[str, float]]] = {}
    for name, metric, value in baseline_metrics:
        baseline_by_metric.setdefault(metric, []).append((name, value))
    for metric, rows in sorted(by_metric.items()):
        rows = sorted(rows, key=lambda item: item[1])
        evidence.append(
            f"{metric} observed across {len(rows)} variant(s): best={rows[0][0]} {rows[0][1]:.0f}"
        )
    for metric, rows in sorted(baseline_by_metric.items()):
        rows = sorted(rows, key=lambda item: item[1])
        evidence.append(f"default {metric}: best={rows[0][0]} {rows[0][1]:.0f}")

    for metric, baseline_rows in sorted(baseline_by_metric.items()):
        flow_rows = by_metric.get(metric, [])
        if not flow_rows:
            continue
        base_name, base_value = min(baseline_rows, key=lambda item: item[1])
        best_name, best_value = min(flow_rows, key=lambda item: item[1])
        delta = (best_value - base_value) / base_value if base_value > 0 else 0.0
        evidence.append(
            f"comparable {metric}: {best_name}={best_value:.0f}, "
            f"{base_name}={base_value:.0f}, delta={delta * 100:+.1f}%"
        )
        if "wait" in metric and delta <= -0.02:
            return gate("direct CPU mechanism", STATUS_PASS, evidence, blockers)
        blockers.append(f"comparable {metric} did not fall by at least 2%")

    has_oracle_or_sync = any(
        any(token in variant_name(v).lower() for token in ("oracle", "sync"))
        and direct_mechanism_metric(v) is not None
        for v in variants
    )
    if baseline_by_metric:
        if not any(metric in by_metric for metric in baseline_by_metric):
            blockers.append("default mechanism metric exists but no FlowCausal row has the same metric")
        return gate("direct CPU mechanism", STATUS_PARTIAL, evidence, blockers)
    if has_oracle_or_sync:
        blockers.append("oracle/sync mechanism metric exists but no default comparable delta is present")
        return gate("direct CPU mechanism", STATUS_PARTIAL, evidence, blockers)

    if any(len(rows) >= 2 for rows in by_metric.values()):
        blockers.append("direct mechanism counters exist but lack default/oracle mechanism baseline")
        return gate("direct CPU mechanism", STATUS_PARTIAL, evidence, blockers)

    blockers.append("no comparable direct-mechanism delta")
    return gate("direct CPU mechanism", STATUS_PARTIAL, evidence, blockers)


def audit_outcome_gate(variants: list[dict[str, Any]]) -> dict[str, Any]:
    baselines = [v for v in variants if is_baseline(v)]
    flow_variants = [v for v in variants if is_optimization_candidate(v)]
    evidence: list[str] = []
    blockers: list[str] = []

    baseline_metric = outcome_metric(baselines[0]) if baselines else None
    if baseline_metric is None:
        return gate("stage/episode outcome", STATUS_FAIL, [], ["no default outcome metric"])
    metric_name, baseline_value = baseline_metric

    comparable: list[tuple[str, float, int]] = []
    for v in flow_variants:
        metric = outcome_metric(v)
        if metric and metric[0] == metric_name:
            comparable.append((variant_name(v), metric[1], trial_count(v)))
    if not comparable:
        return gate("stage/episode outcome", STATUS_FAIL, [], ["no comparable FlowCausal outcome metric"])

    best_name, best_value, best_trials = min(comparable, key=lambda item: item[1])
    delta = (best_value - baseline_value) / baseline_value if baseline_value > 0 else 0.0
    evidence.append(
        f"best comparable outcome {metric_name}: {best_name}={best_value:.4g}, "
        f"default={baseline_value:.4g}, delta={delta * 100:+.1f}%"
    )
    if best_trials >= 3:
        evidence.append(f"{best_name} has {best_trials} trial(s)")
    else:
        blockers.append(f"{best_name} has fewer than 3 trials")

    if delta <= -0.02 and best_trials >= 3:
        return gate("stage/episode outcome", STATUS_PASS, evidence, blockers)
    if delta <= 0.02:
        blockers.append("outcome is neutral or too small for an end-to-end claim")
        return gate("stage/episode outcome", STATUS_PARTIAL, evidence, blockers)
    blockers.append("best FlowCausal outcome is slower than default")
    return gate("stage/episode outcome", STATUS_FAIL, evidence, blockers)


def audit_safety_gate(variants: list[dict[str, Any]]) -> dict[str, Any]:
    baselines = [v for v in variants if is_baseline(v)]
    flow_variants = [v for v in variants if is_flowcausal(v)]
    evidence: list[str] = []
    blockers: list[str] = []

    if not baselines or not has_victim_evidence(baselines[0]):
        return gate("victim/fairness safety", STATUS_FAIL, [], ["no default victim sidecar metric"])
    base = victim(baselines[0])
    flow_with_victim = [v for v in flow_variants if has_victim_evidence(v)]
    if not flow_with_victim:
        return gate("victim/fairness safety", STATUS_FAIL, [], ["no FlowCausal victim sidecar metric"])

    metrics = ("lateness_p95_ms", "lateness_p99_ms", "lateness_max_ms")
    safe_rows = []
    unsafe_rows = []
    for v in flow_with_victim:
        bad = []
        for key in metrics:
            base_value = fnum(base.get(key))
            value = fnum(victim(v).get(key))
            allowed = base_value + max(1.0, base_value * 0.2)
            if value > allowed:
                bad.append(f"{key} {value:.3f}>{allowed:.3f}ms")
        if bad:
            unsafe_rows.append((variant_name(v), bad))
        else:
            safe_rows.append(variant_name(v))

    if safe_rows:
        evidence.append("victim sidecar within conservative envelope for: " + ", ".join(safe_rows[:4]))
    if unsafe_rows:
        for name, bad in unsafe_rows[:4]:
            blockers.append(f"{name} violates victim envelope: {', '.join(bad)}")

    if unsafe_rows:
        return gate("victim/fairness safety", STATUS_FAIL, evidence, blockers)
    min_trials = min((trial_count(v) for v in flow_with_victim), default=0)
    if min_trials >= 3:
        return gate("victim/fairness safety", STATUS_PASS, evidence, blockers)
    blockers.append("victim evidence has fewer than 3 trials")
    return gate("victim/fairness safety", STATUS_PARTIAL, evidence, blockers)


def highest_supported_level(gates: list[dict[str, Any]]) -> str:
    statuses = [str(g.get("status", STATUS_FAIL)) for g in gates]
    if statuses[0] == STATUS_FAIL:
        return "C0/L0: workload not characterized"
    if statuses[1] != STATUS_PASS:
        return "C1/L0: characterization without BPF admission proof"
    if statuses[2] != STATUS_PASS:
        return "L1: mechanism connectivity"
    if statuses[3] != STATUS_PASS:
        return "L2: direct mechanism evidence only"
    if statuses[4] != STATUS_PASS:
        return "L3: outcome improvement with unresolved safety boundary"
    return "L4: candidate outcome+safety result; still needs workload breadth for L5"


def audit_matrix(matrix: dict[str, Any]) -> dict[str, Any]:
    variants = [v for v in as_list(matrix.get("variants")) if isinstance(v, dict)]
    gates = [
        audit_workload_gate(variants),
        audit_admission_gate(variants),
        audit_mechanism_gate(variants),
        audit_outcome_gate(variants),
        audit_safety_gate(variants),
    ]
    return {
        "highest_supported_level": highest_supported_level(gates),
        "gates": gates,
        "forbidden_claims": [
            "FlowCausal speedup without a passing direct-mechanism gate",
            "frontier inference accuracy without oracle/manual labels",
            "victim slowdown is bounded without a passing safety gate",
            "production readiness",
        ],
    }


def markdown_report(path: pathlib.Path, report: dict[str, Any]) -> str:
    lines = [
        f"# Claim-gate audit for `{path}`",
        "",
        f"- Highest supported level: `{report['highest_supported_level']}`",
        "",
        "| Gate | Status | Evidence | Blockers |",
        "| --- | --- | --- | --- |",
    ]
    for item in report["gates"]:
        evidence = "<br>".join(str(x) for x in item.get("evidence", [])) or "-"
        blockers = "<br>".join(str(x) for x in item.get("blockers", [])) or "-"
        lines.append(f"| {item['name']} | {item['status']} | {evidence} | {blockers} |")
    lines.extend(["", "Forbidden claims:", ""])
    for claim in report.get("forbidden_claims", []):
        lines.append(f"- {claim}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summary", type=pathlib.Path, help="matrix-summary.json to audit")
    parser.add_argument("--markdown", action="store_true", help="print Markdown instead of JSON")
    parser.add_argument("--out", type=pathlib.Path, help="optional output path")
    args = parser.parse_args()

    matrix = json.loads(args.summary.read_text(encoding="utf-8"))
    report = audit_matrix(matrix)
    if args.markdown:
        text = markdown_report(args.summary, report)
    else:
        text = json.dumps(report, indent=2, sort_keys=True) + "\n"

    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

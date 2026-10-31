#!/usr/bin/env python3
"""Summarize FlowCausal real-agent evidence readiness.

The lower-level tools answer narrow questions:

* ``build_real_agent_manifest.py``: did artifact intake find raw/repo/endpoint
  triples?
* ``validate_bundle_collection_manifest.py``: is the manifest internally safe?
* ``audit_agent_trace_bundle.py``: is one instance L5-ready?
* ``audit_bundle_collection.py``: do enough instances pass all bundle gates?

This helper turns those reports into a single paper-readiness dashboard with the
six evidence chains that matter for the FlowCausal EuroSys story.  It is a
roadmap generator, not a claim upgrader: missing inputs stay missing, replay-only
evidence stays partial, and a ``pass`` requires an already passing collection
audit with enough L5-ready bundles.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from audit_bundle_collection import (  # noqa: E402
    GATE_NAMES,
    discover_audit_paths,
    read_json as read_audit_json,
    summarize_reports,
)


SCHEMA = "flowcausal.real_agent_evidence_dashboard.v1"
STATUS_ORDER = {"pass": 3, "partial": 2, "fail": 1, "missing": 0}
CHAIN_ACTIONS = {
    "frontier correctness": "add manual/oracle frontier annotations and ensure matrix stage_records preserve command identity",
    "endpoint improvement": "run or import full SWE-bench/SWE-agent/OpenHands harness endpoints and compare resolved/time against Linux default",
    "scheduler mechanism": "collect controller, flowmon/BPF, schedstat, and frontier wait evidence for the same trace",
    "safety/fairness": "run victim/sidecar or co-tenant workloads and keep slowdown within the documented envelope",
    "baseline comparison": "include Linux default, nice/priority, static affinity, static cgroup, and oracle frontier baselines",
    "reproducibility/artifact": "archive raw outputs, normalized traces, endpoints, matrix summaries, checksums, scripts, and manifests",
}


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path_text: str) -> dict[str, Any]:
    path = pathlib.Path(path_text).expanduser().resolve()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"JSON report is not an object: {path}")
    data.setdefault("_path", str(path))
    return data


def norm_status(value: Any) -> str:
    text = str(value or "").lower()
    return text if text in STATUS_ORDER else "missing"


def stage(name: str, status: str, evidence: list[str] | None = None, blockers: list[str] | None = None, next_actions: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": norm_status(status),
        "evidence": evidence or [],
        "blockers": blockers or [],
        "next_actions": next_actions or [],
    }


def intake_stage(report: dict[str, Any] | None, *, min_instances: int) -> dict[str, Any]:
    if not report:
        return stage(
            "artifact intake",
            "missing",
            blockers=["no intake report provided"],
            next_actions=["run build_real_agent_manifest.py on raw agent outputs, per-instance repos, and full-harness endpoints"],
        )
    summary = as_dict(report.get("summary"))
    ready = int(summary.get("ready_instances") or 0)
    blockers: list[str] = []
    next_actions: list[str] = []
    status = "pass" if ready >= min_instances else "partial" if ready > 0 else "fail"
    if ready < min_instances:
        blockers.append(f"ready real-agent artifact triples {ready} < required {min_instances}")
        next_actions.append("collect more raw trajectory + repo + full-harness endpoint triples")
    for item in as_list(report.get("instances")):
        if not isinstance(item, dict):
            continue
        for blocker in as_list(item.get("blockers")):
            blockers.append(f"{item.get('instance_id', '<unknown>')}: {blocker}")
            if len(blockers) >= 12:
                break
        if len(blockers) >= 12:
            break
    return stage(
        "artifact intake",
        status,
        evidence=[
            f"ready_instances={ready}",
            f"raw_instances={summary.get('raw_instances', 0)}",
            f"endpoint_instances={summary.get('endpoint_instances', 0)}",
            f"repo_instances={summary.get('repo_instances', 0)}",
        ],
        blockers=blockers,
        next_actions=next_actions,
    )


def readiness_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions = [str(item) for item in as_list(report.get("next_actions"))[:8]]
    evidence = [
        f"run_readiness={report.get('run_readiness_status', '')}",
        f"evidence_capture={report.get('evidence_capture_status', '')}",
    ]
    for item in as_list(report.get("stages")):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        status = str(item.get("status") or "")
        if name and status:
            evidence.append(f"{name}={status}")
    return stage(
        "harness readiness",
        norm_status(report.get("status")),
        evidence=evidence,
        blockers=blockers,
        next_actions=next_actions,
    )


def capture_plan_preflight_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    summary = as_dict(report.get("summary"))
    script_check = as_dict(report.get("script_check"))
    evidence = [
        f"selected_instances={summary.get('selected_instances', 0)}",
        f"row_pass={summary.get('row_pass', 0)}",
        f"row_fail={summary.get('row_fail', 0)}",
        f"mode={summary.get('mode', '')}",
    ]
    if script_check:
        evidence.append(f"script_check={script_check.get('status', '')}")
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions = []
    if norm_status(report.get("status")) != "pass":
        next_actions.append("fix generated capture plan/script before spending API budget")
    return stage(
        "capture plan preflight",
        norm_status(report.get("status")),
        evidence=evidence,
        blockers=blockers,
        next_actions=next_actions,
    )


def harness_matrix_plan_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    summary = as_dict(report.get("summary"))
    variants = as_list(summary.get("variants"))
    evidence = [
        f"selected_instances={summary.get('selected_instances', 0)}",
        f"row_pass={summary.get('row_pass', 0)}",
        f"row_fail={summary.get('row_fail', 0)}",
        f"trials={summary.get('trials', 0)}",
        "variants=" + ",".join(str(item) for item in variants),
        f"execute={summary.get('execute', False)}",
    ]
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions = []
    if norm_status(report.get("status")) == "pass":
        next_actions.append("execute the generated full-harness matrix scripts after setting an API key")
    else:
        next_actions.append("fix generated full-harness matrix plan before endpoint-matrix execution")
    return stage(
        "full-harness matrix plan",
        norm_status(report.get("status")),
        evidence=evidence,
        blockers=blockers,
        next_actions=next_actions,
    )


def harness_matrix_runbook_preflight_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    summary = as_dict(report.get("summary"))
    variants = as_list(summary.get("required_variants"))
    evidence = [
        f"rows={summary.get('rows', 0)}",
        f"row_pass={summary.get('row_pass', 0)}",
        f"row_fail={summary.get('row_fail', 0)}",
        f"min_trials={summary.get('min_trials', 0)}",
        "required_variants=" + ",".join(str(item) for item in variants),
    ]
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions = []
    if norm_status(report.get("status")) == "pass":
        next_actions.append("execute the guarded per-instance full-harness matrix run scripts after setting an API key")
    else:
        next_actions.append("fix generated per-instance full-harness matrix runbooks before endpoint-matrix execution")
    return stage(
        "full-harness matrix runbook preflight",
        norm_status(report.get("status")),
        evidence=evidence,
        blockers=blockers,
        next_actions=next_actions,
    )


def bootstrap_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    mode = str(report.get("mode") or "")
    raw_status = norm_status(report.get("status"))
    status = "pass" if raw_status == "pass" and mode == "executed" else "partial" if raw_status == "pass" else raw_status
    artifacts = as_dict(report.get("artifacts"))
    evidence = [
        f"mode={mode}",
        "backends=" + ",".join(str(item) for item in as_list(report.get("backends"))),
    ]
    for key in ("install_script", "env_script", "post_install_readiness_script"):
        if artifacts.get(key):
            evidence.append(f"{key}={artifacts.get(key)}")
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions = [str(item) for item in as_list(report.get("next_actions"))[:8]]
    if mode != "executed" and raw_status == "pass":
        blockers.append("bootstrap bundle generated but installer has not been executed")
    return stage(
        "harness bootstrap",
        status,
        evidence=evidence,
        blockers=blockers,
        next_actions=next_actions,
    )


def repo_prep_stage(report: dict[str, Any] | None, *, min_instances: int) -> dict[str, Any] | None:
    if not report:
        return None
    summary = as_dict(report.get("summary"))
    rows = int(summary.get("repo_rows") or 0)
    missing_commit = int(summary.get("missing_base_commit") or 0)
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    warnings = [str(item) for item in as_list(report.get("warnings"))[:8]]
    next_actions: list[str] = []
    status = norm_status(report.get("status"))
    if rows < min_instances:
        blockers.append(f"repo rows {rows} < required {min_instances}")
        next_actions.append("prepare repositories for every selected real-agent instance")
    if missing_commit:
        blockers.append(f"{missing_commit} repo row(s) lack base_commit pinning")
        next_actions.append("prefer task records with base_commit or manually record immutable checkout SHAs")
        status = "partial" if status == "pass" else status
    if blockers and status == "pass":
        status = "partial"
    return stage(
        "repo preparation",
        status,
        evidence=[
            f"repo_rows={rows}",
            f"missing_base_commit={missing_commit}",
            *warnings,
        ],
        blockers=blockers,
        next_actions=next_actions,
    )


def preflight_stage(report: dict[str, Any] | None) -> dict[str, Any]:
    if not report:
        return stage(
            "manifest preflight",
            "missing",
            blockers=["no manifest preflight report provided"],
            next_actions=["run validate_bundle_collection_manifest.py before batch preparation"],
        )
    status = norm_status(report.get("status"))
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    warnings = [str(item) for item in as_list(report.get("warnings"))[:8]]
    next_actions = ["fix manifest blockers before running expensive matrices"] if status == "fail" else []
    if status == "partial":
        next_actions.append("add frontier annotations and matrix summaries before final archival preflight")
    return stage(
        "manifest preflight",
        status,
        evidence=[f"instances={as_dict(report.get('summary')).get('instances', 0)}", *warnings],
        blockers=blockers,
        next_actions=next_actions,
    )


def harness_matrix_stage(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    summary = as_dict(report.get("summary"))
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions: list[str] = []
    for row in as_list(report.get("gates")):
        row_dict = as_dict(row)
        if row_dict.get("status") == "pass":
            continue
        next_actions.extend(str(item) for item in as_list(row_dict.get("next_actions"))[:2])
    for row in as_list(report.get("chains")):
        row_dict = as_dict(row)
        if row_dict.get("status") == "pass":
            continue
        next_actions.extend(str(item) for item in as_list(row_dict.get("next_actions"))[:1])
    return stage(
        "full-harness endpoint matrix",
        norm_status(report.get("status")),
        evidence=[
            f"scope={report.get('highest_supported_scope', '')}",
            f"baseline={summary.get('baseline_variant', '')}",
            f"flowcausal={summary.get('flowcausal_variant', '')}",
            f"min_trials={summary.get('min_trials', '')}",
        ],
        blockers=blockers,
        next_actions=list(dict.fromkeys(action for action in next_actions if action))[:8],
    )


def safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def pct_delta(new_value: float | None, old_value: float | None) -> float | None:
    if new_value is None or old_value is None or old_value == 0:
        return None
    return (new_value - old_value) * 100.0 / old_value


def fmt_number(value: float | None, *, digits: int = 3, suffix: str = "") -> str:
    if value is None:
        return ""
    return f"{value:.{digits}f}{suffix}"


def latex_escape(value: Any) -> str:
    text = str(value)
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(ch, ch) for ch in text)


def gate_statuses(matrix: dict[str, Any]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for gate in as_list(as_dict(matrix.get("claim_gate_audit")).get("gates")):
        gate_dict = as_dict(gate)
        name = str(gate_dict.get("name") or "")
        if name:
            statuses[name] = norm_status(gate_dict.get("status"))
    return statuses


def variant_by_name(matrix: dict[str, Any], name: str) -> dict[str, Any] | None:
    for row in as_list(matrix.get("variants")):
        row_dict = as_dict(row)
        if row_dict.get("name") == name:
            return row_dict
    return None


def variant_metric(variant: dict[str, Any] | None, key: str) -> float | None:
    if not variant:
        return None
    return safe_float(as_dict(variant.get("aggregate")).get(key))


def workload_elapsed_from_log(path: pathlib.Path) -> float | None:
    begin_ns: float | None = None
    end_ns: float | None = None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        rtype = str(record.get("type") or "")
        if rtype == "workload_begin":
            begin_ns = safe_float(record.get("time_ns"))
        elif rtype == "workload_end":
            end_ns = safe_float(record.get("time_ns"))
    if begin_ns is None or end_ns is None or end_ns < begin_ns:
        return None
    return (end_ns - begin_ns) / 1_000_000_000.0


def variant_workload_elapsed_mean(variant: dict[str, Any] | None) -> float | None:
    if not variant:
        return None
    aggregate_value = variant_metric(variant, "workload_elapsed_mean_sec")
    if aggregate_value is not None and aggregate_value > 0.0:
        return aggregate_value
    values: list[float] = []
    for trial in as_list(variant.get("trials")):
        trial_dict = as_dict(trial)
        direct = safe_float(as_dict(trial_dict.get("workload")).get("workload_elapsed_sec"))
        if direct is not None and direct > 0.0:
            values.append(direct)
            continue
        trial_path = str(trial_dict.get("path") or "").strip()
        if not trial_path:
            continue
        parsed = workload_elapsed_from_log(pathlib.Path(trial_path) / "workload.log")
        if parsed is not None and parsed > 0.0:
            values.append(parsed)
    if values:
        return sum(values) / len(values)
    return variant_metric(variant, "total_mean_sec")


def variant_trials(variant: dict[str, Any] | None) -> int:
    if not variant:
        return 0
    explicit = safe_float(variant.get("trials"))
    if explicit is not None:
        return int(explicit)
    n = safe_float(as_dict(as_dict(variant.get("aggregate")).get("critical_summary")).get("n"))
    return int(n or 0)


def display_label(label: str) -> str:
    lower = label.lower()
    if lower.startswith("django14608"):
        if "minblock5ms" in lower or "min-block5ms" in lower:
            return "Django 14608 (wakeup-minblock5ms)"
        if "wakeup-only" in lower:
            return "Django 14608 (wakeup-only)"
        if "safety-backoff" in lower:
            return "Django 14608 (safety-backoff)"
        return "Django 14608 (base)"
    if lower.startswith("sympy21171"):
        return "SymPy 21171"
    if lower.startswith("matplotlib23314"):
        return "Matplotlib 23314"
    return label.split("-real-repo")[0] if "-real-repo" in label else label


def best_non_flow_variant(matrix: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[tuple[float, dict[str, Any]]] = []
    for row in as_list(matrix.get("variants")):
        row_dict = as_dict(row)
        name = str(row_dict.get("name") or "")
        if not name or name.startswith("flowcausal"):
            continue
        critical = variant_metric(row_dict, "critical_mean_sec")
        if critical is None or critical <= 0:
            continue
        candidates.append((critical, row_dict))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def repo_stage_matrix_record(path: pathlib.Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(matrix, dict):
        raise SystemExit(f"repo-stage matrix is not a JSON object: {path}")
    config = as_dict(matrix.get("config"))
    gates = gate_statuses(matrix)
    default = variant_by_name(matrix, "default_taskset")
    flow = variant_by_name(matrix, "flowcausal_width")
    best_base = best_non_flow_variant(matrix)
    default_critical = variant_metric(default, "critical_mean_sec")
    flow_critical = variant_metric(flow, "critical_mean_sec")
    default_harness_total = variant_metric(default, "total_mean_sec")
    flow_harness_total = variant_metric(flow, "total_mean_sec")
    default_total = variant_workload_elapsed_mean(default)
    flow_total = variant_workload_elapsed_mean(flow)
    best_base_critical = variant_metric(best_base, "critical_mean_sec")
    best_base_name = str(best_base.get("name")) if best_base else ""
    flow_victim = as_dict(as_dict(flow.get("aggregate") if flow else {}).get("victim_mean"))
    default_victim = as_dict(as_dict(default.get("aggregate") if default else {}).get("victim_mean"))
    direct = gates.get("direct CPU mechanism", "missing")
    outcome = gates.get("stage/episode outcome", "missing")
    safety = gates.get("victim/fairness safety", "missing")
    usable = bool(default and flow and variant_trials(default) > 0 and variant_trials(flow) > 0 and default_critical and flow_critical)
    if not usable:
        claim_status = "invalid"
        claim = "ignore for claims: missing successful default/FlowCausal critical trials"
    elif direct == "pass" and outcome == "pass" and safety == "pass":
        claim_status = "candidate_success"
        claim = "repo-stage candidate success; still not full real-agent endpoint evidence"
    elif outcome == "pass" and (direct != "pass" or safety != "pass"):
        claim_status = "partial_or_unsafe"
        claim = "repo-stage partial/boundary evidence: outcome improved but direct mechanism or safety gate did not pass"
    elif flow_critical < default_critical:
        claim_status = "weak_partial"
        claim = "repo-stage weak partial evidence: critical time improved vs default, but claim gates do not support a speedup claim"
    else:
        claim_status = "negative"
        claim = "negative repo-stage evidence for current policy"
    label = str(config.get("label") or path.parent.name)
    return {
        "path": str(path.resolve()),
        "label": label,
        "display_label": display_label(label),
        "created_at": str(matrix.get("created_at") or ""),
        "highest_supported_level": str(as_dict(matrix.get("claim_gate_audit")).get("highest_supported_level") or ""),
        "claim_status": claim_status,
        "claim": claim,
        "trials": {
            "default_taskset": variant_trials(default),
            "flowcausal_width": variant_trials(flow),
        },
        "metrics": {
            "default_critical_mean_sec": default_critical,
            "default_total_mean_sec": default_total,
            "default_harness_total_mean_sec": default_harness_total,
            "flowcausal_critical_mean_sec": flow_critical,
            "flowcausal_total_mean_sec": flow_total,
            "flowcausal_harness_total_mean_sec": flow_harness_total,
            "flow_vs_default_critical_delta_pct": pct_delta(flow_critical, default_critical),
            "flow_vs_default_total_delta_pct": pct_delta(flow_total, default_total),
            "flow_vs_default_harness_total_delta_pct": pct_delta(flow_harness_total, default_harness_total),
            "best_non_flow_variant": best_base_name,
            "best_non_flow_critical_mean_sec": best_base_critical,
            "flow_vs_best_non_flow_critical_delta_pct": pct_delta(flow_critical, best_base_critical),
            "default_victim_lateness_p99_ms": safe_float(default_victim.get("lateness_p99_ms")),
            "flowcausal_victim_lateness_p99_ms": safe_float(flow_victim.get("lateness_p99_ms")),
            "default_victim_lateness_max_ms": safe_float(default_victim.get("lateness_max_ms")),
            "flowcausal_victim_lateness_max_ms": safe_float(flow_victim.get("lateness_max_ms")),
        },
        "gates": gates,
    }


def discover_repo_stage_matrices(paths: list[str], *, recursive: bool) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for text in paths:
        path = pathlib.Path(text).expanduser().resolve()
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            pattern = "**/matrix-summary.json" if recursive else "matrix-summary.json"
            found.extend(sorted(path.glob(pattern)))
        else:
            raise SystemExit(f"repo-stage input not found: {path}")
    # Preserve order while deduplicating.
    return list(dict.fromkeys(found))


def repo_stage_evidence(args: argparse.Namespace) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in discover_repo_stage_matrices(args.repo_stage_input or [], recursive=bool(args.repo_stage_recursive)):
        records.append(repo_stage_matrix_record(path))
    return records


def variant_aggregate_dict(variant: dict[str, Any] | None) -> dict[str, Any]:
    return as_dict(variant.get("aggregate")) if variant else {}


def aggregate_nested_float(variant: dict[str, Any] | None, group: str, key: str) -> float | None:
    return safe_float(as_dict(variant_aggregate_dict(variant).get(group)).get(key))


def oracle_driver_returncodes(variant: dict[str, Any] | None) -> list[int]:
    values: list[int] = []
    if not variant:
        return values
    for trial in as_list(variant.get("trials")):
        value = as_dict(as_dict(trial).get("manifest")).get("oracle_frontier_driver_returncode")
        number = safe_float(value)
        if number is not None:
            values.append(int(number))
    return values


def mechanism_smoke_record(path: pathlib.Path) -> dict[str, Any]:
    matrix = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(matrix, dict):
        raise SystemExit(f"mechanism smoke matrix is not a JSON object: {path}")
    config = as_dict(matrix.get("config"))
    label = str(config.get("label") or path.parent.name)
    gates = gate_statuses(matrix)
    default = variant_by_name(matrix, "default_taskset")
    oracle = variant_by_name(matrix, "oracle_frontier")
    flow = variant_by_name(matrix, "flowcausal_width")
    oracle_rcs = oracle_driver_returncodes(oracle)
    oracle_frontier = aggregate_nested_float(oracle, "flowmon_mean", "frontier_enqueues")
    flow_frontier = aggregate_nested_float(flow, "flowmon_mean", "frontier_enqueues")
    oracle_ok = bool(oracle and variant_trials(oracle) > 0 and oracle_rcs and all(rc == 0 for rc in oracle_rcs))
    oracle_hits = bool(oracle_frontier is not None and oracle_frontier > 0)
    flow_hits = bool(flow_frontier is not None and flow_frontier > 0)
    if oracle_ok and oracle_hits and flow_hits:
        status = "connected"
        claim = "mechanism smoke: oracle labels reach BPF and FlowCausal online hints also reach BPF"
    elif oracle and (oracle_hits or oracle_ok):
        status = "weak_partial"
        claim = "mechanism smoke is partially connected, but oracle clean-exit or BPF hit evidence is weak"
    else:
        status = "invalid"
        claim = "ignore for mechanism claims: oracle baseline did not produce usable clean BPF evidence"
    return {
        "path": str(path.resolve()),
        "label": label,
        "display_label": display_label(label),
        "created_at": str(matrix.get("created_at") or ""),
        "highest_supported_level": str(as_dict(matrix.get("claim_gate_audit")).get("highest_supported_level") or ""),
        "status": status,
        "claim": claim,
        "trials": {
            "default_taskset": variant_trials(default),
            "oracle_frontier": variant_trials(oracle),
            "flowcausal_width": variant_trials(flow),
        },
        "metrics": {
            "default_critical_mean_sec": variant_metric(default, "critical_mean_sec"),
            "oracle_critical_mean_sec": variant_metric(oracle, "critical_mean_sec"),
            "flowcausal_critical_mean_sec": variant_metric(flow, "critical_mean_sec"),
            "default_workload_elapsed_mean_sec": variant_workload_elapsed_mean(default),
            "oracle_workload_elapsed_mean_sec": variant_workload_elapsed_mean(oracle),
            "flowcausal_workload_elapsed_mean_sec": variant_workload_elapsed_mean(flow),
            "default_victim_lateness_p99_ms": aggregate_nested_float(default, "victim_mean", "lateness_p99_ms"),
            "oracle_victim_lateness_p99_ms": aggregate_nested_float(oracle, "victim_mean", "lateness_p99_ms"),
            "flowcausal_victim_lateness_p99_ms": aggregate_nested_float(flow, "victim_mean", "lateness_p99_ms"),
            "oracle_frontier_enqueues": oracle_frontier,
            "oracle_fallback_enqueues": aggregate_nested_float(oracle, "flowmon_mean", "fallback_enqueues"),
            "flowcausal_frontier_enqueues": flow_frontier,
            "flowcausal_fallback_enqueues": aggregate_nested_float(flow, "flowmon_mean", "fallback_enqueues"),
            "oracle_label_count_mean": variant_metric(oracle, "workload_frontier_label_count_mean"),
            "flowcausal_label_count_mean": variant_metric(flow, "workload_frontier_label_count_mean"),
        },
        "oracle_driver_returncodes": oracle_rcs,
        "gates": gates,
    }


def mechanism_smoke_evidence(args: argparse.Namespace) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in discover_repo_stage_matrices(
        args.mechanism_smoke_input or [],
        recursive=bool(args.mechanism_smoke_recursive),
    ):
        records.append(mechanism_smoke_record(path))
    return records


def mechanism_smoke_summary_stage(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    connected = sum(1 for row in records if row.get("status") == "connected")
    weak = sum(1 for row in records if row.get("status") == "weak_partial")
    invalid = sum(1 for row in records if row.get("status") == "invalid")
    status = "partial" if connected or weak else "fail"
    return stage(
        "mechanism smoke evidence",
        status,
        evidence=[
            f"connected_smokes={connected}",
            f"weak_partial={weak}",
            f"invalid={invalid}",
        ],
        blockers=["mechanism smoke is not full SWE-agent/OpenHands endpoint evidence"],
        next_actions=[
            "promote connected mechanism smokes into 3-trial real-agent matrices with static/oracle baselines"
        ],
    )


def repo_stage_summary_stage(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not records:
        return None
    counts: dict[str, int] = {}
    for row in records:
        key = str(row.get("claim_status") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    usable = sum(count for key, count in counts.items() if key != "invalid")
    candidate = counts.get("candidate_success", 0)
    partial = counts.get("partial_or_unsafe", 0) + counts.get("weak_partial", 0)
    negative = counts.get("negative", 0)
    invalid = counts.get("invalid", 0)
    status = "pass" if candidate >= 3 else "partial" if candidate or partial or negative else "fail"
    next_actions = []
    if candidate < 3:
        next_actions.append("turn repo-stage boundary evidence into passing direct-mechanism, safety, and endpoint evidence on at least three real-agent instances")
    if invalid:
        next_actions.append("exclude invalid/failed matrix runs from paper claims or rerun them")
    return stage(
        "repo-stage matrix evidence",
        status,
        evidence=[
            f"usable_matrices={usable}",
            f"candidate_success={candidate}",
            f"partial_or_unsafe={partial}",
            f"negative={negative}",
            f"invalid={invalid}",
        ],
        blockers=[] if candidate >= 3 else ["repo-stage evidence is not full SWE-agent/OpenHands endpoint evidence"],
        next_actions=next_actions,
    )


def collection_from_inputs(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.collection_audit:
        return read_json(args.collection_audit)
    if args.bundle_input:
        paths = discover_audit_paths(args.bundle_input, recursive=bool(args.recursive))
        reports = [(path, read_audit_json(path)) for path in paths]
        return summarize_reports(reports, min_ready_bundles=args.min_ready_bundles)
    return None


def collection_stage(report: dict[str, Any] | None, *, min_instances: int) -> dict[str, Any]:
    if not report:
        return stage(
            "bundle collection audit",
            "missing",
            blockers=["no collection audit or bundle audit inputs provided"],
            next_actions=["run prepare_bundle_collection.py and audit_bundle_collection.py after matrix execution"],
        )
    status = norm_status(report.get("status"))
    summary = as_dict(report.get("summary"))
    blockers = [str(item) for item in as_list(report.get("blockers"))[:20]]
    next_actions: list[str] = []
    if int(summary.get("l5_ready_count") or 0) < min_instances:
        next_actions.append("raise enough per-instance bundle audits to L5-ready")
    return stage(
        "bundle collection audit",
        status,
        evidence=[
            f"bundle_count={summary.get('bundle_count', 0)}",
            f"l5_ready_count={summary.get('l5_ready_count', 0)}",
            f"required={min_instances}",
        ],
        blockers=blockers,
        next_actions=next_actions,
    )


def harness_chain_statuses(report: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return six-chain statuses contributed by a full-harness matrix audit.

    A harness matrix audit is real endpoint/safety evidence, but usually only
    for one benchmark instance.  The dashboard therefore treats it as
    supporting evidence for the chain rows, not as a replacement for the final
    multi-instance bundle collection audit.
    """

    if not report:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in as_list(report.get("chains")):
        row_dict = as_dict(row)
        name = str(row_dict.get("name") or "")
        if name in GATE_NAMES:
            out[name] = row_dict
    return out


def merge_chain_status(collection_status: str, harness_status: str) -> str:
    if collection_status == "pass":
        return "pass"
    if harness_status in {"pass", "partial"}:
        return "partial"
    if collection_status in {"partial", "fail"}:
        return collection_status
    if harness_status == "fail":
        return "fail"
    return collection_status


def chain_rows(collection: dict[str, Any] | None, *, min_instances: int, harness_matrix: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    gate_counts = as_dict(collection.get("gate_counts")) if collection else {}
    harness_chains = harness_chain_statuses(harness_matrix)
    for name in GATE_NAMES:
        counts = as_dict(gate_counts.get(name))
        passed = int(counts.get("pass") or 0)
        partial = int(counts.get("partial") or 0)
        failed = int(counts.get("fail") or 0)
        missing = int(counts.get("missing") or 0)
        if passed >= min_instances:
            status = "pass"
        elif passed > 0 or partial > 0:
            status = "partial"
        elif failed > 0 or missing > 0:
            status = "fail"
        else:
            status = "missing"
        collection_status = status
        harness_row = as_dict(harness_chains.get(name))
        harness_status = norm_status(harness_row.get("status")) if harness_row else "missing"
        status = merge_chain_status(collection_status, harness_status)
        blockers: list[str] = []
        if passed < min_instances:
            blockers.append(f"only {passed} pass bundle(s), need {min_instances}")
        top_blockers = []
        if collection:
            for item in as_list(collection.get("top_blockers")):
                blocker = str(as_dict(item).get("blocker") or "")
                if blocker and name.lower().split()[0] in blocker.lower():
                    top_blockers.append(f"{as_dict(item).get('count', 0)}× {blocker}")
        if harness_row:
            for blocker in as_list(harness_row.get("blockers"))[:4]:
                blockers.append(f"harness matrix: {blocker}")
            if harness_status == "pass" and collection_status != "pass":
                blockers.append("harness matrix evidence exists, but final L5 chain still needs enough audited bundles")
        next_actions: list[str] = []
        if status != "pass":
            next_actions.append(CHAIN_ACTIONS.get(name, "collect missing evidence"))
            if harness_row:
                next_actions.extend(str(item) for item in as_list(harness_row.get("next_actions"))[:3])
        rows.append(
            {
                "name": name,
                "status": status,
                "counts": {
                    "pass": passed,
                    "partial": partial,
                    "fail": failed,
                    "missing": missing,
                    "harness_matrix": harness_status,
                },
                "evidence": [f"harness_matrix={harness_status}"] if harness_row else [],
                "blockers": [*blockers, *top_blockers[:4]],
                "next_actions": list(dict.fromkeys(action for action in next_actions if action)),
            }
        )
    return rows


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    readiness = read_json(args.readiness_report) if args.readiness_report else None
    capture_plan_preflight = read_json(args.capture_plan_preflight) if args.capture_plan_preflight else None
    harness_matrix_plan = read_json(args.harness_matrix_plan) if args.harness_matrix_plan else None
    harness_matrix_runbook_preflight = read_json(args.harness_matrix_runbook_preflight) if args.harness_matrix_runbook_preflight else None
    bootstrap = read_json(args.bootstrap_report) if args.bootstrap_report else None
    repo_prep = read_json(args.repo_prep_report) if args.repo_prep_report else None
    harness_matrix = read_json(args.harness_matrix_audit) if args.harness_matrix_audit else None
    intake = read_json(args.intake_report) if args.intake_report else None
    preflight = read_json(args.preflight_report) if args.preflight_report else None
    collection = collection_from_inputs(args)
    repo_stage_records = repo_stage_evidence(args)
    mechanism_smoke_records = mechanism_smoke_evidence(args)
    stages = [
        item for item in [
            readiness_stage(readiness),
            capture_plan_preflight_stage(capture_plan_preflight),
            harness_matrix_plan_stage(harness_matrix_plan),
            harness_matrix_runbook_preflight_stage(harness_matrix_runbook_preflight),
            bootstrap_stage(bootstrap),
            repo_prep_stage(repo_prep, min_instances=args.min_ready_bundles),
            repo_stage_summary_stage(repo_stage_records),
            mechanism_smoke_summary_stage(mechanism_smoke_records),
            harness_matrix_stage(harness_matrix),
            intake_stage(intake, min_instances=args.min_ready_bundles),
            preflight_stage(preflight),
            collection_stage(collection, min_instances=args.min_ready_bundles),
        ]
        if item is not None
    ]
    chains = chain_rows(collection, min_instances=args.min_ready_bundles, harness_matrix=harness_matrix)
    all_statuses = [norm_status(item.get("status")) for item in [*stages, *chains]]
    if all(status == "pass" for status in all_statuses):
        status = "pass"
    elif any(status in {"pass", "partial"} for status in all_statuses):
        status = "partial"
    else:
        status = "fail"
    blockers = []
    for item in stages:
        blockers.extend(str(blocker) for blocker in as_list(item.get("blockers"))[:8])
    for row in chains:
        if row.get("status") != "pass":
            blockers.extend(str(blocker) for blocker in as_list(row.get("blockers"))[:2])
    next_actions = []
    for item in [*stages, *chains]:
        next_actions.extend(str(action) for action in as_list(item.get("next_actions")))
    # Preserve order while deduplicating.
    next_actions = list(dict.fromkeys(action for action in next_actions if action))
    blockers = list(dict.fromkeys(blocker for blocker in blockers if blocker))
    return {
        "schema": SCHEMA,
        "status": status,
        "highest_supported_scope": "EuroSys paper-ready real-agent evidence" if status == "pass" else "not paper-ready",
        "requirements": {
            "min_ready_bundles": args.min_ready_bundles,
            "evidence_chains": list(GATE_NAMES),
        },
        "inputs": {
            "readiness_report": str(pathlib.Path(args.readiness_report).resolve()) if args.readiness_report else "",
            "capture_plan_preflight": str(pathlib.Path(args.capture_plan_preflight).resolve()) if args.capture_plan_preflight else "",
            "harness_matrix_plan": str(pathlib.Path(args.harness_matrix_plan).resolve()) if args.harness_matrix_plan else "",
            "harness_matrix_runbook_preflight": str(pathlib.Path(args.harness_matrix_runbook_preflight).resolve()) if args.harness_matrix_runbook_preflight else "",
            "bootstrap_report": str(pathlib.Path(args.bootstrap_report).resolve()) if args.bootstrap_report else "",
            "repo_prep_report": str(pathlib.Path(args.repo_prep_report).resolve()) if args.repo_prep_report else "",
            "harness_matrix_audit": str(pathlib.Path(args.harness_matrix_audit).resolve()) if args.harness_matrix_audit else "",
            "repo_stage_input": args.repo_stage_input or [],
            "mechanism_smoke_input": args.mechanism_smoke_input or [],
            "intake_report": str(pathlib.Path(args.intake_report).resolve()) if args.intake_report else "",
            "preflight_report": str(pathlib.Path(args.preflight_report).resolve()) if args.preflight_report else "",
            "collection_audit": str(pathlib.Path(args.collection_audit).resolve()) if args.collection_audit else "",
            "bundle_input": args.bundle_input or [],
        },
        "stages": stages,
        "chains": chains,
        "repo_stage_evidence": repo_stage_records,
        "mechanism_smoke_evidence": mechanism_smoke_records,
        "blockers": blockers[:40],
        "next_actions": next_actions[:20],
        "notes": [
            "dashboard pass requires the lower-level collection audit to pass; it does not create new evidence",
            "replay-only or heuristic frontier evidence must remain partial until tied to full harness endpoints and manual/oracle labels",
            "repo-stage matrices are useful mechanism/boundary evidence, but cannot replace real code-agent endpoint evidence",
            "mechanism smokes can prove oracle/controller/BPF plumbing only; they cannot prove endpoint improvement",
            "full-harness matrix audits can contribute endpoint/safety chain evidence, but cannot replace the final multi-instance bundle collection audit",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal real-agent evidence dashboard",
        "",
        f"Status: `{report.get('status')}`",
        f"Scope: {report.get('highest_supported_scope')}",
        "",
        "## Pipeline stages",
        "",
        "| Stage | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for item in as_list(report.get("stages")):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {item.get('name', '')} | `{item.get('status', '')}` | {'; '.join(str(x) for x in as_list(item.get('evidence')))} |"
        )
    lines.extend(["", "## Six evidence chains", "", "| Chain | Status | Pass/Partial/Fail/Missing | Next action |", "| --- | --- | --- | --- |"])
    for row in as_list(report.get("chains")):
        if not isinstance(row, dict):
            continue
        counts = as_dict(row.get("counts"))
        count_text = f"{counts.get('pass', 0)}/{counts.get('partial', 0)}/{counts.get('fail', 0)}/{counts.get('missing', 0)}"
        action = "; ".join(str(x) for x in as_list(row.get("next_actions")))
        lines.append(f"| {row.get('name', '')} | `{row.get('status', '')}` | {count_text} | {action} |")
    if report.get("repo_stage_evidence"):
        lines.extend([
            "",
            "## Repo-stage matrix evidence",
            "",
            "| Label | Claim status | Critical Δ vs default | Critical Δ vs best non-Flow | Workload elapsed Δ vs default | Victim p99 default→FlowCausal | Gates | Claim boundary |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for row in as_list(report.get("repo_stage_evidence")):
            if not isinstance(row, dict):
                continue
            metrics = as_dict(row.get("metrics"))
            gates = as_dict(row.get("gates"))
            gate_text = "; ".join(
                f"{short}={gates.get(full, 'missing')}"
                for short, full in [
                    ("direct", "direct CPU mechanism"),
                    ("outcome", "stage/episode outcome"),
                    ("safety", "victim/fairness safety"),
                ]
            )
            victim_text = (
                f"{fmt_number(safe_float(metrics.get('default_victim_lateness_p99_ms')))}"
                f"→{fmt_number(safe_float(metrics.get('flowcausal_victim_lateness_p99_ms')))} ms"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row.get("display_label") or row.get("label", "")),
                        f"`{row.get('claim_status', '')}`",
                        fmt_number(safe_float(metrics.get("flow_vs_default_critical_delta_pct")), digits=1, suffix="%"),
                        (
                            f"{fmt_number(safe_float(metrics.get('flow_vs_best_non_flow_critical_delta_pct')), digits=1, suffix='%')}"
                            f" vs {metrics.get('best_non_flow_variant', '')}"
                        ),
                        fmt_number(safe_float(metrics.get("flow_vs_default_total_delta_pct")), digits=1, suffix="%"),
                        victim_text,
                        gate_text,
                        str(row.get("claim", "")),
                    ]
            )
                + " |"
            )
    if report.get("mechanism_smoke_evidence"):
        lines.extend([
            "",
            "## Mechanism smoke evidence",
            "",
            "| Label | Status | Trials default/oracle/Flow | Oracle rc | Oracle BPF frontier/fallback | FlowCausal BPF frontier/fallback | Workload elapsed default/oracle/Flow | Claim boundary |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ])
        for row in as_list(report.get("mechanism_smoke_evidence")):
            if not isinstance(row, dict):
                continue
            metrics = as_dict(row.get("metrics"))
            trials = as_dict(row.get("trials"))
            elapsed = (
                f"{fmt_number(safe_float(metrics.get('default_workload_elapsed_mean_sec')))}"
                f"/{fmt_number(safe_float(metrics.get('oracle_workload_elapsed_mean_sec')))}"
                f"/{fmt_number(safe_float(metrics.get('flowcausal_workload_elapsed_mean_sec')))} s"
            )
            oracle_bpf = (
                f"{fmt_number(safe_float(metrics.get('oracle_frontier_enqueues')), digits=1)}"
                f"/{fmt_number(safe_float(metrics.get('oracle_fallback_enqueues')), digits=1)}"
            )
            flow_bpf = (
                f"{fmt_number(safe_float(metrics.get('flowcausal_frontier_enqueues')), digits=1)}"
                f"/{fmt_number(safe_float(metrics.get('flowcausal_fallback_enqueues')), digits=1)}"
            )
            lines.append(
                "| "
                + " | ".join(
                    [
                        str(row.get("display_label") or row.get("label", "")),
                        f"`{row.get('status', '')}`",
                        f"{trials.get('default_taskset', 0)}/{trials.get('oracle_frontier', 0)}/{trials.get('flowcausal_width', 0)}",
                        ",".join(str(x) for x in as_list(row.get("oracle_driver_returncodes"))),
                        oracle_bpf,
                        flow_bpf,
                        elapsed,
                        str(row.get("claim", "")),
                    ]
                )
                + " |"
            )
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in as_list(report.get("blockers")):
            lines.append(f"- {blocker}")
    if report.get("next_actions"):
        lines.extend(["", "## Next actions", ""])
        for action in as_list(report.get("next_actions")):
            lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def latex_table(report: dict[str, Any]) -> str:
    records = [row for row in as_list(report.get("repo_stage_evidence")) if isinstance(row, dict)]
    lines = [
        r"% Auto-generated by experiments/summarize_real_agent_evidence.py.",
        r"% Repo-stage matrices are mechanism/boundary evidence, not full real-agent endpoint evidence.",
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\caption{Current FlowCausal repo-stage evidence. Negative values in critical/workload-elapsed columns mean FlowCausal is faster than Linux default on that metric.}",
        r"\label{tab:flowcausal-repo-stage-current}",
        r"\begin{tabular}{lrrrrrl}",
        r"\toprule",
        r"Workload & Trials & Crit. $\Delta$ default & Crit. $\Delta$ best & Workload $\Delta$ & Victim p99 & Gate status \\",
        r"\midrule",
    ]
    if not records:
        lines.append(r"\multicolumn{7}{c}{No repo-stage matrix evidence supplied.} \\")
    for row in records:
        metrics = as_dict(row.get("metrics"))
        gates = as_dict(row.get("gates"))
        workload = str(row.get("display_label") or row.get("label", ""))
        trials = as_dict(row.get("trials")).get("flowcausal_width", "")
        critical = fmt_number(safe_float(metrics.get("flow_vs_default_critical_delta_pct")), digits=1, suffix="%")
        critical_best = fmt_number(safe_float(metrics.get("flow_vs_best_non_flow_critical_delta_pct")), digits=1, suffix="%")
        total = fmt_number(safe_float(metrics.get("flow_vs_default_total_delta_pct")), digits=1, suffix="%")
        victim = (
            f"{fmt_number(safe_float(metrics.get('default_victim_lateness_p99_ms')))}"
            "->"
            f"{fmt_number(safe_float(metrics.get('flowcausal_victim_lateness_p99_ms')))} ms"
        )
        gate_status = (
            f"D:{gates.get('direct CPU mechanism', 'missing')}, "
            f"O:{gates.get('stage/episode outcome', 'missing')}, "
            f"S:{gates.get('victim/fairness safety', 'missing')}"
        )
        lines.append(
            " & ".join(
                    [
                        latex_escape(workload),
                        latex_escape(trials),
                        latex_escape(critical),
                        latex_escape(critical_best),
                        latex_escape(total),
                        latex_escape(victim),
                        latex_escape(gate_status),
                ]
            )
            + r" \\"
        )
    lines.extend(
        [
            r"\bottomrule",
            r"\end{tabular}",
            r"\vspace{0.35em}",
            r"\footnotesize D/O/S denote direct CPU mechanism, stage outcome, and safety gates. This table does not claim full SWE-agent/OpenHands endpoint improvement.",
            r"\end{table}",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Summarize FlowCausal real-agent evidence readiness across intake/preflight/collection reports",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--intake-report", help="JSON report from build_real_agent_manifest.py")
    parser.add_argument("--readiness-report", help="JSON report from check_real_agent_readiness.py")
    parser.add_argument("--capture-plan-preflight", help="JSON report from preflight_real_agent_capture_plan.py")
    parser.add_argument("--harness-matrix-plan", help="JSON report from plan_real_agent_harness_matrices.py")
    parser.add_argument("--harness-matrix-runbook-preflight", help="JSON report from preflight_real_agent_harness_matrix_runbooks.py")
    parser.add_argument("--bootstrap-report", help="JSON report from bootstrap_real_agent_harness.py")
    parser.add_argument("--repo-prep-report", help="JSON report from prepare_real_agent_repos.py")
    parser.add_argument("--harness-matrix-audit", help="JSON report from audit_real_agent_harness_matrix.py")
    parser.add_argument("--repo-stage-input", action="append", default=[], help="repo-stage matrix-summary.json file or directory")
    parser.add_argument("--repo-stage-recursive", action="store_true", help="recursively discover repo-stage matrix-summary.json under --repo-stage-input directories")
    parser.add_argument("--mechanism-smoke-input", action="append", default=[], help="mechanism smoke matrix-summary.json file or directory, e.g. oracle_frontier repo-stage smoke")
    parser.add_argument("--mechanism-smoke-recursive", action="store_true", help="recursively discover mechanism smoke matrix-summary.json under --mechanism-smoke-input directories")
    parser.add_argument("--preflight-report", help="JSON report from validate_bundle_collection_manifest.py")
    parser.add_argument("--collection-audit", help="JSON report from audit_bundle_collection.py")
    parser.add_argument("--bundle-input", action="append", default=[], help="bundle audit file or directory; summarized if --collection-audit is absent")
    parser.add_argument("--recursive", action="store_true", help="recursively discover bundle audits under --bundle-input directories")
    parser.add_argument("--min-ready-bundles", type=int, default=3)
    parser.add_argument("--output", help="write dashboard JSON here")
    parser.add_argument("--markdown-output", help="write dashboard markdown here")
    parser.add_argument("--latex-output", help="write repo-stage evidence LaTeX table here")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = summarize(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        output = pathlib.Path(args.markdown_output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(report), encoding="utf-8")
    if args.latex_output:
        output = pathlib.Path(args.latex_output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(latex_table(report), encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Run HarnessReplayBench mix cases across variants and trials.

This is the matrix layer above ``run_harness_replay_mix.py``.  It keeps the
mix runner focused on one colocated replay, while this script repeats that mix
under named baselines such as Linux default and static cpuset, then runs the
structural fidelity audit for every trial.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import shlex
import statistics
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from run_buildtest_matrix import parse_flowmon  # noqa: E402

REPO_ROOT = HERE.parents[1]
MIX_RUNNER = HERE / "run_harness_replay_mix.py"
FIDELITY_AUDIT = HERE / "audit_harness_replay_fidelity.py"
LIVE_SANDBOX = HERE / "run_live_sandbox.py"
EXTERNAL_SCX_SANDBOX = HERE / "run_external_scx_sandbox.py"
PROFILE_MATRIX_SUMMARY = HERE / "summarize_harness_replay_profiles.py"
VICTIM_MATRIX_SUMMARY = HERE / "summarize_victim_logs.py"
LOCK_SCHED_SCX_BIN = REPO_ROOT / "lock-sched" / "tools" / "sched_ext" / "build" / "bin"
SCHEMA = "flowcausal.harness_replay_matrix.v1"


def scx_alias(name: str) -> str:
    return f"{name}:kind=scx,scx_scheduler={LOCK_SCHED_SCX_BIN / name}"


NAMED_VARIANT_ALIASES = {
    "scx_simple": scx_alias("scx_simple"),
    "scx_eevdf": scx_alias("scx_eevdf"),
    "scx_qmap": scx_alias("scx_qmap"),
    "scx_central": scx_alias("scx_central"),
    "scx_flatcg": scx_alias("scx_flatcg"),
    "scx_cfl": scx_alias("scx_cfl"),
    "scx_batch": scx_alias("scx_batch"),
    "scx_nest": scx_alias("scx_nest"),
    "scx_hybrid": scx_alias("scx_hybrid"),
    "scx_toponest": scx_alias("scx_toponest"),
    "scx_tailguard": scx_alias("scx_tailguard"),
    "scx_flowcausal_bpf": "scx_flowcausal_bpf:kind=scx,scx_scheduler=/tmp/scx_flowcausal_bpf",
    "scx_flowcausal2_bpf": "scx_flowcausal2_bpf:kind=scx,scx_scheduler=/tmp/scx_flowcausal2_bpf",
    "scx_flowcausal3_bpf": "scx_flowcausal3_bpf:kind=scx,scx_scheduler=/tmp/scx_flowcausal3_bpf",
    "scx_flowcausal_converge": (
        "scx_flowcausal_converge:kind=scx,scx_scheduler=/tmp/scx_flowcausal_converge"
    ),
    "scx_flowcausal_converge_pure_percpu": (
        "scx_flowcausal_converge_pure_percpu:kind=scx,scx_scheduler=/tmp/scx_flowcausal_converge,"
        "scx_args=--shared-percpu-dsq"
    ),
    "scx_flowcausal_stagewidth": (
        "scx_flowcausal_stagewidth:kind=scx,scx_scheduler=/tmp/scx_flowcausal_stagewidth"
    ),
    "scx_flowcausal_stagewidth_pure_percpu": (
        "scx_flowcausal_stagewidth_pure_percpu:kind=scx,scx_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "scx_args=--shared-percpu-dsq"
    ),
    "scx_flowcausal_stagewidth_pure_local_percpu": (
        "scx_flowcausal_stagewidth_pure_local_percpu:kind=scx,scx_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "scx_args=--shared-percpu-dsq --shared-local-dsq"
    ),
    "flowcausal_stagewidth_rootassist": (
        "flowcausal_stagewidth_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_frontier_rootassist": (
        "flowcausal_stagewidth_frontier_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_anchor_rootassist": (
        "flowcausal_stagewidth_anchor_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,locality_frontier_only=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_deadline_rootassist": (
        "flowcausal_stagewidth_deadline_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,locality_frontier_only=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_anchor_remembered": (
        "flowcausal_stagewidth_anchor_remembered:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,locality_frontier_only=true,"
        "locality_remembered_first=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_anchor_localfallback": (
        "flowcausal_stagewidth_anchor_localfallback:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,locality_frontier_only=true,"
        "frontier_fallback_local_dsq=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_anchor_shifted": (
        "flowcausal_stagewidth_anchor_shifted:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,locality_frontier_only=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_partition_rootassist": (
        "flowcausal_stagewidth_partition_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,ttl_ms=0"
    ),
    "flowcausal_stagewidth_partition611_rootassist": (
        "flowcausal_stagewidth_partition611_rootassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_stagewidth,"
        "width_control=true,shared_percpu_dsq=true,ttl_ms=0"
    ),
    "flowcausal_attached": "flowcausal_attached:kind=flowcausal,no_controller=true",
    "flowcausal_attached_shared_local": (
        "flowcausal_attached_shared_local:kind=flowcausal,no_controller=true,shared_local_dsq=true"
    ),
    "flowcausal_attached_shared_percpu": (
        "flowcausal_attached_shared_percpu:kind=flowcausal,no_controller=true,shared_percpu_dsq=true"
    ),
    "flowcausal_flowd": "flowcausal_flowd:kind=flowcausal,no_controller=true,per_case_flowd=true",
    "flowcausal_flowd_frontier_depth1_affinity_rr": (
        "flowcausal_flowd_frontier_depth1_affinity_rr:"
        "kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "frontier_propagation=true,frontier_propagation_depth=1,"
        "flowd_anchor_policy=round-robin"
    ),
    "flowcausal_flowd_frontier_depth1_affinity_frontieronly_rr": (
        "flowcausal_flowd_frontier_depth1_affinity_frontieronly_rr:"
        "kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "frontier_propagation=true,frontier_propagation_depth=1,"
        "flowd_anchor_policy=round-robin,locality_frontier_only=true"
    ),
    "flowcausal_flowd_frontier_depth1_affinity_frontieronly_remembered_rr": (
        "flowcausal_flowd_frontier_depth1_affinity_frontieronly_remembered_rr:"
        "kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "frontier_propagation=true,frontier_propagation_depth=1,"
        "flowd_anchor_policy=round-robin,locality_frontier_only=true,"
        "locality_remembered_first=true"
    ),
    "flowcausal_flowd_frontier_depth1_pressure_wait5_affinity_frontieronly_rr": (
        "flowcausal_flowd_frontier_depth1_pressure_wait5_affinity_frontieronly_rr:"
        "kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "frontier_propagation=true,frontier_propagation_depth=1,"
        "flowd_anchor_policy=round-robin,locality_frontier_only=true,"
        "shared_wait_pressure_us=5000"
    ),
    "flowcausal_cplane": (
        "flowcausal_cplane:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowcausal_scheduler=/tmp/scx_flowcausal,ttl_ms=0"
    ),
    "flowcausal2_cplane": (
        "flowcausal2_cplane:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowcausal_scheduler=/tmp/scx_flowcausal2,ttl_ms=0"
    ),
    "flowcausal_bpf": (
        "flowcausal_bpf:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_bpf,"
        "width_control=true,ttl_ms=0"
    ),
    "flowcausal2_bpf": (
        "flowcausal2_bpf:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal2_bpf,"
        "width_control=true,ttl_ms=0"
    ),
    "flowcausal3_bpf": (
        "flowcausal3_bpf:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal3_bpf,"
        "width_control=true,ttl_ms=0"
    ),
    "flowcausal_bpf_cassist": (
        "flowcausal_bpf_cassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowd_assist_bpf_auto=true,"
        "flowcausal_scheduler=/tmp/scx_flowcausal_bpf,width_control=true,ttl_ms=0"
    ),
    "flowcausal2_bpf_cassist": (
        "flowcausal2_bpf_cassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowd_assist_bpf_auto=true,"
        "flowcausal_scheduler=/tmp/scx_flowcausal2_bpf,width_control=true,ttl_ms=0"
    ),
    "flowcausal3_bpf_cassist": (
        "flowcausal3_bpf_cassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowd_assist_bpf_auto=true,"
        "flowcausal_scheduler=/tmp/scx_flowcausal3_bpf,width_control=true,ttl_ms=0"
    ),
    "flowcausal_converge": (
        "flowcausal_converge:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_converge,"
        "width_control=true,ttl_ms=0"
    ),
    "flowcausal_converge_cassist": (
        "flowcausal_converge_cassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowd_assist_bpf_auto=true,"
        "flowcausal_scheduler=/tmp/scx_flowcausal_converge,width_control=true,ttl_ms=0"
    ),
    "flowcausal_converge_v8_stageassist": (
        "flowcausal_converge_v8_stageassist:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=true,flowd_assist_bpf_auto=true,"
        "flowcausal_scheduler=/tmp/scx_flowcausal_converge,width_control=true,ttl_ms=0"
    ),
    "flowcausal_converge_v9_pure_percpu": (
        "flowcausal_converge_v9_pure_percpu:kind=flowcausal,no_controller=true,per_case_flowd=true,"
        "flowd_control_plane=false,flowcausal_scheduler=/tmp/scx_flowcausal_converge,"
        "width_control=true,frontier_propagation=true,frontier_propagation_depth=1,"
        "shared_percpu_dsq=true,ttl_ms=0"
    ),
    "flowcausal_observe": "flowcausal_observe:kind=flowcausal,hint_dry_run=true,frontier_action=observe",
    "flowcausal_no_width": "flowcausal_no_width:kind=flowcausal,frontier_propagation=true",
    "flowcausal_width": (
        "flowcausal_width:kind=flowcausal,width_control=true,"
        "frontier_propagation=true,stage_aware_frontier_propagation_depth=true"
    ),
    "flowcausal_full": (
        "flowcausal_full:kind=flowcausal,width_control=true,frontier_propagation=true,"
        "stage_aware_frontier_propagation_depth=true,background_dsq=true,background_dispatch_quota=12"
    ),
    "flowcausal_case_observe": (
        "flowcausal_case_observe:kind=flowcausal,per_case_flowctl=true,"
        "hint_dry_run=true,frontier_action=observe"
    ),
    "flowcausal_case_no_width": (
        "flowcausal_case_no_width:kind=flowcausal,per_case_flowctl=true,frontier_propagation=true"
    ),
    "flowcausal_case_width": (
        "flowcausal_case_width:kind=flowcausal,per_case_flowctl=true,width_control=true,"
        "frontier_propagation=true,stage_aware_frontier_propagation_depth=true"
    ),
    "flowcausal_case_full": (
        "flowcausal_case_full:kind=flowcausal,per_case_flowctl=true,width_control=true,"
        "frontier_propagation=true,stage_aware_frontier_propagation_depth=true,"
        "background_dsq=true,background_dispatch_quota=12"
    ),
    "flowcausal_case_stage_aware": (
        "flowcausal_case_stage_aware:kind=flowcausal,per_case_flowctl=true,"
        "frontier_action=stage-aware,frontier_propagation=true,"
        "stage_aware_frontier_propagation_depth=true,frontier_fallback_local_dsq=true"
    ),
    "flowcausal_case_stage_aware_bg": (
        "flowcausal_case_stage_aware_bg:kind=flowcausal,per_case_flowctl=true,"
        "frontier_action=stage-aware,frontier_propagation=true,"
        "stage_aware_frontier_propagation_depth=true,frontier_fallback_local_dsq=true,"
        "background_dsq=true,background_dispatch_quota=12"
    ),
}


@dataclass(frozen=True)
class MatrixVariant:
    name: str
    kind: str = "baseline"
    taskset: str = ""
    nice: int | None = None
    cgroup_cpu_weight: int | None = None
    cgroup_cpu_max: str = ""
    width_control: bool = False
    frontier_width: int | None = None
    max_frontier_width: int | None = None
    hint_dry_run: bool = False
    frontier_action: str = "advance"
    build_frontier_admission: str | None = None
    frontier_propagation: bool = False
    frontier_propagation_depth: int | None = None
    stage_aware_frontier_propagation_depth: bool = False
    background_dsq: bool = False
    background_dispatch_quota: int = 0
    per_case_flowctl: bool = False
    per_case_flowd: bool = False
    no_controller: bool = False
    shared_local_dsq: bool = False
    tracked_local_dsq: bool = False
    frontier_fallback_local_dsq: bool = False
    shared_percpu_dsq: bool = False
    frontier_dispatch_quota: int | None = None
    flowd_anchor_policy: str = ""
    flowd_anchor_cpus: str = ""
    flowd_anchor_cpu: int | None = None
    locality_frontier_only: bool = False
    locality_remembered_first: bool = False
    shared_wait_pressure_us: int | None = None
    pressure_local_cpu: bool = False
    confidence: int | None = None
    policy_window_ms: int | None = None
    boost_budget_ms: int | None = None
    max_vtime_boost_us: int | None = None
    ttl_ms: int | None = None
    flowd_control_plane: bool = False
    flowd_assist_bpf_auto: bool = False
    flowd_cxx_only_session_policy: bool = False
    flowd_affinity_by_session: bool = False
    flowd_python_affinity_by_session: bool = False
    flowd_critical_affinity_by_session: bool = False
    flowd_balanced_affinity_by_session: bool = False
    flowd_affinity_release_after_first_fork: bool = False
    flowd_affinity_release_package_only: bool = False
    flowd_stage_state: bool = False
    flowd_stage_state_poll_ms: int | None = None
    delay_workload_until_flowd: bool = False
    root_flowd: bool = False
    root_flowd_session: str = "harness:supervisor"
    root_flowd_anchor_cpu: int | None = None
    root_flowd_propagation_depth: int | None = None
    flowcausal_scheduler: str = ""
    scx_scheduler: str = ""
    scx_scheduler_args: tuple[str, ...] = ()
    raw_options: tuple[tuple[str, str], ...] = ()


def safe_name(value: str) -> str:
    out = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            out.append(char)
        else:
            out.append("_")
    return "".join(out).strip("._") or "variant"


def parse_variant(spec: str) -> MatrixVariant:
    spec = NAMED_VARIANT_ALIASES.get(spec.strip(), spec)
    if ":" not in spec:
        return MatrixVariant(name=spec.strip())
    name, rest = spec.split(":", 1)
    fields: dict[str, str] = {}
    for item in rest.split(","):
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(f"variant option must be KEY=VALUE: {spec!r}")
        key, value = item.split("=", 1)
        fields[key.strip()] = value.strip()
    kind = fields.get("kind", "baseline").strip() or "baseline"
    if kind not in {"baseline", "flowcausal", "scx"}:
        raise SystemExit(f"unsupported variant kind {kind!r}; expected baseline, flowcausal, or scx")
    nice = parse_optional_int(fields.get("nice"))
    if nice is not None and (nice < -20 or nice > 19):
        raise SystemExit(f"variant nice expects a Linux nice value in [-20, 19], got {nice}")
    cgroup_cpu_weight = parse_optional_int(fields.get("cgroup_cpu_weight", fields.get("cpu_weight")))
    if cgroup_cpu_weight is not None and (cgroup_cpu_weight < 1 or cgroup_cpu_weight > 10000):
        raise SystemExit(
            f"variant cgroup cpu_weight expects a cgroup-v2 value in [1, 10000], got {cgroup_cpu_weight}"
        )
    cgroup_cpu_max = normalize_cpu_max_option(fields.get("cgroup_cpu_max", fields.get("cpu_max", "")))
    build_frontier_admission = fields.get("build_frontier_admission")
    if build_frontier_admission is not None and build_frontier_admission not in {"advance", "tail-only", "observe"}:
        raise SystemExit(
            "variant build_frontier_admission expects advance, tail-only, or observe; "
            f"got {build_frontier_admission!r}"
        )
    flowd_anchor_policy = fields.get("flowd_anchor_policy", fields.get("anchor_policy", ""))
    if flowd_anchor_policy and flowd_anchor_policy not in {"none", "fixed", "round-robin"}:
        raise SystemExit(
            "variant flowd_anchor_policy expects none, fixed, or round-robin; "
            f"got {flowd_anchor_policy!r}"
        )
    return MatrixVariant(
        name=name.strip(),
        kind=kind,
        taskset=fields.get("taskset", ""),
        nice=nice,
        cgroup_cpu_weight=cgroup_cpu_weight,
        cgroup_cpu_max=cgroup_cpu_max,
        width_control=parse_bool(fields.get("width_control", fields.get("width", "false"))),
        frontier_width=parse_optional_int(fields.get("frontier_width")),
        max_frontier_width=parse_optional_int(fields.get("max_frontier_width")),
        hint_dry_run=parse_bool(fields.get("hint_dry_run", fields.get("dry_run", "false"))),
        frontier_action=fields.get("frontier_action", "advance"),
        build_frontier_admission=build_frontier_admission,
        frontier_propagation=parse_bool(fields.get("frontier_propagation", "false")),
        frontier_propagation_depth=parse_optional_int(fields.get("frontier_propagation_depth")),
        stage_aware_frontier_propagation_depth=parse_bool(
            fields.get("stage_aware_frontier_propagation_depth", "false")
        ),
        background_dsq=parse_bool(fields.get("background_dsq", "false")),
        background_dispatch_quota=parse_int(fields.get("background_dispatch_quota", "0")),
        per_case_flowctl=parse_bool(fields.get("per_case_flowctl", "false")),
        per_case_flowd=parse_bool(fields.get("per_case_flowd", "false")),
        no_controller=parse_bool(fields.get("no_controller", fields.get("disable_controller", "false"))),
        shared_local_dsq=parse_bool(fields.get("shared_local_dsq", fields.get("shared_local", "false"))),
        tracked_local_dsq=parse_bool(fields.get("tracked_local_dsq", fields.get("tracked_local", "false"))),
        frontier_fallback_local_dsq=parse_bool(
            fields.get("frontier_fallback_local_dsq", fields.get("frontier_local_dsq", "false"))
        ),
        shared_percpu_dsq=parse_bool(fields.get("shared_percpu_dsq", fields.get("shared_percpu", "false"))),
        frontier_dispatch_quota=parse_optional_int(fields.get("frontier_dispatch_quota")),
        flowd_anchor_policy=flowd_anchor_policy,
        flowd_anchor_cpus=fields.get("flowd_anchor_cpus", fields.get("anchor_cpus", "")),
        flowd_anchor_cpu=parse_optional_int(fields.get("flowd_anchor_cpu", fields.get("anchor_cpu"))),
        locality_frontier_only=parse_bool(fields.get("locality_frontier_only", "false")),
        locality_remembered_first=parse_bool(fields.get("locality_remembered_first", "false")),
        shared_wait_pressure_us=parse_optional_int(fields.get("shared_wait_pressure_us")),
        pressure_local_cpu=parse_bool(fields.get("pressure_local_cpu", "false")),
        confidence=parse_optional_int(fields.get("confidence")),
        policy_window_ms=parse_optional_int(fields.get("policy_window_ms")),
        boost_budget_ms=parse_optional_int(fields.get("boost_budget_ms")),
        max_vtime_boost_us=parse_optional_int(fields.get("max_vtime_boost_us")),
        ttl_ms=parse_optional_int(fields.get("ttl_ms")),
        flowd_control_plane=parse_bool(fields.get("flowd_control_plane", fields.get("c_control_plane", "false"))),
        flowd_assist_bpf_auto=parse_bool(fields.get("flowd_assist_bpf_auto", fields.get("assist_bpf_auto", "false"))),
        flowd_cxx_only_session_policy=parse_bool(
            fields.get(
                "flowd_cxx_only_session_policy",
                fields.get("cxx_only_session_policy", "false"),
            )
        ),
        flowd_affinity_by_session=parse_bool(
            fields.get("flowd_affinity_by_session", fields.get("affinity_by_session", "false"))
        ),
        flowd_python_affinity_by_session=parse_bool(
            fields.get(
                "flowd_python_affinity_by_session",
                fields.get("python_affinity_by_session", "false"),
            )
        ),
        flowd_critical_affinity_by_session=parse_bool(
            fields.get(
                "flowd_critical_affinity_by_session",
                fields.get("critical_affinity_by_session", "false"),
            )
        ),
        flowd_balanced_affinity_by_session=parse_bool(
            fields.get(
                "flowd_balanced_affinity_by_session",
                fields.get("balanced_affinity_by_session", "false"),
            )
        ),
        flowd_affinity_release_after_first_fork=parse_bool(
            fields.get(
                "flowd_affinity_release_after_first_fork",
                fields.get("affinity_release_after_first_fork", "false"),
            )
        ),
        flowd_affinity_release_package_only=parse_bool(
            fields.get(
                "flowd_affinity_release_package_only",
                fields.get("affinity_release_package_only", "false"),
            )
        ),
        flowd_stage_state=parse_bool(
            fields.get("flowd_stage_state", fields.get("stage_state", "false"))
        ),
        flowd_stage_state_poll_ms=parse_optional_int(
            fields.get("flowd_stage_state_poll_ms", fields.get("stage_state_poll_ms"))
        ),
        delay_workload_until_flowd=parse_bool(
            fields.get("delay_workload_until_flowd", fields.get("delay_start_gate", "false"))
        ),
        root_flowd=parse_bool(fields.get("root_flowd", "false")),
        root_flowd_session=fields.get("root_flowd_session", "harness:supervisor"),
        root_flowd_anchor_cpu=parse_optional_int(fields.get("root_flowd_anchor_cpu")),
        root_flowd_propagation_depth=parse_optional_int(fields.get("root_flowd_propagation_depth")),
        flowcausal_scheduler=fields.get("flowcausal_scheduler", ""),
        scx_scheduler=fields.get("scx_scheduler", fields.get("scheduler", "")),
        scx_scheduler_args=parse_scx_scheduler_args(fields.get("scx_args", fields.get("scheduler_args", ""))),
        raw_options=tuple(sorted(fields.items())),
    )


def expand_variant_specs(raw_specs: list[str]) -> list[str]:
    """Expand CLI variant shorthand into one spec per variant.

    Older runbooks often use ``--variant default,flowcausal_no_width``.  Variant
    option specs also use commas after ``name:``, so only split comma lists that
    do not contain a colon.
    """

    expanded: list[str] = []
    for raw in raw_specs:
        text = raw.strip()
        if not text:
            continue
        if ":" not in text and "," in text:
            expanded.extend(part.strip() for part in text.split(",") if part.strip())
        else:
            expanded.append(text)
    return expanded


def parse_bool(value: str | None) -> bool:
    if value is None:
        return False
    text = value.strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off", ""}:
        return False
    raise SystemExit(f"invalid boolean value {value!r}")


def parse_int(value: str | None) -> int:
    try:
        return int(str(value or "0"))
    except ValueError as exc:
        raise SystemExit(f"invalid integer value {value!r}") from exc


def parse_optional_int(value: str | None) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    return parse_int(value)


def parse_scx_scheduler_args(value: str | None) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        return ()
    return tuple(shlex.split(text))


def normalize_cpu_max_option(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("/", " ")
    parts = normalized.split()
    if len(parts) != 2:
        raise SystemExit("variant cpu_max expects 'max/PERIOD', 'QUOTA/PERIOD', or quoted cpu.max text")
    quota, period = parts
    try:
        period_value = int(period)
    except ValueError as exc:
        raise SystemExit(f"variant cpu_max has invalid period: {value!r}") from exc
    if period_value <= 0:
        raise SystemExit(f"variant cpu_max period must be positive: {value!r}")
    if quota != "max":
        try:
            quota_value = int(quota)
        except ValueError as exc:
            raise SystemExit(f"variant cpu_max has invalid quota: {value!r}") from exc
        if quota_value <= 0:
            raise SystemExit(f"variant cpu_max quota must be positive or 'max': {value!r}")
    return f"{quota} {period_value}"


def variant_frontier_width(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(variant.frontier_width if variant.frontier_width is not None else args.flowcausal_frontier_width)


def variant_max_frontier_width(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.max_frontier_width
        if variant.max_frontier_width is not None
        else args.flowcausal_max_frontier_width
    )


def variant_taskset(args: argparse.Namespace, variant: MatrixVariant) -> str:
    return str(variant.taskset or getattr(args, "taskset", "") or "")


def variant_build_frontier_admission(args: argparse.Namespace, variant: MatrixVariant) -> str:
    return str(variant.build_frontier_admission or args.flowcausal_build_frontier_admission)


def variant_frontier_dispatch_quota(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.frontier_dispatch_quota
        if variant.frontier_dispatch_quota is not None
        else args.flowcausal_frontier_dispatch_quota
    )


def variant_frontier_propagation_depth(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.frontier_propagation_depth
        if variant.frontier_propagation_depth is not None
        else args.flowcausal_frontier_propagation_depth
    )


def variant_shared_wait_pressure_us(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.shared_wait_pressure_us
        if variant.shared_wait_pressure_us is not None
        else args.flowcausal_shared_wait_pressure_us
    )


def variant_confidence(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(variant.confidence if variant.confidence is not None else args.flowcausal_confidence)


def variant_policy_window_ms(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.policy_window_ms
        if variant.policy_window_ms is not None
        else args.flowcausal_policy_window_ms
    )


def variant_boost_budget_ms(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.boost_budget_ms
        if variant.boost_budget_ms is not None
        else args.flowcausal_boost_budget_ms
    )


def variant_max_vtime_boost_us(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(
        variant.max_vtime_boost_us
        if variant.max_vtime_boost_us is not None
        else args.flowcausal_max_vtime_boost_us
    )


def variant_ttl_ms(args: argparse.Namespace, variant: MatrixVariant) -> int:
    return int(variant.ttl_ms if variant.ttl_ms is not None else args.flowcausal_ttl_ms)


def victim_config(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "enabled": bool(getattr(args, "victim_command", "")),
        "command": getattr(args, "victim_command", "") or "",
        "taskset": getattr(args, "victim_taskset", "") or "",
        "warmup_sec": float(getattr(args, "victim_warmup_sec", 0.2)),
    }


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


def mean_dict(rows: list[dict[str, Any]]) -> dict[str, float]:
    keys = sorted({key for row in rows if isinstance(row, dict) for key in row})
    out: dict[str, float] = {}
    for key in keys:
        values: list[float] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            value = row.get(key)
            if isinstance(value, bool):
                continue
            try:
                values.append(float(value))
            except (TypeError, ValueError):
                continue
        if values:
            out[key] = statistics.mean(values)
    return out


def variant_summary(trials: list[dict[str, Any]]) -> dict[str, Any]:
    makespan = [float(trial.get("makespan_sec") or 0.0) for trial in trials if trial.get("status") == "pass"]
    mean_critical = [
        float(trial.get("aggregate", {}).get("mean_critical_sec") or 0.0)
        for trial in trials
        if trial.get("status") == "pass" and trial.get("aggregate", {}).get("mean_critical_sec") is not None
    ]
    p95_critical = [
        float(trial.get("aggregate", {}).get("p95_critical_sec") or 0.0)
        for trial in trials
        if trial.get("status") == "pass" and trial.get("aggregate", {}).get("p95_critical_sec") is not None
    ]
    elapsed_jain = [
        float(trial.get("aggregate", {}).get("elapsed_jain_fairness") or 0.0)
        for trial in trials
        if trial.get("status") == "pass" and trial.get("aggregate", {}).get("elapsed_jain_fairness") is not None
    ]
    critical_jain = [
        float(trial.get("aggregate", {}).get("critical_jain_fairness") or 0.0)
        for trial in trials
        if trial.get("status") == "pass" and trial.get("aggregate", {}).get("critical_jain_fairness") is not None
    ]
    failed_stage_commands = [
        float(trial.get("aggregate", {}).get("failed_stage_commands") or 0.0)
        for trial in trials
        if trial.get("status") == "pass"
    ]
    flowmon_rows = [trial.get("flowmon", {}) for trial in trials if isinstance(trial.get("flowmon"), dict)]
    return {
        "trials": len(trials),
        "pass_trials": sum(1 for trial in trials if trial.get("status") == "pass"),
        "makespan_mean_sec": mean(makespan),
        "makespan_std_sec": pstdev(makespan),
        "makespan_p95_sec": p95(makespan),
        "mean_critical_mean_sec": mean(mean_critical),
        "mean_critical_std_sec": pstdev(mean_critical),
        "p95_critical_mean_sec": mean(p95_critical),
        "elapsed_jain_fairness_mean": mean(elapsed_jain),
        "critical_jain_fairness_mean": mean(critical_jain),
        "failed_stage_commands_mean": mean(failed_stage_commands),
        "flowmon_present_trials": sum(1 for row in flowmon_rows if row),
        "flowmon_mean": mean_dict(flowmon_rows),
    }


def relative_delta(base: float | None, value: float | None) -> float | None:
    if base is None or value is None or base == 0:
        return None
    return (value - base) / base


def add_baseline_comparisons(variants: list[dict[str, Any]]) -> None:
    if not variants:
        return
    baseline = variants[0]
    base_summary = baseline.get("summary", {})
    baseline["comparison_to_baseline"] = {
        "baseline": baseline.get("name"),
        "makespan_delta": 0.0,
        "mean_critical_delta": 0.0,
        "p95_critical_delta": 0.0,
    }
    for variant in variants[1:]:
        summary = variant.get("summary", {})
        variant["comparison_to_baseline"] = {
            "baseline": baseline.get("name"),
            "makespan_delta": relative_delta(
                base_summary.get("makespan_mean_sec"),
                summary.get("makespan_mean_sec"),
            ),
            "mean_critical_delta": relative_delta(
                base_summary.get("mean_critical_mean_sec"),
                summary.get("mean_critical_mean_sec"),
            ),
            "p95_critical_delta": relative_delta(
                base_summary.get("p95_critical_mean_sec"),
                summary.get("p95_critical_mean_sec"),
            ),
        }


def build_mix_cmd(args: argparse.Namespace, variant: MatrixVariant, trial_dir: pathlib.Path) -> list[str]:
    cmd = [
        sys.executable,
        str(MIX_RUNNER),
        "--out-dir",
        str(trial_dir),
        "--overwrite",
        "--stagger-ms",
        str(args.stagger_ms),
        "--container-python",
        args.container_python,
        "--container-path-env",
        args.container_path_env,
        "--docker-user",
        args.docker_user,
    ]
    if args.copy_keep_git:
        cmd.append("--copy-keep-git")
    for pattern in getattr(args, "copy_exclude", []):
        cmd.extend(["--copy-exclude", pattern])
    if getattr(args, "system_profile", False):
        cmd.append("--system-profile")
        cmd.extend(["--system-profile-interval-ms", str(args.system_profile_interval_ms)])
    if getattr(args, "keep_success_artifacts", False):
        cmd.append("--keep-success-artifacts")
    else:
        cmd.append("--cleanup-success-artifacts")
    if getattr(args, "cleanup_use_sudo", True):
        cmd.append("--cleanup-use-sudo")
    else:
        cmd.append("--no-cleanup-use-sudo")
    for artifact in getattr(args, "cleanup_artifact", []):
        cmd.extend(["--cleanup-artifact", artifact])
    taskset = variant_taskset(args, variant)
    if taskset:
        cmd.extend(["--taskset", taskset])
    if variant.nice is not None:
        cmd.extend(["--nice", str(variant.nice)])
    if variant.cgroup_cpu_weight is not None:
        cmd.extend(["--cgroup-cpu-weight", str(variant.cgroup_cpu_weight)])
    if variant.cgroup_cpu_max:
        cmd.extend(["--cgroup-cpu-max", variant.cgroup_cpu_max])
    if variant.cgroup_cpu_weight is not None or variant.cgroup_cpu_max:
        cmd.extend(
            [
                "--cgroup-wrapper",
                args.cgroup_wrapper,
                "--cgroup-sudo",
                args.cgroup_sudo,
                "--cgroup-name-prefix",
                safe_name(f"flowcausal-harness-{variant.name}"),
            ]
        )
    for case in args.case:
        cmd.extend(["--case", case])
    if args.manifest:
        cmd.extend(["--manifest", args.manifest])
    for stage in args.allow_failure_stage:
        cmd.extend(["--allow-failure-stage", stage])
    for entry in args.env:
        cmd.extend(["--env", entry])
    for entry in getattr(args, "host_env", []):
        cmd.extend(["--host-env", entry])
    for entry in getattr(args, "container_env", []):
        cmd.extend(["--container-env", entry])
    if variant.kind == "flowcausal" and variant.per_case_flowctl:
        cmd.extend(
            [
                "--per-case-flowctl",
                "--flowctl",
                args.flowcausal_flowctl,
                "--flowctl-pin-dir",
                args.flowcausal_pin_dir,
                "--flowctl-sudo",
                args.flowcausal_flowctl_sudo,
                "--flowctl-stage-kind",
                args.flowcausal_stage_kind,
                "--flowctl-frontier-width",
                str(variant_frontier_width(args, variant)),
                "--flowctl-max-frontier-width",
                str(variant_max_frontier_width(args, variant)),
                "--flowctl-min-frontier-score",
                str(args.flowcausal_min_frontier_score),
                "--flowctl-confidence",
                str(variant_confidence(args, variant)),
                "--flowctl-ttl-ms",
                str(variant_ttl_ms(args, variant)),
                "--flowctl-policy-window-ms",
                str(variant_policy_window_ms(args, variant)),
                "--flowctl-boost-budget-ms",
                str(variant_boost_budget_ms(args, variant)),
                "--flowctl-max-vtime-boost-us",
                str(variant_max_vtime_boost_us(args, variant)),
                "--flowctl-interval-ms",
                str(args.flowcausal_interval_ms),
                "--flowctl-frontier-stability-samples",
                str(args.flowcausal_frontier_stability_samples),
                "--flowctl-frontier-hold-ms",
                str(args.flowcausal_frontier_hold_ms),
                "--flowctl-frontier-bypass-score",
                str(args.flowcausal_frontier_bypass_score),
                "--flowctl-frontier-stability-action",
                args.flowcausal_frontier_stability_action,
                "--flowctl-frontier-action",
                variant.frontier_action,
                "--flowctl-build-frontier-admission",
                variant_build_frontier_admission(args, variant),
            ]
        )
        if variant.hint_dry_run:
            cmd.append("--flowctl-dry-run")
        if variant.width_control:
            cmd.append("--flowctl-width-control")
        if variant.frontier_propagation:
            cmd.extend([
                "--flowctl-frontier-propagation",
                "--flowctl-frontier-propagation-depth",
                str(variant_frontier_propagation_depth(args, variant)),
            ])
        if variant.stage_aware_frontier_propagation_depth:
            cmd.append("--flowctl-stage-aware-frontier-propagation-depth")
        if args.flowcausal_frontier_selected_stability:
            cmd.append("--flowctl-selected-stability")
        if args.flowcausal_policy_fallback:
            cmd.append("--flowctl-policy-fallback")
        if not variant.background_dsq:
            cmd.append("--flowctl-no-background-hints")
        if getattr(args, "flowcausal_flowctl_taskset", ""):
            cmd.extend(["--flowctl-taskset", args.flowcausal_flowctl_taskset])
        if getattr(args, "flowcausal_flowctl_nice", 0):
            cmd.extend(["--flowctl-nice", str(args.flowcausal_flowctl_nice)])
    if variant.kind == "flowcausal" and variant.per_case_flowd:
        cmd.extend(
            [
                "--per-case-flowd",
                "--flowd",
                args.flowcausal_flowd,
                "--flowd-pin-dir",
                args.flowcausal_pin_dir,
                "--flowd-sudo",
                args.flowcausal_flowd_sudo,
                "--flowd-confidence",
                str(variant_confidence(args, variant)),
                "--flowd-max-frontier-width",
                str(variant_max_frontier_width(args, variant)),
                "--flowd-max-vtime-boost-us",
                str(variant_max_vtime_boost_us(args, variant)),
                "--flowd-policy-window-ms",
                str(variant_policy_window_ms(args, variant)),
                "--flowd-boost-budget-ms",
                str(variant_boost_budget_ms(args, variant)),
                "--flowd-ttl-ms",
                str(variant_ttl_ms(args, variant)),
            ]
        )
        if variant.flowd_control_plane:
            cmd.append("--flowd-control-plane")
        if variant.flowd_assist_bpf_auto:
            cmd.append("--flowd-assist-bpf-auto")
        if variant.flowd_cxx_only_session_policy:
            cmd.append("--flowd-cxx-only-session-policy")
        if variant.flowd_affinity_by_session:
            cmd.append("--flowd-affinity-by-session")
        if variant.flowd_python_affinity_by_session:
            cmd.append("--flowd-python-affinity-by-session")
        if variant.flowd_critical_affinity_by_session:
            cmd.append("--flowd-critical-affinity-by-session")
        if variant.flowd_balanced_affinity_by_session:
            cmd.append("--flowd-balanced-affinity-by-session")
        if variant.flowd_affinity_release_after_first_fork:
            cmd.append("--flowd-affinity-release-after-first-fork")
        if variant.flowd_affinity_release_package_only:
            cmd.append("--flowd-affinity-release-package-only")
        if variant.flowd_stage_state:
            cmd.append("--flowd-stage-state")
            if variant.flowd_stage_state_poll_ms is not None:
                cmd.extend(["--flowd-stage-state-poll-ms", str(variant.flowd_stage_state_poll_ms)])
        if variant.delay_workload_until_flowd:
            cmd.append("--delay-workload-until-flowd")
        if variant.width_control:
            cmd.append("--flowd-width-control")
        if variant.frontier_propagation or (variant.frontier_propagation_depth or 0) > 0:
            cmd.append("--flowd-frontier")
            cmd.extend([
                "--flowd-propagation-depth",
                str(variant_frontier_propagation_depth(args, variant)),
            ])
        if variant.flowd_anchor_policy:
            cmd.extend(["--flowd-anchor-policy", variant.flowd_anchor_policy])
        if variant.flowd_anchor_cpus:
            cmd.extend(["--flowd-anchor-cpus", variant.flowd_anchor_cpus])
        if variant.flowd_anchor_cpu is not None:
            cmd.extend(["--flowd-anchor-cpu", str(variant.flowd_anchor_cpu)])
        if args.flowcausal_flowd_events:
            cmd.append("--flowd-events")
    return cmd


def build_flowcausal_cmd(args: argparse.Namespace, variant: MatrixVariant, trial_dir: pathlib.Path) -> list[str]:
    """Wrap one mix trial in the existing live FlowCausal harness.

    The live harness owns scheduler/controller/flowmon lifecycle and writes its
    logs under ``trial_dir/flowcausal-live``.  The actual mix output remains in
    ``trial_dir`` so existing fidelity audits and matrix summaries keep working.
    """

    live_dir = trial_dir / "flowcausal-live"
    scheduler = variant.flowcausal_scheduler or args.flowcausal_scheduler
    cmd = [
        sys.executable,
        str(LIVE_SANDBOX),
        "--out-dir",
        str(live_dir),
        "--attach-scheduler",
        "--no-build-tools",
        "--pin-dir",
        args.flowcausal_pin_dir,
        "--scheduler",
        scheduler,
        "--flowmon",
        args.flowcausal_flowmon,
        "--flowctl",
        args.flowcausal_flowctl,
        "--stage-kind",
        args.flowcausal_stage_kind,
        "--frontier-width",
        str(variant_frontier_width(args, variant)),
        "--max-frontier-width",
        str(variant_max_frontier_width(args, variant)),
        "--min-frontier-score",
        str(args.flowcausal_min_frontier_score),
        "--confidence",
        str(variant_confidence(args, variant)),
        "--ttl-ms",
        str(variant_ttl_ms(args, variant)),
        "--policy-window-ms",
        str(variant_policy_window_ms(args, variant)),
        "--boost-budget-ms",
        str(variant_boost_budget_ms(args, variant)),
        "--max-vtime-boost-us",
        str(variant_max_vtime_boost_us(args, variant)),
        "--interval-ms",
        str(args.flowcausal_interval_ms),
        "--frontier-action",
        variant.frontier_action,
        "--build-frontier-admission",
        variant_build_frontier_admission(args, variant),
        "--frontier-stability-action",
        args.flowcausal_frontier_stability_action,
        "--frontier-stability-samples",
        str(args.flowcausal_frontier_stability_samples),
        "--frontier-hold-ms",
        str(args.flowcausal_frontier_hold_ms),
        "--frontier-bypass-score",
        str(args.flowcausal_frontier_bypass_score),
        "--frontier-dispatch-quota",
        str(variant_frontier_dispatch_quota(args, variant)),
        "--guard-cooldown-dispatches",
        str(args.flowcausal_guard_cooldown_dispatches),
        "--pressure-fallback-enqueues",
        str(args.flowcausal_pressure_fallback_enqueues),
        "--pressure-fallback-ms",
        str(args.flowcausal_pressure_fallback_ms),
        "--shared-wait-pressure-us",
        str(variant_shared_wait_pressure_us(args, variant)),
        "--pressure-dispatch-ms",
        str(args.flowcausal_pressure_dispatch_ms),
        "--frontier-dispatch-window-ms",
        str(args.flowcausal_frontier_dispatch_window_ms),
        "--frontier-dispatch-window-budget",
        str(args.flowcausal_frontier_dispatch_window_budget),
        "--shared-slice-us",
        str(args.flowcausal_shared_slice_us),
        "--shared-wakeup-slice-us",
        str(args.flowcausal_shared_wakeup_slice_us),
        "--shared-wakeup-min-block-us",
        str(args.flowcausal_shared_wakeup_min_block_us),
    ]
    if variant.width_control:
        cmd.append("--width-control")
    if variant.hint_dry_run and not variant.per_case_flowctl:
        cmd.append("--hint-dry-run")
    if variant.frontier_propagation:
        cmd.extend([
            "--frontier-propagation",
            "--frontier-propagation-depth",
            str(variant_frontier_propagation_depth(args, variant)),
        ])
    if variant.stage_aware_frontier_propagation_depth:
        cmd.append("--stage-aware-frontier-propagation-depth")
    if variant.background_dsq:
        cmd.append("--background-dsq")
    if variant.background_dispatch_quota > 0:
        cmd.extend(["--background-dispatch-quota", str(variant.background_dispatch_quota)])
    if variant.shared_local_dsq:
        cmd.append("--shared-local-dsq")
    if variant.tracked_local_dsq:
        cmd.append("--tracked-local-dsq")
    if variant.frontier_fallback_local_dsq:
        cmd.append("--frontier-fallback-local-dsq")
    if variant.shared_percpu_dsq:
        cmd.append("--shared-percpu-dsq")
    if variant.pressure_local_cpu:
        cmd.append("--pressure-local-cpu")
    if variant.locality_frontier_only:
        cmd.append("--locality-frontier-only")
    if variant.locality_remembered_first:
        cmd.append("--locality-remembered-first")
    if variant.no_controller or variant.per_case_flowctl or variant.per_case_flowd:
        cmd.append("--no-controller")
    if variant.root_flowd:
        cmd.extend(["--root-flowd", "--root-flowd-bin", args.flowcausal_flowd])
        if variant.root_flowd_session:
            cmd.extend(["--root-flowd-session", variant.root_flowd_session])
        if variant.root_flowd_anchor_cpu is not None:
            cmd.extend(["--root-flowd-anchor-cpu", str(variant.root_flowd_anchor_cpu)])
        if variant.root_flowd_propagation_depth is not None:
            cmd.extend(["--root-flowd-propagation-depth", str(variant.root_flowd_propagation_depth)])
    if getattr(args, "flowcausal_no_flowmon", False):
        cmd.append("--no-flowmon")
    if args.flowcausal_no_event_flowmon:
        cmd.append("--no-event-flowmon")
    if args.flowcausal_no_final_snapshot:
        cmd.append("--no-final-snapshot")
    if args.flowcausal_collect_characterization:
        cmd.extend(
            [
                "--collect-characterization",
                "--characterization-interval-ms",
                str(args.flowcausal_characterization_interval_ms),
                "--characterization-top-candidates",
                str(args.flowcausal_characterization_top_candidates),
            ]
        )
    if args.flowcausal_shared_wakeup_preempt:
        cmd.append("--shared-wakeup-preempt")
    if args.flowcausal_frontier_wakeup_preempt:
        cmd.append("--frontier-wakeup-preempt")
    if getattr(args, "victim_command", ""):
        cmd.extend(["--victim-command", args.victim_command])
        if getattr(args, "victim_taskset", ""):
            cmd.extend(["--victim-taskset", args.victim_taskset])
        cmd.extend(["--victim-warmup-sec", str(args.victim_warmup_sec)])
    cmd.append("--")
    cmd.extend(build_mix_cmd(args, variant, trial_dir))
    return cmd


def build_victim_baseline_cmd(args: argparse.Namespace, variant: MatrixVariant, trial_dir: pathlib.Path) -> list[str]:
    """Wrap a baseline mix trial only to run a victim sidecar.

    This deliberately does not attach sched_ext and does not start flowctl.  It
    gives baseline/default variants the same sidecar measurement surface as
    FlowCausal variants when ``--victim-command`` is enabled.
    """

    live_dir = trial_dir / "live-sidecar"
    cmd = [
        sys.executable,
        str(LIVE_SANDBOX),
        "--out-dir",
        str(live_dir),
        "--no-build-tools",
        "--hint-dry-run",
        "--no-controller",
        "--no-flowmon",
        "--no-final-snapshot",
        "--victim-command",
        args.victim_command,
        "--victim-warmup-sec",
        str(args.victim_warmup_sec),
    ]
    if getattr(args, "victim_taskset", ""):
        cmd.extend(["--victim-taskset", args.victim_taskset])
    cmd.append("--")
    cmd.extend(build_mix_cmd(args, variant, trial_dir))
    return cmd


def build_external_scx_cmd(args: argparse.Namespace, variant: MatrixVariant, trial_dir: pathlib.Path) -> list[str]:
    """Wrap one mix trial with an arbitrary external scx scheduler.

    Unlike ``build_flowcausal_cmd()``, this path does not pass FlowCausal-only
    scheduler arguments and does not start flowctl/flowmon.  It is the bridge
    needed for scx_simple/qmap/flatcg/etc. baselines.
    """

    if not variant.scx_scheduler:
        raise SystemExit(f"scx variant {variant.name!r} requires scx_scheduler=/path/to/binary")
    live_dir = trial_dir / "external-scx"
    cmd = [
        sys.executable,
        str(EXTERNAL_SCX_SANDBOX),
        "--out-dir",
        str(live_dir),
        "--scheduler",
        variant.scx_scheduler,
        "--scheduler-sudo",
        args.scx_scheduler_sudo,
        "--scheduler-ready-timeout-sec",
        str(args.scx_scheduler_ready_timeout_sec),
        "--scheduler-startup-grace-sec",
        str(args.scx_scheduler_startup_grace_sec),
    ]
    for scheduler_arg in variant.scx_scheduler_args:
        cmd.append(f"--scheduler-arg={scheduler_arg}")
    if getattr(args, "victim_command", ""):
        cmd.extend(["--victim-command", args.victim_command])
        if getattr(args, "victim_taskset", ""):
            cmd.extend(["--victim-taskset", args.victim_taskset])
        cmd.extend(["--victim-warmup-sec", str(args.victim_warmup_sec)])
    cmd.append("--")
    cmd.extend(build_mix_cmd(args, variant, trial_dir))
    return cmd


def live_wrapper_summary(trial_dir: pathlib.Path, dirname: str) -> dict[str, Any]:
    live_dir = trial_dir / dirname
    manifest = read_json_if_exists(live_dir / "manifest.json")
    return {
        "out_dir": str(live_dir),
        "manifest": manifest,
        "victim_log": str(live_dir / "victim.log"),
        "workload_log": str(live_dir / "workload.log"),
    }


def run_subprocess(cmd: list[str], *, stdout_path: pathlib.Path, stderr_path: pathlib.Path) -> int:
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        proc = subprocess.run(cmd, stdout=stdout, stderr=stderr, text=True, check=False)
    return int(proc.returncode)


def run_pre_trial_cache_cleanup(args: argparse.Namespace) -> dict[str, Any]:
    """Optionally reset host filesystem caches before each variant/trial.

    This is intentionally an experiment-control hook in the matrix layer rather
    than workload logic.  It runs before sched_ext is attached for the trial, so
    scheduler lifecycle measurements are not mixed with cache cleanup time.
    """

    record: dict[str, Any] = {
        "enabled": bool(getattr(args, "drop_caches_before_trial", False)),
        "sudo": getattr(args, "drop_caches_sudo", "auto"),
    }
    if not record["enabled"]:
        return record

    sync_proc = subprocess.run(
        ["sync"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    record["sync_returncode"] = int(sync_proc.returncode)
    if sync_proc.returncode != 0:
        record["sync_stderr"] = sync_proc.stderr.strip()
        raise RuntimeError(f"sync failed before trial: {sync_proc.stderr.strip()}")

    drop_shell = "echo 3 > /proc/sys/vm/drop_caches"
    sudo_mode = str(record["sudo"])
    if os.geteuid() == 0:
        cmd = ["sh", "-c", drop_shell]
        record["method"] = "root"
    elif sudo_mode in {"auto", "always"}:
        cmd = ["sudo", "-n", "sh", "-c", drop_shell]
        record["method"] = "sudo"
    elif sudo_mode == "never":
        raise RuntimeError("--drop-caches-before-trial requires root or sudo unless disabled")
    else:
        raise RuntimeError(f"unsupported --drop-caches-sudo value: {sudo_mode!r}")

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    record["drop_caches_returncode"] = int(proc.returncode)
    if proc.returncode != 0:
        record["drop_caches_stderr"] = proc.stderr.strip()
        raise RuntimeError(f"drop_caches failed before trial: {proc.stderr.strip()}")
    return record


def build_postprocess_specs(
    args: argparse.Namespace,
    *,
    out_dir: pathlib.Path,
    matrix_summary: pathlib.Path,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if getattr(args, "system_profile", False):
        output = out_dir / "system-profile-matrix-summary.json"
        markdown = out_dir / "system-profile-matrix-summary.md"
        csv_path = out_dir / "system-profile-matrix-summary.csv"
        specs.append(
            {
                "name": "system_profile_matrix_summary",
                "output": str(output),
                "markdown": str(markdown),
                "csv": str(csv_path),
                "stdout": str(out_dir / "system-profile-matrix-summary.stdout.log"),
                "stderr": str(out_dir / "system-profile-matrix-summary.stderr.log"),
                "cmd": [
                    sys.executable,
                    str(PROFILE_MATRIX_SUMMARY),
                    "--matrix-summary",
                    str(matrix_summary),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--csv-output",
                    str(csv_path),
                ],
            }
        )
    if getattr(args, "victim_command", ""):
        output = out_dir / "victim-latency-matrix-summary.json"
        markdown = out_dir / "victim-latency-matrix-summary.md"
        csv_path = out_dir / "victim-latency-matrix-summary.csv"
        specs.append(
            {
                "name": "victim_latency_matrix_summary",
                "output": str(output),
                "markdown": str(markdown),
                "csv": str(csv_path),
                "stdout": str(out_dir / "victim-latency-matrix-summary.stdout.log"),
                "stderr": str(out_dir / "victim-latency-matrix-summary.stderr.log"),
                "cmd": [
                    sys.executable,
                    str(VICTIM_MATRIX_SUMMARY),
                    "--matrix-summary",
                    str(matrix_summary),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                    "--csv-output",
                    str(csv_path),
                ],
            }
        )
    return specs


def run_postprocess_specs(specs: list[dict[str, Any]]) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for spec in specs:
        name = str(spec["name"])
        rc = run_subprocess(
            list(spec["cmd"]),
            stdout_path=pathlib.Path(str(spec["stdout"])),
            stderr_path=pathlib.Path(str(spec["stderr"])),
        )
        parsed = read_json_if_exists(pathlib.Path(str(spec["output"])))
        results[name] = {
            "cmd": spec["cmd"],
            "returncode": rc,
            "status": parsed.get("status", "missing-output"),
            "output": spec["output"],
            "markdown": spec["markdown"],
            "csv": spec["csv"],
            "stdout": spec["stdout"],
            "stderr": spec["stderr"],
        }
    return results


def run_fidelity_audit(trial_dir: pathlib.Path) -> tuple[int, dict[str, Any]]:
    summary = trial_dir / "harness-replay-mix-summary.json"
    output = trial_dir / "harness-replay-fidelity-audit.json"
    markdown = trial_dir / "harness-replay-fidelity-audit.md"
    cmd = [
        sys.executable,
        str(FIDELITY_AUDIT),
        "--mix-summary",
        str(summary),
        "--output",
        str(output),
        "--markdown-output",
        str(markdown),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if not output.exists():
        return int(proc.returncode or 1), {
            "status": "fail",
            "blockers": [proc.stderr.strip() or "fidelity audit did not produce output"],
        }
    data = json.loads(output.read_text(encoding="utf-8"))
    return int(proc.returncode), data


def read_json_if_exists(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"parse_error": f"invalid JSON in {path}"}
    return data if isinstance(data, dict) else {"parse_error": f"expected object in {path}"}


def flowcausal_live_summary(trial_dir: pathlib.Path) -> dict[str, Any]:
    live_dir = trial_dir / "flowcausal-live"
    manifest = read_json_if_exists(live_dir / "manifest.json")
    return {
        "out_dir": str(live_dir),
        "manifest": manifest,
        "scheduler_log": str(live_dir / "scheduler.log"),
        "controller_log": str(live_dir / "controller.log"),
        "controller_ndjson": str(live_dir / "controller.ndjson"),
        "flowmon_log": str(live_dir / "flowmon.log"),
        "flowmon_final_log": str(live_dir / "flowmon-final.log"),
        "workload_log": str(live_dir / "workload.log"),
    }


def write_csv(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "variant",
        "kind",
        "taskset",
        "nice",
        "cgroup_cpu_weight",
        "cgroup_cpu_max",
        "trial",
        "status",
        "fidelity_status",
        "makespan_sec",
        "mean_critical_sec",
        "p95_critical_sec",
        "failed_stage_commands",
        "elapsed_jain_fairness",
        "critical_jain_fairness",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            aggregate = row.get("aggregate", {})
            nice_value = row.get("nice", "")
            cgroup_weight = row.get("cgroup_cpu_weight", "")
            writer.writerow(
                {
                    "variant": row.get("variant", ""),
                    "kind": row.get("kind", ""),
                    "taskset": row.get("taskset", ""),
                    "nice": "" if nice_value is None else nice_value,
                    "cgroup_cpu_weight": "" if cgroup_weight is None else cgroup_weight,
                    "cgroup_cpu_max": row.get("cgroup_cpu_max", ""),
                    "trial": row.get("trial", ""),
                    "status": row.get("status", ""),
                    "fidelity_status": row.get("fidelity", {}).get("status", ""),
                    "makespan_sec": row.get("makespan_sec"),
                    "mean_critical_sec": aggregate.get("mean_critical_sec"),
                    "p95_critical_sec": aggregate.get("p95_critical_sec"),
                    "failed_stage_commands": aggregate.get("failed_stage_commands"),
                    "elapsed_jain_fairness": aggregate.get("elapsed_jain_fairness"),
                    "critical_jain_fairness": aggregate.get("critical_jain_fairness"),
                }
            )


def write_markdown(path: pathlib.Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Harness replay matrix summary",
        "",
        f"- status: `{payload['status']}`",
        f"- variants: {len(payload['variants'])}",
        f"- trials_per_variant: {payload['trials_per_variant']}",
        "",
        "| variant | kind | taskset | nice | cgroup weight | cgroup max | pass/trials | makespan mean | mean critical | p95 critical | failed cmds | elapsed Jain | critical Jain |",
        "|---|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant in payload["variants"]:
        summary = variant["summary"]
        nice_value = variant.get("nice", "")
        nice_text = "" if nice_value is None else str(nice_value)
        cgroup_weight = variant.get("cgroup_cpu_weight", "")
        cgroup_weight_text = "" if cgroup_weight is None else str(cgroup_weight)
        lines.append(
            f"| `{variant['name']}` | `{variant.get('kind', '')}` | `{variant.get('taskset', '')}` | "
            f"{nice_text} | "
            f"{cgroup_weight_text} | "
            f"{variant.get('cgroup_cpu_max', '')} | "
            f"{summary.get('pass_trials', 0)}/{summary.get('trials', 0)} | "
            f"{summary.get('makespan_mean_sec', '')} | "
            f"{summary.get('mean_critical_mean_sec', '')} | "
            f"{summary.get('p95_critical_mean_sec', '')} | "
            f"{summary.get('failed_stage_commands_mean', '')} | "
            f"{summary.get('elapsed_jain_fairness_mean', '')} | "
            f"{summary.get('critical_jain_fairness_mean', '')} |"
        )
    lines.append("")
    lines.extend(
        [
            "## Relative to baseline",
            "",
            "| variant | baseline | makespan delta | mean critical delta | p95 critical delta |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for variant in payload["variants"]:
        comp = variant.get("comparison_to_baseline", {})
        lines.append(
            f"| `{variant['name']}` | `{comp.get('baseline', '')}` | "
            f"{comp.get('makespan_delta', '')} | "
            f"{comp.get('mean_critical_delta', '')} | "
            f"{comp.get('p95_critical_delta', '')} |"
        )
    postprocess = payload.get("postprocess")
    if isinstance(postprocess, dict) and postprocess:
        lines.extend(
            [
                "",
                "## Postprocess artifacts",
                "",
                "| artifact | status | output |",
                "|---|---|---|",
            ]
        )
        for name, row in postprocess.items():
            if isinstance(row, dict):
                lines.append(
                    f"| `{name}` | `{row.get('status', '')}` | `{row.get('output', '')}` |"
                )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run HarnessReplayBench mix cases across named baseline variants",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--case", action="append", default=[], help="forwarded to run_harness_replay_mix.py")
    parser.add_argument("--manifest", help="forwarded to run_harness_replay_mix.py")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        help=(
            "variant name or name:KEY=VALUE[,KEY=VALUE...]; keys include "
            "taskset, nice, cpu_weight/cgroup_cpu_weight, cpu_max/cgroup_cpu_max, "
            "kind=baseline|flowcausal|scx, scx_scheduler, scx_args, "
            "per_case_flowctl, width_control, "
            "frontier_width, max_frontier_width, "
            "frontier_propagation, stage_aware_frontier_propagation_depth, "
            "background_dsq, background_dispatch_quota, no_controller, "
            "shared_local_dsq, tracked_local_dsq, shared_percpu_dsq, "
            "frontier_fallback_local_dsq, frontier_dispatch_quota, root_flowd, "
            "flowd_affinity_release_after_first_fork"
        ),
    )
    parser.add_argument("--copy-keep-git", action="store_true")
    parser.add_argument(
        "--copy-exclude",
        action="append",
        default=[],
        help="extra copy ignore pattern forwarded to run_harness_replay_mix.py; repeatable",
    )
    parser.add_argument(
        "--system-profile",
        action="store_true",
        help="sample each replay case's host-visible process tree and schedstat while it runs",
    )
    parser.add_argument(
        "--system-profile-interval-ms",
        type=int,
        default=25,
        help="sampling interval forwarded to run_harness_replay_mix.py when --system-profile is enabled",
    )
    parser.add_argument(
        "--keep-success-artifacts",
        action="store_true",
        help="keep successful case work/stage-output artifacts instead of compacting them",
    )
    parser.add_argument(
        "--cleanup-artifact",
        action="append",
        default=[],
        help="extra/replacement artifact child name forwarded to run_harness_replay_mix.py",
    )
    sudo_cleanup = parser.add_mutually_exclusive_group()
    sudo_cleanup.add_argument(
        "--cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_true",
        default=True,
        help="forward sudo-assisted successful-artifact cleanup to run_harness_replay_mix.py",
    )
    sudo_cleanup.add_argument(
        "--no-cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_false",
        help="disable sudo-assisted successful-artifact cleanup",
    )
    parser.add_argument("--taskset", default="", help="default cpuset applied to every variant unless overridden")
    parser.add_argument("--stagger-ms", type=int, default=0)
    parser.add_argument("--allow-failure-stage", action="append", default=["RETRY_PROBE"])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--host-env", action="append", default=[])
    parser.add_argument("--container-env", action="append", default=[])
    parser.add_argument("--container-python", default="/opt/miniconda3/envs/testbed/bin/python")
    parser.add_argument(
        "--container-path-env",
        default="/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )
    parser.add_argument("--docker-user", default="current")
    parser.add_argument("--cgroup-wrapper", default=str(HERE / "run_static_cgroup.py"))
    parser.add_argument("--cgroup-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--scx-scheduler-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--scx-scheduler-ready-timeout-sec", type=float, default=3.0)
    parser.add_argument("--scx-scheduler-startup-grace-sec", type=float, default=0.1)
    parser.add_argument("--flowcausal-pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--flowcausal-scheduler", default="/tmp/scx_flowcausal")
    parser.add_argument("--flowcausal-flowmon", default="/tmp/flowmon")
    parser.add_argument("--flowcausal-flowctl", default=str(HERE.parent / "src" / "controller" / "flowctl.py"))
    parser.add_argument("--flowcausal-flowctl-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--flowcausal-flowd", default="/tmp/flowd")
    parser.add_argument("--flowcausal-flowd-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--flowcausal-flowd-events", action="store_true")
    parser.add_argument("--flowcausal-stage-kind", default="AUTO")
    parser.add_argument("--flowcausal-frontier-width", type=int, default=1)
    parser.add_argument("--flowcausal-max-frontier-width", type=int, default=1)
    parser.add_argument("--flowcausal-min-frontier-score", type=int, default=50)
    parser.add_argument("--flowcausal-confidence", type=int, default=75)
    parser.add_argument("--flowcausal-ttl-ms", type=int, default=1000)
    parser.add_argument("--flowcausal-policy-window-ms", type=int, default=100)
    parser.add_argument("--flowcausal-boost-budget-ms", type=int, default=10)
    parser.add_argument("--flowcausal-max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--flowcausal-interval-ms", type=int, default=100)
    parser.add_argument(
        "--flowcausal-build-frontier-admission",
        choices=("advance", "tail-only", "observe"),
        default="advance",
    )
    parser.add_argument(
        "--flowcausal-frontier-stability-action",
        choices=("suppress", "observe"),
        default="suppress",
    )
    parser.add_argument("--flowcausal-frontier-stability-samples", type=int, default=1)
    parser.add_argument("--flowcausal-frontier-hold-ms", type=int, default=0)
    parser.add_argument("--flowcausal-frontier-bypass-score", type=int, default=140)
    parser.add_argument("--flowcausal-frontier-selected-stability", action="store_true")
    parser.add_argument("--flowcausal-frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--flowcausal-policy-fallback", action="store_true")
    parser.add_argument(
        "--flowcausal-flowctl-taskset",
        default="",
        help="optional CPU list forwarded to per-case flowctl controllers",
    )
    parser.add_argument(
        "--flowcausal-flowctl-nice",
        type=int,
        default=0,
        help="optional nice value forwarded to per-case flowctl controllers",
    )
    parser.add_argument("--flowcausal-frontier-dispatch-quota", type=int, default=4)
    parser.add_argument("--flowcausal-guard-cooldown-dispatches", type=int, default=0)
    parser.add_argument("--flowcausal-pressure-fallback-enqueues", type=int, default=0)
    parser.add_argument("--flowcausal-pressure-fallback-ms", type=int, default=0)
    parser.add_argument("--flowcausal-shared-wait-pressure-us", type=int, default=0)
    parser.add_argument("--flowcausal-pressure-dispatch-ms", type=int, default=0)
    parser.add_argument("--flowcausal-frontier-dispatch-window-ms", type=int, default=0)
    parser.add_argument("--flowcausal-frontier-dispatch-window-budget", type=int, default=0)
    parser.add_argument("--flowcausal-shared-slice-us", type=int, default=0)
    parser.add_argument("--flowcausal-shared-wakeup-slice-us", type=int, default=0)
    parser.add_argument("--flowcausal-shared-wakeup-min-block-us", type=int, default=0)
    parser.add_argument(
        "--flowcausal-no-flowmon",
        action="store_true",
        help="disable FlowCausal flowmon sidecar and final snapshot for pure scheduler-overhead runs",
    )
    parser.add_argument("--flowcausal-no-event-flowmon", action="store_true")
    parser.add_argument("--flowcausal-no-final-snapshot", action="store_true")
    parser.add_argument("--flowcausal-collect-characterization", action="store_true")
    parser.add_argument("--flowcausal-characterization-interval-ms", type=int, default=50)
    parser.add_argument("--flowcausal-characterization-top-candidates", type=int, default=6)
    parser.add_argument("--flowcausal-shared-wakeup-preempt", action="store_true")
    parser.add_argument("--flowcausal-frontier-wakeup-preempt", action="store_true")
    parser.add_argument(
        "--victim-command",
        default="",
        help=(
            "optional shell-style victim sidecar passed to run_live_sandbox.py for "
            "flowcausal variants; use with --victim-taskset to measure co-runner safety"
        ),
    )
    parser.add_argument(
        "--victim-taskset",
        default="",
        help="optional CPU list for the victim sidecar command in flowcausal variants",
    )
    parser.add_argument(
        "--victim-warmup-sec",
        type=float,
        default=0.2,
        help="delay after starting the victim sidecar before the replay workload starts",
    )
    parser.add_argument(
        "--interleave-trials",
        action="store_true",
        help=(
            "run trial-1 for all variants before trial-2, reducing variant-order "
            "bias from filesystem/page-cache warmup and background noise"
        ),
    )
    parser.add_argument(
        "--drop-caches-before-trial",
        action="store_true",
        help="run sync and drop host page caches before every variant/trial",
    )
    parser.add_argument(
        "--drop-caches-sudo",
        choices=("auto", "always", "never"),
        default="auto",
        help="sudo policy used by --drop-caches-before-trial when not running as root",
    )
    return parser.parse_args()


def run_variant_trial(
    args: argparse.Namespace,
    *,
    out_dir: pathlib.Path,
    variant: MatrixVariant,
    trial: int,
) -> dict[str, Any]:
    trial_dir = out_dir / "runs" / safe_name(variant.name) / f"trial-{trial:02d}"
    trial_dir.mkdir(parents=True, exist_ok=True)
    cache_control = run_pre_trial_cache_cleanup(args)
    if variant.kind == "flowcausal":
        cmd = build_flowcausal_cmd(args, variant, trial_dir)
    elif variant.kind == "scx":
        cmd = build_external_scx_cmd(args, variant, trial_dir)
    elif getattr(args, "victim_command", ""):
        cmd = build_victim_baseline_cmd(args, variant, trial_dir)
    else:
        cmd = build_mix_cmd(args, variant, trial_dir)
    rc = run_subprocess(
        cmd,
        stdout_path=trial_dir / "matrix-run.stdout.log",
        stderr_path=trial_dir / "matrix-run.stderr.log",
    )
    summary_path = trial_dir / "harness-replay-mix-summary.json"
    if summary_path.exists():
        summary_text = summary_path.read_text(encoding="utf-8")
        if summary_text.strip():
            try:
                trial_payload = json.loads(summary_text)
            except json.JSONDecodeError as exc:
                trial_payload = {
                    "status": "fail",
                    "aggregate": {},
                    "blockers": [f"invalid mix summary json: {exc}"],
                }
        else:
            trial_payload = {"status": "fail", "aggregate": {}, "blockers": ["empty mix summary"]}
    else:
        summary_text = ""
        trial_payload = {"status": "fail", "aggregate": {}, "blockers": ["missing mix summary"]}
    audit_rc, audit_payload = (
        run_fidelity_audit(trial_dir) if summary_path.exists() and summary_text.strip() else (1, {"status": "fail"})
    )
    trial_payload.update(
        {
            "variant": variant.name,
            "kind": variant.kind,
            "taskset": variant_taskset(args, variant),
            "nice": variant.nice,
            "cgroup_cpu_weight": variant.cgroup_cpu_weight,
            "cgroup_cpu_max": variant.cgroup_cpu_max,
            "flowcausal_scheduler": variant.flowcausal_scheduler,
            "flowd_control_plane": variant.flowd_control_plane,
            "flowd_assist_bpf_auto": variant.flowd_assist_bpf_auto,
            "flowd_cxx_only_session_policy": variant.flowd_cxx_only_session_policy,
            "flowd_affinity_by_session": variant.flowd_affinity_by_session,
            "flowd_python_affinity_by_session": variant.flowd_python_affinity_by_session,
            "flowd_affinity_release_after_first_fork": variant.flowd_affinity_release_after_first_fork,
            "flowd_affinity_release_package_only": variant.flowd_affinity_release_package_only,
            "flowd_stage_state": variant.flowd_stage_state,
            "flowd_stage_state_poll_ms": variant.flowd_stage_state_poll_ms,
            "scx_scheduler": variant.scx_scheduler,
            "scx_scheduler_args": list(variant.scx_scheduler_args),
            "variant_options": dict(variant.raw_options),
            "trial": trial,
            "run_dir": str(trial_dir),
            "matrix_command": cmd,
            "mix_returncode": rc,
            "fidelity_returncode": audit_rc,
            "fidelity": {
                "status": audit_payload.get("status", "fail"),
                "output": str(trial_dir / "harness-replay-fidelity-audit.json"),
            },
            "cache_control": cache_control,
        }
    )
    if variant.kind == "flowcausal":
        trial_payload["flowcausal_live"] = flowcausal_live_summary(trial_dir)
        trial_payload["live_wrapper"] = live_wrapper_summary(trial_dir, "flowcausal-live")
        trial_payload["flowmon"] = parse_flowmon(trial_dir / "flowcausal-live" / "flowmon-final.log")
    elif variant.kind == "scx":
        trial_payload["external_scx"] = live_wrapper_summary(trial_dir, "external-scx")
        trial_payload["live_wrapper"] = live_wrapper_summary(trial_dir, "external-scx")
    elif getattr(args, "victim_command", ""):
        trial_payload["live_wrapper"] = live_wrapper_summary(trial_dir, "live-sidecar")
    return trial_payload


def main() -> int:
    args = parse_args()
    if not args.case and not args.manifest:
        raise SystemExit("at least one --case or --manifest is required")
    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        raise SystemExit(f"{out_dir} exists and is non-empty; pass --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    variant_specs = expand_variant_specs(args.variant) or ["default", "cpuset_0_1:taskset=0-1"]
    variants = [parse_variant(spec) for spec in variant_specs]
    trial_rows: list[dict[str, Any]] = []
    variant_trials_by_name: dict[str, list[dict[str, Any]]] = {variant.name: [] for variant in variants}
    status = "pass"

    if args.interleave_trials:
        run_order = [
            (variant, trial)
            for trial in range(1, args.trials + 1)
            for variant in variants
        ]
    else:
        run_order = [
            (variant, trial)
            for variant in variants
            for trial in range(1, args.trials + 1)
        ]

    for variant, trial in run_order:
        trial_payload = run_variant_trial(args, out_dir=out_dir, variant=variant, trial=trial)
        if (
            trial_payload.get("mix_returncode") != 0
            or trial_payload.get("fidelity_returncode") != 0
            or trial_payload.get("status") != "pass"
        ):
            status = "fail"
        variant_trials_by_name[variant.name].append(trial_payload)
        trial_rows.append(trial_payload)

    variant_payloads: list[dict[str, Any]] = []
    for variant in variants:
        variant_trials = sorted(variant_trials_by_name[variant.name], key=lambda row: int(row.get("trial", 0)))
        variant_payloads.append(
            {
                "name": variant.name,
                "kind": variant.kind,
                "taskset": variant_taskset(args, variant),
                "nice": variant.nice,
                "cgroup_cpu_weight": variant.cgroup_cpu_weight,
                "cgroup_cpu_max": variant.cgroup_cpu_max,
                "scx_scheduler": variant.scx_scheduler,
                "scx_scheduler_args": list(variant.scx_scheduler_args),
                "variant_options": dict(variant.raw_options),
                "summary": variant_summary(variant_trials),
                "trials": variant_trials,
            }
        )
    add_baseline_comparisons(variant_payloads)

    payload = {
        "schema": SCHEMA,
        "status": status,
        "out_dir": str(out_dir),
        "trials_per_variant": args.trials,
        "victim": victim_config(args),
        "experiment_control": {
            "copy_keep_git": bool(args.copy_keep_git),
            "cleanup_success_artifacts": not bool(args.keep_success_artifacts),
            "cleanup_artifact": list(getattr(args, "cleanup_artifact", [])),
            "cleanup_use_sudo": bool(getattr(args, "cleanup_use_sudo", True)),
            "taskset": str(getattr(args, "taskset", "") or ""),
            "interleave_trials": bool(args.interleave_trials),
            "drop_caches_before_trial": bool(getattr(args, "drop_caches_before_trial", False)),
            "drop_caches_sudo": str(getattr(args, "drop_caches_sudo", "auto")),
        },
        "variants": variant_payloads,
    }
    summary_json = out_dir / "harness-replay-matrix-summary.json"
    summary_csv = out_dir / "harness-replay-matrix-summary.csv"
    summary_md = out_dir / "harness-replay-matrix-summary.md"
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_csv(summary_csv, trial_rows)
    write_markdown(summary_md, payload)
    postprocess = run_postprocess_specs(
        build_postprocess_specs(args, out_dir=out_dir, matrix_summary=summary_json)
    )
    if postprocess:
        payload["postprocess"] = postprocess
        if any(row.get("returncode") not in (0, None) for row in postprocess.values()):
            status = "fail"
            payload["status"] = status
        summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_markdown(summary_md, payload)
    print(json.dumps({"status": status, "summary_json": str(summary_json)}, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

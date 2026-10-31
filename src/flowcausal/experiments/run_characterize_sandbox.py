#!/usr/bin/env python3
"""Characterize a sandbox/code-agent workflow before enabling FlowCausal.

The paper needs a Go/No-Go gate before making any scheduling claim: does the
workflow actually expose a CPU-side critical frontier, and can ordinary
process-tree evidence identify it?  This harness runs an arbitrary command,
samples its process subtree through /proc, reuses flowctl's online frontier
ranking logic, and records lower-bound scheduling deltas from
/proc/<pid>/schedstat when available.

It does not attach sched_ext, does not write BPF maps, and must not be cited as
a FlowCausal speedup result.  Its output is workload characterization evidence:
stage, process width, ranked frontier candidates, parent-wait/barrier-tail
signals, and sampled run-delay/runtime deltas.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
FLOWCTL_PATH = ROOT / "src" / "controller" / "flowctl.py"


def load_flowctl() -> Any:
    spec = importlib.util.spec_from_file_location("flowctl_characterization", FLOWCTL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {FLOWCTL_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


flowctl = load_flowctl()


@dataclass(frozen=True)
class SchedSample:
    runtime_ns: int = 0
    wait_ns: int = 0
    slices: int = 0


@dataclass(frozen=True)
class PidSample:
    cpu_ticks: int
    sched: SchedSample


def read_schedstat(pid: int) -> SchedSample:
    """Read /proc/<pid>/schedstat.

    Linux reports three integers: runtime ns, runqueue wait ns, and timeslices.
    Some kernels disable schedstats or expose zeros.  The harness keeps the raw
    values and lets the summary say whether nonzero schedstat evidence exists.
    """

    try:
        fields = pathlib.Path(f"/proc/{pid}/schedstat").read_text().split()
    except OSError:
        return SchedSample()
    if len(fields) < 3:
        return SchedSample()
    try:
        return SchedSample(int(fields[0]), int(fields[1]), int(fields[2]))
    except ValueError:
        return SchedSample()


def positive_delta(current: int, previous: int) -> int:
    if current < previous:
        return 0
    return current - previous


def command_text(cmd: list[str]) -> str:
    return " ".join(subprocess.list2cmdline([part]) for part in cmd)


def start_workload(
    cmd: list[str],
    log_path: pathlib.Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    log = log_path.open("w", encoding="utf-8")
    print("+ " + command_text(cmd), flush=True)
    return subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
        env=env,
    )


def stop_process_group(proc: subprocess.Popen[str] | None, timeout_sec: float = 5.0) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return proc.poll()
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                return proc.poll()
        try:
            return proc.wait(timeout=timeout_sec)
        except subprocess.TimeoutExpired:
            continue
    return proc.wait()


def parse_args() -> argparse.Namespace:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    parser = argparse.ArgumentParser(
        description="Characterize a sandbox workflow's process frontier without attaching FlowCausal",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default=f"experiments/results/characterize-sandbox-{stamp}")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=50)
    parser.add_argument("--frontier-width", type=int, default=2)
    parser.add_argument("--adaptive-frontier-width", action="store_true")
    parser.add_argument("--adaptive-max-frontier-width", type=int, default=8)
    parser.add_argument("--top-candidates", type=int, default=6)
    parser.add_argument("--min-frontier-score", type=int, default=50)
    parser.add_argument("--stage-kind", default="AUTO")
    parser.add_argument(
        "--stage-state-file",
        default=None,
        help="optional workload-emitted live stage JSON file used before command inference",
    )
    parser.add_argument("--taskset", default=None, help="optional CPU list for the workload command")
    parser.add_argument("--timeout-sec", type=float, default=0.0, help="kill workload after this many seconds; 0 disables")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="workload command after --")
    return parser.parse_args()


def selected_frontiers(
    ranked: list[tuple[int, int, str]],
    *,
    width: int,
    min_score: int,
) -> set[int]:
    return {
        pid
        for score, pid, _reason in ranked[: max(1, width)]
        if score >= max(0, min_score)
    }


def sample_tree(
    root_pid: int,
    *,
    forced_stage: Any,
    stage_state_file: pathlib.Path | None = None,
    previous: dict[int, PidSample],
    sample_index: int,
    start_ns: int,
    width: int,
    adaptive_width: bool,
    adaptive_max_width: int,
    min_score: int,
    top_candidates: int,
) -> tuple[dict[str, Any], dict[int, PidSample]]:
    pids = flowctl.subtree(root_pid)
    infos = flowctl.collect_proc_info(pids)
    now_ns = time.time_ns()
    stage, stage_source, stage_state = flowctl.resolve_live_stage(
        infos.values(),
        forced_stage=forced_stage,
        stage_state_file=stage_state_file,
    )
    ranked = flowctl.rank_frontier_candidates(infos.values(), stage)
    width_decision = flowctl.decide_adaptive_frontier_width(
        infos.values(),
        ranked,
        requested_frontier_width=width,
        requested_max_frontier_width=width,
        min_frontier_score=min_score,
        adaptive_frontier_width=adaptive_width,
        adaptive_max_frontier_width=adaptive_max_width,
    )
    frontiers = selected_frontiers(
        ranked,
        width=width_decision.frontier_width,
        min_score=min_score,
    )

    current: dict[int, PidSample] = {}
    per_pid_delta: dict[int, dict[str, int]] = {}
    totals = Counter()
    state_counts = Counter(info.state for info in infos.values())
    command_counts = Counter(info.comm for info in infos.values() if info.comm)

    for pid, info in infos.items():
        sched = read_schedstat(pid)
        current[pid] = PidSample(cpu_ticks=info.cpu_ticks, sched=sched)
        old = previous.get(pid)
        if old is None:
            delta = {
                "cpu_ticks": 0,
                "sched_runtime_ns": 0,
                "sched_wait_ns": 0,
                "sched_slices": 0,
            }
        else:
            delta = {
                "cpu_ticks": positive_delta(info.cpu_ticks, old.cpu_ticks),
                "sched_runtime_ns": positive_delta(sched.runtime_ns, old.sched.runtime_ns),
                "sched_wait_ns": positive_delta(sched.wait_ns, old.sched.wait_ns),
                "sched_slices": positive_delta(sched.slices, old.sched.slices),
            }
        per_pid_delta[pid] = delta
        totals["cpu_ticks_delta"] += delta["cpu_ticks"]
        totals["sched_runtime_delta_ns"] += delta["sched_runtime_ns"]
        totals["sched_wait_delta_ns"] += delta["sched_wait_ns"]
        totals["sched_slices_delta"] += delta["sched_slices"]
        if pid in frontiers:
            totals["frontier_cpu_ticks_delta"] += delta["cpu_ticks"]
            totals["frontier_sched_runtime_delta_ns"] += delta["sched_runtime_ns"]
            totals["frontier_sched_wait_delta_ns"] += delta["sched_wait_ns"]
            totals["frontier_sched_slices_delta"] += delta["sched_slices"]

    candidate_rows: list[dict[str, Any]] = []
    for score, pid, reason in ranked[: max(1, top_candidates)]:
        info = infos.get(pid)
        if info is None:
            continue
        delta = per_pid_delta.get(pid, {})
        candidate_rows.append(
            {
                "pid": pid,
                "ppid": info.ppid,
                "comm": info.comm,
                "state": info.state,
                "leaf": info.leaf,
                "child_count": info.child_count,
                "score": score,
                "reason": reason,
                "selected_frontier": pid in frontiers,
                "cpu_ticks_delta": delta.get("cpu_ticks", 0),
                "sched_runtime_delta_ns": delta.get("sched_runtime_ns", 0),
                "sched_wait_delta_ns": delta.get("sched_wait_ns", 0),
                "sched_slices_delta": delta.get("sched_slices", 0),
            }
        )

    record = {
        "type": "characterization_sample",
        "sample": sample_index,
        "time_ns": now_ns,
        "elapsed_ms": (now_ns - start_ns) / 1_000_000.0,
        "root_pid": root_pid,
        "stage": stage.name,
        "stage_source": stage_source,
        "stage_label": str(stage_state.get("stage") or stage.name) if isinstance(stage_state, dict) else stage.name,
        "process_count": len(infos),
        "leaf_count": sum(1 for info in infos.values() if info.leaf),
        "state_counts": dict(sorted(state_counts.items())),
        "top_commands": command_counts.most_common(8),
        "selected_frontiers": sorted(frontiers),
        "adaptive_width": width_decision.__dict__,
        "frontier_candidates": candidate_rows,
        "aggregate_delta": dict(totals),
        "signals": {
            "has_parent_wait": any("parent-wait" in reason for _score, _pid, reason in ranked),
            "has_barrier_tail": any("barrier-tail" in reason for _score, _pid, reason in ranked),
            "ranked_candidates": len(ranked),
            "schedstat_nonzero": totals["sched_runtime_delta_ns"] > 0 or totals["sched_wait_delta_ns"] > 0,
        },
    }
    return record, current


def write_summary(
    out_dir: pathlib.Path,
    *,
    manifest: dict[str, Any],
    records: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = Counter()
    stage_counts = Counter()
    stage_totals: dict[str, Counter[str]] = {}
    stage_frontier_samples = Counter()
    stage_parent_wait_samples = Counter()
    stage_barrier_tail_samples = Counter()
    stage_schedstat_samples = Counter()
    command_counts = Counter()
    max_process_count = 0
    max_leaf_count = 0
    samples_with_frontier = 0
    samples_with_parent_wait = 0
    samples_with_barrier_tail = 0
    samples_with_schedstat = 0

    for record in records:
        stage = str(record.get("stage", "UNKNOWN"))
        stage_counts[stage] += 1
        stage_totals.setdefault(stage, Counter())
        max_process_count = max(max_process_count, int(record.get("process_count", 0)))
        max_leaf_count = max(max_leaf_count, int(record.get("leaf_count", 0)))
        if record.get("selected_frontiers"):
            samples_with_frontier += 1
            stage_frontier_samples[stage] += 1
        signals = record.get("signals", {})
        if signals.get("has_parent_wait"):
            samples_with_parent_wait += 1
            stage_parent_wait_samples[stage] += 1
        if signals.get("has_barrier_tail"):
            samples_with_barrier_tail += 1
            stage_barrier_tail_samples[stage] += 1
        if signals.get("schedstat_nonzero"):
            samples_with_schedstat += 1
            stage_schedstat_samples[stage] += 1
        for key, value in record.get("aggregate_delta", {}).items():
            intval = int(value)
            totals[key] += intval
            stage_totals[stage][key] += intval
        for comm, count in record.get("top_commands", []):
            command_counts[str(comm)] += int(count)

    total_wait = totals["sched_wait_delta_ns"]
    total_runtime = totals["sched_runtime_delta_ns"]
    frontier_wait = totals["frontier_sched_wait_delta_ns"]
    frontier_runtime = totals["frontier_sched_runtime_delta_ns"]
    sampled_elapsed_sec = 0.0
    if records:
        sampled_elapsed_sec = max(float(record.get("elapsed_ms", 0.0)) for record in records) / 1000.0
    schedstat_observed = samples_with_schedstat > 0 and (total_wait > 0 or total_runtime > 0)
    frontier_observed = samples_with_frontier > 0
    if frontier_observed and schedstat_observed:
        characterization_level = "C1: frontier candidates with sampled schedstat deltas"
    elif frontier_observed:
        characterization_level = "C0.5: frontier candidates without nonzero schedstat deltas"
    else:
        characterization_level = "C0: process-tree trace only"

    stage_delta_summary: dict[str, dict[str, Any]] = {}
    for stage, count in sorted(stage_counts.items()):
        local_totals = stage_totals.get(stage, Counter())
        local_wait = local_totals["sched_wait_delta_ns"]
        local_runtime = local_totals["sched_runtime_delta_ns"]
        local_frontier_wait = local_totals["frontier_sched_wait_delta_ns"]
        local_frontier_runtime = local_totals["frontier_sched_runtime_delta_ns"]
        local_frontier_samples = stage_frontier_samples[stage]
        local_schedstat_samples = stage_schedstat_samples[stage]
        stage_delta_summary[stage] = {
            "sample_count": count,
            "samples_with_frontier": local_frontier_samples,
            "samples_with_parent_wait": stage_parent_wait_samples[stage],
            "samples_with_barrier_tail": stage_barrier_tail_samples[stage],
            "samples_with_schedstat": local_schedstat_samples,
            "aggregate_delta": dict(local_totals),
            "frontier_wait_share": (
                local_frontier_wait / local_wait if local_wait else None
            ),
            "frontier_runtime_share": (
                local_frontier_runtime / local_runtime if local_runtime else None
            ),
            "frontier_wait_per_frontier_sample_ns": (
                local_frontier_wait / local_frontier_samples
                if local_frontier_samples
                else None
            ),
            "frontier_wait_per_schedstat_sample_ns": (
                local_frontier_wait / local_schedstat_samples
                if local_schedstat_samples
                else None
            ),
            "frontier_runtime_per_frontier_sample_ns": (
                local_frontier_runtime / local_frontier_samples
                if local_frontier_samples
                else None
            ),
        }

    summary = {
        "characterization_level": characterization_level,
        "sample_count": len(records),
        "samples_with_frontier": samples_with_frontier,
        "samples_with_parent_wait": samples_with_parent_wait,
        "samples_with_barrier_tail": samples_with_barrier_tail,
        "samples_with_schedstat": samples_with_schedstat,
        "stage_sample_counts": dict(sorted(stage_counts.items())),
        "stage_delta_summary": stage_delta_summary,
        "max_process_count": max_process_count,
        "max_leaf_count": max_leaf_count,
        "top_commands": command_counts.most_common(12),
        "aggregate_delta": dict(totals),
        "frontier_wait_share": (frontier_wait / total_wait) if total_wait else None,
        "frontier_runtime_share": (frontier_runtime / total_runtime) if total_runtime else None,
        "sampled_elapsed_sec": sampled_elapsed_sec,
        "frontier_wait_per_frontier_sample_ns": (
            frontier_wait / samples_with_frontier if samples_with_frontier else None
        ),
        "frontier_wait_per_schedstat_sample_ns": (
            frontier_wait / samples_with_schedstat if samples_with_schedstat else None
        ),
        "frontier_wait_per_elapsed_sec": (
            frontier_wait / sampled_elapsed_sec if sampled_elapsed_sec > 0.0 else None
        ),
        "frontier_runtime_per_frontier_sample_ns": (
            frontier_runtime / samples_with_frontier if samples_with_frontier else None
        ),
        "frontier_runtime_per_elapsed_sec": (
            frontier_runtime / sampled_elapsed_sec if sampled_elapsed_sec > 0.0 else None
        ),
        "claim_discipline": {
            "permitted": [
                "the workload exposes process-tree frontier candidates under flowctl's current scorer",
                "sampled /proc schedstat deltas provide a lower-bound CPU wait/runtime characterization when nonzero",
                "parent-wait and barrier-tail reasons can be audited from characterization.ndjson",
            ],
            "forbidden": [
                "FlowCausal improves this workload",
                "frontier inference is accurate",
                "frontier wait explains stage time without manual trace validation",
                "victim slowdown is bounded",
            ],
            "next_evidence_needed": [
                "manual validation of selected frontier candidates against workload stage markers",
                "repeated trials on real repositories or code-agent tasks",
                "EEVDF/static-cgroup/taskset baselines",
                "live FlowCausal BPF admission counters and victim sidecars",
            ],
        },
        "manifest": manifest,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    readme = [
        "# FlowCausal sandbox characterization",
        "",
        "This run observes a sandbox/workflow process tree without attaching the FlowCausal scheduler.",
        "It is a Go/No-Go input for the paper, not a scheduling result.",
        "",
        f"- Characterization level: `{characterization_level}`",
        f"- Samples: {len(records)}",
        f"- Samples with selected frontier: {samples_with_frontier}",
        f"- Samples with parent-wait evidence: {samples_with_parent_wait}",
        f"- Samples with barrier-tail evidence: {samples_with_barrier_tail}",
        f"- Samples with nonzero schedstat deltas: {samples_with_schedstat}",
        f"- Max process count: {max_process_count}",
        f"- Max leaf count: {max_leaf_count}",
        f"- Frontier wait share: {summary['frontier_wait_share']}",
        f"- Frontier runtime share: {summary['frontier_runtime_share']}",
        f"- Frontier wait per frontier sample ns: {summary['frontier_wait_per_frontier_sample_ns']}",
        f"- Frontier wait per elapsed second: {summary['frontier_wait_per_elapsed_sec']}",
        "",
        "## Files",
        "",
        "- `workload.log`: raw workload output and stage markers, if any.",
        "- `characterization.ndjson`: per-sample process-tree and frontier-candidate records.",
        "- `summary.json`: machine-readable aggregate and claim discipline.",
        "- `manifest.json`: command line and return codes.",
        "",
        "## Claim discipline",
        "",
        "Permitted: process-tree characterization and lower-bound schedstat observation.",
        "Forbidden: FlowCausal speedup, frontier accuracy, victim safety, or production-readiness claims.",
        "",
    ]
    (out_dir / "README.md").write_text("\n".join(readme), encoding="utf-8")
    return summary


def main() -> int:
    args = parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing workload command; pass it after --")
    if args.taskset:
        command = ["taskset", "-c", args.taskset, *command]

    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists():
        if not args.overwrite:
            raise SystemExit(f"output directory exists; pass --overwrite: {out_dir}")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    forced_stage = flowctl.parse_optional_stage_kind(args.stage_kind)
    stage_state_file = pathlib.Path(args.stage_state_file).resolve() if args.stage_state_file else None
    workload_env = os.environ.copy()
    if stage_state_file is not None:
        workload_env["FLOWCAUSAL_STAGE_STATE_FILE"] = str(stage_state_file)
    interval_sec = max(1, args.interval_ms) / 1000.0
    manifest: dict[str, Any] = {
        "command": command,
        "out_dir": str(out_dir),
        "interval_ms": args.interval_ms,
        "frontier_width": args.frontier_width,
        "adaptive_frontier_width": bool(args.adaptive_frontier_width),
        "adaptive_max_frontier_width": args.adaptive_max_frontier_width,
        "top_candidates": args.top_candidates,
        "min_frontier_score": args.min_frontier_score,
        "stage_kind": args.stage_kind,
        "stage_state_file": str(stage_state_file) if stage_state_file is not None else "",
        "taskset": args.taskset,
        "timeout_sec": args.timeout_sec,
    }

    proc: subprocess.Popen[str] | None = None
    records: list[dict[str, Any]] = []
    previous: dict[int, PidSample] = {}
    start_ns = time.time_ns()
    timed_out = False
    workload_rc: int | None = None
    try:
        proc = start_workload(command, out_dir / "workload.log", env=workload_env)
        manifest["root_pid"] = proc.pid
        deadline = time.monotonic() + args.timeout_sec if args.timeout_sec > 0 else None
        sample_index = 0
        with (out_dir / "characterization.ndjson").open("w", encoding="utf-8") as log:
            while proc.poll() is None:
                if deadline is not None and time.monotonic() >= deadline:
                    timed_out = True
                    workload_rc = stop_process_group(proc)
                    proc = None
                    break
                try:
                    record, previous = sample_tree(
                        manifest["root_pid"],
                        forced_stage=forced_stage,
                        stage_state_file=stage_state_file,
                        previous=previous,
                        sample_index=sample_index,
                        start_ns=start_ns,
                        width=args.frontier_width,
                        adaptive_width=args.adaptive_frontier_width,
                        adaptive_max_width=args.adaptive_max_frontier_width,
                        min_score=args.min_frontier_score,
                        top_candidates=args.top_candidates,
                    )
                except Exception as exc:  # keep workload cleanup reliable
                    record = {
                        "type": "characterization_error",
                        "sample": sample_index,
                        "time_ns": time.time_ns(),
                        "error": repr(exc),
                    }
                log.write(json.dumps(record, sort_keys=True) + "\n")
                log.flush()
                if record.get("type") == "characterization_sample":
                    records.append(record)
                sample_index += 1
                time.sleep(interval_sec)
        if proc is not None:
            workload_rc = proc.wait()
            proc = None
    finally:
        if proc is not None and proc.poll() is None:
            workload_rc = stop_process_group(proc)
        manifest["workload_returncode"] = workload_rc
        manifest["timed_out"] = timed_out
        manifest["elapsed_sec"] = (time.time_ns() - start_ns) / 1_000_000_000.0
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        write_summary(out_dir, manifest=manifest, records=records)

    print(f"results={out_dir}")
    return int(workload_rc or 0)


if __name__ == "__main__":
    raise SystemExit(main())

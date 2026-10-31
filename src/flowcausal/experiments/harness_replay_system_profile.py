#!/usr/bin/env python3
"""Replay-time system profiling for HarnessReplayBench.

Structural fidelity verifies that a replay executed the expected commands.
This module records the Linux process-tree shape and lower-bound CPU scheduling
signals while the replay is running: process width, newly observed pids,
``/proc/<pid>/schedstat`` runtime/wait deltas, and a simple CPU migration
proxy.  It is intentionally a profiler, not a proof that replay matches a live
agent run.  A later system-fidelity audit can compare two profiles captured
with the same schema.
"""
from __future__ import annotations

import collections
import json
import pathlib
import threading
import time
from dataclasses import dataclass
from typing import Any


SCHEMA = "flowcausal.harness_replay_system_profile.v1"


@dataclass(frozen=True)
class SchedStat:
    runtime_ns: int = 0
    wait_ns: int = 0
    slices: int = 0


@dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    comm: str
    state: str
    cpu_ticks: int
    processor: int | None
    children: tuple[int, ...]
    sched: SchedStat

    @property
    def leaf(self) -> bool:
        return not self.children


@dataclass(frozen=True)
class PidPrevious:
    cpu_ticks: int
    processor: int | None
    sched: SchedStat


def positive_delta(current: int, previous: int) -> int:
    return current - previous if current >= previous else 0


def read_task_children(pid: int) -> tuple[int, ...]:
    children: set[int] = set()
    task_dir = pathlib.Path("/proc") / str(pid) / "task"
    try:
        tasks = list(task_dir.iterdir())
    except OSError:
        return ()
    for task in tasks:
        try:
            text = (task / "children").read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for item in text.split():
            try:
                children.add(int(item))
            except ValueError:
                continue
    return tuple(sorted(children))


def read_schedstat(pid: int) -> SchedStat:
    try:
        fields = pathlib.Path(f"/proc/{pid}/schedstat").read_text().split()
    except OSError:
        return SchedStat()
    if len(fields) < 3:
        return SchedStat()
    try:
        return SchedStat(runtime_ns=int(fields[0]), wait_ns=int(fields[1]), slices=int(fields[2]))
    except ValueError:
        return SchedStat()


def parse_proc_stat(pid: int) -> tuple[int, str, int, int, int | None] | None:
    try:
        text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    end = text.rfind(")")
    if end < 0:
        return None
    try:
        parsed_pid = int(text.split("(", 1)[0].strip())
    except ValueError:
        parsed_pid = pid
    rest = text[end + 2 :].split()
    if len(rest) < 13:
        return None
    state = rest[0]
    try:
        ppid = int(rest[1])
        utime = int(rest[11])
        stime = int(rest[12])
    except ValueError:
        return None
    processor: int | None = None
    # field 39 (processor) appears as rest[36] because rest[0] is field 3.
    if len(rest) > 36:
        try:
            processor = int(rest[36])
        except ValueError:
            processor = None
    return parsed_pid, state, ppid, utime + stime, processor


def read_comm(pid: int) -> str:
    try:
        return pathlib.Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def read_proc_info(pid: int) -> ProcInfo | None:
    parsed = parse_proc_stat(pid)
    if parsed is None:
        return None
    parsed_pid, state, ppid, cpu_ticks, processor = parsed
    return ProcInfo(
        pid=parsed_pid,
        ppid=ppid,
        comm=read_comm(pid),
        state=state,
        cpu_ticks=cpu_ticks,
        processor=processor,
        children=read_task_children(pid),
        sched=read_schedstat(pid),
    )


def proc_subtree(root_pid: int) -> list[int]:
    seen: set[int] = set()
    queue = collections.deque([root_pid])
    while queue:
        pid = queue.popleft()
        if pid in seen:
            continue
        seen.add(pid)
        for child in read_task_children(pid):
            if child not in seen:
                queue.append(child)
    return sorted(seen)


def read_stage_state(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


class SystemProfileRecorder:
    def __init__(
        self,
        *,
        root_pid: int,
        out_dir: pathlib.Path,
        stage_state_file: pathlib.Path | None,
        interval_ms: int = 25,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self.root_pid = int(root_pid)
        self.out_dir = pathlib.Path(out_dir)
        self.stage_state_file = pathlib.Path(stage_state_file) if stage_state_file else None
        self.interval_sec = max(1, int(interval_ms)) / 1000.0
        self.provenance = provenance or {}
        self.samples_path = self.out_dir / "system-profile.ndjson"
        self.summary_path = self.out_dir / "system-profile-summary.json"
        self.markdown_path = self.out_dir / "system-profile-summary.md"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._previous: dict[int, PidPrevious] = {}
        self._records: list[dict[str, Any]] = []
        self._seen_pids: set[int] = set()
        self._sample_index = 0
        self._started_ns = 0

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._started_ns = time.time_ns()
        self._thread = threading.Thread(target=self._run, name=f"flowcausal-system-profile-{self.root_pid}", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_sec * 4))
        summary = summarize_records(
            self._records,
            root_pid=self.root_pid,
            started_ns=self._started_ns,
            provenance=self.provenance,
        )
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.markdown_path.write_text(render_markdown(summary), encoding="utf-8")
        return {
            "enabled": True,
            "root_pid": self.root_pid,
            "samples": summary["sample_count"],
            "summary": str(self.summary_path),
            "samples_log": str(self.samples_path),
            "markdown": str(self.markdown_path),
            "profile_level": summary["profile_level"],
            "max_process_count": summary["max_process_count"],
            "unique_pids_seen": summary["unique_pids_seen"],
            "schedstat_observed": summary["schedstat_observed"],
        }

    def _run(self) -> None:
        with self.samples_path.open("w", encoding="utf-8") as handle:
            while not self._stop.is_set():
                record = self.sample_once()
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
                if record.get("type") == "system_profile_sample":
                    self._records.append(record)
                self._stop.wait(self.interval_sec)

    def sample_once(self) -> dict[str, Any]:
        now_ns = time.time_ns()
        pids = proc_subtree(self.root_pid)
        infos = {pid: info for pid in pids if (info := read_proc_info(pid)) is not None}
        stage_state = read_stage_state(self.stage_state_file)
        stage = str(stage_state.get("stage") or "UNKNOWN")
        phase = str(stage_state.get("phase") or "")
        current: dict[int, PidPrevious] = {}
        aggregate = collections.Counter()
        state_counts = collections.Counter()
        command_counts = collections.Counter()
        new_pids = 0
        migration_events = 0
        for pid, info in infos.items():
            state_counts[info.state] += 1
            if info.comm:
                command_counts[info.comm] += 1
            if pid not in self._seen_pids:
                new_pids += 1
                self._seen_pids.add(pid)
            old = self._previous.get(pid)
            current[pid] = PidPrevious(cpu_ticks=info.cpu_ticks, processor=info.processor, sched=info.sched)
            if old is None:
                continue
            cpu_delta = positive_delta(info.cpu_ticks, old.cpu_ticks)
            runtime_delta = positive_delta(info.sched.runtime_ns, old.sched.runtime_ns)
            wait_delta = positive_delta(info.sched.wait_ns, old.sched.wait_ns)
            slices_delta = positive_delta(info.sched.slices, old.sched.slices)
            aggregate["cpu_ticks_delta"] += cpu_delta
            aggregate["sched_runtime_delta_ns"] += runtime_delta
            aggregate["sched_wait_delta_ns"] += wait_delta
            aggregate["sched_slices_delta"] += slices_delta
            if (
                old.processor is not None
                and info.processor is not None
                and old.processor != info.processor
            ):
                migration_events += 1
        self._previous = current
        record = {
            "type": "system_profile_sample",
            "schema": SCHEMA,
            "sample": self._sample_index,
            "time_ns": now_ns,
            "elapsed_ms": (now_ns - self._started_ns) / 1_000_000.0 if self._started_ns else 0.0,
            "root_pid": self.root_pid,
            "stage": stage,
            "phase": phase,
            "process_count": len(infos),
            "leaf_count": sum(1 for info in infos.values() if info.leaf),
            "new_pids": new_pids,
            "state_counts": dict(sorted(state_counts.items())),
            "top_commands": command_counts.most_common(8),
            "aggregate_delta": dict(aggregate),
            "cpu_migration_events": migration_events,
            "schedstat_nonzero": aggregate["sched_runtime_delta_ns"] > 0 or aggregate["sched_wait_delta_ns"] > 0,
        }
        self._sample_index += 1
        return record


def summarize_records(
    records: list[dict[str, Any]],
    *,
    root_pid: int,
    started_ns: int,
    provenance: dict[str, Any],
) -> dict[str, Any]:
    totals = collections.Counter()
    stage_counts = collections.Counter()
    stage_totals: dict[str, collections.Counter[str]] = {}
    command_counts = collections.Counter()
    max_process_count = 0
    max_leaf_count = 0
    samples_with_schedstat = 0
    unique_pids_seen = 0
    migration_events = 0
    for record in records:
        stage = str(record.get("stage") or "UNKNOWN")
        stage_counts[stage] += 1
        stage_totals.setdefault(stage, collections.Counter())
        max_process_count = max(max_process_count, int(record.get("process_count", 0) or 0))
        max_leaf_count = max(max_leaf_count, int(record.get("leaf_count", 0) or 0))
        unique_pids_seen += int(record.get("new_pids", 0) or 0)
        migration_events += int(record.get("cpu_migration_events", 0) or 0)
        if record.get("schedstat_nonzero"):
            samples_with_schedstat += 1
        for key, value in (record.get("aggregate_delta") or {}).items():
            try:
                intval = int(value)
            except (TypeError, ValueError):
                continue
            totals[key] += intval
            stage_totals[stage][key] += intval
        for comm, count in record.get("top_commands", []):
            command_counts[str(comm)] += int(count or 0)

    stage_delta_summary: dict[str, Any] = {}
    for stage, count in sorted(stage_counts.items()):
        local = stage_totals.get(stage, collections.Counter())
        stage_delta_summary[stage] = {
            "sample_count": count,
            "aggregate_delta": dict(local),
            "sched_wait_per_sample_ns": (
                local["sched_wait_delta_ns"] / count if count else None
            ),
            "sched_runtime_per_sample_ns": (
                local["sched_runtime_delta_ns"] / count if count else None
            ),
        }

    schedstat_observed = samples_with_schedstat > 0 and (
        totals["sched_runtime_delta_ns"] > 0 or totals["sched_wait_delta_ns"] > 0
    )
    if records and schedstat_observed:
        profile_level = "S1: process-tree profile with schedstat deltas"
    elif records:
        profile_level = "S0.5: process-tree profile without nonzero schedstat deltas"
    else:
        profile_level = "S0: no replay-time samples"

    elapsed_sec = 0.0
    if records:
        elapsed_sec = max(float(record.get("elapsed_ms", 0.0) or 0.0) for record in records) / 1000.0
    return {
        "schema": SCHEMA,
        "profile_level": profile_level,
        "root_pid": root_pid,
        "started_ns": started_ns,
        "sample_count": len(records),
        "stage_sample_counts": dict(sorted(stage_counts.items())),
        "stage_delta_summary": stage_delta_summary,
        "max_process_count": max_process_count,
        "max_leaf_count": max_leaf_count,
        "unique_pids_seen": unique_pids_seen,
        "cpu_migration_events": migration_events,
        "samples_with_schedstat": samples_with_schedstat,
        "schedstat_observed": schedstat_observed,
        "aggregate_delta": dict(totals),
        "top_commands": command_counts.most_common(12),
        "sampled_elapsed_sec": elapsed_sec,
        "sched_wait_per_elapsed_sec": (
            totals["sched_wait_delta_ns"] / elapsed_sec if elapsed_sec > 0 else None
        ),
        "sched_runtime_per_elapsed_sec": (
            totals["sched_runtime_delta_ns"] / elapsed_sec if elapsed_sec > 0 else None
        ),
        "provenance": provenance,
        "claim_discipline": {
            "permitted": [
                "replay-time process-tree width and lower-bound schedstat profile",
                "stage-local runtime/wait/migration proxy for the replay run",
                "evidence that the replay exercised fork/exec-like process creation through newly observed pids",
            ],
            "forbidden": [
                "system fidelity against the original live agent run without a comparable live profile",
                "frontier correctness",
                "FlowCausal speedup or safety",
            ],
            "next_evidence_needed": [
                "capture the same profile schema during original/live harness runs",
                "compare live vs replay process width, unique pids, stage mix, runtime/wait, and migration",
                "combine with structural fidelity and mechanism/victim audits",
            ],
        },
    }


def render_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Harness replay system profile",
        "",
        f"- profile_level: `{summary['profile_level']}`",
        f"- samples: {summary['sample_count']}",
        f"- max_process_count: {summary['max_process_count']}",
        f"- max_leaf_count: {summary['max_leaf_count']}",
        f"- unique_pids_seen: {summary['unique_pids_seen']}",
        f"- cpu_migration_events: {summary['cpu_migration_events']}",
        f"- schedstat_observed: `{summary['schedstat_observed']}`",
        "",
        "## Stage summary",
        "",
        "| stage | samples | sched wait ns | sched runtime ns | wait/sample ns | runtime/sample ns |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for stage, row in summary.get("stage_delta_summary", {}).items():
        delta = row.get("aggregate_delta", {})
        lines.append(
            f"| `{stage}` | {row.get('sample_count')} | "
            f"{delta.get('sched_wait_delta_ns', 0)} | {delta.get('sched_runtime_delta_ns', 0)} | "
            f"{row.get('sched_wait_per_sample_ns')} | {row.get('sched_runtime_per_sample_ns')} |"
        )
    lines.extend(
        [
            "",
            "This profile is replay-time system evidence. It is not by itself a live-vs-replay system fidelity pass.",
            "",
        ]
    )
    return "\n".join(lines)


def disabled_profile() -> dict[str, Any]:
    return {"enabled": False}

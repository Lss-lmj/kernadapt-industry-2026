#!/usr/bin/env python3
"""FlowCausal user-space policy/control prototype.

The controller maps a sandbox cgroup or process subtree to FlowCausal's
session--stage--cohort--frontier model and writes bounded hints into BPF maps
exported by scx_flowcausal.  It is deliberately small: no LLM, no online expert
logic, and no hidden policy magic.  The research question stays in the kernel
scheduler: does making the dynamic workflow frontier visible improve stage
completion under interference?

This file is usable as a dry-run planner and as a live map writer once the
scx_flowcausal loader has pinned its BPF maps.
"""
from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import pathlib
import re
import struct
import subprocess
import time
from typing import Iterable, TextIO


DEFAULT_PIN_DIR = pathlib.Path("/sys/fs/bpf/flowcausal")


class Stage(enum.IntEnum):
    UNKNOWN = 0
    MODEL_WAIT = 1
    SHELL = 2
    SEARCH = 3
    BUILD = 4
    TEST = 5
    RETRY = 6
    IO_WAIT = 7


TASK_FRONTIER = 1 << 0
TASK_COHORT_TAIL = 1 << 1
TASK_LOCALITY = 1 << 2
TASK_PROPAGATE = 1 << 4
TASK_OBSERVE_ONLY = 1 << 6
TASK_BACKGROUND = 1 << 7
TASK_EFFECTIVE_HINT_MASK = (
    TASK_FRONTIER
    | TASK_LOCALITY
    | TASK_PROPAGATE
    | TASK_OBSERVE_ONLY
    | TASK_BACKGROUND
)

MIN_FRONTIER_SCORE = 50
FRONTIER_ACTIONS = {"advance", "observe", "auto", "stage-aware"}
FRONTIER_STABILITY_ACTIONS = {"suppress", "observe"}
BUILD_FRONTIER_ADMISSIONS = {"advance", "tail-only", "observe"}

POLICY_ENABLE = 1 << 0
POLICY_LOCALITY = 1 << 1
POLICY_WIDTH_CTL = 1 << 2
POLICY_FALLBACK = 1 << 4


@dataclasses.dataclass(frozen=True)
class TaskHint:
    pid: int
    tgid: int
    parent_pid: int
    session_id: int
    stage_id: int
    cohort_id: int
    flags: int
    confidence: int
    anchor_cpu: int = -1
    propagation_depth: int = 0
    enqueue_ts_ns: int = 0
    running_since_ns: int = 0
    runnable_wait_ns: int = 0
    runtime_ns: int = 0
    last_update_ns: int = 0

    def pack_key(self) -> bytes:
        return struct.pack("I", self.pid)

    def pack_value(self) -> bytes:
        last_update_ns = self.last_update_ns or time.time_ns()
        return struct.pack(
            "QQQQQQQQIIIIiI",
            self.session_id,
            self.stage_id,
            self.cohort_id,
            self.enqueue_ts_ns,
            self.running_since_ns,
            self.runnable_wait_ns,
            self.runtime_ns,
            last_update_ns,
            self.tgid,
            self.parent_pid,
            self.flags,
            self.confidence,
            self.anchor_cpu,
            max(0, self.propagation_depth),
        )


@dataclasses.dataclass(frozen=True)
class SessionPolicy:
    session_id: int
    flags: int = POLICY_ENABLE
    min_confidence: int = 70
    max_frontier_width: int = 1
    max_vtime_boost_ns: int = 500_000
    window_ns: int = 100_000_000
    boost_budget_ns: int = 10_000_000
    budget_used_ns: int = 0
    budget_window_start_ns: int = 0
    expires_ns: int = 0
    anchor_cpu: int = -1

    def pack_key(self) -> bytes:
        return struct.pack("Q", self.session_id)

    def pack_value(self) -> bytes:
        return struct.pack(
            "IIIIQQQQQiI",
            self.flags,
            self.min_confidence,
            self.max_frontier_width,
            self.max_vtime_boost_ns,
            self.window_ns,
            self.boost_budget_ns,
            self.budget_used_ns,
            self.budget_window_start_ns,
            self.expires_ns,
            self.anchor_cpu,
            0,
        )


@dataclasses.dataclass(frozen=True)
class ProcInfo:
    pid: int
    ppid: int
    state: str
    start_time: int
    cpu_ticks: int
    comm: str
    command: str
    child_count: int
    child_pids: tuple[int, ...]

    @property
    def leaf(self) -> bool:
        return self.child_count == 0


@dataclasses.dataclass(frozen=True)
class HintPlan:
    root_pid: int
    policy: SessionPolicy
    hints: list[TaskHint]
    stage: Stage
    frontier_pids: set[int]
    propagation_pids: set[int]
    ranked_frontiers: list[tuple[int, int, str]]
    proc_comms: dict[int, str]
    stability: dict[str, object] = dataclasses.field(default_factory=dict)
    adaptive_width: dict[str, object] = dataclasses.field(default_factory=dict)
    adaptive_propagation: dict[str, object] = dataclasses.field(default_factory=dict)
    action: dict[str, object] = dataclasses.field(default_factory=dict)
    stage_source: str = "inferred"
    stage_label: str = ""


@dataclasses.dataclass
class FrontierStabilityState:
    """Small per-watch hysteresis state for online frontier selection.

    The controller still scores each process tree snapshot independently, but a
    live watcher can optionally require a candidate to be observed across
    multiple consecutive samples before it is promoted.  Strong evidence can
    bypass the debounce gate, and a still-alive previous frontier can be held
    briefly to avoid spending budget on tick-to-tick top-N churn.
    """

    seen_ticks: dict[int, int] = dataclasses.field(default_factory=dict)
    held_until_ns: dict[int, int] = dataclasses.field(default_factory=dict)
    active_frontiers: set[int] = dataclasses.field(default_factory=set)


@dataclasses.dataclass(frozen=True)
class AdaptiveWidthDecision:
    """Auditable controller-side frontier-width choice for wide cohorts."""

    enabled: bool
    requested_frontier_width: int
    requested_max_frontier_width: int
    adaptive_max_frontier_width: int
    frontier_width: int
    max_frontier_width: int
    serviceable_leaf_count: int = 0
    eligible_candidate_count: int = 0
    parent_wait_candidate_count: int = 0
    max_barrier_tail: int = 0
    reason: str = "static"


@dataclasses.dataclass(frozen=True)
class AdaptivePropagationDepthDecision:
    """Auditable controller-side propagation-depth choice.

    The default remains a fixed propagation depth for reproducibility.  When the
    adaptive knob is enabled, the controller uses the shallow depth that the
    stage-localized replay already supports (depth 1) when it has a concrete
    frontier, and only spends the configured maximum depth when propagation is
    acting as a spawn-anchor safety net for short-lived children that may appear
    between polling ticks.
    """

    enabled: bool
    requested_depth: int
    propagation_depth: int
    frontier_count: int = 0
    anchor_count: int = 0
    stage: str = "UNKNOWN"
    eligible_candidate_count: int = 0
    selected_parent_wait_count: int = 0
    selected_max_barrier_tail: int = 0
    policy: str = "static"
    reason: str = "static"


def stable_u64(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8", "surrogateescape"):
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def read_int(path: pathlib.Path, default: int = 0) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return default


def proc_children() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for entry in pathlib.Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        stat = entry / "stat"
        try:
            text = stat.read_text(errors="replace")
        except OSError:
            continue
        # /proc/<pid>/stat has "pid (comm) state ppid ..."; comm can contain
        # spaces, so split after the closing parenthesis.
        try:
            tail = text.rsplit(")", 1)[1].strip().split()
            ppid = int(tail[1])
            pid = int(entry.name)
        except (IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def proc_info(pid: int, child_map: dict[int, list[int]] | None = None) -> ProcInfo | None:
    stat = pathlib.Path(f"/proc/{pid}/stat")
    try:
        text = stat.read_text(errors="replace")
        tail = text.rsplit(")", 1)[1].strip().split()
        state = tail[0]
        ppid = int(tail[1])
        utime = int(tail[11])
        stime = int(tail[12])
        start_time = int(tail[19])
    except (OSError, IndexError, ValueError):
        return None

    if child_map is None:
        child_map = proc_children()
    return ProcInfo(
        pid=pid,
        ppid=ppid,
        state=state,
        start_time=start_time,
        cpu_ticks=utime + stime,
        comm=comm_of(pid),
        command=command_line(pid),
        child_count=len(child_map.get(pid, ())),
        child_pids=tuple(sorted(child_map.get(pid, ()))),
    )


def collect_proc_info(pids: Iterable[int]) -> dict[int, ProcInfo]:
    child_map = proc_children()
    out: dict[int, ProcInfo] = {}
    for pid in pids:
        info = proc_info(pid, child_map)
        if info is not None:
            out[pid] = info
    return out


def subtree(root_pid: int) -> list[int]:
    tree = proc_children()
    out: list[int] = []
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.append(pid)
        stack.extend(tree.get(pid, ()))
    return sorted(out)


def ppid_of(pid: int) -> int:
    try:
        text = pathlib.Path(f"/proc/{pid}/stat").read_text(errors="replace")
        return int(text.rsplit(")", 1)[1].strip().split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def comm_of(pid: int) -> str:
    try:
        return pathlib.Path(f"/proc/{pid}/comm").read_text(errors="replace").strip()
    except OSError:
        return ""


def command_tokens(command: str, comm: str = "") -> tuple[str, ...]:
    """Return executable-like tokens from a /proc command line.

    The online scorer should recognize actual tools (``make``, ``pytest``,
    ``ld.lld``), not option names such as ``--make-jobs`` from a benchmark
    harness.  We therefore look at command-line tokens and path basenames, and
    ignore dash-prefixed options.
    """

    tokens: list[str] = []
    for raw in (comm, *command.split()):
        token = raw.strip().strip("'\"")
        if not token or token.startswith("-"):
            continue
        base = token.rsplit("/", 1)[-1].lower()
        if base:
            tokens.append(base)
    return tuple(tokens)


def has_token(tokens: Iterable[str], *names: str) -> bool:
    wanted = {name.lower() for name in names}
    return any(token in wanted for token in tokens)


def has_token_prefix(tokens: Iterable[str], *prefixes: str) -> bool:
    wanted = tuple(prefix.lower() for prefix in prefixes)
    return any(token.startswith(wanted) for token in tokens)


def infer_stage(command: str) -> Stage:
    tokens = command_tokens(command)
    if (
        has_token(tokens, "pytest", "ctest", "jest", "bats", "lit")
        or (has_token(tokens, "cargo", "go", "npm") and has_token(tokens, "test"))
        or (has_token(tokens, "make", "ninja") and has_token(tokens, "test", "check"))
        or any(re.search(r"(^|[_-])(test[_-]runner|[a-z0-9][a-z0-9_.-]*[_-]test)$", token) for token in tokens)
    ):
        return Stage.TEST
    if has_token(
        tokens,
        "make",
        "ninja",
        "cmake",
        "gcc",
        "g++",
        "clang",
        "clang++",
        "ld",
        "ld.lld",
        "lld",
        "mold",
        "gold",
        "collect2",
        "ar",
        "cargo",
        "go",
        "javac",
        "mvn",
        "gradle",
    ) or has_token_prefix(tokens, "cc1", "rustc"):
        return Stage.BUILD
    if has_token(tokens, "rg", "grep", "find", "fd", "ripgrep"):
        return Stage.SEARCH
    return Stage.SHELL


def command_line(pid: int) -> str:
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()


def infer_stage_from_infos(infos: Iterable[ProcInfo]) -> Stage:
    priority = {
        Stage.UNKNOWN: 0,
        Stage.MODEL_WAIT: 1,
        Stage.IO_WAIT: 1,
        Stage.SHELL: 2,
        Stage.SEARCH: 3,
        Stage.BUILD: 4,
        Stage.TEST: 5,
        Stage.RETRY: 6,
    }
    stage = Stage.SHELL
    for info in infos:
        inferred = infer_stage(f"{info.comm} {info.command}")
        if priority[inferred] > priority[stage]:
            stage = inferred
    return stage


STAGE_ALIASES = {
    "RETRY_PROBE": "RETRY",
    "PACKAGE": "SHELL",
    "PACKAGE_BG": "SHELL",
    "PACKAGE_BACKGROUND": "SHELL",
}


def normalize_stage_token(text: str | None) -> str:
    normalized = (text or "").strip().upper().replace("-", "_")
    return STAGE_ALIASES.get(normalized, normalized)


def parse_stage_state_file(path: pathlib.Path | None) -> tuple[Stage | None, dict[str, object]]:
    """Read a workload-emitted live stage state file.

    The file is an optional bridge from a staged workflow wrapper to the online
    controller.  It is intentionally advisory: malformed, missing, or unknown
    states are ignored so the controller falls back to ordinary process-tree
    inference.  ``RETRY_PROBE`` and ``PACKAGE_BACKGROUND`` are workload-facing
    labels; the scheduler model maps them to ``RETRY`` and ``SHELL``.
    """

    if path is None:
        return None, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, {}
    if not isinstance(data, dict):
        return None, {}
    stage_text = normalize_stage_token(str(data.get("stage", "")))
    if not stage_text:
        return None, data
    try:
        return parse_stage_kind(stage_text), data
    except ValueError:
        return None, data


def resolve_live_stage(
    infos: Iterable[ProcInfo],
    *,
    forced_stage: Stage | None,
    stage_state_file: pathlib.Path | None = None,
) -> tuple[Stage, str, dict[str, object]]:
    """Resolve the stage for a live controller tick.

    Priority is explicit CLI forcing, then a workload-emitted state file, then
    process-command inference.  The returned source is recorded in
    ``controller.ndjson`` so experiments can audit whether a real repo result is
    using a live stage bridge or merely falling back to command heuristics.
    """

    if forced_stage is not None:
        return forced_stage, "forced", {}
    state_stage, state = parse_stage_state_file(stage_state_file)
    if state_stage is not None:
        return state_stage, "state-file", state
    return infer_stage_from_infos(infos), "inferred", {}


def command_role_score(command: str, comm: str, stage: Stage) -> tuple[int, str]:
    text = f"{comm} {command}".lower()
    tokens = command_tokens(command, comm)
    if stage == Stage.BUILD:
        if has_token(tokens, "ld", "ld.lld", "lld", "mold", "gold", "collect2", "ar"):
            return 80, "link/archive tail"
        if has_token(tokens, "gcc", "g++", "clang", "clang++", "rustc", "javac", "go") or has_token_prefix(tokens, "cc1"):
            return 55, "compiler worker"
        if has_token(tokens, "bpftool", "objtool", "pahole", "strip", "llvm-strip"):
            return 45, "build tool"
        if has_token(tokens, "make", "ninja", "cmake", "cargo", "mvn", "gradle"):
            return 35, "build driver"
    elif stage in {Stage.TEST, Stage.RETRY}:
        if (
            has_token(tokens, "pytest", "ctest", "jest", "bats", "lit")
            or (has_token(tokens, "cargo", "go", "npm") and has_token(tokens, "test"))
            or (has_token(tokens, "make", "ninja") and has_token(tokens, "test", "check"))
        ):
            return 80, "test driver"
        if has_token(tokens, "python", "python3", "node", "java", "ruby", "pytest") or any(
            re.search(r"(^|[_-])(test[_-]runner|[a-z0-9][a-z0-9_.-]*[_-]test)", token)
            for token in tokens
        ):
            return 55, "test worker"
    elif stage == Stage.SEARCH:
        if has_token(tokens, "rg", "grep", "find", "fd", "ripgrep"):
            return 80, "search worker"
    return 10, "generic task"


def is_timer_wait(info: ProcInfo) -> bool:
    text = f"{info.comm} {info.command}".lower()
    return bool(re.search(r"\b(sleep|timeout)\b", text)) and info.state in {"S", "I"} and info.cpu_ticks == 0


def is_cpu_serviceable(info: ProcInfo) -> bool:
    if is_timer_wait(info):
        return False
    return info.state in {"R", "D"} or info.cpu_ticks > 0


def descendants(pid: int, children_by_parent: dict[int, list[int]]) -> list[int]:
    out: list[int] = []
    stack = list(children_by_parent.get(pid, ()))
    while stack:
        child = stack.pop()
        out.append(child)
        stack.extend(children_by_parent.get(child, ()))
    return out


def wait_frontier_evidence(infos_by_pid: dict[int, ProcInfo]) -> dict[int, tuple[int, list[str]]]:
    """Return extra scores for children that unblock a sleeping parent.

    This is intentionally conservative.  A sleeping parent by itself is not a
    frontier; only CPU-serviceable leaf descendants under that parent receive a
    bonus.  When only a few such leaves remain, the evidence is stronger because
    the workflow is likely near a fork/join barrier tail.
    """

    children_by_parent: dict[int, list[int]] = {}
    for info in infos_by_pid.values():
        if info.ppid in infos_by_pid:
            children_by_parent.setdefault(info.ppid, []).append(info.pid)

    evidence: dict[int, tuple[int, list[str]]] = {}
    for parent in infos_by_pid.values():
        if parent.state not in {"S", "D"}:
            continue
        if parent.pid not in children_by_parent:
            continue

        desc = descendants(parent.pid, children_by_parent)
        leaf_desc = [
            pid
            for pid in desc
            if pid in infos_by_pid
            and pid not in children_by_parent
            and is_cpu_serviceable(infos_by_pid[pid])
        ]
        if not leaf_desc:
            continue

        parent_name = parent.comm or "parent"
        parent_bonus = 45 if parent.state == "S" else 35
        tail_bonus = 0
        if len(leaf_desc) == 1:
            tail_bonus = 35
        elif len(leaf_desc) <= 3:
            tail_bonus = 20
        elif len(leaf_desc) <= 5:
            tail_bonus = 10

        for pid in leaf_desc:
            bonus, reasons = evidence.get(pid, (0, []))
            local_reasons = [f"parent-wait:{parent_name}"]
            if tail_bonus:
                local_reasons.append(f"barrier-tail:{len(leaf_desc)}")
            evidence[pid] = (bonus + parent_bonus + tail_bonus, reasons + local_reasons)

    return evidence


def rank_frontier_candidates(infos: Iterable[ProcInfo], stage: Stage) -> list[tuple[int, int, str]]:
    candidates = list(infos)
    if not candidates:
        return []

    infos_by_pid = {info.pid: info for info in candidates}
    wait_evidence = wait_frontier_evidence(infos_by_pid)
    by_start = {info.pid: rank for rank, info in enumerate(sorted(candidates, key=lambda x: x.start_time))}
    max_rank = max(by_start.values()) if by_start else 0
    role_by_pid = {
        info.pid: command_role_score(info.command, info.comm, stage)
        for info in candidates
    }
    has_stage_specific_candidate = (
        stage in {Stage.BUILD, Stage.TEST, Stage.SEARCH}
        and any(
            role_score >= 35 and role_reason != "generic task"
            for role_score, role_reason in role_by_pid.values()
        )
    )
    ranked: list[tuple[int, int, str]] = []

    for info in candidates:
        score = 0
        reasons: list[str] = []
        role_score, role_reason = role_by_pid[info.pid]
        score += role_score
        reasons.append(role_reason)

        if has_stage_specific_candidate and role_reason == "generic task":
            score -= 140
            reasons.append("stage-generic-background")

        if is_timer_wait(info):
            score -= 60
            reasons.append("timer-wait")

        if info.state in {"R", "D"}:
            score += 35
            reasons.append(f"state={info.state}")
        elif info.state in {"S", "I"}:
            score += 8
            reasons.append(f"state={info.state}")
            if not info.leaf:
                score -= 35
                reasons.append("waiting-parent")

        if info.leaf:
            score += 20
            reasons.append("leaf")

        # Later cohort members are often the tail at a fork/join barrier.  Use
        # start-time rank only as a weak tie-breaker so command/stage evidence
        # dominates over accidental process age.
        if max_rank:
            tail_bonus = int(20 * by_start[info.pid] / max_rank)
            score += tail_bonus
            if tail_bonus:
                reasons.append(f"young+{tail_bonus}")

        if info.cpu_ticks > 0:
            score += 5
            reasons.append("cpu-active")

        extra_score, extra_reasons = wait_evidence.get(info.pid, (0, []))
        if extra_score:
            score += extra_score
            reasons.extend(extra_reasons)

        ranked.append((score, info.pid, ",".join(reasons)))

    return sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True)


def choose_frontier(
    pids: Iterable[int],
    stage: Stage,
    width: int = 1,
    min_frontier_score: int = MIN_FRONTIER_SCORE,
) -> set[int]:
    infos = collect_proc_info(pids)
    ranked = rank_frontier_candidates(infos.values(), stage)
    min_score = max(0, min_frontier_score)
    return {
        pid
        for score, pid, _ in ranked[: max(1, width)]
        if score >= min_score
    }


def barrier_tail_width(reason: str) -> int:
    widths = [int(item) for item in re.findall(r"barrier-tail:(\d+)", reason)]
    return max(widths) if widths else 0


def is_stage_generic_background_reason(reason: str) -> bool:
    return "stage-generic-background" in reason


def decide_adaptive_frontier_width(
    infos: Iterable[ProcInfo],
    ranked: list[tuple[int, int, str]],
    *,
    requested_frontier_width: int,
    requested_max_frontier_width: int,
    min_frontier_score: int,
    adaptive_frontier_width: bool,
    adaptive_max_frontier_width: int,
) -> AdaptiveWidthDecision:
    """Choose a bounded frontier width from the current process-tree shape.

    This is deliberately a small, explainable heuristic.  It does not infer a
    full DAG and it does not claim that every selected process is critical.  It
    only distinguishes narrow tail-frontier cases from wide cohort-frontier
    cases that the characterization traces exposed.
    """

    requested_width = max(1, requested_frontier_width)
    requested_max = max(1, requested_max_frontier_width)
    adaptive_cap = max(1, adaptive_max_frontier_width)
    if not adaptive_frontier_width:
        return AdaptiveWidthDecision(
            enabled=False,
            requested_frontier_width=requested_width,
            requested_max_frontier_width=requested_max,
            adaptive_max_frontier_width=adaptive_cap,
            frontier_width=requested_width,
            max_frontier_width=requested_max,
        )

    infos_by_pid = {info.pid: info for info in infos}
    min_score = max(0, min_frontier_score)
    eligible = [(score, pid, reason) for score, pid, reason in ranked if score >= min_score]
    serviceable_leaf_count = sum(
        1 for info in infos_by_pid.values() if info.leaf and is_cpu_serviceable(info)
    )
    parent_wait_candidate_count = sum(1 for _score, _pid, reason in eligible if "parent-wait:" in reason)
    max_barrier_tail = max((barrier_tail_width(reason) for _score, _pid, reason in eligible), default=0)

    width = requested_width
    reasons = [f"base={requested_width}"]
    cap = max(requested_width, adaptive_cap)

    if serviceable_leaf_count >= max(2, requested_width * 4):
        width = max(width, cap)
        reasons.append(f"wide-leaves={serviceable_leaf_count}")
    elif serviceable_leaf_count >= max(2, requested_width * 2):
        width = max(width, min(cap, requested_width * 2))
        reasons.append(f"moderate-leaves={serviceable_leaf_count}")

    if parent_wait_candidate_count >= max(2, requested_width * 2):
        width = max(width, min(cap, parent_wait_candidate_count))
        reasons.append(f"parent-wait-candidates={parent_wait_candidate_count}")

    if max_barrier_tail:
        width = max(width, min(cap, max_barrier_tail))
        reasons.append(f"barrier-tail={max_barrier_tail}")

    if eligible:
        width = min(width, max(requested_width, min(cap, len(eligible))))
    else:
        width = requested_width
        reasons.append("no-eligible-candidates")

    width = max(1, min(cap, width))
    return AdaptiveWidthDecision(
        enabled=True,
        requested_frontier_width=requested_width,
        requested_max_frontier_width=requested_max,
        adaptive_max_frontier_width=adaptive_cap,
        frontier_width=width,
        max_frontier_width=width,
        serviceable_leaf_count=serviceable_leaf_count,
        eligible_candidate_count=len(eligible),
        parent_wait_candidate_count=parent_wait_candidate_count,
        max_barrier_tail=max_barrier_tail,
        reason=",".join(reasons),
    )


def normalize_frontier_action(text: str | None) -> str:
    action = (text or "advance").strip().lower().replace("_", "-")
    if action == "observe-only":
        action = "observe"
    if action in {"stage", "stageaware", "stage-adaptive"}:
        action = "stage-aware"
    if action not in FRONTIER_ACTIONS:
        valid = ", ".join(sorted(FRONTIER_ACTIONS))
        raise ValueError(f"unknown frontier action: {text}; valid values: {valid}")
    return action


def normalize_frontier_stability_action(text: str | None) -> str:
    action = (text or "suppress").strip().lower().replace("_", "-")
    if action == "observe-only":
        action = "observe"
    if action not in FRONTIER_STABILITY_ACTIONS:
        valid = ", ".join(sorted(FRONTIER_STABILITY_ACTIONS))
        raise ValueError(f"unknown frontier stability action: {text}; valid values: {valid}")
    return action


def normalize_build_frontier_admission(text: str | None) -> str:
    mode = (text or "advance").strip().lower().replace("_", "-")
    if mode in {"off", "default", "none"}:
        mode = "advance"
    if mode in {"observe-only", "fallback", "fallback-only"}:
        mode = "observe"
    if mode in {"tail", "strong-tail", "tailonly"}:
        mode = "tail-only"
    if mode not in BUILD_FRONTIER_ADMISSIONS:
        valid = ", ".join(sorted(BUILD_FRONTIER_ADMISSIONS))
        raise ValueError(f"unknown build frontier admission mode: {text}; valid values: {valid}")
    return mode


def decide_frontier_action(
    infos: Iterable[ProcInfo],
    ranked: list[tuple[int, int, str]],
    frontier: set[int],
    width_decision: AdaptiveWidthDecision,
    *,
    stage: Stage = Stage.UNKNOWN,
    requested_action: str,
    explicit_policy_fallback: bool,
    min_frontier_score: int,
    build_frontier_admission: str = "advance",
    propagation: set[int] | None = None,
) -> dict[str, object]:
    """Choose whether this snapshot should advance or only observe a frontier.

    ``advance`` preserves the original FlowCausal behavior.  ``observe`` keeps
    the same selected frontier hints, but routes them through BPF's explicit
    fallback path so experiments can audit that the controller found a plausible
    frontier without granting preferential service.  ``auto`` is a conservative
    refusal gate for ambiguous online snapshots: it advances narrow, CPU-
    serviceable frontiers, but observes when no frontier is selected, when the
    selected frontier is not CPU-serviceable, or when a static narrow width is
    being applied to a much wider set of equally eligible candidates.  ``stage-
    aware`` keeps those safety gates, then admits only stronger stage-specific
    frontiers: BUILD link/archive or multi-tail barriers, TEST/RETRY drivers or
    multi-tail barriers.  Weak stage frontiers are suppressed rather than routed
    through a scheduling-affecting observe path.  When no concrete frontier is
    visible, it also suppresses propagation anchors; otherwise a generic
    sandbox driver can create hidden frontier boosts for fork/exec children.
    """

    action = normalize_frontier_action(requested_action)
    build_admission = normalize_build_frontier_admission(build_frontier_admission)
    infos_by_pid = {info.pid: info for info in infos}
    min_score = max(0, min_frontier_score)
    eligible = [(score, pid, reason) for score, pid, reason in ranked if score >= min_score]
    selected_rows = [(score, pid, reason) for score, pid, reason in ranked if pid in frontier]
    selected_serviceable = [
        pid
        for _score, pid, _reason in selected_rows
        if pid in infos_by_pid and is_cpu_serviceable(infos_by_pid[pid])
    ]
    selected_width = max(1, len(frontier))
    eligible_count = len(eligible)
    selected_max_barrier_tail = max((barrier_tail_width(reason) for _score, _pid, reason in selected_rows), default=0)
    selected_has_link_tail = any("link/archive tail" in reason for _score, _pid, reason in selected_rows)
    selected_has_test_driver = any("test driver" in reason for _score, _pid, reason in selected_rows)
    selected_has_only_test_workers = bool(selected_rows) and all(
        "test worker" in reason and "test driver" not in reason
        for _score, _pid, reason in selected_rows
    )
    build_admission_triggered = False
    build_admission_reason = ""
    task_observe_only_pids: list[int] = []
    task_suppress_frontier_pids: list[int] = []
    task_suppress_propagation_pids: list[int] = []
    build_tail_strong = selected_has_link_tail or selected_max_barrier_tail > 1
    propagation_count = len(propagation or set())
    has_ambiguous_wide_cohort = False
    score_margin = None
    if selected_rows and eligible_count > len(selected_rows):
        last_selected_score = min(score for score, _pid, _reason in selected_rows)
        next_scores = [score for score, pid, _reason in eligible if pid not in frontier]
        if next_scores:
            score_margin = last_selected_score - max(next_scores)
    if (
        not width_decision.enabled
        and eligible_count >= max(4, selected_width * 3)
        and score_margin is not None
        and score_margin <= 10
    ):
        has_ambiguous_wide_cohort = True

    reasons: list[str] = []
    final_action = "advance"
    if explicit_policy_fallback:
        final_action = "observe"
        reasons.append("explicit-policy-fallback")
    elif action == "observe":
        final_action = "observe"
        reasons.append("requested-observe")
    elif action == "auto":
        if not frontier:
            final_action = "observe"
            reasons.append("no-selected-frontier")
        elif not selected_serviceable:
            final_action = "observe"
            reasons.append("selected-frontier-not-cpu-serviceable")
        elif has_ambiguous_wide_cohort:
            final_action = "observe"
            reasons.append(
                f"ambiguous-wide-cohort:eligible={eligible_count},width={selected_width},margin={score_margin}"
            )
        elif (
            stage in {Stage.TEST, Stage.RETRY}
            and selected_has_only_test_workers
            and not selected_has_test_driver
            and selected_max_barrier_tail <= 1
        ):
            final_action = "observe"
            reasons.append(
                "weak-test-worker-frontier:"
                f"selected={len(selected_rows)},max_tail={selected_max_barrier_tail}"
            )
        else:
            reasons.append("auto-advance")
    elif action == "stage-aware":
        if not frontier:
            task_suppress_propagation_pids = sorted(propagation or set())
            reasons.append(
                f"stage-aware-no-frontier-suppress-propagation:anchors={propagation_count}"
                if propagation_count
                else "stage-aware-no-selected-frontier-neutral"
            )
        elif not selected_serviceable:
            task_suppress_frontier_pids = sorted(frontier)
            task_suppress_propagation_pids = sorted(propagation or set())
            reasons.append("stage-aware-selected-frontier-not-cpu-serviceable-suppress")
        elif has_ambiguous_wide_cohort:
            task_suppress_frontier_pids = sorted(frontier)
            task_suppress_propagation_pids = sorted(propagation or set())
            reasons.append(
                "stage-aware-ambiguous-wide-cohort-suppress:"
                f"eligible={eligible_count},width={selected_width},margin={score_margin}"
            )
        elif stage == Stage.BUILD:
            if build_tail_strong:
                reasons.append(
                    "stage-aware-build-tail-advance:"
                    f"max_tail={selected_max_barrier_tail},link_tail={int(selected_has_link_tail)}"
                )
            else:
                build_admission_triggered = True
                build_admission_reason = (
                    "stage-aware-build-tail-suppress:"
                    f"max_tail={selected_max_barrier_tail},link_tail={int(selected_has_link_tail)}"
                )
                task_suppress_frontier_pids = sorted(frontier)
                task_suppress_propagation_pids = sorted(propagation or set())
                reasons.append(build_admission_reason)
        elif stage in {Stage.TEST, Stage.RETRY}:
            if selected_has_test_driver:
                reasons.append(
                    "stage-aware-test-driver-advance:"
                    f"max_tail={selected_max_barrier_tail}"
                )
            elif selected_max_barrier_tail >= 2:
                reasons.append(
                    "stage-aware-test-barrier-tail-advance:"
                    f"max_tail={selected_max_barrier_tail}"
                )
            else:
                task_suppress_frontier_pids = sorted(frontier)
                task_suppress_propagation_pids = sorted(propagation or set())
                reasons.append(
                    "stage-aware-test-worker-suppress:"
                    f"selected={len(selected_rows)},max_tail={selected_max_barrier_tail}"
                )
        else:
            task_suppress_frontier_pids = sorted(frontier)
            task_suppress_propagation_pids = sorted(propagation or set())
            reasons.append(f"stage-aware-{stage.name.lower()}-suppress")
    else:
        reasons.append("requested-advance")

    if (
        stage == Stage.BUILD
        and frontier
        and final_action == "advance"
        and build_admission != "advance"
    ):
        if build_admission == "observe":
            build_admission_triggered = True
            build_admission_reason = "build-admission-observe"
            task_observe_only_pids = sorted(frontier)
            reasons.append(build_admission_reason)
        elif build_admission == "tail-only" and not build_tail_strong:
            build_admission_triggered = True
            build_admission_reason = (
                f"build-tail-only-observe:max_tail={selected_max_barrier_tail},"
                f"link_tail={int(selected_has_link_tail)}"
            )
            task_observe_only_pids = sorted(frontier)
            reasons.append(build_admission_reason)
        elif build_admission == "tail-only":
            build_admission_reason = (
                f"build-tail-only-advance:max_tail={selected_max_barrier_tail},"
                f"link_tail={int(selected_has_link_tail)}"
            )
            reasons.append(build_admission_reason)

    return {
        "requested": action,
        "action": final_action,
        "policy_fallback": final_action == "observe",
        "reason": ",".join(reasons),
        "stage": stage.name,
        "eligible_candidates": eligible_count,
        "selected_count": len(frontier),
        "selected_serviceable_count": len(selected_serviceable),
        "score_margin": score_margin,
        "task_observe_only_pids": task_observe_only_pids,
        "task_suppress_frontier_pids": task_suppress_frontier_pids,
        "task_suppress_propagation_pids": task_suppress_propagation_pids,
        "build_frontier_admission": build_admission,
        "effective_build_frontier_admission": "tail-only" if action == "stage-aware" else build_admission,
        "build_admission_triggered": build_admission_triggered,
        "build_admission_reason": build_admission_reason,
        "build_selected_max_barrier_tail": selected_max_barrier_tail,
        "build_selected_link_tail": selected_has_link_tail,
        "test_selected_max_barrier_tail": selected_max_barrier_tail if stage in {Stage.TEST, Stage.RETRY} else 0,
        "test_selected_driver": selected_has_test_driver,
        "test_selected_worker_only": selected_has_only_test_workers if stage in {Stage.TEST, Stage.RETRY} else False,
    }


def apply_frontier_action(plan: HintPlan, action: dict[str, object]) -> HintPlan:
    """Materialize an action decision into policy and task flags."""

    policy_flags = plan.policy.flags
    if action.get("policy_fallback"):
        policy_flags |= POLICY_FALLBACK
    else:
        policy_flags &= ~POLICY_FALLBACK

    hints: list[TaskHint] = []
    task_observe_only_pids = {
        int(pid)
        for pid in action.get("task_observe_only_pids", [])
        if isinstance(pid, int)
    }
    task_suppress_frontier_pids = {
        int(pid)
        for pid in action.get("task_suppress_frontier_pids", [])
        if isinstance(pid, int)
    }
    task_suppress_propagation_pids = {
        int(pid)
        for pid in action.get("task_suppress_propagation_pids", [])
        if isinstance(pid, int)
    }
    for hint in plan.hints:
        flags = hint.flags & ~TASK_OBSERVE_ONLY
        if hint.pid in task_suppress_frontier_pids:
            flags &= ~(TASK_FRONTIER | TASK_COHORT_TAIL)
        if hint.pid in task_suppress_propagation_pids:
            flags &= ~TASK_PROPAGATE
        if (action.get("action") == "observe" and (flags & TASK_FRONTIER)) or hint.pid in task_observe_only_pids:
            flags |= TASK_OBSERVE_ONLY
        propagation_depth = hint.propagation_depth
        if hint.pid in task_suppress_propagation_pids:
            propagation_depth = 0
        hints.append(dataclasses.replace(hint, flags=flags, propagation_depth=propagation_depth))

    return dataclasses.replace(
        plan,
        policy=dataclasses.replace(plan.policy, flags=policy_flags),
        hints=hints,
        frontier_pids=plan.frontier_pids - task_suppress_frontier_pids,
        propagation_pids=plan.propagation_pids - task_suppress_propagation_pids,
        action=action,
    )


def frontier_propagation_anchors(
    frontier_pids: Iterable[int],
    infos_by_pid: dict[int, ProcInfo],
    root_pid: int,
) -> set[int]:
    anchors: set[int] = set()
    for frontier_pid in frontier_pids:
        current = infos_by_pid.get(frontier_pid)
        seen: set[int] = set()
        while current is not None and current.ppid in infos_by_pid:
            parent_pid = current.ppid
            if parent_pid in seen:
                break
            seen.add(parent_pid)
            if parent_pid == root_pid:
                break
            anchors.add(parent_pid)
            current = infos_by_pid.get(parent_pid)
    return anchors


def spawn_propagation_anchors(
    root_pid: int,
    infos_by_pid: dict[int, ProcInfo],
    frontier_pids: set[int],
) -> set[int]:
    """Select conservative fork-time anchors when polling has no frontier yet.

    A sandbox driver may spend most samples as a sleeping Python/shell process
    and then fork a short-lived worker between two controller ticks.  Marking
    the driver as a propagation anchor is weaker than boosting it: the driver
    itself remains on the shared path, but a direct child can inherit a short
    frontier hint at fork time.  This is enabled only by
    ``--frontier-propagation`` and only when no concrete frontier is currently
    selected.
    """

    if frontier_pids:
        return set()
    root = infos_by_pid.get(root_pid)
    if root is None:
        return set()
    if is_timer_wait(root):
        return set()
    if root.state not in {"R", "S", "D"}:
        return set()
    text = f"{root.comm} {root.command}".lower()
    tokens = command_tokens(root.command, root.comm)
    if (
        has_token(tokens, "python", "python3", "bash", "sh", "node", "ruby")
        or "sandbox" in text
        or "agent" in text
        or "runner" in text
    ):
        return {root_pid}
    return set()


def decide_adaptive_propagation_depth(
    *,
    frontier_propagation: bool,
    requested_depth: int,
    adaptive: bool,
    stage_aware: bool = False,
    stage: Stage = Stage.UNKNOWN,
    frontier_pids: set[int],
    propagation_pids: set[int],
    ranked: list[tuple[int, int, str]] | None = None,
    min_frontier_score: int = MIN_FRONTIER_SCORE,
) -> AdaptivePropagationDepthDecision:
    requested = max(0, requested_depth)
    ranked = ranked or []
    selected_reasons = [reason for score, pid, reason in ranked if pid in frontier_pids]
    eligible_candidate_count = sum(
        1 for score, _pid, reason in ranked
        if score >= max(0, min_frontier_score) and not is_stage_generic_background_reason(reason)
    )
    selected_parent_wait_count = sum(1 for reason in selected_reasons if "parent-wait:" in reason)
    selected_max_barrier_tail = max((barrier_tail_width(reason) for reason in selected_reasons), default=0)
    policy_name = "stage-aware" if stage_aware else ("adaptive" if adaptive else "static")
    if not frontier_propagation:
        return AdaptivePropagationDepthDecision(
            enabled=adaptive or stage_aware,
            requested_depth=requested,
            propagation_depth=0,
            frontier_count=len(frontier_pids),
            anchor_count=len(propagation_pids),
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason="propagation-disabled",
        )
    if not adaptive and not stage_aware:
        return AdaptivePropagationDepthDecision(
            enabled=False,
            requested_depth=requested,
            propagation_depth=requested,
            frontier_count=len(frontier_pids),
            anchor_count=len(propagation_pids),
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason="static",
        )
    if requested <= 1:
        return AdaptivePropagationDepthDecision(
            enabled=True,
            requested_depth=requested,
            propagation_depth=requested,
            frontier_count=len(frontier_pids),
            anchor_count=len(propagation_pids),
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason="requested-depth<=1",
        )
    if not propagation_pids:
        return AdaptivePropagationDepthDecision(
            enabled=True,
            requested_depth=requested,
            propagation_depth=0,
            frontier_count=len(frontier_pids),
            anchor_count=0,
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason="no-propagation-anchors",
        )
    if stage_aware:
        if not frontier_pids:
            return AdaptivePropagationDepthDecision(
                enabled=True,
                requested_depth=requested,
                propagation_depth=requested,
                frontier_count=0,
                anchor_count=len(propagation_pids),
                stage=stage.name,
                eligible_candidate_count=eligible_candidate_count,
                selected_parent_wait_count=selected_parent_wait_count,
                selected_max_barrier_tail=selected_max_barrier_tail,
                policy=policy_name,
                reason="stage-aware-spawn-anchor-use-requested-depth",
            )
        if stage == Stage.BUILD and selected_max_barrier_tail != 1:
            return AdaptivePropagationDepthDecision(
                enabled=True,
                requested_depth=requested,
                propagation_depth=0,
                frontier_count=len(frontier_pids),
                anchor_count=len(propagation_pids),
                stage=stage.name,
                eligible_candidate_count=eligible_candidate_count,
                selected_parent_wait_count=selected_parent_wait_count,
                selected_max_barrier_tail=selected_max_barrier_tail,
                policy=policy_name,
                reason="stage-aware-build-wide-or-weak-tail-depth0",
            )
        if stage == Stage.BUILD:
            return AdaptivePropagationDepthDecision(
                enabled=True,
                requested_depth=requested,
                propagation_depth=1,
                frontier_count=len(frontier_pids),
                anchor_count=len(propagation_pids),
                stage=stage.name,
                eligible_candidate_count=eligible_candidate_count,
                selected_parent_wait_count=selected_parent_wait_count,
                selected_max_barrier_tail=selected_max_barrier_tail,
                policy=policy_name,
                reason="stage-aware-build-single-tail-depth1",
            )
        if stage in {Stage.TEST, Stage.RETRY}:
            reason = (
                "stage-aware-test-barrier-depth1"
                if selected_max_barrier_tail
                else "stage-aware-test-parent-wait-depth1"
                if selected_parent_wait_count
                else "stage-aware-test-concrete-depth1"
            )
            return AdaptivePropagationDepthDecision(
                enabled=True,
                requested_depth=requested,
                propagation_depth=1,
                frontier_count=len(frontier_pids),
                anchor_count=len(propagation_pids),
                stage=stage.name,
                eligible_candidate_count=eligible_candidate_count,
                selected_parent_wait_count=selected_parent_wait_count,
                selected_max_barrier_tail=selected_max_barrier_tail,
                policy=policy_name,
                reason=reason,
            )
        return AdaptivePropagationDepthDecision(
            enabled=True,
            requested_depth=requested,
            propagation_depth=0,
            frontier_count=len(frontier_pids),
            anchor_count=len(propagation_pids),
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason=f"stage-aware-{stage.name.lower()}-depth0",
        )
    if frontier_pids:
        return AdaptivePropagationDepthDecision(
            enabled=True,
            requested_depth=requested,
            propagation_depth=1,
            frontier_count=len(frontier_pids),
            anchor_count=len(propagation_pids),
            stage=stage.name,
            eligible_candidate_count=eligible_candidate_count,
            selected_parent_wait_count=selected_parent_wait_count,
            selected_max_barrier_tail=selected_max_barrier_tail,
            policy=policy_name,
            reason="concrete-frontier-depth1",
        )
    return AdaptivePropagationDepthDecision(
        enabled=True,
        requested_depth=requested,
        propagation_depth=requested,
        frontier_count=0,
        anchor_count=len(propagation_pids),
        stage=stage.name,
        eligible_candidate_count=eligible_candidate_count,
        selected_parent_wait_count=selected_parent_wait_count,
        selected_max_barrier_tail=selected_max_barrier_tail,
        policy=policy_name,
        reason="spawn-anchor-use-requested-depth",
    )


def frontier_propagation_anchors_from_hints(
    frontier_pids: Iterable[int],
    hints_by_pid: dict[int, TaskHint],
    root_pid: int,
) -> set[int]:
    anchors: set[int] = set()
    for frontier_pid in frontier_pids:
        current = hints_by_pid.get(frontier_pid)
        seen: set[int] = set()
        while current is not None and current.parent_pid in hints_by_pid:
            parent_pid = current.parent_pid
            if parent_pid in seen:
                break
            seen.add(parent_pid)
            if parent_pid == root_pid:
                break
            anchors.add(parent_pid)
            current = hints_by_pid.get(parent_pid)
    return anchors


def apply_frontier_stability(
    plan: HintPlan,
    state: FrontierStabilityState,
    *,
    frontier_width: int,
    min_frontier_score: int,
    stability_samples: int,
    hold_ms: int,
    bypass_score: int,
    selected_only: bool = False,
    stability_action: str = "suppress",
) -> HintPlan:
    """Apply optional debounce/hysteresis to an online frontier plan.

    This is controller-side admission hygiene, not a hidden oracle.  It never
    creates frontier candidates that the scorer did not rank above the minimum
    score.  It only delays weak, newly observed candidates, lets very strong
    evidence pass immediately, and keeps a previous frontier for a short hold
    interval while that pid remains a valid candidate.

    The default mode counts any eligible candidate.  ``selected_only`` is a
    stricter tail-strength gate: only the frontier pids selected by the current
    snapshot accrue stability.  This refuses churny top-1 frontiers that appear
    in short sandbox stages, while letting persistent critical tails through.

    The default ``suppress`` action preserves the original debounce behavior:
    unstable candidates are removed from the live hint set.  ``observe`` keeps
    the current selected frontier hints when none of them is stable yet, but
    routes the session through fallback so BPF observes the same workflow
    context without granting preferential frontier service.  This is useful for
    endpoint replay experiments where deleting an unstable frontier also deletes
    the propagation context needed to understand short-lived children.
    """

    stability_samples = max(1, stability_samples)
    hold_ms = max(0, hold_ms)
    bypass_score = max(0, bypass_score)
    stability_action = normalize_frontier_stability_action(stability_action)
    if stability_samples <= 1 and hold_ms <= 0:
        return plan

    width = max(1, frontier_width)
    min_score = max(0, min_frontier_score)
    now = time.monotonic_ns()
    hold_ns = hold_ms * 1_000_000
    candidate_rows = [
        (score, pid, reason)
        for score, pid, reason in plan.ranked_frontiers
        if score >= min_score and pid in plan.proc_comms
    ]
    live_candidate_pids = {pid for _, pid, _ in candidate_rows}
    planned_frontier_pids = set(plan.frontier_pids)
    tracked_rows = [
        (score, pid, reason)
        for score, pid, reason in candidate_rows
        if not selected_only or pid in planned_frontier_pids
    ]
    tracked_pids = {pid for _, pid, _ in tracked_rows}

    for pid in list(state.seen_ticks):
        if pid not in tracked_pids:
            state.seen_ticks.pop(pid, None)
            state.held_until_ns.pop(pid, None)
    for pid in tracked_pids:
        state.seen_ticks[pid] = state.seen_ticks.get(pid, 0) + 1

    held = {
        pid
        for pid in state.active_frontiers
        if pid in live_candidate_pids and state.held_until_ns.get(pid, 0) >= now
    }

    eligible: set[int] = set()
    suppressed: list[dict[str, object]] = []
    admission_rows = [
        (score, pid, reason)
        for score, pid, reason in candidate_rows
        if not selected_only or pid in tracked_pids or pid in held
    ]
    for score, pid, reason in admission_rows:
        seen = state.seen_ticks.get(pid, 0)
        strong = bypass_score > 0 and score >= bypass_score
        is_held = pid in held
        if seen >= stability_samples or strong or is_held:
            eligible.add(pid)
        else:
            suppressed.append(
                {
                    "pid": pid,
                    "score": score,
                    "seen": seen,
                    "reason": reason,
                    "comm": plan.proc_comms.get(pid, ""),
                }
            )

    selected_order: list[int] = []
    for _, pid, _ in admission_rows:
        if len(selected_order) >= width:
            break
        if pid in held and pid in eligible and pid not in selected_order:
            selected_order.append(pid)
    for _, pid, _ in admission_rows:
        if len(selected_order) >= width:
            break
        if pid in eligible and pid not in selected_order:
            selected_order.append(pid)

    stable_selected = set(selected_order)
    suppressed_pids = {
        int(item["pid"])
        for item in suppressed
        if isinstance(item.get("pid"), int)
    }
    observed_unstable_order: list[int] = []
    unstable_observe = False
    if stability_action == "observe" and not stable_selected and planned_frontier_pids:
        for _, pid, _ in candidate_rows:
            if len(observed_unstable_order) >= width:
                break
            if pid in planned_frontier_pids and pid in suppressed_pids and pid not in observed_unstable_order:
                observed_unstable_order.append(pid)
        unstable_observe = bool(observed_unstable_order)

    selected = set(observed_unstable_order) if unstable_observe else stable_selected
    state.active_frontiers = stable_selected
    if hold_ns > 0:
        for pid in stable_selected:
            state.held_until_ns[pid] = now + hold_ns
    for pid in list(state.held_until_ns):
        if pid not in live_candidate_pids and pid not in stable_selected:
            state.held_until_ns.pop(pid, None)

    original_hints = {hint.pid: hint for hint in plan.hints}
    if plan.propagation_pids and not plan.frontier_pids:
        propagation = set(plan.propagation_pids)
    elif plan.propagation_pids:
        propagation = frontier_propagation_anchors_from_hints(selected, original_hints, plan.root_pid)
    else:
        propagation = set()

    hints: list[TaskHint] = []
    for hint in plan.hints:
        flags = hint.flags & TASK_LOCALITY
        confidence = 0
        if hint.pid in selected:
            flags |= TASK_FRONTIER | TASK_COHORT_TAIL
            confidence = hint.confidence if hint.confidence > 0 else plan.policy.min_confidence
        if hint.pid in propagation:
            flags |= TASK_PROPAGATE
            if confidence <= 0:
                confidence = plan.policy.min_confidence
        hints.append(
            dataclasses.replace(
                hint,
                flags=flags,
                confidence=confidence,
                propagation_depth=hint.propagation_depth if hint.pid in propagation else 0,
            )
        )

    diagnostic = {
        "enabled": True,
        "samples_required": stability_samples,
        "hold_ms": hold_ms,
        "bypass_score": bypass_score,
        "mode": "selected" if selected_only else "candidate",
        "action": stability_action,
        "candidates": len(admission_rows),
        "ranked_candidates": len(candidate_rows),
        "selected": sorted(selected),
        "stable_selected": sorted(stable_selected),
        "observed_unstable": sorted(observed_unstable_order),
        "unstable_observe": unstable_observe,
        "held": sorted(held & selected),
        "suppressed": suppressed[:8],
        "suppressed_count": len(suppressed),
    }
    updated = dataclasses.replace(
        plan,
        hints=hints,
        frontier_pids=selected,
        propagation_pids=propagation,
        stability=diagnostic,
    )
    if plan.action:
        action = dict(plan.action)
        action["selected_count"] = len(selected)
        if unstable_observe:
            action["action"] = "observe"
            action["policy_fallback"] = True
            reason = str(action.get("reason") or "")
            action["reason"] = (
                f"{reason},stability-observe-until-stable"
                if reason
                else "stability-observe-until-stable"
            )
        elif action.get("requested") == "auto" and not selected:
            action["action"] = "observe"
            action["policy_fallback"] = True
            reason = str(action.get("reason") or "")
            action["reason"] = (
                f"{reason},stability-no-selected-frontier"
                if reason
                else "stability-no-selected-frontier"
            )
        return apply_frontier_action(updated, action)
    return updated


def bpftool_update(map_path: str, key: bytes, value: bytes) -> None:
    key_hex = " ".join(f"{b:02x}" for b in key)
    value_hex = " ".join(f"{b:02x}" for b in value)
    cmd = [
        "bpftool",
        "map",
        "update",
        "pinned",
        map_path,
        "key",
        "hex",
        *key_hex.split(),
        "value",
        "hex",
        *value_hex.split(),
    ]
    subprocess.run(cmd, check=True)


def bpftool_delete(map_path: str, key: bytes) -> None:
    key_hex = " ".join(f"{b:02x}" for b in key)
    cmd = [
        "bpftool",
        "map",
        "delete",
        "pinned",
        map_path,
        "key",
        "hex",
        *key_hex.split(),
    ]
    subprocess.run(cmd, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def bpftool_lookup(map_path: str, key: bytes) -> bytes | None:
    key_hex = " ".join(f"{b:02x}" for b in key)
    cmd = [
        "bpftool",
        "-j",
        "map",
        "lookup",
        "pinned",
        map_path,
        "key",
        "hex",
        *key_hex.split(),
    ]
    proc = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if proc.returncode != 0:
        return None
    try:
        data = json.loads(proc.stdout)
        item = data[0] if isinstance(data, list) else data
        value = item.get("value") if isinstance(item, dict) else None
        if isinstance(value, list):
            return bytes(int(str(byte), 16) for byte in value)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    _, sep, value_text = proc.stdout.partition("value:")
    if not sep:
        return None
    bytes_hex = re.findall(r"\b[0-9a-fA-F]{2}\b", value_text)
    if not bytes_hex:
        return None
    return bytes(int(item, 16) for item in bytes_hex)


def unpack_session_policy(session_id: int, value: bytes | None) -> SessionPolicy | None:
    if value is None:
        return None
    fmt = "IIIIQQQQQiI"
    size = struct.calcsize(fmt)
    if len(value) < size:
        return None
    fields = struct.unpack(fmt, value[:size])
    return SessionPolicy(
        session_id=session_id,
        flags=fields[0],
        min_confidence=fields[1],
        max_frontier_width=fields[2],
        max_vtime_boost_ns=fields[3],
        window_ns=fields[4],
        boost_budget_ns=fields[5],
        budget_used_ns=fields[6],
        budget_window_start_ns=fields[7],
        expires_ns=fields[8],
        anchor_cpu=fields[9],
    )


def unpack_task_ctx(pid: int, value: bytes | None) -> TaskHint | None:
    if value is None:
        return None
    fmt = "QQQQQQQQIIIIiI"
    size = struct.calcsize(fmt)
    if len(value) < size:
        return None
    fields = struct.unpack(fmt, value[:size])
    return TaskHint(
        pid=pid,
        tgid=fields[8],
        parent_pid=fields[9],
        session_id=fields[0],
        stage_id=fields[1],
        cohort_id=fields[2],
        enqueue_ts_ns=fields[3],
        running_since_ns=fields[4],
        runnable_wait_ns=fields[5],
        runtime_ns=fields[6],
        last_update_ns=fields[7],
        flags=fields[10],
        confidence=fields[11],
        anchor_cpu=fields[12],
        propagation_depth=fields[13],
    )


def merge_existing_task_hint(task_map: pathlib.Path, hint: TaskHint) -> TaskHint:
    existing = unpack_task_ctx(
        hint.pid,
        bpftool_lookup(str(task_map), hint.pack_key()),
    )
    if existing is None:
        return hint

    # Preserve BPF-owned per-task accounting fields, but never preserve old
    # semantic hint flags.  The current snapshot must be able to demote a
    # previous frontier/propagation anchor; otherwise a live policy refresh can
    # keep stale frontiers eligible until process exit.
    return dataclasses.replace(
        hint,
        enqueue_ts_ns=existing.enqueue_ts_ns,
        running_since_ns=existing.running_since_ns,
        runnable_wait_ns=existing.runnable_wait_ns,
        runtime_ns=existing.runtime_ns,
        last_update_ns=existing.last_update_ns,
    )


def merge_existing_policy(policy_map: pathlib.Path, policy: SessionPolicy) -> SessionPolicy:
    existing = unpack_session_policy(
        policy.session_id,
        bpftool_lookup(str(policy_map), policy.pack_key()),
    )
    if existing is None:
        return policy

    # Preserve only kernel-owned budget accounting across fast user-space
    # refreshes.  Semantic policy flags, including explicit fallback, must come
    # from the current controller decision; otherwise a safety fallback run
    # would become sticky and could not be cleared by the next update.
    return dataclasses.replace(
        policy,
        budget_used_ns=existing.budget_used_ns,
        budget_window_start_ns=existing.budget_window_start_ns,
    )


def is_effective_task_hint(hint: TaskHint) -> bool:
    return bool(hint.flags & TASK_EFFECTIVE_HINT_MASK)


def inaccessible_or_missing(paths: Iterable[pathlib.Path]) -> list[str]:
    bad: list[str] = []
    for path in paths:
        try:
            if not path.exists():
                bad.append(str(path))
        except OSError as exc:
            bad.append(f"{path} ({exc.strerror})")
    return bad


def parse_stage_kind(text: str | None) -> Stage:
    if not text:
        return Stage.SHELL
    normalized = normalize_stage_token(text)
    if normalized.isdigit():
        try:
            return Stage(int(normalized))
        except ValueError as exc:
            raise ValueError(f"unknown stage kind: {text}") from exc
    try:
        return Stage[normalized]
    except KeyError as exc:
        valid = ", ".join(s.name for s in Stage)
        raise ValueError(f"unknown stage kind: {text}; valid values: {valid}") from exc


def parse_optional_stage_kind(text: str | None) -> Stage | None:
    if not text:
        return None
    normalized = normalize_stage_token(text)
    if normalized in {"AUTO", "INFER", "INFERRED"}:
        return None
    return parse_stage_kind(text)


def clamp_confidence(value: int) -> int:
    return max(0, min(value, 100))


def update_maps(
    task_map: pathlib.Path,
    policy_map: pathlib.Path,
    policy: SessionPolicy,
    hints: list[TaskHint],
    *,
    known_task_pids: set[int] | None = None,
    force_delete_pids: set[int] | None = None,
) -> SessionPolicy:
    force_delete_pids = force_delete_pids or set()
    effective_hints = [hint for hint in hints if is_effective_task_hint(hint)]
    delete_pids: set[int] = set()
    for hint in hints:
        if is_effective_task_hint(hint):
            continue
        if known_task_pids is None or hint.pid in known_task_pids:
            delete_pids.add(hint.pid)

    # Watch mode sees many short-lived tasks that are explicitly suppressed by
    # the stage-aware policy before they were ever written to task_ctx.  Issuing
    # bpftool delete for those unknown pids is semantically a no-op but costs a
    # fork/exec per pid per tick.  If this tick has no effective hints and no
    # known task entries to remove, avoid touching BPF maps altogether.
    if not effective_hints and not delete_pids:
        return policy

    if effective_hints:
        policy = merge_existing_policy(policy_map, policy)
        bpftool_update(str(policy_map), policy.pack_key(), policy.pack_value())

    for hint in hints:
        if not is_effective_task_hint(hint):
            if hint.pid in delete_pids:
                bpftool_delete(str(task_map), hint.pack_key())
            if known_task_pids is not None:
                known_task_pids.discard(hint.pid)
            continue
        if known_task_pids is not None and hint.pid not in known_task_pids:
            merged = hint
        else:
            merged = merge_existing_task_hint(task_map, hint)
        bpftool_update(str(task_map), merged.pack_key(), merged.pack_value())
        if known_task_pids is not None:
            known_task_pids.add(hint.pid)
    return policy


def plan_record(plan: HintPlan, tick: int) -> dict[str, object]:
    hint_by_pid = {hint.pid: hint for hint in plan.hints}
    selected = []
    for score, pid, reason in plan.ranked_frontiers:
        if pid not in plan.frontier_pids:
            continue
        selected.append(
            {
                "pid": pid,
                "score": score,
                "reason": reason,
                "comm": plan.proc_comms.get(pid) or comm_of(pid),
                "confidence": hint_by_pid[pid].confidence if pid in hint_by_pid else 0,
            }
        )
    propagation = [
        {
            "pid": pid,
            "comm": plan.proc_comms.get(pid) or comm_of(pid),
        }
        for pid in sorted(plan.propagation_pids)
    ]
    record: dict[str, object] = {
        "type": "flowctl_watch",
        "tick": tick,
        "time_ns": time.time_ns(),
        "session_id": plan.policy.session_id,
        "stage": plan.stage.name,
        "stage_source": plan.stage_source,
        "stage_label": plan.stage_label,
        "tasks": len(plan.hints),
        "frontiers": selected,
        "propagation_anchors": propagation,
        "policy": dataclasses.asdict(plan.policy),
    }
    if plan.stability:
        record["stability"] = plan.stability
    if plan.adaptive_width:
        record["adaptive_width"] = plan.adaptive_width
    if plan.adaptive_propagation:
        record["adaptive_propagation"] = plan.adaptive_propagation
    if plan.action:
        record["frontier_action"] = plan.action
    return record


def print_plan(plan: HintPlan, *, tick: int | None = None) -> None:
    prefix = f"tick={tick} " if tick is not None else ""
    stage_detail = f"{plan.stage.name}/{plan.stage_source}" if plan.stage_source else plan.stage.name
    print(
        f"{prefix}session_id={plan.policy.session_id} stage={stage_detail} "
        f"tasks={len(plan.hints)} policy={dataclasses.asdict(plan.policy)}"
    )
    frontier_reasons = {pid: (score, reason) for score, pid, reason in plan.ranked_frontiers}
    for hint in plan.hints:
        if hint.flags & TASK_FRONTIER:
            marker = "FRONTIER"
        elif hint.flags & TASK_PROPAGATE:
            marker = "ANCHOR"
        else:
            marker = "cohort"
        if hint.pid in frontier_reasons:
            score, reason = frontier_reasons[hint.pid]
            detail = f" score={score:<3d} reason={reason}"
        else:
            detail = ""
        print(
            f"{marker:8s} pid={hint.pid:<7d} ppid={hint.parent_pid:<7d} "
            f"conf={hint.confidence:<3d} comm={plan.proc_comms.get(hint.pid) or comm_of(hint.pid)}{detail}"
        )
    if plan.stability:
        suppressed = plan.stability.get("suppressed_count", 0)
        held = plan.stability.get("held", [])
        print(
            "stability "
            f"samples={plan.stability.get('samples_required')} "
            f"hold_ms={plan.stability.get('hold_ms')} "
            f"bypass_score={plan.stability.get('bypass_score')} "
            f"suppressed={suppressed} held={held}"
        )
    if plan.adaptive_width:
        print(
            "adaptive_width "
            f"enabled={plan.adaptive_width.get('enabled')} "
            f"frontier_width={plan.adaptive_width.get('frontier_width')} "
            f"max_frontier_width={plan.adaptive_width.get('max_frontier_width')} "
            f"reason={plan.adaptive_width.get('reason')}"
        )
    if plan.adaptive_propagation:
        print(
            "adaptive_propagation "
            f"enabled={plan.adaptive_propagation.get('enabled')} "
            f"depth={plan.adaptive_propagation.get('propagation_depth')} "
            f"requested={plan.adaptive_propagation.get('requested_depth')} "
            f"anchors={plan.adaptive_propagation.get('anchor_count')} "
            f"reason={plan.adaptive_propagation.get('reason')}"
        )
    if plan.action:
        print(
            "frontier_action "
            f"requested={plan.action.get('requested')} "
            f"action={plan.action.get('action')} "
            f"fallback={plan.action.get('policy_fallback')} "
            f"reason={plan.action.get('reason')}"
        )


def watch_subtree(
    *,
    root_pid: int,
    session: str,
    confidence: int,
    anchor_cpu: int,
    ttl_ms: int,
    policy_window_ms: int,
    boost_budget_ms: int,
    max_vtime_boost_us: int,
    width_control: bool,
    max_frontier_width: int,
    frontier_width: int,
    adaptive_frontier_width: bool,
    adaptive_max_frontier_width: int,
    min_frontier_score: int,
    frontier_propagation: bool,
    frontier_propagation_depth: int,
    adaptive_frontier_propagation_depth: bool,
    stage_aware_frontier_propagation_depth: bool,
    frontier_stability_samples: int,
    frontier_hold_ms: int,
    frontier_bypass_score: int,
    frontier_selected_stability: bool,
    frontier_stability_action: str,
    policy_fallback: bool,
    frontier_action: str,
    build_frontier_admission: str,
    background_hints: bool,
    forced_stage: Stage | None,
    interval_ms: int,
    duration_sec: float | None,
    dry_run: bool,
    task_map: pathlib.Path,
    policy_map: pathlib.Path,
    json_log: TextIO | None,
    stage_state_file: pathlib.Path | None = None,
) -> int:
    deadline = time.monotonic() + duration_sec if duration_sec and duration_sec > 0 else None
    tick = 0
    stability_state = FrontierStabilityState()
    known_task_pids: set[int] = set()
    while True:
        if not pathlib.Path(f"/proc/{root_pid}").exists():
            print(f"root pid {root_pid} exited; stopping watch")
            return 0
        if deadline is not None and time.monotonic() >= deadline:
            print(f"duration {duration_sec:.3f}s elapsed; stopping watch")
            return 0

        plan = build_hint_plan(
            root_pid,
            session,
            confidence,
            anchor_cpu,
            ttl_ms,
            policy_window_ms,
            boost_budget_ms,
            max_vtime_boost_us,
            width_control,
            max_frontier_width,
            frontier_width,
            min_frontier_score,
            frontier_propagation,
            frontier_propagation_depth,
            policy_fallback,
            forced_stage,
            adaptive_frontier_width,
            adaptive_max_frontier_width,
            frontier_action,
            adaptive_frontier_propagation_depth=adaptive_frontier_propagation_depth,
            stage_aware_frontier_propagation_depth=stage_aware_frontier_propagation_depth,
            build_frontier_admission=build_frontier_admission,
            stage_state_file=stage_state_file,
            background_hints=background_hints,
        )
        plan = apply_frontier_stability(
            plan,
            stability_state,
            frontier_width=int(plan.adaptive_width.get("frontier_width", frontier_width))
            if plan.adaptive_width
            else frontier_width,
            min_frontier_score=min_frontier_score,
            stability_samples=frontier_stability_samples,
            hold_ms=frontier_hold_ms,
            bypass_score=frontier_bypass_score,
            selected_only=frontier_selected_stability,
            stability_action=frontier_stability_action,
        )
        if dry_run:
            print_plan(plan, tick=tick)
        else:
            action = plan.action if isinstance(plan.action, dict) else {}
            force_delete_pids = {
                int(pid)
                for key in ("task_suppress_frontier_pids", "task_suppress_propagation_pids")
                for pid in action.get(key, [])
                if isinstance(pid, int)
            }
            effective_policy = update_maps(
                task_map,
                policy_map,
                plan.policy,
                plan.hints,
                known_task_pids=known_task_pids,
                force_delete_pids=force_delete_pids,
            )
            plan = dataclasses.replace(plan, policy=effective_policy)
            print(
                f"tick={tick} updated {len(plan.hints)} task hints "
                f"frontiers={sorted(plan.frontier_pids)} stage={plan.stage.name} "
                f"ttl_ms={max(1, ttl_ms)}"
            )

        if json_log is not None:
            json_log.write(json.dumps(plan_record(plan, tick), sort_keys=True) + "\n")
            json_log.flush()

        tick += 1
        time.sleep(max(1, interval_ms) / 1000.0)


def build_hints(
    root_pid: int,
    session_name: str,
    confidence: int,
    anchor_cpu: int,
    ttl_ms: int,
    policy_window_ms: int,
    boost_budget_ms: int,
    max_vtime_boost_us: int,
    width_control: bool,
    max_frontier_width: int,
    frontier_width: int = 1,
    min_frontier_score: int = MIN_FRONTIER_SCORE,
    frontier_propagation: bool = False,
    frontier_propagation_depth: int = 2,
    policy_fallback: bool = False,
    forced_stage: Stage | None = None,
    adaptive_frontier_width: bool = False,
    adaptive_max_frontier_width: int = 8,
    frontier_action: str = "advance",
    adaptive_frontier_propagation_depth: bool = False,
    stage_aware_frontier_propagation_depth: bool = False,
    background_hints: bool = True,
) -> tuple[SessionPolicy, list[TaskHint]]:
    plan = build_hint_plan(
        root_pid,
        session_name,
        confidence,
        anchor_cpu,
        ttl_ms,
        policy_window_ms,
        boost_budget_ms,
        max_vtime_boost_us,
        width_control,
        max_frontier_width,
        frontier_width,
        min_frontier_score,
        frontier_propagation,
        frontier_propagation_depth,
        policy_fallback,
        forced_stage,
        adaptive_frontier_width,
        adaptive_max_frontier_width,
        frontier_action,
        adaptive_frontier_propagation_depth=adaptive_frontier_propagation_depth,
        stage_aware_frontier_propagation_depth=stage_aware_frontier_propagation_depth,
        background_hints=background_hints,
    )
    return plan.policy, plan.hints


def build_hint_plan(
    root_pid: int,
    session_name: str,
    confidence: int,
    anchor_cpu: int,
    ttl_ms: int,
    policy_window_ms: int,
    boost_budget_ms: int,
    max_vtime_boost_us: int,
    width_control: bool,
    max_frontier_width: int,
    frontier_width: int,
    min_frontier_score: int,
    frontier_propagation: bool,
    frontier_propagation_depth: int,
    policy_fallback: bool,
    forced_stage: Stage | None,
    adaptive_frontier_width: bool = False,
    adaptive_max_frontier_width: int = 8,
    frontier_action: str = "advance",
    adaptive_frontier_propagation_depth: bool = False,
    stage_aware_frontier_propagation_depth: bool = False,
    build_frontier_admission: str = "advance",
    stage_state_file: pathlib.Path | None = None,
    stage_source: str | None = None,
    stage_label: str | None = None,
    background_hints: bool = True,
) -> HintPlan:
    pids = subtree(root_pid)
    infos = collect_proc_info(pids)
    pids = sorted(infos)
    if stage_source is None:
        stage, resolved_stage_source, stage_state = resolve_live_stage(
            infos.values(),
            forced_stage=forced_stage,
            stage_state_file=stage_state_file,
        )
        stage_source = resolved_stage_source
        if stage_label is None and stage_state:
            stage_label = str(stage_state.get("stage") or stage.name)
    else:
        stage = forced_stage or infer_stage_from_infos(infos.values())
    session_id = stable_u64(session_name)
    stage_id = stable_u64(f"{session_name}:{stage.name}:{root_pid}")
    cohort_id = stable_u64(f"{session_name}:{root_pid}:{stage.name}:cohort0")
    ranked = rank_frontier_candidates(infos.values(), stage)
    ranked_by_pid = {pid: (score, reason) for score, pid, reason in ranked}
    selectable_ranked = [
        (score, pid, reason)
        for score, pid, reason in ranked
        if not is_stage_generic_background_reason(reason)
    ]
    width_decision = decide_adaptive_frontier_width(
        infos.values(),
        selectable_ranked,
        requested_frontier_width=frontier_width,
        requested_max_frontier_width=max_frontier_width,
        min_frontier_score=min_frontier_score,
        adaptive_frontier_width=adaptive_frontier_width,
        adaptive_max_frontier_width=adaptive_max_frontier_width,
    )
    frontier = {
        pid
        for score, pid, _ in selectable_ranked[: max(1, width_decision.frontier_width)]
        if score >= max(0, min_frontier_score)
    }
    propagation = (
        frontier_propagation_anchors(frontier, infos, root_pid)
        if frontier_propagation
        else set()
    )
    if frontier_propagation:
        propagation |= spawn_propagation_anchors(root_pid, infos, frontier)
    confidence = clamp_confidence(confidence)
    confidence_floor = confidence if confidence > 0 else 0
    propagation_depth_decision = decide_adaptive_propagation_depth(
        frontier_propagation=frontier_propagation,
        requested_depth=frontier_propagation_depth,
        adaptive=adaptive_frontier_propagation_depth,
        stage_aware=stage_aware_frontier_propagation_depth,
        stage=stage,
        frontier_pids=frontier,
        propagation_pids=propagation,
        ranked=selectable_ranked,
        min_frontier_score=min_frontier_score,
    )
    propagation_depth = propagation_depth_decision.propagation_depth
    policy_flags = POLICY_ENABLE
    if anchor_cpu >= 0:
        policy_flags |= POLICY_LOCALITY
    if width_control:
        policy_flags |= POLICY_WIDTH_CTL
    if policy_fallback:
        policy_flags |= POLICY_FALLBACK
    policy = SessionPolicy(
        session_id=session_id,
        flags=policy_flags,
        min_confidence=max(0, min(confidence, 100)),
        max_frontier_width=max(1, width_decision.max_frontier_width),
        max_vtime_boost_ns=max(0, max_vtime_boost_us) * 1_000,
        window_ns=max(1, policy_window_ms) * 1_000_000,
        boost_budget_ns=max(0, boost_budget_ms) * 1_000_000,
        expires_ns=time.time_ns() + max(1, ttl_ms) * 1_000_000,
        anchor_cpu=anchor_cpu,
    )
    hints: list[TaskHint] = []
    for pid in pids:
        info = infos.get(pid)
        flags = 0
        task_confidence = 0
        if pid in frontier:
            flags |= TASK_FRONTIER | TASK_COHORT_TAIL
            task_confidence = confidence
        if pid in propagation:
            flags |= TASK_PROPAGATE
            if task_confidence <= 0:
                task_confidence = confidence_floor
        if anchor_cpu >= 0:
            flags |= TASK_LOCALITY
        _score, rank_reason = ranked_by_pid.get(pid, (0, ""))
        if background_hints and "stage-generic-background" in rank_reason:
            flags |= TASK_BACKGROUND
        hints.append(
            TaskHint(
                pid=pid,
                tgid=pid,
                parent_pid=info.ppid if info else ppid_of(pid),
                session_id=session_id,
                stage_id=stage_id,
                cohort_id=cohort_id,
                flags=flags,
                confidence=task_confidence,
                anchor_cpu=anchor_cpu,
                propagation_depth=propagation_depth if flags & TASK_PROPAGATE else 0,
            )
        )
    plan = HintPlan(
        root_pid=root_pid,
        policy=policy,
        hints=hints,
        stage=stage,
        frontier_pids=frontier,
        propagation_pids=propagation,
        ranked_frontiers=ranked,
        proc_comms={pid: info.comm for pid, info in infos.items()},
        adaptive_width=dataclasses.asdict(width_decision),
        adaptive_propagation=dataclasses.asdict(propagation_depth_decision),
        stage_source=stage_source or ("forced" if forced_stage is not None else "inferred"),
        stage_label=stage_label or stage.name,
    )
    action_decision = decide_frontier_action(
        infos.values(),
        selectable_ranked,
        frontier,
        width_decision,
        stage=stage,
        requested_action=frontier_action,
        explicit_policy_fallback=policy_fallback,
        min_frontier_score=min_frontier_score,
        build_frontier_admission=build_frontier_admission,
        propagation=propagation,
    )
    return apply_frontier_action(plan, action_decision)


def build_oracle_hint(
    frontier_pid: int,
    session_name: str,
    stage_kind: Stage,
    stage_label: str,
    cohort_label: str,
    confidence: int,
    anchor_cpu: int,
    ttl_ms: int,
    policy_window_ms: int,
    boost_budget_ms: int,
    max_vtime_boost_us: int,
    width_control: bool,
    max_frontier_width: int,
    policy_fallback: bool = False,
    frontier_action: str = "advance",
) -> tuple[SessionPolicy, list[TaskHint]]:
    session_id = stable_u64(session_name)
    stage_id = stable_u64(f"{session_name}:{stage_kind.name}:{stage_label}")
    cohort_id = stable_u64(f"{session_name}:{stage_label}:{cohort_label}")
    confidence = clamp_confidence(confidence)
    action = normalize_frontier_action(frontier_action)
    observe_only = policy_fallback or action == "observe"
    policy_flags = POLICY_ENABLE
    if anchor_cpu >= 0:
        policy_flags |= POLICY_LOCALITY
    if width_control:
        policy_flags |= POLICY_WIDTH_CTL
    if observe_only:
        policy_flags |= POLICY_FALLBACK
    policy = SessionPolicy(
        session_id=session_id,
        flags=policy_flags,
        min_confidence=confidence,
        max_frontier_width=max(1, max_frontier_width),
        max_vtime_boost_ns=max(0, max_vtime_boost_us) * 1_000,
        window_ns=max(1, policy_window_ms) * 1_000_000,
        boost_budget_ns=max(0, boost_budget_ms) * 1_000_000,
        expires_ns=time.time_ns() + max(1, ttl_ms) * 1_000_000,
        anchor_cpu=anchor_cpu,
    )
    flags = TASK_FRONTIER | TASK_COHORT_TAIL
    if observe_only:
        flags |= TASK_OBSERVE_ONLY
    if anchor_cpu >= 0:
        flags |= TASK_LOCALITY
    hint = TaskHint(
        pid=frontier_pid,
        tgid=frontier_pid,
        parent_pid=ppid_of(frontier_pid),
        session_id=session_id,
        stage_id=stage_id,
        cohort_id=cohort_id,
        flags=flags,
        confidence=confidence,
        anchor_cpu=anchor_cpu,
    )
    return policy, [hint]


def main() -> int:
    parser = argparse.ArgumentParser(description="FlowCausal sandbox frontier controller")
    parser.add_argument("--root-pid", type=int, help="root process of the sandbox/tool subtree")
    parser.add_argument("--frontier-pid", type=int, help="directly mark this pid as an oracle frontier")
    parser.add_argument("--stage-kind", default="AUTO", help="stage kind name/numeric enum; AUTO infers it for --root-pid")
    parser.add_argument(
        "--stage-state-file",
        help=(
            "optional JSON file emitted by a staged workload; each watch tick "
            "uses its live stage before falling back to command inference"
        ),
    )
    parser.add_argument("--stage-label", default="stage0", help="stable stage label for --frontier-pid")
    parser.add_argument("--cohort-label", default="cohort0", help="stable cohort label for --frontier-pid")
    parser.add_argument("--session", default=None, help="stable session name; default: pid:<root-pid>")
    parser.add_argument("--pin-dir", default=str(DEFAULT_PIN_DIR), help="directory containing pinned FlowCausal maps")
    parser.add_argument("--task-map", help="pinned BPF task_ctx map path; default: <pin-dir>/task_ctx")
    parser.add_argument("--policy-map", help="pinned BPF session_policy map path; default: <pin-dir>/session_policy")
    parser.add_argument("--confidence", type=int, default=75)
    parser.add_argument("--anchor-cpu", type=int, default=-1)
    parser.add_argument("--ttl-ms", type=int, default=1000, help="hint expiration time in milliseconds")
    parser.add_argument("--policy-window-ms", type=int, default=100, help="policy accounting window in milliseconds")
    parser.add_argument("--boost-budget-ms", type=int, default=10, help="frontier boost budget for a session; 0 disables the budget cap")
    parser.add_argument("--max-vtime-boost-us", type=int, default=500, help="maximum virtual-time boost per admitted frontier enqueue")
    parser.add_argument("--width-control", action="store_true", help="enforce max frontier width in the BPF scheduler")
    parser.add_argument("--max-frontier-width", type=int, default=1, help="maximum admitted frontier width when --width-control is enabled")
    parser.add_argument("--frontier-width", type=int, default=1, help="number of inferred frontier candidates to mark in --root-pid mode")
    parser.add_argument("--adaptive-frontier-width", action="store_true", help="derive frontier width from current runnable/leaf cohort shape in --root-pid mode")
    parser.add_argument("--adaptive-max-frontier-width", type=int, default=8, help="hard cap for --adaptive-frontier-width")
    parser.add_argument("--min-frontier-score", type=int, default=MIN_FRONTIER_SCORE, help="minimum online score required before a ranked process is marked as frontier")
    parser.add_argument("--frontier-propagation", action="store_true", help="mark wait-chain ancestors as fork-time propagation anchors for short-lived child frontiers")
    parser.add_argument("--frontier-propagation-depth", type=int, default=2, help="maximum number of fork hops a propagation anchor may pass frontier hints to")
    parser.add_argument(
        "--adaptive-frontier-propagation-depth",
        action="store_true",
        help=(
            "choose propagation depth per snapshot: depth 1 for concrete "
            "frontiers, requested depth for spawn-anchor/no-frontier snapshots"
        ),
    )
    parser.add_argument(
        "--stage-aware-frontier-propagation-depth",
        action="store_true",
        help=(
            "experimental propagation-depth policy: keep TEST/RETRY concrete "
            "frontiers shallow, suppress broad BUILD propagation, and reserve "
            "the requested depth for spawn anchors"
        ),
    )
    parser.add_argument("--frontier-stability-samples", type=int, default=1, help="consecutive watch samples required before a weak online candidate is marked frontier; 1 disables debounce")
    parser.add_argument("--frontier-hold-ms", type=int, default=0, help="keep a still-valid previous frontier for this many milliseconds to reduce tick-to-tick churn")
    parser.add_argument("--frontier-bypass-score", type=int, default=140, help="score at which a candidate bypasses --frontier-stability-samples")
    parser.add_argument(
        "--frontier-selected-stability",
        action="store_true",
        help=(
            "count only currently selected frontier pids toward stability, "
            "refusing churny top-frontier snapshots in short sandbox stages"
        ),
    )
    parser.add_argument(
        "--frontier-stability-action",
        default="suppress",
        choices=sorted(FRONTIER_STABILITY_ACTIONS),
        help=(
            "when stability delays the current selected frontier, either remove "
            "it from live hints (suppress) or keep it as observe-only fallback "
            "until it becomes stable"
        ),
    )
    parser.add_argument("--frontier-action", default="advance", choices=sorted(FRONTIER_ACTIONS), help="advance selected frontiers, observe them through fallback, or auto-refuse ambiguous snapshots")
    parser.add_argument(
        "--build-frontier-admission",
        default="advance",
        choices=sorted(BUILD_FRONTIER_ADMISSIONS),
        help=(
            "BUILD-stage admission override: advance as before, observe all "
            "BUILD frontiers, or advance only link/archive and wider barrier-tail "
            "BUILD frontiers"
        ),
    )
    parser.add_argument("--policy-fallback", action="store_true", help="write hints but set the session fallback flag so BPF sends frontier hits to the shared DSQ")
    parser.add_argument("--no-background-hints", action="store_true", help="do not mark stage-generic tasks as background in task_ctx")
    parser.add_argument("--watch", action="store_true", help="continuously refresh inferred root-pid hints until the root exits")
    parser.add_argument("--interval-ms", type=int, default=100, help="watch refresh interval")
    parser.add_argument("--duration-sec", type=float, default=None, help="optional maximum watch duration")
    parser.add_argument("--json-log", help="optional JSONL file for inferred frontier decisions")
    parser.add_argument("--dry-run", action="store_true", help="print inferred hints without updating maps")
    args = parser.parse_args()
    try:
        frontier_action = normalize_frontier_action(args.frontier_action)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    try:
        build_frontier_admission = normalize_build_frontier_admission(args.build_frontier_admission)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    if args.frontier_pid is not None:
        if args.watch:
            raise SystemExit("--watch is only supported with --root-pid inference, not --frontier-pid oracle mode")
        try:
            stage_kind = parse_optional_stage_kind(args.stage_kind) or Stage.SHELL
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        session = args.session or f"pid:{args.frontier_pid}"
        policy, hints = build_oracle_hint(
            args.frontier_pid,
            session,
            stage_kind,
            args.stage_label,
            args.cohort_label,
            args.confidence,
            args.anchor_cpu,
            args.ttl_ms,
            args.policy_window_ms,
            args.boost_budget_ms,
            args.max_vtime_boost_us,
            args.width_control,
            args.max_frontier_width,
            args.policy_fallback,
            frontier_action,
        )
    else:
        if args.root_pid is None:
            raise SystemExit("either --root-pid or --frontier-pid is required")
        try:
            forced_stage = parse_optional_stage_kind(args.stage_kind)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        stage_state_file = pathlib.Path(args.stage_state_file) if args.stage_state_file else None
        session = args.session or f"pid:{args.root_pid}"
        plan = build_hint_plan(
            args.root_pid,
            session,
            args.confidence,
            args.anchor_cpu,
            args.ttl_ms,
            args.policy_window_ms,
            args.boost_budget_ms,
            args.max_vtime_boost_us,
            args.width_control,
            args.max_frontier_width,
            args.frontier_width,
            args.min_frontier_score,
            args.frontier_propagation,
            args.frontier_propagation_depth,
            args.policy_fallback,
            forced_stage,
            args.adaptive_frontier_width,
            args.adaptive_max_frontier_width,
            frontier_action,
            adaptive_frontier_propagation_depth=args.adaptive_frontier_propagation_depth,
            stage_aware_frontier_propagation_depth=args.stage_aware_frontier_propagation_depth,
            build_frontier_admission=build_frontier_admission,
            stage_state_file=stage_state_file,
            background_hints=not args.no_background_hints,
        )
        policy, hints = plan.policy, plan.hints

    pin_dir = pathlib.Path(args.pin_dir)
    task_map = pathlib.Path(args.task_map) if args.task_map else pin_dir / "task_ctx"
    policy_map = pathlib.Path(args.policy_map) if args.policy_map else pin_dir / "session_policy"

    if args.dry_run:
        if args.watch:
            json_file = open(args.json_log, "a", encoding="utf-8") if args.json_log else None
            try:
                return watch_subtree(
                    root_pid=args.root_pid,
                    session=session,
                    confidence=args.confidence,
                    anchor_cpu=args.anchor_cpu,
                    ttl_ms=args.ttl_ms,
                    policy_window_ms=args.policy_window_ms,
                    boost_budget_ms=args.boost_budget_ms,
                    max_vtime_boost_us=args.max_vtime_boost_us,
                    width_control=args.width_control,
                    max_frontier_width=args.max_frontier_width,
                    frontier_width=args.frontier_width,
                    adaptive_frontier_width=args.adaptive_frontier_width,
                    adaptive_max_frontier_width=args.adaptive_max_frontier_width,
                    min_frontier_score=args.min_frontier_score,
                    frontier_propagation=args.frontier_propagation,
                    frontier_propagation_depth=args.frontier_propagation_depth,
                    adaptive_frontier_propagation_depth=args.adaptive_frontier_propagation_depth,
                    stage_aware_frontier_propagation_depth=args.stage_aware_frontier_propagation_depth,
                    frontier_stability_samples=args.frontier_stability_samples,
                    frontier_hold_ms=args.frontier_hold_ms,
                    frontier_bypass_score=args.frontier_bypass_score,
                    frontier_selected_stability=args.frontier_selected_stability,
                    frontier_stability_action=args.frontier_stability_action,
                    policy_fallback=args.policy_fallback,
                    frontier_action=frontier_action,
                    build_frontier_admission=build_frontier_admission,
                    background_hints=not args.no_background_hints,
                    forced_stage=forced_stage,
                    interval_ms=args.interval_ms,
                    duration_sec=args.duration_sec,
                    dry_run=True,
                    task_map=task_map,
                    policy_map=policy_map,
                    json_log=json_file,
                    stage_state_file=stage_state_file,
                )
            finally:
                if json_file is not None:
                    json_file.close()
        if args.frontier_pid is None:
            print_plan(plan)
        else:
            print(f"session_id={policy.session_id} policy={dataclasses.asdict(policy)}")
            for hint in hints:
                marker = "FRONTIER" if hint.flags & TASK_FRONTIER else "cohort"
                print(f"{marker:8s} pid={hint.pid:<7d} ppid={hint.parent_pid:<7d} comm={comm_of(hint.pid)}")
        return 0

    missing = inaccessible_or_missing((task_map, policy_map))
    if missing:
        raise SystemExit(
            "missing or inaccessible pinned FlowCausal map(s): "
            + ", ".join(missing)
            + "\nStart scx_flowcausal first and run this command with sufficient privileges, or pass --dry-run."
        )

    if args.watch:
        json_file = open(args.json_log, "a", encoding="utf-8") if args.json_log else None
        try:
            return watch_subtree(
                root_pid=args.root_pid,
                session=session,
                confidence=args.confidence,
                anchor_cpu=args.anchor_cpu,
                ttl_ms=args.ttl_ms,
                policy_window_ms=args.policy_window_ms,
                boost_budget_ms=args.boost_budget_ms,
                max_vtime_boost_us=args.max_vtime_boost_us,
                width_control=args.width_control,
                max_frontier_width=args.max_frontier_width,
                frontier_width=args.frontier_width,
                adaptive_frontier_width=args.adaptive_frontier_width,
                adaptive_max_frontier_width=args.adaptive_max_frontier_width,
                min_frontier_score=args.min_frontier_score,
                frontier_propagation=args.frontier_propagation,
                frontier_propagation_depth=args.frontier_propagation_depth,
                adaptive_frontier_propagation_depth=args.adaptive_frontier_propagation_depth,
                stage_aware_frontier_propagation_depth=args.stage_aware_frontier_propagation_depth,
                frontier_stability_samples=args.frontier_stability_samples,
                frontier_hold_ms=args.frontier_hold_ms,
                frontier_bypass_score=args.frontier_bypass_score,
                frontier_selected_stability=args.frontier_selected_stability,
                frontier_stability_action=args.frontier_stability_action,
                policy_fallback=args.policy_fallback,
                frontier_action=frontier_action,
                build_frontier_admission=build_frontier_admission,
                background_hints=not args.no_background_hints,
                forced_stage=forced_stage,
                interval_ms=args.interval_ms,
                duration_sec=args.duration_sec,
                dry_run=False,
                task_map=task_map,
                policy_map=policy_map,
                json_log=json_file,
                stage_state_file=stage_state_file,
            )
        finally:
            if json_file is not None:
                json_file.close()

    policy = update_maps(task_map, policy_map, policy, hints)

    print(
        f"updated {len(hints)} task hints and policy for session_id={policy.session_id} "
        f"ttl_ms={max(1, args.ttl_ms)}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

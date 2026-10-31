#!/usr/bin/env python3
"""Translate frontier_label records into FlowCausal oracle frontier hints.

The live sandbox can mirror command-level ``frontier_label`` records into a
JSONL file.  This driver tails that file and writes one oracle hint per label by
invoking ``flowctl --frontier-pid``.  It is intentionally small and auditable:
the driver does not infer frontiers, it only replays labels that another
component already emitted.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def fnum(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


STAGE_ALIASES = {
    "INSPECT": "SEARCH",
    "EDIT": "SHELL",
    "RETRY_PROBE": "RETRY",
    "PACKAGE_BACKGROUND": "SHELL",
}


def normalize_stage_for_flowctl(value: Any) -> str:
    text = str(value or "SHELL").strip().upper() or "SHELL"
    return STAGE_ALIASES.get(text, text)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate frontier_label JSONL records into oracle frontier hints",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--label-file", required=True, help="JSONL file written by agent_trace_workflow")
    parser.add_argument("--root-pid", type=int, required=True, help="root pid of the sandbox workload")
    parser.add_argument("--session", default=None, help="stable FlowCausal session name")
    parser.add_argument("--flowctl", required=True, help="path to FlowCausal flowctl.py")
    parser.add_argument("--pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--task-map", default="")
    parser.add_argument("--policy-map", default="")
    parser.add_argument("--confidence", type=int, default=100)
    parser.add_argument("--anchor-cpu", type=int, default=-1)
    parser.add_argument("--ttl-ms", type=int, default=1000)
    parser.add_argument("--policy-window-ms", type=int, default=100)
    parser.add_argument("--boost-budget-ms", type=int, default=10)
    parser.add_argument("--max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument("--width-control", action="store_true")
    parser.add_argument("--frontier-action", choices=("advance", "observe"), default="advance")
    parser.add_argument("--poll-interval-ms", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def flowctl_command(args: argparse.Namespace, record: dict[str, Any]) -> list[str]:
    label = as_dict(record.get("label"))
    stage = normalize_stage_for_flowctl(record.get("stage") or label.get("stage") or "SHELL")
    stage_label = str(
        label.get("stage_label")
        or f"{record.get('command_index', 'cmd')}-{record.get('source_id', 'unknown')}"
    )
    cohort_label = str(label.get("cohort_label") or record.get("source_id") or "cohort0")
    confidence = int(fnum(label.get("confidence"), default=float(args.confidence)))
    anchor_cpu = int(fnum(label.get("anchor_cpu"), default=float(args.anchor_cpu)))
    ttl_ms = int(fnum(label.get("ttl_ms"), default=float(args.ttl_ms)))
    policy_window_ms = int(fnum(label.get("policy_window_ms"), default=float(args.policy_window_ms)))
    boost_budget_ms = int(fnum(label.get("boost_budget_ms"), default=float(args.boost_budget_ms)))
    max_vtime_boost_us = int(fnum(label.get("max_vtime_boost_us"), default=float(args.max_vtime_boost_us)))
    max_frontier_width = int(fnum(label.get("max_frontier_width"), default=float(args.max_frontier_width)))
    frontier_action = str(label.get("frontier_action") or args.frontier_action)
    policy_fallback = bool(label.get("policy_fallback", False))
    cmd = [
        sys.executable,
        args.flowctl,
        "--frontier-pid",
        str(int(record["pid"])),
        "--session",
        args.session or f"pid:{args.root_pid}",
        "--stage-kind",
        stage,
        "--stage-label",
        stage_label,
        "--cohort-label",
        cohort_label,
        "--pin-dir",
        args.pin_dir,
        "--confidence",
        str(confidence),
        "--anchor-cpu",
        str(anchor_cpu),
        "--ttl-ms",
        str(ttl_ms),
        "--policy-window-ms",
        str(policy_window_ms),
        "--boost-budget-ms",
        str(boost_budget_ms),
        "--max-vtime-boost-us",
        str(max_vtime_boost_us),
        "--max-frontier-width",
        str(max_frontier_width),
        "--frontier-action",
        frontier_action,
    ]
    if args.task_map:
        cmd.extend(["--task-map", args.task_map])
    if args.policy_map:
        cmd.extend(["--policy-map", args.policy_map])
    if policy_fallback:
        cmd.append("--policy-fallback")
    if args.width_control or label.get("width_control"):
        cmd.append("--width-control")
    return cmd


def tail_labels(path: pathlib.Path, root_pid: int, poll_interval_ms: int):
    offset = 0
    while True:
        read_any = False
        if path.exists():
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                while True:
                    line = handle.readline()
                    if not line:
                        offset = handle.tell()
                        break
                    offset = handle.tell()
                    line = line.strip()
                    if not line:
                        continue
                    read_any = True
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(record, dict):
                        yield record
        if not pathlib.Path(f"/proc/{root_pid}").exists():
            if path.exists():
                current_size = path.stat().st_size
                if offset >= current_size and not read_any:
                    return
            else:
                return
        time.sleep(max(1, poll_interval_ms) / 1000.0)


def main() -> int:
    args = parse_args()
    label_path = pathlib.Path(args.label_file).resolve()
    if not label_path.exists():
        label_path.parent.mkdir(parents=True, exist_ok=True)
        label_path.touch()

    seen: set[tuple[Any, ...]] = set()
    for record in tail_labels(label_path, args.root_pid, args.poll_interval_ms):
        if record.get("type") != "frontier_label":
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        key = (pid, record.get("command_index"), record.get("source_id"))
        if key in seen:
            continue
        seen.add(key)
        cmd = flowctl_command(args, record)
        if args.dry_run:
            print("+ " + " ".join(shlex.quote(part) for part in cmd), flush=True)
            continue
        subprocess.run(cmd, check=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

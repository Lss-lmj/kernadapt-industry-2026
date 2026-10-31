#!/usr/bin/env python3
"""Summarize lock-monitorV4 text logs for paper evidence.

The parser intentionally reports only fields emitted by lock-monitorV4:
lock type/id, waiter/owner tgid/tid/comm/prio, and wait latency.  CPU, LLC,
and NUMA handoff fields are marked unavailable unless future monitor logs
explicitly include them.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


EVENT_RE = re.compile(
    r"TYPE:\s+(?P<type>\w+)\s+\|\s+"
    r"(?:(?:Addr:\s+0x(?P<mutex>[0-9a-fA-F]+))|(?:FUTEX_KEY\s+\(uaddr\):\s+0x(?P<futex>[0-9a-fA-F]+)))\s+\|\s+"
    r"Waiter:\s+(?P<waiter_tgid>\d+)/(?P<waiter_tid>\d+)\s+"
    r"\((?P<waiter_comm>[^,)]*),\s*prio:(?P<waiter_prio>-?\d+)\)\s+\|\s+"
    r"Owner:\s+(?P<owner_tgid>\d+)/(?P<owner_tid>\d+)\s+"
    r"\((?P<owner_comm>[^,)]*),\s*prio:(?P<owner_prio>-?\d+)\)\s+\|\s+"
    r"Latency:\s+(?P<latency_ms>[0-9.]+)\s+ms"
)


@dataclass(frozen=True)
class LockEvent:
    lock_type: str
    lock_id: str
    waiter_tgid: int
    waiter_tid: int
    waiter_comm: str
    waiter_prio: int
    owner_tgid: int
    owner_tid: int
    owner_comm: str
    owner_prio: int
    latency_ms: float


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def parse_line(line: str) -> LockEvent | None:
    match = EVENT_RE.search(line)
    if not match:
        return None
    lock_type = match.group("type")
    lock_addr = match.group("mutex") or match.group("futex")
    assert lock_addr is not None
    lock_prefix = "mutex" if lock_type.lower() == "mutex" else "futex"
    return LockEvent(
        lock_type=lock_type,
        lock_id=f"{lock_prefix}:0x{lock_addr.lower()}",
        waiter_tgid=int(match.group("waiter_tgid")),
        waiter_tid=int(match.group("waiter_tid")),
        waiter_comm=match.group("waiter_comm").strip(),
        waiter_prio=int(match.group("waiter_prio")),
        owner_tgid=int(match.group("owner_tgid")),
        owner_tid=int(match.group("owner_tid")),
        owner_comm=match.group("owner_comm").strip(),
        owner_prio=int(match.group("owner_prio")),
        latency_ms=float(match.group("latency_ms")),
    )


def parse_log(path: Path) -> tuple[list[LockEvent], int]:
    events: list[LockEvent] = []
    unparsable = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "TYPE:" not in line:
                continue
            event = parse_line(line)
            if event is None:
                unparsable += 1
            else:
                events.append(event)
    return events, unparsable


def filter_events(
    events: list[LockEvent],
    *,
    waiter_comm: str | None = None,
    owner_comm: str | None = None,
    comm: str | None = None,
) -> list[LockEvent]:
    """Filter events by command names without changing parser semantics."""

    def match(value: str, pattern: str | None) -> bool:
        if not pattern:
            return True
        return value == pattern

    filtered: list[LockEvent] = []
    for event in events:
        if waiter_comm and not match(event.waiter_comm, waiter_comm):
            continue
        if owner_comm and not match(event.owner_comm, owner_comm):
            continue
        if comm and event.waiter_comm != comm and event.owner_comm != comm:
            continue
        filtered.append(event)
    return filtered


def counter_top(counter: Counter[Any], total: float, top_n: int) -> list[dict[str, Any]]:
    rows = []
    for key, value in counter.most_common(top_n):
        rows.append(
            {
                "key": key,
                "value": value,
                "share": (float(value) / total) if total else 0.0,
            }
        )
    return rows


def summarize(events: list[LockEvent], unparsable: int, top_n: int) -> dict[str, Any]:
    latencies = [event.latency_ms for event in events]
    total_wait_ms = sum(latencies)
    lock_event_counts: Counter[str] = Counter(event.lock_id for event in events)
    lock_wait_ms: Counter[str] = Counter()
    lock_waiters: dict[str, set[int]] = defaultdict(set)
    lock_owners: dict[str, set[int]] = defaultdict(set)
    owner_event_counts: Counter[str] = Counter()
    owner_wait_ms: Counter[str] = Counter()
    waiter_comm_counts: Counter[str] = Counter()
    owner_comm_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()

    for event in events:
        owner_key = f"{event.owner_tgid}/{event.owner_tid}:{event.owner_comm}"
        lock_wait_ms[event.lock_id] += event.latency_ms
        lock_waiters[event.lock_id].add(event.waiter_tid)
        lock_owners[event.lock_id].add(event.owner_tid)
        owner_event_counts[owner_key] += 1
        owner_wait_ms[owner_key] += event.latency_ms
        waiter_comm_counts[event.waiter_comm] += 1
        owner_comm_counts[event.owner_comm] += 1
        type_counts[event.lock_type] += 1

    distinct_waiters_per_lock = [len(waiters) for waiters in lock_waiters.values()]
    distinct_owners_per_lock = [len(owners) for owners in lock_owners.values()]
    event_count = len(events)
    top_lock_by_count = counter_top(lock_event_counts, event_count, top_n)
    top_lock_by_wait_ms = counter_top(lock_wait_ms, total_wait_ms, top_n)
    top_owner_by_count = counter_top(owner_event_counts, event_count, top_n)
    top_owner_by_wait_ms = counter_top(owner_wait_ms, total_wait_ms, top_n)

    return {
        "event_count": event_count,
        "unparsable_event_lines": unparsable,
        "lock_count": len(lock_event_counts),
        "type_counts": dict(type_counts),
        "latency_ms": {
            "total": total_wait_ms,
            "mean": statistics.mean(latencies) if latencies else None,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else None,
        },
        "top_lock_by_event_count": top_lock_by_count,
        "top_lock_by_wait_ms": top_lock_by_wait_ms,
        "top5_lock_event_share": sum(row["value"] for row in top_lock_by_count[:5]) / event_count if event_count else 0.0,
        "top5_lock_wait_ms_share": sum(row["value"] for row in top_lock_by_wait_ms[:5]) / total_wait_ms if total_wait_ms else 0.0,
        "distinct_waiters_per_lock": {
            "mean": statistics.mean(distinct_waiters_per_lock) if distinct_waiters_per_lock else None,
            "p95": percentile([float(v) for v in distinct_waiters_per_lock], 95),
            "max": max(distinct_waiters_per_lock) if distinct_waiters_per_lock else None,
        },
        "distinct_owners_per_lock": {
            "mean": statistics.mean(distinct_owners_per_lock) if distinct_owners_per_lock else None,
            "p95": percentile([float(v) for v in distinct_owners_per_lock], 95),
            "max": max(distinct_owners_per_lock) if distinct_owners_per_lock else None,
        },
        "top_owner_by_event_count": top_owner_by_count,
        "top_owner_by_wait_ms": top_owner_by_wait_ms,
        "top_waiter_comm": counter_top(waiter_comm_counts, event_count, top_n),
        "top_owner_comm": counter_top(owner_comm_counts, event_count, top_n),
        "unavailable_fields": [
            "waiter_cpu",
            "owner_cpu",
            "successor_cpu",
            "same_llc_handoff",
            "remote_numa_handoff",
        ],
        "schema_note": (
            "lock-monitorV4 emits lock id, waiter/owner identity, comm, priority, "
            "and wait latency. It does not emit CPU/LLC/NUMA handoff fields."
        ),
    }


def pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def num(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    rows = [
        ("events", summary["event_count"]),
        ("locks", summary["lock_count"]),
        ("total_wait_ms", num(summary["latency_ms"]["total"])),
        ("p95_latency_ms", num(summary["latency_ms"]["p95"])),
        ("top5_lock_event_share", pct(summary["top5_lock_event_share"])),
        ("top5_lock_wait_ms_share", pct(summary["top5_lock_wait_ms_share"])),
        ("p95_distinct_waiters_per_lock", num(summary["distinct_waiters_per_lock"]["p95"], 1)),
        ("max_distinct_waiters_per_lock", summary["distinct_waiters_per_lock"]["max"]),
    ]
    lines = ["# Lock Monitor Summary", "", "| Metric | Value |", "| --- | ---: |"]
    lines.extend(f"| {key} | {value} |" for key, value in rows)
    lines.extend(["", "## Top Locks by Wait Time", "", "| Lock | Wait ms | Share |", "| --- | ---: | ---: |"])
    for row in summary["top_lock_by_wait_ms"]:
        lines.append(f"| `{row['key']}` | {num(row['value'])} | {pct(row['share'])} |")
    lines.extend(["", "## Top Owners by Wait Time", "", "| Owner | Wait ms | Share |", "| --- | ---: | ---: |"])
    for row in summary["top_owner_by_wait_ms"]:
        lines.append(f"| `{row['key']}` | {num(row['value'])} | {pct(row['share'])} |")
    lines.extend(
        [
            "",
            "## Unavailable Fields",
            "",
            ", ".join(f"`{field}`" for field in summary["unavailable_fields"]),
            "",
            summary["schema_note"],
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--log", type=Path, required=True, help="Raw lock-monitorV4 stdout/stderr log")
    ap.add_argument("--out-json", type=Path, help="Write JSON summary")
    ap.add_argument("--out-md", type=Path, help="Write Markdown summary")
    ap.add_argument("--top-n", type=int, default=10)
    ap.add_argument("--waiter-comm", help="Only include events whose waiter comm exactly matches this value")
    ap.add_argument("--owner-comm", help="Only include events whose owner comm exactly matches this value")
    ap.add_argument("--comm", help="Only include events whose waiter or owner comm exactly matches this value")
    args = ap.parse_args(argv)

    events, unparsable = parse_log(args.log)
    raw_event_count = len(events)
    events = filter_events(events, waiter_comm=args.waiter_comm, owner_comm=args.owner_comm, comm=args.comm)
    summary = summarize(events, unparsable, args.top_n)
    summary["source_log"] = str(args.log)
    summary["filters"] = {
        "waiter_comm": args.waiter_comm,
        "owner_comm": args.owner_comm,
        "comm": args.comm,
        "raw_event_count": raw_event_count,
        "filtered_event_count": len(events),
    }
    summary["parsed_events"] = [asdict(event) for event in events[: min(len(events), 20)]]

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(summary, args.out_md)

    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

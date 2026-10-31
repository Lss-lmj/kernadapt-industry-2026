#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
TEST_DIR = SCRIPT_DIR.parent
REPO_ROOT = TEST_DIR.parent
HANDOFF_MONITOR = REPO_ROOT / "tools" / "handoff-locality-monitor" / "handoff_locality_monitor"

sys.path.insert(0, str(SCRIPT_DIR))
import formal_mariadb_oltp as formal  # noqa: E402

DEFAULT_HANDOFF_SCHEDS = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
]
DEFAULT_HANDOFF_THREADS = [1, 2, 4, 8, 16, 32, 64, 128, 256]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_list_int(s: str) -> list[int]:
    return [int(x) for x in s.replace(",", " ").split() if x.strip()]


def parse_list_str(s: str) -> list[str]:
    return [x for x in s.replace(",", " ").split() if x.strip()]


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sudo_cmd(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_cmd(["sudo", "-n", *args], timeout=timeout)


def ensure_monitor_built() -> None:
    if HANDOFF_MONITOR.exists() and os.access(HANDOFF_MONITOR, os.X_OK):
        return
    cp = run_cmd(["make", "-C", str(HANDOFF_MONITOR.parent)], timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "failed to build handoff monitor")


def raw_dir_for(root: Path, scheduler: str, threads: int, run_id: int) -> Path:
    return root / "handoff_locality" / "raw" / f"{scheduler}_t{threads}_r{run_id:02d}"


def existing_run_ids(raw_root: Path, scheduler: str, threads: int) -> list[int]:
    ids: list[int] = []
    if not raw_root.exists():
        return ids
    prefix = f"{scheduler}_t{threads}_r"
    for p in raw_root.iterdir():
        if not p.is_dir() or not p.name.startswith(prefix):
            continue
        try:
            ids.append(int(p.name.rsplit("_r", 1)[1]))
        except ValueError:
            pass
    return sorted(ids)


def start_handoff_monitor(args: argparse.Namespace, raw: Path, pid: str) -> tuple[subprocess.Popen[Any] | None, str | None]:
    out_json = raw / "handoff.json"
    log_path = raw / "handoff_monitor.log"
    cmd = [
        "sudo",
        "-n",
        str(HANDOFF_MONITOR),
        "--pid",
        pid,
        "--out-json",
        str(out_json),
        "--max-gap-ms",
        str(args.max_gap_ms),
    ]
    if args.monitor_library:
        cmd += ["--library", args.monitor_library]
    if args.include_same_tid:
        cmd += ["--include-same-tid"]

    f = log_path.open("wb")
    proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
    time.sleep(args.monitor_start_delay)
    if proc.poll() is not None:
        f.close()
        detail = log_path.read_text(errors="replace") if log_path.exists() else ""
        return None, f"handoff monitor exited early rc={proc.returncode}: {detail[-1000:]}"
    return proc, None


def stop_handoff_monitor(proc: subprocess.Popen[Any] | None) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.send_signal(signal.SIGTERM)
        return proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=5)


def run_one(args: argparse.Namespace, root: Path, scheduler: str, threads: int, run_id: int) -> None:
    raw = raw_dir_for(root, scheduler, threads, run_id)
    raw.mkdir(parents=True, exist_ok=True)
    env: dict[str, Any] = {
        "phase": "handoff_locality",
        "scheduler": scheduler,
        "threads": threads,
        "run_id": run_id,
        "start_timestamp": now_iso(),
        "kernel_version": run_cmd(["uname", "-r"]).stdout.strip(),
        "failure_type": "",
        "sysbench_returncode": None,
        "handoff_monitor_returncode": None,
    }
    write_json(raw / "env.json", env)

    scx_proc: subprocess.Popen[Any] | None = None
    monitor_proc: subprocess.Popen[Any] | None = None
    try:
        initial_state, rollback = formal.cleanup_scx()
        env["sched_ext_initial_state"] = initial_state
        if rollback or initial_state != "disabled":
            env["failure_type"] = "pre_run_rollback_failure"
            env["failure_detail"] = f"sched_ext not clean before run: state={initial_state} rollback={int(rollback)}"
            return

        if scheduler != "default":
            scx_proc, err = formal.start_scheduler(scheduler, raw, args.scx_args.get(scheduler, []))
            if err:
                env["failure_type"] = "scheduler_startup_failure"
                env["failure_detail"] = err
                return
        env["sched_ext_after_start_state"] = formal.scx_state()

        formal.restart_mariadb()
        env.update(formal.mariadb_env())
        if env.get("innodb_buffer_pool_size") != 134217728:
            env["failure_type"] = "env_failure"
            env["failure_detail"] = f"innodb_buffer_pool_size={env.get('innodb_buffer_pool_size')} is not 128MB"
            return

        if args.warmup_time > 0:
            log(f"Warmup handoff {scheduler} t{threads} r{run_id}: {args.warmup_time}s")
            rc, timed_out = formal.run_sysbench(
                formal.sysbench_args(args, threads, args.warmup_time, histogram=False),
                raw / "sysbench.warmup.log",
                timeout_s=formal.sysbench_timeout(args.warmup_time, args),
            )
            env["warmup_returncode"] = rc
            env["warmup_timed_out"] = timed_out
            if timed_out:
                env["failure_type"] = "warmup_timeout"
                return
            if rc != 0:
                env["failure_type"] = "warmup_failure"
                return

        pid = formal.mariadb_pid()
        env["mariadbd_pid"] = pid
        if not pid:
            env["failure_type"] = "env_failure"
            env["failure_detail"] = "cannot find mariadbd pid for handoff monitor"
            return

        monitor_proc, err = start_handoff_monitor(args, raw, pid)
        if err:
            env["failure_type"] = "handoff_monitor_startup_failure"
            env["failure_detail"] = err
            return

        log(f"Measure handoff {scheduler} t{threads} r{run_id}: {args.run_time}s")
        rc, timed_out = formal.run_sysbench(
            formal.sysbench_args(args, threads, args.run_time, histogram=True),
            raw / "sysbench.log",
            timeout_s=formal.sysbench_timeout(args.run_time, args),
        )
        env["sysbench_returncode"] = rc
        env["sysbench_timed_out"] = timed_out
        if timed_out:
            env["failure_type"] = "sysbench_timeout"
        elif rc != 0:
            env["failure_type"] = "sysbench_failure"

        env["handoff_monitor_returncode"] = stop_handoff_monitor(monitor_proc)
        monitor_proc = None
        if env["handoff_monitor_returncode"] != 0 and not env.get("failure_type"):
            env["failure_type"] = "handoff_monitor_failure"
        if not (raw / "handoff.json").exists() and not env.get("failure_type"):
            env["failure_type"] = "handoff_missing_output"

        handoff = load_json(raw / "handoff.json")
        events = handoff.get("handoff_counts", {}).get("handoff_events")
        env["handoff_events"] = events
        min_events = 0 if threads <= 1 else args.min_handoff_events
        if (events is None or int(events) < min_events) and not env.get("failure_type"):
            env["failure_type"] = "handoff_too_few_events"
            env["failure_detail"] = f"handoff_events={events}, min={min_events}"

    finally:
        if monitor_proc is not None:
            env["handoff_monitor_returncode"] = stop_handoff_monitor(monitor_proc)
        final_state, rollback = formal.cleanup_scx(scx_proc)
        env["sched_ext_final_state"] = final_state
        sched_failure = formal.scheduler_log_failure(raw)
        if sched_failure:
            env["scheduler_log_failure"] = sched_failure
            if not env.get("failure_type"):
                env["failure_type"] = "scheduler_runtime_failure"
                env["failure_detail"] = sched_failure
        if rollback:
            env["rollback_failure"] = True
            if not env.get("failure_type"):
                env["failure_type"] = "rollback_failure"
        env["end_timestamp"] = now_iso()
        write_json(raw / "env.json", env)


def csv_value(obj: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return "" if cur is None else cur


def summarize(root: Path, args: argparse.Namespace) -> None:
    raw_root = root / "handoff_locality" / "raw"
    rows: list[dict[str, Any]] = []
    if raw_root.exists():
        for raw in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            env = load_json(raw / "env.json")
            handoff = load_json(raw / "handoff.json")
            metrics = formal.parse_sysbench_log(raw / "sysbench.log")
            failure = env.get("failure_type", "")
            valid = (
                env.get("sysbench_returncode") == 0
                and not failure
                and csv_value(handoff, ("handoff_counts", "handoff_events")) != ""
            )
            rows.append({
                "scheduler": env.get("scheduler", ""),
                "threads": env.get("threads", ""),
                "run_id": env.get("run_id", ""),
                "valid": int(valid),
                "failure_type": failure,
                "failure_detail": env.get("failure_detail", ""),
                "tps": metrics.get("tps", ""),
                "lat_p95": metrics.get("lat_p95_ms", ""),
                "lat_p99": metrics.get("lat_p99_ms", ""),
                "handoff_events": csv_value(handoff, ("handoff_counts", "handoff_events")),
                "same_cpu": csv_value(handoff, ("handoff_counts", "same_cpu")),
                "same_llc": csv_value(handoff, ("handoff_counts", "same_llc")),
                "same_socket_other_llc": csv_value(handoff, ("handoff_counts", "same_socket_other_llc")),
                "cross_socket": csv_value(handoff, ("handoff_counts", "cross_socket")),
                "unknown_topology": csv_value(handoff, ("handoff_counts", "unknown_topology")),
                "same_cpu_ratio": csv_value(handoff, ("handoff_ratios", "same_cpu")),
                "same_llc_ratio": csv_value(handoff, ("handoff_ratios", "same_llc")),
                "same_socket_other_llc_ratio": csv_value(handoff, ("handoff_ratios", "same_socket_other_llc")),
                "cross_socket_ratio": csv_value(handoff, ("handoff_ratios", "cross_socket")),
                "unknown_topology_ratio": csv_value(handoff, ("handoff_ratios", "unknown_topology")),
                "lock_enter": csv_value(handoff, ("counters", "lock_enter")),
                "lock_success": csv_value(handoff, ("counters", "lock_success")),
                "unlock_enter": csv_value(handoff, ("counters", "unlock_enter")),
                "no_last_release": csv_value(handoff, ("counters", "no_last_release")),
                "same_tid_skipped": csv_value(handoff, ("counters", "same_tid_skipped")),
                "stale_skipped": csv_value(handoff, ("counters", "stale_skipped")),
                "start_timestamp": env.get("start_timestamp", ""),
                "end_timestamp": env.get("end_timestamp", ""),
                "raw_dir": str(raw),
            })

    summary_dir = root / "handoff_locality"
    write_csv(summary_dir / "summary_handoff_runs.csv", rows)
    write_csv(root / "summary_handoff_runs.csv", rows)

    cases: list[dict[str, Any]] = []
    for scheduler in args.schedulers:
        for threads in args.threads:
            vals = [r for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] == 1]
            cases.append({
                "scheduler": scheduler,
                "threads": threads,
                "n": len(vals),
                "handoff_events_mean": mean_field(vals, "handoff_events"),
                "same_cpu_ratio_mean": mean_field(vals, "same_cpu_ratio"),
                "same_llc_ratio_mean": mean_field(vals, "same_llc_ratio"),
                "same_socket_other_llc_ratio_mean": mean_field(vals, "same_socket_other_llc_ratio"),
                "cross_socket_ratio_mean": mean_field(vals, "cross_socket_ratio"),
                "tps_mean": mean_field(vals, "tps"),
                "lat_p95_mean": mean_field(vals, "lat_p95"),
                "failures": sum(1 for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] != 1),
            })
    write_csv(summary_dir / "summary_handoff_cases.csv", cases)
    write_csv(root / "summary_handoff_cases.csv", cases)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                fields.append(key)
                seen.add(key)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def values(rows: list[dict[str, Any]], field: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        val = row.get(field)
        if val in ("", None):
            continue
        try:
            out.append(float(val))
        except (TypeError, ValueError):
            pass
    return out


def fmt(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(v):
        return ""
    return f"{v:.6f}"


def mean_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = values(rows, field)
    return fmt(statistics.mean(vals) if vals else math.nan)


def run_matrix(args: argparse.Namespace, root: Path) -> None:
    raw_root = root / "handoff_locality" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scheduler in args.schedulers:
            while True:
                valid = []
                for run_id in existing_run_ids(raw_root, scheduler, threads):
                    raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
                    env = load_json(raw / "env.json")
                    handoff = load_json(raw / "handoff.json")
                    if (
                        env.get("sysbench_returncode") == 0
                        and not env.get("failure_type")
                        and handoff.get("handoff_counts", {}).get("handoff_events") is not None
                    ):
                        valid.append(run_id)
                if len(valid) >= args.runs:
                    log(f"Complete handoff {scheduler} t{threads}: valid={len(valid)}")
                    break
                run_ids = existing_run_ids(raw_root, scheduler, threads)
                next_id = (max(run_ids) + 1) if run_ids else 1
                if next_id > args.max_attempts_per_case:
                    log(f"ERROR too many handoff attempts for {scheduler} t{threads}; valid={len(valid)}")
                    break
                log(f"Run handoff {scheduler} t{threads} r{next_id} (valid {len(valid)}/{args.runs})")
                run_one(args, root, scheduler, threads, next_id)
                summarize(root, args)
                env_path = raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json"
                if env_path.exists() and load_json(env_path).get("failure_type") == "scheduler_startup_failure":
                    log(f"Skip remaining handoff runs for {scheduler} t{threads} after scheduler_startup_failure")
                    break


def write_readme(root: Path, args: argparse.Namespace) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "handoff_config.json", {
        "created_at": now_iso(),
        "workload": "MariaDB/Sysbench OLTP read-write",
        "phase": "handoff_locality",
        "threads": args.threads,
        "schedulers": args.schedulers,
        "runs": args.runs,
        "run_time": args.run_time,
        "warmup_time": args.warmup_time,
        "tables": args.tables,
        "table_size": args.table_size,
        "max_gap_ms": args.max_gap_ms,
        "include_same_tid": args.include_same_tid,
        "monitor": str(HANDOFF_MONITOR),
        "mariadb_restart_per_run": True,
        "cpu_pinning": "disabled",
    })
    readme = root / "README_handoff_locality.md"
    if not readme.exists():
        readme.write_text(
            "# MariaDB handoff locality evidence\n\n"
            "This directory contains direct same-lock handoff placement evidence.\n"
            "The handoff monitor is enabled only for the mechanism-evidence phase; "
            "do not use these runs as the primary TPS/latency measurements.\n\n"
            "- Raw runs: `handoff_locality/raw/`\n"
            "- Per-run summary: `summary_handoff_runs.csv`\n"
            "- Per-case summary: `summary_handoff_cases.csv`\n",
        )


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="MariaDB/Sysbench OLTP handoff locality evidence runner.")
    ap.add_argument("--root", default="", help="Result root. Default: test3 result/formal_mariadb_handoff_<timestamp>")
    ap.add_argument("--phase", choices=["run", "summarize", "smoke"], default="run")
    ap.add_argument("--threads", type=parse_list_int, default=DEFAULT_HANDOFF_THREADS)
    ap.add_argument("--schedulers", type=parse_list_str, default=DEFAULT_HANDOFF_SCHEDS)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--run-time", type=int, default=60)
    ap.add_argument("--warmup-time", type=int, default=10)
    ap.add_argument("--tables", type=int, default=16)
    ap.add_argument("--table-size", type=int, default=1_000_000)
    ap.add_argument("--report-interval", type=int, default=10)
    ap.add_argument("--db-name", default="sbtest")
    ap.add_argument("--db-user", default="sbuser")
    ap.add_argument("--db-pass", default="password")
    ap.add_argument("--max-gap-ms", type=int, default=100)
    ap.add_argument("--min-handoff-events", type=int, default=100)
    ap.add_argument("--include-same-tid", action="store_true")
    ap.add_argument("--monitor-library", default="")
    ap.add_argument("--monitor-start-delay", type=float, default=1.0)
    ap.add_argument("--max-attempts-per-case", type=int, default=5)
    ap.add_argument("--sysbench-timeout-slack", type=int, default=90)
    ap.add_argument("--sysbench-timeout-factor", type=float, default=2.0)
    ap.add_argument("--scx-arg", action="append", default=[], help="Scheduler args, e.g. --scx-arg scx_lockcoop='-t 4'")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    args.scx_args = formal.parse_scx_args(args.scx_arg)
    if args.phase == "smoke":
        args.threads = [256]
        args.schedulers = ["default", "scx_simple", "scx_lockboost", "scx_lockcoop", "scx_lockcoop"]
        args.runs = 1
        args.run_time = min(args.run_time, 10)
        args.warmup_time = min(args.warmup_time, 5)

    root = Path(args.root).resolve() if args.root else TEST_DIR / "result" / f"formal_mariadb_handoff_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ensure_monitor_built()
    write_readme(root, args)

    if shutil.which("sysbench") is None:
        raise SystemExit("error: sysbench not found")
    if sudo_cmd(["true"]).returncode != 0:
        raise SystemExit("error: sudo -n true failed; refresh sudo credential first")

    log(f"handoff root: {root}")
    log(f"phase: {args.phase}")
    if args.phase in ("run", "smoke"):
        run_matrix(args, root)
        summarize(root, args)
    elif args.phase == "summarize":
        summarize(root, args)

    state, rollback = formal.cleanup_scx()
    log(f"final sched_ext state: {state} rollback_failure={int(rollback)}")
    log(f"done root: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

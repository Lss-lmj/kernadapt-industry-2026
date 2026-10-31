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
OWNER_MONITOR = REPO_ROOT / "tools" / "owner-progress-monitor" / "owner_progress_monitor"

sys.path.insert(0, str(SCRIPT_DIR))
import formal_mariadb_oltp as formal  # noqa: E402

DEFAULT_OWNER_SCHEDS = ["default", "scx_simple", "scx_lockboost", "scx_lockcoop"]
DEFAULT_OWNER_THREADS = [64, 128, 256]


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
    if OWNER_MONITOR.exists() and os.access(OWNER_MONITOR, os.X_OK):
        return
    cp = run_cmd(["make", "-C", str(OWNER_MONITOR.parent)], timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "failed to build owner-progress monitor")


def raw_dir_for(root: Path, scheduler: str, threads: int, run_id: int) -> Path:
    return root / "owner_progress" / "raw" / f"{scheduler}_t{threads}_r{run_id:02d}"


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


def start_owner_monitor(args: argparse.Namespace, raw: Path, pid: str) -> tuple[subprocess.Popen[Any] | None, str | None]:
    out_json = raw / "owner_progress.json"
    log_path = raw / "owner_progress_monitor.log"
    cmd = [
        "sudo",
        "-n",
        str(OWNER_MONITOR),
        "--pid",
        pid,
        "--out-json",
        str(out_json),
        "--min-wait-us",
        str(args.min_wait_us),
        "--pthread-owner-offset",
        str(args.pthread_owner_offset),
    ]
    if args.monitor_library:
        cmd += ["--library", args.monitor_library]

    f = log_path.open("wb")
    proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
    time.sleep(args.monitor_start_delay)
    if proc.poll() is not None:
        f.close()
        detail = log_path.read_text(errors="replace") if log_path.exists() else ""
        return None, f"owner-progress monitor exited early rc={proc.returncode}: {detail[-1000:]}"
    return proc, None


def stop_owner_monitor(proc: subprocess.Popen[Any] | None) -> int | None:
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
        "phase": "owner_progress",
        "scheduler": scheduler,
        "threads": threads,
        "run_id": run_id,
        "start_timestamp": now_iso(),
        "kernel_version": run_cmd(["uname", "-r"]).stdout.strip(),
        "failure_type": "",
        "sysbench_returncode": None,
        "owner_monitor_returncode": None,
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
            log(f"Warmup owner-progress {scheduler} t{threads} r{run_id}: {args.warmup_time}s")
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
            env["failure_detail"] = "cannot find mariadbd pid for owner-progress monitor"
            return

        monitor_proc, err = start_owner_monitor(args, raw, pid)
        if err:
            env["failure_type"] = "owner_monitor_startup_failure"
            env["failure_detail"] = err
            return

        log(f"Measure owner-progress {scheduler} t{threads} r{run_id}: {args.run_time}s")
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

        env["owner_monitor_returncode"] = stop_owner_monitor(monitor_proc)
        monitor_proc = None
        if env["owner_monitor_returncode"] != 0 and not env.get("failure_type"):
            env["failure_type"] = "owner_monitor_failure"
        if not (raw / "owner_progress.json").exists() and not env.get("failure_type"):
            env["failure_type"] = "owner_missing_output"

        owner = load_json(raw / "owner_progress.json")
        events = owner.get("counters", {}).get("contention_events")
        env["contention_events"] = events
        min_events = 0 if threads <= 1 else args.min_contention_events
        if (events is None or int(events) < min_events) and not env.get("failure_type"):
            env["failure_type"] = "owner_too_few_events"
            env["failure_detail"] = f"contention_events={events}, min={min_events}"

    finally:
        if monitor_proc is not None:
            env["owner_monitor_returncode"] = stop_owner_monitor(monitor_proc)
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
    raw_root = root / "owner_progress" / "raw"
    rows: list[dict[str, Any]] = []
    if raw_root.exists():
        for raw in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            env = load_json(raw / "env.json")
            owner = load_json(raw / "owner_progress.json")
            metrics = formal.parse_sysbench_log(raw / "sysbench.log")
            failure = env.get("failure_type", "")
            valid = (
                env.get("sysbench_returncode") == 0
                and not failure
                and csv_value(owner, ("counters", "contention_events")) != ""
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
                "contention_events": csv_value(owner, ("counters", "contention_events")),
                "owner_already_running": csv_value(owner, ("counters", "owner_already_running")),
                "owner_not_running": csv_value(owner, ("counters", "owner_not_running")),
                "owner_run_delay_samples": csv_value(owner, ("counters", "owner_run_delay_samples")),
                "owner_unlock_delay_samples": csv_value(owner, ("counters", "owner_unlock_delay_samples")),
                "waiter_wait_samples": csv_value(owner, ("counters", "waiter_wait_samples")),
                "fanin_samples": csv_value(owner, ("counters", "fanin_samples")),
                "owner_already_running_ratio": csv_value(owner, ("ratios", "owner_already_running")),
                "owner_not_running_ratio": csv_value(owner, ("ratios", "owner_not_running")),
                "owner_run_delay_coverage": csv_value(owner, ("ratios", "owner_run_delay_coverage")),
                "owner_unlock_delay_coverage": csv_value(owner, ("ratios", "owner_unlock_delay_coverage")),
                "waiter_wait_coverage": csv_value(owner, ("ratios", "waiter_wait_coverage")),
                "owner_next_run_p50_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p50_us")),
                "owner_next_run_p95_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p95_us")),
                "owner_next_run_p99_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p99_us")),
                "owner_unlock_p50_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p50_us")),
                "owner_unlock_p95_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p95_us")),
                "owner_unlock_p99_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p99_us")),
                "waiter_wait_p50_us": csv_value(owner, ("histograms", "waiter_lock_wait_us", "p50_us")),
                "waiter_wait_p95_us": csv_value(owner, ("histograms", "waiter_lock_wait_us", "p95_us")),
                "waiter_wait_p99_us": csv_value(owner, ("histograms", "waiter_lock_wait_us", "p99_us")),
                "waiter_fanin_p50": csv_value(owner, ("histograms", "waiter_fanin", "p50_us")),
                "waiter_fanin_p95": csv_value(owner, ("histograms", "waiter_fanin", "p95_us")),
                "waiter_fanin_p99": csv_value(owner, ("histograms", "waiter_fanin", "p99_us")),
                "start_timestamp": env.get("start_timestamp", ""),
                "end_timestamp": env.get("end_timestamp", ""),
                "raw_dir": str(raw),
            })

    summary_dir = root / "owner_progress"
    write_csv(summary_dir / "summary_owner_runs.csv", rows)
    write_csv(root / "summary_owner_runs.csv", rows)

    cases: list[dict[str, Any]] = []
    for scheduler in args.schedulers:
        for threads in args.threads:
            vals = [r for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] == 1]
            cases.append({
                "scheduler": scheduler,
                "threads": threads,
                "n": len(vals),
                "contention_events_mean": mean_field(vals, "contention_events"),
                "owner_next_run_p50_us_mean": mean_field(vals, "owner_next_run_p50_us"),
                "owner_next_run_p95_us_mean": mean_field(vals, "owner_next_run_p95_us"),
                "owner_unlock_p50_us_mean": mean_field(vals, "owner_unlock_p50_us"),
                "owner_unlock_p95_us_mean": mean_field(vals, "owner_unlock_p95_us"),
                "waiter_wait_p50_us_mean": mean_field(vals, "waiter_wait_p50_us"),
                "waiter_wait_p95_us_mean": mean_field(vals, "waiter_wait_p95_us"),
                "waiter_fanin_p95_mean": mean_field(vals, "waiter_fanin_p95"),
                "owner_already_running_ratio_mean": mean_field(vals, "owner_already_running_ratio"),
                "owner_run_delay_coverage_mean": mean_field(vals, "owner_run_delay_coverage"),
                "owner_unlock_delay_coverage_mean": mean_field(vals, "owner_unlock_delay_coverage"),
                "waiter_wait_coverage_mean": mean_field(vals, "waiter_wait_coverage"),
                "tps_mean": mean_field(vals, "tps"),
                "lat_p95_mean": mean_field(vals, "lat_p95"),
                "failures": sum(1 for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] != 1),
            })
    write_csv(summary_dir / "summary_owner_cases.csv", cases)
    write_csv(root / "summary_owner_cases.csv", cases)


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
    raw_root = root / "owner_progress" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scheduler in args.schedulers:
            while True:
                valid = []
                for run_id in existing_run_ids(raw_root, scheduler, threads):
                    raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
                    env = load_json(raw / "env.json")
                    owner = load_json(raw / "owner_progress.json")
                    if (
                        env.get("sysbench_returncode") == 0
                        and not env.get("failure_type")
                        and owner.get("counters", {}).get("contention_events") is not None
                    ):
                        valid.append(run_id)
                if len(valid) >= args.runs:
                    log(f"Complete owner-progress {scheduler} t{threads}: valid={len(valid)}")
                    break
                run_ids = existing_run_ids(raw_root, scheduler, threads)
                next_id = (max(run_ids) + 1) if run_ids else 1
                if next_id > args.max_attempts_per_case:
                    log(f"ERROR too many owner-progress attempts for {scheduler} t{threads}; valid={len(valid)}")
                    break
                log(f"Run owner-progress {scheduler} t{threads} r{next_id} (valid {len(valid)}/{args.runs})")
                run_one(args, root, scheduler, threads, next_id)
                summarize(root, args)
                env_path = raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json"
                if env_path.exists() and load_json(env_path).get("failure_type") == "scheduler_startup_failure":
                    log(f"Skip remaining owner-progress runs for {scheduler} t{threads} after scheduler_startup_failure")
                    break


def write_readme(root: Path, args: argparse.Namespace) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "owner_progress_config.json", {
        "created_at": now_iso(),
        "workload": "MariaDB/Sysbench OLTP read-write",
        "phase": "owner_progress",
        "threads": args.threads,
        "schedulers": args.schedulers,
        "runs": args.runs,
        "run_time": args.run_time,
        "warmup_time": args.warmup_time,
        "tables": args.tables,
        "table_size": args.table_size,
        "min_wait_us": args.min_wait_us,
        "pthread_owner_offset": args.pthread_owner_offset,
        "monitor": str(OWNER_MONITOR),
        "mariadb_restart_per_run": True,
        "cpu_pinning": "disabled",
    })
    readme = root / "README_owner_progress.md"
    if not readme.exists():
        readme.write_text(
            "# MariaDB owner-progress evidence\n\n"
            "This directory contains independent owner-progress monitor evidence. "
            "The monitor is enabled only for mechanism-evidence runs; do not use "
            "these runs as primary TPS/latency measurements.\n\n"
            "- Raw runs: `owner_progress/raw/`\n"
            "- Per-run summary: `summary_owner_runs.csv`\n"
            "- Per-case summary: `summary_owner_cases.csv`\n",
        )


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="MariaDB/Sysbench OLTP owner-progress evidence runner.")
    ap.add_argument("--root", default="", help="Result root. Default: test3 result/formal_mariadb_owner_progress_<timestamp>")
    ap.add_argument("--phase", choices=["run", "summarize", "smoke"], default="run")
    ap.add_argument("--threads", type=parse_list_int, default=DEFAULT_OWNER_THREADS)
    ap.add_argument("--schedulers", type=parse_list_str, default=DEFAULT_OWNER_SCHEDS)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--run-time", type=int, default=60)
    ap.add_argument("--warmup-time", type=int, default=10)
    ap.add_argument("--tables", type=int, default=16)
    ap.add_argument("--table-size", type=int, default=1_000_000)
    ap.add_argument("--report-interval", type=int, default=10)
    ap.add_argument("--db-name", default="sbtest")
    ap.add_argument("--db-user", default="sbuser")
    ap.add_argument("--db-pass", default="password")
    ap.add_argument("--min-wait-us", type=int, default=1)
    ap.add_argument("--pthread-owner-offset", type=int, default=8)
    ap.add_argument("--min-contention-events", type=int, default=1)
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
        args.threads = [64]
        args.schedulers = ["default", "scx_simple", "scx_lockboost", "scx_lockcoop"]
        args.runs = 1
        args.run_time = min(args.run_time, 10)
        args.warmup_time = min(args.warmup_time, 5)

    root = Path(args.root).resolve() if args.root else TEST_DIR / "result" / f"formal_mariadb_owner_progress_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ensure_monitor_built()
    write_readme(root, args)

    if shutil.which("sysbench") is None:
        raise SystemExit("error: sysbench not found")
    if sudo_cmd(["true"]).returncode != 0:
        raise SystemExit("error: sudo -n true failed; refresh sudo credential first")

    log(f"owner-progress root: {root}")
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

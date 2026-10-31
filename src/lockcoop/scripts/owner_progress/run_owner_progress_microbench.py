#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
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
BENCH = TEST_DIR / "owner_progress_bench"
OWNER_MONITOR = REPO_ROOT / "tools" / "owner-progress-monitor" / "owner_progress_monitor"
SCX_BIN_DIR = REPO_ROOT / "lock-sched" / "tools" / "sched_ext" / "build" / "bin"

DEFAULT_SCHEDS = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_eevdf",
]
ALL_SCX_SCHEDS = [
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_eevdf",
    "scx_nest",
    "scx_batch",
    "scx_qmap",
    "scx_tailguard",
    "scx_toponest",
    "scx_central",
    "scx_flatcg",
]

FLOAT_RE = r"([0-9]+(?:\.[0-9]+)?)"
INT_RE = r"([0-9]+)"
BENCH_PATTERNS: dict[str, re.Pattern[str]] = {
    "mode_reported": re.compile(r"^Mode\s*:\s*(\S+)\s*$"),
    "threads_reported": re.compile(rf"^Threads\s*:\s*{INT_RE}\s*$"),
    "hog_threads_reported": re.compile(rf"^Hog threads\s*:\s*{INT_RE}\s*$"),
    "groups_reported": re.compile(rf"^Groups\s*:\s*{INT_RE}\s*$"),
    "waiters_reported": re.compile(rf"^Waiters\s*:\s*{INT_RE}\s*$"),
    "waiters_per_group_reported": re.compile(rf"^Waiters/group\s*:\s*{INT_RE}\s*$"),
    "locks_reported": re.compile(rf"^Locks\s*:\s*{INT_RE}\s*$"),
    "duration_reported_s": re.compile(rf"^Duration\s*:\s*{FLOAT_RE}\s+s\s*$"),
    "critical_us_reported": re.compile(rf"^Critical section\s*:\s*{INT_RE}\s+us\s*$"),
    "outside_us_reported": re.compile(rf"^Outside work\s*:\s*{INT_RE}\s+us\s*$"),
    "owner_yields_reported": re.compile(rf"^Owner yields\s*:\s*{INT_RE}\s*$"),
    "pin_cpus_reported": re.compile(rf"^Pinned CPUs\s*:\s*{INT_RE}\s*$"),
    "owner_rounds": re.compile(rf"^Owner rounds\s*:\s*{INT_RE}\s*$"),
    "hog_spins": re.compile(rf"^Hog spins\s*:\s*{INT_RE}\s*$"),
    "total_ops": re.compile(rf"^Total ops\s*:\s*{INT_RE}\s*$"),
    "throughput_ops_s": re.compile(rf"^Throughput\s*:\s*{FLOAT_RE}\s+ops/s\s*$"),
    "ops_min_per_thread": re.compile(rf"^Ops/thread min\s*:\s*{INT_RE}\s*$"),
    "ops_max_per_thread": re.compile(rf"^Ops/thread max\s*:\s*{INT_RE}\s*$"),
    "ops_mean_per_thread": re.compile(rf"^Ops/thread mean\s*:\s*{FLOAT_RE}\s*$"),
    "zero_progress_threads": re.compile(rf"^Zero-progress thr\s*:\s*{INT_RE}\s*$"),
    "jain_fairness": re.compile(rf"^Jain fairness\s*:\s*{FLOAT_RE}\s*$"),
    "lock_wait_avg_us": re.compile(rf"^Lock wait avg\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "lock_wait_p50_us": re.compile(rf"^Lock wait p50\s*:\s*{INT_RE}\s+us\s*$"),
    "lock_wait_p95_us": re.compile(rf"^Lock wait p95\s*:\s*{INT_RE}\s+us\s*$"),
    "lock_wait_p99_us": re.compile(rf"^Lock wait p99\s*:\s*{INT_RE}\s+us\s*$"),
    "lock_wait_p999_us": re.compile(rf"^Lock wait p99\.9\s*:\s*{INT_RE}\s+us\s*$"),
    "lock_wait_max_us": re.compile(rf"^Lock wait max\s*:\s*{FLOAT_RE}\s+us\s*$"),
}


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def parse_list_int(s: str) -> list[int]:
    return [int(x) for x in s.replace(",", " ").split() if x.strip()]


def parse_list_str(s: str) -> list[str]:
    return [x for x in s.replace(",", " ").split() if x.strip()]


def run_cmd(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def sudo_cmd(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_cmd(["sudo", "-n", *args], timeout=timeout)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


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
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def scx_state() -> str:
    p = Path("/sys/kernel/sched_ext/state")
    if not p.exists():
        return "unknown"
    try:
        return p.read_text().strip()
    except OSError:
        return "unknown"


def wait_scx_disabled(timeout_s: float = 5.0) -> str:
    deadline = time.time() + timeout_s
    state = scx_state()
    while time.time() < deadline:
        state = scx_state()
        if state == "disabled":
            return state
        time.sleep(0.1)
    return state


def cleanup_scx(proc: subprocess.Popen[Any] | None = None) -> tuple[str, bool]:
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    for sched in ALL_SCX_SCHEDS:
        sudo_cmd(["pkill", "-TERM", "-f", str(SCX_BIN_DIR / sched)], timeout=5)

    state = wait_scx_disabled(5)
    if state != "disabled":
        for sched in ALL_SCX_SCHEDS:
            sudo_cmd(["pkill", "-KILL", "-f", str(SCX_BIN_DIR / sched)], timeout=5)
        state = wait_scx_disabled(5)
    return state, state != "disabled"


def parse_scx_args(items: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"invalid --scx-arg: {item}; expected scheduler='args'")
        sched, value = item.split("=", 1)
        out[sched.strip()] = [x for x in value.strip().split() if x]
    return out


def start_scheduler(scheduler: str, raw_dir: Path, scx_args: list[str]) -> tuple[subprocess.Popen[Any] | None, str | None]:
    if scheduler == "default":
        return None, None
    binary = SCX_BIN_DIR / scheduler
    if not binary.exists() or not os.access(binary, os.X_OK):
        return None, f"scx binary not executable: {binary}"

    log_path = raw_dir / "scheduler.log"
    f = log_path.open("ab")
    proc = subprocess.Popen(["sudo", "-n", str(binary), *scx_args], stdout=f, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    if proc.poll() is not None:
        f.close()
        return None, f"scheduler exited early with returncode {proc.returncode}"

    deadline = time.time() + 5.0
    state = scx_state()
    while time.time() < deadline:
        state = scx_state()
        if state != "disabled":
            return proc, None
        time.sleep(0.1)

    try:
        proc.terminate()
    except OSError:
        pass
    f.close()
    return None, f"sched_ext state stayed disabled after start; last_state={state}"


def scheduler_log_failure(raw: Path) -> str:
    path = raw / "scheduler.log"
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    if "EXIT: scx_bpf_error" in text or "scx_bpf_error" in text:
        m = re.search(r"EXIT:\s*(.+)", text)
        return m.group(1).strip() if m else "scx_bpf_error in scheduler.log"
    return ""


def ensure_built() -> None:
    cp = run_cmd(["make", "-C", str(TEST_DIR)], timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "failed to build owner_progress_bench")
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


def start_monitor(args: argparse.Namespace, raw: Path, pid: int) -> tuple[subprocess.Popen[Any] | None, str | None]:
    out_json = raw / "owner_progress.json"
    log_path = raw / "owner_progress_monitor.log"
    cmd = [
        "sudo",
        "-n",
        str(OWNER_MONITOR),
        "--pid",
        str(pid),
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
        return None, f"owner-progress monitor exited early rc={proc.returncode}: {detail[-1200:]}"
    return proc, None


def stop_process(proc: subprocess.Popen[Any] | None, *, timeout: int = 10) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return proc.returncode
    try:
        proc.send_signal(signal.SIGTERM)
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=5)


def bench_cmd(args: argparse.Namespace, threads: int) -> list[str]:
    return [
        str(BENCH),
        "--mode",
        str(args.mode),
        "--threads",
        str(threads),
        "--locks",
        str(args.locks),
        "--groups",
        str(args.groups),
        "--hog-threads",
        str(args.hog_threads),
        "--duration",
        str(args.duration),
        "--critical-us",
        str(args.critical_us),
        "--outside-us",
        str(args.outside_us),
        "--owner-yields",
        str(args.owner_yields),
        "--pin-cpus",
        str(args.pin_cpus),
        "--start-delay-ms",
        str(args.start_delay_ms),
    ]


def run_one(args: argparse.Namespace, root: Path, scheduler: str, threads: int, run_id: int) -> None:
    raw = raw_dir_for(root, scheduler, threads, run_id)
    raw.mkdir(parents=True, exist_ok=True)
    env: dict[str, Any] = {
        "benchmark": "owner-progress microbenchmark",
        "scheduler": scheduler,
        "threads": threads,
        "mode": args.mode,
        "run_id": run_id,
        "locks": args.locks,
        "groups": args.groups,
        "hog_threads": args.hog_threads,
        "duration_s": args.duration,
        "critical_us": args.critical_us,
        "outside_us": args.outside_us,
        "owner_yields": args.owner_yields,
        "pin_cpus": args.pin_cpus,
        "start_delay_ms": args.start_delay_ms,
        "monitor": str(OWNER_MONITOR),
        "monitor_scope": "pthread_mutex_lock/unlock uprobes plus sched_switch tracepoint",
        "start_timestamp": now_iso(),
        "kernel_version": run_cmd(["uname", "-r"]).stdout.strip(),
        "failure_type": "",
        "bench_returncode": None,
        "owner_monitor_returncode": None,
    }
    write_json(raw / "env.json", env)

    scx_proc: subprocess.Popen[Any] | None = None
    bench_proc: subprocess.Popen[Any] | None = None
    monitor_proc: subprocess.Popen[Any] | None = None

    try:
        initial_state, rollback = cleanup_scx()
        env["sched_ext_initial_state"] = initial_state
        if rollback or initial_state != "disabled":
            env["failure_type"] = "pre_run_rollback_failure"
            env["failure_detail"] = f"sched_ext not clean before run: state={initial_state} rollback={int(rollback)}"
            return

        if scheduler != "default":
            scx_proc, err = start_scheduler(scheduler, raw, args.scx_args.get(scheduler, []))
            if err:
                env["failure_type"] = "scheduler_startup_failure"
                env["failure_detail"] = err
                return
        env["sched_ext_after_start_state"] = scx_state()

        cmd = bench_cmd(args, threads)
        env["bench_cmd"] = cmd
        with (raw / "bench.log").open("wb") as f:
            f.write(("# start: " + now_iso() + "\n").encode())
            f.write(("# cmd: " + " ".join(cmd) + "\n\n").encode())
            f.flush()
            bench_proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
            monitor_proc, err = start_monitor(args, raw, bench_proc.pid)
            if err:
                env["failure_type"] = "owner_monitor_startup_failure"
                env["failure_detail"] = err
                return

            timeout_s = args.duration + math.ceil(args.start_delay_ms / 1000.0) + args.bench_timeout_slack
            timed_out = False
            try:
                env["bench_returncode"] = bench_proc.wait(timeout=timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                env["bench_timed_out"] = True
                try:
                    os.killpg(bench_proc.pid, signal.SIGTERM)
                except OSError:
                    pass
                try:
                    env["bench_returncode"] = bench_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(bench_proc.pid, signal.SIGKILL)
                    except OSError:
                        pass
                    env["bench_returncode"] = bench_proc.wait(timeout=10)
            f.write(("\n# end: " + now_iso() + "\n").encode())
            f.write(("# returncode: " + str(env["bench_returncode"]) + "\n").encode())
            f.write(("# timed_out: " + str(int(timed_out)) + "\n").encode())

        env["owner_monitor_returncode"] = stop_process(monitor_proc)
        monitor_proc = None
        if env["bench_returncode"] != 0 and not env.get("failure_type"):
            env["failure_type"] = "bench_failure"
        if env["owner_monitor_returncode"] != 0 and not env.get("failure_type"):
            env["failure_type"] = "owner_monitor_failure"
        if not (raw / "owner_progress.json").exists() and not env.get("failure_type"):
            env["failure_type"] = "owner_missing_output"

        owner = load_json(raw / "owner_progress.json")
        events = owner.get("counters", {}).get("contention_events")
        env["contention_events"] = events
        if (events is None or int(events) < args.min_contention_events) and not env.get("failure_type"):
            env["failure_type"] = "owner_too_few_events"
            env["failure_detail"] = f"contention_events={events}, min={args.min_contention_events}"

    finally:
        if monitor_proc is not None:
            env["owner_monitor_returncode"] = stop_process(monitor_proc)
        if bench_proc is not None and bench_proc.poll() is None:
            try:
                os.killpg(bench_proc.pid, signal.SIGTERM)
            except OSError:
                pass
            stop_process(bench_proc)
        final_state, rollback = cleanup_scx(scx_proc)
        env["sched_ext_final_state"] = final_state
        sched_failure = scheduler_log_failure(raw)
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


def parse_bench_log(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        for key, pattern in BENCH_PATTERNS.items():
            m = pattern.match(line)
            if not m:
                continue
            value = m.group(1)
            if key in {"mode_reported"}:
                out[key] = value
            elif key in {"throughput_ops_s", "duration_reported_s", "ops_mean_per_thread", "jain_fairness", "lock_wait_avg_us", "lock_wait_max_us"}:
                out[key] = float(value)
            else:
                out[key] = int(value)
            break
    return out


def csv_value(obj: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = obj
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return "" if cur is None else cur


def to_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v):
        return None
    return v


def fmt(value: float | None) -> str:
    if value is None or math.isnan(value):
        return ""
    return f"{value:.6f}"


def mean_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = [v for r in rows if (v := to_float(r.get(field))) is not None]
    return fmt(statistics.mean(vals) if vals else None)


def stdev_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = [v for r in rows if (v := to_float(r.get(field))) is not None]
    if len(vals) < 2:
        return ""
    return fmt(statistics.stdev(vals))


def cv_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = [v for r in rows if (v := to_float(r.get(field))) is not None]
    if len(vals) < 2:
        return ""
    m = statistics.mean(vals)
    if m == 0:
        return ""
    return fmt(statistics.stdev(vals) / m)


def summarize(root: Path, args: argparse.Namespace) -> None:
    raw_root = root / "owner_progress" / "raw"
    rows: list[dict[str, Any]] = []
    if raw_root.exists():
        for raw in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            env = load_json(raw / "env.json")
            owner = load_json(raw / "owner_progress.json")
            bench = parse_bench_log(raw / "bench.log")
            failure = env.get("failure_type", "")
            valid = (
                env.get("bench_returncode") == 0
                and not failure
                and bench.get("throughput_ops_s") is not None
                and csv_value(owner, ("counters", "contention_events")) != ""
            )
            rows.append({
                "scheduler": env.get("scheduler", ""),
                "threads": env.get("threads", ""),
                "mode": env.get("mode", ""),
                "run_id": env.get("run_id", ""),
                "valid": int(valid),
                "failure_type": failure,
                "failure_detail": env.get("failure_detail", ""),
                "locks": env.get("locks", ""),
                "groups": env.get("groups", ""),
                "hog_threads": env.get("hog_threads", ""),
                "duration_s": env.get("duration_s", ""),
                "critical_us": env.get("critical_us", ""),
                "outside_us": env.get("outside_us", ""),
                "owner_yields": env.get("owner_yields", ""),
                "pin_cpus": env.get("pin_cpus", ""),
                "owner_rounds": bench.get("owner_rounds", ""),
                "hog_spins": bench.get("hog_spins", ""),
                "throughput_ops_s": bench.get("throughput_ops_s", ""),
                "total_ops": bench.get("total_ops", ""),
                "ops_min_per_thread": bench.get("ops_min_per_thread", ""),
                "ops_max_per_thread": bench.get("ops_max_per_thread", ""),
                "ops_mean_per_thread": bench.get("ops_mean_per_thread", ""),
                "zero_progress_threads": bench.get("zero_progress_threads", ""),
                "jain_fairness": bench.get("jain_fairness", ""),
                "lock_wait_avg_us": bench.get("lock_wait_avg_us", ""),
                "lock_wait_p50_us": bench.get("lock_wait_p50_us", ""),
                "lock_wait_p95_us": bench.get("lock_wait_p95_us", ""),
                "lock_wait_p99_us": bench.get("lock_wait_p99_us", ""),
                "lock_wait_p999_us": bench.get("lock_wait_p999_us", ""),
                "lock_wait_max_us": bench.get("lock_wait_max_us", ""),
                "contention_events": csv_value(owner, ("counters", "contention_events")),
                "owner_already_running": csv_value(owner, ("counters", "owner_already_running")),
                "owner_not_running": csv_value(owner, ("counters", "owner_not_running")),
                "contended_owner_tracked": csv_value(owner, ("counters", "contended_owner_tracked")),
                "owner_offcpu_events": csv_value(owner, ("counters", "owner_offcpu_events")),
                "owner_preempted_events": csv_value(owner, ("counters", "owner_preempted_events")),
                "owner_offcpu_delay_samples": csv_value(owner, ("counters", "owner_offcpu_delay_samples")),
                "owner_runnable_offcpu_delay_samples": csv_value(owner, ("counters", "owner_runnable_offcpu_delay_samples")),
                "owner_run_delay_samples": csv_value(owner, ("counters", "owner_run_delay_samples")),
                "owner_unlock_delay_samples": csv_value(owner, ("counters", "owner_unlock_delay_samples")),
                "waiter_wait_samples": csv_value(owner, ("counters", "waiter_wait_samples")),
                "fanin_samples": csv_value(owner, ("counters", "fanin_samples")),
                "owner_already_running_ratio": csv_value(owner, ("ratios", "owner_already_running")),
                "owner_not_running_ratio": csv_value(owner, ("ratios", "owner_not_running")),
                "owner_run_delay_coverage": csv_value(owner, ("ratios", "owner_run_delay_coverage")),
                "owner_unlock_delay_coverage": csv_value(owner, ("ratios", "owner_unlock_delay_coverage")),
                "owner_offcpu_events_per_contention": csv_value(owner, ("ratios", "owner_offcpu_events_per_contention")),
                "owner_preempted_events_per_contention": csv_value(owner, ("ratios", "owner_preempted_events_per_contention")),
                "owner_runnable_offcpu_fraction": csv_value(owner, ("ratios", "owner_runnable_offcpu_fraction")),
                "waiter_wait_coverage": csv_value(owner, ("ratios", "waiter_wait_coverage")),
                "owner_next_run_p50_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p50_us")),
                "owner_next_run_p95_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p95_us")),
                "owner_next_run_p99_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p99_us")),
                "owner_unlock_p50_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p50_us")),
                "owner_unlock_p95_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p95_us")),
                "owner_unlock_p99_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p99_us")),
                "owner_offcpu_p50_us": csv_value(owner, ("histograms", "owner_offcpu_delay_us", "p50_us")),
                "owner_offcpu_p95_us": csv_value(owner, ("histograms", "owner_offcpu_delay_us", "p95_us")),
                "owner_offcpu_p99_us": csv_value(owner, ("histograms", "owner_offcpu_delay_us", "p99_us")),
                "owner_runnable_offcpu_p50_us": csv_value(owner, ("histograms", "owner_runnable_offcpu_delay_us", "p50_us")),
                "owner_runnable_offcpu_p95_us": csv_value(owner, ("histograms", "owner_runnable_offcpu_delay_us", "p95_us")),
                "owner_runnable_offcpu_p99_us": csv_value(owner, ("histograms", "owner_runnable_offcpu_delay_us", "p99_us")),
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

    scheds = args.schedulers if args.schedulers else sorted({str(r["scheduler"]) for r in rows if r.get("scheduler")})
    threads_list = args.threads if args.threads else sorted({int(r["threads"]) for r in rows if str(r.get("threads", "")).isdigit()})
    cases: list[dict[str, Any]] = []
    metric_fields = [
        "throughput_ops_s",
        "lock_wait_p95_us",
        "lock_wait_p99_us",
        "lock_wait_p999_us",
        "owner_next_run_p95_us",
        "owner_next_run_p99_us",
        "owner_unlock_p95_us",
        "owner_unlock_p99_us",
        "waiter_wait_p95_us",
        "waiter_wait_p99_us",
        "waiter_fanin_p95",
        "owner_already_running_ratio",
        "owner_not_running_ratio",
        "owner_run_delay_coverage",
        "owner_unlock_delay_coverage",
        "owner_offcpu_p95_us",
        "owner_offcpu_p99_us",
        "owner_runnable_offcpu_p95_us",
        "owner_runnable_offcpu_p99_us",
        "owner_offcpu_events_per_contention",
        "owner_preempted_events_per_contention",
        "owner_runnable_offcpu_fraction",
        "owner_offcpu_events",
        "owner_preempted_events",
        "waiter_wait_coverage",
        "contention_events",
        "owner_rounds",
        "hog_spins",
    ]
    for scheduler in scheds:
        for threads in threads_list:
            vals = [
                r for r in rows
                if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] == 1
            ]
            case: dict[str, Any] = {
                "scheduler": scheduler,
                "threads": threads,
                "n": len(vals),
                "failures": sum(
                    1 for r in rows
                    if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] != 1
                ),
            }
            for field in metric_fields:
                case[f"{field}_mean"] = mean_field(vals, field)
                case[f"{field}_stdev"] = stdev_field(vals, field)
                case[f"{field}_cv"] = cv_field(vals, field)
            cases.append(case)
    write_csv(summary_dir / "summary_owner_cases.csv", cases)
    write_csv(root / "summary_owner_cases.csv", cases)


def write_config(root: Path, args: argparse.Namespace) -> None:
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "owner_progress_microbench_config.json", {
        "created_at": now_iso(),
        "benchmark": "owner-progress microbenchmark",
        "mode": args.mode,
        "schedulers": args.schedulers,
        "threads": args.threads,
        "runs": args.runs,
        "duration_s": args.duration,
        "locks": args.locks,
        "groups": args.groups,
        "hog_threads": args.hog_threads,
        "critical_us": args.critical_us,
        "outside_us": args.outside_us,
        "owner_yields": args.owner_yields,
        "pin_cpus": args.pin_cpus,
        "start_delay_ms": args.start_delay_ms,
        "min_wait_us": args.min_wait_us,
        "pthread_owner_offset": args.pthread_owner_offset,
        "monitor": str(OWNER_MONITOR),
        "scheduler_bin_dir": str(SCX_BIN_DIR),
        "cpu_pinning": "first N CPUs" if args.pin_cpus else "disabled",
        "scx_nest_included": False,
    })


def run_matrix(args: argparse.Namespace, root: Path) -> None:
    raw_root = root / "owner_progress" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scheduler in args.schedulers:
            while True:
                valid: list[int] = []
                for run_id in existing_run_ids(raw_root, scheduler, threads):
                    raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
                    env = load_json(raw / "env.json")
                    bench = parse_bench_log(raw / "bench.log")
                    owner = load_json(raw / "owner_progress.json")
                    if (
                        env.get("bench_returncode") == 0
                        and not env.get("failure_type")
                        and bench.get("throughput_ops_s") is not None
                        and owner.get("counters", {}).get("contention_events") is not None
                    ):
                        valid.append(run_id)
                if len(valid) >= args.runs:
                    log(f"Complete {scheduler} t{threads}: valid={len(valid)}")
                    break
                run_ids = existing_run_ids(raw_root, scheduler, threads)
                next_id = (max(run_ids) + 1) if run_ids else 1
                if next_id > args.max_attempts_per_case:
                    log(f"ERROR too many attempts for {scheduler} t{threads}; valid={len(valid)}")
                    break
                log(f"Run {scheduler} t{threads} r{next_id} (valid {len(valid)}/{args.runs})")
                run_one(args, root, scheduler, threads, next_id)
                summarize(root, args)
                env_path = raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json"
                env = load_json(env_path)
                if env.get("failure_type") == "scheduler_startup_failure":
                    log(f"Skip remaining runs for {scheduler} t{threads} after scheduler_startup_failure")
                    break


def call_plot(root: Path) -> None:
    script = SCRIPT_DIR / "plot_owner_progress.py"
    cp = subprocess.run([sys.executable, str(script), "--root", str(root)], text=True)
    if cp.returncode != 0:
        raise RuntimeError("plot_owner_progress.py failed")


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Owner-progress microbenchmark runner.")
    ap.add_argument("--root", default="", help="Result root. Default: test15 result/owner_progress_microbench_<timestamp>")
    ap.add_argument("--phase", choices=["run", "summarize", "plot", "smoke"], default="run")
    ap.add_argument("--schedulers", type=parse_list_str, default=DEFAULT_SCHEDS)
    ap.add_argument("--threads", type=parse_list_int, default=[64, 128, 256])
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--mode", choices=["phased", "convoy", "owner-preempt"], default="phased")
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--locks", type=int, default=1)
    ap.add_argument("--groups", type=int, default=8)
    ap.add_argument("--hog-threads", type=int, default=0)
    ap.add_argument("--critical-us", type=int, default=100)
    ap.add_argument("--outside-us", type=int, default=0)
    ap.add_argument("--owner-yields", type=int, default=0)
    ap.add_argument("--pin-cpus", type=int, default=0)
    ap.add_argument("--start-delay-ms", type=int, default=1000)
    ap.add_argument("--min-wait-us", type=int, default=1)
    ap.add_argument("--pthread-owner-offset", type=int, default=8)
    ap.add_argument("--monitor-library", default="")
    ap.add_argument("--monitor-start-delay", type=float, default=0.5)
    ap.add_argument("--min-contention-events", type=int, default=1)
    ap.add_argument("--bench-timeout-slack", type=int, default=60)
    ap.add_argument("--max-attempts-per-case", type=int, default=8)
    ap.add_argument("--scx-arg", action="append", default=[], help="Scheduler args, e.g. --scx-arg scx_lockboost='-t 4 -d 100'")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    args.scx_args = parse_scx_args(args.scx_arg)
    if args.phase == "smoke":
        args.schedulers = ["default", "scx_simple", "scx_lockboost"]
        args.threads = [32]
        args.runs = 1
        args.mode = "owner-preempt"
        args.groups = 4
        args.duration = min(args.duration, 5)
        args.critical_us = max(args.critical_us, 2000)
        args.hog_threads = max(args.hog_threads, 32)
        args.pin_cpus = args.pin_cpus or 8

    root = Path(args.root).resolve() if args.root else TEST_DIR / "result" / f"owner_progress_microbench_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ensure_built()
    write_config(root, args)

    if not BENCH.exists() or not os.access(BENCH, os.X_OK):
        raise SystemExit(f"error: benchmark not executable: {BENCH}")
    if not OWNER_MONITOR.exists() or not os.access(OWNER_MONITOR, os.X_OK):
        raise SystemExit(f"error: owner-progress monitor not executable: {OWNER_MONITOR}")
    if sudo_cmd(["true"]).returncode != 0:
        raise SystemExit("error: sudo -n true failed; configure passwordless sudo or refresh sudo credential")

    log(f"root: {root}")
    log(f"phase: {args.phase}")
    if args.phase in ("run", "smoke"):
        run_matrix(args, root)
        summarize(root, args)
        call_plot(root)
    elif args.phase == "summarize":
        summarize(root, args)
    elif args.phase == "plot":
        call_plot(root)

    state, rollback = cleanup_scx()
    log(f"final sched_ext state: {state} rollback_failure={int(rollback)}")
    log(f"summary runs: {root / 'summary_owner_runs.csv'}")
    log(f"summary cases: {root / 'summary_owner_cases.csv'}")
    log(f"done root: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

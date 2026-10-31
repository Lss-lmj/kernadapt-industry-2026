#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
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
BENCH = TEST_DIR / "futex_mutex_bench"
OWNER_MONITOR = REPO_ROOT / "tools" / "owner-progress-monitor" / "owner_progress_monitor"
SCX_BIN_DIR = REPO_ROOT / "lock-sched" / "tools" / "sched_ext" / "build" / "bin"

DEFAULT_SCHEDS = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_locknest_v8",
    "scx_lockcoop",
    "scx_lockboost4",
    "scx_lockboost5",
    "scx_lockboost6",
    "scx_eevdf",
]
ALL_SCX_SCHEDS = [
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_locknest_v8",
    "scx_lockcoop",
    "scx_eevdf",
    "scx_nest",
    "scx_batch",
    "scx_qmap",
    "scx_tailguard",
    "scx_toponest",
    "scx_central",
    "scx_flatcg",
    "scx_locknest_v9",
    "scx_lockboost2",
    "scx_lockboost3",
    "scx_lockboost4",
    "scx_lockboost5",
    "scx_lockboost6",
]
SCX_SCHED_ALIASES = {
    "scx_locknest_v8_weak": ("scx_locknest_v8", ["-F", "weak"]),
    "scx_locknest_v8_current": ("scx_locknest_v8", ["-F", "current"]),
    "scx_locknest_v8_mutexonly": ("scx_locknest_v8", ["-F", "mutexonly"]),
    "scx_locknest_v8_futexonly": ("scx_locknest_v8", ["-F", "futexonly"]),
    "scx_locknest_v9_weak": ("scx_locknest_v9", ["-F", "weak"]),
    "scx_locknest_v9_current": ("scx_locknest_v9", ["-F", "current"]),
    "scx_locknest_v9_mutexonly": ("scx_locknest_v9", ["-F", "mutexonly"]),
    "scx_locknest_v9_futexonly": ("scx_locknest_v9", ["-F", "futexonly"]),
    "scx_lockboost2_off": ("scx_lockboost2", ["-F", "off"]),
    "scx_lockboost2_weak": ("scx_lockboost2", ["-F", "weak"]),
    "scx_lockboost2_equal": ("scx_lockboost2", ["-F", "equal"]),
    "scx_lockboost3_off": ("scx_lockboost3", ["-F", "off"]),
    "scx_lockboost3_weak": ("scx_lockboost3", ["-F", "weak"]),
    "scx_lockboost3_equal": ("scx_lockboost3", ["-F", "equal"]),
    "scx_lockboost3_owner": ("scx_lockboost3", ["-F", "owner"]),
    "scx_lockboost3_ownerweak": ("scx_lockboost3", ["-F", "ownerweak"]),
    "scx_lockboost4_off": ("scx_lockboost4", ["-F", "off"]),
    "scx_lockboost4_weak": ("scx_lockboost4", ["-F", "weak"]),
    "scx_lockboost4_equal": ("scx_lockboost4", ["-F", "equal"]),
    "scx_lockboost4_owner": ("scx_lockboost4", ["-F", "owner"]),
    "scx_lockboost4_ownerweak": ("scx_lockboost4", ["-F", "ownerweak"]),
    "scx_lockboost5_off": ("scx_lockboost5", ["-F", "off"]),
    "scx_lockboost5_owner": ("scx_lockboost5", ["-F", "owner"]),
    "scx_lockboost5_ownerweak": ("scx_lockboost5", ["-F", "ownerweak"]),
    "scx_lockboost5_count_only": ("scx_lockboost5", ["-F", "ownerweak", "-a", "4", "-A", "8", "-T", "0"]),
    "scx_lockboost5_time_only": ("scx_lockboost5", ["-F", "ownerweak", "-a", "1024", "-A", "0"]),
    "scx_lockboost6_count_only": ("scx_lockboost6", ["-F", "ownerweak", "-a", "4", "-A", "8", "-T", "0", "-m", "0"]),
    "scx_lockboost6_mutex_time": ("scx_lockboost6", ["-F", "ownerweak", "-a", "4", "-A", "8", "-T", "0", "-m", "5"]),
}


def resolve_lockboost5plus_scheduler(scheduler: str) -> tuple[str, list[str]] | None:
    if not (scheduler.startswith("scx_lockboost5") or scheduler.startswith("scx_lockboost6")):
        return None

    binary = "scx_lockboost6" if scheduler.startswith("scx_lockboost6") else "scx_lockboost5"
    policy = "ownerweak"
    owner_slice = "4"
    owner_inf = "8"
    tail_ms = "0" if binary == "scx_lockboost6" else "5"
    age_cap = "50"
    slice_score = "16"
    inf_score = "64"
    hard_ms = "0"
    mutex_tail_ms = "0"
    mutex_age_cap = "50"
    mutex_slice_score = "16"
    mutex_inf_score = "64"
    mutex_hard_ms = "0"

    for token in scheduler.split("_")[2:]:
        if token in {"off", "weak", "equal", "owner", "ownerweak"}:
            policy = token
        elif token == "countonly" or token == "count_only":
            tail_ms = "0"
        elif token == "timeonly" or token == "time_only":
            owner_slice = "1024"
            owner_inf = "0"
        elif token.startswith("T") and token[1:].isdigit():
            tail_ms = token[1:]
        elif token.startswith("B") and token[1:].isdigit():
            age_cap = token[1:]
        elif token.startswith("s") and token[1:].isdigit():
            slice_score = token[1:]
        elif token.startswith("I") and token[1:].isdigit():
            inf_score = token[1:]
        elif token.startswith("H") and token[1:].isdigit():
            hard_ms = token[1:]
        elif token.startswith("m") and token[1:].isdigit():
            mutex_tail_ms = token[1:]
        elif token.startswith("R") and token[1:].isdigit():
            mutex_age_cap = token[1:]
        elif token.startswith("S") and token[1:].isdigit():
            mutex_slice_score = token[1:]
        elif token.startswith("P") and token[1:].isdigit():
            mutex_inf_score = token[1:]
        elif token.startswith("G") and token[1:].isdigit():
            mutex_hard_ms = token[1:]
        elif token.startswith("a") and token[1:].isdigit():
            owner_slice = token[1:]
        elif token.startswith("i") and token[1:].isdigit():
            owner_inf = token[1:]

    args = [
        "-F", policy,
        "-a", owner_slice,
        "-A", owner_inf,
        "-T", tail_ms,
        "-B", age_cap,
        "-s", slice_score,
        "-I", inf_score,
        "-H", hard_ms,
    ]
    if binary == "scx_lockboost6":
        args.extend([
            "-m", mutex_tail_ms,
            "-R", mutex_age_cap,
            "-S", mutex_slice_score,
            "-P", mutex_inf_score,
            "-G", mutex_hard_ms,
        ])
    return binary, args


def resolve_scx_scheduler(scheduler: str) -> tuple[str, list[str]]:
    if scheduler in SCX_SCHED_ALIASES:
        return SCX_SCHED_ALIASES[scheduler]

    lockboost5plus = resolve_lockboost5plus_scheduler(scheduler)
    if lockboost5plus is not None:
        return lockboost5plus

    match = re.fullmatch(
        r"scx_lockboost4_(off|weak|equal|owner|ownerweak)(?:_a([0-9]+))?(?:_i([0-9]+))?",
        scheduler,
    )
    if match:
        policy, owner_slice, owner_inf = match.groups()
        args = ["-F", policy]
        if owner_slice is not None:
            args.extend(["-a", owner_slice])
        if owner_inf is not None:
            args.extend(["-A", owner_inf])
        return "scx_lockboost4", args

    return scheduler, []
SCENARIOS = {
    "mutex_1lock_cs0": ["--mode", "mutex", "--locks", "1", "--critical-ns", "0", "--outside-ns", "0"],
    "mutex_1lock_cs1us": ["--mode", "mutex", "--locks", "1", "--critical-ns", "1000", "--outside-ns", "0"],
    "mutex_8lock_cs1us": ["--mode", "mutex", "--locks", "8", "--critical-ns", "1000", "--outside-ns", "0"],
}

FLOAT_RE = r"([0-9]+(?:\.[0-9]+)?)"
INT_RE = r"([0-9]+)"
BENCH_PATTERNS: dict[str, re.Pattern[str]] = {
    "mode_reported": re.compile(r"^Mode\s*:\s*(\S+)\s*$"),
    "threads_reported": re.compile(rf"^Threads\s*:\s*{INT_RE}\s*$"),
    "locks_reported": re.compile(rf"^Locks\s*:\s*{INT_RE}\s*$"),
    "duration_target_s": re.compile(rf"^Duration target\s*:\s*{FLOAT_RE}\s+s\s*$"),
    "critical_ns_reported": re.compile(rf"^Critical ns\s*:\s*{INT_RE}\s*$"),
    "outside_ns_reported": re.compile(rf"^Outside ns\s*:\s*{INT_RE}\s*$"),
    "start_delay_ms_reported": re.compile(rf"^Start delay ms\s*:\s*{INT_RE}\s*$"),
    "total_ops": re.compile(rf"^Total operations\s*:\s*{INT_RE}\s*$"),
    "elapsed_s": re.compile(rf"^Elapsed time\s*:\s*{FLOAT_RE}\s+s\s*$"),
    "throughput_ops_s": re.compile(rf"^Throughput\s*:\s*{FLOAT_RE}\s+ops/s\s*$"),
    "ops_min_per_thread": re.compile(rf"^Ops/thread min\s*:\s*{FLOAT_RE}\s*$"),
    "ops_max_per_thread": re.compile(rf"^Ops/thread max\s*:\s*{FLOAT_RE}\s*$"),
    "ops_mean_per_thread": re.compile(rf"^Ops/thread mean\s*:\s*{FLOAT_RE}\s*$"),
    "zero_progress_threads": re.compile(rf"^Zero-progress thr\s*:\s*{INT_RE}\s*$"),
    "jain_fairness": re.compile(rf"^Jain fairness\s*:\s*{FLOAT_RE}\s*$"),
    "latency_avg_us": re.compile(rf"^Latency avg\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p50_us": re.compile(rf"^Latency p50\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p95_us": re.compile(rf"^Latency p95\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p99_us": re.compile(rf"^Latency p99\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_p999_us": re.compile(rf"^Latency p99\.9\s*:\s*{FLOAT_RE}\s+us\s*$"),
    "latency_max_us": re.compile(rf"^Latency max\s*:\s*{FLOAT_RE}\s+us\s*$"),
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


def start_scheduler(scheduler: str, raw_dir: Path) -> tuple[subprocess.Popen[Any] | None, str | None]:
    if scheduler == "default":
        return None, None
    binary_name, extra_args = resolve_scx_scheduler(scheduler)
    binary = SCX_BIN_DIR / binary_name
    if not binary.exists() or not os.access(binary, os.X_OK):
        return None, f"scx binary not executable: {binary}"
    log_path = raw_dir / "scheduler.log"
    f = log_path.open("ab")
    proc = subprocess.Popen(["sudo", "-n", str(binary), *extra_args], stdout=f, stderr=subprocess.STDOUT)
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
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "failed to build futex_mutex_bench")
    cp = run_cmd(["make", "-C", str(OWNER_MONITOR.parent)], timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "failed to build owner-progress monitor")


def raw_dir_for(root: Path, scheduler: str, scenario: str, threads: int, run_id: int) -> Path:
    return root / "mutex_monitor" / "raw" / f"{scheduler}_{scenario}_t{threads}_r{run_id:02d}"


def existing_run_ids(raw_root: Path, scheduler: str, scenario: str, threads: int) -> list[int]:
    ids: list[int] = []
    if not raw_root.exists():
        return ids
    prefix = f"{scheduler}_{scenario}_t{threads}_r"
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


def bench_cmd(args: argparse.Namespace, scenario: str, threads: int) -> list[str]:
    return [
        str(BENCH),
        "--threads",
        str(threads),
        "--duration",
        str(args.duration),
        "--start-delay-ms",
        str(args.start_delay_ms),
        *SCENARIOS[scenario],
    ]


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
            elif key in {
                "duration_target_s",
                "elapsed_s",
                "throughput_ops_s",
                "ops_min_per_thread",
                "ops_max_per_thread",
                "ops_mean_per_thread",
                "jain_fairness",
                "latency_avg_us",
                "latency_p50_us",
                "latency_p95_us",
                "latency_p99_us",
                "latency_p999_us",
                "latency_max_us",
            }:
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


def run_one(args: argparse.Namespace, root: Path, scheduler: str, scenario: str, threads: int, run_id: int) -> None:
    raw = raw_dir_for(root, scheduler, scenario, threads, run_id)
    raw.mkdir(parents=True, exist_ok=True)
    env: dict[str, Any] = {
        "benchmark": "test8 futex/mutex owner-progress monitor",
        "scheduler": scheduler,
        "scenario": scenario,
        "threads": threads,
        "run_id": run_id,
        "duration_s": args.duration,
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
            scx_proc, err = start_scheduler(scheduler, raw)
            if err:
                env["failure_type"] = "scheduler_startup_failure"
                env["failure_detail"] = err
                return
        env["sched_ext_after_start_state"] = scx_state()
        cmd = bench_cmd(args, scenario, threads)
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


def summarize(root: Path, args: argparse.Namespace) -> None:
    raw_root = root / "mutex_monitor" / "raw"
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
                "instrumented": 1,
                "scheduler": env.get("scheduler", ""),
                "scenario": env.get("scenario", ""),
                "threads": env.get("threads", ""),
                "run_id": env.get("run_id", ""),
                "valid": int(valid),
                "failure_type": failure,
                "failure_detail": env.get("failure_detail", ""),
                "duration_s": env.get("duration_s", ""),
                "start_delay_ms": env.get("start_delay_ms", ""),
                "locks": bench.get("locks_reported", ""),
                "critical_ns": bench.get("critical_ns_reported", ""),
                "throughput_ops_s": bench.get("throughput_ops_s", ""),
                "total_ops": bench.get("total_ops", ""),
                "latency_p95_us": bench.get("latency_p95_us", ""),
                "latency_p99_us": bench.get("latency_p99_us", ""),
                "latency_p999_us": bench.get("latency_p999_us", ""),
                "zero_progress_threads": bench.get("zero_progress_threads", ""),
                "jain_fairness": bench.get("jain_fairness", ""),
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
                "owner_next_run_p95_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p95_us")),
                "owner_next_run_p99_us": csv_value(owner, ("histograms", "owner_next_run_delay_us", "p99_us")),
                "owner_unlock_p95_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p95_us")),
                "owner_unlock_p99_us": csv_value(owner, ("histograms", "owner_contention_to_unlock_delay_us", "p99_us")),
                "owner_offcpu_p95_us": csv_value(owner, ("histograms", "owner_offcpu_delay_us", "p95_us")),
                "owner_offcpu_p99_us": csv_value(owner, ("histograms", "owner_offcpu_delay_us", "p99_us")),
                "owner_runnable_offcpu_p95_us": csv_value(owner, ("histograms", "owner_runnable_offcpu_delay_us", "p95_us")),
                "owner_runnable_offcpu_p99_us": csv_value(owner, ("histograms", "owner_runnable_offcpu_delay_us", "p99_us")),
                "waiter_wait_p95_us": csv_value(owner, ("histograms", "waiter_lock_wait_us", "p95_us")),
                "waiter_wait_p99_us": csv_value(owner, ("histograms", "waiter_lock_wait_us", "p99_us")),
                "waiter_fanin_p50": csv_value(owner, ("histograms", "waiter_fanin", "p50_us")),
                "waiter_fanin_p95": csv_value(owner, ("histograms", "waiter_fanin", "p95_us")),
                "waiter_fanin_p99": csv_value(owner, ("histograms", "waiter_fanin", "p99_us")),
                "start_timestamp": env.get("start_timestamp", ""),
                "end_timestamp": env.get("end_timestamp", ""),
                "raw_dir": str(raw),
            })
    write_csv(root / "summary_mutex_monitor_runs.csv", rows)

    metric_fields = [
        "throughput_ops_s",
        "latency_p999_us",
        "contention_events",
        "owner_next_run_p95_us",
        "owner_unlock_p95_us",
        "owner_offcpu_p95_us",
        "owner_runnable_offcpu_p95_us",
        "owner_preempted_events_per_contention",
        "owner_not_running_ratio",
        "waiter_fanin_p95",
    ]
    cases: list[dict[str, Any]] = []
    for scheduler in args.schedulers:
        for scenario in args.scenarios:
            for threads in args.threads:
                vals = [
                    r for r in rows
                    if r["scheduler"] == scheduler
                    and r["scenario"] == scenario
                    and int(r["threads"] or 0) == threads
                    and r["valid"] == 1
                ]
                case: dict[str, Any] = {
                    "instrumented": 1,
                    "scheduler": scheduler,
                    "scenario": scenario,
                    "threads": threads,
                    "n": len(vals),
                    "failures": sum(
                        1 for r in rows
                        if r["scheduler"] == scheduler
                        and r["scenario"] == scenario
                        and int(r["threads"] or 0) == threads
                        and r["valid"] != 1
                    ),
                }
                for field in metric_fields:
                    case[f"{field}_mean"] = mean_field(vals, field)
                    case[f"{field}_stdev"] = stdev_field(vals, field)
                cases.append(case)
    write_csv(root / "summary_mutex_monitor_cases.csv", cases)


def write_config(root: Path, args: argparse.Namespace) -> None:
    write_json(root / "mutex_monitor_config.json", {
        "created_at": now_iso(),
        "benchmark": "test8 futex/mutex owner-progress monitor",
        "instrumented": True,
        "warning": (
            "Throughput/latency are measured while owner-progress uprobes and "
            "sched_switch tracing are active. Use these results for mechanism "
            "diagnostics, not raw performance comparison with run_full_sweep.sh."
        ),
        "schedulers": args.schedulers,
        "scenarios": args.scenarios,
        "threads": args.threads,
        "runs": args.runs,
        "duration_s": args.duration,
        "start_delay_ms": args.start_delay_ms,
        "monitor": str(OWNER_MONITOR),
        "scheduler_bin_dir": str(SCX_BIN_DIR),
    })


def run_matrix(args: argparse.Namespace, root: Path) -> None:
    raw_root = root / "mutex_monitor" / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scenario in args.scenarios:
            for scheduler in args.schedulers:
                while True:
                    valid: list[int] = []
                    for run_id in existing_run_ids(raw_root, scheduler, scenario, threads):
                        raw = raw_dir_for(root, scheduler, scenario, threads, run_id)
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
                        log(f"Complete {scheduler} {scenario} t{threads}: valid={len(valid)}")
                        break
                    run_ids = existing_run_ids(raw_root, scheduler, scenario, threads)
                    next_id = (max(run_ids) + 1) if run_ids else 1
                    if next_id > args.max_attempts_per_case:
                        log(f"ERROR too many attempts for {scheduler} {scenario} t{threads}; valid={len(valid)}")
                        break
                    log(f"Run {scheduler} {scenario} t{threads} r{next_id} (valid {len(valid)}/{args.runs})")
                    run_one(args, root, scheduler, scenario, threads, next_id)
                    summarize(root, args)
                    env = load_json(raw_dir_for(root, scheduler, scenario, threads, next_id) / "env.json")
                    if env.get("failure_type") == "scheduler_startup_failure":
                        log(f"Skip remaining runs for {scheduler} {scenario} t{threads} after scheduler_startup_failure")
                        break


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Run test8 mutex scenarios with owner-progress monitor.")
    ap.add_argument("--root", default="", help="Result root. Default: test8 result/mutex_monitor_<timestamp>")
    ap.add_argument("--phase", choices=["run", "summarize", "smoke"], default="run")
    ap.add_argument("--schedulers", type=parse_list_str, default=DEFAULT_SCHEDS)
    ap.add_argument("--scenarios", type=parse_list_str, default=list(SCENARIOS))
    ap.add_argument("--threads", type=parse_list_int, default=[64, 128, 256])
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--duration", type=float, default=10.0)
    ap.add_argument("--start-delay-ms", type=int, default=1000)
    ap.add_argument("--min-wait-us", type=int, default=1)
    ap.add_argument("--pthread-owner-offset", type=int, default=8)
    ap.add_argument("--monitor-library", default="")
    ap.add_argument("--monitor-start-delay", type=float, default=0.5)
    ap.add_argument("--min-contention-events", type=int, default=1)
    ap.add_argument("--bench-timeout-slack", type=int, default=60)
    ap.add_argument("--max-attempts-per-case", type=int, default=6)
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    unknown = [s for s in args.scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenarios: {' '.join(unknown)}")
    if args.phase == "smoke":
        args.schedulers = ["default", "scx_simple", "scx_lockboost"]
        args.scenarios = ["mutex_1lock_cs0", "mutex_1lock_cs1us", "mutex_8lock_cs1us"]
        args.threads = [64]
        args.runs = 1
        args.duration = min(args.duration, 3.0)

    root = Path(args.root).resolve() if args.root else TEST_DIR / "result" / f"mutex_monitor_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
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
    log("NOTE: owner-progress monitor is enabled; throughput is instrumented, not raw performance")
    if args.phase in ("run", "smoke"):
        run_matrix(args, root)
        summarize(root, args)
    elif args.phase == "summarize":
        summarize(root, args)

    state, rollback = cleanup_scx()
    log(f"final sched_ext state: {state} rollback_failure={int(rollback)}")
    log(f"summary runs: {root / 'summary_mutex_monitor_runs.csv'}")
    log(f"summary cases: {root / 'summary_mutex_monitor_cases.csv'}")
    log(f"done root: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

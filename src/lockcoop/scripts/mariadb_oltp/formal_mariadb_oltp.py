#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
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


TEST_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = TEST_DIR.parent
SCX_BIN_DIR = REPO_ROOT / "lock-sched" / "tools" / "sched_ext" / "build" / "bin"

DEFAULT_SCHEDS = [
    "default",
    "scx_simple",
    "scx_lcas",
    "scx_cfl",
    "scx_nest",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_locknest_v2",
    "scx_locknest_v3",
    "scx_locknest_v4",
    "scx_locknest_v5",
    "scx_locknest_v6",
    "scx_locknest_v7",
    "scx_lockcoop",
    "scx_lockboost",
    "scx_lockboost2",
    "scx_lockboost3",
    "scx_lockboost4",
    "scx_lockboost5",
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
}


def resolve_lockboost5_scheduler(scheduler: str) -> tuple[str, list[str]] | None:
    if not scheduler.startswith("scx_lockboost5"):
        return None

    policy = "ownerweak"
    owner_slice = "4"
    owner_inf = "8"
    tail_ms = "5"
    age_cap = "50"
    slice_score = "16"
    inf_score = "64"
    hard_ms = "0"

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
        elif token.startswith("a") and token[1:].isdigit():
            owner_slice = token[1:]
        elif token.startswith("i") and token[1:].isdigit():
            owner_inf = token[1:]

    return "scx_lockboost5", [
        "-F", policy,
        "-a", owner_slice,
        "-A", owner_inf,
        "-T", tail_ms,
        "-B", age_cap,
        "-s", slice_score,
        "-I", inf_score,
        "-H", hard_ms,
    ]


def resolve_scx_scheduler(scheduler: str) -> tuple[str, list[str]]:
    if scheduler in SCX_SCHED_ALIASES:
        return SCX_SCHED_ALIASES[scheduler]

    lockboost5 = resolve_lockboost5_scheduler(scheduler)
    if lockboost5 is not None:
        return lockboost5

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
DEFAULT_THREADS = [64, 128, 192, 256]
DEFAULT_PERF_EVENTS = [
    "task-clock",
    "context-switches",
    "cpu-migrations",
    "cycles",
    "instructions",
    "cache-references",
    "cache-misses",
]


TPS_RE = re.compile(r"^\s*transactions:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
QPS_RE = re.compile(r"^\s*queries:\s*[0-9]+\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
IGNORED_RE = re.compile(r"^\s*ignored errors:\s*(?P<n>[0-9]+)\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
RECONNECTS_RE = re.compile(r"^\s*reconnects:\s*(?P<n>[0-9]+)\s*\((?P<v>[0-9]+(?:\.[0-9]+)?) per sec\.\)\s*$")
LAT_KV_RE = re.compile(r"^\s*(?P<k>min|avg|max|95th percentile|99th percentile):\s*(?P<v>[0-9]+(?:\.[0-9]+)?)\s*$")
HIST_RE = re.compile(r"^\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s+\|.*\s+(?P<count>[0-9]+)\s*$")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_cmd(
    cmd: list[str],
    *,
    timeout: int | None = None,
    check: bool = False,
    text: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=text, timeout=timeout, check=check)


def sudo_cmd(args: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return run_cmd(["sudo", "-n", *args], timeout=timeout)


def scx_state() -> str:
    p = Path("/sys/kernel/sched_ext/state")
    if not p.exists():
        return "unknown"
    try:
        return p.read_text().strip()
    except OSError:
        return "unknown"


def scx_nr_rejected() -> int | None:
    path = Path("/sys/kernel/sched_ext/nr_rejected")
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    for sched in DEFAULT_SCHEDS:
        if sched == "default":
            continue
        sudo_cmd(["pkill", "-TERM", "-f", str(SCX_BIN_DIR / sched)], timeout=5)
    for binary_name, _ in SCX_SCHED_ALIASES.values():
        sudo_cmd(["pkill", "-TERM", "-f", str(SCX_BIN_DIR / binary_name)], timeout=5)

    state = wait_scx_disabled(5)
    if state != "disabled":
        for sched in DEFAULT_SCHEDS:
            if sched == "default":
                continue
            sudo_cmd(["pkill", "-KILL", "-f", str(SCX_BIN_DIR / sched)], timeout=5)
        for binary_name, _ in SCX_SCHED_ALIASES.values():
            sudo_cmd(["pkill", "-KILL", "-f", str(SCX_BIN_DIR / binary_name)], timeout=5)
        state = wait_scx_disabled(5)
    return state, state != "disabled"


def start_scheduler(scheduler: str, raw_dir: Path, scx_args: list[str]) -> tuple[subprocess.Popen[Any] | None, str | None]:
    if scheduler == "default":
        return None, None

    binary_name, alias_args = resolve_scx_scheduler(scheduler)
    binary = SCX_BIN_DIR / binary_name
    if not binary.exists() or not os.access(binary, os.X_OK):
        return None, f"scx binary not executable: {binary}"

    scheduler_log = raw_dir / "scheduler.log"
    f = scheduler_log.open("ab")
    proc = subprocess.Popen(
        ["sudo", "-n", str(binary), *alias_args, *scx_args],
        stdout=f,
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.0)
    if proc.poll() is not None:
        f.close()
        return None, f"scheduler exited early with returncode {proc.returncode}"

    deadline = time.time() + 5
    state = scx_state()
    while time.time() < deadline:
        state = scx_state()
        if proc.poll() is not None:
            f.close()
            return None, f"scheduler exited during attach with returncode {proc.returncode}; last_state={state}"
        if state == "enabled":
            return proc, None
        time.sleep(0.1)

    try:
        proc.terminate()
    except OSError:
        pass
    f.close()
    return None, f"sched_ext state stayed disabled after start; last_state={state}"


def mysql_query(sql: str) -> str:
    cp = sudo_cmd(["mysql", "-N", "-B", "-e", sql], timeout=30)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "mysql query failed")
    return cp.stdout


def mysql_dict(sql: str) -> dict[str, str]:
    out: dict[str, str] = {}
    text = mysql_query(sql)
    for line in text.splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2:
            out[parts[0]] = parts[1]
    return out


def mariadb_env() -> dict[str, Any]:
    variables = mysql_dict(
        "SHOW VARIABLES WHERE Variable_name IN "
        "('version','innodb_buffer_pool_size','innodb_buffer_pool_instances',"
        "'innodb_flush_log_at_trx_commit','sync_binlog','log_bin','binlog_format',"
        "'max_connections','max_prepared_stmt_count');"
    )
    return {
        "mariadb_version": variables.get("version", ""),
        "innodb_buffer_pool_size": int(variables.get("innodb_buffer_pool_size", "0") or "0"),
        "variables": variables,
    }


def restart_mariadb() -> None:
    cp = sudo_cmd(["systemctl", "restart", "mariadb"], timeout=120)
    if cp.returncode != 0:
        raise RuntimeError(cp.stderr.strip() or cp.stdout.strip() or "systemctl restart mariadb failed")
    deadline = time.time() + 60
    last = ""
    while time.time() < deadline:
        cp = sudo_cmd(["mysql", "-N", "-B", "-e", "SELECT 1;"], timeout=10)
        if cp.returncode == 0:
            time.sleep(5)
            return
        last = cp.stderr.strip() or cp.stdout.strip()
        time.sleep(1)
    raise RuntimeError(f"mariadb not ready after restart: {last}")


def mariadb_pid() -> str:
    for cmd in (["pidof", "mariadbd"], ["pgrep", "-xo", "mariadbd"]):
        cp = run_cmd(cmd, timeout=10)
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.split()[0]
    return ""


def sysbench_args(args: argparse.Namespace, threads: int, duration: int, *, histogram: bool) -> list[str]:
    out = [
        "sysbench",
        "oltp_read_write",
        f"--mysql-db={args.db_name}",
        f"--mysql-user={args.db_user}",
        f"--mysql-password={args.db_pass}",
        f"--tables={args.tables}",
        f"--table-size={args.table_size}",
        f"--threads={threads}",
        f"--time={duration}",
        f"--report-interval={args.report_interval}",
        "--percentile=95",
    ]
    if histogram:
        out.append("--histogram=on")
    out.append("run")
    return out


def run_sysbench(cmd: list[str], log_path: Path, *, timeout_s: int | None = None) -> tuple[int, bool]:
    with log_path.open("wb") as f:
        f.write(("# start: " + now_iso() + "\n").encode())
        f.write(("# cmd: " + " ".join(cmd) + "\n\n").encode())
        f.flush()
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, start_new_session=True)
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            f.write(("\n# timeout: exceeded " + str(timeout_s) + " seconds\n").encode())
            f.flush()
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                try:
                    rc = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    rc = 124
            if rc == 0:
                rc = 124
        f.write(("\n# end: " + now_iso() + "\n").encode())
        f.write(("# returncode: " + str(rc) + "\n").encode())
        f.write(("# timed_out: " + str(int(timed_out)) + "\n").encode())
        return rc, timed_out


def parse_sysbench_log(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return {}

    out: dict[str, Any] = {}
    hist: list[tuple[float, int]] = []
    in_hist = False
    for line in lines:
        if line.startswith("Latency histogram"):
            in_hist = True
            continue
        if in_hist and line.startswith("SQL statistics:"):
            in_hist = False
        if in_hist:
            m = HIST_RE.match(line)
            if m:
                hist.append((float(m.group("value")), int(m.group("count"))))
            continue

        m = TPS_RE.match(line)
        if m:
            out["tps"] = float(m.group("v"))
            continue
        m = QPS_RE.match(line)
        if m:
            out["qps"] = float(m.group("v"))
            continue
        m = IGNORED_RE.match(line)
        if m:
            out["ignored_errors"] = int(m.group("n"))
            out["ignored_errors_per_s"] = float(m.group("v"))
            continue
        m = RECONNECTS_RE.match(line)
        if m:
            out["reconnects"] = int(m.group("n"))
            out["reconnects_per_s"] = float(m.group("v"))
            continue
        m = LAT_KV_RE.match(line)
        if m:
            key = m.group("k")
            val = float(m.group("v"))
            if key == "avg":
                out["lat_avg_ms"] = val
            elif key == "max":
                out["lat_max_ms"] = val
            elif key == "95th percentile":
                out["lat_p95_ms"] = val
            elif key == "99th percentile":
                out["lat_p99_ms"] = val
            elif key == "min":
                out["lat_min_ms"] = val

    if hist:
        out["lat_p95_hist_ms"] = hist_percentile(hist, 0.95)
        out["lat_p99_hist_ms"] = hist_percentile(hist, 0.99)
        if "lat_p99_ms" not in out:
            out["lat_p99_ms"] = out["lat_p99_hist_ms"]
        if "lat_p95_ms" not in out:
            out["lat_p95_ms"] = out["lat_p95_hist_ms"]
    return out


def hist_percentile(hist: list[tuple[float, int]], q: float) -> float | None:
    total = sum(c for _v, c in hist)
    if total <= 0:
        return None
    target = math.ceil(total * q)
    acc = 0
    for value, count in hist:
        acc += count
        if acc >= target:
            return value
    return hist[-1][0]


def parse_perf_csv(path: Path, duration_s: int) -> dict[str, Any]:
    vals: dict[str, Any] = {}
    if not path.exists():
        return vals
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("# scope:"):
            vals["perf_scope"] = line.split(":", 1)[1].strip()
            continue
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            value = float(parts[0])
        except ValueError:
            continue
        unit = parts[1]
        event = parts[2]
        if event == "task-clock":
            vals["task_clock_ms"] = value if unit == "msec" else value * 1000.0
        else:
            vals[event] = value

    task_clock = vals.get("task_clock_ms")
    if task_clock is not None and duration_s > 0:
        vals["cpus_utilized"] = float(task_clock) / (duration_s * 1000.0)
    if vals.get("context-switches") is not None:
        vals["cs_per_s"] = float(vals["context-switches"]) / duration_s
    if vals.get("cpu-migrations") is not None:
        vals["mig_per_s"] = float(vals["cpu-migrations"]) / duration_s
    cycles = vals.get("cycles")
    instructions = vals.get("instructions")
    if cycles and instructions and float(cycles) > 0:
        vals["ipc"] = float(instructions) / float(cycles)
    refs = vals.get("cache-references")
    misses = vals.get("cache-misses")
    if refs and misses and float(refs) > 0:
        vals["cache_miss_rate"] = float(misses) / float(refs)
    return vals


def parse_proc_stat(path: Path) -> tuple[int, int, int] | None:
    if not path.exists():
        return None
    total = idle = None
    cpu_count = 0
    for line in path.read_text(errors="replace").splitlines():
        if line.startswith("cpu "):
            nums = [int(x) for x in line.split()[1:] if x.isdigit()]
            if len(nums) >= 4:
                total = sum(nums)
                idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        elif re.match(r"^cpu[0-9]+\s", line):
            cpu_count += 1
    if total is None or idle is None or cpu_count == 0:
        return None
    return total, idle, cpu_count


def proc_busy(before: Path, after: Path) -> tuple[float | None, float | None]:
    b = parse_proc_stat(before)
    a = parse_proc_stat(after)
    if not b or not a:
        return None, None
    dt = a[0] - b[0]
    di = a[1] - b[1]
    if dt <= 0:
        return None, None
    busy = max(0.0, min(1.0, (dt - di) / dt))
    return busy, busy * a[2]


def parse_numastat(path: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"^Total\s+([0-9.]+)\s+([0-9.]+)\s+([0-9.]+)\s*$", line.strip())
        if not m:
            continue
        n0 = float(m.group(1))
        n1 = float(m.group(2))
        total = float(m.group(3))
        out["numastat_node0_mb"] = n0
        out["numastat_node1_mb"] = n1
        out["numastat_total_mb"] = total
        if total:
            out["numastat_node0_ratio"] = n0 / total
            out["numastat_node1_ratio"] = n1 / total
        break
    return out


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


def load_env(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def scheduler_log_failure(raw: Path) -> str:
    path = raw / "scheduler.log"
    if not path.exists():
        return ""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return ""
    if "EXIT: scx_bpf_error" in text or "scx_bpf_error" in text:
        m = re.search(r"EXIT:\s*(.+)", text)
        return m.group(1).strip() if m else "scx_bpf_error in scheduler.log"
    return ""


def valid_performance_runs(raw_root: Path, scheduler: str, threads: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_id in existing_run_ids(raw_root, scheduler, threads):
        raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
        env = load_env(raw / "env.json")
        metrics = parse_sysbench_log(raw / "sysbench.log")
        if (
            env.get("sysbench_returncode") == 0
            and not env.get("failure_type")
            and not scheduler_log_failure(raw)
            and metrics.get("tps") is not None
        ):
            out.append({"run_id": run_id, **metrics})
    return out


def valid_metric_runs(raw_root: Path, scheduler: str, threads: int, duration: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_id in existing_run_ids(raw_root, scheduler, threads):
        raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
        env = load_env(raw / "env.json")
        metrics = parse_sysbench_log(raw / "sysbench.log")
        perf = parse_perf_csv(raw / "perf.log", duration)
        if (
            env.get("sysbench_returncode") == 0
            and not env.get("failure_type")
            and not scheduler_log_failure(raw)
            and metrics.get("tps") is not None
            and perf.get("task_clock_ms") is not None
        ):
            out.append({"run_id": run_id, **metrics, **perf})
    return out


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def sysbench_timeout(duration: int, args: argparse.Namespace) -> int:
    return max(duration + args.sysbench_timeout_slack, int(duration * args.sysbench_timeout_factor))


def failure_count(raw_root: Path, scheduler: str, threads: int, failure_type: str) -> int:
    n = 0
    for run_id in existing_run_ids(raw_root, scheduler, threads):
        raw = raw_root / f"{scheduler}_t{threads}_r{run_id:02d}"
        env = load_env(raw / "env.json")
        sched_failure = scheduler_log_failure(raw)
        actual_failure = env.get("failure_type") or ("scheduler_runtime_failure" if sched_failure else "")
        if actual_failure == failure_type:
            n += 1
    return n


def global_failure_count(raw_root: Path, scheduler: str, failure_types: set[str]) -> int:
    if not raw_root.exists():
        return 0
    n = 0
    prefix = f"{scheduler}_t"
    for raw in raw_root.iterdir():
        if not raw.is_dir() or not raw.name.startswith(prefix):
            continue
        env = load_env(raw / "env.json")
        sched_failure = scheduler_log_failure(raw)
        actual_failure = env.get("failure_type") or ("scheduler_runtime_failure" if sched_failure else "")
        if actual_failure in failure_types:
            n += 1
    return n


def global_failure_count_all_phases(root: Path, scheduler: str, failure_types: set[str]) -> int:
    return sum(
        global_failure_count(root / phase / "raw", scheduler, failure_types)
        for phase in ("performance", "mechanism")
    )


def ensure_config(root: Path, args: argparse.Namespace) -> None:
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.json"
    config = {
        "created_at": now_iso(),
        "workload": "MariaDB/Sysbench OLTP read-write",
        "tables": args.tables,
        "table_size": args.table_size,
        "threads": args.threads,
        "run_time": args.run_time,
        "warmup_time": args.warmup_time,
        "schedulers": args.schedulers,
        "scheduler_order_fixed": True,
        "cpu_pinning": "disabled",
        "mariadb_restart_per_run": True,
        "performance_min_runs": args.performance_min_runs,
        "performance_cv_threshold": args.cv_threshold,
        "performance_max_valid_runs": args.performance_max_valid_runs,
        "mechanism_runs": args.mechanism_runs,
        "perf_events": args.perf_events,
        "sysbench_timeout_slack": args.sysbench_timeout_slack,
        "sysbench_timeout_factor": args.sysbench_timeout_factor,
        "max_timeouts_per_case": args.max_timeouts_per_case,
        "max_runtime_failures_per_case": args.max_runtime_failures_per_case,
        "max_global_runtime_failures_per_scheduler": args.max_global_runtime_failures_per_scheduler,
    }
    if config_path.exists():
        old = load_env(config_path)
        old.update({k: v for k, v in config.items() if k != "created_at"})
        config = old
    write_json(config_path, config)
    readme = root / "README.md"
    if not readme.exists():
        readme.write_text(
            "# formal_mariadb_oltp\n\n"
            "Formal MariaDB/Sysbench OLTP data for paper Section 6.3.\n\n"
            "- Phase 1 performance data: `performance/`\n"
            "- Phase 2 mechanism data: `mechanism/`\n"
            "- Root-level `summary_cases.csv` and `summary_metrics_cases.csv` are copied from the phase summaries.\n"
            "- CPU pinning is disabled.\n"
            "- MariaDB is restarted before every run.\n"
            "- sched_ext is cleaned before and after every run.\n",
        )


def raw_dir_for(phase_dir: Path, scheduler: str, threads: int, run_id: int) -> Path:
    return phase_dir / "raw" / f"{scheduler}_t{threads}_r{run_id:02d}"


def start_perf(raw_dir: Path, duration: int, pid: str, events: list[str]) -> subprocess.Popen[Any] | None:
    if not pid:
        return None
    numastat = shutil.which("numastat")
    if numastat:
        with (raw_dir / "numastat.before.log").open("wb") as f:
            subprocess.run(["sudo", "-n", numastat, "-p", pid], stdout=f, stderr=subprocess.STDOUT)
    try:
        with (raw_dir / "numa_maps.before.log").open("wb") as f:
            subprocess.run(["sudo", "-n", "cat", f"/proc/{pid}/numa_maps"], stdout=f, stderr=subprocess.STDOUT)
    except Exception:
        pass
    shutil.copyfile("/proc/stat", raw_dir / "proc_stat.before.log")

    perf_log = raw_dir / "perf.log"
    perf_err = raw_dir / "perf.err"
    with perf_log.open("wb") as f:
        f.write(f"# date: {now_iso()}\n".encode())
        f.write(b"# scope: pid\n")
        f.write(f"# pid: {pid}\n".encode())
        f.write(f"# duration_s: {duration}\n".encode())
        f.write(("# events: " + ",".join(events) + "\n\n").encode())
    out = perf_log.open("ab")
    err = perf_err.open("wb")
    return subprocess.Popen(
        ["sudo", "-n", "perf", "stat", "-p", pid, "-e", ",".join(events), "-x,", "-o", str(perf_log), "--append", "--", "sleep", str(duration)],
        stdout=out,
        stderr=err,
    )


def stop_perf(raw_dir: Path, proc: subprocess.Popen[Any] | None, pid: str) -> int | None:
    rc = None
    if proc is not None:
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                rc = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
    numastat = shutil.which("numastat")
    if numastat and pid:
        with (raw_dir / "numastat.after.log").open("wb") as f:
            subprocess.run(["sudo", "-n", numastat, "-p", pid], stdout=f, stderr=subprocess.STDOUT)
    if pid:
        with (raw_dir / "numa_maps.after.log").open("wb") as f:
            subprocess.run(["sudo", "-n", "cat", f"/proc/{pid}/numa_maps"], stdout=f, stderr=subprocess.STDOUT)
    shutil.copyfile("/proc/stat", raw_dir / "proc_stat.after.log")
    return rc


def run_one(args: argparse.Namespace, phase: str, root: Path, scheduler: str, threads: int, run_id: int) -> None:
    phase_dir = root / phase
    raw = raw_dir_for(phase_dir, scheduler, threads, run_id)
    raw.mkdir(parents=True, exist_ok=True)
    env: dict[str, Any] = {
        "phase": phase,
        "scheduler": scheduler,
        "threads": threads,
        "run_id": run_id,
        "start_timestamp": now_iso(),
        "kernel_version": run_cmd(["uname", "-r"]).stdout.strip(),
        "sysbench_returncode": None,
        "failure_type": "",
        "sched_ext_nr_rejected_initial": scx_nr_rejected(),
    }
    write_json(raw / "env.json", env)

    scx_proc: subprocess.Popen[Any] | None = None
    perf_proc: subprocess.Popen[Any] | None = None
    try:
        initial_state, rollback = cleanup_scx()
        env["sched_ext_initial_state"] = initial_state
        if rollback:
            env["pre_run_rollback_failure"] = True
        if initial_state != "disabled" or rollback:
            env["failure_type"] = "pre_run_rollback_failure"
            env["failure_detail"] = f"sched_ext not clean before run: state={initial_state} rollback_failure={int(rollback)}"
            return

        if scheduler != "default":
            binary_name, _ = resolve_scx_scheduler(scheduler)
            binary = SCX_BIN_DIR / binary_name
            env["scheduler_binary"] = str(binary)
            env["scheduler_sha256"] = sha256_file(binary)
            scx_proc, err = start_scheduler(scheduler, raw, args.scx_args.get(scheduler, []))
            if err:
                env["failure_type"] = "scheduler_startup_failure"
                env["failure_detail"] = err
                log(f"ERROR {scheduler} t{threads} r{run_id}: {err}")
                return
        env["sched_ext_after_start_state"] = scx_state()
        env["sched_ext_nr_rejected_after_start"] = scx_nr_rejected()

        try:
            restart_mariadb()
        except Exception as exc:
            env["failure_type"] = "env_failure"
            env["failure_detail"] = str(exc)
            write_json(raw / "env.json", env)
            raise

        db = mariadb_env()
        env.update(db)
        if env.get("innodb_buffer_pool_size") != 134217728:
            env["failure_type"] = "env_failure"
            env["failure_detail"] = f"innodb_buffer_pool_size={env.get('innodb_buffer_pool_size')} is not 128MB"
            write_json(raw / "env.json", env)
            raise RuntimeError(env["failure_detail"])

        if args.warmup_time > 0:
            log(f"Warmup {phase} {scheduler} t{threads} r{run_id}: {args.warmup_time}s")
            warmup_rc, warmup_timeout = run_sysbench(
                sysbench_args(args, threads, args.warmup_time, histogram=False),
                raw / "sysbench.warmup.log",
                timeout_s=sysbench_timeout(args.warmup_time, args),
            )
            env["warmup_returncode"] = warmup_rc
            env["warmup_timed_out"] = warmup_timeout
            if warmup_timeout:
                env["failure_type"] = "warmup_timeout"
                return
            if warmup_rc != 0:
                env["failure_type"] = "warmup_failure"
                return

        pid = ""
        if phase == "mechanism":
            pid = mariadb_pid()
            env["mariadbd_pid"] = pid
            if not pid:
                env["failure_type"] = "env_failure"
                env["failure_detail"] = "cannot find mariadbd pid for perf"
                return
            perf_proc = start_perf(raw, args.run_time, pid, args.perf_events)

        log(f"Measure {phase} {scheduler} t{threads} r{run_id}: {args.run_time}s")
        rc, timed_out = run_sysbench(
            sysbench_args(args, threads, args.run_time, histogram=True),
            raw / "sysbench.log",
            timeout_s=sysbench_timeout(args.run_time, args),
        )
        env["sysbench_returncode"] = rc
        env["sysbench_timed_out"] = timed_out
        if timed_out and not env.get("failure_type"):
            env["failure_type"] = "sysbench_timeout"
        elif rc != 0 and not env.get("failure_type"):
            env["failure_type"] = "sysbench_failure"

        if phase == "mechanism":
            env["perf_returncode"] = stop_perf(raw, perf_proc, pid)
            perf_proc = None
            if env.get("perf_returncode") not in (0, None) and not env.get("failure_type"):
                env["failure_type"] = "perf_failure"

        if scheduler != "default":
            env["sched_ext_after_measure_state"] = scx_state()
            env["scheduler_loader_returncode_after_measure"] = scx_proc.poll() if scx_proc is not None else None
            if env["sched_ext_after_measure_state"] != "enabled" or env["scheduler_loader_returncode_after_measure"] is not None:
                if not env.get("failure_type"):
                    env["failure_type"] = "scheduler_runtime_failure"
                    env["failure_detail"] = (
                        f"scheduler did not remain enabled: state={env['sched_ext_after_measure_state']} "
                        f"loader_returncode={env['scheduler_loader_returncode_after_measure']}"
                    )

    finally:
        final_state, rollback = cleanup_scx(scx_proc)
        env["sched_ext_final_state"] = final_state
        env["sched_ext_nr_rejected_final"] = scx_nr_rejected()
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


def summarize(root: Path, args: argparse.Namespace, *, phase: str | None = None) -> None:
    if phase in (None, "performance"):
        summarize_performance(root, args)
    if phase in (None, "mechanism"):
        summarize_metrics(root, args)


def summarize_performance(root: Path, args: argparse.Namespace) -> None:
    phase_dir = root / "performance"
    raw_root = phase_dir / "raw"
    rows: list[dict[str, Any]] = []
    failures_by_case: dict[tuple[str, int], int] = {}
    if raw_root.exists():
        for raw in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            env = load_env(raw / "env.json")
            metrics = parse_sysbench_log(raw / "sysbench.log")
            scheduler = env.get("scheduler", "")
            threads = int(env.get("threads", 0) or 0)
            sched_failure = scheduler_log_failure(raw)
            failure = env.get("failure_type", "")
            if sched_failure and not failure:
                failure = "scheduler_runtime_failure"
            valid = env.get("sysbench_returncode") == 0 and not failure and metrics.get("tps") is not None
            if not valid:
                failures_by_case[(scheduler, threads)] = failures_by_case.get((scheduler, threads), 0) + 1
            rows.append({
                "scheduler": scheduler,
                "threads": threads,
                "run_id": env.get("run_id", ""),
                "valid": int(valid),
                "failure_type": failure,
                "failure_detail": env.get("failure_detail", "") or sched_failure,
                "tps": metrics.get("tps", ""),
                "qps": metrics.get("qps", ""),
                "lat_avg": metrics.get("lat_avg_ms", ""),
                "lat_p95": metrics.get("lat_p95_ms", ""),
                "lat_p99": metrics.get("lat_p99_ms", ""),
                "lat_max": metrics.get("lat_max_ms", ""),
                "ignored_errors": metrics.get("ignored_errors", ""),
                "sysbench_returncode": env.get("sysbench_returncode", ""),
                "start_timestamp": env.get("start_timestamp", ""),
                "end_timestamp": env.get("end_timestamp", ""),
                "kernel_version": env.get("kernel_version", ""),
                "mariadb_version": env.get("mariadb_version", ""),
                "innodb_buffer_pool_size": env.get("innodb_buffer_pool_size", ""),
                "sched_ext_initial_state": env.get("sched_ext_initial_state", ""),
                "sched_ext_after_start_state": env.get("sched_ext_after_start_state", ""),
                "sched_ext_final_state": env.get("sched_ext_final_state", ""),
                "raw_dir": str(raw),
            })
    write_csv(phase_dir / "summary_runs.csv", rows)
    shutil.copyfile(phase_dir / "summary_runs.csv", root / "summary_runs.csv")

    cases: list[dict[str, Any]] = []
    for scheduler in args.schedulers:
        for threads in args.threads:
            vals = [r for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] == 1]
            tps = [float(r["tps"]) for r in vals]
            p95 = [float(r["lat_p95"]) for r in vals if r["lat_p95"] != ""]
            p99 = [float(r["lat_p99"]) for r in vals if r["lat_p99"] != ""]
            qps = [float(r["qps"]) for r in vals if r["qps"] != ""]
            avg = [float(r["lat_avg"]) for r in vals if r["lat_avg"] != ""]
            sd = statistics.pstdev(tps) if len(tps) >= 2 else 0.0
            mu = statistics.mean(tps) if tps else math.nan
            cases.append({
                "scheduler": scheduler,
                "threads": threads,
                "n": len(vals),
                "tps_mean": fmt(mu),
                "tps_stdev": fmt(sd),
                "tps_cv": fmt(sd / mu if tps and mu else math.nan),
                "lat_avg_mean": fmt(statistics.mean(avg) if avg else math.nan),
                "lat_p95_mean": fmt(statistics.mean(p95) if p95 else math.nan),
                "lat_p99_mean": fmt(statistics.mean(p99) if p99 else math.nan),
                "qps_mean": fmt(statistics.mean(qps) if qps else math.nan),
                "failures": failures_by_case.get((scheduler, threads), 0),
            })
    write_csv(phase_dir / "summary_cases.csv", cases)
    shutil.copyfile(phase_dir / "summary_cases.csv", root / "summary_cases.csv")


def summarize_metrics(root: Path, args: argparse.Namespace) -> None:
    phase_dir = root / "mechanism"
    raw_root = phase_dir / "raw"
    rows: list[dict[str, Any]] = []
    failures_by_case: dict[tuple[str, int], int] = {}
    if raw_root.exists():
        for raw in sorted(p for p in raw_root.iterdir() if p.is_dir()):
            env = load_env(raw / "env.json")
            metrics = parse_sysbench_log(raw / "sysbench.log")
            perf = parse_perf_csv(raw / "perf.log", args.run_time)
            busy_ratio, busy_cpus = proc_busy(raw / "proc_stat.before.log", raw / "proc_stat.after.log")
            numa = parse_numastat(raw / "numastat.after.log")
            scheduler = env.get("scheduler", "")
            threads = int(env.get("threads", 0) or 0)
            sched_failure = scheduler_log_failure(raw)
            failure = env.get("failure_type", "")
            if sched_failure and not failure:
                failure = "scheduler_runtime_failure"
            valid = (
                env.get("sysbench_returncode") == 0
                and not failure
                and metrics.get("tps") is not None
                and perf.get("task_clock_ms") is not None
            )
            if not valid:
                failures_by_case[(scheduler, threads)] = failures_by_case.get((scheduler, threads), 0) + 1
            rows.append({
                "scheduler": scheduler,
                "threads": threads,
                "run_id": env.get("run_id", ""),
                "valid": int(valid),
                "failure_type": failure,
                "failure_detail": env.get("failure_detail", "") or sched_failure,
                "tps": metrics.get("tps", ""),
                "lat_p95": metrics.get("lat_p95_ms", ""),
                "lat_p99": metrics.get("lat_p99_ms", ""),
                "ipc": perf.get("ipc", ""),
                "pmu_instructions_available": int(perf.get("instructions", 0) > 0),
                "pmu_cache_counters_available": int(perf.get("cache-references", 0) > 0),
                "instructions": perf.get("instructions", ""),
                "cycles": perf.get("cycles", ""),
                "cache_references": perf.get("cache-references", ""),
                "cache_misses": perf.get("cache-misses", ""),
                "cache_miss_rate": perf.get("cache_miss_rate", ""),
                "context_switches": perf.get("context-switches", ""),
                "cpu_migrations": perf.get("cpu-migrations", ""),
                "cs_per_s": perf.get("cs_per_s", ""),
                "mig_per_s": perf.get("mig_per_s", ""),
                "cpus_utilized": perf.get("cpus_utilized", ""),
                "system_busy_ratio": busy_ratio if busy_ratio is not None else "",
                "busy_cpus": busy_cpus if busy_cpus is not None else "",
                "numastat_node0_ratio": numa.get("numastat_node0_ratio", ""),
                "sysbench_returncode": env.get("sysbench_returncode", ""),
                "perf_returncode": env.get("perf_returncode", ""),
                "start_timestamp": env.get("start_timestamp", ""),
                "end_timestamp": env.get("end_timestamp", ""),
                "kernel_version": env.get("kernel_version", ""),
                "mariadb_version": env.get("mariadb_version", ""),
                "innodb_buffer_pool_size": env.get("innodb_buffer_pool_size", ""),
                "sched_ext_initial_state": env.get("sched_ext_initial_state", ""),
                "sched_ext_final_state": env.get("sched_ext_final_state", ""),
                "raw_dir": str(raw),
            })
    write_csv(phase_dir / "summary_metrics_runs.csv", rows)
    shutil.copyfile(phase_dir / "summary_metrics_runs.csv", root / "summary_metrics_runs.csv")

    cases: list[dict[str, Any]] = []
    for scheduler in args.schedulers:
        for threads in args.threads:
            vals = [r for r in rows if r["scheduler"] == scheduler and int(r["threads"] or 0) == threads and r["valid"] == 1]
            cases.append({
                "scheduler": scheduler,
                "threads": threads,
                "n": len(vals),
                "ipc_mean": mean_field(vals, "ipc"),
                "ipc_stdev": stdev_field(vals, "ipc"),
                "cpus_utilized_mean": mean_field(vals, "cpus_utilized"),
                "busy_cpus_mean": mean_field(vals, "busy_cpus"),
                "mig_per_s_mean": mean_field(vals, "mig_per_s"),
                "cs_per_s_mean": mean_field(vals, "cs_per_s"),
                "cache_miss_rate_mean": mean_field(vals, "cache_miss_rate"),
                "failures": failures_by_case.get((scheduler, threads), 0),
            })
    write_csv(phase_dir / "summary_metrics_cases.csv", cases)
    shutil.copyfile(phase_dir / "summary_metrics_cases.csv", root / "summary_metrics_cases.csv")


def fmt(value: Any) -> str:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    if math.isnan(v):
        return ""
    return f"{v:.6f}"


def values(rows: list[dict[str, Any]], field: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        if r.get(field) in ("", None):
            continue
        try:
            out.append(float(r[field]))
        except (TypeError, ValueError):
            pass
    return out


def mean_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = values(rows, field)
    return fmt(statistics.mean(vals) if vals else math.nan)


def stdev_field(rows: list[dict[str, Any]], field: str) -> str:
    vals = values(rows, field)
    return fmt(statistics.pstdev(vals) if len(vals) >= 2 else (0.0 if vals else math.nan))


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
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def maybe_plot(root: Path) -> None:
    plot_script = Path(__file__).with_name("plot_formal_mariadb_oltp.py")
    if not plot_script.exists():
        return
    subprocess.run([sys.executable, str(plot_script), "--root", str(root)], check=False)


def maybe_update_ledger() -> None:
    ledger_script = Path(__file__).with_name("build_experiment_ledger.py")
    if not ledger_script.exists():
        return
    subprocess.run([sys.executable, str(ledger_script)], check=False)


def phase_performance(args: argparse.Namespace, root: Path) -> None:
    phase_dir = root / "performance"
    raw_root = phase_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scheduler in args.schedulers:
            global_runtime_failures = global_failure_count(
                raw_root,
                scheduler,
                {"scheduler_runtime_failure", "rollback_failure"},
            )
            global_runtime_failures = max(
                global_runtime_failures,
                global_failure_count_all_phases(root, scheduler, {"scheduler_runtime_failure", "rollback_failure"}),
            )
            if global_runtime_failures >= args.max_global_runtime_failures_per_scheduler:
                log(f"ERROR skip {scheduler} t{threads} after {global_runtime_failures} global scheduler runtime/rollback failures")
                continue
            if scheduler != "default":
                state, rollback = cleanup_scx()
                if state != "disabled" or rollback:
                    log(f"WARN: sched_ext cleanup before {scheduler} t{threads}: state={state}")
            while True:
                valid = valid_performance_runs(raw_root, scheduler, threads)
                if len(valid) < args.performance_min_runs:
                    target = args.performance_min_runs
                else:
                    tps = [float(r["tps"]) for r in valid]
                    mu = statistics.mean(tps) if tps else 0.0
                    cv = statistics.pstdev(tps) / mu if len(tps) >= 2 and mu else 0.0
                    if cv > args.cv_threshold and len(valid) < args.performance_max_valid_runs:
                        target = args.performance_max_valid_runs
                    else:
                        log(f"Complete performance {scheduler} t{threads}: valid={len(valid)} cv={cv:.3f}")
                        break
                run_ids = existing_run_ids(raw_root, scheduler, threads)
                next_id = (max(run_ids) + 1) if run_ids else 1
                if next_id > args.max_attempts_per_case:
                    log(f"ERROR too many attempts for {scheduler} t{threads}; valid={len(valid)} target={target}")
                    break
                if failure_count(raw_root, scheduler, threads, "sysbench_timeout") >= args.max_timeouts_per_case:
                    log(f"ERROR skip {scheduler} t{threads} after {args.max_timeouts_per_case} sysbench_timeout failures")
                    break
                runtime_failures = (
                    failure_count(raw_root, scheduler, threads, "scheduler_runtime_failure")
                    + failure_count(raw_root, scheduler, threads, "rollback_failure")
                )
                if runtime_failures >= args.max_runtime_failures_per_case:
                    log(f"ERROR skip {scheduler} t{threads} after {runtime_failures} scheduler runtime/rollback failures")
                    break
                log(f"Run performance {scheduler} t{threads} r{next_id} (valid {len(valid)}/{target})")
                run_one(args, "performance", root, scheduler, threads, next_id)
                summarize(root, args, phase="performance")
                if (raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json").exists():
                    env = load_env(raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json")
                    if env.get("failure_type") == "scheduler_startup_failure":
                        log(f"Skip remaining runs for {scheduler} t{threads} after scheduler_startup_failure")
                        break


def phase_mechanism(args: argparse.Namespace, root: Path) -> None:
    phase_dir = root / "mechanism"
    raw_root = phase_dir / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    for threads in args.threads:
        for scheduler in args.schedulers:
            global_runtime_failures = global_failure_count(
                raw_root,
                scheduler,
                {"scheduler_runtime_failure", "rollback_failure"},
            )
            global_runtime_failures = max(
                global_runtime_failures,
                global_failure_count_all_phases(root, scheduler, {"scheduler_runtime_failure", "rollback_failure"}),
            )
            if global_runtime_failures >= args.max_global_runtime_failures_per_scheduler:
                log(f"ERROR skip mechanism {scheduler} t{threads} after {global_runtime_failures} global scheduler runtime/rollback failures")
                continue
            while True:
                valid = valid_metric_runs(raw_root, scheduler, threads, args.run_time)
                if len(valid) >= args.mechanism_runs:
                    log(f"Complete mechanism {scheduler} t{threads}: valid={len(valid)}")
                    break
                run_ids = existing_run_ids(raw_root, scheduler, threads)
                next_id = (max(run_ids) + 1) if run_ids else 1
                if next_id > args.max_attempts_per_case:
                    log(f"ERROR too many mechanism attempts for {scheduler} t{threads}; valid={len(valid)}")
                    break
                if failure_count(raw_root, scheduler, threads, "sysbench_timeout") >= args.max_timeouts_per_case:
                    log(f"ERROR skip mechanism {scheduler} t{threads} after {args.max_timeouts_per_case} sysbench_timeout failures")
                    break
                runtime_failures = (
                    failure_count(raw_root, scheduler, threads, "scheduler_runtime_failure")
                    + failure_count(raw_root, scheduler, threads, "rollback_failure")
                )
                if runtime_failures >= args.max_runtime_failures_per_case:
                    log(f"ERROR skip mechanism {scheduler} t{threads} after {runtime_failures} scheduler runtime/rollback failures")
                    break
                log(f"Run mechanism {scheduler} t{threads} r{next_id} (valid {len(valid)}/{args.mechanism_runs})")
                run_one(args, "mechanism", root, scheduler, threads, next_id)
                summarize(root, args, phase="mechanism")
                env_path = raw_root / f"{scheduler}_t{threads}_r{next_id:02d}" / "env.json"
                if env_path.exists() and load_env(env_path).get("failure_type") == "scheduler_startup_failure":
                    log(f"Skip remaining mechanism runs for {scheduler} t{threads} after scheduler_startup_failure")
                    break


def parse_list_int(s: str) -> list[int]:
    return [int(x) for x in s.replace(",", " ").split() if x.strip()]


def parse_list_str(s: str) -> list[str]:
    return [x for x in s.replace(",", " ").split() if x.strip()]


def parse_scx_args(items: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            continue
        name, raw = item.split("=", 1)
        out[name.strip()] = [x for x in raw.split() if x]
    return out


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Formal MariaDB/Sysbench OLTP runner for test3.")
    ap.add_argument("--root", default="", help="Result root. Default: test3 result/formal_mariadb_oltp_<timestamp>")
    ap.add_argument("--phase", choices=["performance", "mechanism", "summarize", "plot"], default="performance")
    ap.add_argument("--threads", type=parse_list_int, default=DEFAULT_THREADS)
    ap.add_argument("--schedulers", type=parse_list_str, default=DEFAULT_SCHEDS)
    ap.add_argument("--run-time", type=int, default=60)
    ap.add_argument("--warmup-time", type=int, default=10)
    ap.add_argument("--tables", type=int, default=16)
    ap.add_argument("--table-size", type=int, default=1_000_000)
    ap.add_argument("--report-interval", type=int, default=10)
    ap.add_argument("--db-name", default="sbtest")
    ap.add_argument("--db-user", default="sbuser")
    ap.add_argument("--db-pass", default="password")
    ap.add_argument("--performance-min-runs", type=int, default=5)
    ap.add_argument("--performance-max-valid-runs", type=int, default=8)
    ap.add_argument("--cv-threshold", type=float, default=0.15)
    ap.add_argument("--mechanism-runs", type=int, default=3)
    ap.add_argument("--max-attempts-per-case", type=int, default=10)
    ap.add_argument("--max-timeouts-per-case", type=int, default=2)
    ap.add_argument("--max-runtime-failures-per-case", type=int, default=2)
    ap.add_argument("--max-global-runtime-failures-per-scheduler", type=int, default=2)
    ap.add_argument("--sysbench-timeout-slack", type=int, default=90)
    ap.add_argument("--sysbench-timeout-factor", type=float, default=2.0)
    ap.add_argument("--perf-events", type=parse_list_str, default=DEFAULT_PERF_EVENTS)
    ap.add_argument("--scx-arg", action="append", default=[], help="Scheduler args, e.g. --scx-arg scx_lockcoop='-t 4'")
    return ap


def main() -> int:
    args = build_arg_parser().parse_args()
    args.scx_args = parse_scx_args(args.scx_arg)
    if args.root:
        root = Path(args.root).resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = TEST_DIR / "result" / f"formal_mariadb_oltp_{stamp}"
    ensure_config(root, args)

    if shutil.which("sysbench") is None:
        raise SystemExit("error: sysbench not found")
    if sudo_cmd(["true"]).returncode != 0:
        raise SystemExit("error: sudo -n true failed; refresh sudo credential first")

    log(f"formal root: {root}")
    log(f"phase: {args.phase}")
    if args.phase == "performance":
        phase_performance(args, root)
        summarize(root, args, phase="performance")
        maybe_plot(root)
        maybe_update_ledger()
    elif args.phase == "mechanism":
        phase_mechanism(args, root)
        summarize(root, args, phase="mechanism")
        maybe_plot(root)
        maybe_update_ledger()
    elif args.phase == "summarize":
        summarize(root, args, phase=None)
        maybe_update_ledger()
    elif args.phase == "plot":
        maybe_plot(root)
    state, rollback = cleanup_scx()
    log(f"final sched_ext state: {state} rollback_failure={int(rollback)}")
    log(f"done root: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

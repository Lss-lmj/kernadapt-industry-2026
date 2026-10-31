#!/usr/bin/env python3
"""Collect a machine-readable state ledger for LockAgent runs."""

from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import subprocess
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SCX_BIN_DIR = Path(os.environ.get("KERNADAPT_SCX_BIN_DIR", REPO_ROOT / "scx" / "build" / "bin"))
ALLOWED_SCHEDULERS = [
    "default",
    "scx_simple",
    "scx_lockboost",
    "scx_lockcoop",
    "scx_lockcoop",
    "scx_hybrid",
    "scx_toponest",
    "scx_cfl",
    "scx_lcas",
]


def command_output(cmd: list[str], timeout_s: int = 5) -> str | None:
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def sched_ext_state() -> str:
    state = Path("/sys/kernel/sched_ext/state")
    if not state.exists():
        return "unknown"
    try:
        return state.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def numa_nodes() -> int | None:
    nodes = Path("/sys/devices/system/node")
    if not nodes.exists():
        return None
    return len([p for p in nodes.glob("node[0-9]*") if p.is_dir()])


def executable_schedulers() -> list[str]:
    existing = ["default"]
    for scheduler in ALLOWED_SCHEDULERS:
        if scheduler == "default":
            continue
        binary = SCX_BIN_DIR / scheduler
        if binary.exists() and os.access(binary, os.X_OK):
            existing.append(scheduler)
    return existing


def mysql_query(user: str, password: str, db_name: str, query: str) -> str | None:
    cmd = [
        "mysql",
        f"-u{user}",
        f"-p{password}",
        "-N",
        "-B",
        "-e",
        query,
        db_name,
    ]
    return command_output(cmd, timeout_s=10)


def mariadb_state(user: str, password: str, db_name: str) -> dict[str, Any]:
    variables: dict[str, str | None] = {}
    for name in (
        "innodb_buffer_pool_size",
        "innodb_flush_log_at_trx_commit",
        "sync_binlog",
        "log_bin",
        "performance_schema",
        "max_connections",
        "max_prepared_stmt_count",
    ):
        out = mysql_query(user, password, db_name, f"SHOW VARIABLES LIKE '{name}';")
        variables[name] = out.split("\t", 1)[1] if out and "\t" in out else None

    table_count: int | None = None
    out = mysql_query(
        user,
        password,
        db_name,
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE();",
    )
    if out:
        try:
            table_count = int(out.splitlines()[-1])
        except ValueError:
            table_count = None

    row_samples: dict[str, int | None] = {}
    tables = mysql_query(
        user,
        password,
        db_name,
        "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
        "AND table_name LIKE 'sbtest%' ORDER BY table_name LIMIT 3;",
    )
    for table in tables.splitlines() if tables else []:
        count = mysql_query(user, password, db_name, f"SELECT COUNT(*) FROM `{table}`;")
        try:
            row_samples[table] = int(count.splitlines()[-1]) if count else None
        except ValueError:
            row_samples[table] = None

    return {
        "engine": "mariadb",
        "db_name": db_name,
        "variables": variables,
        "innodb_buffer_pool_size": variables.get("innodb_buffer_pool_size"),
        "table_count": table_count,
        "rows_per_table_sample": row_samples,
        "collection_status": "ok" if any(v is not None for v in variables.values()) else "unknown",
    }


def collect_ledger(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "run_id": args.run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {
            "hostname": socket.gethostname(),
            "kernel": platform.release(),
            "platform": platform.platform(),
            "numa_nodes": numa_nodes(),
            "cpu_count": os.cpu_count(),
        },
        "sched_ext": {
            "initial_state": sched_ext_state(),
            "allowed_schedulers": ALLOWED_SCHEDULERS,
            "executable_schedulers": executable_schedulers(),
            "scx_bin_dir": str(SCX_BIN_DIR),
        },
        "database": mariadb_state(args.db_user, args.db_pass, args.db_name),
        "benchmark": {
            "workload": args.workload,
            "threads": args.threads,
            "profile_s": args.profile_s,
            "run_s": args.run_s,
            "restart_protocol": args.restart_protocol,
        },
        "agent": {
            "mode": "single_process_multi_role",
            "roles": ["ProfilerAgent", "PolicyAgent", "CriticAgent", "RuntimeGuard", "ExecutorAgent", "CuratorAgent"],
        },
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--workload", required=True)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--profile-s", type=int, required=True)
    ap.add_argument("--run-s", type=int, required=True)
    ap.add_argument("--restart-protocol", default="restart_before_profile_and_before_execution")
    ap.add_argument("--db-user", default=os.environ.get("LOCKAGENT_DB_USER", "sbuser"))
    ap.add_argument("--db-pass", default=os.environ.get("LOCKAGENT_DB_PASS", "password"))
    ap.add_argument("--db-name", default=os.environ.get("LOCKAGENT_DB_NAME", "sbtest"))
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    ledger = collect_ledger(args)
    write_json(args.out, ledger)
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

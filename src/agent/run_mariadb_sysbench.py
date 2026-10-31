#!/usr/bin/env python3
"""Run one MariaDB/Sysbench OLTP read-write trial for LockAgent.

Unlike the repository sweep scripts, this runner writes only to the provided
output directory and does not update test3 result summaries.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def privileged_cmd(*args: str) -> list[str]:
    if os.geteuid() == 0:
        return list(args)
    return ["sudo", "-n", *args]


def run(cmd: list[str], log: Path | None = None) -> None:
    if log:
        with log.open("w", encoding="utf-8") as f:
            subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, text=True, check=True)
    else:
        subprocess.run(cmd, check=True)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threads", type=int, required=True)
    ap.add_argument("--time", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--db-name", default="sbtest")
    ap.add_argument("--db-user", default="sbuser")
    ap.add_argument("--db-pass", default="password")
    ap.add_argument("--tables", type=int, default=16)
    ap.add_argument("--table-size", type=int, default=1000000)
    ap.add_argument("--report-interval", type=int, default=10)
    ap.add_argument("--restart-mariadb", action="store_true", default=True)
    ap.add_argument("--no-restart-mariadb", action="store_false", dest="restart_mariadb")
    args = ap.parse_args(argv)

    if not shutil.which("sysbench"):
        raise FileNotFoundError("sysbench not found")

    args.out.mkdir(parents=True, exist_ok=True)
    if args.restart_mariadb:
        run(privileged_cmd("systemctl", "restart", "mariadb"), args.out / "mariadb_restart.log")
        time.sleep(5)

    sysbench_args = [
        "sysbench",
        "oltp_read_write",
        f"--mysql-db={args.db_name}",
        f"--mysql-user={args.db_user}",
        f"--mysql-password={args.db_pass}",
        f"--tables={args.tables}",
        f"--table-size={args.table_size}",
        f"--threads={args.threads}",
        f"--time={args.time}",
        f"--report-interval={args.report_interval}",
        "--percentile=95",
        "run",
    ]

    manifest = {
        "cmd": sysbench_args,
        "threads": args.threads,
        "time": args.time,
        "db_name": args.db_name,
        "db_user": args.db_user,
        "tables": args.tables,
        "table_size": args.table_size,
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    (args.out / "sysbench_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    run(sysbench_args, args.out / "sysbench.log")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

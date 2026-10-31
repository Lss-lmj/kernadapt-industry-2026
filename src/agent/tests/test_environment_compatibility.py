#!/usr/bin/env python3
"""Local checks for environment compatibility comparison."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "environment_compatibility.py"
spec = importlib.util.spec_from_file_location("environment_compatibility", MODULE_PATH)
assert spec and spec.loader
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)


def ledger(buffer_pool: str = "134217728", threads: int = 256) -> dict:
    return {
        "benchmark": {"workload": "mariadb-sysbench", "threads": threads, "restart_protocol": "test3"},
        "database": {
            "engine": "mariadb",
            "db_name": "sbtest",
            "innodb_buffer_pool_size": buffer_pool,
            "rows_per_table_sample": {"sbtest1": 1000000},
        },
        "host": {"numa_nodes": 2, "cpu_count": 256},
    }


def main() -> int:
    ok = compat.compare_ledgers(ledger(), ledger())
    assert ok["compatible"] is True, ok

    bad = compat.compare_ledgers(ledger("134217728"), ledger("48318382080"))
    assert bad["compatible"] is False, bad
    assert "buffer_pool_mismatch" in bad["reasons"], bad

    bad_threads = compat.compare_ledgers(ledger(threads=64), ledger(threads=256))
    assert "threads_mismatch" in bad_threads["reasons"], bad_threads
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

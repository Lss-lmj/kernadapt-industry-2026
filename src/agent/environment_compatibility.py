#!/usr/bin/env python3
"""Compare LockAgent state ledgers for environment compatibility."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def nested(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def compare_ledgers(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    checks = {
        "benchmark.workload": "workload_mismatch",
        "benchmark.threads": "threads_mismatch",
        "benchmark.restart_protocol": "restart_protocol_mismatch",
        "database.engine": "database_engine_mismatch",
        "database.db_name": "database_name_mismatch",
        "database.innodb_buffer_pool_size": "buffer_pool_mismatch",
        "host.numa_nodes": "numa_nodes_mismatch",
        "host.cpu_count": "cpu_count_mismatch",
    }
    reasons: list[str] = []
    details: dict[str, dict[str, Any]] = {}
    for path, reason in checks.items():
        lval = nested(left, path)
        rval = nested(right, path)
        details[path] = {"left": lval, "right": rval}
        if lval is None or rval is None:
            reasons.append(f"unknown_{reason}")
        elif lval != rval:
            reasons.append(reason)

    left_rows = nested(left, "database.rows_per_table_sample")
    right_rows = nested(right, "database.rows_per_table_sample")
    if isinstance(left_rows, dict) and isinstance(right_rows, dict):
        common = sorted(set(left_rows) & set(right_rows))
        for table in common:
            if left_rows.get(table) != right_rows.get(table):
                reasons.append("table_rows_mismatch")
                break
    else:
        reasons.append("unknown_table_rows")

    reasons = sorted(set(reasons))
    return {
        "compatible": not reasons,
        "verdict": "compatible" if not reasons else "incompatible",
        "reasons": reasons,
        "details": details,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--left", type=Path, required=True)
    ap.add_argument("--right", type=Path, required=True)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    result = compare_ledgers(load_json(args.left), load_json(args.right))
    if args.out:
        write_json(args.out, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["compatible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

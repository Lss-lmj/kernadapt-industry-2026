#!/usr/bin/env python3
"""Smoke tests for profile_builder output."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
BUILDER = KERNADAPT_ROOT / "agent" / "profile_builder.py"


def build(workload: str, threads: int, default_metrics: dict | None = None) -> dict:
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        out = Path(td) / "profile.json"
        cmd = [
            sys.executable,
            str(BUILDER),
            "--workload",
            workload,
            "--threads",
            str(threads),
            "--out",
            str(out),
        ]
        if default_metrics is not None:
            metrics_path = td_path / "default_metrics.json"
            metrics_path.write_text(json.dumps(default_metrics), encoding="utf-8")
            cmd.extend(["--default-metrics", str(metrics_path)])
        subprocess.run(cmd, cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    mariadb = build("mariadb-sysbench", 256)
    assert mariadb["workload"]["name"] == "MariaDB Sysbench OLTP"
    assert "signals" in mariadb
    assert mariadb["data_provenance"]["contains_oracle_best"] is False
    assert "scx_lockcoop" in mariadb["candidate_schedulers"]
    assert mariadb["signals"]["default_tps_observed"] is None
    assert mariadb["data_provenance"]["summary_csv"] is None

    live = build(
        "mariadb-sysbench",
        256,
        {
            "tps": 32211.06,
            "lat_avg_ms": 7.93,
            "lat_p95_ms": 20.61,
            "lat_max_ms": 599.64,
            "fairness_events_cv": 0.007,
        },
    )
    assert live["signals"]["default_tps_observed"] == 32211.06
    assert live["signals"]["default_p95_ms_observed"] == 20.61
    assert live["signals"]["default_metrics_source"] == "live_profile_window"
    assert live["signals"]["affinity_parallelism_risk"] == "high_parallelism_collapse_risk"

    rocksdb = build("rocksdb-cfl", 128)
    assert rocksdb["workload"]["name"].startswith("RocksDB")
    assert rocksdb["signals"]["lock_address_cardinality"] == "high"
    assert rocksdb["data_provenance"]["contains_oracle_best"] is False
    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

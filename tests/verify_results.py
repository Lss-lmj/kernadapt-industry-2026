#!/usr/bin/env python3
"""Recompute archived openEuler results; never synthesize benchmark samples."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import statistics

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "tests/lockcoop/records/arm_openeulerpaper53_node0all_no_nest_runs5_results_20260801.md"
CSV = ROOT / "tests/lockcoop/openeuler_node0_runs.csv"
SUMMARY = ROOT / "tests/lockcoop/openeuler_node0_summary.json"


def report_rows():
    rows = []
    text = REPORT.read_text(encoding="utf-8").split("### 7.1", 1)[1].split("### 7.2", 1)[0]
    for line in text.splitlines():
        columns = [x.strip() for x in line.strip().strip("|").split("|")]
        if len(columns) != 4 or not re.fullmatch(r"(?:default|scx_\w+)", columns[0]):
            continue
        tps = [float(x.strip()) for x in columns[2].split(",")]
        p95 = [float(x.strip()) for x in columns[3].split(",")]
        assert len(tps) == len(p95) == 5, columns
        for index, (throughput, latency) in enumerate(zip(tps, p95), 1):
            rows.append({"scheduler": columns[0], "threads": int(columns[1]), "run": index, "tps": throughput, "p95_ms": latency})
    assert len(rows) == 140, len(rows)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extract", action="store_true", help="extract the report's 140 recorded samples into CSV")
    args = parser.parse_args()
    expected = report_rows()
    if args.extract:
        with CSV.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(expected[0]))
            writer.writeheader()
            writer.writerows(expected)
    with CSV.open(newline="", encoding="utf-8") as handle:
        rows = [{"scheduler": x["scheduler"], "threads": int(x["threads"]), "run": int(x["run"]), "tps": float(x["tps"]), "p95_ms": float(x["p95_ms"])} for x in csv.DictReader(handle)]
    assert rows == expected, "CSV differs from the archived report"
    cases = []
    for scheduler in dict.fromkeys(x["scheduler"] for x in rows):
        for threads in (64, 128, 192, 256):
            sample = [x for x in rows if x["scheduler"] == scheduler and x["threads"] == threads]
            assert len(sample) == 5
            cases.append({"scheduler": scheduler, "threads": threads, "runs": 5, "tps_mean": round(statistics.mean(x["tps"] for x in sample), 2), "p95_ms_mean": round(statistics.mean(x["p95_ms"] for x in sample), 2)})
    point = {x["scheduler"]: x for x in cases if x["threads"] == 256}
    target = point["scx_lockcoop"]
    assert abs(target["tps_mean"] - 4304.33) <= 0.01
    assert abs(target["p95_ms_mean"] - 133.95) <= 0.01
    result = {"evidence_type": "samples_transcribed_from_archived_experiment_report", "source": str(REPORT.relative_to(ROOT)), "source_sha256": hashlib.sha256(REPORT.read_bytes()).hexdigest(), "environment": {"os": "openEuler 24.03 LTS", "kernel": "6.17.0-lockcoop-scx617", "architecture": "aarch64", "cpus": "0-127", "warmup_seconds": 10, "measurement_seconds": 60, "runs": 5}, "sample_count": len(rows), "cases": cases, "comparison_256_threads": {baseline: {"throughput_ratio": round(target["tps_mean"] / point[baseline]["tps_mean"], 4), "p95_reduction_percent": round((1 - target["p95_ms_mean"] / point[baseline]["p95_ms_mean"]) * 100, 2)} for baseline in ("default", "scx_simple")}}
    if args.extract:
        SUMMARY.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    else:
        assert json.loads(SUMMARY.read_text()) == result, "Summary needs regeneration from source samples"
    print("Verified 140 samples / 28 cases against the archived openEuler report.")
    print(json.dumps(result["comparison_256_threads"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

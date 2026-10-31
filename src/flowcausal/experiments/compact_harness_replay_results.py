#!/usr/bin/env python3
"""Compact historical HarnessReplayBench result directories.

By default this scans ``experiments/results/harness-replaybench`` for
``harness-replay-mix-summary.json`` files and removes reproducible heavy
artifacts from successful case run directories only.  It keeps summaries,
CSV/Markdown reports, workload/controller/flowd/scheduler logs, fidelity audits,
stage-state files, and failed-case work directories.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
from typing import Any

from harness_replay_cleanup import DEFAULT_SUCCESS_ARTIFACTS, compact_case_artifacts


HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_ROOT = HERE / "results" / "harness-replaybench"


def format_bytes(value: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    amount = float(value)
    for unit in units:
        if amount < 1024.0 or unit == units[-1]:
            return f"{amount:.1f}{unit}" if unit != "B" else f"{int(amount)}B"
        amount /= 1024.0
    return f"{value}B"


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"_load_error": str(exc)}
    return data if isinstance(data, dict) else {"_load_error": "summary is not a JSON object"}


def iter_mix_summaries(root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(root.rglob("harness-replay-mix-summary.json"))


def compact_summary(
    summary_path: pathlib.Path,
    *,
    artifact_names: list[str],
    dry_run: bool,
    include_failed: bool,
    use_sudo: bool,
) -> dict[str, Any]:
    payload = load_json(summary_path)
    cases = payload.get("cases")
    record: dict[str, Any] = {
        "summary": str(summary_path),
        "status": payload.get("status"),
        "cases": 0,
        "cleaned_cases": 0,
        "skipped_cases": 0,
        "removed_bytes": 0,
        "case_records": [],
    }
    if not isinstance(cases, list):
        record["error"] = payload.get("_load_error") or "summary has no cases list"
        return record

    for case in cases:
        if not isinstance(case, dict):
            continue
        run_dir_text = case.get("run_dir")
        if not isinstance(run_dir_text, str) or not run_dir_text:
            continue
        returncode = case.get("returncode")
        success = returncode == 0
        enabled = bool(success or include_failed)
        cleanup = compact_case_artifacts(
            pathlib.Path(run_dir_text),
            enabled=enabled,
            success=True if enabled else False,
            artifact_names=artifact_names,
            dry_run=dry_run,
            use_sudo=use_sudo,
        )
        case_record = {
            "instance_id": case.get("instance_id"),
            "returncode": returncode,
            "run_dir": run_dir_text,
            "cleanup": cleanup,
        }
        record["cases"] += 1
        if enabled:
            record["cleaned_cases"] += 1
            record["removed_bytes"] += int(cleanup.get("removed_bytes") or 0)
        else:
            record["skipped_cases"] += 1
        record["case_records"].append(case_record)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compact HarnessReplayBench historical outputs while preserving evidence files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="result tree to scan")
    parser.add_argument("--dry-run", action="store_true", help="report what would be removed without deleting")
    parser.add_argument(
        "--include-failed",
        action="store_true",
        help="also clean failed cases; normally failed work dirs are preserved for debugging",
    )
    parser.add_argument(
        "--artifact",
        action="append",
        default=list(DEFAULT_SUCCESS_ARTIFACTS),
        help="direct child artifact to remove from selected case run dirs; repeatable",
    )
    parser.add_argument("--report-json", help="where to write a detailed cleanup report")
    parser.add_argument("--report-md", help="where to write a short Markdown cleanup report")
    sudo_cleanup = parser.add_mutually_exclusive_group()
    sudo_cleanup.add_argument(
        "--cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_true",
        default=True,
        help="retry cleanup with sudo -n if Docker/root-owned files block normal removal",
    )
    sudo_cleanup.add_argument(
        "--no-cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_false",
        help="do not use sudo while compacting historical results",
    )
    return parser.parse_args()


def write_markdown(path: pathlib.Path, report: dict[str, Any]) -> None:
    lines = [
        "# HarnessReplayBench cleanup report",
        "",
        f"- root: `{report['root']}`",
        f"- dry_run: `{report['dry_run']}`",
        f"- summaries: {report['summaries']}",
        f"- cases: {report['cases']}",
        f"- cleaned_cases: {report['cleaned_cases']}",
        f"- skipped_cases: {report['skipped_cases']}",
        f"- removed_bytes: {report['removed_bytes']} ({report['removed_human']})",
        "",
        "| summary | cleaned | skipped | removed |",
        "|---|---:|---:|---:|",
    ]
    for item in report["summary_records"]:
        removed = int(item.get("removed_bytes") or 0)
        rel = item.get("summary", "")
        lines.append(
            f"| `{rel}` | {item.get('cleaned_cases', 0)} | {item.get('skipped_cases', 0)} | "
            f"{format_bytes(removed)} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.root).resolve()
    summaries = iter_mix_summaries(root)
    records = [
        compact_summary(
            summary,
            artifact_names=args.artifact,
            dry_run=bool(args.dry_run),
            include_failed=bool(args.include_failed),
            use_sudo=bool(args.cleanup_use_sudo),
        )
        for summary in summaries
    ]
    removed = sum(int(item.get("removed_bytes") or 0) for item in records)
    report = {
        "schema": "flowcausal.harness_replay_cleanup.v1",
        "root": str(root),
        "dry_run": bool(args.dry_run),
        "include_failed": bool(args.include_failed),
        "cleanup_use_sudo": bool(args.cleanup_use_sudo),
        "artifacts": args.artifact,
        "timestamp_ns": time.time_ns(),
        "summaries": len(records),
        "cases": sum(int(item.get("cases") or 0) for item in records),
        "cleaned_cases": sum(int(item.get("cleaned_cases") or 0) for item in records),
        "skipped_cases": sum(int(item.get("skipped_cases") or 0) for item in records),
        "removed_bytes": removed,
        "removed_human": format_bytes(removed),
        "summary_records": records,
    }
    report_json = pathlib.Path(args.report_json).resolve() if args.report_json else root / "cleanup-report.json"
    report_md = pathlib.Path(args.report_md).resolve() if args.report_md else root / "cleanup-report.md"
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_markdown(report_md, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "dry_run": bool(args.dry_run),
                "summaries": len(records),
                "cases": report["cases"],
                "cleaned_cases": report["cleaned_cases"],
                "skipped_cases": report["skipped_cases"],
                "removed_bytes": removed,
                "removed_human": report["removed_human"],
                "report_json": str(report_json),
                "report_md": str(report_md),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

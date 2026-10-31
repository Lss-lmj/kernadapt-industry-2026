#!/usr/bin/env python3
"""Local checks for curated evidence bundle packaging."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
KERNADAPT_ROOT = ROOT
BUNDLER = KERNADAPT_ROOT / "analysis" / "evidence_bundle.py"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        source = base / "batch_blocked"
        write_json(
            source / "batch_readiness.json",
            {
                "schema_version": "lockagent.same_batch_readiness.v0.1",
                "status": "blocked",
                "blocking_reasons": ["sudo_noninteractive_available"],
                "checks": [
                    {
                        "name": "sudo_noninteractive_available",
                        "passed": False,
                        "severity": "block",
                        "failure_category": "executor_failure",
                    }
                ],
            },
        )
        write_json(
            source / "batch_manifest.json",
            {
                "batch_id": "batch_blocked",
                "status": "blocked",
                "check_only": True,
                "trials": [],
                "readiness_status": "blocked",
                "readiness_blocking_reasons": ["sudo_noninteractive_available"],
            },
        )
        write_json(source / "state_ledger.json", {"run_id": "batch_blocked"})

        out = base / "bundle"
        proc = subprocess.run(
            [
                sys.executable,
                str(BUNDLER),
                "--source",
                str(source),
                "--out",
                str(out),
                "--protocol",
                "mariadb-sysbench-readiness",
                "--notes",
                "sudo blocked same-batch smoke",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stdout
        manifest = json.loads((out / "evidence_manifest.json").read_text(encoding="utf-8"))
        assert manifest["schema_version"] == "lockagent.evidence_bundle.v0.1", manifest
        assert manifest["source_type"] == "same_batch", manifest
        assert manifest["evidence_classes"] == ["process_evidence", "negative_evidence"], manifest
        assert manifest["claim_safety"]["performance_claim_safe"] is False, manifest
        assert manifest["claim_safety"]["process_claim_safe"] is True, manifest
        assert manifest["claim_safety"]["negative_claim_safe"] is True, manifest
        assert manifest["readiness_blocking_reasons"] == ["sudo_noninteractive_available"], manifest
        assert manifest["artifact_map"]["batch_readiness.json"]["sha256"], manifest
        assert (out / "artifacts" / "batch_readiness.json").exists(), manifest
        readme = (out / "README_EVIDENCE.md").read_text(encoding="utf-8")
        assert "Performance claim safe: `False`" in readme, readme
        assert "sudo_noninteractive_available" in readme, readme

        unsafe = subprocess.run(
            [
                sys.executable,
                str(BUNDLER),
                "--source",
                str(source),
                "--out",
                str(base / "unsafe_bundle"),
                "--class",
                "performance_evidence",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert unsafe.returncode == 2, unsafe.stdout
        assert "not safe for performance_evidence" in unsafe.stdout, unsafe.stdout

        manifest_only = base / "manifest_only"
        no_copy = subprocess.run(
            [
                sys.executable,
                str(BUNDLER),
                "--source",
                str(source),
                "--out",
                str(manifest_only),
                "--no-copy",
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert no_copy.returncode == 0, no_copy.stdout
        manifest_only_json = json.loads((manifest_only / "evidence_manifest.json").read_text(encoding="utf-8"))
        assert manifest_only_json["copied_artifact_count"] == 0, manifest_only_json
        assert not (manifest_only / "artifacts" / "batch_readiness.json").exists(), manifest_only_json

    print("ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

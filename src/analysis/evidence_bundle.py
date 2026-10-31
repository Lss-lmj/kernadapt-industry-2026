#!/usr/bin/env python3
"""Create curated KernAdapt evidence bundles from run or batch artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "lockagent.evidence_bundle.v0.1"
DEFAULT_OUT_ROOT = Path("results/evidence")
EVIDENCE_CLASSES = {
    "debug_run",
    "process_evidence",
    "performance_evidence",
    "negative_evidence",
    "skill_evolution_evidence",
}


BATCH_ARTIFACTS = [
    "batch_manifest.json",
    "batch_readiness.json",
    "state_ledger.json",
    "batch_summary.json",
    "batch_summary.csv",
    "process_evaluation.json",
    "process_evaluation.csv",
    "same_batch_validation.json",
    "skill_repository_update.json",
    "skill_evolution_ledger.jsonl",
]


ONLINE_ARTIFACTS = [
    "online_manifest.json",
    "agent_run.json",
    "state_ledger.json",
    "profile.json",
    "llm_prompt.json",
    "decision.json",
    "critic_preflight.json",
    "runtime_policy_preflight.json",
    "execution/manifest.json",
    "execution/status.json",
    "execution/guard_preflight.json",
    "execution/guard_postflight.json",
    "execution/benchmark/sysbench.log",
    "critic_report.json",
    "trajectory_status.json",
    "failure_attribution.json",
    "fallback_artifacts.json",
    "provenance.json",
    "causal_attribution.json",
    "counterfactual_plan.json",
    "same_batch_validation.json",
    "skill_curation.json",
    "rejected_hypothesis.json",
    "self_evolution_plan.json",
    "system_evolution_state.json",
    "systemic_override_application.json",
    "skill_evolution_application.json",
    "global_memory_record.json",
    "resume_plan.json",
    "resume_result.json",
    "resume_audit.json",
    "execution_graph_runtime.json",
    "graph_scheduler_plan.json",
    "native_langgraph_result.json",
    "runtime_parity.json",
    "process_evaluation.json",
    "process_evaluation.csv",
    "online_summary.json",
    "online_summary.csv",
    "policy_audit.json",
]


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def date_label() -> str:
    return time.strftime("%Y%m%d")


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sanitize_label(label: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label.strip())
    label = label.strip("._-")
    return label or "lockagent_evidence"


def classify_source(source: Path) -> str:
    if (source / "batch_manifest.json").exists() or (source / "batch_readiness.json").exists():
        return "same_batch"
    if (source / "online_manifest.json").exists() or (source / "agent_run.json").exists():
        return "online_run"
    return "generic"


def artifact_list_for(source_type: str) -> list[str]:
    if source_type == "same_batch":
        return list(BATCH_ARTIFACTS)
    if source_type == "online_run":
        return list(ONLINE_ARTIFACTS)
    return [
        path.name
        for path in sorted(Path(".").glob("*"))
        if path.suffix in {".json", ".jsonl", ".csv", ".md", ".log"}
    ]


def unique_out_path(root: Path, label: str) -> Path:
    candidate = root / f"{date_label()}_{sanitize_label(label)}"
    if not candidate.exists():
        return candidate
    for i in range(1, 1000):
        suffixed = root / f"{candidate.name}_{i:02d}"
        if not suffixed.exists():
            return suffixed
    raise RuntimeError(f"could not allocate unique evidence bundle path under {root}")


def infer_classes(
    requested: list[str],
    *,
    readiness: dict[str, Any] | None,
    batch_manifest: dict[str, Any] | None,
    trajectory: dict[str, Any] | None,
    failure: dict[str, Any] | None,
) -> list[str]:
    classes = list(dict.fromkeys(requested))
    if readiness:
        if "process_evidence" not in classes:
            classes.append("process_evidence")
        if readiness.get("status") == "blocked" and "negative_evidence" not in classes:
            classes.append("negative_evidence")
    if batch_manifest and batch_manifest.get("status") in {"blocked", "failed"}:
        if "negative_evidence" not in classes:
            classes.append("negative_evidence")
    if trajectory and trajectory.get("valid") is False:
        if "negative_evidence" not in classes:
            classes.append("negative_evidence")
    categories = (failure or {}).get("failure_categories")
    if isinstance(categories, list) and categories:
        if "negative_evidence" not in classes:
            classes.append("negative_evidence")
    if not classes:
        classes.append("debug_run")
    return classes


def summarize_claim_safety(
    *,
    source_type: str,
    readiness: dict[str, Any] | None,
    batch_manifest: dict[str, Any] | None,
    trajectory: dict[str, Any] | None,
    execution_status: dict[str, Any] | None,
) -> dict[str, Any]:
    readiness_status = (readiness or {}).get("status")
    batch_status = (batch_manifest or {}).get("status")
    check_only = bool((batch_manifest or {}).get("check_only"))
    trial_count = len((batch_manifest or {}).get("trials", [])) if isinstance((batch_manifest or {}).get("trials"), list) else 0
    trajectory_valid = (trajectory or {}).get("valid")
    executor_status = (execution_status or {}).get("status")
    executor_rc = (execution_status or {}).get("returncode")

    if source_type == "same_batch":
        performance_safe = bool(readiness_status == "ready" and batch_status == "ok" and not check_only and trial_count > 0)
    elif source_type == "online_run":
        performance_safe = bool(trajectory_valid is True and executor_status == "ok" and executor_rc == 0)
    else:
        performance_safe = False

    negative_safe = bool(
        readiness_status == "blocked"
        or batch_status in {"blocked", "failed"}
        or trajectory_valid is False
        or (executor_rc not in {None, 0})
    )
    process_safe = bool(readiness or batch_manifest or trajectory or execution_status)
    reasons: list[str] = []
    if readiness_status == "blocked":
        reasons.extend(str(item) for item in (readiness or {}).get("blocking_reasons", []))
    if check_only:
        reasons.append("check_only")
    if source_type == "same_batch" and trial_count == 0:
        reasons.append("no_trials")
    if trajectory_valid is False:
        reasons.append("trajectory_invalid")
    if executor_rc not in {None, 0}:
        reasons.append("executor_nonzero")
    return {
        "performance_claim_safe": performance_safe,
        "process_claim_safe": process_safe,
        "negative_claim_safe": negative_safe,
        "claim_safety_reasons": list(dict.fromkeys(reasons)),
    }


def copy_selected_artifacts(
    *,
    source: Path,
    out: Path,
    relative_paths: list[str],
    copy_files: bool,
) -> tuple[dict[str, Any], list[str]]:
    artifacts: dict[str, Any] = {}
    missing: list[str] = []
    for rel in relative_paths:
        rel_path = Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            continue
        src = source / rel_path
        exists = src.exists() and src.is_file()
        dest = out / "artifacts" / rel_path
        record = {
            "source": str(src),
            "exists": exists,
            "sha256": file_sha256(src) if exists else None,
            "copied_to": str(dest) if exists and copy_files else None,
        }
        artifacts[rel] = record
        if not exists:
            missing.append(rel)
            continue
        if copy_files:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
    return artifacts, missing


def build_readme(manifest: dict[str, Any]) -> str:
    classes = ", ".join(manifest["evidence_classes"])
    blocking = ", ".join(manifest.get("readiness_blocking_reasons") or []) or "none"
    missing = ", ".join(manifest.get("missing_recommended_artifacts") or []) or "none"
    claim_safety = manifest.get("claim_safety", {})
    lines = [
        "# KernAdapt Evidence Bundle",
        "",
        f"- Bundle ID: `{manifest['bundle_id']}`",
        f"- Source: `{manifest['source_dir']}`",
        f"- Source type: `{manifest['source_type']}`",
        f"- Evidence classes: `{classes}`",
        f"- Protocol: `{manifest.get('protocol') or 'unspecified'}`",
        f"- Generated at: `{manifest['generated_at']}`",
        "",
        "## Claim Safety",
        "",
        f"- Performance claim safe: `{claim_safety.get('performance_claim_safe')}`",
        f"- Process claim safe: `{claim_safety.get('process_claim_safe')}`",
        f"- Negative claim safe: `{claim_safety.get('negative_claim_safe')}`",
        f"- Readiness status: `{manifest.get('readiness_status')}`",
        f"- Readiness blocking reasons: `{blocking}`",
        "",
        "## Artifacts",
        "",
        f"- Copied artifacts: `{manifest.get('copied_artifact_count', 0)}`",
        f"- Missing recommended artifacts: `{missing}`",
        "",
        "This bundle is curated metadata for audit and paper triage. Raw runtime",
        "outputs remain outside source-code commits unless this bundle is explicitly",
        "selected as paper evidence.",
        "",
    ]
    notes = manifest.get("notes") or []
    if notes:
        lines.extend(["## Notes", ""])
        lines.extend(f"- {note}" for note in notes)
        lines.append("")
    return "\n".join(lines)


def create_bundle(args: argparse.Namespace) -> dict[str, Any]:
    source = args.source.resolve()
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {source}")
    source_type = classify_source(source)
    label = sanitize_label(args.label or source.name)
    out = args.out.resolve() if args.out else unique_out_path(args.out_root.resolve(), label)
    if out.exists() and any(out.iterdir()) and not args.overwrite:
        raise FileExistsError(f"output bundle already exists and is not empty: {out}")
    out.mkdir(parents=True, exist_ok=True)

    readiness = read_json(source / "batch_readiness.json")
    batch_manifest = read_json(source / "batch_manifest.json")
    trajectory = read_json(source / "trajectory_status.json")
    failure = read_json(source / "failure_attribution.json")
    execution_status = read_json(source / "execution" / "status.json")

    requested_classes = args.evidence_class or []
    classes = infer_classes(
        requested_classes,
        readiness=readiness,
        batch_manifest=batch_manifest,
        trajectory=trajectory,
        failure=failure,
    )
    invalid_classes = [item for item in classes if item not in EVIDENCE_CLASSES]
    if invalid_classes:
        raise ValueError(f"unknown evidence classes: {invalid_classes}")

    relative_paths = list(dict.fromkeys([*artifact_list_for(source_type), *(args.include or [])]))
    artifact_map, missing = copy_selected_artifacts(
        source=source,
        out=out,
        relative_paths=relative_paths,
        copy_files=not args.no_copy,
    )
    copied_count = sum(1 for item in artifact_map.values() if item.get("copied_to"))
    claim_safety = summarize_claim_safety(
        source_type=source_type,
        readiness=readiness,
        batch_manifest=batch_manifest,
        trajectory=trajectory,
        execution_status=execution_status,
    )
    if "performance_evidence" in classes and not claim_safety["performance_claim_safe"] and not args.allow_unsafe_performance:
        raise RuntimeError(
            "source is not safe for performance_evidence; use a same-batch complete run or pass --allow-unsafe-performance"
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": out.name,
        "generated_at": timestamp(),
        "source_dir": str(source),
        "source_type": source_type,
        "evidence_classes": classes,
        "protocol": args.protocol,
        "notes": args.notes or [],
        "readiness_status": (readiness or {}).get("status"),
        "readiness_blocking_reasons": (readiness or {}).get("blocking_reasons", []),
        "batch_status": (batch_manifest or {}).get("status"),
        "batch_check_only": bool((batch_manifest or {}).get("check_only")),
        "batch_trial_count": len((batch_manifest or {}).get("trials", [])) if isinstance((batch_manifest or {}).get("trials"), list) else 0,
        "claim_safety": claim_safety,
        "artifact_map": artifact_map,
        "copied_artifact_count": copied_count,
        "missing_recommended_artifacts": missing,
    }
    (out / "evidence_manifest.json").write_text(stable_json(manifest) + "\n", encoding="utf-8")
    (out / "README_EVIDENCE.md").write_text(build_readme(manifest), encoding="utf-8")
    return manifest | {"bundle_dir": str(out)}


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source", type=Path, required=True, help="Source KernAdapt run or same-batch directory")
    ap.add_argument("--out", type=Path, help="Output evidence bundle directory")
    ap.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT, help="Root used when --out is omitted")
    ap.add_argument("--label", help="Bundle label used when --out is omitted")
    ap.add_argument("--class", dest="evidence_class", action="append", choices=sorted(EVIDENCE_CLASSES), help="Evidence class; can be repeated")
    ap.add_argument("--protocol", help="Human-readable protocol name")
    ap.add_argument("--notes", action="append", help="Additional note included in README and manifest")
    ap.add_argument("--include", action="append", help="Additional relative artifact path to copy")
    ap.add_argument("--no-copy", action="store_true", help="Write manifest/README only; do not copy artifacts")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--allow-unsafe-performance", action="store_true", help="Allow performance_evidence class even when claim safety is false")
    return ap.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        result = create_bundle(args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(result["bundle_dir"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

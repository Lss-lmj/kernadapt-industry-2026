#!/usr/bin/env python3
"""Prepare a reproducible real-agent evidence bundle for FlowCausal.

This script wires together the per-artifact helpers:

1. raw SWE-agent/OpenHands output -> ``flowcausal.agent_trace.v1``;
2. manual/oracle labels -> annotated trace with label provenance;
3. raw full-harness endpoint -> ``flowcausal.full_harness_endpoint.v1``;
4. annotated trace -> repo replay matrix command;
5. optional matrix summary -> six-gate bundle audit.

By default the script is generate-only: it normalizes local artifacts and writes
``run-matrix.sh``/``run-audit.sh`` without attaching sched_ext.  Pass
``--run-matrix`` only when the machine is ready to run the scheduler experiment.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
WORKSPACE = ROOT.parent


def safe_name(value: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in value.strip())
    return out.strip("-") or "instance"


def sh(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def run(cmd: list[str], *, cwd: pathlib.Path = WORKSPACE) -> None:
    print("+ " + sh(cmd), flush=True)
    subprocess.run(cmd, cwd=str(cwd), check=True)


def write_executable_script(path: pathlib.Path, cmd: list[str], *, cwd: pathlib.Path = WORKSPACE) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"cd {shlex.quote(str(cwd))}\n"
        f"{sh(cmd)}\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | 0o111)


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def has_frontier_label(trace_path: pathlib.Path) -> bool:
    data = read_json(trace_path)
    commands = data.get("commands") if isinstance(data, dict) else []
    if not isinstance(commands, list):
        return False
    for command in commands:
        if isinstance(command, dict) and command.get("frontier_label") is not None:
            return True
    return False


def build_workload_command(*, trace_path: pathlib.Path, repo_dir: pathlib.Path, bundle_safe_name: str) -> str:
    work_dir = f"/tmp/flowcausal-trace-replay-{bundle_safe_name}-${{FLOWCAUSAL_VARIANT}}-${{FLOWCAUSAL_TRIAL}}"
    parts = [
        shlex.quote(sys.executable),
        shlex.quote(str(HERE / "workloads" / "agent_trace_workflow.py")),
        "--trace-file",
        shlex.quote(str(trace_path)),
        "--repo-dir",
        shlex.quote(str(repo_dir)),
        "--work-dir",
        work_dir,
        "--overwrite",
        "--pre-sleep-ms",
        "0",
        "--post-sleep-ms",
        "0",
    ]
    return " ".join(parts)


def build_matrix_cmd(
    args: argparse.Namespace,
    *,
    trace_path: pathlib.Path,
    repo_dir: pathlib.Path,
    matrix_dir: pathlib.Path,
    bundle_safe_name: str,
    include_oracle: bool,
) -> list[str]:
    cmd = [
        sys.executable,
        str(HERE / "run_repo_ci_matrix.py"),
        "--overwrite",
        "--out-dir",
        str(matrix_dir),
        "--label",
        f"real-agent-{args.instance_id}",
        "--trials",
        str(args.trials),
        "--stage-kind",
        "AUTO",
        "--frontier-width",
        str(args.frontier_width),
        "--max-frontier-width",
        str(args.max_frontier_width),
        "--frontier-propagation",
        "--stage-aware-frontier-propagation-depth",
        "--build-frontier-admission",
        args.build_frontier_admission,
        "--frontier-stability-action",
        args.frontier_stability_action,
        "--include-static-baselines",
        "--collect-characterization",
        "--command",
        build_workload_command(trace_path=trace_path, repo_dir=repo_dir, bundle_safe_name=bundle_safe_name),
    ]
    if include_oracle:
        cmd.insert(-2, "--include-oracle-baseline")
    if args.taskset:
        cmd.extend(["--taskset", args.taskset])
    if args.victim_command:
        cmd.extend(["--victim-command", args.victim_command])
    if args.victim_taskset:
        cmd.extend(["--victim-taskset", args.victim_taskset])
    if args.victim_warmup_sec is not None:
        cmd.extend(["--victim-warmup-sec", str(args.victim_warmup_sec)])
    if args.matrix_extra_args:
        cmd.extend(shlex.split(args.matrix_extra_args))
    return cmd


def build_audit_cmd(
    *,
    raw_agent_output: pathlib.Path,
    normalized_trace: pathlib.Path,
    full_harness_endpoint: pathlib.Path,
    matrix_summary: pathlib.Path,
    audit_output: pathlib.Path,
    audit_markdown_output: pathlib.Path | None = None,
    harness_matrix_summary: pathlib.Path | None = None,
    harness_matrix_audit: pathlib.Path | None = None,
    victim_log: pathlib.Path | None = None,
    live_dir: pathlib.Path | None = None,
    harness_matrix_source_variant: str = "",
    harness_matrix_source_trial: int | None = None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(HERE / "audit_agent_trace_bundle.py"),
        "--raw-agent-output",
        str(raw_agent_output),
        "--normalized-trace",
        str(normalized_trace),
        "--full-harness-result",
        str(full_harness_endpoint),
        "--matrix-summary",
        str(matrix_summary),
        "--output",
        str(audit_output),
    ]
    if audit_markdown_output is not None:
        cmd.extend(["--markdown-output", str(audit_markdown_output)])
    if harness_matrix_summary is not None:
        cmd.extend(["--harness-matrix-summary", str(harness_matrix_summary)])
    if harness_matrix_audit is not None:
        cmd.extend(["--harness-matrix-audit", str(harness_matrix_audit)])
    if victim_log is not None:
        cmd.extend(["--victim-log", str(victim_log)])
    if live_dir is not None:
        cmd.extend(["--live-dir", str(live_dir)])
    if harness_matrix_source_variant:
        cmd.extend(["--harness-matrix-source-variant", harness_matrix_source_variant])
    if harness_matrix_source_trial is not None:
        cmd.extend(["--harness-matrix-source-trial", str(harness_matrix_source_trial)])
    return cmd


def write_readme(
    path: pathlib.Path,
    *,
    manifest: dict[str, Any],
    matrix_cmd: list[str],
    audit_cmd: list[str] | None,
) -> None:
    lines = [
        "# FlowCausal real-agent evidence bundle",
        "",
        "This directory is generated by `experiments/prepare_agent_trace_bundle.py`.",
        "It is an artifact scaffold for one real code-agent benchmark instance.",
        "",
        "## Artifacts",
        "",
    ]
    for key, value in manifest.get("artifacts", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Run replay matrix",
            "",
            "```bash",
            "./run-matrix.sh",
            "```",
            "",
            "Expanded command:",
            "",
            "```bash",
            sh(matrix_cmd),
            "```",
        ]
    )
    if audit_cmd is not None:
        lines.extend(
            [
                "",
                "## Run bundle audit",
                "",
                "```bash",
                "./run-audit.sh",
                "```",
                "",
                "Expanded command:",
                "",
                "```bash",
                sh(audit_cmd),
                "```",
            ]
        )
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "A generated bundle is not a EuroSys result by itself.  The claim only",
            "becomes strong after the replay matrix is run, the full-harness endpoint",
            "is aligned, manual/oracle frontier labels are present, and the six-gate",
            "bundle audit passes.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a FlowCausal real-agent trace evidence bundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--raw-agent-output", required=True)
    parser.add_argument("--raw-format", choices=("auto", "swe-agent", "openhands", "flowcausal"), default="auto")
    parser.add_argument("--source", default="", help="logical raw-agent source label")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--agent-repo-root", default="")
    parser.add_argument("--repo-dir", required=True, help="local repository checkout used for trace replay")
    parser.add_argument("--frontier-annotations", help="manual/oracle frontier label JSON/JSONL")
    parser.add_argument("--annotator", default="")
    parser.add_argument("--annotation-source", choices=("manual", "oracle"), default="manual")
    parser.add_argument("--replace-heuristic", action="store_true")
    parser.add_argument("--emit-heuristic-frontier-labels", action="store_true")
    parser.add_argument("--critical-policy", choices=("last-test", "last-foreground", "explicit-only"), default="last-test")
    parser.add_argument("--full-harness-result", required=True)
    parser.add_argument(
        "--full-harness-format",
        choices=("auto", "swe-bench", "swe-agent", "openhands", "flowcausal", "generic"),
        default="auto",
    )
    parser.add_argument("--scheduler-variant", default="flowcausal_width")
    parser.add_argument("--matrix-summary", help="existing matrix-summary.json to audit instead of running matrix")
    parser.add_argument("--harness-matrix-summary", help="full-harness matrix summary that selected this bundle's raw/endpoint trial")
    parser.add_argument("--harness-matrix-audit", help="audit_real_agent_harness_matrix.py JSON for the full-harness matrix")
    parser.add_argument("--victim-log", help="optional full-harness/live victim.log archived with the selected trial")
    parser.add_argument("--live-dir", help="optional live scheduler directory archived with the selected trial")
    parser.add_argument("--harness-matrix-source-variant", default="", help="variant of the selected full-harness matrix trial")
    parser.add_argument("--harness-matrix-source-trial", type=int, help="trial number of the selected full-harness matrix trial")
    parser.add_argument("--harness-matrix-trial-selection", default="", help="selection policy used by export_harness_matrix_bundle_manifest.py")
    parser.add_argument("--run-matrix", action="store_true")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--taskset", default="")
    parser.add_argument("--frontier-width", type=int, default=1)
    parser.add_argument("--max-frontier-width", type=int, default=1)
    parser.add_argument("--build-frontier-admission", choices=("advance", "tail-only", "observe"), default="tail-only")
    parser.add_argument("--frontier-stability-action", choices=("suppress", "observe"), default="observe")
    parser.add_argument("--victim-command", default="")
    parser.add_argument("--victim-taskset", default="")
    parser.add_argument("--victim-warmup-sec", type=float, default=0.2)
    parser.add_argument("--matrix-extra-args", default="", help="extra args appended to run_repo_ci_matrix.py")
    parser.add_argument("--created-at", help="timestamp forwarded to normalizers/annotator")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"{out_dir} exists; pass --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)
    traces_dir = out_dir / "traces"
    results_dir = out_dir / "results"
    traces_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    instance_safe = safe_name(args.instance_id)
    raw_agent_output = pathlib.Path(args.raw_agent_output).resolve()
    repo_dir = pathlib.Path(args.repo_dir).resolve()
    full_harness_raw = pathlib.Path(args.full_harness_result).resolve()
    harness_matrix_summary = pathlib.Path(args.harness_matrix_summary).resolve() if args.harness_matrix_summary else None
    harness_matrix_audit = pathlib.Path(args.harness_matrix_audit).resolve() if args.harness_matrix_audit else None
    victim_log = pathlib.Path(args.victim_log).resolve() if args.victim_log else None
    live_dir = pathlib.Path(args.live_dir).resolve() if args.live_dir else None
    matrix_dir = results_dir / "matrix"
    audit_output = out_dir / "agent-trace-bundle-audit.json"
    audit_markdown_output = out_dir / "agent-trace-bundle-audit.md"

    normalized_trace = traces_dir / f"{instance_safe}.flowcausal-trace.json"
    annotated_trace = traces_dir / f"{instance_safe}.annotated.flowcausal-trace.json"
    annotation_template = traces_dir / f"{instance_safe}.frontier-labels.template.json"
    endpoint = traces_dir / f"{instance_safe}.full-harness-endpoint.json"

    normalize_trace_cmd = [
        sys.executable,
        str(HERE / "normalize_agent_trace.py"),
        "--input",
        str(raw_agent_output),
        "--output",
        str(normalized_trace),
        "--format",
        args.raw_format,
        "--instance-id",
        args.instance_id,
        "--critical-policy",
        args.critical_policy,
    ]
    if args.source:
        normalize_trace_cmd.extend(["--source", args.source])
    if args.agent_repo_root:
        normalize_trace_cmd.extend(["--agent-repo-root", args.agent_repo_root])
    if args.emit_heuristic_frontier_labels:
        normalize_trace_cmd.append("--emit-heuristic-frontier-labels")
    if args.created_at:
        normalize_trace_cmd.extend(["--created-at", args.created_at])
    run(normalize_trace_cmd)

    trace_for_matrix = normalized_trace
    annotate_cmd: list[str] | None = None
    annotation_template_cmd: list[str] | None = None
    if args.frontier_annotations:
        annotation_path = pathlib.Path(args.frontier_annotations).resolve()
        annotate_cmd = [
            sys.executable,
            str(HERE / "annotate_frontier_labels.py"),
            "--input",
            str(normalized_trace),
            "--annotations",
            str(annotation_path),
            "--output",
            str(annotated_trace),
            "--source",
            args.annotation_source,
            "--mark-critical",
        ]
        if args.annotator:
            annotate_cmd.extend(["--annotator", args.annotator])
        if args.replace_heuristic:
            annotate_cmd.append("--replace-heuristic")
        if args.created_at:
            annotate_cmd.extend(["--created-at", args.created_at])
        run(annotate_cmd)
        trace_for_matrix = annotated_trace
    else:
        annotation_template_cmd = [
            sys.executable,
            str(HERE / "generate_frontier_annotation_template.py"),
            "--input",
            str(normalized_trace),
            "--output",
            str(annotation_template),
            "--candidate-policy",
            "critical-or-test",
        ]
        if args.created_at:
            annotation_template_cmd.extend(["--created-at", args.created_at])
        run(annotation_template_cmd)

    normalize_endpoint_cmd = [
        sys.executable,
        str(HERE / "normalize_full_harness_result.py"),
        "--input",
        str(full_harness_raw),
        "--output",
        str(endpoint),
        "--format",
        args.full_harness_format,
        "--instance-id",
        args.instance_id,
        "--scheduler-variant",
        args.scheduler_variant,
    ]
    if args.created_at:
        normalize_endpoint_cmd.extend(["--created-at", args.created_at])
    run(normalize_endpoint_cmd)

    include_oracle = has_frontier_label(trace_for_matrix)
    matrix_cmd = build_matrix_cmd(
        args,
        trace_path=trace_for_matrix,
        repo_dir=repo_dir,
        matrix_dir=matrix_dir,
        bundle_safe_name=instance_safe,
        include_oracle=include_oracle,
    )
    write_executable_script(out_dir / "run-matrix.sh", matrix_cmd)

    matrix_summary = pathlib.Path(args.matrix_summary).resolve() if args.matrix_summary else matrix_dir / "matrix-summary.json"
    audit_cmd: list[str] | None = None
    if args.run_matrix:
        run(matrix_cmd)
    if args.matrix_summary or matrix_summary.exists():
        audit_cmd = build_audit_cmd(
            raw_agent_output=raw_agent_output,
            normalized_trace=trace_for_matrix,
            full_harness_endpoint=endpoint,
            matrix_summary=matrix_summary,
            audit_output=audit_output,
            audit_markdown_output=audit_markdown_output,
            harness_matrix_summary=harness_matrix_summary,
            harness_matrix_audit=harness_matrix_audit,
            victim_log=victim_log,
            live_dir=live_dir,
            harness_matrix_source_variant=args.harness_matrix_source_variant,
            harness_matrix_source_trial=args.harness_matrix_source_trial,
        )
        write_executable_script(out_dir / "run-audit.sh", audit_cmd)
        run(audit_cmd)
    else:
        audit_cmd = build_audit_cmd(
            raw_agent_output=raw_agent_output,
            normalized_trace=trace_for_matrix,
            full_harness_endpoint=endpoint,
            matrix_summary=matrix_summary,
            audit_output=audit_output,
            audit_markdown_output=audit_markdown_output,
            harness_matrix_summary=harness_matrix_summary,
            harness_matrix_audit=harness_matrix_audit,
            victim_log=victim_log,
            live_dir=live_dir,
            harness_matrix_source_variant=args.harness_matrix_source_variant,
            harness_matrix_source_trial=args.harness_matrix_source_trial,
        )
        write_executable_script(out_dir / "run-audit.sh", audit_cmd)

    status = "audited" if audit_output.exists() else ("matrix-ready" if not args.run_matrix else "matrix-run-without-audit")
    manifest = {
        "schema": "flowcausal.agent_trace_bundle_manifest.v1",
        "created_at": args.created_at or time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "status": status,
        "instance_id": args.instance_id,
        "inputs": {
            "raw_agent_output": str(raw_agent_output),
            "repo_dir": str(repo_dir),
            "frontier_annotations": str(pathlib.Path(args.frontier_annotations).resolve()) if args.frontier_annotations else "",
            "full_harness_result": str(full_harness_raw),
            "matrix_summary": str(matrix_summary) if args.matrix_summary else "",
            "harness_matrix_summary": str(harness_matrix_summary) if harness_matrix_summary else "",
            "harness_matrix_audit": str(harness_matrix_audit) if harness_matrix_audit else "",
            "victim_log": str(victim_log) if victim_log else "",
            "live_dir": str(live_dir) if live_dir else "",
            "harness_matrix_source_variant": args.harness_matrix_source_variant,
            "harness_matrix_source_trial": args.harness_matrix_source_trial if args.harness_matrix_source_trial is not None else "",
            "harness_matrix_trial_selection": args.harness_matrix_trial_selection,
        },
        "artifacts": {
            "normalized_trace": str(normalized_trace),
            "annotated_trace": str(annotated_trace) if args.frontier_annotations else "",
            "frontier_annotation_template": str(annotation_template),
            "trace_for_matrix": str(trace_for_matrix),
            "full_harness_endpoint": str(endpoint),
            "matrix_dir": str(matrix_dir),
            "matrix_summary": str(matrix_summary),
            "harness_matrix_summary": str(harness_matrix_summary) if harness_matrix_summary else "",
            "harness_matrix_audit": str(harness_matrix_audit) if harness_matrix_audit else "",
            "victim_log": str(victim_log) if victim_log else "",
            "live_dir": str(live_dir) if live_dir else "",
            "bundle_audit": str(audit_output),
            "bundle_audit_markdown": str(audit_markdown_output),
            "run_matrix_script": str(out_dir / "run-matrix.sh"),
            "run_audit_script": str(out_dir / "run-audit.sh"),
        },
        "commands": {
            "normalize_agent_trace": normalize_trace_cmd,
            "generate_frontier_annotation_template": annotation_template_cmd or [],
            "annotate_frontier_labels": annotate_cmd or [],
            "normalize_full_harness_result": normalize_endpoint_cmd,
            "run_repo_ci_matrix": matrix_cmd,
            "audit_agent_trace_bundle": audit_cmd or [],
        },
        "notes": [
            "Generated artifacts alone do not prove a EuroSys claim.",
            "Use run-matrix.sh on the target scheduler machine, then run-audit.sh.",
            "The oracle_frontier baseline is included only when the trace contains frontier_label records.",
            "If frontier_annotations was omitted, the generated annotation template is disabled and does not count as frontier-correctness evidence.",
        ],
    }
    (out_dir / "bundle-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_readme(out_dir / "README.md", manifest=manifest, matrix_cmd=matrix_cmd, audit_cmd=audit_cmd)
    print(json.dumps({"out_dir": str(out_dir), "status": status}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

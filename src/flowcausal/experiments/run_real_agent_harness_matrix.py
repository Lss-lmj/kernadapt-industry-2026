#!/usr/bin/env python3
"""Run a full real-agent harness endpoint matrix for FlowCausal.

This script is the missing bridge between two existing pieces:

* ``capture_real_agent_run.py`` archives one SWE-agent/OpenHands/SWE-bench style
  harness run, including raw trajectory and full-harness endpoint; and
* ``run_repo_ci_matrix.py`` runs replay/CI commands under scheduler variants.

For the paper we also need a stricter artifact: the *complete* agent harness
itself should be runnable under the same scheduler matrix.  This helper creates
that matrix for one benchmark instance.  It is conservative by default: without
``--execute`` it only writes a manifest and a run script.  When executed, each
variant/trial is archived through ``capture_real_agent_run.py`` so endpoint
evidence remains clearly separated from replay-only evidence.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import shlex
import statistics
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from normalize_full_harness_result import normalize_endpoint, read_json_or_jsonl
from validate_bundle_collection_manifest import safe_name


HERE = pathlib.Path(__file__).resolve().parent
CAPTURE = HERE / "capture_real_agent_run.py"
RUN_LIVE = HERE / "run_live_sandbox.py"
RUN_STATIC_CGROUP = HERE / "run_static_cgroup.py"
SCHEMA = "flowcausal.real_agent_harness_matrix.v1"


VARIANT_PRESETS: dict[str, dict[str, Any]] = {
    "default": {
        "mode": "direct",
        "description": "run the complete harness directly; no FlowCausal observation or sched_ext attachment",
        "live_args": [],
    },
    "nice_priority": {
        "mode": "direct",
        "description": "run the complete harness with a static Linux nice-priority baseline",
        "baseline_type": "nice_priority",
    },
    "static_affinity": {
        "mode": "direct",
        "description": "run the complete harness pinned to a fixed CPU subset as a static-affinity baseline",
        "baseline_type": "static_affinity",
        "skip_global_taskset": True,
    },
    "static_cgroup": {
        "mode": "direct",
        "description": "run the complete harness inside a cgroup-v2 CPU-control baseline group",
        "baseline_type": "static_cgroup",
    },
    "default_observed": {
        "mode": "live",
        "description": "run the complete harness through run_live_sandbox with dry-run hints for comparable observation logs",
        "live_args": ["--hint-dry-run", "--no-build-tools"],
    },
    "flowcausal_no_width": {
        "mode": "live",
        "description": "attach FlowCausal and enable stage-aware frontier propagation, but leave width control disabled",
        "live_args": [
            "--attach-scheduler",
            "--frontier-propagation",
            "--stage-aware-frontier-propagation-depth",
            "--frontier-stability-action",
            "observe",
            "--build-frontier-admission",
            "tail-only",
            "--background-dsq",
            "--background-dispatch-quota",
            "12",
        ],
    },
    "flowcausal_width": {
        "mode": "live",
        "description": "attach FlowCausal with the current stage-aware/tail-only policy and BPF width control",
        "live_args": [
            "--attach-scheduler",
            "--frontier-propagation",
            "--stage-aware-frontier-propagation-depth",
            "--frontier-stability-action",
            "observe",
            "--build-frontier-admission",
            "tail-only",
            "--width-control",
            "--max-frontier-width",
            "1",
            "--background-dsq",
            "--background-dispatch-quota",
            "12",
        ],
    },
}


def parse_variant_list(values: list[str]) -> list[str]:
    variants: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item:
                variants.append(item)
    return variants or ["default", "flowcausal_width"]


def parse_env_templates(values: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"--harness-env must be KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit(f"--harness-env key is empty: {item!r}")
        env[key] = value
    return env


def format_template(template: str, context: dict[str, Any]) -> str:
    try:
        return template.format(**context)
    except KeyError as exc:
        available = ", ".join(sorted(context))
        raise SystemExit(f"unknown template placeholder {exc!s}; available: {available}") from exc


def split_template(template: str, context: dict[str, Any]) -> list[str]:
    rendered = format_template(template, context)
    try:
        return shlex.split(rendered)
    except ValueError as exc:
        raise SystemExit(f"failed to parse --command-template after expansion: {exc}") from exc


def maybe_rel(path: pathlib.Path, base: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path)


def fnum(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0.0 else None


def build_live_args(args: argparse.Namespace, preset: dict[str, Any]) -> list[str]:
    live_args = list(preset.get("live_args", []))
    if args.collect_characterization and "--collect-characterization" not in live_args:
        live_args.append("--collect-characterization")
    if args.live_no_build_tools and "--no-build-tools" not in live_args:
        live_args.append("--no-build-tools")
    if args.taskset:
        live_args.extend(["--taskset", args.taskset])
    if args.victim_command:
        live_args.extend(["--victim-command", args.victim_command])
    if args.victim_taskset:
        live_args.extend(["--victim-taskset", args.victim_taskset])
    for extra in args.live_arg:
        live_args.extend(shlex.split(extra))
    return live_args


def first_taskset_cpu(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "0"
    first = text.split(",", 1)[0].strip()
    if "-" in first:
        first = first.split("-", 1)[0].strip()
    return first or "0"


def static_affinity_taskset(args: argparse.Namespace) -> str:
    return str(args.static_affinity_taskset or first_taskset_cpu(args.taskset))


def preset_direct_prefixes(args: argparse.Namespace, preset: dict[str, Any]) -> list[str]:
    baseline_type = str(preset.get("baseline_type") or "")
    if baseline_type == "nice_priority":
        return [f"nice -n {int(args.nice_value)}"]
    if baseline_type == "static_affinity":
        return [f"taskset -c {static_affinity_taskset(args)}"]
    if baseline_type == "static_cgroup":
        prefix: list[str] = []
        if args.static_cgroup_sudo:
            prefix.extend(["sudo", "-n"])
        prefix.extend([sys.executable, str(pathlib.Path(args.static_cgroup_wrapper).expanduser().resolve())])
        if args.static_cgroup_cpu_weight:
            prefix.extend(["--cpu-weight", str(args.static_cgroup_cpu_weight)])
        if args.static_cgroup_cpu_max:
            prefix.extend(["--cpu-max", str(args.static_cgroup_cpu_max)])
        prefix.append("--")
        return [shell_join(prefix)]
    return []


def apply_direct_prefix(args: argparse.Namespace, command: list[str], *, preset: dict[str, Any]) -> list[str]:
    out = list(command)
    prefixes = [*args.direct_prefix, *preset_direct_prefixes(args, preset)]
    for prefix in reversed(prefixes):
        parsed = shlex.split(prefix)
        if parsed:
            out = [*parsed, *out]
    if args.taskset and not preset.get("skip_global_taskset"):
        out = ["taskset", "-c", args.taskset, *out]
    return out


def trial_context(
    args: argparse.Namespace,
    *,
    out_dir: pathlib.Path,
    variant: str,
    trial: int,
) -> dict[str, Any]:
    safe_instance = safe_name(args.instance_id)
    safe_variant = safe_name(variant)
    trial_name = f"trial-{trial:02d}"
    trial_dir = out_dir / "runs" / safe_variant / trial_name
    work_dir = trial_dir / "work"
    capture_dir = trial_dir / "capture"
    live_dir = trial_dir / "live"
    raw_agent_output = pathlib.Path(
        format_template(
            args.raw_agent_output_template,
            {
                "out_dir": str(out_dir),
                "trial_dir": str(trial_dir),
                "work_dir": str(work_dir),
                "capture_dir": str(capture_dir),
                "live_dir": str(live_dir),
                "instance_id": args.instance_id,
                "safe_instance_id": safe_instance,
                "variant": variant,
                "safe_variant": safe_variant,
                "trial": trial,
                "trial_name": trial_name,
            },
        )
    )
    endpoint_result = pathlib.Path(
        format_template(
            args.endpoint_result_template,
            {
                "out_dir": str(out_dir),
                "trial_dir": str(trial_dir),
                "work_dir": str(work_dir),
                "capture_dir": str(capture_dir),
                "live_dir": str(live_dir),
                "instance_id": args.instance_id,
                "safe_instance_id": safe_instance,
                "variant": variant,
                "safe_variant": safe_variant,
                "trial": trial,
                "trial_name": trial_name,
            },
        )
    )
    context = {
        "out_dir": str(out_dir),
        "trial_dir": str(trial_dir),
        "work_dir": str(work_dir),
        "capture_dir": str(capture_dir),
        "live_dir": str(live_dir),
        "repo_dir": str(pathlib.Path(args.repo_dir).expanduser().resolve()),
        "raw_agent_output": str(raw_agent_output),
        "endpoint_result": str(endpoint_result),
        "instance_id": args.instance_id,
        "safe_instance_id": safe_instance,
        "variant": variant,
        "safe_variant": safe_variant,
        "trial": trial,
        "trial_name": trial_name,
    }
    return context


def build_trial_command(args: argparse.Namespace, *, context: dict[str, Any], preset: dict[str, Any]) -> dict[str, Any]:
    harness_command = split_template(args.command_template, context)
    mode = str(preset.get("mode") or "direct")
    if mode == "direct":
        wrapped_command = apply_direct_prefix(args, harness_command, preset=preset)
    elif mode == "live":
        wrapped_command = [
            sys.executable,
            str(RUN_LIVE),
            "--out-dir",
            str(context["live_dir"]),
            "--session",
            f"{context['instance_id']}:{context['variant']}:{context['trial']}",
            *build_live_args(args, preset),
            "--",
            *harness_command,
        ]
    else:
        raise SystemExit(f"unsupported variant mode for {context['variant']!r}: {mode!r}")

    capture_cmd = [
        sys.executable,
        str(CAPTURE),
        "--out-dir",
        str(context["capture_dir"]),
        "--instance-id",
        str(context["instance_id"]),
        "--repo-dir",
        str(context["repo_dir"]),
        "--raw-agent-output",
        str(context["raw_agent_output"]),
        "--endpoint-result",
        str(context["endpoint_result"]),
        "--raw-format",
        args.raw_format,
        "--source",
        args.source,
        "--scheduler-variant",
        str(context["variant"]),
        "--work-dir",
        str(context["work_dir"]),
    ]
    if args.system_profile:
        capture_cmd.append("--system-profile")
        capture_cmd.extend(["--system-profile-interval-ms", str(args.system_profile_interval_ms)])
        if args.system_profile_stage_state_template:
            capture_cmd.extend(
                [
                    "--system-profile-stage-state-file",
                    format_template(args.system_profile_stage_state_template, context),
                ]
            )
    for key, template in parse_env_templates(args.harness_env).items():
        capture_cmd.extend(["--env", f"{key}={format_template(template, context)}"])
    if args.allow_command_failure:
        capture_cmd.append("--allow-command-failure")
    capture_cmd.extend(["--", *wrapped_command])
    return {
        "mode": mode,
        "harness_command": harness_command,
        "wrapped_command": wrapped_command,
        "capture_command": capture_cmd,
    }


def shell_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def normalize_endpoint_file(endpoint_path: pathlib.Path, *, args: argparse.Namespace, variant: str) -> dict[str, Any]:
    data, input_kind, _rows = read_json_or_jsonl(endpoint_path)
    ns = SimpleNamespace(
        format="auto",
        source=args.source,
        instance_id=args.instance_id,
        scheduler_variant=variant,
        created_at=args.created_at,
    )
    return normalize_endpoint(data, input_kind, ns, endpoint_path)


def read_capture_report(capture_dir: pathlib.Path) -> dict[str, Any]:
    path = capture_dir / "capture-report.json"
    if not path.exists():
        return {"status": "missing", "blockers": [f"capture report missing: {path}"]}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"status": "fail", "blockers": [f"invalid capture report JSON: {path}: {exc}"]}
    return payload if isinstance(payload, dict) else {"status": "fail", "blockers": [f"capture report is not an object: {path}"]}


def summarize_trials(plan: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    blockers: list[str] = []
    warnings: list[str] = []
    for item in plan:
        context = item["context"]
        capture_dir = pathlib.Path(context["capture_dir"])
        report = read_capture_report(capture_dir)
        artifacts = report.get("artifacts") if isinstance(report.get("artifacts"), dict) else {}
        endpoint_path_text = str(artifacts.get("full_harness_result") or "")
        endpoint: dict[str, Any] = {}
        if endpoint_path_text:
            endpoint_path = pathlib.Path(endpoint_path_text)
            if endpoint_path.exists():
                try:
                    endpoint = normalize_endpoint_file(endpoint_path, args=args, variant=context["variant"])
                    normalized_path = capture_dir / "normalized-endpoint.json"
                    normalized_path.write_text(
                        json.dumps(endpoint, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                except SystemExit as exc:
                    blockers.append(f"{context['variant']} trial {context['trial']}: endpoint normalization failed: {exc}")
            else:
                blockers.append(f"{context['variant']} trial {context['trial']}: endpoint artifact missing: {endpoint_path}")
        else:
            blockers.append(f"{context['variant']} trial {context['trial']}: capture report has no endpoint artifact")
        status = str(report.get("status") or "missing")
        if status != "pass":
            warnings.append(f"{context['variant']} trial {context['trial']}: capture status={status}")
        run_payload = report.get("run") if isinstance(report.get("run"), dict) else {}
        profile_payload = run_payload.get("system_profile") if isinstance(run_payload, dict) and isinstance(run_payload.get("system_profile"), dict) else {}
        trials.append(
            {
                "variant": context["variant"],
                "trial": context["trial"],
                "status": status,
                "capture_dir": context["capture_dir"],
                "live_dir": context["live_dir"] if item["command"]["mode"] == "live" else "",
                "raw_agent_output": artifacts.get("raw_agent_output", ""),
                "full_harness_result": endpoint_path_text,
                "system_profile_summary": artifacts.get("system_profile_summary", ""),
                "system_profile_samples": artifacts.get("system_profile_samples", ""),
                "system_profile_markdown": artifacts.get("system_profile_markdown", ""),
                "system_profile_enabled": bool(profile_payload.get("enabled")),
                "system_profile_status": profile_payload.get("profile_level", ""),
                "resolved": endpoint.get("resolved"),
                "elapsed_sec": endpoint.get("elapsed_sec"),
                "run_returncode": run_payload.get("returncode") if isinstance(run_payload, dict) else None,
                "blockers": report.get("blockers", []),
                "warnings": report.get("warnings", []),
            }
        )

    by_variant: dict[str, dict[str, Any]] = {}
    for variant in sorted({row["variant"] for row in trials}):
        rows = [row for row in trials if row["variant"] == variant]
        elapsed = [value for value in (fnum(row.get("elapsed_sec")) for row in rows) if value is not None]
        resolved = [row.get("resolved") for row in rows if row.get("resolved") is not None]
        aggregate: dict[str, Any] = {
            "trials": len(rows),
            "passes": sum(1 for row in rows if row.get("status") == "pass"),
            "endpoint_rows": len(elapsed),
            "system_profile_rows": sum(1 for row in rows if row.get("system_profile_summary")),
            "system_profile_enabled": sum(1 for row in rows if row.get("system_profile_enabled")),
            "resolved_true": sum(1 for value in resolved if value is True),
            "resolved_false": sum(1 for value in resolved if value is False),
            "resolved_unknown": len(rows) - len(resolved),
        }
        if elapsed:
            aggregate.update(
                {
                    "elapsed_sec_mean": statistics.mean(elapsed),
                    "elapsed_sec_min": min(elapsed),
                    "elapsed_sec_max": max(elapsed),
                    "elapsed_sec_std": statistics.pstdev(elapsed) if len(elapsed) > 1 else 0.0,
                }
            )
        by_variant[variant] = aggregate

    baseline = args.baseline_variant
    baseline_elapsed = fnum(by_variant.get(baseline, {}).get("elapsed_sec_mean"))
    comparisons: dict[str, dict[str, Any]] = {}
    for variant, aggregate in by_variant.items():
        variant_elapsed = fnum(aggregate.get("elapsed_sec_mean"))
        row: dict[str, Any] = {"baseline_variant": baseline}
        if baseline_elapsed is not None and baseline_elapsed > 0 and variant_elapsed is not None:
            row["elapsed_delta_sec"] = variant_elapsed - baseline_elapsed
            row["elapsed_delta_pct"] = (variant_elapsed - baseline_elapsed) * 100.0 / baseline_elapsed
            row["speedup_x"] = baseline_elapsed / variant_elapsed if variant_elapsed > 0 else None
        else:
            row["status"] = "partial"
            row["reason"] = "missing comparable elapsed_sec"
        comparisons[variant] = row

    status = "pass" if not blockers and all(row.get("status") == "pass" for row in trials) else "partial" if trials else "generated"
    return {
        "schema": SCHEMA,
        "status": status,
        "instance_id": args.instance_id,
        "baseline_variant": baseline,
        "trials": trials,
        "aggregate": by_variant,
        "comparisons": comparisons,
        "warnings": warnings,
        "blockers": blockers,
        "notes": [
            "This is full-harness endpoint evidence, not replay-only evidence.",
            "Endpoint speedups are only paper claims after frontier, mechanism, baseline, safety and artifact gates are also audited.",
        ],
    }


def write_manifest(out_dir: pathlib.Path, args: argparse.Namespace, plan: list[dict[str, Any]], *, executed: bool, summary: dict[str, Any] | None) -> pathlib.Path:
    manifest = {
        "schema": SCHEMA,
        "status": "executed" if executed else "generated",
        "instance_id": args.instance_id,
        "repo_dir": str(pathlib.Path(args.repo_dir).expanduser().resolve()),
        "variants": parse_variant_list(args.variants),
        "trials": args.trials,
        "baseline_variant": args.baseline_variant,
        "static_baselines": {
            "nice_value": args.nice_value,
            "static_affinity_taskset": static_affinity_taskset(args),
            "static_cgroup_cpu_weight": args.static_cgroup_cpu_weight,
            "static_cgroup_cpu_max": args.static_cgroup_cpu_max,
            "static_cgroup_wrapper": str(pathlib.Path(args.static_cgroup_wrapper).expanduser().resolve()),
            "static_cgroup_sudo": bool(args.static_cgroup_sudo),
        },
        "command_template": args.command_template,
        "raw_agent_output_template": args.raw_agent_output_template,
        "endpoint_result_template": args.endpoint_result_template,
        "system_profile": {
            "enabled": bool(args.system_profile),
            "interval_ms": args.system_profile_interval_ms,
            "stage_state_template": args.system_profile_stage_state_template,
        },
        "runs": [
            {
                "variant": item["context"]["variant"],
                "trial": item["context"]["trial"],
                "mode": item["command"]["mode"],
                "work_dir": item["context"]["work_dir"],
                "capture_dir": item["context"]["capture_dir"],
                "live_dir": item["context"]["live_dir"] if item["command"]["mode"] == "live" else "",
                "raw_agent_output": item["context"]["raw_agent_output"],
                "endpoint_result": item["context"]["endpoint_result"],
                "capture_command": item["command"]["capture_command"],
                "wrapped_command": item["command"]["wrapped_command"],
            }
            for item in plan
        ],
        "summary": summary,
        "notes": [
            "Run run-harness-matrix.sh or pass --execute to execute the matrix.",
            "Each trial is archived by capture_real_agent_run.py in its own capture directory.",
        ],
    }
    path = out_dir / "real-agent-harness-matrix-manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def write_run_script(out_dir: pathlib.Path, plan: list[dict[str, Any]], args: argparse.Namespace) -> pathlib.Path:
    path = out_dir / "run-harness-matrix.sh"
    lines = ["#!/usr/bin/env bash", "set -euo pipefail", ""]
    required_env = [str(item) for item in getattr(args, "require_any_env", []) if str(item)]
    if required_env:
        lines.extend(
            [
                "FLOWCAUSAL_HAS_REQUIRED_ENV=0",
                f"for key in {' '.join(shlex.quote(key) for key in required_env)}; do",
                "  if [[ -n \"${!key:-}\" ]]; then",
                "    FLOWCAUSAL_HAS_REQUIRED_ENV=1",
                "  fi",
                "done",
                "if [[ \"$FLOWCAUSAL_HAS_REQUIRED_ENV\" -ne 1 ]]; then",
                f"  echo 'set at least one API key before running this full-harness matrix: {' '.join(required_env)}' >&2",
                "  exit 2",
                "fi",
                "",
            ]
        )
    for item in plan:
        lines.append(f"mkdir -p {shlex.quote(item['context']['work_dir'])}")
        lines.append(shell_join(item["command"]["capture_command"]))
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    path.chmod(0o755)
    return path


def write_summary_files(out_dir: pathlib.Path, summary: dict[str, Any]) -> None:
    (out_dir / "harness-matrix-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    csv_path = out_dir / "harness-matrix-summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "variant",
                "trials",
                "passes",
                "endpoint_rows",
                "resolved_true",
                "resolved_false",
                "resolved_unknown",
                "elapsed_sec_mean",
                "elapsed_delta_pct",
                "speedup_x",
            ],
        )
        writer.writeheader()
        for variant, aggregate in sorted(summary.get("aggregate", {}).items()):
            comparison = summary.get("comparisons", {}).get(variant, {})
            writer.writerow(
                {
                    "variant": variant,
                    "trials": aggregate.get("trials", 0),
                    "passes": aggregate.get("passes", 0),
                    "endpoint_rows": aggregate.get("endpoint_rows", 0),
                    "resolved_true": aggregate.get("resolved_true", 0),
                    "resolved_false": aggregate.get("resolved_false", 0),
                    "resolved_unknown": aggregate.get("resolved_unknown", 0),
                    "elapsed_sec_mean": aggregate.get("elapsed_sec_mean", ""),
                    "elapsed_delta_pct": comparison.get("elapsed_delta_pct", ""),
                    "speedup_x": comparison.get("speedup_x", ""),
                }
            )
    md_lines = [
        "# Real-agent harness matrix summary",
        "",
        f"Status: `{summary.get('status')}`",
        "",
        "| variant | trials | passes | resolved T/F/? | mean elapsed (s) | delta vs baseline | speedup |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for variant, aggregate in sorted(summary.get("aggregate", {}).items()):
        comparison = summary.get("comparisons", {}).get(variant, {})
        md_lines.append(
            "| {variant} | {trials} | {passes} | {rt}/{rf}/{ru} | {elapsed} | {delta} | {speedup} |".format(
                variant=variant,
                trials=aggregate.get("trials", 0),
                passes=aggregate.get("passes", 0),
                rt=aggregate.get("resolved_true", 0),
                rf=aggregate.get("resolved_false", 0),
                ru=aggregate.get("resolved_unknown", 0),
                elapsed=f"{aggregate.get('elapsed_sec_mean'):.3f}" if isinstance(aggregate.get("elapsed_sec_mean"), (int, float)) else "",
                delta=f"{comparison.get('elapsed_delta_pct'):.1f}%" if isinstance(comparison.get("elapsed_delta_pct"), (int, float)) else "",
                speedup=f"{comparison.get('speedup_x'):.3f}x" if isinstance(comparison.get("speedup_x"), (int, float)) else "",
            )
        )
    if summary.get("blockers"):
        md_lines.extend(["", "## Blockers", ""])
        md_lines.extend(f"- {item}" for item in summary["blockers"])
    if summary.get("warnings"):
        md_lines.extend(["", "## Warnings", ""])
        md_lines.extend(f"- {item}" for item in summary["warnings"])
    (out_dir / "harness-matrix-summary.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate or execute a full SWE-agent/OpenHands/SWE-bench harness endpoint matrix",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--command-template", required=True, help="harness command with placeholders such as {instance_id}, {raw_agent_output}, {endpoint_result}")
    parser.add_argument("--raw-agent-output-template", default="{work_dir}/agent.traj")
    parser.add_argument("--endpoint-result-template", default="{work_dir}/endpoint.json")
    parser.add_argument("--variant", dest="variants", action="append", default=[], help="variant name or comma-separated list; presets: " + ", ".join(sorted(VARIANT_PRESETS)))
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--baseline-variant", default="default")
    parser.add_argument("--raw-format", default="auto", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="real-agent-harness-matrix")
    parser.add_argument("--taskset", default="", help="optional CPU list applied to direct and live variants")
    parser.add_argument("--victim-command", default="")
    parser.add_argument("--victim-taskset", default="")
    parser.add_argument("--direct-prefix", action="append", default=[], help="shell prefix prepended to direct-mode variants, e.g. 'nice -n 5'")
    parser.add_argument("--nice-value", type=int, default=-5, help="nice adjustment for the nice_priority baseline; negative values may require privilege")
    parser.add_argument("--static-affinity-taskset", default="", help="CPU list for static_affinity; default uses the first CPU from --taskset")
    parser.add_argument("--static-cgroup-cpu-weight", type=int, default=10000, help="cpu.weight for static_cgroup; 0 leaves kernel default")
    parser.add_argument("--static-cgroup-cpu-max", default="", help="optional cpu.max for static_cgroup, e.g. '50000 100000'")
    parser.add_argument("--static-cgroup-wrapper", default=str(RUN_STATIC_CGROUP), help="wrapper script for the static_cgroup baseline")
    parser.add_argument("--static-cgroup-sudo", action="store_true", help="run static_cgroup wrapper through sudo -n")
    parser.add_argument("--live-arg", action="append", default=[], help="extra shlex-parsed argument string appended to every live-mode run")
    parser.add_argument("--live-no-build-tools", action="store_true", help="append --no-build-tools to live-mode runs")
    parser.add_argument("--no-characterization", dest="collect_characterization", action="store_false", help="do not add --collect-characterization to live-mode runs")
    parser.set_defaults(collect_characterization=True)
    parser.add_argument("--harness-env", action="append", default=[], help="extra KEY=VALUE template passed to capture_real_agent_run.py --env")
    parser.add_argument("--system-profile", action="store_true", help="ask capture_real_agent_run.py to record a process-tree system profile for every full-harness trial")
    parser.add_argument("--system-profile-interval-ms", type=int, default=25, help="sampling interval passed to capture_real_agent_run.py --system-profile")
    parser.add_argument("--system-profile-stage-state-template", default="", help="optional template passed to capture_real_agent_run.py --system-profile-stage-state-file")
    parser.add_argument("--require-any-env", action="append", default=[], help="generated run-harness-matrix.sh exits unless at least one listed env var is set")
    parser.add_argument("--allow-command-failure", action="store_true")
    parser.add_argument("--execute", action="store_true", help="actually execute the matrix; default only writes manifest and run script")
    parser.add_argument("--created-at", default="", help="deterministic timestamp for normalized endpoint summaries in tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.trials <= 0:
        raise SystemExit("--trials must be positive")
    variants = parse_variant_list(args.variants)
    unknown = [variant for variant in variants if variant not in VARIANT_PRESETS]
    if unknown:
        raise SystemExit(f"unknown variant(s): {', '.join(unknown)}; known: {', '.join(sorted(VARIANT_PRESETS))}")

    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    plan: list[dict[str, Any]] = []
    for variant in variants:
        preset = VARIANT_PRESETS[variant]
        for trial in range(1, args.trials + 1):
            context = trial_context(args, out_dir=out_dir, variant=variant, trial=trial)
            pathlib.Path(context["work_dir"]).mkdir(parents=True, exist_ok=True)
            command = build_trial_command(args, context=context, preset=preset)
            plan.append({"context": context, "preset": preset, "command": command})

    script_path = write_run_script(out_dir, plan, args)
    summary: dict[str, Any] | None = None
    if args.execute:
        for item in plan:
            proc = subprocess.run(item["command"]["capture_command"], check=False)
            if proc.returncode != 0 and not args.allow_command_failure:
                # Continue to summarize already-written reports, but preserve
                # the failure in the final return code.
                item["execute_returncode"] = proc.returncode
            else:
                item["execute_returncode"] = proc.returncode
        summary = summarize_trials(plan, args)
        for item in plan:
            if item.get("execute_returncode") not in (None, 0):
                summary.setdefault("blockers", []).append(
                    f"{item['context']['variant']} trial {item['context']['trial']}: capture command exited {item['execute_returncode']}"
                )
                summary["status"] = "partial"
        write_summary_files(out_dir, summary)
    manifest_path = write_manifest(out_dir, args, plan, executed=args.execute, summary=summary)
    print(
        json.dumps(
            {
                "status": "executed" if args.execute else "generated",
                "manifest": str(manifest_path),
                "run_script": str(script_path),
                "summary": str(out_dir / "harness-matrix-summary.json") if args.execute else "",
            },
            sort_keys=True,
        )
    )
    if args.execute and summary and summary.get("blockers"):
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

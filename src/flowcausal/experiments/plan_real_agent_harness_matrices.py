#!/usr/bin/env python3
"""Generate full-harness endpoint matrix runbooks from a real-agent plan.

``plan_real_agent_instances.py`` selects SWE-bench instances and emits the
single-run capture commands.  For EuroSys evidence we also need to run the
*complete* harness under Linux/default and FlowCausal scheduler variants.  This
helper bridges those two artifacts: it reads the selected instances, reuses the
mini-swe-agent command options, and writes one ``run_real_agent_harness_matrix``
command per instance.

By default it only generates a runbook/script.  It does not spend API budget and
does not create endpoint evidence.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
from typing import Any

from validate_bundle_collection_manifest import safe_name


HERE = pathlib.Path(__file__).resolve().parent
MATRIX = HERE / "run_real_agent_harness_matrix.py"
MINI_WRAPPER = HERE / "run_mini_swebench_single_capture.py"
SCHEMA = "flowcausal.real_agent_harness_matrix_plan.v1"
PLAN_SCHEMA = "flowcausal.real_agent_instance_plan.v1"
DEFAULT_API_ENVS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")
DEFAULT_VARIANTS = (
    "default",
    "nice_priority",
    "static_affinity",
    "static_cgroup",
    "default_observed",
    "flowcausal_no_width",
    "flowcausal_width",
)


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"plan is not a JSON object: {path}")
    return payload


def command_arg(cmd: list[str], name: str, default: str = "") -> str:
    try:
        index = cmd.index(name)
    except ValueError:
        return default
    if index + 1 >= len(cmd):
        return default
    return str(cmd[index + 1])


def command_has(cmd: list[str], name: str) -> bool:
    return name in cmd


def command_values(cmd: list[str], name: str) -> list[str]:
    out: list[str] = []
    for index, item in enumerate(cmd):
        if item == name and index + 1 < len(cmd):
            out.append(str(cmd[index + 1]))
    return out


def shell_join(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def path_from(value: str, *, root: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def mini_options_from_capture_command(cmd: list[str]) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    if not any(part.endswith("run_mini_swebench_single_capture.py") for part in cmd):
        blockers.append("capture command does not invoke run_mini_swebench_single_capture.py")
    opts = {
        "subset": command_arg(cmd, "--subset", "lite"),
        "split": command_arg(cmd, "--split", "test"),
        "cost_limit": command_arg(cmd, "--cost-limit", "3.0"),
        "mini_extra": command_arg(cmd, "--mini-extra"),
        "model": command_arg(cmd, "--model"),
        "configs": command_values(cmd, "--config"),
        "yolo": command_has(cmd, "--yolo"),
        "allow_command_failure": command_has(cmd, "--allow-command-failure"),
    }
    if not opts["mini_extra"]:
        blockers.append("capture command missing --mini-extra")
    return opts, blockers


def mini_command_template(opts: dict[str, Any]) -> str:
    cmd = [
        "python3",
        str(MINI_WRAPPER),
        "--instance-id",
        "{instance_id}",
        "--trajectory",
        "{raw_agent_output}",
        "--endpoint",
        "{endpoint_result}",
        "--subset",
        str(opts.get("subset") or "lite"),
        "--split",
        str(opts.get("split") or "test"),
        "--cost-limit",
        str(opts.get("cost_limit") or "3.0"),
        "--mini-extra",
        str(opts.get("mini_extra") or "mini-extra"),
    ]
    if opts.get("model"):
        cmd.extend(["--model", str(opts["model"])])
    for item in as_list(opts.get("configs")):
        cmd.extend(["--config", str(item)])
    if opts.get("yolo"):
        cmd.append("--yolo")
    if opts.get("allow_command_failure"):
        cmd.append("--allow-command-failure")
    return shell_join(cmd)


def matrix_command(
    row: dict[str, Any],
    args: argparse.Namespace,
    *,
    root: pathlib.Path,
    matrix_root: pathlib.Path,
    required_env: list[str],
) -> tuple[list[str], dict[str, Any], list[str]]:
    instance_id = str(row.get("instance_id") or "")
    repo_dir = str(row.get("repo_dir") or "")
    cmd = [str(item) for item in as_list(row.get("capture_command"))]
    blockers: list[str] = []
    if not instance_id:
        blockers.append("missing instance_id")
    if not repo_dir:
        blockers.append("missing repo_dir")
    else:
        resolved_repo = path_from(repo_dir, root=root)
        if not resolved_repo.is_dir():
            blockers.append(f"repo_dir missing: {resolved_repo}")
    opts, opt_blockers = mini_options_from_capture_command(cmd)
    blockers.extend(opt_blockers)
    if opts.get("mini_extra"):
        mini_extra = path_from(str(opts["mini_extra"]), root=root)
        if not mini_extra.exists():
            blockers.append(f"mini-extra missing: {mini_extra}")
        opts["mini_extra"] = str(mini_extra)
    safe = safe_name(instance_id)
    out_dir = matrix_root / safe
    variants = ",".join(args.variant)
    command = [
        "python3",
        str(MATRIX),
        "--out-dir",
        str(out_dir),
        "--instance-id",
        instance_id,
        "--repo-dir",
        str(path_from(repo_dir, root=root)) if repo_dir else "",
        "--variant",
        variants,
        "--trials",
        str(args.trials),
        "--baseline-variant",
        args.baseline_variant,
        "--raw-format",
        args.raw_format,
        "--source",
        args.source,
        "--taskset",
        args.taskset,
        "--nice-value",
        str(args.nice_value),
        "--static-affinity-taskset",
        args.static_affinity_taskset,
        "--static-cgroup-cpu-weight",
        str(args.static_cgroup_cpu_weight),
        "--command-template",
        mini_command_template(opts),
    ]
    if args.static_cgroup_cpu_max:
        command.extend(["--static-cgroup-cpu-max", args.static_cgroup_cpu_max])
    if args.static_cgroup_sudo:
        command.append("--static-cgroup-sudo")
    if args.execute:
        command.append("--execute")
    if args.allow_command_failure:
        command.append("--allow-command-failure")
    if args.victim_command:
        command.extend(["--victim-command", args.victim_command])
    if args.victim_taskset:
        command.extend(["--victim-taskset", args.victim_taskset])
    for key in required_env:
        command.extend(["--require-any-env", key])
    return command, {"matrix_out_dir": str(out_dir), "mini_options": opts}, blockers


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    root = pathlib.Path(args.root).expanduser().resolve()
    input_plan_path = path_from(args.instance_plan, root=root)
    input_plan = read_json(input_plan_path)
    blockers: list[str] = []
    warnings: list[str] = []
    if input_plan.get("schema") != PLAN_SCHEMA:
        blockers.append(f"unexpected instance plan schema: {input_plan.get('schema')}")
    selected = [row for row in as_list(input_plan.get("selected")) if isinstance(row, dict)]
    if len(selected) < args.min_instances:
        blockers.append(f"selected instances {len(selected)} < required {args.min_instances}")
    matrix_root = path_from(args.matrix_root, root=root)
    run_env = as_dict(input_plan.get("run_environment"))
    env_script = str(args.env_script or run_env.get("env_script") or "")
    required_env = list(args.require_any_env or []) or [str(item) for item in as_list(run_env.get("require_any_env")) if str(item)] or list(DEFAULT_API_ENVS)
    rows: list[dict[str, Any]] = []
    for row in selected:
        cmd, metadata, row_blockers = matrix_command(row, args, root=root, matrix_root=matrix_root, required_env=required_env)
        instance_id = str(row.get("instance_id") or "")
        rows.append(
            {
                "instance_id": instance_id,
                "repo_dir": str(row.get("repo_dir") or ""),
                "status": "pass" if not row_blockers else "fail",
                "matrix_command": cmd,
                "matrix_command_shell": shell_join(cmd),
                "matrix_out_dir": metadata.get("matrix_out_dir", ""),
                "mini_options": metadata.get("mini_options", {}),
                "blockers": row_blockers,
            }
        )
        blockers.extend(f"{instance_id or '<unknown>'}: {item}" for item in row_blockers)
    status = "pass" if not blockers else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "instance_plan": str(input_plan_path),
        "matrix_root": str(matrix_root),
        "summary": {
            "selected_instances": len(selected),
            "row_pass": sum(1 for row in rows if row["status"] == "pass"),
            "row_fail": sum(1 for row in rows if row["status"] == "fail"),
            "trials": args.trials,
            "variants": args.variant,
            "execute": args.execute,
        },
        "run_environment": {
            "env_script": env_script,
            "require_any_env": required_env,
        },
        "rows": rows,
        "warnings": warnings,
        "blockers": blockers,
        "notes": [
            "generated matrix commands do not run unless the generated script is executed",
            "full-harness matrix summaries still require subsequent audit_real_agent_harness_matrix.py and bundle collection audit",
        ],
    }


def markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal real-agent full-harness matrix plan",
        "",
        f"Status: `{plan.get('status')}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in as_dict(plan.get("summary")).items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Matrix rows", "", "| Instance | Status | Matrix dir |", "| --- | --- | --- |"])
    for row in as_list(plan.get("rows")):
        if not isinstance(row, dict):
            continue
        lines.append(f"| `{row.get('instance_id', '')}` | `{row.get('status', '')}` | `{row.get('matrix_out_dir', '')}` |")
    lines.extend(["", "## Commands", ""])
    for row in as_list(plan.get("rows")):
        if not isinstance(row, dict):
            continue
        lines.append(f"### {row.get('instance_id', '')}")
        lines.append("")
        lines.append("```bash")
        lines.append(str(row.get("matrix_command_shell", "")))
        lines.append("```")
        lines.append("")
    if plan.get("blockers"):
        lines.extend(["## Blockers", ""])
        for blocker in as_list(plan.get("blockers")):
            lines.append(f"- {blocker}")
    return "\n".join(lines) + "\n"


def shell_script(plan: dict[str, Any], *, root: pathlib.Path) -> str:
    run_env = as_dict(plan.get("run_environment"))
    summary = as_dict(plan.get("summary"))
    execute = bool(summary.get("execute"))
    env_script = str(run_env.get("env_script") or "")
    required_env = [str(item) for item in as_list(run_env.get("require_any_env")) if str(item)]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(root))}",
        "",
        "# Generated by plan_real_agent_harness_matrices.py.",
        "# This script generates full-harness endpoint matrix runbooks.",
        "# It spends API budget only when the plan was generated with --execute.",
        "",
    ]
    if env_script:
        lines.extend(
            [
                f"if [[ ! -f {shlex.quote(env_script)} ]]; then",
                f"  echo 'missing environment script: {env_script}' >&2",
                "  exit 2",
                "fi",
                f"source {shlex.quote(env_script)}",
                "",
            ]
        )
    if required_env and execute:
        lines.extend(
            [
                "FLOWCAUSAL_HAS_REQUIRED_ENV=0",
                f"for key in {' '.join(shlex.quote(key) for key in required_env)}; do",
                "  if [[ -n \"${!key:-}\" ]]; then",
                "    FLOWCAUSAL_HAS_REQUIRED_ENV=1",
                "  fi",
                "done",
                "if [[ \"$FLOWCAUSAL_HAS_REQUIRED_ENV\" -ne 1 ]]; then",
                f"  echo 'set at least one API key before full-harness matrices: {' '.join(required_env)}' >&2",
                "  exit 2",
                "fi",
                "",
            ]
        )
    elif required_env:
        lines.extend(
            [
                "# API-key guard skipped because this plan has execute=false.",
                "# The per-instance run-harness-matrix.sh scripts will spend API budget when executed.",
                "",
            ]
        )
    lines.append(f"mkdir -p {shlex.quote(str(plan.get('matrix_root', '')))}")
    lines.append("")
    for row in as_list(plan.get("rows")):
        if not isinstance(row, dict):
            continue
        lines.append(f"# {row.get('instance_id', '')}")
        lines.append(str(row.get("matrix_command_shell", "")))
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate full-harness matrix commands from FlowCausal real-agent instance plan",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--instance-plan", required=True)
    parser.add_argument("--matrix-root", default="flowcausal/experiments/results/real-agent-captures/full-harness-matrices")
    parser.add_argument("--root", default=".")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--variant", action="append", default=[], help="variant or comma-separated variants")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--baseline-variant", default="default")
    parser.add_argument("--taskset", default="0-1")
    parser.add_argument("--nice-value", type=int, default=-5)
    parser.add_argument("--static-affinity-taskset", default="", help="default uses the first CPU from --taskset inside run_real_agent_harness_matrix.py")
    parser.add_argument("--static-cgroup-cpu-weight", type=int, default=10000)
    parser.add_argument("--static-cgroup-cpu-max", default="")
    parser.add_argument("--static-cgroup-sudo", action="store_true")
    parser.add_argument("--raw-format", default="swe-agent", choices=("auto", "swe-agent", "openhands", "flowcausal"))
    parser.add_argument("--source", default="mini-swe-agent-full-harness-matrix")
    parser.add_argument("--execute", action="store_true", help="include --execute in generated matrix commands")
    parser.add_argument("--allow-command-failure", action="store_true")
    parser.add_argument("--victim-command", default="")
    parser.add_argument("--victim-taskset", default="")
    parser.add_argument("--env-script", default="")
    parser.add_argument("--require-any-env", action="append", default=[])
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    parser.add_argument("--script-output")
    args = parser.parse_args()
    variants: list[str] = []
    for value in args.variant or []:
        variants.extend(item.strip() for item in value.split(",") if item.strip())
    args.variant = variants or list(DEFAULT_VARIANTS)
    return args


def main() -> int:
    args = parse_args()
    root = pathlib.Path(args.root).expanduser().resolve()
    plan = build_plan(args)
    output = pathlib.Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        path = pathlib.Path(args.markdown_output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(markdown(plan), encoding="utf-8")
    if args.script_output:
        path = pathlib.Path(args.script_output).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(shell_script(plan, root=root), encoding="utf-8")
        path.chmod(path.stat().st_mode | 0o111)
    print(json.dumps({"status": plan["status"], "rows": plan["summary"]["selected_instances"], "output": str(output)}, sort_keys=True))
    return 0 if plan["status"] == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

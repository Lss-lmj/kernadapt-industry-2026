#!/usr/bin/env python3
"""Preflight a generated real-agent capture plan before spending API budget.

This checker validates the *launch surface* for FlowCausal's real sandbox/code
agent experiments.  It intentionally does not run SWE-bench or claim endpoint
evidence.  Instead it catches the expensive, boring failures early:

* selected repos are present;
* the generated capture commands reference the expected wrappers;
* mini-swe-agent plans point at an executable ``mini-extra``;
* the generated shell script exists, is executable, and contains the API-key
  guard requested by the plan.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
from typing import Any


SCHEMA = "flowcausal.real_agent_capture_plan_preflight.v1"
PLAN_SCHEMA = "flowcausal.real_agent_instance_plan.v1"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"cannot read JSON plan {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"plan must be a JSON object: {path}")
    return data


def command_arg(cmd: list[str], name: str) -> str:
    try:
        index = cmd.index(name)
    except ValueError:
        return ""
    if index + 1 >= len(cmd):
        return ""
    return str(cmd[index + 1])


def path_from(value: str, *, base: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def executable(path: pathlib.Path) -> bool:
    return path.exists() and path.is_file() and os.access(path, os.X_OK)


def inspect_row(row: dict[str, Any], *, root: pathlib.Path, mode: str) -> dict[str, Any]:
    instance_id = str(row.get("instance_id") or "")
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []
    cmd = [str(item) for item in as_list(row.get("capture_command"))]
    if not instance_id:
        blockers.append("missing instance_id")
    if not cmd:
        blockers.append("missing capture_command")
    elif not any(part.endswith("capture_real_agent_run.py") for part in cmd):
        blockers.append("capture_command does not invoke capture_real_agent_run.py")
    else:
        evidence.append("capture_real_agent_run.py command present")

    repo_dir_text = str(row.get("repo_dir") or command_arg(cmd, "--repo-dir"))
    repo_dir = path_from(repo_dir_text, base=root) if repo_dir_text else pathlib.Path("")
    if not repo_dir_text:
        blockers.append("missing repo_dir")
    elif not repo_dir.is_dir():
        blockers.append(f"repo_dir missing: {repo_dir}")
    else:
        evidence.append(f"repo_dir exists: {repo_dir}")

    raw_output = command_arg(cmd, "--raw-agent-output")
    endpoint = command_arg(cmd, "--endpoint-result")
    if not raw_output:
        blockers.append("capture_command missing --raw-agent-output")
    else:
        evidence.append(f"raw output target configured: {raw_output}")
    if not endpoint:
        blockers.append("capture_command missing --endpoint-result")
    else:
        evidence.append(f"endpoint target configured: {endpoint}")

    if mode == "mini-swe-agent":
        if not any(part.endswith("run_mini_swebench_single_capture.py") for part in cmd):
            blockers.append("mini-swe-agent mode command does not invoke run_mini_swebench_single_capture.py")
        else:
            evidence.append("run_mini_swebench_single_capture.py command present")
        mini_extra_text = command_arg(cmd, "--mini-extra")
        if not mini_extra_text:
            blockers.append("mini-swe-agent mode command missing --mini-extra")
        else:
            mini_extra = path_from(mini_extra_text, base=root)
            if not executable(mini_extra):
                blockers.append(f"mini-extra is not executable: {mini_extra}")
            else:
                evidence.append(f"mini-extra executable: {mini_extra}")
    elif mode and mode != "custom":
        warnings.append(f"unknown run_environment mode: {mode}")

    status = "pass" if not blockers else "fail"
    return {
        "instance_id": instance_id,
        "status": status,
        "blockers": blockers,
        "warnings": warnings,
        "evidence": evidence,
    }


def inspect_script(path: pathlib.Path | None, *, root: pathlib.Path, env_script: str, required_env: list[str]) -> dict[str, Any]:
    if path is None:
        return {"status": "skipped", "evidence": ["no script path supplied"], "blockers": [], "warnings": []}
    script = path_from(str(path), base=root)
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = []
    if not script.exists():
        return {"status": "fail", "script": str(script), "evidence": [], "blockers": [f"script missing: {script}"], "warnings": []}
    if not os.access(script, os.X_OK):
        blockers.append(f"script is not executable: {script}")
    else:
        evidence.append(f"script executable: {script}")
    text = script.read_text(encoding="utf-8", errors="replace")
    if env_script:
        if env_script in text:
            evidence.append(f"script sources env_script: {env_script}")
        else:
            blockers.append(f"script does not source env_script: {env_script}")
    if required_env:
        missing_keys = [key for key in required_env if key not in text]
        if "FLOWCAUSAL_HAS_REQUIRED_ENV" not in text:
            blockers.append("script missing API-key guard variable FLOWCAUSAL_HAS_REQUIRED_ENV")
        if missing_keys:
            blockers.append("script API-key guard missing env var(s): " + ", ".join(missing_keys))
        if not missing_keys and "FLOWCAUSAL_HAS_REQUIRED_ENV" in text:
            evidence.append("script contains API-key guard for: " + ", ".join(required_env))
    elif "API_KEY" not in text:
        warnings.append("no required API-key envs declared and no API_KEY string found in script")
    return {
        "status": "pass" if not blockers else "fail",
        "script": str(script),
        "evidence": evidence,
        "blockers": blockers,
        "warnings": warnings,
    }


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    root = pathlib.Path(args.root).expanduser().resolve()
    plan_path = path_from(args.plan, base=root)
    plan = read_json(plan_path)
    blockers: list[str] = []
    warnings: list[str] = []
    evidence: list[str] = [f"plan={plan_path}"]

    if plan.get("schema") != PLAN_SCHEMA:
        blockers.append(f"unexpected plan schema: {plan.get('schema')}")
    selected = [row for row in as_list(plan.get("selected")) if isinstance(row, dict)]
    min_instances = int(args.min_instances)
    if len(selected) < min_instances:
        blockers.append(f"selected instances {len(selected)} < required {min_instances}")
    else:
        evidence.append(f"selected_instances={len(selected)}")

    run_env = as_dict(plan.get("run_environment"))
    mode = str(run_env.get("mode") or "")
    env_script = str(run_env.get("env_script") or "")
    required_env = [str(item) for item in as_list(run_env.get("require_any_env")) if str(item)]
    if env_script:
        env_path = path_from(env_script, base=root)
        if env_path.exists():
            evidence.append(f"env_script exists: {env_path}")
        else:
            blockers.append(f"env_script missing: {env_path}")

    rows = [inspect_row(row, root=root, mode=mode) for row in selected]
    for row in rows:
        blockers.extend(f"{row.get('instance_id')}: {item}" for item in as_list(row.get("blockers")))
        warnings.extend(f"{row.get('instance_id')}: {item}" for item in as_list(row.get("warnings")))

    script_report = inspect_script(pathlib.Path(args.script) if args.script else None, root=root, env_script=env_script, required_env=required_env)
    blockers.extend(str(item) for item in as_list(script_report.get("blockers")))
    warnings.extend(str(item) for item in as_list(script_report.get("warnings")))

    status = "pass" if not blockers else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "plan": str(plan_path),
        "script": str(path_from(args.script, base=root)) if args.script else "",
        "summary": {
            "selected_instances": len(selected),
            "min_instances": min_instances,
            "row_pass": sum(1 for row in rows if row.get("status") == "pass"),
            "row_fail": sum(1 for row in rows if row.get("status") == "fail"),
            "mode": mode,
        },
        "evidence": evidence,
        "rows": rows,
        "script_check": script_report,
        "blockers": blockers,
        "warnings": warnings,
        "notes": [
            "capture-plan preflight validates launch artifacts only",
            "it does not run a code agent and cannot support endpoint-improvement claims",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal real-agent capture-plan preflight",
        "",
        f"Status: `{report.get('status')}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key, value in as_dict(report.get("summary")).items():
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Rows", "", "| Instance | Status | Evidence |", "| --- | --- | --- |"])
    for row in as_list(report.get("rows")):
        if not isinstance(row, dict):
            continue
        lines.append(
            f"| `{row.get('instance_id', '')}` | `{row.get('status', '')}` | {'; '.join(str(item) for item in as_list(row.get('evidence'))[:4])} |"
        )
    script = as_dict(report.get("script_check"))
    lines.extend(["", "## Script check", "", f"Status: `{script.get('status', '')}`", ""])
    for item in as_list(script.get("evidence")):
        lines.append(f"- {item}")
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in as_list(report.get("blockers")):
            lines.append(f"- {blocker}")
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in as_list(report.get("warnings")):
            lines.append(f"- {warning}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Preflight a generated FlowCausal real-agent capture plan without running the agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--plan", required=True, help="instance-plan JSON from plan_real_agent_instances.py")
    parser.add_argument("--script", help="generated capture shell script to inspect")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--root", default=".", help="repository/workspace root for relative paths")
    parser.add_argument("--output", help="write JSON report")
    parser.add_argument("--markdown-output", help="write Markdown report")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = summarize(args)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        output = pathlib.Path(args.output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    if args.markdown_output:
        output = pathlib.Path(args.markdown_output).expanduser().resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown(report), encoding="utf-8")
    print(text, end="")
    return 0 if report.get("status") == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

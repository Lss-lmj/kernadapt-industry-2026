#!/usr/bin/env python3
"""Check whether this machine is ready to collect real agent workloads.

FlowCausal's EuroSys evidence path now has capture/intake/preflight/audit tools,
but a different question remains: can this host actually start collecting
SWE-agent/OpenHands/SWE-bench instances today?

This checker audits the local environment without installing anything:

* available harness commands / Python modules;
* Docker availability, unless skipped;
* API-key environment variables, unless skipped;
* existing capture manifests and captured instance count.

It deliberately separates ``run_readiness_status`` from
``evidence_capture_status``.  A machine can be ready to run while still having
zero paper evidence.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any


SCHEMA = "flowcausal.real_agent_harness_readiness.v1"
DEFAULT_FRAMEWORK_COMMANDS = ("sweagent", "swe-agent", "mini-swe-agent", "mini-extra", "openhands")
DEFAULT_FRAMEWORK_MODULES = ("swebench", "sweagent", "swe_agent", "mini_swe_agent", "openhands")
DEFAULT_API_ENVS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def run_short(cmd: list[str], *, env: dict[str, str], timeout: float = 5.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip()[:500],
            "stderr": proc.stderr.strip()[:500],
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": str(exc)}


def command_checks(names: list[str], *, path: str) -> list[dict[str, Any]]:
    env = os.environ.copy()
    if path:
        env["PATH"] = path
    out: list[dict[str, Any]] = []
    for name in names:
        resolved = shutil.which(name, path=path or None)
        row: dict[str, Any] = {
            "name": name,
            "available": bool(resolved),
            "path": resolved or "",
        }
        if resolved:
            version = run_short([resolved, "--version"], env=env, timeout=3.0)
            row["version_probe"] = version
        out.append(row)
    return out


def required_command_stage(names: list[str], *, path: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not names:
        return stage("required capture commands", "skipped", ["no required capture commands configured"], []), []
    checks = command_checks(names, path=path)
    missing = [str(row.get("name")) for row in checks if not row.get("available")]
    evidence = [f"{row.get('name')}={row.get('path')}" for row in checks if row.get("available")]
    blockers = [f"required capture command not found: {name}" for name in missing]
    return (
        stage(
            "required capture commands",
            "pass" if not blockers else "fail",
            evidence,
            blockers,
            ["install the missing command or pass its exact path with --required-command"] if blockers else [],
        ),
        checks,
    )


def module_checks(names: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in names:
        spec = importlib.util.find_spec(name)
        out.append(
            {
                "name": name,
                "available": spec is not None,
                "origin": getattr(spec, "origin", "") if spec is not None else "",
            }
        )
    return out


def docker_check(*, path: str, skip: bool) -> dict[str, Any]:
    if skip:
        return {"status": "skipped", "evidence": ["docker check skipped"], "blockers": []}
    env = os.environ.copy()
    if path:
        env["PATH"] = path
    docker = shutil.which("docker", path=path or None)
    if not docker:
        return {"status": "fail", "evidence": [], "blockers": ["docker command not found"]}
    version = run_short([docker, "--version"], env=env, timeout=3.0)
    info = run_short([docker, "info"], env=env, timeout=5.0)
    evidence = [f"docker={docker}"]
    if version.get("stdout"):
        evidence.append(str(version.get("stdout")))
    blockers: list[str] = []
    if not version.get("ok"):
        blockers.append("docker --version failed")
    if not info.get("ok"):
        blockers.append("docker info failed; daemon may be unavailable or user lacks permission")
    return {"status": "pass" if not blockers else "fail", "evidence": evidence, "blockers": blockers, "version_probe": version, "info_probe": info}


def api_key_check(names: list[str], *, skip: bool) -> dict[str, Any]:
    if skip:
        return {"status": "skipped", "evidence": ["API-key check skipped"], "blockers": []}
    present = [name for name in names if os.environ.get(name)]
    evidence = [f"{name}=set" for name in present]
    missing = [name for name in names if name not in present]
    status = "pass" if present else "fail"
    blockers = [] if present else [f"none of the API-key environment variables are set: {', '.join(names)}"]
    return {
        "status": status,
        "evidence": evidence,
        "missing": missing,
        "blockers": blockers,
        "notes": ["secret values are intentionally not printed"],
    }


def read_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def capture_check(path: pathlib.Path, *, min_instances: int) -> dict[str, Any]:
    manifest = path / "real-agent-manifest.json"
    if not manifest.exists():
        return {
            "status": "fail",
            "capture_dir": str(path),
            "manifest": str(manifest),
            "captured_instances": 0,
            "evidence": [],
            "blockers": [f"capture manifest not found: {manifest}"],
            "next_actions": ["run capture_real_agent_run.py for at least three real instances"],
        }
    data = read_json(manifest)
    instances = [row for row in as_list(data.get("instances")) if isinstance(row, dict)]
    blockers: list[str] = []
    for row in instances:
        iid = str(row.get("instance_id") or "<unknown>")
        for key in ("raw_agent_output", "repo_dir", "full_harness_result"):
            value = str(row.get(key) or "")
            if not value:
                blockers.append(f"{iid}: missing {key}")
                continue
            candidate = pathlib.Path(value)
            if not candidate.is_absolute():
                candidate = path / candidate
            if not candidate.exists():
                blockers.append(f"{iid}: {key} path does not exist: {candidate}")
    count = len(instances)
    if count < min_instances:
        blockers.append(f"captured manifest instances {count} < required {min_instances}")
    evidence = [f"manifest={manifest}", f"captured_instances={count}"]
    if not blockers:
        status = "pass"
    elif count > 0:
        status = "partial"
    else:
        status = "fail"
    return {
        "status": status,
        "capture_dir": str(path),
        "manifest": str(manifest),
        "captured_instances": count,
        "evidence": evidence,
        "blockers": blockers,
        "instances": [{"instance_id": str(row.get("instance_id") or ""), "row": row} for row in instances],
        "next_actions": [] if status == "pass" else ["capture more real instances and rerun manifest preflight"],
    }


def stage(name: str, status: str, evidence: list[str] | None = None, blockers: list[str] | None = None, next_actions: list[str] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "evidence": evidence or [],
        "blockers": blockers or [],
        "next_actions": next_actions or [],
    }


def summarize(args: argparse.Namespace) -> dict[str, Any]:
    framework_commands = args.framework_command or list(DEFAULT_FRAMEWORK_COMMANDS)
    framework_modules = args.framework_module or list(DEFAULT_FRAMEWORK_MODULES)
    api_envs = args.api_env or list(DEFAULT_API_ENVS)
    path = args.path or os.environ.get("PATH", "")

    commands = command_checks(framework_commands, path=path)
    required_commands_stage, required_commands = required_command_stage(args.required_command or [], path=path)
    modules = module_checks(framework_modules)
    command_hits = [row for row in commands if row.get("available")]
    module_hits = [row for row in modules if row.get("available")]
    framework_blockers: list[str] = []
    framework_evidence: list[str] = []
    if command_hits:
        framework_evidence.append("commands=" + ", ".join(str(row.get("name")) for row in command_hits))
    if module_hits:
        framework_evidence.append("python_modules=" + ", ".join(str(row.get("name")) for row in module_hits))
    if not command_hits and not module_hits:
        framework_blockers.append("no SWE-agent/mini-swe-agent/OpenHands/SWE-bench command or Python module found")
    framework_stage = stage(
        "framework tools",
        "pass" if not framework_blockers else "fail",
        framework_evidence,
        framework_blockers,
        ["install/configure SWE-agent, mini-swe-agent, or OpenHands in an isolated environment"] if framework_blockers else [],
    )

    docker = docker_check(path=path, skip=args.skip_docker_check)
    docker_stage = stage("docker", str(docker.get("status")), as_list(docker.get("evidence")), as_list(docker.get("blockers")))
    api = api_key_check(api_envs, skip=args.skip_api_key_check)
    api_stage = stage("api credentials", str(api.get("status")), as_list(api.get("evidence")), as_list(api.get("blockers")))
    capture = capture_check(pathlib.Path(args.capture_dir).expanduser().resolve(), min_instances=args.min_instances)
    capture_stage = stage(
        "captured real instances",
        str(capture.get("status")),
        as_list(capture.get("evidence")),
        as_list(capture.get("blockers")),
        as_list(capture.get("next_actions")),
    )

    run_required = [framework_stage, required_commands_stage, docker_stage, api_stage]
    run_ready = all(item["status"] in {"pass", "skipped"} for item in run_required)
    evidence_ready = capture_stage["status"] == "pass"
    if run_ready and evidence_ready:
        status = "pass"
    elif run_ready:
        status = "partial"
    else:
        status = "fail"
    next_actions: list[str] = []
    blockers: list[str] = []
    for item in (framework_stage, required_commands_stage, docker_stage, api_stage, capture_stage):
        blockers.extend(item["blockers"])
        next_actions.extend(item["next_actions"])
    if not run_ready:
        next_actions.append("fix environment blockers before attempting the three-instance run")
    elif not evidence_ready:
        next_actions.append("run capture_real_agent_run.py for three selected SWE-bench/SWE-agent/mini-swe-agent/OpenHands instances")

    return {
        "schema": SCHEMA,
        "status": status,
        "run_readiness_status": "pass" if run_ready else "fail",
        "evidence_capture_status": str(capture.get("status")),
        "requirements": {
            "min_instances": args.min_instances,
            "framework_commands": framework_commands,
            "framework_modules": framework_modules,
            "required_commands": args.required_command or [],
            "api_envs": api_envs,
            "docker_required": not args.skip_docker_check,
            "api_key_required": not args.skip_api_key_check,
        },
        "stages": [framework_stage, required_commands_stage, docker_stage, api_stage, capture_stage],
        "checks": {
            "commands": commands,
            "required_commands": required_commands,
            "modules": modules,
            "docker": docker,
            "api_credentials": api,
            "capture": capture,
        },
        "blockers": list(dict.fromkeys(blockers)),
        "next_actions": list(dict.fromkeys(next_actions)),
        "notes": [
            "run_readiness_status only means the host can attempt harness runs",
            "evidence_capture_status only counts archived real-agent artifacts; bundle audits still decide paper readiness",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal real-agent harness readiness",
        "",
        f"Status: `{report.get('status')}`",
        f"Run readiness: `{report.get('run_readiness_status')}`",
        f"Evidence capture: `{report.get('evidence_capture_status')}`",
        "",
        "| Stage | Status | Evidence |",
        "| --- | --- | --- |",
    ]
    for item in as_list(report.get("stages")):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"| {item.get('name', '')} | `{item.get('status', '')}` | {'; '.join(str(x) for x in as_list(item.get('evidence')))} |"
        )
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in as_list(report.get("blockers")):
            lines.append(f"- {blocker}")
    if report.get("next_actions"):
        lines.extend(["", "## Next actions", ""])
        for action in as_list(report.get("next_actions")):
            lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check local readiness for collecting real SWE-agent/mini-swe-agent/OpenHands/SWE-bench FlowCausal evidence",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--capture-dir", default="flowcausal/experiments/results/real-agent-captures")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--framework-command", action="append", help="framework command to look for; repeatable")
    parser.add_argument("--framework-module", action="append", help="Python module to look for; repeatable")
    parser.add_argument("--required-command", action="append", help="command/path that must exist for this capture plan; repeatable")
    parser.add_argument("--api-env", action="append", help="API-key env var; pass if any listed variable is set")
    parser.add_argument("--skip-docker-check", action="store_true")
    parser.add_argument("--skip-api-key-check", action="store_true")
    parser.add_argument("--path", help="PATH override for command checks, useful for reproducible tests")
    parser.add_argument("--output", help="write readiness JSON here")
    parser.add_argument("--markdown-output", help="write readiness markdown here")
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
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

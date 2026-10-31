#!/usr/bin/env python3
"""Generate an isolated bootstrap plan for real-agent harness tools.

FlowCausal's real workload path needs SWE-agent/OpenHands/SWE-bench style
harnesses, but installing those tools globally makes artifacts hard to
reproduce.  This helper writes a small installation bundle:

* ``install-harness.sh``: creates a venv and installs selected tools;
* ``env.sh``: activates the venv and exposes the chosen capture directory;
* ``api-keys.env.template``: names required API-key variables without secrets;
* ``post-install-readiness.sh``: reruns FlowCausal readiness after installation;
* ``bootstrap-report.json``: records commands, versions, and blockers.

The default is generate-only.  Pass ``--execute`` when you intentionally want to
run the generated installer.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import shutil
import subprocess
import sys
from typing import Any


SCHEMA = "flowcausal.real_agent_harness_bootstrap.v1"
DEFAULT_BACKENDS = ("swe-agent-source", "openhands-uv")
DOC_URLS = {
    "swe-agent-source": "https://swe-agent.com/latest/installation/source/",
    "mini-swe-agent": "https://mini-swe-agent.com/latest/",
    "openhands-uv": "https://docs.openhands.dev/openhands/usage/how-to/cli-mode",
    "openhands-pip": "https://docs.openhands.dev/openhands/usage/installation",
    "swebench-pip": "https://www.swebench.com/",
}
API_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY")


def sh(value: str | pathlib.Path) -> str:
    return shlex.quote(str(value))


def write_executable(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | 0o111)


def python_version_tuple() -> tuple[int, int, int]:
    return sys.version_info.major, sys.version_info.minor, sys.version_info.micro


def backend_install_lines(backend: str, *, src_dir: pathlib.Path, venv_dir: pathlib.Path, swe_agent_repo: str) -> list[str]:
    lines: list[str] = []
    if backend == "swe-agent-source":
        repo_dir = src_dir / "SWE-agent"
        lines.extend(
            [
                f"mkdir -p {sh(src_dir)}",
                f"if [ ! -d {sh(repo_dir / '.git')} ]; then git clone --depth 1 {sh(swe_agent_repo)} {sh(repo_dir)}; else git -C {sh(repo_dir)} fetch --depth 1 origin && git -C {sh(repo_dir)} pull --ff-only; fi",
                f"python -m pip install --editable {sh(repo_dir)}",
            ]
        )
    elif backend == "mini-swe-agent":
        lines.append("python -m pip install mini-swe-agent")
    elif backend == "openhands-pip":
        lines.append("python -m pip install openhands")
    elif backend == "openhands-uv":
        lines.extend(
            [
                "python -m pip install uv",
                "uv tool install openhands --python 3.12",
                "echo 'OpenHands CLI installed with uv; if openhands is not on PATH, follow uv tool PATH guidance and rerun readiness'",
            ]
        )
    elif backend == "swebench-pip":
        lines.append("python -m pip install swebench")
    else:
        raise SystemExit(f"unknown backend: {backend}")
    return lines


def build_install_script(args: argparse.Namespace, *, out_dir: pathlib.Path, venv_dir: pathlib.Path, src_dir: pathlib.Path) -> str:
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {sh(pathlib.Path.cwd())}",
        f"{sh(args.python)} -m venv {sh(venv_dir)}",
        f"source {sh(venv_dir / 'bin' / 'activate')}",
        "python -m pip install --upgrade pip setuptools wheel",
    ]
    for backend in args.backend:
        lines.extend(["", f"# backend: {backend}", *backend_install_lines(backend, src_dir=src_dir, venv_dir=venv_dir, swe_agent_repo=args.swe_agent_repo)])
    lines.extend(
        [
            "",
            "python - <<'PY'",
            "import importlib.util",
            "for name in ['swebench', 'sweagent', 'swe_agent', 'mini_swe_agent', 'openhands']:",
            "    print(f'{name}={bool(importlib.util.find_spec(name))}')",
            "PY",
            "for cmd in sweagent swe-agent mini-swe-agent openhands; do command -v \"$cmd\" >/dev/null 2>&1 && echo \"$cmd=$(command -v \"$cmd\")\" || true; done",
        ]
    )
    return "\n".join(lines) + "\n"


def build_env_script(args: argparse.Namespace, *, out_dir: pathlib.Path, venv_dir: pathlib.Path, capture_dir: pathlib.Path) -> str:
    lines = [
        "#!/usr/bin/env bash",
        f"source {sh(venv_dir / 'bin' / 'activate')}",
        f"export FLOWCAUSAL_REAL_AGENT_CAPTURE_DIR={sh(capture_dir)}",
        "# export OPENAI_API_KEY=...",
        "# export ANTHROPIC_API_KEY=...",
        "# export OPENROUTER_API_KEY=...",
        "# export DEEPSEEK_API_KEY=...",
    ]
    return "\n".join(lines) + "\n"


def build_api_template() -> str:
    return "\n".join([f"# {key}=" for key in API_KEYS]) + "\n"


def build_readiness_script(args: argparse.Namespace, *, out_dir: pathlib.Path, venv_dir: pathlib.Path, capture_dir: pathlib.Path) -> str:
    readiness = pathlib.Path(__file__).resolve().parent / "check_real_agent_readiness.py"
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"source {sh(venv_dir / 'bin' / 'activate')}",
        f"python {sh(readiness)} \\",
        f"  --capture-dir {sh(capture_dir)} \\",
        f"  --min-instances {int(args.min_instances)} \\",
        f"  --output {sh(out_dir / 'readiness.json')} \\",
        f"  --markdown-output {sh(out_dir / 'readiness.md')}",
    ]
    return "\n".join(lines) + "\n"


def detect_blockers(args: argparse.Namespace) -> list[str]:
    blockers: list[str] = []
    python_path = pathlib.Path(args.python)
    if python_path.is_absolute() or "/" in args.python:
        if not python_path.exists():
            blockers.append(f"--python executable does not exist: {args.python}")
    elif shutil.which(args.python) is None:
        blockers.append(f"--python executable was not found in PATH: {args.python}")
    major, minor, _micro = python_version_tuple()
    if (major, minor) < (3, 10):
        blockers.append("Python < 3.10 is too old for the FlowCausal experiment helpers")
    if "openhands-pip" in args.backend and (major, minor) < (3, 12):
        blockers.append("OpenHands pip installation expects Python 3.12; use --python pointing at python3.12 or use openhands-uv")
    return blockers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate an isolated SWE-agent/OpenHands/SWE-bench harness bootstrap bundle",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--out-dir", default="flowcausal/experiments/results/real-agent-harness")
    parser.add_argument("--venv-dir", help="venv directory; default is <out-dir>/.venv")
    parser.add_argument("--src-dir", help="source checkout directory; default is <out-dir>/src")
    parser.add_argument("--capture-dir", default="flowcausal/experiments/results/real-agent-captures")
    parser.add_argument("--backend", action="append", choices=("swe-agent-source", "mini-swe-agent", "openhands-uv", "openhands-pip", "swebench-pip"), help="tool backend to install; repeatable")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--swe-agent-repo", default="https://github.com/SWE-agent/SWE-agent.git")
    parser.add_argument("--min-instances", type=int, default=3)
    parser.add_argument("--execute", action="store_true", help="run install-harness.sh after generating it")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.backend = args.backend or list(DEFAULT_BACKENDS)
    out_dir = pathlib.Path(args.out_dir).expanduser().resolve()
    venv_dir = pathlib.Path(args.venv_dir).expanduser().resolve() if args.venv_dir else out_dir / ".venv"
    src_dir = pathlib.Path(args.src_dir).expanduser().resolve() if args.src_dir else out_dir / "src"
    capture_dir = pathlib.Path(args.capture_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    install_path = out_dir / "install-harness.sh"
    env_path = out_dir / "env.sh"
    api_path = out_dir / "api-keys.env.template"
    readiness_path = out_dir / "post-install-readiness.sh"
    report_path = out_dir / "bootstrap-report.json"

    write_executable(install_path, build_install_script(args, out_dir=out_dir, venv_dir=venv_dir, src_dir=src_dir))
    write_executable(env_path, build_env_script(args, out_dir=out_dir, venv_dir=venv_dir, capture_dir=capture_dir))
    api_path.write_text(build_api_template(), encoding="utf-8")
    write_executable(readiness_path, build_readiness_script(args, out_dir=out_dir, venv_dir=venv_dir, capture_dir=capture_dir))

    blockers = detect_blockers(args)
    run: dict[str, Any] | None = None
    if args.execute and not blockers:
        proc = subprocess.run([str(install_path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        run = {
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
        if proc.returncode != 0:
            blockers.append(f"install-harness.sh failed with returncode={proc.returncode}")
    elif args.execute and blockers:
        run = {"skipped": "not executed because bootstrap blockers exist"}

    report = {
        "schema": SCHEMA,
        "status": "pass" if not blockers else "fail",
        "mode": "executed" if args.execute and run and not run.get("skipped") else "generated",
        "python": args.python,
        "python_version": ".".join(str(part) for part in python_version_tuple()),
        "backends": list(args.backend),
        "doc_urls": {backend: DOC_URLS.get(backend, "") for backend in args.backend},
        "artifacts": {
            "install_script": str(install_path),
            "env_script": str(env_path),
            "api_key_template": str(api_path),
            "post_install_readiness_script": str(readiness_path),
            "capture_dir": str(capture_dir),
            "venv_dir": str(venv_dir),
            "src_dir": str(src_dir),
        },
        "blockers": blockers,
        "run": run,
        "next_actions": [
            f"review {install_path}",
            f"run {install_path} when ready" if not args.execute else f"run {readiness_path}",
            f"source {env_path}",
            "set one API key in the shell, then capture three real instances",
        ],
        "notes": [
            "bootstrap generation does not prove the real-agent experiment has run",
            "Docker and API-key readiness are checked by check_real_agent_readiness.py after installation",
        ],
    }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "install_script": str(install_path)}, sort_keys=True))
    return 0 if not blockers else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

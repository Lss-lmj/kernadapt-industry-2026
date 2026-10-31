#!/usr/bin/env python3
"""Generate repo-backed HarnessReplayBench traces from stage specs.

The real mini-swe-agent traces in HarnessReplayBench preserve actual agent
tool calls, but the current seed set is mostly INSPECT/TEST-heavy.  This tool
fills a different, explicitly labeled role: it turns a repository stage spec
or a conservative local preset into the same ``flowcausal.agent_trace.v1``
schema so BUILD/TEST/PACKAGE-heavy workflows can enter the replay/matrix
pipeline without being mislabeled as live agent trajectories.

Generated traces are repo-backed stage traces, not frontier or endpoint
evidence by themselves.  They are useful for benchmark breadth, replay
plumbing, and scheduler mechanism experiments that still need the normal
fidelity/direct-mechanism/safety gates before entering the paper's main
evaluation.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import subprocess
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
WORKLOADS = HERE / "workloads"
sys.path.insert(0, str(WORKLOADS))

from agent_trace_workflow import normalize_stage  # noqa: E402


SCHEMA = "flowcausal.agent_trace.v1"
SPEC_SCHEMA = "flowcausal.repo_stage_spec.v1"
GENERATOR_NAME = "flowcausal/experiments/generate_harness_replay_trace_from_stage_spec.py"


FLOWCAUSAL_PYTHON_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-python.inspect",
        "stage": "INSPECT",
        "cmd": "find experiments src/controller -maxdepth 2 -type f | sort | head -200",
        "cwd": ".",
    },
    {
        "id": "flowcausal-python.build.compileall",
        "stage": "BUILD",
        "cmd": "python3 -m compileall -q experiments src/controller",
        "cwd": ".",
    },
    {
        "id": "flowcausal-python.test.core",
        "stage": "TEST",
        "cmd": "python3 experiments/test_normalize_agent_trace.py && python3 experiments/test_summarize_harness_replaybench.py",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "preset marks the validation command as the user-visible gate",
        },
    },
    {
        "id": "flowcausal-python.package.artifact",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-python-artifact.tar.gz\" experiments/*.py experiments/workloads/*.py src/controller/*.py",
        "cwd": ".",
    },
]

FLOWCAUSAL_CONTROLLER_CI_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-controller-ci.inspect",
        "stage": "INSPECT",
        "cmd": "find src/controller experiments/workloads -maxdepth 2 -type f | sort | head -200",
        "cwd": ".",
    },
    {
        "id": "flowcausal-controller-ci.build.compileall",
        "stage": "BUILD",
        "cmd": "python3 -m compileall -q src/controller experiments/workloads experiments/*.py",
        "cwd": ".",
    },
    {
        "id": "flowcausal-controller-ci.test.unit",
        "stage": "TEST",
        "cmd": "python3 src/controller/test_flowctl.py && python3 experiments/test_capture_real_agent_run.py",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "controller/unit-test validation is the user-visible gate",
        },
    },
    {
        "id": "flowcausal-controller-ci.package.logs",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-controller-ci.tar.gz\" src/controller experiments/workloads experiments/test_capture_real_agent_run.py",
        "cwd": ".",
    },
]

FLOWCAUSAL_FLOWBENCH_C_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-flowbench-c.inspect",
        "stage": "INSPECT",
        "cmd": "find experiments/flowbench -maxdepth 1 -type f | sort",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-c.build.make",
        "stage": "BUILD",
        "cmd": "make -C experiments/flowbench clean all",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-c.test.smoke",
        "stage": "TEST",
        "cmd": "make -C experiments/flowbench smoke",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "C microbenchmark smoke is the user-visible gate",
        },
    },
    {
        "id": "flowcausal-flowbench-c.package.binary",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-flowbench-c.tar.gz\" experiments/flowbench/flowbench experiments/flowbench/*.c experiments/flowbench/*.py experiments/flowbench/Makefile",
        "cwd": ".",
    },
]

FLOWCAUSAL_FLOWBENCH_STRESS_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-flowbench-stress.inspect",
        "stage": "INSPECT",
        "cmd": "find experiments/flowbench -maxdepth 1 -type f | sort",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-stress.build.flowbench",
        "stage": "BUILD",
        "cmd": "make -C experiments/flowbench flowbench",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-stress.test.stress",
        "stage": "TEST",
        "cmd": "python3 experiments/flowbench/run_flowbench.py --sessions 3 --trials 1 --stages 3 --cohort 6 --frontier-width 1 --work-ms 8 --tail-work-ms 24 --out-dir \"$FLOWCAUSAL_STAGE_OUT/flowbench-stress\"",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "flowbench stress run is the user-visible gate",
        },
    },
    {
        "id": "flowcausal-flowbench-stress.package.results",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT/package\" && cp \"$FLOWCAUSAL_STAGE_OUT/flowbench-stress/summary.json\" \"$FLOWCAUSAL_STAGE_OUT/package/\" && cp \"$FLOWCAUSAL_STAGE_OUT/flowbench-stress/raw.ndjson\" \"$FLOWCAUSAL_STAGE_OUT/package/\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-flowbench-stress.tar.gz\" -C \"$FLOWCAUSAL_STAGE_OUT/package\" summary.json raw.ndjson",
        "cwd": ".",
    },
]

FLOWCAUSAL_FLOWBENCH_PRESSURE_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-flowbench-pressure.inspect",
        "stage": "INSPECT",
        "cmd": "find experiments/flowbench -maxdepth 1 -type f | sort",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-pressure.build.flowbench",
        "stage": "BUILD",
        "cmd": "make -C experiments/flowbench flowbench",
        "cwd": ".",
    },
    {
        "id": "flowcausal-flowbench-pressure.test.pressure",
        "stage": "TEST",
        "cmd": "python3 experiments/flowbench/run_flowbench.py --sessions 6 --trials 1 --stages 4 --cohort 8 --frontier-width 1 --work-ms 25 --tail-work-ms 120 --out-dir \"$FLOWCAUSAL_STAGE_OUT/flowbench-pressure\"",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "flowbench pressure run is the user-visible CPU-frontier gate",
        },
    },
    {
        "id": "flowcausal-flowbench-pressure.package.results",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT/package\" && cp \"$FLOWCAUSAL_STAGE_OUT/flowbench-pressure/summary.json\" \"$FLOWCAUSAL_STAGE_OUT/package/\" && cp \"$FLOWCAUSAL_STAGE_OUT/flowbench-pressure/raw.ndjson\" \"$FLOWCAUSAL_STAGE_OUT/package/\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-flowbench-pressure.tar.gz\" -C \"$FLOWCAUSAL_STAGE_OUT/package\" summary.json raw.ndjson",
        "cwd": ".",
    },
]

FLOWCAUSAL_PAPER_TABLES_PRESET: list[dict[str, Any]] = [
    {
        "id": "flowcausal-paper-tables.inspect",
        "stage": "INSPECT",
        "cmd": "find paper docs -maxdepth 2 -type f \\( -name '*.md' -o -name '*.tex' \\) | sort | head -200",
        "cwd": ".",
    },
    {
        "id": "flowcausal-paper-tables.build.tables",
        "stage": "BUILD",
        "cmd": "python3 -c 'import os; from pathlib import Path; out=Path(os.environ[\"FLOWCAUSAL_STAGE_OUT\"])/\"document-breadth-table.tex\"; rows=[(\"plan\", len(Path(\"plan.md\").read_text(encoding=\"utf-8\").splitlines())), (\"harness\", len(Path(\"docs/harness-replaybench.md\").read_text(encoding=\"utf-8\").splitlines())), (\"outline\", len(Path(\"paper/outline.md\").read_text(encoding=\"utf-8\").splitlines()))]; out.parent.mkdir(parents=True, exist_ok=True); out.write_text(\"\\\\\\\\begin{tabular}{lr}\\n\" + \"\\n\".join(f\"{name} & {count} \\\\\\\\\" for name, count in rows) + \"\\n\\\\\\\\end{tabular}\\n\", encoding=\"utf-8\")'",
        "cwd": ".",
    },
    {
        "id": "flowcausal-paper-tables.test.validate",
        "stage": "TEST",
        "cmd": "test -s \"$FLOWCAUSAL_STAGE_OUT/document-breadth-table.tex\" && grep -q 'tabular' \"$FLOWCAUSAL_STAGE_OUT/document-breadth-table.tex\"",
        "cwd": ".",
        "critical": True,
        "frontier_label": {
            "source": "repo-stage-preset-heuristic",
            "role": "foreground-gate-candidate",
            "reason": "generated paper table validation is the user-visible gate",
        },
    },
    {
        "id": "flowcausal-paper-tables.package.table",
        "stage": "PACKAGE_BACKGROUND",
        "cmd": "mkdir -p \"$FLOWCAUSAL_STAGE_OUT/package\" && cp \"$FLOWCAUSAL_STAGE_OUT/document-breadth-table.tex\" \"$FLOWCAUSAL_STAGE_OUT/package/\" && tar -czf \"$FLOWCAUSAL_STAGE_OUT/flowcausal-paper-tables.tar.gz\" -C \"$FLOWCAUSAL_STAGE_OUT/package\" document-breadth-table.tex",
        "cwd": ".",
    },
]


PRESETS: dict[str, list[dict[str, Any]]] = {
    "flowcausal-controller-ci": FLOWCAUSAL_CONTROLLER_CI_PRESET,
    "flowcausal-flowbench-c": FLOWCAUSAL_FLOWBENCH_C_PRESET,
    "flowcausal-flowbench-pressure": FLOWCAUSAL_FLOWBENCH_PRESSURE_PRESET,
    "flowcausal-flowbench-stress": FLOWCAUSAL_FLOWBENCH_STRESS_PRESET,
    "flowcausal-paper-tables": FLOWCAUSAL_PAPER_TABLES_PRESET,
    "flowcausal-python": FLOWCAUSAL_PYTHON_PRESET,
}


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_value(repo_dir: pathlib.Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo_dir), *args],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def repo_provenance(repo_dir: pathlib.Path) -> dict[str, Any]:
    repo_dir = repo_dir.resolve()
    return {
        "repo_dir": str(repo_dir),
        "git_head": git_value(repo_dir, "rev-parse", "HEAD"),
        "git_branch": git_value(repo_dir, "rev-parse", "--abbrev-ref", "HEAD"),
        "git_remote_origin": git_value(repo_dir, "remote", "get-url", "origin"),
    }


def read_json(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON spec: {path}: {exc}") from exc


def load_spec_commands(path: pathlib.Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    data = read_json(path)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)], {}
    if not isinstance(data, dict):
        raise SystemExit("--spec expects a JSON object or a list of command objects")
    commands = data.get("commands")
    if not isinstance(commands, list):
        raise SystemExit("--spec object must contain a commands list")
    meta = {key: value for key, value in data.items() if key != "commands"}
    return [item for item in commands if isinstance(item, dict)], meta


def parse_stage_command(value: str, *, index: int) -> dict[str, Any]:
    if "::" in value:
        stage, command = value.split("::", 1)
    elif "=" in value:
        stage, command = value.split("=", 1)
    else:
        raise SystemExit("--stage-command expects STAGE::COMMAND or STAGE=COMMAND")
    stage = stage.strip()
    command = command.strip()
    if not stage or not command:
        raise SystemExit(f"invalid --stage-command entry: {value!r}")
    return {
        "id": f"cli-stage-command[{index}]",
        "stage": stage,
        "cmd": command,
        "cwd": ".",
    }


def command_from_record(record: dict[str, Any]) -> str:
    for key in ("cmd", "command", "bash", "shell"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise SystemExit(f"stage command missing cmd/command/bash/shell: {record}")


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_commands(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for index, raw in enumerate(records):
        command = command_from_record(raw)
        stage = normalize_stage(raw.get("stage"), command)
        record: dict[str, Any] = {
            "id": str(raw.get("id") or raw.get("source_id") or f"stage_spec[{index}]"),
            "cmd": command,
            "stage": stage,
            "turn": int(raw.get("turn", index) or 0),
            "cwd": str(raw.get("cwd") or "."),
        }
        for key in ("allow_failure", "critical"):
            if key in raw:
                record[key] = as_bool(raw.get(key), default=False)
        for key in ("frontier_label", "observed_returncode", "duration_sec", "metadata"):
            if key in raw:
                record[key] = raw[key]
        out.append(record)
    if not out:
        raise SystemExit("stage spec produced no commands")
    return out


def choose_critical(commands: list[dict[str, Any]], policy: str) -> int | None:
    if any(bool(command.get("critical")) for command in commands):
        return None
    if policy == "explicit-only":
        return None
    if policy == "last-test":
        test_indices = [idx for idx, command in enumerate(commands) if command.get("stage") == "TEST"]
        if test_indices:
            return test_indices[-1]
    foreground = [
        idx
        for idx, command in enumerate(commands)
        if command.get("stage") not in {"PACKAGE_BACKGROUND"}
    ]
    return foreground[-1] if foreground else len(commands) - 1


def apply_critical_policy(commands: list[dict[str, Any]], args: argparse.Namespace) -> None:
    critical_index = choose_critical(commands, args.critical_policy)
    if critical_index is None:
        return
    commands[critical_index]["critical"] = True
    if args.emit_heuristic_frontier_labels and "frontier_label" not in commands[critical_index]:
        commands[critical_index]["frontier_label"] = {
            "source": "repo-stage-generator-heuristic",
            "role": "foreground-gate-candidate",
            "critical_policy": args.critical_policy,
        }


def collect_records(args: argparse.Namespace) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if args.spec:
        return load_spec_commands(pathlib.Path(args.spec).resolve())
    if args.stage_command:
        return [parse_stage_command(value, index=index) for index, value in enumerate(args.stage_command)], {}
    if args.preset:
        return [dict(item) for item in PRESETS[args.preset]], {"preset": args.preset}
    raise SystemExit("provide --spec, at least one --stage-command, or --preset")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate repo-backed flowcausal.agent_trace.v1 traces from stage specs",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--repo-dir", required=True, help="repository backing the generated commands")
    parser.add_argument("--output", required=True, help="output normalized trace JSON")
    parser.add_argument("--spec", help=f"JSON stage spec, usually schema {SPEC_SCHEMA}")
    parser.add_argument(
        "--stage-command",
        action="append",
        default=[],
        help="inline command in STAGE::COMMAND or STAGE=COMMAND form; repeatable",
    )
    parser.add_argument("--preset", choices=sorted(PRESETS), help="built-in repo-backed stage preset")
    parser.add_argument("--source", default="repo-stage-spec", help="logical source label for the trace")
    parser.add_argument("--instance-id", help="trace instance id")
    parser.add_argument(
        "--critical-policy",
        choices=["last-test", "last-foreground", "explicit-only"],
        default="last-test",
    )
    parser.add_argument("--emit-heuristic-frontier-labels", action="store_true")
    parser.add_argument("--created-at", help="override created_at for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_dir = pathlib.Path(args.repo_dir).resolve()
    if not repo_dir.is_dir():
        raise SystemExit(f"repo dir not found: {repo_dir}")
    output = pathlib.Path(args.output).resolve()
    records, spec_meta = collect_records(args)
    commands = normalize_commands(records)
    apply_critical_policy(commands, args)

    created_at = args.created_at or _dt.datetime.now(_dt.UTC).isoformat()
    spec_path = pathlib.Path(args.spec).resolve() if args.spec else None
    instance_id = args.instance_id or str(spec_meta.get("instance_id") or spec_meta.get("id") or "")
    if not instance_id:
        instance_id = args.preset or "repo-stage-spec"
    payload = {
        "schema": SCHEMA,
        "source": args.source,
        "source_format": "repo-stage-spec",
        "instance_id": instance_id,
        "created_at": created_at,
        "commands": commands,
        "provenance": {
            "generator": GENERATOR_NAME,
            "generator_schema": SPEC_SCHEMA,
            "input_file": str(spec_path) if spec_path else "",
            "input_sha256": file_sha256(spec_path) if spec_path else "",
            "preset": args.preset or spec_meta.get("preset", ""),
            "source_kind": "repo-backed-stage-spec",
            "repo": repo_provenance(repo_dir),
            "normalized_commands": len(commands),
            "critical_policy": args.critical_policy,
            "notes": [
                "This is a repo-backed stage trace for HarnessReplayBench breadth.",
                "It is not a live agent trajectory, full endpoint result, or frontier oracle.",
                "LLM/API wait is absent by construction; scheduler claims still require fidelity, direct-mechanism, and safety gates.",
            ],
        },
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": "pass",
                "output": str(output),
                "commands": len(commands),
                "instance_id": instance_id,
                "stages": [command["stage"] for command in commands],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

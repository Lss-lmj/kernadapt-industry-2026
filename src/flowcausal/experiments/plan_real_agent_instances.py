#!/usr/bin/env python3
"""Select real SWE-bench/SWE-agent/OpenHands instances and emit capture commands.

The FlowCausal real-agent evidence path needs a small but explicit first
experiment set.  This helper reads a user-provided task list (JSON/JSONL from
SWE-bench, SWE-agent, OpenHands, or a hand-written subset), selects a diverse
set of instances by repository, and writes:

* a machine-readable instance plan;
* a markdown runbook;
* a shell script with ``capture_real_agent_run.py`` commands.

It does not download benchmarks, clone repos, run agents, or invent instance
ids.  If required fields are absent, those records are skipped and reported.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shlex
from typing import Any

from validate_bundle_collection_manifest import safe_name


HERE = pathlib.Path(__file__).resolve().parent
SCHEMA = "flowcausal.real_agent_instance_plan.v1"
INSTANCE_KEYS = ("instance_id", "task_id", "problem_id", "sample_id", "id")
REPO_KEYS = ("repo", "repository", "repo_name", "project", "github_repo")


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def read_json_or_jsonl(path: pathlib.Path) -> list[Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"invalid JSONL at {path}:{line_no}: {exc}") from exc
        return rows
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON at {path}: {exc}") from exc
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("instances", "tasks", "data", "rows", "results"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    raise SystemExit(f"task list must be a JSON object/list or JSONL rows: {path}")


def first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def count_listish(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return len(parsed)
        except json.JSONDecodeError:
            return 1
    return 0


def task_weight(row: dict[str, Any]) -> int:
    # Prefer tasks with at least some test metadata, but avoid depending on a
    # specific benchmark schema.
    return (
        count_listish(row.get("FAIL_TO_PASS"))
        + count_listish(row.get("PASS_TO_PASS"))
        + count_listish(row.get("fail_to_pass"))
        + count_listish(row.get("pass_to_pass"))
    )


def normalize_task(raw: Any, index: int) -> tuple[dict[str, Any] | None, list[str]]:
    warnings: list[str] = []
    if not isinstance(raw, dict):
        return None, [f"tasks[{index}] is not an object"]
    instance_id = first_text(raw, INSTANCE_KEYS)
    repo = first_text(raw, REPO_KEYS)
    blockers: list[str] = []
    if not instance_id:
        blockers.append("missing instance_id")
    if not repo:
        blockers.append("missing repo")
    if blockers:
        return None, [f"tasks[{index}]: {', '.join(blockers)}"]
    return (
        {
            "index": index,
            "instance_id": instance_id,
            "repo": repo,
            "repo_safe": safe_name(repo.replace("/", "__")),
            "instance_safe": safe_name(instance_id),
            "weight": task_weight(raw),
            "raw": {
                key: raw.get(key)
                for key in (
                    "base_commit",
                    "version",
                    "created_at",
                    "difficulty",
                    "FAIL_TO_PASS",
                    "PASS_TO_PASS",
                    "fail_to_pass",
                    "pass_to_pass",
                )
                if key in raw
            },
        },
        warnings,
    )


def select_diverse(tasks: list[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    by_repo: dict[str, list[dict[str, Any]]] = {}
    for task in sorted(tasks, key=lambda item: (-int(item.get("weight", 0)), int(item.get("index", 0)))):
        by_repo.setdefault(str(task["repo"]), []).append(task)
    selected: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    repos = sorted(by_repo, key=lambda repo: (-len(by_repo[repo]), repo))
    while len(selected) < count:
        progressed = False
        for repo in repos:
            while by_repo[repo] and str(by_repo[repo][0]["instance_id"]) in used_ids:
                by_repo[repo].pop(0)
            if not by_repo[repo]:
                continue
            task = by_repo[repo].pop(0)
            selected.append(task)
            used_ids.add(str(task["instance_id"]))
            progressed = True
            if len(selected) >= count:
                break
        if not progressed:
            break
    return selected


def template_value(template: str, task: dict[str, Any], *, capture_dir: pathlib.Path, repo_root: pathlib.Path) -> str:
    repo_dir = repo_root / str(task["instance_safe"])
    values = {
        "instance_id": str(task["instance_id"]),
        "instance_safe": str(task["instance_safe"]),
        "repo": str(task["repo"]),
        "repo_safe": str(task["repo_safe"]),
        "repo_dir": str(repo_dir),
        "capture_dir": str(capture_dir),
    }
    return template.format(**values)


def shell_var(instance_id: str, suffix: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", instance_id).strip("_").upper() or "INSTANCE"
    return f"{base}_{suffix}"


def shell_quote_list(parts: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def output_path_from_template(
    template: str | None,
    task: dict[str, Any],
    *,
    args: argparse.Namespace,
    default_leaf: str,
) -> tuple[str, str | None]:
    if template:
        return (
            template_value(
                template,
                task,
                capture_dir=pathlib.Path(args.capture_dir),
                repo_root=pathlib.Path(args.repo_root),
            ),
            None,
        )
    if args.mini_swebench:
        return str(pathlib.Path(args.capture_dir) / "runs" / default_leaf.format(instance_id=str(task["instance_id"]))), None
    var = shell_var(str(task["instance_id"]), "RAW" if default_leaf.endswith(".traj") else "ENDPOINT")
    return f"${{{var}:?set {var} to the harness {'trajectory' if default_leaf.endswith('.traj') else 'endpoint'} file}}", f"requires shell variable {var}"


def mini_swebench_command(args: argparse.Namespace, *, task: dict[str, Any], raw_output: str, endpoint: str) -> list[str]:
    cmd = [
        "python3",
        str(HERE / "run_mini_swebench_single_capture.py"),
        "--instance-id",
        str(task["instance_id"]),
        "--trajectory",
        raw_output,
        "--endpoint",
        endpoint,
        "--subset",
        str(args.mini_subset),
        "--split",
        str(args.mini_split),
        "--cost-limit",
        str(args.mini_cost_limit),
        "--mini-extra",
        str(args.mini_extra),
    ]
    if args.mini_model:
        cmd.extend(["--model", str(args.mini_model)])
    for item in args.mini_config:
        cmd.extend(["--config", str(item)])
    if args.mini_yolo:
        cmd.append("--yolo")
    if args.mini_allow_command_failure:
        cmd.append("--allow-command-failure")
    return cmd


def capture_command(task: dict[str, Any], args: argparse.Namespace, *, repo_dir: pathlib.Path) -> tuple[list[str], list[str]]:
    label_template = args.label_template
    matrix_template = args.matrix_summary_template
    notes: list[str] = []

    raw_output, raw_note = output_path_from_template(
        args.raw_output_template,
        task,
        args=args,
        default_leaf="{instance_id}.traj",
    )
    endpoint, endpoint_note = output_path_from_template(
        args.endpoint_template,
        task,
        args=args,
        default_leaf="{instance_id}.endpoint.json",
    )
    if raw_note:
        notes.append(raw_note)
    if endpoint_note:
        notes.append(endpoint_note)

    cmd = [
        "python3",
        str(HERE / "capture_real_agent_run.py"),
        "--out-dir",
        str(args.capture_dir),
        "--instance-id",
        str(task["instance_id"]),
        "--repo-dir",
        str(repo_dir),
        "--raw-agent-output",
        raw_output,
        "--endpoint-result",
        endpoint,
        "--raw-format",
        str(args.raw_format),
        "--source",
        str(args.source),
    ]
    if args.agent_repo_root:
        cmd.extend(["--agent-repo-root", str(args.agent_repo_root)])
    if args.taskset:
        cmd.extend(["--taskset", str(args.taskset)])
    if label_template:
        cmd.extend(["--frontier-annotations", template_value(label_template, task, capture_dir=pathlib.Path(args.capture_dir), repo_root=pathlib.Path(args.repo_root))])
    if matrix_template:
        cmd.extend(["--matrix-summary", template_value(matrix_template, task, capture_dir=pathlib.Path(args.capture_dir), repo_root=pathlib.Path(args.repo_root))])
    if args.mini_swebench:
        cmd.append("--")
        cmd.extend(mini_swebench_command(args, task=task, raw_output=raw_output, endpoint=endpoint))
    elif args.harness_command_template:
        cmd.append("--")
        cmd.extend(shlex.split(template_value(args.harness_command_template, task, capture_dir=pathlib.Path(args.capture_dir), repo_root=pathlib.Path(args.repo_root))))
    else:
        cmd.append("--no-run")
    return cmd, notes


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    input_path = pathlib.Path(args.task_list).expanduser().resolve()
    raw_rows = read_json_or_jsonl(input_path)
    warnings: list[str] = []
    tasks: list[dict[str, Any]] = []
    for index, row in enumerate(raw_rows):
        task, row_warnings = normalize_task(row, index)
        warnings.extend(row_warnings)
        if task is not None:
            tasks.append(task)
    selected = select_diverse(tasks, args.num_instances)
    blockers: list[str] = []
    if len(selected) < args.num_instances:
        blockers.append(f"selected instances {len(selected)} < requested {args.num_instances}")
    selected_repos = sorted({str(task["repo"]) for task in selected})
    if len(selected_repos) < len(selected):
        warnings.append(f"selected repo diversity {len(selected_repos)} unique repo(s) for {len(selected)} instance(s)")
    capture_dir = pathlib.Path(args.capture_dir)
    repo_root = pathlib.Path(args.repo_root)
    rows: list[dict[str, Any]] = []
    for task in selected:
        repo_dir = repo_root / str(task["instance_safe"])
        cmd, notes = capture_command(task, args, repo_dir=repo_dir)
        rows.append(
            {
                "instance_id": task["instance_id"],
                "repo": task["repo"],
                "repo_dir": str(repo_dir),
                "weight": task["weight"],
                "capture_command": cmd,
                "capture_command_shell": shell_quote_list(cmd),
                "notes": notes,
                "raw": task["raw"],
            }
        )
    status = "pass" if not blockers else "partial" if selected else "fail"
    return {
        "schema": SCHEMA,
        "status": status,
        "task_list": str(input_path),
        "capture_dir": str(args.capture_dir),
        "repo_root": str(args.repo_root),
        "summary": {
            "requested_instances": args.num_instances,
            "input_rows": len(raw_rows),
            "eligible_tasks": len(tasks),
            "selected_instances": len(selected),
            "selected_unique_repos": len(selected_repos),
        },
        "selected": rows,
        "warnings": warnings[:50],
        "blockers": blockers,
        "next_commands": {
            "readiness": [
                "python3",
                str(HERE / "check_real_agent_readiness.py"),
                "--capture-dir",
                str(args.capture_dir),
                "--min-instances",
                str(args.num_instances),
                "--output",
                str(pathlib.Path(args.capture_dir) / "readiness.json"),
                "--markdown-output",
                str(pathlib.Path(args.capture_dir) / "readiness.md"),
            ],
            "preflight": [
                "python3",
                str(HERE / "validate_bundle_collection_manifest.py"),
                "--manifest",
                str(pathlib.Path(args.capture_dir) / "real-agent-manifest.json"),
                "--out-dir",
                str(args.capture_dir),
                "--output",
                str(pathlib.Path(args.capture_dir) / "manifest-preflight.json"),
            ],
        },
        "notes": [
            "selection is a run plan only; it is not evidence until capture, matrix, and audits complete",
            "repo_dir is derived from --repo-root and instance_safe; clone or prepare those repos before capture",
        ],
        "run_environment": {
            "mode": "mini-swe-agent" if args.mini_swebench else "custom",
            "env_script": str(args.env_script) if args.env_script else "",
            "require_any_env": list(args.require_any_env),
        },
    }


def markdown(plan: dict[str, Any]) -> str:
    lines = [
        "# FlowCausal real-agent three-instance plan",
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
    lines.extend(["", "## Selected instances", "", "| Instance | Repo | Weight | Repo dir |", "| --- | --- | ---: | --- |"])
    for row in as_list(plan.get("selected")):
        if not isinstance(row, dict):
            continue
        lines.append(f"| `{row.get('instance_id', '')}` | `{row.get('repo', '')}` | {row.get('weight', 0)} | `{row.get('repo_dir', '')}` |")
    if plan.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in as_list(plan.get("warnings")):
            lines.append(f"- {warning}")
    if plan.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for blocker in as_list(plan.get("blockers")):
            lines.append(f"- {blocker}")
    lines.extend(["", "## Capture commands", ""])
    for row in as_list(plan.get("selected")):
        if not isinstance(row, dict):
            continue
        lines.append(f"### {row.get('instance_id', '')}")
        lines.append("")
        lines.append("```bash")
        lines.append(str(row.get("capture_command_shell", "")))
        lines.append("```")
        lines.append("")
    return "\n".join(lines) + "\n"


def shell_script(plan: dict[str, Any]) -> str:
    run_environment = as_dict(plan.get("run_environment"))
    env_script = str(run_environment.get("env_script") or "")
    require_any_env = [str(item) for item in as_list(run_environment.get("require_any_env")) if str(item)]
    lines = [
        "#!/usr/bin/env bash",
        "set -euo pipefail",
        f"cd {shlex.quote(str(pathlib.Path.cwd()))}",
        "",
        "# Generated by plan_real_agent_instances.py.",
        "# Fill required RAW/ENDPOINT variables if templates were not supplied.",
        "",
    ]
    if env_script:
        quoted_env_script = shlex.quote(env_script)
        lines.extend(
            [
                f"if [[ ! -f {quoted_env_script} ]]; then",
                f"  echo 'missing environment script: {env_script}' >&2",
                "  exit 2",
                "fi",
                f"source {quoted_env_script}",
                "",
            ]
        )
    if require_any_env:
        keys = " ".join(shlex.quote(key) for key in require_any_env)
        lines.extend(
            [
                "FLOWCAUSAL_HAS_REQUIRED_ENV=0",
                f"for key in {keys}; do",
                "  if [[ -n \"${!key:-}\" ]]; then",
                "    FLOWCAUSAL_HAS_REQUIRED_ENV=1",
                "  fi",
                "done",
                "if [[ \"$FLOWCAUSAL_HAS_REQUIRED_ENV\" -ne 1 ]]; then",
                f"  echo 'set at least one API key before capture: {' '.join(require_any_env)}' >&2",
                "  exit 2",
                "fi",
                "",
            ]
        )
    lines.extend(
        [
            f"mkdir -p {shlex.quote(str(pathlib.Path(str(plan.get('capture_dir', '.'))) / 'runs'))}",
            "",
        ]
    )
    for row in as_list(plan.get("selected")):
        if not isinstance(row, dict):
            continue
        lines.append(f"# {row.get('instance_id', '')} ({row.get('repo', '')})")
        for note in as_list(row.get("notes")):
            lines.append(f"# NOTE: {note}")
        lines.append(str(row.get("capture_command_shell", "")))
        lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Select real agent benchmark instances and generate FlowCausal capture commands",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--task-list", required=True, help="JSON/JSONL task list with instance_id and repo fields")
    parser.add_argument("--num-instances", type=int, default=3)
    parser.add_argument("--capture-dir", default="experiments/results/real-agent-captures")
    parser.add_argument("--repo-root", default="benchmarks/harness-replaybench/repos")
    parser.add_argument("--raw-format", choices=("auto", "swe-agent", "openhands", "flowcausal"), default="swe-agent")
    parser.add_argument("--source", default="swe-agent")
    parser.add_argument("--agent-repo-root", default="/testbed")
    parser.add_argument("--taskset", default="0-1")
    parser.add_argument("--raw-output-template", help="format string for raw trajectory path")
    parser.add_argument("--endpoint-template", help="format string for full-harness endpoint path")
    parser.add_argument("--label-template", help="format string for optional frontier annotations")
    parser.add_argument("--matrix-summary-template", help="format string for optional existing matrix summary")
    parser.add_argument("--harness-command-template", help="format string for command after capture_real_agent_run.py --")
    parser.add_argument("--mini-swebench", action="store_true", help="generate run_mini_swebench_single_capture.py commands")
    parser.add_argument("--mini-extra", default="mini-extra", help="mini-extra command/path for --mini-swebench")
    parser.add_argument("--mini-subset", default="lite")
    parser.add_argument("--mini-split", default="test")
    parser.add_argument("--mini-model", default="")
    parser.add_argument("--mini-config", action="append", default=[])
    parser.add_argument("--mini-cost-limit", type=float, default=3.0)
    parser.add_argument("--mini-yolo", action="store_true")
    parser.add_argument("--mini-allow-command-failure", action="store_true")
    parser.add_argument("--env-script", help="shell script to source at the top of generated capture script")
    parser.add_argument("--require-any-env", action="append", default=[], help="generated script exits unless at least one listed env var is set")
    parser.add_argument("--output", required=True)
    parser.add_argument("--markdown-output")
    parser.add_argument("--script-output")
    args = parser.parse_args()
    if args.mini_swebench and args.harness_command_template:
        parser.error("--mini-swebench and --harness-command-template are mutually exclusive")
    return args


def main() -> int:
    args = parse_args()
    plan = build_plan(args)
    output = pathlib.Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_output:
        md = pathlib.Path(args.markdown_output).expanduser().resolve()
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(markdown(plan), encoding="utf-8")
    if args.script_output:
        script = pathlib.Path(args.script_output).expanduser().resolve()
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(shell_script(plan), encoding="utf-8")
        script.chmod(script.stat().st_mode | 0o111)
    print(json.dumps({"status": plan["status"], "selected_instances": plan["summary"]["selected_instances"], "output": str(output)}, sort_keys=True))
    return 0 if plan["status"] != "fail" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

#!/usr/bin/env python3
"""Import public code-agent trajectories into HarnessReplayBench.

The importer is intentionally offline-after-download: it fetches public rows
from Hugging Face datasets, stores the raw row under ``source-artifacts/``, and
normalizes only the sandbox-visible command path into ``traces/upstream/``.

It does not execute benchmark workloads, run tests, launch containers, invoke
LLMs, or contact agent APIs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import re
import shlex
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


BENCHMARK_ROOT = pathlib.Path(__file__).resolve().parents[1]
TRACE_SCHEMA = "harness_replaybench.trace.v1"
SOURCE_ARTIFACT_SCHEMA = "harness_replaybench.source_artifact.v1"
OPENHANDS_DATASET = "nebius/SWE-rebench-openhands-trajectories"
SWE_AGENT_DATASET = "nebius/SWE-agent-trajectories"
REPLAY_PYTHON = "${HRB_REPLAY_PYTHON:-python3}"

OPENHANDS_SHELL_TOOLS = {"execute_bash", "bash", "shell", "run_shell", "run_command", "terminal"}
OPENHANDS_EDITOR_TOOLS = {"str_replace_editor", "editor"}
SWE_AGENT_NATIVE_TOOLS = {
    "open",
    "create",
    "edit",
    "insert",
    "find_file",
    "search_file",
    "search_dir",
    "goto",
    "scroll_down",
    "scroll_up",
    "set_cursors",
}
CONTROL_COMMANDS = {"submit", "exit", "finish", "done", "stop"}
SEARCH_PROBE_COMMAND_RE = re.compile(r"(^|[;&|]\s*)(grep|rg|find)\b")


def value_is_true(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sanitize_name(text: str) -> str:
    text = text.strip()
    text = re.sub(r"[^A-Za-z0-9_.+-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "unknown"


def py_literal(value: str) -> str:
    return repr(str(value))


def python_view_command(path: str, *, start_line: int | None = None, context: int = 80) -> str:
    start_value = "None" if start_line is None else str(max(1, start_line))
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
start_line = {start_value}
if p.is_dir():
    for idx, child in enumerate(sorted(p.rglob("*"))):
        if idx >= 200:
            print("... truncated after 200 entries")
            break
        print(child)
else:
    text = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if start_line is None:
        lo, hi = 1, min(len(text), {context})
    else:
        lo, hi = max(1, start_line - 20), min(len(text), start_line + {context})
    for lineno in range(lo, hi + 1):
        print(f"{{lineno}}:{{text[lineno - 1]}}")
PY"""


def python_touch_command(path: str) -> str:
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
p.parent.mkdir(parents=True, exist_ok=True)
p.touch()
PY"""


def python_write_command(path: str, text: str) -> str:
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
p.parent.mkdir(parents=True, exist_ok=True)
p.write_text({py_literal(text)}, encoding="utf-8")
PY"""


def python_line_replace_command(path: str, start: int, end: int, text: str) -> str:
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
start = max(1, {int(start)})
end = max(start, {int(end)})
prefix = lines[: start - 1]
suffix = lines[end:]
new_lines = {py_literal(text)}.splitlines()
p.write_text("\\n".join(prefix + new_lines + suffix) + "\\n", encoding="utf-8")
PY"""


def python_string_replace_command(path: str, old: str, new: str) -> str:
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
text = p.read_text(encoding="utf-8", errors="replace")
old = {py_literal(old)}
new = {py_literal(new)}
if old not in text:
    raise SystemExit(f"old string not found in {{p}}")
p.write_text(text.replace(old, new, 1), encoding="utf-8")
PY"""


def python_insert_command(path: str, insert_line: int | None, text: str) -> str:
    if insert_line is None:
        return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
existing = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
separator = "" if (not existing or existing.endswith("\\n")) else "\\n"
p.write_text(existing + separator + {py_literal(text)} + "\\n", encoding="utf-8")
PY"""
    return f"""python3 - <<'PY'
from pathlib import Path
p = Path({py_literal(path)})
lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
idx = max(0, min(len(lines), {int(insert_line)}))
new_lines = {py_literal(text)}.splitlines()
p.write_text("\\n".join(lines[:idx] + new_lines + lines[idx:]) + "\\n", encoding="utf-8")
PY"""


def parse_jsonish(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "{[":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def command_from_record(record: dict[str, Any]) -> str:
    for key in ("cmd", "command", "bash", "shell"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    action = record.get("action")
    if isinstance(action, dict):
        return command_from_record(action)
    args = record.get("args")
    if isinstance(args, dict):
        return command_from_record(args)
    return ""


def parse_action_words(first_line: str) -> list[str]:
    try:
        return shlex.split(first_line, comments=False, posix=True)
    except ValueError:
        return first_line.strip().split()


def split_edit_body(action: str) -> tuple[str, str]:
    lines = action.splitlines()
    if not lines:
        return "", ""
    body_lines = lines[1:]
    if body_lines and body_lines[-1].strip() == "end_of_edit":
        body_lines = body_lines[:-1]
    return lines[0], "\n".join(body_lines)


def rewrite_repo_root(command: str, *, repo_root: str) -> str:
    root = repo_root.rstrip("/")
    if not root or not root.startswith("/"):
        return command
    pattern = re.compile(rf"(?<![\w./-]){re.escape(root)}(?=$|/|[^\w./-])")
    rewritten = pattern.sub(".", command)
    if root.startswith("/workspace/"):
        rewritten = re.sub(r"(?<![\w./-])/workspace(?=$|/|[^\w./-])", ".", rewritten)
    return rewritten


def rewrite_python_alias(command: str) -> str:
    return re.sub(r"(?<![\w./-])python3?(?=$|[;&|()\s])", REPLAY_PYTHON, command)


def map_cwd(raw_cwd: str, *, repo_root: str) -> tuple[str, str]:
    if not raw_cwd:
        return ".", ""
    if not repo_root:
        return ".", raw_cwd
    try:
        raw_path = pathlib.PurePosixPath(raw_cwd)
        root_path = pathlib.PurePosixPath(repo_root)
        rel = raw_path.relative_to(root_path)
    except ValueError:
        return ".", raw_cwd
    rel_text = str(rel) or "."
    if rel_text.startswith(".."):
        return ".", raw_cwd
    return rel_text, raw_cwd


def infer_stage(command: str, stage_override: str | None = None) -> str:
    if stage_override:
        return stage_override
    text = command.lower()
    first = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if "complete_task_and_submit_final_output" in text:
        return "PACKAGE_BACKGROUND"
    if re.search(r"\bgit\s+diff\b.*>\s*patch", text) or re.search(r"\bcat\s+patch", text):
        return "PACKAGE_BACKGROUND"
    if re.search(r"\b(tar|zip|gzip|xz|bzip2)\b", text):
        return "PACKAGE_BACKGROUND"
    if re.search(r"\b(pytest|tox|nox|unittest)\b", text):
        return "TEST"
    if re.search(r"\b(make|cargo|npm|pnpm|yarn)\s+test\b", text):
        return "TEST"
    if re.search(r"\bpython(?:3)?\s+-m\s+(pytest|unittest|django\s+test)\b", text):
        return "TEST"
    if re.search(r"\bpython(?:3)?\b", text) and re.search(
        r"\b(assert|test|check|verify|reproduce|regression|failed|success)\b", text
    ):
        return "TEST"
    if re.search(r"\b(pip|python(?:3)?\s+-m\s+pip|uv|poetry|conda)\s+(install|sync|env|create)\b", text):
        return "BUILD"
    if re.search(r"\b(make|cmake|ninja|cargo\s+build|npm\s+install|pnpm\s+install|yarn\s+install)\b", text):
        return "BUILD"
    if re.search(r"\b(py_compile|compileall)\b", text):
        return "BUILD"
    if first in {"cc", "gcc", "g++", "clang", "clang++", "ld", "ar", "rustc"}:
        return "BUILD"
    if re.search(r"\b(sed\s+-i|perl\s+-pi|git\s+apply|apply_patch)\b", text):
        return "EDIT"
    if re.search(r"\bpatch\b\s*(?:<|-p)", text):
        return "EDIT"
    if re.search(r"\bpython(?:3)?\b", text) and re.search(r"write_text|open\([^)]*['\"]w|replace\(|\.write\(", text):
        return "EDIT"
    if first in {"find", "grep", "rg", "ag", "ls", "cat", "sed", "awk", "wc", "head", "tail"}:
        return "INSPECT"
    if re.search(r"(^|[;&|()\s])(?:find|grep|rg|ag|ls|cat|awk|wc|head|tail)(?=$|[;&|()\s])", text):
        return "INSPECT"
    if "git grep" in text or re.search(r"\bgit\s+(diff|status|log|show|ls-files)\b", text):
        return "INSPECT"
    return "SHELL"


def is_search_probe(command: str) -> bool:
    return bool(SEARCH_PROBE_COMMAND_RE.search(command))


def is_control_command(command: str) -> bool:
    text = command.strip()
    if not text or "\n" in text:
        return False
    try:
        first = shlex.split(text, comments=False, posix=True)[0].lower()
    except Exception:
        first = text.split(maxsplit=1)[0].lower()
    return first in CONTROL_COMMANDS


def source_path(prefix: str, offset: int, instance_id: str) -> pathlib.Path:
    return pathlib.Path("source-artifacts") / prefix / f"offset-{offset:06d}-{sanitize_name(instance_id)}.json"


def trace_path(prefix: str, instance_id: str, suffix: str) -> pathlib.Path:
    return pathlib.Path("traces") / "upstream" / f"{sanitize_name(instance_id)}.{suffix}.hrb-trace.json"


def infer_openhands_repo_root(row: dict[str, Any]) -> str:
    text = stable_json(row)
    uploaded = re.search(r"<uploaded_files>\s*(/workspace/[^\s<]+)", text)
    if uploaded:
        return uploaded.group(1).rstrip("/")
    match = re.search(r"/workspace/[A-Za-z0-9_.+-]+__[A-Za-z0-9_.+-]+__unknown", text)
    if match:
        return match.group(0).rstrip("/")
    instance = str(row.get("instance_id") or "").strip()
    if instance:
        return f"/workspace/{instance}__unknown"
    return "/workspace"


def update_swe_state_from_observation(text: str, state: dict[str, str]) -> None:
    open_match = re.search(r"\(Open file:\s*([^)]+)\)", text)
    if open_match:
        value = open_match.group(1).strip()
        if value and value.lower() != "n/a":
            state["open_file"] = value
    cwd_match = re.search(r"\(Current directory:\s*([^)]+)\)", text)
    if cwd_match:
        value = cwd_match.group(1).strip()
        if value:
            state["cwd"] = value
            state.setdefault("repo_root", value)


def swe_observation_failed(text: str) -> bool:
    patterns = (
        r"\bEXECUTION TIMED OUT\b",
        r"\bFile .+ not found\b",
        r"\bNo such file or directory\b",
        r"\bcommand not found\b",
        r"Traceback \(most recent call last\)",
        r"\bexited with (?:status|code)\s+[1-9]\d*\b",
        r"(?:^|\n)[^\n]+:\s+error:\s+",
    )
    return any(re.search(pattern, text, re.IGNORECASE | re.DOTALL) for pattern in patterns)


def infer_swe_repo_root(row: dict[str, Any]) -> str:
    for step in row.get("trajectory", []):
        if isinstance(step, dict) and step.get("role") in {"user", "tool"}:
            text = str(step.get("text") or step.get("content") or "")
            match = re.search(r"\(Current directory:\s*([^)]+)\)", text)
            if match:
                return match.group(1).strip()
    logs = str(row.get("eval_logs") or "")
    match = re.search(r"cd\s+(/[A-Za-z0-9_.+/:-]+)\s+&&", logs)
    if match:
        return match.group(1).strip().rstrip("/")
    return ""


def extract_fenced_commands(text: str) -> list[str]:
    commands: list[str] = []
    for match in re.finditer(r"```(?:[A-Za-z0-9_+.-]+)?\s*\n(.*?)```", text, flags=re.DOTALL):
        body = match.group(1).strip()
        if body:
            commands.append(body)
    return commands


def translate_swe_action(action: str, state: dict[str, str]) -> tuple[str, dict[str, Any]]:
    stripped = action.strip()
    if not stripped:
        return "", {}
    first_line, body = split_edit_body(stripped)
    words = parse_action_words(first_line)
    tool = words[0].strip().lower() if words else ""
    if tool in CONTROL_COMMANDS:
        return "", {"agent_action_kind": "control"}
    if tool not in SWE_AGENT_NATIVE_TOOLS:
        return stripped, {"agent_action_kind": "shell"}

    meta: dict[str, Any] = {
        "agent_action": stripped,
        "agent_action_kind": tool,
        "agent_action_translation": "swe_agent_native_to_shell",
    }
    open_file = state.get("open_file", "")
    if tool == "open":
        path = words[1] if len(words) >= 2 else open_file
        line = None
        if len(words) >= 3:
            try:
                line = int(words[2])
            except ValueError:
                line = None
        if not path:
            return "", meta
        state["open_file"] = path
        return python_view_command(path, start_line=line), {**meta, "stage_override": "INSPECT"}
    if tool == "create":
        path = words[1] if len(words) >= 2 else open_file
        if not path:
            return "", meta
        state["open_file"] = path
        return python_touch_command(path), {**meta, "stage_override": "EDIT"}
    if tool == "find_file":
        pattern = words[1] if len(words) >= 2 else "*"
        root = words[2] if len(words) >= 3 else "."
        return f"find {shlex.quote(root)} -name {shlex.quote(pattern)} -print | head -50", {
            **meta,
            "stage_override": "INSPECT",
        }
    if tool in {"search_file", "search_dir"}:
        pattern = words[1] if len(words) >= 2 else ""
        target = words[2] if len(words) >= 3 else "."
        if not pattern:
            return "", meta
        return f"grep -R -n -- {shlex.quote(pattern)} {shlex.quote(target)} | head -80", {
            **meta,
            "stage_override": "INSPECT",
            "allow_failure": True,
        }
    if tool in {"goto", "scroll_down", "scroll_up", "set_cursors"}:
        path = open_file or "."
        return python_view_command(path), {**meta, "stage_override": "INSPECT"}
    if tool == "insert":
        path = open_file
        if not path:
            return "", meta
        text = words[1] if len(words) >= 2 else body
        return python_insert_command(path, None, text), {**meta, "stage_override": "EDIT"}
    if tool == "edit":
        path = open_file
        if not path:
            return "", meta
        if len(words) >= 2 and re.fullmatch(r"\d+:\d+", words[1]):
            start_text, end_text = words[1].split(":", 1)
            return python_line_replace_command(path, int(start_text), int(end_text), body), {
                **meta,
                "stage_override": "EDIT",
            }
        if len(words) >= 3:
            return python_string_replace_command(path, words[1], words[2]), {
                **meta,
                "stage_override": "EDIT",
            }
        if body:
            return python_write_command(path, body), {**meta, "stage_override": "EDIT"}
    return "", meta


def openhands_tool_returncodes(events: list[Any]) -> dict[str, int]:
    returncodes: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("role") != "tool":
            continue
        tool_call_id = event.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            continue
        content = str(event.get("content") or "")
        match = re.search(r"(?:finished|completed) with exit code\s+(-?\d+)", content, re.IGNORECASE)
        if match:
            returncodes[tool_call_id.strip()] = int(match.group(1))
    return returncodes


def openhands_command_from_tool_call(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}
    function = tool_call.get("function")
    if not isinstance(function, dict):
        function = {}
    name = str(function.get("name") or tool_call.get("name") or "").strip().lower()
    arguments = parse_jsonish(function.get("arguments", tool_call.get("arguments", tool_call.get("args", ""))))
    if not isinstance(arguments, dict):
        arguments = {}
    meta = {"openhands_tool": name, "openhands_tool_arguments": arguments}
    if name in OPENHANDS_SHELL_TOOLS:
        # OpenHands uses the same tool for starting a shell command and for
        # sending input to an already running interactive shell.  An input
        # event (for example Ctrl-C) does not create a process of its own and
        # therefore must not enter the replay command sequence.
        if value_is_true(arguments.get("is_input")):
            return "", {**meta, "agent_action_kind": "shell_input"}
        return command_from_record(arguments), {**meta, "agent_action_kind": "shell"}
    if name in OPENHANDS_EDITOR_TOOLS:
        editor_cmd = str(arguments.get("command") or "").strip().lower()
        path = str(arguments.get("path") or "").strip()
        if editor_cmd == "view" and path:
            view_range = arguments.get("view_range")
            start_line = None
            if isinstance(view_range, list) and view_range:
                try:
                    start_line = int(view_range[0])
                except (TypeError, ValueError):
                    start_line = None
            return python_view_command(path, start_line=start_line), {
                **meta,
                "agent_action_kind": "editor_view",
                "stage_override": "INSPECT",
            }
        if editor_cmd == "create" and path:
            return python_write_command(path, str(arguments.get("file_text") or "")), {
                **meta,
                "agent_action_kind": "editor_create",
                "stage_override": "EDIT",
            }
        if editor_cmd == "str_replace" and path:
            return python_string_replace_command(
                path,
                str(arguments.get("old_str") or ""),
                str(arguments.get("new_str") or ""),
            ), {
                **meta,
                "agent_action_kind": "editor_str_replace",
                "stage_override": "EDIT",
            }
        if editor_cmd == "insert" and path:
            insert_line = None
            try:
                insert_line = int(arguments.get("insert_line"))
            except (TypeError, ValueError):
                insert_line = None
            return python_insert_command(path, insert_line, str(arguments.get("new_str") or "")), {
                **meta,
                "agent_action_kind": "editor_insert",
                "stage_override": "EDIT",
            }
    return "", meta


def make_command(
    *,
    source_id: str,
    turn: int,
    command: str,
    raw_cwd: str,
    repo_root: str,
    stage_override: str | None,
    meta: dict[str, Any],
) -> dict[str, Any] | None:
    original = command.strip()
    if not original or is_control_command(original):
        return None
    rewritten = rewrite_repo_root(original, repo_root=repo_root)
    rewritten = rewrite_python_alias(rewritten)
    cwd, mapped_from = map_cwd(raw_cwd, repo_root=repo_root)
    stage = infer_stage(rewritten, stage_override)
    record: dict[str, Any] = {
        "id": source_id,
        "stage": stage,
        "cmd": rewritten,
        "cwd": cwd,
        "turn": turn,
    }
    if mapped_from:
        record["agent_working_dir"] = mapped_from
    if rewritten != original:
        record["agent_cmd"] = original
        record["command_rewrite"] = {
            "source": "hrb-importer",
            "agent_repo_root": repo_root,
            "path_replacement": "." if repo_root else "",
            "python_alias": "python_to_hrb_replay_python"
            if rewrite_python_alias(original) != original
            else "",
        }
    if stage == "INSPECT" and is_search_probe(rewritten):
        record["allow_failure"] = True
        record["allow_failure_reason"] = "inspect_search_probe"
    if meta.get("allow_failure"):
        record["allow_failure"] = True
    for key, value in meta.items():
        if key not in {"stage_override", "allow_failure"}:
            record[key] = value
    return record


def choose_critical(commands: list[dict[str, Any]]) -> None:
    if any(command.get("critical") for command in commands):
        return
    test_indices = [idx for idx, command in enumerate(commands) if command.get("stage") == "TEST"]
    if test_indices:
        critical_index = test_indices[-1]
    else:
        foreground = [
            idx
            for idx, command in enumerate(commands)
            if command.get("stage") not in {"PACKAGE_BACKGROUND"}
        ]
        critical_index = foreground[-1] if foreground else len(commands) - 1
    if commands:
        commands[critical_index]["critical"] = True
        commands[critical_index]["frontier_label"] = {
            "source": "hrb-normalizer-heuristic",
            "role": "foreground-gate-candidate",
            "critical_policy": "last-test-else-last-foreground",
        }


@dataclass
class NormalizedRow:
    dataset: str
    source_format: str
    source: str
    row_offset: int
    instance_id: str
    trace_suffix: str
    repo: str
    model_name: str
    row: dict[str, Any]
    commands: list[dict[str, Any]]
    repo_root: str
    raw_events_or_steps: int

    @property
    def stage_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for command in self.commands:
            stage = str(command.get("stage") or "")
            counts[stage] = counts.get(stage, 0) + 1
        return counts


def normalize_openhands_row(row: dict[str, Any], row_offset: int) -> NormalizedRow | None:
    events = row.get("trajectory")
    if not isinstance(events, list):
        return None
    instance_id = str(row.get("instance_id") or f"openhands-offset-{row_offset}").strip()
    repo_root = infer_openhands_repo_root(row)
    returncodes = openhands_tool_returncodes(events)
    commands: list[dict[str, Any]] = []
    for event_index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        tool_calls = event.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_index, tool_call in enumerate(tool_calls):
            command, meta = openhands_command_from_tool_call(tool_call)
            if not command:
                continue
            tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if isinstance(tool_call_id, str) and tool_call_id.strip():
                meta["tool_call_id"] = tool_call_id.strip()
                if tool_call_id.strip() in returncodes:
                    rc = returncodes[tool_call_id.strip()]
                    meta["observed_returncode"] = rc
                    if rc != 0:
                        meta["allow_failure"] = True
            record = make_command(
                source_id=f"trajectory[{event_index}].tool_calls[{tool_index}]",
                turn=len(commands),
                command=command,
                raw_cwd=repo_root,
                repo_root=repo_root,
                stage_override=meta.get("stage_override"),
                meta=meta,
            )
            if record is not None:
                if "observed_returncode" in meta:
                    record["observed_returncode"] = int(meta["observed_returncode"])
                commands.append(record)
    if not commands:
        return None
    choose_critical(commands)
    return NormalizedRow(
        dataset=OPENHANDS_DATASET,
        source_format="openhands",
        source="upstream-openhands-swerebench",
        row_offset=row_offset,
        instance_id=instance_id,
        trace_suffix="openhands-upstream",
        repo=str(row.get("repo") or ""),
        model_name="",
        row=row,
        commands=commands,
        repo_root=repo_root,
        raw_events_or_steps=len(events),
    )


def normalize_swe_agent_row(row: dict[str, Any], row_offset: int) -> NormalizedRow | None:
    trajectory = row.get("trajectory")
    if not isinstance(trajectory, list):
        return None
    base_instance_id = str(row.get("instance_id") or f"swe-agent-offset-{row_offset}").strip()
    model_name = str(row.get("model_name") or "").strip()
    instance_id = base_instance_id
    repo_root = infer_swe_repo_root(row)
    state = {"repo_root": repo_root, "cwd": repo_root, "open_file": ""}
    commands: list[dict[str, Any]] = []
    pending_observation: list[int] = []
    for step_index, step in enumerate(trajectory):
        if not isinstance(step, dict):
            continue
        role = str(step.get("role") or "").lower()
        text = str(step.get("text") or step.get("content") or "")
        if role in {"user", "tool"}:
            if pending_observation and swe_observation_failed(text):
                for command_index in pending_observation:
                    commands[command_index]["allow_failure"] = True
                    commands[command_index]["allow_failure_reason"] = "recorded_failed_observation"
            pending_observation.clear()
            update_swe_state_from_observation(text, state)
            continue
        if role not in {"ai", "assistant"}:
            continue
        if step.get("mask") is False:
            continue
        for command_index, action in enumerate(extract_fenced_commands(text)):
            command, meta = translate_swe_action(action, state)
            if not command:
                continue
            record = make_command(
                source_id=f"trajectory[{step_index}].fenced_command[{command_index}]",
                turn=len(commands),
                command=command,
                raw_cwd=state.get("cwd", repo_root),
                repo_root=repo_root,
                stage_override=meta.get("stage_override"),
                meta=meta,
            )
            if record is not None:
                commands.append(record)
                pending_observation.append(len(commands) - 1)
    if not commands:
        return None
    choose_critical(commands)
    return NormalizedRow(
        dataset=SWE_AGENT_DATASET,
        source_format="swe-agent",
        source="upstream-swe-agent-trajectories",
        row_offset=row_offset,
        instance_id=instance_id,
        trace_suffix=f"{sanitize_name(model_name)}.swe-agent-upstream" if model_name else "swe-agent-upstream",
        repo=str(row.get("repo") or ""),
        model_name=model_name,
        row=row,
        commands=commands,
        repo_root=repo_root,
        raw_events_or_steps=len(trajectory),
    )


def build_trace_payload(item: NormalizedRow, source_artifact_rel: str, downloaded_at: str) -> dict[str, Any]:
    row_text = stable_json(item.row)
    return {
        "schema": TRACE_SCHEMA,
        "benchmark": "HarnessReplayBench",
        "source": item.source,
        "source_format": item.source_format,
        "instance_id": item.instance_id,
        "created_at": downloaded_at,
        "commands": item.commands,
        "provenance": {
            "dataset": item.dataset,
            "dataset_config": "default",
            "dataset_split": "train",
            "row_offset": item.row_offset,
            "row_sha256": sha256_text(row_text),
            "source_artifact": source_artifact_rel,
            "raw_events_or_steps": item.raw_events_or_steps,
            "normalized_commands": len(item.commands),
            "dropped_or_non_command_events": max(0, item.raw_events_or_steps - len(item.commands)),
            "normalizer": "tools/import_hf_agent_traces.py",
            "critical_policy": "last-test-else-last-foreground",
            "agent_repo_root": item.repo_root,
            "command_path_rewrite": "agent_repo_root_to_relative" if item.repo_root else "",
            "notes": [
                "This trace preserves sandbox-visible commands for scheduler-agnostic replay.",
                "It replays the recorded local tool path deterministically.",
                "If no prepared repo snapshot is packaged, the trace is part of the trace pool but not a runnable formal suite case.",
            ],
        },
    }


def build_source_artifact(item: NormalizedRow, downloaded_at: str) -> dict[str, Any]:
    return {
        "schema": SOURCE_ARTIFACT_SCHEMA,
        "dataset": item.dataset,
        "dataset_config": "default",
        "dataset_split": "train",
        "row_offset": item.row_offset,
        "downloaded_at": downloaded_at,
        "source_url": f"https://huggingface.co/datasets/{item.dataset}",
        "instance_id": item.instance_id,
        "source_format": item.source_format,
        "row_sha256": sha256_text(stable_json(item.row)),
        "row": item.row,
    }


def fetch_rows(dataset: str, *, offset: int, length: int) -> list[tuple[int, dict[str, Any]]]:
    query = urllib.parse.urlencode(
        {
            "dataset": dataset,
            "config": "default",
            "split": "train",
            "offset": offset,
            "length": length,
        }
    )
    url = f"https://datasets-server.huggingface.co/rows?{query}"
    with urllib.request.urlopen(url, timeout=60) as handle:
        payload = json.load(handle)
    rows = []
    for wrapper in payload.get("rows", []):
        row_idx = int(wrapper.get("row_idx", offset + len(rows)))
        row = wrapper.get("row")
        if isinstance(row, dict):
            rows.append((row_idx, row))
    return rows


def existing_instances() -> set[str]:
    instances: set[str] = set()
    for path in (BENCHMARK_ROOT / "traces").rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        instance_id = data.get("instance_id")
        if isinstance(instance_id, str):
            instances.add(instance_id)
    return instances


def score_item(item: NormalizedRow) -> tuple[int, int, int, int]:
    stages = item.stage_counts
    diversity = len(stages)
    has_test = 1 if stages.get("TEST", 0) else 0
    has_build = 1 if stages.get("BUILD", 0) else 0
    return diversity, has_test + has_build, len(item.commands), -item.row_offset


def collect_candidates(
    *,
    dataset: str,
    normalizer: Any,
    scan_limit: int,
    start_offset: int,
    page_size: int,
    min_commands: int,
    skip_instances: set[str],
) -> list[NormalizedRow]:
    candidates_by_instance: dict[str, NormalizedRow] = {}
    stop_offset = start_offset + scan_limit
    for offset in range(start_offset, stop_offset, page_size):
        rows = fetch_rows(dataset, offset=offset, length=min(page_size, stop_offset - offset))
        if not rows:
            break
        for row_idx, row in rows:
            item = normalizer(row, row_idx)
            if item is None:
                continue
            if item.instance_id in skip_instances:
                continue
            if len(item.commands) < min_commands:
                continue
            previous = candidates_by_instance.get(item.instance_id)
            if previous is None or score_item(item) > score_item(previous):
                candidates_by_instance[item.instance_id] = item
    candidates = list(candidates_by_instance.values())
    candidates.sort(key=score_item, reverse=True)
    return candidates


def import_items(items: list[NormalizedRow], *, source_prefix: str, downloaded_at: str, replace: bool) -> list[dict[str, Any]]:
    imported: list[dict[str, Any]] = []
    for item in items:
        source_rel = source_path(source_prefix, item.row_offset, item.instance_id)
        trace_rel = trace_path(source_prefix, item.instance_id, item.trace_suffix)
        source_abs = BENCHMARK_ROOT / source_rel
        trace_abs = BENCHMARK_ROOT / trace_rel
        if trace_abs.exists() and not replace:
            continue
        source_artifact = build_source_artifact(item, downloaded_at)
        trace_payload = build_trace_payload(item, str(source_rel), downloaded_at)
        write_json(source_abs, source_artifact)
        write_json(trace_abs, trace_payload)
        imported.append(
            {
                "instance_id": item.instance_id,
                "source_format": item.source_format,
                "row_offset": item.row_offset,
                "commands": len(item.commands),
                "stages": item.stage_counts,
                "trace": str(trace_rel),
                "source_artifact": str(source_rel),
            }
        )
    return imported


def rebuild_local_sources(instances: set[str] | None = None) -> list[dict[str, Any]]:
    rebuilt: list[dict[str, Any]] = []
    normalizers = {
        "openhands": (normalize_openhands_row, "openhands"),
        "swe-agent": (normalize_swe_agent_row, "swe-agent"),
    }
    for source_abs in sorted((BENCHMARK_ROOT / "source-artifacts").glob("*/*.json")):
        payload = json.loads(source_abs.read_text(encoding="utf-8"))
        source_format = str(payload.get("source_format") or "")
        entry = normalizers.get(source_format)
        row = payload.get("row")
        if entry is None or not isinstance(row, dict):
            continue
        instance_id = str(payload.get("instance_id") or row.get("instance_id") or "")
        if instances and instance_id not in instances:
            continue
        normalizer, prefix = entry
        item = normalizer(row, int(payload.get("row_offset") or 0))
        if item is None:
            continue
        source_rel = source_abs.relative_to(BENCHMARK_ROOT).as_posix()
        trace_rel = trace_path(prefix, item.instance_id, item.trace_suffix)
        trace_payload = build_trace_payload(
            item,
            source_rel,
            str(payload.get("downloaded_at") or dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()),
        )
        write_json(BENCHMARK_ROOT / trace_rel, trace_payload)
        rebuilt.append(
            {
                "instance_id": item.instance_id,
                "commands": len(item.commands),
                "trace": trace_rel.as_posix(),
            }
        )
    return rebuilt


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import public OpenHands/SWE-agent trajectories into HarnessReplayBench",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--openhands-count", type=int, default=10)
    parser.add_argument("--swe-agent-count", type=int, default=6)
    parser.add_argument("--scan-limit", type=int, default=160)
    parser.add_argument("--openhands-start", type=int, default=0)
    parser.add_argument("--swe-agent-start", type=int, default=0)
    parser.add_argument(
        "--swe-agent-instance",
        action="append",
        default=[],
        metavar="INSTANCE@OFFSET",
        help="按公开数据集行号导入指定 SWE-agent 任务，可重复",
    )
    parser.add_argument("--page-size", type=int, default=40)
    parser.add_argument("--min-commands", type=int, default=20)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--rebuild-local",
        action="store_true",
        help="只使用数据包内来源记录重建已导入轨迹，不访问网络",
    )
    parser.add_argument(
        "--instance",
        action="append",
        default=[],
        help="配合 --rebuild-local 只重建指定任务，可重复使用",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.rebuild_local:
        rebuilt = rebuild_local_sources(set(args.instance) if args.instance else None)
        if args.instance:
            found = {str(item.get("instance_id") or "") for item in rebuilt}
            missing = set(args.instance) - found
            if missing:
                raise SystemExit(f"本地来源记录中没有这些任务：{sorted(missing)}")
        print(json.dumps({"rebuilt": rebuilt}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    skip = existing_instances()
    downloaded_at = dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat()

    openhands = collect_candidates(
        dataset=OPENHANDS_DATASET,
        normalizer=normalize_openhands_row,
        scan_limit=args.scan_limit,
        start_offset=args.openhands_start,
        page_size=args.page_size,
        min_commands=args.min_commands,
        skip_instances=skip,
    )[: max(0, args.openhands_count)]
    skip.update(item.instance_id for item in openhands)

    if args.swe_agent_instance:
        swe_agent = []
        for spec in args.swe_agent_instance:
            if "@" not in spec:
                raise SystemExit(f"--swe-agent-instance 格式错误：{spec}")
            instance, offset_text = spec.rsplit("@", 1)
            offset = int(offset_text)
            rows = fetch_rows(SWE_AGENT_DATASET, offset=offset, length=1)
            if len(rows) != 1:
                raise SystemExit(f"无法读取数据集第 {offset} 行")
            item = normalize_swe_agent_row(rows[0][1], rows[0][0])
            if item is None or item.instance_id != instance:
                actual = item.instance_id if item is not None else "无法解析"
                raise SystemExit(f"第 {offset} 行任务为 {actual}，不是 {instance}")
            if item.instance_id in skip:
                continue
            swe_agent.append(item)
    else:
        swe_agent = collect_candidates(
            dataset=SWE_AGENT_DATASET,
            normalizer=normalize_swe_agent_row,
            scan_limit=args.scan_limit,
            start_offset=args.swe_agent_start,
            page_size=args.page_size,
            min_commands=args.min_commands,
            skip_instances=skip,
        )[: max(0, args.swe_agent_count)]

    selected = openhands + swe_agent
    summary = [
        {
            "instance_id": item.instance_id,
            "source_format": item.source_format,
            "row_offset": item.row_offset,
            "commands": len(item.commands),
            "stages": item.stage_counts,
        }
        for item in selected
    ]
    if args.dry_run:
        print(json.dumps({"selected": summary}, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    imported = []
    imported.extend(import_items(openhands, source_prefix="openhands", downloaded_at=downloaded_at, replace=args.replace))
    imported.extend(import_items(swe_agent, source_prefix="swe-agent", downloaded_at=downloaded_at, replace=args.replace))
    print(json.dumps({"imported": imported}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except urllib.error.URLError as exc:
        print(f"download failed: {exc}", file=sys.stderr)
        raise SystemExit(2)

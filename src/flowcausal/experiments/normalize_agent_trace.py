#!/usr/bin/env python3
"""Normalize real code-agent trajectory files into FlowCausal replay traces.

FlowCausal's scheduler matrix consumes a compact command-trace schema:

    {"schema": "flowcausal.agent_trace.v1", "commands": [{"cmd": ...}, ...]}

Real agent frameworks use different output files.  SWE-agent writes ``.traj``
JSON files with a ``trajectory`` of thought/action/observation turns, while
OpenHands evaluation outputs usually contain event ``history`` records whose
runtime actions include shell commands.  This script extracts the
sandbox-visible command sequence, attaches conservative stage/critical-path
metadata, and preserves provenance so a later experiment can be audited against
the original full-agent/harness run.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import pathlib
import re
import shlex
import sys
from typing import Any


HERE = pathlib.Path(__file__).resolve().parent
WORKLOADS = HERE / "workloads"
sys.path.insert(0, str(WORKLOADS))

from agent_trace_workflow import command_from_record, normalize_stage  # noqa: E402


SCHEMA = "flowcausal.agent_trace.v1"
CONTROL_COMMANDS = {"submit", "exit", "finish", "done", "stop"}
MINI_AGENT_SHELL_TOOLS = {"bash", "shell", "run_shell", "run_command", "terminal"}
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
OPENHANDS_SHELL_TOOLS = {"execute_bash", "bash", "shell", "run_shell", "run_command", "terminal"}
OPENHANDS_EDITOR_TOOLS = {"str_replace_editor", "editor"}
SEARCH_PROBE_COMMAND_RE = re.compile(r"(^|[;&|]\s*)(grep|rg|find)\b")


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_raw(path: pathlib.Path) -> tuple[Any, str, int]:
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
        return rows, "jsonl", len(rows)
    try:
        return json.loads(text), "json", 1
    except json.JSONDecodeError as exc:
        raise SystemExit(f"invalid JSON input: {path}: {exc}") from exc


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return str(value)


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


def command_first_token(command: str) -> str:
    try:
        parts = shlex.split(command, comments=False, posix=True)
    except ValueError:
        parts = command.strip().split()
    return parts[0].strip().lower() if parts else ""


def is_control_command(command: str) -> bool:
    text = command.strip()
    first = command_first_token(text)
    if first not in CONTROL_COMMANDS:
        return False
    # ``exit 1`` and ``submit`` are agent/session controls, not repository
    # work.  A command such as ``python exit.py`` is not filtered because the
    # first token differs.
    return len(text.splitlines()) == 1


def parse_maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped or stripped[0] not in "[{":
        return value
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return value


def nested_get_dict(record: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = parse_maybe_json(record.get(key))
    return value if isinstance(value, dict) else None


def extract_raw_cwd(record: Any) -> str:
    if isinstance(record, dict):
        for key in ("cwd", "working_dir", "workdir"):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("state", "args", "action", "metadata", "extras"):
            nested = nested_get_dict(record, key)
            if nested is not None:
                cwd = extract_raw_cwd(nested)
                if cwd:
                    return cwd
    return ""


def map_cwd(raw_cwd: str, *, agent_repo_root: str) -> tuple[str, str]:
    if not raw_cwd:
        return ".", ""
    if not agent_repo_root:
        return ".", raw_cwd
    try:
        raw_path = pathlib.PurePosixPath(raw_cwd)
        root_path = pathlib.PurePosixPath(agent_repo_root)
        rel = raw_path.relative_to(root_path)
    except ValueError:
        return ".", raw_cwd
    rel_text = str(rel) or "."
    if rel_text.startswith(".."):
        return ".", raw_cwd
    return rel_text, raw_cwd


def rewrite_agent_repo_root(command: str, *, agent_repo_root: str) -> str:
    """Map agent-side absolute repo paths to replay-local relative paths.

    mini-swe-agent/SWE-bench commands commonly refer to ``/testbed`` directly,
    e.g. ``cd /testbed && pytest ...`` or ``grep foo /testbed/pkg/mod.py``.
    ``agent_trace_workflow.py`` replays commands from its prepared work tree,
    so leaving those absolute paths intact would make the trace structurally
    valid but not replayable on the host.  When the caller supplies
    ``--agent-repo-root /testbed``, replace exact root occurrences with ``.``.
    """

    root = agent_repo_root.rstrip("/")
    if not root or not root.startswith("/"):
        return command
    pattern = re.compile(rf"(?<![\w./-]){re.escape(root)}(?=$|/|[^\w./-])")
    rewritten = pattern.sub(".", command)
    if root.startswith("/workspace/"):
        # OpenHands often first views the workspace parent directory before it
        # enters the checked-out repository, e.g. ``view /workspace`` followed
        # by ``cd /workspace/<repo>``.  The replay workdir already represents
        # the repository sandbox, so map that parent view to ``.`` instead of
        # leaving an unreplayable host-absolute path.
        rewritten = re.sub(r"(?<![\w./-])/workspace(?=$|/|[^\w./-])", ".", rewritten)
    return rewritten


REPLAY_PYTHON = "${FLOWCAUSAL_REPLAY_PYTHON:-python3}"


def rewrite_python_alias(command: str) -> str:
    """Prefer a configurable replay Python for trajectory shell commands.

    Several SWE-agent/OpenHands environments expose ``python`` while lean
    host replay environments expose only ``python3``; SWE-rebench Docker images
    often keep the task dependencies in ``/opt/miniconda3/envs/testbed/bin``.
    The interpreter name is an environment alias rather than workflow
    semantics, so normalize standalone shell tokens conservatively and preserve
    the original command in ``agent_cmd`` when the rewrite changes the command
    string.  Callers can set ``FLOWCAUSAL_REPLAY_PYTHON`` to pin the container
    interpreter while host replay falls back to ``python3``.
    """

    return re.sub(r"(?<![\w./-])python3?(?=$|[;&|()\s])", REPLAY_PYTHON, command)


def is_search_probe_command(command: str) -> bool:
    """Return true for read-only shell probes where nonzero means "not found".

    Real code agents often run ``grep``/``rg``/``find`` while exploring.  A
    return code of 1 for these commands is part of the observation stream, not
    a harness failure.  Mark only INSPECT-stage search probes as allowed
    failures so TEST/BUILD failures are still caught by replay gates.
    """

    return bool(SEARCH_PROBE_COMMAND_RE.search(command))


def make_command_record(
    *,
    command: str,
    source_id: str,
    turn: int,
    raw: Any,
    args: argparse.Namespace,
    stage_override: str | None = None,
) -> dict[str, Any] | None:
    original_command = command.strip()
    if not original_command:
        return None
    if is_control_command(original_command) and not args.keep_control_commands:
        return None
    command = rewrite_agent_repo_root(original_command, agent_repo_root=args.agent_repo_root)
    command = rewrite_python_alias(command)
    cwd, raw_cwd = map_cwd(extract_raw_cwd(raw), agent_repo_root=args.agent_repo_root)
    stage = normalize_stage(stage_override, command)
    record: dict[str, Any] = {
        "id": source_id,
        "cmd": command,
        "stage": stage,
        "turn": turn,
        "cwd": cwd,
    }
    if raw_cwd:
        record["agent_working_dir"] = raw_cwd
    if command != original_command:
        record["agent_cmd"] = original_command
        record["command_rewrite"] = {
            "source": "normalizer",
            "agent_repo_root": args.agent_repo_root,
            "path_replacement": "." if args.agent_repo_root else "",
            "python_alias": (
                "python_to_flowcausal_replay_python"
                if rewrite_python_alias(original_command) != original_command
                else ""
            ),
        }
    if stage == "RETRY_PROBE":
        record["allow_failure"] = True
    if stage == "INSPECT" and is_search_probe_command(command):
        record["allow_failure"] = True
        record["allow_failure_reason"] = "inspect_search_probe"
    return record


def state_open_file(record: dict[str, Any]) -> str:
    state = nested_get_dict(record, "state")
    if state is None:
        return ""
    value = state.get("open_file")
    return value.strip() if isinstance(value, str) else ""


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


def translate_swe_agent_action(action: str, step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate SWE-agent native editor/navigation commands to replay-safe shell.

    SWE-agent trajectories mix ordinary shell commands with special commands
    implemented by the SWE-agent environment (``open``, ``edit``,
    ``find_file``...).  Replaying those strings directly through ``/bin/sh``
    would be semantically wrong.  This translator preserves the original action
    and emits conservative shell/Python equivalents that exercise the same
    sandbox-visible file-system path.
    """

    stripped = action.strip()
    if not stripped:
        return "", {}
    first_line, body = split_edit_body(stripped)
    words = parse_action_words(first_line)
    tool = words[0].strip().lower() if words else ""
    if tool not in SWE_AGENT_NATIVE_TOOLS:
        return stripped, {"agent_action_kind": "shell"}
    if tool in CONTROL_COMMANDS:
        return stripped, {"agent_action_kind": "control"}

    meta: dict[str, Any] = {
        "agent_action": stripped,
        "agent_action_kind": tool,
        "agent_action_translation": "swe_agent_native_to_shell",
    }
    open_file = state_open_file(step)

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
        return python_view_command(path, start_line=line), {**meta, "stage_override": "INSPECT"}

    if tool == "create":
        path = words[1] if len(words) >= 2 else open_file
        if not path:
            return "", meta
        return python_touch_command(path), {**meta, "stage_override": "EDIT"}

    if tool == "find_file":
        pattern = words[1] if len(words) >= 2 else "*"
        root = words[2] if len(words) >= 3 else "."
        command = f"find {shlex.quote(root)} -name {shlex.quote(pattern)} -print | head -50"
        return command, {**meta, "stage_override": "INSPECT"}

    if tool in {"search_file", "search_dir"}:
        pattern = words[1] if len(words) >= 2 else ""
        target = words[2] if len(words) >= 3 else "."
        if not pattern:
            return "", meta
        command = f"grep -R -n -- {shlex.quote(pattern)} {shlex.quote(target)} | head -80"
        return command, {**meta, "stage_override": "INSPECT", "allow_failure": True}

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

    return "", meta


def extract_flowcausal(data: Any, args: argparse.Namespace) -> tuple[list[dict[str, Any]], int]:
    records = data.get("commands", data) if isinstance(data, dict) else data
    if not isinstance(records, list):
        raise SystemExit("flowcausal format expects a JSON object/list with commands")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(records):
        if not isinstance(item, dict):
            continue
        command = command_from_record(item)
        normalized = make_command_record(
            command=command,
            source_id=str(item.get("id") or item.get("event_id") or index),
            turn=int(item.get("turn", index) or 0),
            raw=item,
            args=args,
        )
        if normalized is None:
            continue
        for key in ("allow_failure", "critical", "frontier_label"):
            if key in item:
                normalized[key] = item[key]
        out.append(normalized)
    return out, len(records)


def extract_swe_agent(data: Any, args: argparse.Namespace) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(data, dict) or not isinstance(data.get("trajectory"), list):
        raise SystemExit("swe-agent format expects a JSON object with a trajectory list")
    out: list[dict[str, Any]] = []
    for index, step in enumerate(data["trajectory"]):
        if not isinstance(step, dict):
            continue
        action = step.get("action")
        if isinstance(action, dict):
            command = command_from_record(action)
            action_meta: dict[str, Any] = {"agent_action_kind": "dict"}
        else:
            command, action_meta = translate_swe_agent_action(str(action or ""), step)
        normalized = make_command_record(
            command=command,
            source_id=str(step.get("id") or step.get("step") or f"trajectory[{index}]"),
            turn=index,
            raw=step,
            args=args,
            stage_override=action_meta.get("stage_override"),
        )
        if normalized is not None:
            for key, value in action_meta.items():
                if key != "stage_override":
                    normalized[key] = value
            if action_meta.get("allow_failure"):
                normalized["allow_failure"] = True
            out.append(normalized)
    return out, len(data["trajectory"])


def mini_tool_call_command(tool_call: Any) -> tuple[str, Any, str]:
    if not isinstance(tool_call, dict):
        return "", tool_call, ""
    function = tool_call.get("function")
    if not isinstance(function, dict):
        function = {}
    name = str(function.get("name") or tool_call.get("name") or "").strip().lower()
    if name and name not in MINI_AGENT_SHELL_TOOLS:
        return "", tool_call, name

    arguments = parse_maybe_json(
        function.get("arguments", tool_call.get("arguments", tool_call.get("args", "")))
    )
    command = ""
    if isinstance(arguments, dict):
        command = command_from_record(arguments)
    elif isinstance(arguments, str) and name in MINI_AGENT_SHELL_TOOLS:
        command = arguments
    return command, arguments, name


def mini_tool_returncodes(messages: list[Any]) -> dict[str, int]:
    returncodes: dict[str, int] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        tool_call_id = message.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            continue
        content = str(message.get("content") or "")
        match = re.search(r"<returncode>\s*(-?\d+)\s*</returncode>", content)
        if match is None:
            continue
        returncodes[tool_call_id] = int(match.group(1))
    return returncodes


def extract_mini_swe_agent(data: Any, args: argparse.Namespace) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        raise SystemExit("mini-swe-agent format expects a JSON object with a messages list")
    out: list[dict[str, Any]] = []
    raw_tool_calls = 0
    observed_returncodes = mini_tool_returncodes(data["messages"])
    for message_index, message in enumerate(data["messages"]):
        if not isinstance(message, dict):
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_index, tool_call in enumerate(tool_calls):
            raw_tool_calls += 1
            command, arguments, tool_name = mini_tool_call_command(tool_call)
            raw = {
                "message": message,
                "tool_call": tool_call,
                "arguments": arguments,
                "tool_name": tool_name,
            }
            source_id = f"messages[{message_index}].tool_calls[{tool_index}]"
            normalized = make_command_record(
                command=command,
                source_id=source_id,
                turn=message_index,
                raw=raw,
                args=args,
            )
            if normalized is None:
                continue
            tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if isinstance(tool_call_id, str) and tool_call_id.strip():
                tool_call_id = tool_call_id.strip()
                normalized["tool_call_id"] = tool_call_id
                if tool_call_id in observed_returncodes:
                    normalized["observed_returncode"] = observed_returncodes[tool_call_id]
                    if observed_returncodes[tool_call_id] != 0:
                        normalized["allow_failure"] = True
            out.append(normalized)
    return out, raw_tool_calls


def action_signature(record: dict[str, Any]) -> str:
    fields: list[str] = []
    for key in ("type", "action", "source", "event_type", "kind", "class", "cls"):
        value = record.get(key)
        if isinstance(value, str):
            fields.append(value)
        elif isinstance(value, dict):
            fields.append(action_signature(value))
    return " ".join(fields).lower()


def looks_like_runtime_command(record: dict[str, Any]) -> bool:
    signature = action_signature(record)
    if any(token in signature for token in ("cmdrun", "cmd_run", "bash", "shell", "runaction")):
        return True
    if any(token in signature for token in ("observation", "obs", "message")):
        return False
    # Some OpenHands dumps store ``{"action": "run", "args": {"command": ...}}``.
    return "run" in signature and "command" in stable_json(record).lower()


def openhands_command_from_event(event: Any) -> str:
    if isinstance(event, dict):
        action = event.get("action")
        if isinstance(action, dict) and looks_like_runtime_command(action):
            command = command_from_record(action)
            if command:
                return command
        if looks_like_runtime_command(event):
            command = command_from_record(event)
            if command:
                return command
        args = event.get("args")
        if isinstance(args, dict) and looks_like_runtime_command({**event, **args}):
            command = command_from_record(args)
            if command:
                return command
    elif isinstance(event, list):
        for item in event:
            command = openhands_command_from_event(item)
            if command:
                return command
    return ""


def openhands_tool_call_command(tool_call: Any) -> tuple[str, dict[str, Any]]:
    if not isinstance(tool_call, dict):
        return "", {}
    function = tool_call.get("function")
    if not isinstance(function, dict):
        function = {}
    name = str(function.get("name") or tool_call.get("name") or "").strip().lower()
    arguments = parse_maybe_json(function.get("arguments", tool_call.get("arguments", tool_call.get("args", ""))))
    if not isinstance(arguments, dict):
        arguments = {}
    meta = {
        "openhands_tool": name,
        "openhands_tool_arguments": arguments,
    }
    if name in OPENHANDS_SHELL_TOOLS:
        command = command_from_record(arguments)
        return command, {**meta, "agent_action_kind": "shell"}
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


def openhands_commands_from_message(message: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    out: list[tuple[str, dict[str, Any]]] = []
    for tool_index, tool_call in enumerate(tool_calls):
        command, meta = openhands_tool_call_command(tool_call)
        if command:
            meta = dict(meta)
            meta["tool_call_index"] = tool_index
            tool_call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
            if isinstance(tool_call_id, str) and tool_call_id.strip():
                meta["tool_call_id"] = tool_call_id.strip()
            out.append((command, meta))
    return out


def openhands_tool_returncodes(events: list[Any]) -> dict[str, int]:
    returncodes: dict[str, int] = {}
    for event in events:
        if not isinstance(event, dict) or event.get("role") != "tool":
            continue
        tool_call_id = event.get("tool_call_id")
        if not isinstance(tool_call_id, str) or not tool_call_id.strip():
            continue
        content = str(event.get("content") or "")
        # OpenHands observations commonly contain both phrases below.
        match = re.search(r"finished with exit code\s+(-?\d+)", content, re.IGNORECASE)
        if match is None:
            match = re.search(r"completed with exit code\s+(-?\d+)", content, re.IGNORECASE)
        if match is None:
            continue
        returncodes[tool_call_id.strip()] = int(match.group(1))
    return returncodes


def iter_openhands_events(data: Any) -> list[Any]:
    if isinstance(data, dict):
        row = data.get("row")
        if isinstance(row, dict):
            return iter_openhands_events(row)
        for key in ("history", "events", "trajectory"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        return [data]
    if isinstance(data, list):
        if all(isinstance(row, dict) and isinstance(row.get("history"), list) for row in data):
            events: list[Any] = []
            for row in data:
                events.extend(row["history"])
            return events
        return data
    return []


def extract_openhands(data: Any, args: argparse.Namespace) -> tuple[list[dict[str, Any]], int]:
    events = iter_openhands_events(data)
    out: list[dict[str, Any]] = []
    observed_returncodes = openhands_tool_returncodes(events)
    for index, event in enumerate(events):
        commands: list[tuple[str, dict[str, Any]]] = []
        if isinstance(event, dict):
            commands.extend(openhands_commands_from_message(event))
        if not commands:
            command = openhands_command_from_event(event)
            if command:
                commands.append((command, {}))
        for command_index, (command, meta) in enumerate(commands):
            normalized = make_command_record(
                command=command,
                source_id=f"history[{index}].tool_calls[{command_index}]",
                turn=len(out),
                raw=event,
                args=args,
                stage_override=meta.get("stage_override"),
            )
            if normalized is not None:
                for key, value in meta.items():
                    if key != "stage_override":
                        normalized[key] = value
                tool_call_id = meta.get("tool_call_id")
                if isinstance(tool_call_id, str) and tool_call_id in observed_returncodes:
                    normalized["observed_returncode"] = observed_returncodes[tool_call_id]
                    if observed_returncodes[tool_call_id] != 0:
                        normalized["allow_failure"] = True
                out.append(normalized)
    return out, len(events)


def infer_format(data: Any, input_kind: str) -> str:
    if isinstance(data, dict):
        if isinstance(data.get("commands"), list):
            return "flowcausal"
        if isinstance(data.get("messages"), list):
            trajectory_format = str(data.get("trajectory_format") or "").lower()
            if "mini-swe-agent" in trajectory_format or data.get("messages"):
                return "mini-swe-agent"
        if isinstance(data.get("trajectory"), list):
            if any(
                isinstance(row, dict)
                and isinstance(row.get("tool_calls"), list)
                and any(
                    isinstance(call, dict)
                    and str(call.get("function", {}).get("name") if isinstance(call.get("function"), dict) else "").lower()
                    in OPENHANDS_SHELL_TOOLS | OPENHANDS_EDITOR_TOOLS
                    for call in row.get("tool_calls", [])
                )
                for row in data.get("trajectory", [])
            ):
                return "openhands"
            return "swe-agent"
        if isinstance(data.get("history"), list) or isinstance(data.get("events"), list):
            return "openhands"
    if isinstance(data, list):
        if any(isinstance(row, dict) and isinstance(row.get("history"), list) for row in data):
            return "openhands"
        if any(isinstance(row, dict) and ("action" in row and "observation" in row) for row in data):
            return "swe-agent"
    return "flowcausal" if input_kind == "json" else "openhands"


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
    return foreground[-1] if foreground else (len(commands) - 1 if commands else None)


def normalize_trace(data: Any, input_kind: str, args: argparse.Namespace) -> tuple[str, list[dict[str, Any]], int]:
    fmt = args.format
    if fmt == "auto":
        fmt = infer_format(data, input_kind)
    if fmt == "swe-agent":
        commands, raw_count = extract_swe_agent(data, args)
    elif fmt == "mini-swe-agent":
        commands, raw_count = extract_mini_swe_agent(data, args)
    elif fmt == "openhands":
        commands, raw_count = extract_openhands(data, args)
    elif fmt == "flowcausal":
        commands, raw_count = extract_flowcausal(data, args)
    else:
        raise SystemExit(f"unsupported format: {fmt}")
    critical_index = choose_critical(commands, args.critical_policy)
    if critical_index is not None:
        commands[critical_index]["critical"] = True
        if args.emit_heuristic_frontier_labels and "frontier_label" not in commands[critical_index]:
            commands[critical_index]["frontier_label"] = {
                "source": "flowcausal-normalizer-heuristic",
                "role": "foreground-gate-candidate",
                "critical_policy": args.critical_policy,
            }
    return fmt, commands, raw_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize SWE-agent/mini-swe-agent/OpenHands/FlowCausal trajectories into replayable FlowCausal traces",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", required=True, help="raw .traj/.json/.jsonl trajectory file")
    parser.add_argument("--output", required=True, help="output flowcausal.agent_trace.v1 JSON file")
    parser.add_argument(
        "--format",
        choices=["auto", "swe-agent", "mini-swe-agent", "openhands", "flowcausal"],
        default="auto",
    )
    parser.add_argument("--source", help="logical source label stored in the normalized trace")
    parser.add_argument("--instance-id", help="benchmark instance id stored in the normalized trace")
    parser.add_argument(
        "--agent-repo-root",
        default="",
        help="agent-side absolute repo root; working_dir below it is mapped to replay cwd",
    )
    parser.add_argument(
        "--critical-policy",
        choices=["last-test", "last-foreground", "explicit-only"],
        default="last-test",
        help="how to mark critical=true when the raw trace has no explicit critical command",
    )
    parser.add_argument(
        "--emit-heuristic-frontier-labels",
        action="store_true",
        help="attach a non-oracle heuristic frontier_label to the chosen critical command",
    )
    parser.add_argument("--keep-control-commands", action="store_true", help="keep submit/exit-style agent controls")
    parser.add_argument("--created-at", help="override creation timestamp for deterministic tests")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = pathlib.Path(args.input).resolve()
    output_path = pathlib.Path(args.output).resolve()
    data, input_kind, raw_rows = read_raw(input_path)
    fmt, commands, raw_count = normalize_trace(data, input_kind, args)
    if not commands:
        raise SystemExit(f"no replayable commands extracted from {input_path}")

    created_at = args.created_at or _dt.datetime.now(_dt.UTC).isoformat()
    source = args.source or fmt
    instance_id = args.instance_id or (
        str(data.get("instance_id") or data.get("task_id") or data.get("id") or "")
        if isinstance(data, dict)
        else ""
    )
    if not instance_id and isinstance(data, dict) and isinstance(data.get("row"), dict):
        row = data["row"]
        instance_id = str(row.get("instance_id") or row.get("task_id") or row.get("id") or "")
    payload = {
        "schema": SCHEMA,
        "source": source,
        "source_format": fmt,
        "instance_id": instance_id,
        "created_at": created_at,
        "commands": commands,
        "provenance": {
            "input_file": str(input_path),
            "input_sha256": file_sha256(input_path),
            "input_kind": input_kind,
            "raw_rows": raw_rows,
            "raw_events_or_steps": raw_count,
            "normalized_commands": len(commands),
            "dropped_or_non_command_events": max(0, raw_count - len(commands)),
            "normalizer": "flowcausal/experiments/normalize_agent_trace.py",
            "critical_policy": args.critical_policy,
            "agent_repo_root": args.agent_repo_root,
            "command_path_rewrite": "agent_repo_root_to_relative" if args.agent_repo_root else "",
            "notes": [
                "This file preserves sandbox-visible commands for scheduler replay.",
                "It is not by itself a full-agent endpoint or frontier-correctness oracle.",
            ],
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output_path), "format": fmt, "commands": len(commands)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

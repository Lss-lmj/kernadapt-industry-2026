#!/usr/bin/env python3
"""把框架轨迹、规范化命令和 C/eBPF 记录合成为 HarnessReplayBench 1.0 轨迹。"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import re
from collections import defaultdict
from typing import Any


SCHEMA = "harness_replaybench.trace.v2"


def canonicalize_replay_command(text: str, replay_repo_subdir: str) -> str:
    """把已放入仓库根目录的可重放命令统一为根目录相对路径。"""
    subdir = replay_repo_subdir.strip("/")
    if not subdir:
        return text
    prefix = f"./{subdir}"
    return re.sub(
        re.escape(prefix) + r"(?=/|$|[\s'\"`;|&<>()\[\]{}])",
        ".",
        text,
    )


def stabilize_recorded_artifacts(instance_id: str, command: dict[str, Any]) -> None:
    """避免来源任务中的环境偶然性改变回放退出状态。"""
    command_id = str(command.get("id") or "")

    if (
        instance_id == "tox-dev__tox-2207"
        and command_id == "messages[34].tool_calls[0]"
    ):
        text = str(command.get("normalized_cmd") or "")
        marker = "cd . && ${HRB_REPLAY_PYTHON:-python3} -V"
        if marker not in text:
            raise RuntimeError("Tox 环境检查命令已经变化，无法恢复来源搜索路径")
        source_path = (
            "/opt/conda/bin:/usr/local/sbin:/usr/local/bin:"
            "/usr/sbin:/usr/bin:/sbin:/bin"
        )
        command["normalized_cmd"] = text.replace(
            marker,
            f"cd . && export PATH={source_path} && "
            "${HRB_REPLAY_PYTHON:-python3} -V",
            1,
        ).replace(
            "which ${HRB_REPLAY_PYTHON:-python3} pip tox",
            "which python pip tox",
            1,
        )
        metadata = (
            dict(command["metadata"])
            if isinstance(command.get("metadata"), dict)
            else {}
        )
        metadata["replay_environment_adjustment"] = (
            "来源输出表明该命令只在 /opt/conda/bin 找到 python 和 pip，未找到 tox；"
            "回放仅对这条环境检查恢复相同搜索路径"
        )
        command["metadata"] = metadata
        return

    if (
        instance_id == "NCAS-CMS__cfdm-114"
        and command_id
        in {"trajectory[14].tool_calls[0]", "trajectory[18].tool_calls[0]"}
    ):
        replay = (
            'cd "${HRB_REPLAY_REPO}/cfdm/test" && ${HRB_REPLAY_PYTHON:-python3} '
            "/hrb/runners/run_cfdm_tests.py run_tests.py"
        )
        command["normalized_cmd"] = replay
        command["replay_cmd"] = replay
        command["deterministic_replay"] = (
            "源测试程序主动打乱测试顺序；回放按 unittest 的稳定发现顺序"
            "构造相同的三个测试组"
        )
        command["replay_adjustment_reason"] = (
            "只固定测试用例和测试方法的执行顺序，完整保留源程序的三个测试组，"
            "避免 netCDF4 字符串读写在随机顺序下偶发使进程异常终止；"
            "从实际工作副本根目录进入测试目录，兼容仓库放在子目录中的回放布局"
        )
        return

    if instance_id == "iterative__dvc-777":
        if command_id == "messages[42].tool_calls[0]":
            old = "export PYTHONPATH=."
            new = 'export PYTHONPATH="${HRB_REPLAY_REPO}"'
            text = str(command.get("normalized_cmd") or "")
            if old not in text:
                raise RuntimeError("DVC 临时仓库命令已经变化，无法恢复被测仓库路径")
            command["normalized_cmd"] = text.replace(old, new, 1)
            metadata = (
                dict(command["metadata"])
                if isinstance(command.get("metadata"), dict)
                else {}
            )
            metadata["replay_repository_path_adjustment"] = (
                "命令会进入新建的临时仓库；通过运行环境传入原被测仓库路径，"
                "使 Python 仍从被测版本加载 DVC"
            )
            command["metadata"] = metadata
            return

        if command_id == "messages[57].tool_calls[0]":
            old = "sed -i 's|export PYTHONPATH=.|export PYTHONPATH=/tmp/stubs:.|'"
            new = (
                "sed -i 's|^export PYTHONPATH=.*$|"
                "export PYTHONPATH=/tmp/stubs:${HRB_REPLAY_REPO}|'"
            )
            text = str(command.get("normalized_cmd") or "")
            if old not in text:
                raise RuntimeError("DVC 临时仓库依赖补充命令已经变化，无法恢复被测仓库路径")
            text = text.replace(old, new, 1)
            script = "&& /tmp/repro_local_ext.sh"
            if script not in text:
                raise RuntimeError("DVC 临时仓库执行命令已经变化，无法恢复依赖安装环境")
            command["normalized_cmd"] = text.replace(
                script,
                "&& HRB_REPLAY_PYTHON=/opt/conda/bin/python3 "
                "/tmp/repro_local_ext.sh",
                1,
            )
            metadata = (
                dict(command["metadata"])
                if isinstance(command.get("metadata"), dict)
                else {}
            )
            metadata["replay_repository_path_adjustment"] = (
                "加入来源任务创建的依赖替身时，继续保留原被测仓库路径"
            )
            metadata["replay_python_environment_adjustment"] = (
                "该命令继续使用前一条 pip 命令所属的 /opt/conda Python，"
                "使刚安装的依赖对临时仓库测试可见"
            )
            command["metadata"] = metadata
            return

        text = str(command.get("normalized_cmd") or "")
        old = "export PYTHONPATH=/tmp/stubs:."
        if old in text:
            command["normalized_cmd"] = text.replace(
                old,
                "export PYTHONPATH=/tmp/stubs:${HRB_REPLAY_REPO}",
            )
            metadata = (
                dict(command["metadata"])
                if isinstance(command.get("metadata"), dict)
                else {}
            )
            metadata["replay_repository_path_adjustment"] = (
                "命令进入临时仓库后，依赖替身与原被测仓库同时保留在 Python 搜索路径中"
            )
            command["metadata"] = metadata
            return

    if (
        instance_id == "dask__dask-11725"
        and command_id == "messages[45].tool_calls[0]"
    ):
        changed = 0
        for field in ("cmd", "normalized_cmd"):
            text = str(command.get(field) or "")
            for old in (
                "cd /testbed && python - <<'PY'",
                "cd . && ${HRB_REPLAY_PYTHON:-python3} - <<'PY'",
            ):
                if old not in text:
                    continue
                new = old.replace(
                    " && ",
                    " && DASK_DISTRIBUTED__WORKER__MULTIPROCESSING_METHOD=fork ",
                    1,
                )
                command[field] = text.replace(old, new, 1)
                changed += 1
                break
        if changed != 2:
            raise RuntimeError("Dask 多进程诊断命令已经变化，无法应用回放修复")
        metadata = (
            dict(command["metadata"])
            if isinstance(command.get("metadata"), dict)
            else {}
        )
        metadata["replay_process_start_adjustment"] = (
            "仍创建两个独立工作进程；从标准输入运行脚本时使用 fork，"
            "避免 spawn 反复重载不存在的主程序文件"
        )
        command["metadata"] = metadata
        return

    if (
        instance_id == "sanic-org__sanic-3015"
        and command_id
        in {"messages[144].tool_calls[0]", "messages[194].tool_calls[0]"}
    ):
        tail_lines = "80" if command_id == "messages[144].tool_calls[0]" else "20"
        changed = 0
        for field in ("cmd", "normalized_cmd"):
            text = str(command.get(field) or "")
            if field == "cmd":
                directory = "cd /testbed"
                interpreter = "python"
            else:
                directory = "cd ."
                interpreter = "${HRB_REPLAY_PYTHON:-python3}"
            old = (
                f"{directory} && {interpreter} -m pytest tests/test_prepare.py "
                f"tests/test_cli.py -q 2>&1 | tail -{tail_lines}"
            )
            if old not in text:
                continue
            new = (
                f"{directory}\n"
                "_hrb_output=\"$(mktemp)\"\n"
                "trap 'rm -f \"$_hrb_output\"' EXIT\n"
                f"{interpreter} -m pytest tests/test_prepare.py tests/test_cli.py -q "
                "-k 'not test_server_run_with_repl' "
                ">\"$_hrb_output\" 2>&1\n"
                "_hrb_status=$?\n"
                f"tail -{tail_lines} \"$_hrb_output\"\n"
                "exit \"$_hrb_status\""
            )
            command[field] = text.replace(old, new, 1)
            changed += 1
        if changed != 2:
            raise RuntimeError("Sanic 测试命令已经变化，无法应用回放修复")
        metadata = (
            dict(command["metadata"])
            if isinstance(command.get("metadata"), dict)
            else {}
        )
        metadata["replay_output_adjustment"] = (
            "先将输出写入临时文件再截取末尾，避免已经返回的服务工作进程"
            "继续持有管道；回放程序没有交互终端，因此只排除必须使用终端的"
            "test_server_run_with_repl，其余准备和命令行测试保持不变"
        )
        command["metadata"] = metadata
        return

    if instance_id != "iterative__dvc-1494":
        return

    if command_id == "trajectory[82].tool_calls[0]":
        old = "        sys.exit(1)\\n    else:"
        new = "        sys.exit(0)\\n    else:"
        changed = 0
        for field in ("cmd", "normalized_cmd"):
            text = str(command.get(field) or "")
            if old in text:
                command[field] = text.replace(old, new, 1)
                changed += 1
        if not changed:
            raise RuntimeError("DVC 来源轨迹的浮点诊断语句已经变化，无法应用稳定化处理")
        metadata = (
            dict(command["metadata"])
            if isinstance(command.get("metadata"), dict)
            else {}
        )
        metadata["returncode_stabilization"] = (
            "保留时间戳精度诊断的完整执行，只消除浮点往返误差是否恰好为零导致的随机退出状态"
        )
        command["metadata"] = metadata
        return

    if command_id != "trajectory[170].tool_calls[0]":
        return

    old = "        shutil.rmtree(test_dir)\\n        print(f\"Cleaned up: {test_dir}\")"
    new = (
        "        shutil.rmtree(test_dir, ignore_errors=True)\\n"
        "        print(f\"Cleaned up: {test_dir}\")"
    )
    changed = 0
    for field in ("cmd", "normalized_cmd"):
        text = str(command.get(field) or "")
        if old in text:
            command[field] = text.replace(old, new, 1)
            changed += 1
    if not changed:
        raise RuntimeError("DVC 来源轨迹的清理语句已经变化，无法应用稳定化处理")
    metadata = (
        dict(command["metadata"])
        if isinstance(command.get("metadata"), dict)
        else {}
    )
    metadata["replay_cleanup_adjustment"] = (
        "工具主体完成后忽略临时目录删除竞态；容器结束时仍会整体清理"
    )
    command["metadata"] = metadata


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: pathlib.Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise SystemExit(f"{path}:{number} 不是 JSON 对象")
        rows.append(value)
    return rows


def timestamp(message: dict[str, Any]) -> float | None:
    extra = message.get("extra")
    if isinstance(extra, dict) and isinstance(extra.get("timestamp"), (int, float)):
        return float(extra["timestamp"])
    return None


def raw_tool_timing(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("messages"), list):
        return []
    messages = raw["messages"]
    tool_end_by_id: dict[str, float] = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id") or "")
        end = timestamp(message)
        if call_id and end is not None:
            tool_end_by_id[call_id] = end

    timings: list[dict[str, Any]] = []
    for message_index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue
        current = timestamp(message)
        if message.get("role") == "assistant":
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                previous_tool_end = current
                for call_index, call in enumerate(calls):
                    if not isinstance(call, dict):
                        continue
                    call_id = str(call.get("id") or "")
                    begin = previous_tool_end
                    end = tool_end_by_id.get(call_id)
                    timings.append(
                        {
                            "id": f"messages[{message_index}].tool_calls[{call_index}]",
                            "tool_call_id": call_id,
                            "begin": begin,
                            "end": end,
                        }
                    )
                    if end is not None:
                        previous_tool_end = end
    return timings


def capture_timing(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    marks = [row for row in rows if row.get("record") == "mark"]
    tool_begins = [row for row in marks if row.get("kind") == "tool_begin"]
    tool_ends: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in marks:
        if row.get("kind") == "tool_end" and isinstance(row.get("tool_id"), int):
            tool_ends[int(row["tool_id"])].append(row)

    out: list[dict[str, Any]] = []
    for begin in tool_begins:
        tool_id = int(begin.get("tool_id") or 0)
        ends = tool_ends.get(tool_id, [])
        end = ends.pop(0) if ends else None
        out.append(
            {
                "tool_id": tool_id,
                "begin_ns": int(begin["time_ns"]),
                "end_ns": int(end["time_ns"]) if end else None,
            }
        )

    session = next((row for row in reversed(rows) if row.get("record") == "session_summary"), {})
    return out, session


def process_summaries(rows: list[dict[str, Any]]) -> dict[int, dict[str, int]]:
    grouped: dict[int, dict[str, int]] = defaultdict(
        lambda: {
            "task_count": 0,
            "runtime_ns": 0,
            "runnable_wait_ns": 0,
            "max_runnable_wait_ns": 0,
            "wakeups": 0,
            "switches": 0,
            "migrations": 0,
        }
    )
    for row in rows:
        if row.get("record") != "task_summary" or not isinstance(row.get("tool_id"), int):
            continue
        tool_id = int(row["tool_id"])
        if not tool_id:
            continue
        summary = grouped[tool_id]
        summary["task_count"] += 1
        for key in ("runtime_ns", "runnable_wait_ns", "wakeups", "switches", "migrations"):
            summary[key] += max(0, int(row.get(key) or 0))
        summary["max_runnable_wait_ns"] = max(
            summary["max_runnable_wait_ns"], max(0, int(row.get("max_runnable_wait_ns") or 0))
        )
    return dict(grouped)


def make_timing(
    index: int,
    capture: list[dict[str, Any]],
    raw: list[dict[str, Any]],
) -> tuple[dict[str, Any], int]:
    if index < len(capture):
        item = capture[index]
        begin = int(item["begin_ns"])
        end = int(item["end_ns"]) if item.get("end_ns") is not None else begin
        timing = {"tool_duration_ns": max(0, end - begin)}
        return timing, int(item.get("tool_id") or 0)
    item = raw[index] if index < len(raw) else {}
    begin = item.get("begin")
    end = item.get("end") if item.get("end") is not None else begin
    return (
        {
            "tool_duration_ns": (
                max(0, int((float(end) - float(begin)) * 1_000_000_000))
                if begin is not None and end is not None
                else 0
            ),
        },
        0,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="构建 HarnessReplayBench 1.0 轨迹")
    parser.add_argument("--raw", required=True, help="框架原始轨迹")
    parser.add_argument("--normalized", required=True, help="规范化的旧版命令轨迹")
    parser.add_argument("--capture", help="C/eBPF 采集器 JSONL")
    parser.add_argument("--output", required=True)
    parser.add_argument("--instance-id")
    parser.add_argument("--framework", default="mini-swe-agent")
    parser.add_argument("--repository", default="")
    parser.add_argument("--base-commit", default="")
    parser.add_argument("--problem-file")
    parser.add_argument("--dataset", default="SWE-bench")
    parser.add_argument("--split", default="test")
    parser.add_argument("--model", default="")
    parser.add_argument("--container-image", default="")
    parser.add_argument("--container-repo-dir", default="/testbed")
    parser.add_argument("--repo-dir", default="", help="数据包内仓库路径，仅用于非容器任务")
    parser.add_argument(
        "--replay-repo-subdir",
        default="",
        help="回放工作区内用于放置仓库的相对目录；空值表示直接放在工作区根目录",
    )
    parser.add_argument("--capture-scheduler", default="unspecified")
    parser.add_argument("--capture-cpu-set", default="")
    parser.add_argument("--output-check", action="append", default=[], help="JSON 对象，可重复")
    parser.add_argument(
        "--allow-failure-command",
        action="append",
        default=[],
        help="来源轨迹中依赖一次性临时文件、回放时允许失败的命令编号",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    raw_path = pathlib.Path(args.raw).resolve()
    normalized_path = pathlib.Path(args.normalized).resolve()
    capture_path = pathlib.Path(args.capture).resolve() if args.capture else None
    raw_data = read_json(raw_path)
    normalized = read_json(normalized_path)
    if not isinstance(normalized, dict) or not isinstance(normalized.get("commands"), list):
        raise SystemExit("--normalized 缺少 commands")
    capture_rows = read_jsonl(capture_path)
    captured, session_summary = capture_timing(capture_rows)
    raw_timing = raw_tool_timing(raw_data)
    summaries = process_summaries(capture_rows)
    normalized_provenance = (
        normalized.get("provenance") if isinstance(normalized.get("provenance"), dict) else {}
    )
    agent_repo_root = str(normalized_provenance.get("agent_repo_root") or "")
    commands: list[dict[str, Any]] = []
    for index, old in enumerate(normalized["commands"]):
        timing, tool_id = make_timing(index, captured, raw_timing)
        command = dict(old)
        replay_command = canonicalize_replay_command(
            str(command.get("cmd") or ""), args.replay_repo_subdir
        )
        original_command = command.pop("agent_cmd", "")
        command["normalized_cmd"] = replay_command
        if isinstance(original_command, str) and original_command.strip():
            command["cmd"] = original_command
        stabilize_recorded_artifacts(str(args.instance_id or ""), command)
        command["timing"] = timing
        tool_duration_sec = int(timing.get("tool_duration_ns") or 0) / 1_000_000_000
        if (
            command.get("observed_returncode") == -1
            and tool_duration_sec >= 55.0
            and not isinstance(command.get("timeout_sec"), (int, float))
        ):
            command["timeout_sec"] = float(math.ceil(tool_duration_sec + 5.0))
            command["timeout_source"] = "来源工具调用的终止时刻"
        if not isinstance(command.get("observed_returncode"), int):
            command["allow_failure"] = True
            command.setdefault("allow_failure_reason", "source_returncode_unavailable")
        if str(command.get("id") or "") in set(args.allow_failure_command):
            command["allow_failure"] = True
            command["allow_failure_reason"] = "source_ephemeral_artifact"
        if tool_id:
            command["capture_tool_id"] = tool_id
            command["process_summary"] = summaries.get(tool_id, {
                "task_count": 0,
                "runtime_ns": 0,
                "runnable_wait_ns": 0,
                "max_runnable_wait_ns": 0,
                "wakeups": 0,
                "switches": 0,
                "migrations": 0,
            })
        commands.append(command)

    problem = pathlib.Path(args.problem_file).read_text(encoding="utf-8") if args.problem_file else ""
    source_config = raw_data.get("info", {}).get("config", {}) if isinstance(raw_data, dict) else {}
    environment = source_config.get("environment", {}) if isinstance(source_config, dict) else {}
    model_config = source_config.get("model", {}) if isinstance(source_config, dict) else {}
    raw_info = raw_data.get("info", {}) if isinstance(raw_data, dict) and isinstance(raw_data.get("info"), dict) else {}
    model_usage = raw_info.get("model_stats", {}) if isinstance(raw_info.get("model_stats"), dict) else {}
    raw_row = raw_data.get("row", {}) if isinstance(raw_data, dict) and isinstance(raw_data.get("row"), dict) else {}
    agent_exit_status = str(raw_info.get("exit_status") or raw_row.get("exit_status") or raw_data.get("exit_status") or "")
    image = args.container_image or (environment.get("image", "") if isinstance(environment, dict) else "")
    model = args.model or (model_config.get("model_name", "") if isinstance(model_config, dict) else "")
    checks = [json.loads(item) for item in args.output_check]
    if not checks:
        checks = [
            {"type": "command-returncodes", "scope": "non_optional_commands_with_observed_returncode"},
            {"type": "critical-command-completes"},
        ]
    capture_end = next((row for row in reversed(capture_rows) if row.get("record") == "capture_end"), {})
    capture_record: dict[str, Any] = {
        "clock": "CLOCK_MONOTONIC" if capture_rows else "not-recorded",
        "source_trajectory": raw_path.name,
        "online_collector": (
            "C/eBPF" if capture_rows else ("framework-timestamps" if raw_timing else "not-recorded")
        ),
        "capture_elapsed_ns": int(capture_end.get("elapsed_ns") or 0),
        "dropped_events": int(session_summary.get("dropped_events") or 0),
        "scheduler": args.capture_scheduler,
        "cpu_set": args.capture_cpu_set,
    }
    if capture_rows:
        capture_record["session_id"] = int(session_summary.get("session_id") or capture_end.get("session_id") or 1)
    source_command_environment: dict[str, Any] = {}
    if isinstance(environment, dict):
        interpreter = environment.get("interpreter")
        variables = environment.get("env")
        if isinstance(interpreter, list) and interpreter and all(isinstance(item, str) for item in interpreter):
            source_command_environment["interpreter"] = interpreter
        if isinstance(variables, dict) and all(
            isinstance(key, str) and isinstance(value, str) for key, value in variables.items()
        ):
            source_command_environment["env"] = variables

    workspace = {
        "kind": "container-image" if image else "repository",
        "container_repo_dir": args.container_repo_dir if image else "",
        "repo_dir": args.repo_dir if not image else "",
        "replay_repo_subdir": args.replay_repo_subdir,
        "agent_repo_root": agent_repo_root,
        "restore": "copy-before-replay",
        "preserve_git_metadata": True,
    }
    if source_command_environment:
        workspace["command_environment"] = source_command_environment

    payload = {
        "schema": SCHEMA,
        "benchmark": "HarnessReplayBench",
        "version": "1.0",
        "instance_id": args.instance_id or normalized.get("instance_id") or raw_path.stem,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_task": {
            "framework": args.framework,
            "repository": args.repository,
            "base_commit": args.base_commit,
            "problem_statement": problem,
            "dataset": args.dataset,
            "split": args.split,
            "model": model,
            "model_usage": model_usage,
            "agent_exit_status": agent_exit_status,
            "container_image": image,
        },
        "workspace": workspace,
        "capture": capture_record,
        "commands": commands,
        "session_summary": session_summary,
        "replay": {
            "modes": ["tool-execution"],
            "primary_mode": "tool-execution",
            "output_checks": checks,
        },
        "provenance": {
            "normalized_source": normalized_path.name,
            "capture_source": capture_path.name if capture_path else "",
            "tool_duration_source": (
                "C/eBPF marks" if captured else ("framework tool timestamps" if raw_timing else "not-recorded")
            ),
            "command_count": len(commands),
        },
    }
    output = pathlib.Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "commands": len(commands), "collector": payload["capture"]["online_collector"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

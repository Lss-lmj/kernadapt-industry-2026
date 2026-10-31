#!/usr/bin/env python3
"""调度器无关的 HarnessReplayBench 1.0 单任务回放程序。"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, TextIO


_FLOWCAUSAL_CLIENTS: dict[pathlib.Path, socket.socket] = {}
_FLOWCAUSAL_SEQUENCES: dict[pathlib.Path, int] = {}
_FLOWCAUSAL_GENERATIONS: dict[tuple[pathlib.Path, int], int] = {}


def stable_id(text: str) -> int:
    """与 C 控制程序使用相同的 64 位会话标识算法。"""
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


@dataclass
class Command:
    index: int
    identifier: str
    text: str
    cwd: str
    stage: str
    observed_returncode: int | None
    allow_failure: bool
    critical: bool
    asynchronous: bool
    wait_for: tuple[str, ...]
    timeout_sec: float | None
    manual_progress: bool


@dataclass
class Running:
    command: Command
    process: subprocess.Popen[str]
    started_ns: int
    tool_gate: pathlib.Path | None = None
    pidfd: int | None = None
    wait_event: threading.Event | None = None
    wait_thread: threading.Thread | None = None
    returncode: int | None = None
    output_start: int = 0
    output_file: TextIO | None = None


def open_process_handle(process: subprocess.Popen[str]) -> int | None:
    """返回可由内核通知退出的进程句柄；旧内核再退回轮询等待。"""
    pidfd_open = getattr(os, "pidfd_open", None)
    if pidfd_open is None:
        return None
    try:
        return int(pidfd_open(process.pid, 0))
    except OSError:
        return None


def close_process_handle(running: Running) -> None:
    if running.pidfd is None:
        return
    try:
        os.close(running.pidfd)
    except OSError:
        pass
    running.pidfd = None


def start_process_wait(running: Running) -> None:
    """在容器不允许 pidfd_open 时，用阻塞 waitpid 线程接收退出通知。"""
    if running.pidfd is not None:
        return
    running.wait_event = threading.Event()

    def reap() -> None:
        running.returncode = running.process.wait()
        assert running.wait_event is not None
        running.wait_event.set()

    running.wait_thread = threading.Thread(target=reap, daemon=True)
    running.wait_thread.start()


def wait_process(running: Running, timeout: float) -> int:
    """等待进程退出，同时避免 Popen.wait(timeout) 的指数退避误差。"""
    if running.pidfd is None:
        if running.wait_event is None:
            return running.process.wait(timeout=timeout)
        if not running.wait_event.wait(timeout):
            raise subprocess.TimeoutExpired(running.process.args, timeout)
        assert running.returncode is not None
        return running.returncode
    poller = select.poll()
    poller.register(running.pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    timeout_ms = max(1, math.ceil(timeout * 1000))
    if not poller.poll(timeout_ms):
        raise subprocess.TimeoutExpired(running.process.args, timeout)
    return running.process.wait()


def emit(out: TextIO, kind: str, **values: Any) -> None:
    record = {"record": kind, "time_ns": time.monotonic_ns(), **values}
    out.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    out.flush()


def parse_env(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise SystemExit(f"环境变量必须使用 KEY=VALUE：{item}")
        key, value = item.split("=", 1)
        if not key:
            raise SystemExit("环境变量名不能为空")
        result[key] = value
    return result


def safe_working_dir(root: pathlib.Path, value: str) -> pathlib.Path:
    candidate = (root / (value or ".")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"命令工作目录越出任务工作区：{value}") from exc
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def unlink_if_exists(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def string_list(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def load_commands(trace: dict[str, Any]) -> list[Command]:
    commands: list[Command] = []
    for index, value in enumerate(trace.get("commands", [])):
        if not isinstance(value, dict) or not str(value.get("cmd") or "").strip():
            raise SystemExit(f"第 {index} 条命令无效")
        # cmd 是采集时由智能体实际提交给工具运行环境的命令。它所用
        # 的解释器、路径和 shell 行为都是负载的一部分，回放时不得用
        # normalized_cmd 全局替换。只有公开轨迹缺少临时文件、把中断
        # 记成普通命令，或源程序主动打乱测试顺序等已经查明的问题，
        # 才允许显式提供 replay_cmd；同时必须写明修复原因。
        replay_override = value.get("replay_cmd")
        if replay_override is not None and not str(
            value.get("replay_adjustment_reason") or ""
        ).strip():
            raise SystemExit(f"第 {index} 条 replay_cmd 缺少修复原因")
        replay_text = str(replay_override if replay_override is not None else value["cmd"])
        if not replay_text.strip():
            raise SystemExit(f"第 {index} 条可重放命令无效")
        timing = value.get("timing") if isinstance(value.get("timing"), dict) else {}
        frontier = (
            value.get("frontier_label")
            if isinstance(value.get("frontier_label"), dict)
            else {}
        )
        frontier_source = str(frontier.get("source") or "").lower()
        # 只有人工逐项核对或独立理想结果明确选中的工具才进入人工对照。
        # 导入阶段产生的 heuristic/normalizer 标注不能参与在线决策。
        manual_progress = frontier_source in {"manual", "oracle"} or frontier_source.startswith(
            "manual-"
        )
        observed = value.get("observed_returncode")
        commands.append(
            Command(
                index=index,
                identifier=str(value.get("id") or f"command-{index}"),
                text=replay_text,
                cwd=str(value.get("cwd") or "."),
                stage=str(value.get("stage") or "SHELL"),
                observed_returncode=int(observed) if isinstance(observed, int) else None,
                allow_failure=bool(value.get("allow_failure", False)),
                critical=bool(value.get("critical", False)),
                asynchronous=bool(value.get("async", value.get("asynchronous", False))),
                wait_for=string_list(value.get("wait_for") or value.get("depends_on")),
                timeout_sec=(float(value["timeout_sec"]) if isinstance(value.get("timeout_sec"), (int, float)) else None),
                manual_progress=manual_progress,
            )
        )
    if not commands:
        raise SystemExit("轨迹中没有命令")
    return commands


def copy_workspace(
    source: pathlib.Path,
    destination: pathlib.Path,
    preserve_git: bool,
    repo_subdir: str = "",
) -> pathlib.Path:
    if destination.exists():
        shutil.rmtree(destination)
    relative = pathlib.PurePosixPath(repo_subdir)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"仓库相对目录不合法：{repo_subdir}")
    target = destination.joinpath(*relative.parts) if repo_subdir else destination
    target.parent.mkdir(parents=True, exist_ok=True)
    ignore = None if preserve_git else shutil.ignore_patterns(".git")
    shutil.copytree(source, target, symlinks=True, ignore=ignore)
    return target


def restore_agent_paths(
    work: pathlib.Path,
    repo_target: pathlib.Path,
    agent_repo_root: str,
    repo_subdir: str,
) -> None:
    if not agent_repo_root:
        return
    original = pathlib.Path(agent_repo_root)
    if not original.is_absolute() or original == pathlib.Path("/"):
        raise ValueError(f"智能体仓库路径不合法：{agent_repo_root}")
    alias = pathlib.Path("/workspace") if repo_subdir and original.parts[:2] == ("/", "workspace") else original
    target = work if alias == pathlib.Path("/workspace") else repo_target
    if alias.exists() or alias.is_symlink():
        backup = pathlib.Path("/tmp") / f"hrb-source-{os.getpid()}-{alias.name or 'root'}"
        if backup.exists() or backup.is_symlink():
            if backup.is_dir() and not backup.is_symlink():
                shutil.rmtree(backup)
            else:
                backup.unlink()
        shutil.move(str(alias), str(backup))
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.symlink_to(target, target_is_directory=True)


def wait_gate(path: pathlib.Path, ready: pathlib.Path | None, timeout: float) -> int | None:
    if ready is not None:
        ready.parent.mkdir(parents=True, exist_ok=True)
        ready.touch()
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"等待统一启动信号超时：{path}")
        time.sleep(0.01)
    value = path.read_text(encoding='ascii').strip()
    # Old standalone callers create an empty gate. Keep their execution-only
    # measurement, and explicitly mark that no release timestamp was recorded.
    return int(value) if value else None


def send_capture_mark(
    marker: pathlib.Path | None,
    socket: pathlib.Path | None,
    session: str,
    operation: str,
    *,
    tool: str = "",
    pid: int = 0,
    stage: str = "",
) -> None:
    if marker is None or socket is None:
        return
    command = [str(marker), operation, "--socket", str(socket), "--session", session]
    if tool:
        command.extend(["--tool", tool])
    if pid:
        command.extend(["--pid", str(pid)])
    if stage:
        command.extend(["--stage", stage])
    result = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法写入内核采集标记 {operation}：{result.stderr.strip()}")


def send_flowcausal_event(
    socket_path: pathlib.Path | None,
    session: str,
    command: Command,
    operation: str,
    root_pid: int = 0,
) -> None:
    if socket_path is None:
        return
    tool_id = command.index + 1
    sequence = _FLOWCAUSAL_SEQUENCES.get(socket_path, 0) + 1
    _FLOWCAUSAL_SEQUENCES[socket_path] = sequence
    generation_key = (socket_path, tool_id)
    if operation == "tool-begin":
        generation = sequence
        _FLOWCAUSAL_GENERATIONS[generation_key] = generation
    else:
        generation = _FLOWCAUSAL_GENERATIONS.get(generation_key, 0)
    # FlowCausal 只能从这里得知工具是否已经脱离智能体的同步等待。
    # INSPECT/EDIT/BUILD/TEST 等标签只用于基准分析，不能直接决定调度。
    execution_mode = "asynchronous" if command.asynchronous else "synchronous"
    request = (
        f"2 {operation} {stable_id(session)} {tool_id} 0 {generation} {sequence} "
        f"{execution_mode} {command.stage} {root_pid} "
        f"{1 if command.manual_progress else 0}\n"
    )
    client = _FLOWCAUSAL_CLIENTS.get(socket_path)
    try:
        if client is None:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(5.0)
            client.connect(str(socket_path))
            _FLOWCAUSAL_CLIENTS[socket_path] = client
        client.sendall(request.encode("utf-8"))
        response = client.recv(128).decode("utf-8", errors="replace").strip()
    except OSError as exc:
        if client is not None:
            client.close()
        _FLOWCAUSAL_CLIENTS.pop(socket_path, None)
        raise RuntimeError(f"无法发送 FlowCausal 工具事件 {operation}：{exc}") from exc
    if response != "OK":
        raise RuntimeError(f"FlowCausal 拒绝工具事件 {operation}：{response}")
    if operation == "tool-end":
        _FLOWCAUSAL_GENERATIONS.pop(generation_key, None)


def launch(
    command: Command,
    work: pathlib.Path,
    env: dict[str, str],
    out: TextIO,
    command_log: TextIO,
    *,
    marker: pathlib.Path | None,
    capture_socket: pathlib.Path | None,
    capture_session: str,
    register_pid: bool,
    flowcausal_socket: pathlib.Path | None,
    command_interpreter: tuple[str, ...],
) -> Running:
    # 原始 cwd 相对于智能体的工作区；轨迹中的绝对仓库路径由
    # restore_agent_paths 在一次性容器内恢复。
    cwd = safe_working_dir(work, command.cwd)
    output_start = os.fstat(command_log.fileno()).st_size
    env = dict(env)
    if env.get('HRB_RUNTIME_TOOL_CONTEXT') == '1':
        # Facts about the current call, for an explicitly selected runtime adapter.
        env['FLOW_PROGRESS_TOOL_ID'] = str(command.index + 1)
        env['FLOWCAUSAL_TRACE_COMMAND_INDEX'] = str(command.index)
    # 工具开始必须早于子进程创建，否则一个启动很快的命令可能先 fork，
    # 使最初几个进程无法归入本次工具执行。
    send_capture_mark(
        marker,
        capture_socket,
        capture_session,
        "tool-begin",
        tool=command.identifier,
        stage=command.stage,
    )
    if flowcausal_socket is not None:
        # 先登记当前工具，再原样启动命令。执行器随后对这个子进程的
        # Popen.wait() 会在内核中形成真实的父子等待关系，因此无需在
        # 命令外增加轮询文件的 shell 包装层，也无需预先猜测关键 pid。
        try:
            send_flowcausal_event(
                flowcausal_socket, capture_session, command, "tool-begin"
            )
            process = subprocess.Popen(
                [*command_interpreter, command.text],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=command_log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            # 进程建立后补充工具根 pid。命令没有额外包装层；控制程序
            # 只记录边界，后续需要推进的后代仍由内核等待关系确定。
            send_flowcausal_event(
                flowcausal_socket,
                capture_session,
                command,
                "tool-update",
                root_pid=process.pid,
            )
        except Exception:
            if "process" in locals() and process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
            try:
                send_flowcausal_event(
                    flowcausal_socket, capture_session, command, "tool-end"
                )
            except Exception:
                pass
            send_capture_mark(
                marker,
                capture_socket,
                capture_session,
                "tool-end",
                tool=command.identifier,
                stage=command.stage,
            )
            raise
    else:
        try:
            process = subprocess.Popen(
                [*command_interpreter, command.text],
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=command_log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception:
            send_capture_mark(
                marker,
                capture_socket,
                capture_session,
                "tool-end",
                tool=command.identifier,
                stage=command.stage,
            )
            raise
    started = time.monotonic_ns()
    if register_pid:
        send_capture_mark(
            marker,
            capture_socket,
            capture_session,
            "session-pid",
            pid=process.pid,
        )
    emit(
        out,
        "command_begin",
        command_id=command.identifier,
        command_index=command.index,
        stage=command.stage,
        pid=process.pid,
        asynchronous=command.asynchronous,
    )
    running = Running(command, process, started, None, open_process_handle(process))
    running.output_start = output_start
    running.output_file = command_log
    start_process_wait(running)
    return running


def finish(
    running: Running,
    out: TextIO,
    *,
    marker: pathlib.Path | None,
    capture_socket: pathlib.Path | None,
    capture_session: str,
    default_timeout: float,
    flowcausal_socket: pathlib.Path | None,
) -> tuple[int, int]:
    timeout = running.command.timeout_sec or default_timeout
    try:
        returncode = wait_process(running, timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(running.process.pid, signal.SIGTERM)
            wait_process(running, 3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(running.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        send_capture_mark(
            marker,
            capture_socket,
            capture_session,
            "tool-end",
            tool=running.command.identifier,
            stage=running.command.stage,
        )
        send_flowcausal_event(
            flowcausal_socket, capture_session, running.command, "tool-end"
        )
        if running.tool_gate is not None:
            unlink_if_exists(running.tool_gate)
        finished = time.monotonic_ns()
        if running.command.allow_failure:
            command = running.command
            matches = command.observed_returncode in {None, -1}
            emit(
                out,
                "command_end",
                command_id=command.identifier,
                command_index=command.index,
                stage=command.stage,
                pid=running.process.pid,
                returncode=-1,
                observed_returncode=command.observed_returncode,
                returncode_matches=matches,
                allow_failure=True,
                asynchronous=command.asynchronous,
                timed_out=True,
                output_span=[running.output_start, os.fstat(running.output_file.fileno()).st_size],
                elapsed_ns=finished - running.started_ns,
            )
            if not matches:
                raise RuntimeError(
                    f"命令超时状态与来源记录不同：{command.identifier}，"
                    f"来源 {command.observed_returncode}，回放 -1"
                ) from exc
            return -1, finished
        raise TimeoutError(f"命令执行超过 {timeout:.1f} 秒：{running.command.identifier}") from exc
    finally:
        close_process_handle(running)
    finished = time.monotonic_ns()
    command = running.command
    send_capture_mark(
        marker,
        capture_socket,
        capture_session,
        "tool-end",
        tool=command.identifier,
        stage=command.stage,
    )
    send_flowcausal_event(flowcausal_socket, capture_session, command, "tool-end")
    if running.tool_gate is not None:
        unlink_if_exists(running.tool_gate)
    # subprocess 用负数表示“被信号结束”，而来源 shell 按惯例记录
    # 128+信号编号。两者表达同一结果，例如 SIGINT 是 -2/130。
    matches = (
        command.observed_returncode in {None, -1}
        or returncode == command.observed_returncode
        or (
            returncode < 0
            and command.observed_returncode == 128 - returncode
        )
    )
    recorded_returncode = 128 - returncode if returncode < 0 else returncode
    emit(
        out,
        "command_end",
        command_id=command.identifier,
        command_index=command.index,
        stage=command.stage,
        pid=running.process.pid,
        returncode=recorded_returncode,
        observed_returncode=command.observed_returncode,
        returncode_matches=matches,
        allow_failure=command.allow_failure,
        output_span=[running.output_start, os.fstat(running.output_file.fileno()).st_size],
        asynchronous=command.asynchronous,
        elapsed_ns=finished - running.started_ns,
    )
    # “允许失败”只表示来源工作流预期这条调试命令可能非零，不能
    # 放宽已经记录的具体返回状态。只要来源提供了非负返回状态，
    # 回放就必须一致；否则同一工作量可能因提前崩溃而被误判为成功。
    if command.observed_returncode not in {None, -1} and not matches:
        raise RuntimeError(
            f"命令退出状态与来源记录不同：{command.identifier}，来源 {command.observed_returncode}，回放 {returncode}"
        )
    if not command.allow_failure and returncode != 0:
        raise subprocess.CalledProcessError(returncode, command.text)
    return returncode, finished


def stop_running(running: dict[str, Running]) -> None:
    for item in running.values():
        if item.process.poll() is None:
            try:
                os.killpg(item.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for item in running.values():
        try:
            wait_process(item, 3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(item.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        finally:
            close_process_handle(item)


def main() -> int:
    parser = argparse.ArgumentParser(description="回放一个 HarnessReplayBench 1.0 任务")
    parser.add_argument("--trace", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--events", required=True)
    parser.add_argument("--command-log", required=True)
    parser.add_argument("--gate")
    parser.add_argument("--ready")
    parser.add_argument("--gate-timeout", type=float, default=600.0)
    parser.add_argument("--command-timeout", type=float, default=1800.0)
    parser.add_argument("--mode", choices=("tool-execution",), default="tool-execution")
    parser.add_argument("--preserve-git", action="store_true")
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--capture-marker")
    parser.add_argument("--capture-socket")
    parser.add_argument("--capture-session", default="")
    parser.add_argument("--capture-register-pids", action="store_true")
    parser.add_argument(
        "--tool-event-socket",
        "--flowcausal-socket",
        dest="flowcausal_socket",
        help="可选的会话工具事件控制程序套接字",
    )
    parser.add_argument(
        "--restore-agent-paths",
        action="store_true",
        help="在一次性容器内恢复轨迹记录的智能体绝对工作路径",
    )
    args = parser.parse_args()
    if args.command_timeout <= 0:
        raise SystemExit("--command-timeout 必须大于零")

    trace_path = pathlib.Path(args.trace).resolve()
    repo = pathlib.Path(args.repo).resolve()
    work = pathlib.Path(args.work).resolve()
    events_path = pathlib.Path(args.events).resolve()
    command_log_path = pathlib.Path(args.command_log).resolve()
    marker = pathlib.Path(args.capture_marker).resolve() if args.capture_marker else None
    capture_socket = pathlib.Path(args.capture_socket) if args.capture_socket else None
    flowcausal_socket = pathlib.Path(args.flowcausal_socket) if args.flowcausal_socket else None
    if bool(marker) != bool(capture_socket):
        raise SystemExit("--capture-marker 和 --capture-socket 必须同时使用")
    if marker is not None and (not marker.is_file() or not os.access(marker, os.X_OK)):
        raise SystemExit(f"采集标记程序不可执行：{marker}")
    capture_session = args.capture_session or str(trace_path.stem)
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    if trace.get("schema") != "harness_replaybench.trace.v2":
        raise SystemExit("只接受 HarnessReplayBench 1.0 轨迹")
    if args.mode not in trace.get("replay", {}).get("modes", []):
        raise SystemExit(f"轨迹不支持 {args.mode} 回放")
    commands = load_commands(trace)
    workspace = trace.get("workspace") if isinstance(trace.get("workspace"), dict) else {}
    source_command_environment = (
        workspace.get("command_environment")
        if isinstance(workspace.get("command_environment"), dict)
        else {}
    )
    raw_interpreter = source_command_environment.get("interpreter", ["/bin/bash", "-c"])
    if not isinstance(raw_interpreter, list) or not raw_interpreter or not all(
        isinstance(item, str) and item for item in raw_interpreter
    ):
        raise SystemExit("轨迹中的工具命令解释器无效")
    command_interpreter = tuple(raw_interpreter)
    repo_subdir = str(workspace.get("replay_repo_subdir") or "")
    repo_target = copy_workspace(repo, work, args.preserve_git, repo_subdir)
    if args.restore_agent_paths:
        restore_agent_paths(
            work,
            repo_target,
            str(workspace.get("agent_repo_root") or ""),
            repo_subdir,
        )
    events_path.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["HRB_REPLAY_REPO"] = str(repo_target)
    env.update(parse_env(args.env))
    running: dict[str, Running] = {}
    completed: dict[str, tuple[int, int]] = {}
    status = "pass"
    failure = ""
    origin = time.monotonic_ns()
    release_ns = origin
    command_log_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("w", encoding="utf-8") as out, command_log_path.open("w", encoding="utf-8") as command_log:
        try:
            recorded_release = None
            if args.gate:
                recorded_release = wait_gate(
                    pathlib.Path(args.gate),
                    pathlib.Path(args.ready) if args.ready else None,
                    args.gate_timeout,
                )
            origin = time.monotonic_ns()
            release_ns = recorded_release if recorded_release is not None else origin
            emit(
                out,
                "workload_begin",
                instance_id=str(trace.get("instance_id") or trace_path.stem),
                command_count=len(commands),
                replay_mode=args.mode,
                command_source="recorded",
                release_ns=release_ns,
                release_recorded=recorded_release is not None,
                execution_begin_ns=origin,
                release_to_start_ns=max(0, origin-release_ns),
            )
            for command in commands:
                for predecessor in command.wait_for:
                    item = running.pop(predecessor, None)
                    if item is not None:
                        completed[predecessor] = finish(
                            item,
                            out,
                            marker=marker,
                            capture_socket=capture_socket,
                            capture_session=capture_session,
                            default_timeout=args.command_timeout,
                            flowcausal_socket=flowcausal_socket,
                        )
                item = launch(
                    command,
                    work,
                    env,
                    out,
                    command_log,
                    marker=marker,
                    capture_socket=capture_socket,
                    capture_session=capture_session,
                    register_pid=args.capture_register_pids,
                    flowcausal_socket=flowcausal_socket,
                    command_interpreter=command_interpreter,
                )
                if command.asynchronous:
                    running[command.identifier] = item
                else:
                    completed[command.identifier] = finish(
                        item,
                        out,
                        marker=marker,
                        capture_socket=capture_socket,
                        capture_session=capture_session,
                        default_timeout=args.command_timeout,
                        flowcausal_socket=flowcausal_socket,
                    )
            for identifier, item in list(running.items()):
                completed[identifier] = finish(
                    item,
                    out,
                    marker=marker,
                    capture_socket=capture_socket,
                    capture_session=capture_session,
                    default_timeout=args.command_timeout,
                    flowcausal_socket=flowcausal_socket,
                )
                running.pop(identifier, None)
        except Exception as exc:
            status = "fail"
            failure = str(exc)
            stop_running(running)
            emit(out, "workload_error", message=failure)
        end_ns = time.monotonic_ns()
        milestone_ends = [completed[item.identifier][1] for item in commands if item.critical and item.identifier in completed]
        if not milestone_ends:
            normal_ends = [
                completed[item.identifier][1]
                for item in commands
                if item.stage != "PACKAGE_BACKGROUND" and item.identifier in completed
            ]
            milestone_ends = normal_ends[-1:]
        try:
            send_capture_mark(marker, capture_socket, capture_session, "session-end")
        except Exception as exc:
            if status == "pass":
                status = "fail"
                failure = str(exc)
        emit(
            out,
            "workload_end",
            status=status,
            elapsed_ns=max(0, end_ns - origin),
            workflow_completion_ns=max(0, end_ns - release_ns),
            selected_tool_milestone_ns=(
                max(0, max(milestone_ends) - origin) if milestone_ends else None
            ),
            selected_tool_milestone_semantics="heuristic_not_critical_path",
            # 兼容 1.0 早期结果读取器；新分析不得将其解释为关键路径。
            critical_ns=max(0, max(milestone_ends) - origin) if milestone_ends else None,
            critical_metric_status="deprecated_heuristic_alias",
            completed_commands=len(completed),
            failure=failure,
        )
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())

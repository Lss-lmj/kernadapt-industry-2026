#!/usr/bin/env python3
"""运行 HarnessReplayBench 1.0 套件并生成统一结果。"""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, TextIO

from work_observation import observe


ROOT = pathlib.Path(__file__).resolve().parents[1]
CASE_RUNNER = ROOT / "runners" / "replay_case.py"
CAPTURE = ROOT / "capture" / "build" / "hrb_capture"
MARKER = ROOT / "capture" / "build" / "hrb_mark"


@dataclass
class RunningCase:
    value: dict[str, Any]
    directory: pathlib.Path
    process: subprocess.Popen[str]
    stdout: TextIO
    stderr: TextIO
    flowd_socket_dir: pathlib.Path | None = None
    flowd: "FlowdControl | None" = None


@dataclass
class KernelCapture:
    process: subprocess.Popen[str]
    output: pathlib.Path
    log: TextIO
    socket_dir: pathlib.Path
    socket: pathlib.Path
    session: str


@dataclass
class FlowdControl:
    process: subprocess.Popen[str]
    log: TextIO
    log_path: pathlib.Path
    socket: pathlib.Path
    session: str
    root_pid: int
    command: list[str]
    resource_path: pathlib.Path | None = None


def write_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_json(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"文件不是 JSON 对象：{path}")
    return value


def package_path(text: str) -> pathlib.Path:
    path = pathlib.Path(text)
    result = path.resolve() if path.is_absolute() else (ROOT / path).resolve()
    return result


def package_reference(path: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def image_exists(image: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def prepare_image(image: str) -> None:
    if image_exists(image):
        return
    returncode = 1
    for attempt in range(1, 4):
        result = subprocess.run(
            ["docker", "pull", image],
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
            timeout=1800,
            check=False,
        )
        returncode = result.returncode
        if returncode == 0 and image_exists(image):
            return
        if attempt < 3:
            time.sleep(5)
    raise RuntimeError(f"无法准备容器镜像：{image}，退出状态 {returncode}")


def image_record(image: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法读取容器镜像信息：{image}：{result.stderr.strip()}")
    values = json.loads(result.stdout)
    if not isinstance(values, list) or not values or not isinstance(values[0], dict):
        raise RuntimeError(f"容器镜像信息格式异常：{image}")
    value = values[0]
    return {
        "name": image,
        "id": str(value.get("Id") or ""),
        "repo_digests": sorted(str(item) for item in value.get("RepoDigests", []) if item),
        "created": str(value.get("Created") or ""),
        "architecture": str(value.get("Architecture") or ""),
        "os": str(value.get("Os") or ""),
    }


def drop_page_cache() -> None:
    subprocess.run(["sync"], check=True)
    if os.geteuid() == 0:
        pathlib.Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="utf-8")
        return
    result = subprocess.run(
        ["sudo", "-n", "tee", "/proc/sys/vm/drop_caches"],
        input="3\n",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法清空页缓存：{result.stderr.strip()}")


def capture_session_name(suite_id: str, index: int, instance: str) -> str:
    return f"{suite_id}:{index}:{instance}"


def stable_id(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def docker_command(
    case: dict[str, Any],
    directory: pathlib.Path,
    manifest: dict[str, Any],
    args: argparse.Namespace,
    *,
    capture: KernelCapture | None,
    session: str,
    flowd_socket_dir: pathlib.Path | None,
) -> list[str]:
    image = str(case["container_image"])
    trace = package_path(str(case["trace_file"]))
    try:
        trace_rel = trace.relative_to(ROOT).as_posix()
    except ValueError as exc:
        raise RuntimeError(f"容器轨迹必须位于数据包内：{trace}") from exc
    python = str(case.get("container_python") or "auto")
    command = [
        "docker", "run", "--rm",
        "--cidfile", str(directory / "container.cid"),
        "--user", str(case.get("docker_user") or "root"),
        "--pids-limit", str(args.container_pids_limit),
    ]
    cpu_set = str(manifest.get("cpu_set") or "")
    if cpu_set:
        command.extend(["--cpuset-cpus", cpu_set])
    command.extend(["-v", f"{ROOT}:/hrb:ro", "-v", f"{directory}:/out"])
    if args.runtime_directory:
        command.extend(['-v', f'{pathlib.Path(args.runtime_directory).resolve()}:/hrb-runtime:ro'])
    if args.shared_directory:
        command.extend(['-v', f'{pathlib.Path(args.shared_directory).resolve()}:/hrb-shared'])
    if capture is not None:
        command.extend(["-v", f"{capture.socket_dir}:/hrb-capture"])
    if flowd_socket_dir is not None:
        command.extend(["-v", f"{flowd_socket_dir}:/hrb-flowd"])
    for item in case.get("container_env", []):
        command.extend(["-e", str(item)])
    for item in args.container_env:
        command.extend(['-e', item])
    command.append(image)
    if python == "auto":
        command.extend(["/bin/sh", "/hrb/runners/launch_replay.sh", "/hrb/runners/replay_case.py"])
    else:
        command.extend([python, "/hrb/runners/replay_case.py"])
    command.extend(
        [
            "--trace", f"/hrb/{trace_rel}",
            "--repo", str(case["container_repo_dir"]),
            "--work", "/out/work",
            "--events", "/out/events.jsonl",
            "--command-log", "/out/command-output.log",
            "--gate", "/out/start.gate",
            "--ready", "/out/ready",
            "--mode", str(manifest["replay_mode"]),
            "--command-timeout", str(args.command_timeout),
            "--preserve-git",
            "--restore-agent-paths",
            "--capture-session", session,
        ]
    )
    if capture is not None:
        command.extend(
            [
                "--capture-marker", "/hrb/capture/build/hrb_mark",
                "--capture-socket", f"/hrb-capture/{capture.socket.name}",
            ]
        )
    if flowd_socket_dir is not None:
        command.extend(["--tool-event-socket", "/hrb-flowd/flowd.sock"])
    return command


def host_command(
    case: dict[str, Any],
    directory: pathlib.Path,
    manifest: dict[str, Any],
    args: argparse.Namespace,
    *,
    capture: KernelCapture | None,
    session: str,
    flowd_socket_dir: pathlib.Path | None,
) -> list[str]:
    command = [
        sys.executable,
        str(CASE_RUNNER),
        "--trace", str(package_path(str(case["trace_file"]))),
        "--repo", str(package_path(str(case["repo_dir"]))),
        "--work", str(directory / "work"),
        "--events", str(directory / "events.jsonl"),
        "--command-log", str(directory / "command-output.log"),
        "--gate", str(directory / "start.gate"),
        "--ready", str(directory / "ready"),
        "--mode", str(manifest["replay_mode"]),
        "--command-timeout", str(args.command_timeout),
        "--preserve-git",
        "--capture-session", session,
    ]
    if capture is not None:
        command.extend(
            [
                "--capture-marker", str(MARKER),
                "--capture-socket", str(capture.socket),
                "--capture-register-pids",
            ]
        )
    if flowd_socket_dir is not None:
        command.extend(["--tool-event-socket", str(flowd_socket_dir / "flowd.sock")])
    cpu_set = str(manifest.get("cpu_set") or "")
    if cpu_set:
        command = ["taskset", "-c", cpu_set, *command]
    return command


def start_kernel_capture(
    output: pathlib.Path,
    suite_id: str,
    timeout: float,
    detailed_tool_progress: bool = False,
) -> KernelCapture:
    for binary in (CAPTURE, MARKER):
        if not binary.is_file() or not os.access(binary, os.X_OK):
            raise RuntimeError(f"内核采集程序未构建：{binary}；请先运行 make -C capture")
    socket_dir = pathlib.Path(tempfile.mkdtemp(prefix="hrb-capture-", dir="/tmp"))
    socket_dir.chmod(0o777)
    socket = socket_dir / "capture.sock"
    capture_output = output / "kernel-capture.jsonl"
    log = (output / "kernel-capture.log").open("w", encoding="utf-8")
    command = [
        str(CAPTURE),
        "--output", str(capture_output),
        "--socket", str(socket),
        "--session", suite_id,
        "--owner-pid", str(os.getpid()),
        "--attach-only",
        "--quiet",
    ]
    if detailed_tool_progress:
        command.append("--detailed-tool-progress")
    if os.geteuid() != 0:
        command = ["sudo", "-n", *command]
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while not socket.exists():
        if process.poll() is not None:
            log.flush()
            log.close()
            shutil.rmtree(socket_dir, ignore_errors=True)
            raise RuntimeError(f"内核采集程序提前退出，状态 {process.returncode}")
        if time.monotonic() >= deadline:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)
            log.close()
            shutil.rmtree(socket_dir, ignore_errors=True)
            raise TimeoutError("等待内核采集程序启动超时")
        time.sleep(0.02)
    return KernelCapture(process, capture_output, log, socket_dir, socket, suite_id)


def send_mark(capture: KernelCapture, operation: str, session: str, *extra: str) -> None:
    result = subprocess.run(
        [str(MARKER), operation, "--socket", str(capture.socket), "--session", session, *extra],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"无法写入内核采集标记 {operation}：{result.stderr.strip()}")


def docker_root_pid(cid_file: pathlib.Path) -> int:
    cid = cid_file.read_text(encoding="utf-8").strip()
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Pid}}", cid],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        raise RuntimeError(f"无法读取容器 {cid} 的主进程：{result.stderr.strip()}")
    pid = int(result.stdout.strip())
    if pid <= 0:
        raise RuntimeError(f"容器 {cid} 尚未运行")
    return pid


def docker_cgroup(cid_file: pathlib.Path) -> pathlib.Path:
    pid = docker_root_pid(cid_file)
    cgroup_text = pathlib.Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8")
    unified = next((line.split("::", 1)[1] for line in cgroup_text.splitlines() if "::" in line), "")
    path = pathlib.Path("/sys/fs/cgroup") / unified.lstrip("/")
    if not path.is_dir():
        raise RuntimeError(f"容器 cgroup 不存在：{path}")
    return path


def privileged(command: list[str]) -> list[str]:
    return command if os.geteuid() == 0 else ["sudo", "-n", *command]


def start_flowd(
    item: RunningCase,
    *,
    binary: pathlib.Path,
    pin_dir: str,
    session: str,
    extra_args: list[str],
    timeout: float,
    measure_resources: bool,
) -> FlowdControl:
    if item.flowd_socket_dir is None:
        raise RuntimeError("FlowCausal 套接字目录尚未建立")
    root_pid = (
        docker_root_pid(item.directory / "container.cid")
        if item.value.get("container_image")
        else item.process.pid
    )
    socket_path = item.flowd_socket_dir / "flowd.sock"
    log_path = item.directory / "flowd.log"
    resource_path = item.directory / "flowd-resource.json" if measure_resources else None
    resource_args = ["--resource-output", str(resource_path)] if resource_path is not None else []
    command = privileged(
        [
            str(binary),
            *extra_args,
            *resource_args,
            "--root-pid", str(root_pid),
            "--session", session,
            "--pin-dir", pin_dir,
            "--serve-socket", str(socket_path),
        ]
    )
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    control = FlowdControl(
        process, log, log_path, socket_path, session, root_pid, command,
        resource_path,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            log.flush()
            raise RuntimeError(f"flowd_v2 提前退出，状态 {process.returncode}：{log_path}")
        if socket_path.exists():
            log.flush()
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "flowd registered session=" in text and "flowd serving socket=" in text:
                return control
        time.sleep(0.02)
    raise TimeoutError(f"等待 flowd_v2 启动超时：{session}")


def flowd_stats(binary: pathlib.Path, pin_dir: str, session: str) -> dict[str, Any]:
    command = privileged(
        [
            str(binary),
            "--operation", "stats",
            "--session", session,
            "--pin-dir", pin_dir,
            "--once",
        ]
    )
    completed = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"读取 FlowCausal 会话统计失败：{completed.stderr.strip()}")
    for line in reversed(completed.stdout.splitlines()):
        if not line.startswith("{"):
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            return value
    raise RuntimeError(f"FlowCausal 会话没有统计记录：{session}")


def stop_flowd(control: FlowdControl, timeout: float = 10.0) -> int:
    if control.process.poll() is None:
        try:
            os.killpg(control.process.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
    try:
        returncode = control.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(control.process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        returncode = control.process.wait(timeout=timeout)
    if not control.log.closed:
        control.log.close()
    return returncode


def finish_kernel_capture(capture: KernelCapture, timeout: float) -> dict[str, Any]:
    send_mark(capture, "session-end", capture.session)
    try:
        returncode = capture.process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(capture.process.pid, signal.SIGTERM)
        returncode = capture.process.wait(timeout=10)
    capture.log.close()
    if returncode != 0:
        raise RuntimeError(f"内核采集程序退出状态异常：{returncode}")
    rows = read_events(capture.output)
    sessions = [row for row in rows if row.get("record") == "session_summary"]
    tasks = [row for row in rows if row.get("record") == "task_summary"]
    tools = [row for row in rows if row.get("record") == "tool_summary"]
    end = next((row for row in reversed(rows) if row.get("record") == "capture_end"), {})
    return {
        "collector": "C/eBPF",
        'capture_end_recorded': bool(end),
        "capture_elapsed_ns": int(end.get("elapsed_ns") or 0),
        "dropped_events": max([int(row.get("dropped_events") or 0) for row in sessions] or [0]),
        "task_count": len(tasks),
        "runtime_ns": sum(int(row.get("runtime_ns") or 0) for row in tasks),
        "runnable_wait_ns": sum(int(row.get("runnable_wait_ns") or 0) for row in tasks),
        "blocking_wait_ns": sum(int(row.get("blocking_wait_ns") or 0) for row in tasks),
        "max_runnable_wait_ns": max([int(row.get("max_runnable_wait_ns") or 0) for row in tasks] or [0]),
        "wakeups": sum(int(row.get("wakeups") or 0) for row in tasks),
        "switches": sum(int(row.get("switches") or 0) for row in tasks),
        "migrations": sum(int(row.get("migrations") or 0) for row in tasks),
        "sessions": sessions,
        "tool_count": len(tools),
        "tool_returns": sum(int(row.get("tool_returns") or 0) for row in tools),
        "process_exits": sum(int(row.get("exits") or 0) for row in sessions),
        "wait_returns": sum(int(row.get("wait_returns") or 0) for row in sessions),
        "raw_bytes": capture.output.stat().st_size if capture.output.exists() else 0,
    }


def remove_capture_artifacts(capture: KernelCapture) -> None:
    for path in (capture.output, capture.output.with_name("kernel-capture.log")):
        if not path.exists():
            continue
        try:
            path.unlink()
        except PermissionError:
            subprocess.run(["sudo", "-n", "rm", "-f", "--", str(path)], check=False)
    shutil.rmtree(capture.socket_dir, ignore_errors=True)


def read_events(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.is_file():
        return records
    decoder = json.JSONDecoder()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        # Some interactive programs print their prompt without a newline.
        # Structured replay events can therefore start in the middle of a
        # physical line.  Keep the JSON object that reaches the line end.
        for offset in reversed(
            [index for index, character in enumerate(line) if character == "{"]
        ):
            try:
                value, end = decoder.raw_decode(line, offset)
            except json.JSONDecodeError:
                continue
            if line[end:].strip() or not isinstance(value, dict):
                continue
            records.append(value)
            break
    return records


def percentile(values: list[float], percent: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(len(ordered) * percent / 100) - 1))]


def tool_service_summary(command_ends: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总智能体从发起同步工具到工具返回期间可见的本地阻塞时间。"""
    commands: list[dict[str, Any]] = []
    by_stage: dict[str, list[float]] = defaultdict(list)
    for event in command_ends:
        elapsed = max(0, int(event.get("elapsed_ns") or 0)) / 1_000_000_000
        asynchronous = bool(event.get("asynchronous", False))
        row = {
            "command_id": str(event.get("command_id") or ""),
            "command_index": int(event.get("command_index") or 0),
            "stage": str(event.get("stage") or "SHELL"),
            "asynchronous": asynchronous,
            "elapsed_sec": elapsed,
        }
        commands.append(row)
        if not asynchronous:
            by_stage[row["stage"]].append(elapsed)
    synchronous = [row["elapsed_sec"] for row in commands if not row["asynchronous"]]
    return {
        "definition": "同步工具从发起到返回的本地墙钟时间",
        "all_tool_count": len(commands),
        "synchronous_tool_count": len(synchronous),
        "asynchronous_tool_count": len(commands) - len(synchronous),
        "synchronous_total_sec": sum(synchronous),
        "synchronous_mean_sec": sum(synchronous) / len(synchronous) if synchronous else None,
        "synchronous_p50_sec": percentile(synchronous, 50),
        "synchronous_p95_sec": percentile(synchronous, 95),
        "synchronous_p99_sec": percentile(synchronous, 99),
        "synchronous_max_sec": max(synchronous) if synchronous else None,
        "by_stage": {
            stage: {
                "count": len(values),
                "total_sec": sum(values),
                "mean_sec": sum(values) / len(values),
                "p95_sec": percentile(values, 95),
            }
            for stage, values in sorted(by_stage.items())
        },
        "commands": commands,
    }


def remove_work_tree(path: pathlib.Path, case_directory: pathlib.Path) -> None:
    """只删除本轮任务目录内可重建的工作副本。"""
    resolved = path.resolve()
    resolved.relative_to(case_directory.resolve())
    try:
        shutil.rmtree(resolved)
        return
    except PermissionError:
        pass
    result = subprocess.run(
        ["sudo", "-n", "rm", "-rf", "--", str(resolved)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode != 0 or resolved.exists():
        raise RuntimeError(f"无法清理成功任务工作区 {resolved}：{result.stderr.strip()}")


def measured_windows(
    case: dict[str, Any], command_ends: list[dict[str, Any]], kind: str
) -> tuple[str | None, list[dict[str, Any]]]:
    relation_name = str(case.get("execution_relations_file") or "")
    if not relation_name:
        return None, []
    relation_path = package_path(relation_name)
    relation = load_json(relation_path)
    if relation.get("schema") != "harness_replaybench.execution_relations.v1":
        raise RuntimeError(f"执行关系文件格式不正确：{relation_path}")
    if relation.get("instance_id") != case.get("instance_id"):
        raise RuntimeError(f"执行关系与任务不一致：{relation_path}")
    by_command = {
        str(event.get("command_id") or ""): event
        for event in command_ends
        if event.get("command_id")
    }
    rows: list[dict[str, Any]] = []
    for declared in relation.get(kind, []):
        if not isinstance(declared, dict):
            continue
        command_id = str(declared.get("command_id") or "")
        event = by_command.get(command_id)
        if event is None:
            continue
        rows.append(
            {
                **declared,
                "elapsed_sec": max(0, int(event.get("elapsed_ns") or 0)) / 1_000_000_000,
            }
        )
    return package_reference(relation_path), rows


def release_case(path: pathlib.Path) -> int:
    """Publish the host CLOCK_MONOTONIC release time with the start signal."""
    released = time.monotonic_ns()
    temporary = path.with_suffix('.tmp')
    temporary.write_text(str(released) + '\n', encoding='ascii')
    temporary.replace(path)
    return released


def case_result(item: RunningCase, returncode: int, keep_output: bool) -> dict[str, Any]:
    events_path = item.directory / "events.jsonl"
    events = read_events(events_path)
    begin = next((event for event in events if event.get("record") == "workload_begin"), {})
    end = next((event for event in reversed(events) if event.get("record") == "workload_end"), {})
    command_ends = [event for event in events if event.get("record") == "command_end"]
    tool_service = tool_service_summary(command_ends)
    stage_ns: dict[str, int] = defaultdict(int)
    for event in command_ends:
        stage_ns[str(event.get("stage") or "SHELL")] += max(0, int(event.get("elapsed_ns") or 0))
    relations_file, causal_windows = measured_windows(item.value, command_ends, "causal_windows")
    _, join_windows = measured_windows(item.value, command_ends, "join_windows")
    causal_seconds = [float(window["elapsed_sec"]) for window in causal_windows]
    join_seconds = [float(window["elapsed_sec"]) for window in join_windows]
    status = "pass" if returncode == 0 and end.get("status") == "pass" else "fail"
    output_path = item.directory / "command-output.log"
    output_bytes = output_path.stat().st_size if output_path.exists() else 0
    if output_path.exists():
        observation = observe(events, output_path, load_json(package_path(item.value['trace_file'])))
        write_json(item.directory / 'work-observation.json', observation)
    else:
        observation = None
    work = item.directory / "work"
    result_root = item.directory.parents[1]
    work_bytes = sum(path.stat().st_size for path in work.rglob("*") if path.is_file()) if work.exists() else 0
    if status == "pass":
        if work.exists():
            remove_work_tree(work, item.directory)
        if output_path.exists() and not keep_output:
            output_path.unlink()
        for name in ("ready", "start.gate", "container.cid"):
            (item.directory / name).unlink(missing_ok=True)
    workflow_completion_sec = (
        float(end["workflow_completion_ns"]) / 1_000_000_000
        if isinstance(end.get("workflow_completion_ns"), int)
        else float(end.get("elapsed_ns") or 0) / 1_000_000_000
    )
    selected_milestone_sec = (
        float(end["selected_tool_milestone_ns"]) / 1_000_000_000
        if isinstance(end.get("selected_tool_milestone_ns"), int)
        else (
            float(end["critical_ns"]) / 1_000_000_000
            if isinstance(end.get("critical_ns"), int)
            else None
        )
    )
    return {
        "instance_id": str(item.value["instance_id"]),
        "status": status,
        "returncode": returncode,
        "command_source": str(begin.get("command_source") or "unknown"),
        "workload_begin_ns": begin.get("time_ns"),
        'release_ns': begin.get('release_ns'),
        'release_recorded': bool(begin.get('release_recorded')),
        'release_to_start_sec': float(begin.get('release_to_start_ns') or 0) / 1_000_000_000,
        "workload_end_ns": end.get("time_ns"),
        "elapsed_sec": float(end.get("elapsed_ns") or 0) / 1_000_000_000,
        "workflow_completion_sec": workflow_completion_sec,
        "selected_tool_milestone_sec": selected_milestone_sec,
        "selected_tool_milestone_semantics": "heuristic_not_critical_path",
        "critical_sec": selected_milestone_sec,
        "critical_metric_status": "deprecated_heuristic_alias",
        "completed_commands": int(end.get("completed_commands") or 0),
        "command_returncode_matches": sum(bool(event.get("returncode_matches")) for event in command_ends),
        "command_count": len(command_ends),
        "stage_seconds": {key: value / 1_000_000_000 for key, value in sorted(stage_ns.items())},
        "tool_service": tool_service,
        "execution_relations_file": relations_file,
        "causal_windows": causal_windows,
        "mean_causal_window_sec": sum(causal_seconds) / len(causal_seconds) if causal_seconds else None,
        "p95_causal_window_sec": percentile(causal_seconds, 95),
        "join_windows": join_windows,
        "mean_join_window_sec": sum(join_seconds) / len(join_seconds) if join_seconds else None,
        "p95_join_window_sec": percentile(join_seconds, 95),
        "allowed_failure_commands": sum(
            bool(event.get("allow_failure")) and int(event.get("returncode") or 0) != 0 for event in command_ends
        ),
        "unexpected_failure_commands": sum(
            not bool(event.get("allow_failure")) and int(event.get("returncode") or 0) != 0 for event in command_ends
        ),
        "failure": str(end.get("failure") or ""),
        'work_observation': {
            'file': (item.directory / 'work-observation.json').relative_to(result_root).as_posix(),
            'expected_commands': observation['expected_commands'],
            'observed_commands': observation['observed_commands'],
            'source_unknown_commands': observation['source_unknown_commands'],
        } if observation is not None else None,
        "retained": {
            "events": events_path.relative_to(result_root).as_posix(),
            "stdout": (item.directory / "runner.stdout.log").relative_to(result_root).as_posix(),
            "stderr": (item.directory / "runner.stderr.log").relative_to(result_root).as_posix(),
            "command_output_bytes": output_bytes,
            "work_bytes_removed": work_bytes if status == "pass" else 0,
            "full_failure_scene": status != "pass",
        },
    }


def system_record(cpu_set: str) -> dict[str, Any]:
    cpu_model = ""
    for line in pathlib.Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("model name") and ":" in line:
            cpu_model = line.split(":", 1)[1].strip()
            break
    governors = sorted(
        {
            path.read_text(encoding="utf-8", errors="replace").strip()
            for path in pathlib.Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_governor")
            if path.is_file()
        }
    )
    numa_nodes = sorted(path.name for path in pathlib.Path("/sys/devices/system/node").glob("node[0-9]*"))
    selected_cpus: list[int] = []
    for part in cpu_set.split(",") if cpu_set else []:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            begin, end = part.split("-", 1)
            selected_cpus.extend(range(int(begin), int(end) + 1))
        else:
            selected_cpus.append(int(part))
    cpu_topology: list[dict[str, Any]] = []
    for cpu in sorted(set(selected_cpus)):
        root = pathlib.Path(f"/sys/devices/system/cpu/cpu{cpu}")
        topology = root / "topology"
        node = next((path.name for path in root.glob("node[0-9]*")), "")

        def value(name: str) -> str:
            path = topology / name
            return path.read_text(encoding="utf-8").strip() if path.is_file() else ""

        cpu_topology.append(
            {
                "cpu": cpu,
                "core_id": value("core_id"),
                "package_id": value("physical_package_id"),
                "thread_siblings": value("thread_siblings_list"),
                "numa_node": node,
            }
        )
    clocksource = pathlib.Path("/sys/devices/system/clocksource/clocksource0/current_clocksource")
    no_turbo = pathlib.Path("/sys/devices/system/cpu/intel_pstate/no_turbo")
    return {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_set": cpu_set,
        "logical_cpus": os.cpu_count(),
        "cpu_model": cpu_model,
        "online_cpus": pathlib.Path("/sys/devices/system/cpu/online").read_text(encoding="utf-8").strip(),
        "smt_active": (
            pathlib.Path("/sys/devices/system/cpu/smt/active").read_text(encoding="utf-8").strip()
            if pathlib.Path("/sys/devices/system/cpu/smt/active").is_file()
            else "unknown"
        ),
        "numa_nodes": numa_nodes,
        "selected_cpu_topology": cpu_topology,
        "scaling_governors": governors,
        "clocksource": clocksource.read_text(encoding="utf-8").strip() if clocksource.is_file() else "",
        "intel_pstate_no_turbo": no_turbo.read_text(encoding="utf-8").strip() if no_turbo.is_file() else "",
        "load_average": list(os.getloadavg()),
        "available_memory_bytes": int(
            next(
                (line.split()[1] for line in pathlib.Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:")),
                "0",
            )
        ) * 1024,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 HarnessReplayBench 1.0 套件")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--scheduler-label", default="unspecified")
    parser.add_argument("--ready-timeout", type=float, default=600.0)
    parser.add_argument("--command-timeout", type=float, default=1800.0)
    parser.add_argument(
        "--container-pids-limit",
        type=int,
        default=2048,
        help="限制每个容器的进程总数，防止异常轨迹耗尽主机资源",
    )
    parser.add_argument("--drop-caches", action="store_true")
    parser.add_argument("--keep-command-output", action="store_true")
    parser.add_argument('--runtime-directory', help='Optional application runtime, mounted at /hrb-runtime')
    parser.add_argument('--shared-directory', help='Optional local data/socket directory, mounted at /hrb-shared')
    parser.add_argument('--container-env', action='append', default=[], help='Explicit application configuration KEY=VALUE')
    parser.add_argument("--kernel-capture", action="store_true", help="用 C/eBPF 记录回放过程的任务与调度统计")
    parser.add_argument("--keep-kernel-capture", action="store_true", help="成功时仍保留逐任务内核采集文件")
    parser.add_argument(
        "--detailed-tool-progress",
        action="store_true",
        help="记录每次工具执行的处理器时间、排队、进程退出和等待结束，只用于机制分析",
    )
    parser.add_argument("--tool-controller", "--flowd-v2", dest="flowd_v2", help="可选的会话工具事件控制程序")
    parser.add_argument(
        "--tool-controller-pin-dir",
        "--flowd-pin-dir",
        dest="flowd_pin_dir",
        default="/sys/fs/bpf/flowcausal_v2",
    )
    parser.add_argument(
        "--tool-controller-arg",
        dest="flowd_v2_args",
        action="append",
        default=[],
        help="传给 C 工具事件控制程序的参数，可重复使用",
    )
    parser.add_argument(
        "--measure-controller-resources",
        action="store_true",
        help="记录每个 C 控制进程的处理器时间和峰值内存",
    )
    args = parser.parse_args()
    if args.command_timeout <= 0:
        raise SystemExit("--command-timeout 必须大于零")
    if args.container_pids_limit < 64:
        raise SystemExit("--container-pids-limit 不能小于 64")
    if args.detailed_tool_progress and not args.kernel_capture:
        raise SystemExit("--detailed-tool-progress 需要同时启用 --kernel-capture")
    flowd_binary = pathlib.Path(args.flowd_v2).resolve() if args.flowd_v2 else None
    if flowd_binary is not None and (not flowd_binary.is_file() or not os.access(flowd_binary, os.X_OK)):
        raise SystemExit(f"C 控制程序不可执行：{flowd_binary}")
    manifest_path = pathlib.Path(args.manifest).resolve()
    manifest = load_json(manifest_path)
    if manifest.get("schema") != "harness_replaybench.manifest.v2":
        raise SystemExit("只接受 HarnessReplayBench 1.0 套件清单")
    output = pathlib.Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise SystemExit(f"结果目录非空：{output}")
    output.mkdir(parents=True, exist_ok=True)
    cases = [item for item in manifest.get("cases", []) if isinstance(item, dict)]
    if not cases:
        raise SystemExit("套件中没有任务")

    images: dict[str, dict[str, Any]] = {}
    for case in cases:
        trace = package_path(str(case.get("trace_file") or ""))
        if not trace.is_file():
            raise SystemExit(f"轨迹不存在：{trace}")
        if case.get("container_image"):
            image = str(case["container_image"])
            prepare_image(image)
            if image not in images:
                images[image] = image_record(image)
        elif not package_path(str(case.get("repo_dir") or "")).is_dir():
            raise SystemExit(f"仓库不存在：{case.get('repo_dir')}")
    if args.drop_caches:
        drop_page_cache()

    running: list[RunningCase] = []
    capture: KernelCapture | None = None
    capture_summary: dict[str, Any] | None = None
    capture_finished = False
    environment_before = system_record(str(manifest.get("cpu_set") or ""))
    try:
        if args.kernel_capture:
            capture = start_kernel_capture(
                output,
                str(manifest["suite_id"]),
                args.ready_timeout,
                detailed_tool_progress=args.detailed_tool_progress,
            )
        for index, case in enumerate(cases):
            directory = output / "cases" / f"{index:02d}-{case['instance_id']}"
            directory.mkdir(parents=True, exist_ok=True)
            session = capture_session_name(str(manifest["suite_id"]), index, str(case["instance_id"]))
            flowd_socket_dir = None
            if flowd_binary is not None:
                flowd_socket_dir = pathlib.Path(tempfile.mkdtemp(prefix="hrb-flowd-", dir="/tmp"))
                flowd_socket_dir.chmod(0o777)
            command = (
                docker_command(
                    case,
                    directory,
                    manifest,
                    args,
                    capture=capture,
                    session=session,
                    flowd_socket_dir=flowd_socket_dir,
                )
                if case.get("container_image")
                else host_command(
                    case,
                    directory,
                    manifest,
                    args,
                    capture=capture,
                    session=session,
                    flowd_socket_dir=flowd_socket_dir,
                )
            )
            write_json(directory / "run-command.json", command)
            stdout = (directory / "runner.stdout.log").open("w", encoding="utf-8")
            stderr = (directory / "runner.stderr.log").open("w", encoding="utf-8")
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr, text=True, start_new_session=True)
            running.append(RunningCase(case, directory, process, stdout, stderr, flowd_socket_dir))

        deadline = time.monotonic() + args.ready_timeout
        while not all((item.directory / "ready").exists() for item in running):
            failed = [item for item in running if item.process.poll() is not None]
            if failed:
                raise RuntimeError(f"任务在同步起点前退出：{failed[0].value['instance_id']}")
            if time.monotonic() >= deadline:
                raise TimeoutError("等待任务准备完成超时")
            time.sleep(0.02)
        if capture is not None:
            for index, item in enumerate(running):
                if not item.value.get("container_image"):
                    continue
                cgroup = docker_cgroup(item.directory / "container.cid")
                session = capture_session_name(
                    str(manifest["suite_id"]), index, str(item.value["instance_id"])
                )
                send_mark(capture, "session-cgroup", session, "--cgroup", str(cgroup))
        if flowd_binary is not None:
            for index, item in enumerate(running):
                session = capture_session_name(
                    str(manifest["suite_id"]), index, str(item.value["instance_id"])
                )
                item.flowd = start_flowd(
                    item,
                    binary=flowd_binary,
                    pin_dir=args.flowd_pin_dir,
                    session=session,
                    extra_args=args.flowd_v2_args,
                    timeout=args.ready_timeout,
                    measure_resources=args.measure_controller_resources,
                )
        release_start = time.monotonic()
        for item in sorted(running, key=lambda value: int(value.value.get("start_offset_ms") or 0)):
            target = release_start + int(item.value.get("start_offset_ms") or 0) / 1000
            while time.monotonic() < target:
                time.sleep(min(0.02, target - time.monotonic()))
            release_case(item.directory / "start.gate")

        results: list[dict[str, Any]] = []
        for item in running:
            returncode = item.process.wait()
            item.stdout.close()
            item.stderr.close()
            results.append(case_result(item, returncode, args.keep_command_output))
        if capture is not None:
            capture_summary = finish_kernel_capture(capture, args.ready_timeout)
            capture_summary["session_ids"] = [
                {
                    "instance_id": str(item.value["instance_id"]),
                    "session_id": stable_id(
                        capture_session_name(str(manifest["suite_id"]), index, str(item.value["instance_id"]))
                    ),
                }
                for index, item in enumerate(running)
            ]
            observed_sessions = {int(row['session_id']) for row in capture_summary['sessions']}
            capture_summary['missing_session_ids'] = [row['session_id'] for row in capture_summary['session_ids']
                                                       if row['session_id'] not in observed_sessions]
            capture_summary['complete'] = (capture_summary['capture_end_recorded']
                and not capture_summary['missing_session_ids'] and capture_summary['dropped_events'] == 0)
            capture_finished = True
        flowd_summary: list[dict[str, Any]] = []
        if flowd_binary is not None:
            for item in running:
                if item.flowd is None:
                    raise RuntimeError(f"FlowCausal 会话未启动：{item.value['instance_id']}")
                stats = flowd_stats(flowd_binary, args.flowd_pin_dir, item.flowd.session)
                returncode = stop_flowd(item.flowd)
                if returncode != 0:
                    raise RuntimeError(f"flowd_v2 退出状态异常：{item.value['instance_id']}={returncode}")
                resources = None
                if item.flowd.resource_path is not None:
                    if not item.flowd.resource_path.is_file():
                        raise RuntimeError(
                            f"flowd_v2 没有写出资源记录：{item.value['instance_id']}"
                        )
                    resources = load_json(item.flowd.resource_path)
                flowd_summary.append(
                    {
                        "instance_id": str(item.value["instance_id"]),
                        "session": item.flowd.session,
                        "root_pid": item.flowd.root_pid,
                        "command": item.flowd.command,
                        "log": item.flowd.log_path.relative_to(output).as_posix(),
                        "stats": stats,
                        "resources": resources,
                    }
                )
        beginnings = [int(item["workload_begin_ns"]) for item in results if isinstance(item.get("workload_begin_ns"), int)]
        endings = [int(item["workload_end_ns"]) for item in results if isinstance(item.get("workload_end_ns"), int)]
        elapsed = [float(item["elapsed_sec"]) for item in results]
        critical = [float(item["critical_sec"]) for item in results if item.get("critical_sec") is not None]
        workflow_completion = [float(item["workflow_completion_sec"]) for item in results]
        selected_milestones = [
            float(item["selected_tool_milestone_sec"])
            for item in results
            if item.get("selected_tool_milestone_sec") is not None
        ]
        synchronous_tool_service = [
            float(command["elapsed_sec"])
            for item in results
            for command in item.get("tool_service", {}).get("commands", [])
            if not command.get("asynchronous")
        ]
        causal_windows = [
            float(window["elapsed_sec"])
            for item in results
            for window in item.get("causal_windows", [])
        ]
        join_windows = [
            float(window["elapsed_sec"])
            for item in results
            for window in item.get("join_windows", [])
        ]
        releases = [int(item['release_ns']) for item in results if isinstance(item.get('release_ns'), int)]
        execution_makespan = (max(endings) - min(beginnings)) / 1_000_000_000 if beginnings and endings else 0.0
        makespan = (max(endings) - min(releases)) / 1_000_000_000 if releases and endings else execution_makespan
        result = {
            "schema": "harness_replaybench.result.v2",
            "suite_id": str(manifest["suite_id"]),
            "benchmark_layer": str(manifest["benchmark_layer"]),
            "scheduler": args.scheduler_label,
            "replay_mode": str(manifest["replay_mode"]),
            "status": (
                "pass"
                if all(item["status"] == "pass" for item in results)
                and (capture_summary is None or capture_summary['complete'])
                else "fail"
            ),
            "manifest": package_reference(manifest_path),
            "environment": {
                **environment_before,
                "container_images": [images[name] for name in sorted(images)],
                "after": system_record(str(manifest.get("cpu_set") or "")),
            },
            "experiment": {
                'container_env': args.container_env,
                'runtime_directory': args.runtime_directory,
                'shared_directory': args.shared_directory,
                "release_pattern": manifest.get("release_pattern"),
                "command_source": "recorded",
                "page_cache_cleared": args.drop_caches,
                "container_pids_limit": args.container_pids_limit,
                "success_artifacts_compacted": True,
                'timing_semantics': 'release-to-completion-v1',
            },
            "aggregate": {
                "makespan_sec": makespan,
                'execution_makespan_sec': execution_makespan,
                "mean_elapsed_sec": sum(elapsed) / len(elapsed),
                "p50_elapsed_sec": percentile(elapsed, 50),
                "p95_elapsed_sec": percentile(elapsed, 95),
                "p99_elapsed_sec": percentile(elapsed, 99),
                "mean_workflow_completion_sec": sum(workflow_completion) / len(workflow_completion),
                "p95_workflow_completion_sec": percentile(workflow_completion, 95),
                "mean_selected_tool_milestone_sec": (
                    sum(selected_milestones) / len(selected_milestones)
                    if selected_milestones else None
                ),
                "p95_selected_tool_milestone_sec": percentile(selected_milestones, 95),
                "synchronous_tool_count": len(synchronous_tool_service),
                "synchronous_tool_service_total_sec": sum(synchronous_tool_service),
                "mean_synchronous_tool_service_sec": (
                    sum(synchronous_tool_service) / len(synchronous_tool_service)
                    if synchronous_tool_service else None
                ),
                "p95_synchronous_tool_service_sec": percentile(synchronous_tool_service, 95),
                "p99_synchronous_tool_service_sec": percentile(synchronous_tool_service, 99),
                "mean_critical_sec": sum(critical) / len(critical) if critical else None,
                "p50_critical_sec": percentile(critical, 50),
                "p95_critical_sec": percentile(critical, 95),
                "p99_critical_sec": percentile(critical, 99),
                "causal_window_count": len(causal_windows),
                "mean_causal_window_sec": (
                    sum(causal_windows) / len(causal_windows) if causal_windows else None
                ),
                "p50_causal_window_sec": percentile(causal_windows, 50),
                "p95_causal_window_sec": percentile(causal_windows, 95),
                "p99_causal_window_sec": percentile(causal_windows, 99),
                "join_window_count": len(join_windows),
                "mean_join_window_sec": (
                    sum(join_windows) / len(join_windows) if join_windows else None
                ),
                "p50_join_window_sec": percentile(join_windows, 50),
                "p95_join_window_sec": percentile(join_windows, 95),
                "p99_join_window_sec": percentile(join_windows, 99),
                "sessions_per_sec": len(results) / makespan if makespan > 0 else 0.0,
                "unexpected_failure_commands": sum(item["unexpected_failure_commands"] for item in results),
                "allowed_failure_commands": sum(item["allowed_failure_commands"] for item in results),
            },
            "cases": results,
        }
        if any(item.get("command_source") != "recorded" for item in results):
            result["status"] = "fail"
            result["experiment"]["command_source"] = "mixed-or-unknown"
        if capture_summary is not None:
            result["kernel_capture"] = capture_summary
        if flowd_binary is not None:
            result["flowcausal_control"] = {
                "program": str(flowd_binary),
                "pin_dir": args.flowd_pin_dir,
                "resources_measured": args.measure_controller_resources,
                "sessions": flowd_summary,
            }
        write_json(output / "result.json", result)
        if capture is not None and result["status"] == "pass" and not args.keep_kernel_capture:
            remove_capture_artifacts(capture)
            result["kernel_capture"]["raw_retained"] = False
            write_json(output / "result.json", result)
        elif capture_summary is not None:
            result["kernel_capture"]["raw_retained"] = True
            write_json(output / "result.json", result)
        print(json.dumps({"status": result["status"], "result": str(output / "result.json")}, ensure_ascii=False))
        return 0 if result["status"] == "pass" else 1
    finally:
        for item in running:
            if item.flowd is not None and item.flowd.process.poll() is None:
                stop_flowd(item.flowd)
            if item.flowd_socket_dir is not None:
                shutil.rmtree(item.flowd_socket_dir, ignore_errors=True)
            if not item.stdout.closed:
                item.stdout.close()
            if not item.stderr.closed:
                item.stderr.close()
            if item.process.poll() is None:
                try:
                    os.killpg(item.process.pid, signal.SIGTERM)
                    item.process.wait(timeout=5)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(item.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                cid_file = item.directory / "container.cid"
                if cid_file.is_file():
                    cid = cid_file.read_text(encoding="utf-8", errors="replace").strip()
                    if cid:
                        subprocess.run(
                            ["docker", "rm", "-f", cid],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            check=False,
                        )
        if capture is not None and not capture_finished:
            try:
                finish_kernel_capture(capture, min(args.ready_timeout, 30.0))
            except Exception:
                if capture.process.poll() is None:
                    try:
                        os.killpg(capture.process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if not capture.log.closed:
                    capture.log.close()
        if capture is not None:
            shutil.rmtree(capture.socket_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

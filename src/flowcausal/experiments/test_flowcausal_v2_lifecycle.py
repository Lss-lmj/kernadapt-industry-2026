#!/usr/bin/env python3
"""FlowCausal v2 进程身份、工具异常结束和控制程序恢复测试。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import TextIO


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCHEDULER = pathlib.Path("/tmp/scx_flowcausal_v2")
FLOWD = pathlib.Path("/tmp/flowd_v2")
HELPER = pathlib.Path("/tmp/flowcausal_v2_test_helper")
PIN_DIR = pathlib.Path("/sys/fs/bpf/flowcausal_v2")
SCX_STATE = pathlib.Path("/sys/kernel/sched_ext/state")


def stable_id(text: str) -> int:
    value = 1469598103934665603
    for byte in text.encode("utf-8"):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value or 1


def cgroup_path(pid: int) -> pathlib.Path:
    for line in pathlib.Path(f"/proc/{pid}/cgroup").read_text(encoding="utf-8").splitlines():
        if line.startswith("0::"):
            return pathlib.Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    raise RuntimeError(f"无法确定进程 {pid} 的 cgroup")


def wait_until(predicate, *, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise TimeoutError(description)


def privileged(*command: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["sudo", "-n", *command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if check and result.returncode:
        raise RuntimeError(
            f"命令失败：{' '.join(command)}\n{result.stdout}{result.stderr}"
        )
    return result


def helper(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return privileged(str(HELPER), *arguments, check=check)


def start_flowd(
    *,
    session: str,
    root_pid: int,
    socket_path: pathlib.Path,
    log_path: pathlib.Path,
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.Popen[str], TextIO]:
    log = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        [
            "sudo",
            "-n",
            str(FLOWD),
            "--root-pid",
            str(root_pid),
            "--session",
            session,
            "--pin-dir",
            str(PIN_DIR),
            "--serve-socket",
            str(socket_path),
            *extra_args,
        ],
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    def ready() -> bool:
        if process.poll() is not None:
            log.flush()
            raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
        if not socket_path.exists():
            return False
        log.flush()
        text = log_path.read_text(encoding="utf-8", errors="replace")
        return "flowd serving socket=" in text

    wait_until(ready, timeout=10, description="等待 flowd_v2 启动超时")
    return process, log


def controller_pid(cgroup_id: int) -> int:
    result = helper("controller-pid", str(PIN_DIR), str(cgroup_id))
    return int(result.stdout.strip())


def send_request(client: socket.socket, request: str) -> str:
    client.sendall((request + "\n").encode("utf-8"))
    data = bytearray()
    while b"\n" not in data:
        part = client.recv(128)
        if not part:
            raise RuntimeError("控制连接在返回结果前关闭")
        data.extend(part)
    response = data.decode("utf-8", errors="replace").strip()
    if response != "OK":
        raise RuntimeError(f"控制程序拒绝请求：{response}")
    return response


def connect(path: pathlib.Path) -> socket.socket:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(3)
    client.connect(str(path))
    return client


def read_session_stats(session_id: int) -> dict[str, object]:
    result = privileged(
        str(FLOWD), "--pin-dir", str(PIN_DIR), "--operation", "stats",
        "--session-id", str(session_id), "--once",
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise RuntimeError(f"没有取得会话统计：{result.stdout}{result.stderr}")
    return json.loads(lines[-1])


def stop_process(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    if process.poll() is not None:
        return
    privileged("kill", f"-{sig.value}", str(process.pid), check=False)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        privileged("pkill", "-KILL", "-x", "scx_flowcausal_v2", check=False)
        process.wait(timeout=8)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    privileged("true")
    output = args.output or pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-lifecycle-"))
    output.mkdir(parents=True, exist_ok=True)
    session = "flowcausal-v2:lifecycle"
    session_id = stable_id(session)
    original_cgroup = cgroup_path(os.getpid())
    session_cgroup = pathlib.Path("/sys/fs/cgroup") / f"flowcausal-v2-lifecycle-{os.getpid()}"
    cgroup_id = 0
    socket_path = output / "flowd.sock"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    flowd: subprocess.Popen[str] | None = None
    flowd_log: TextIO | None = None
    root_worker: subprocess.Popen[str] | None = None
    checks: dict[str, object] = {}

    try:
        scheduler = subprocess.Popen(
            [
                "sudo",
                "-n",
                str(SCHEDULER),
                "--managed-cpus",
                "0-7",
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled"
            and privileged(
                "test", "-e", str(PIN_DIR / "session_config"), check=False
            ).returncode
            == 0,
            timeout=15,
            description="等待 scx_flowcausal_v2 启动超时",
        )
        # 使用独立会话 cgroup，避免把登录会话中的无关进程一起登记为
        # FlowCausal 任务；实际沙箱部署同样是一会话一 cgroup。
        privileged("mkdir", str(session_cgroup))
        privileged(
            "bash", "-c",
            f"printf '%s\\n' {os.getpid()} > {session_cgroup / 'cgroup.procs'}",
        )
        cgroup_id = session_cgroup.stat().st_ino
        flowd, flowd_log = start_flowd(
            session=session,
            root_pid=os.getpid(),
            socket_path=socket_path,
            log_path=output / "flowd-before-recovery.log",
        )

        # 工具进程以非零状态退出，连接随后中断；控制程序应结束当前工具。
        client = connect(socket_path)
        send_request(client, f"2 tool-begin {session_id} 100 0 1 1 synchronous TEST 0")
        failed_tool = subprocess.Popen(
            ["bash", "-c", "sleep 0.2; exit 17"],
            stdin=subprocess.DEVNULL,
        )
        send_request(
            client,
            f"2 tool-update {session_id} 100 0 1 2 synchronous TEST {failed_tool.pid}",
        )
        returncode = failed_tool.wait(timeout=5)
        if returncode != 17:
            raise RuntimeError(f"异常工具返回状态不符：{returncode}")
        client.close()
        wait_until(
            lambda: helper(
                "assert-tool-ended",
                str(PIN_DIR),
                str(session_id),
                str(cgroup_id),
                "100",
                check=False,
            ).returncode
            == 0,
            timeout=3,
            description="连接中断后工具状态没有清理",
        )
        checks["异常工具退出并断线清理"] = "通过"

        # 将正在运行任务的身份时间替换为不可能值，验证内核拒绝旧进程身份。
        client = connect(socket_path)
        send_request(client, f"2 tool-begin {session_id} 200 0 4 4 synchronous TEST 0")
        reused = subprocess.Popen(
            ["bash", "-c", "read -r value; for ((i=0; i<100000; i++)); do :; done"],
            stdin=subprocess.PIPE,
            text=True,
        )
        send_request(
            client,
            f"2 tool-update {session_id} 200 0 4 5 synchronous TEST {reused.pid}",
        )
        helper(
            "inject-stale",
            str(PIN_DIR),
            str(reused.pid),
            str(session_id),
            str(cgroup_id),
            "200",
            "4",
        )
        assert reused.stdin is not None
        reused.stdin.write("run\n")
        reused.stdin.close()
        if reused.wait(timeout=8) != 0:
            raise RuntimeError("进程身份测试任务失败")

        def reused_observed() -> bool:
            result = helper("reused-count", str(PIN_DIR), str(session_id), check=False)
            return result.returncode == 0 and int(result.stdout.strip() or "0") >= 1

        wait_until(reused_observed, timeout=3, description="未观察到旧进程身份替换")
        reused_replacements = int(
            helper("reused-count", str(PIN_DIR), str(session_id)).stdout.strip()
        )
        checks["进程号复用身份检查"] = "通过"
        send_request(client, f"2 tool-end {session_id} 200 0 4 6 synchronous TEST 0")
        client.close()

        # 工具开始时登记尚未 exec 的真实根进程。随后工具信息从准备阶段
        # 更新为构建阶段，但不覆盖该进程的任务状态；exec 事件必须从
        # 当前工具配置重新取得执行特征，而不是沿用旧值。
        read_fd, write_fd = os.pipe()
        exec_child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,sys; "
                    "os.read(int(sys.argv[1]),1); "
                    "os.execv('/bin/sleep',['sleep','0.5'])"
                ),
                str(read_fd),
            ],
            pass_fds=(read_fd,),
            stdin=subprocess.DEVNULL,
        )
        os.close(read_fd)
        client = connect(socket_path)
        send_request(
            client,
            f"2 tool-begin {session_id} 250 0 5 7 synchronous PREPARE {exec_child.pid}",
        )
        send_request(
            client,
            f"2 tool-update {session_id} 250 0 5 8 synchronous BUILD 0",
        )
        os.write(write_fd, b"x")
        os.close(write_fd)

        def exec_reclassified() -> bool:
            return int(read_session_stats(session_id).get("exec_reclassifications", 0)) >= 1

        wait_until(exec_reclassified, timeout=2, description="exec 后没有重新判断执行特征")
        checks["exec 后根据当前工具信息重新判断执行特征"] = "通过"
        if exec_child.wait(timeout=3) != 0:
            raise RuntimeError("exec 重新判断测试进程失败")
        send_request(client, f"2 tool-end {session_id} 250 0 5 9 synchronous BUILD 0")
        client.close()

        # 模拟会话外创建的服务进程被运行程序迁入会话 cgroup。控制程序
        # 只在核对迁入已经完成后登记它；启动工具返回后，该进程仍保留
        # 服务来源，并按有最长等待限制的后台任务运行。
        service_read, service_write = os.pipe()
        service = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import os,sys; os.read(int(sys.argv[1]),1)",
                str(service_read),
            ],
            pass_fds=(service_read,),
            stdin=subprocess.DEVNULL,
        )
        os.close(service_read)
        privileged(
            "bash", "-c",
            f"printf '%s\\n' {service.pid} > {original_cgroup / 'cgroup.procs'}",
        )
        privileged(
            "bash", "-c",
            f"printf '%s\\n' {service.pid} > {session_cgroup / 'cgroup.procs'}",
        )
        client = connect(socket_path)
        send_request(
            client,
            f"2 tool-begin {session_id} 260 0 6 10 asynchronous SERVICE {service.pid}",
        )
        send_request(client, f"2 tool-end {session_id} 260 0 6 11 asynchronous SERVICE 0")

        def service_classified() -> bool:
            return helper(
                "assert-task-class", str(PIN_DIR), str(service.pid),
                str(session_id), "260", "4", "3", check=False,
            ).returncode == 0

        wait_until(service_classified, timeout=2, description="长期服务没有保留后台归属")
        checks["外部进程迁入后可登记为会话任务"] = "通过"
        checks["工具返回后长期服务仍保留后台归属"] = "通过"
        os.write(service_write, b"x")
        os.close(service_write)
        if service.wait(timeout=3) != 0:
            raise RuntimeError("长期服务测试进程失败")
        client.close()

        # 模拟 C 控制程序被强制结束；时间戳过期后内核停止采用该会话，
        # 新实例清除旧工具状态并恢复同一会话。
        client = connect(socket_path)
        send_request(client, f"2 tool-begin {session_id} 300 0 7 12 synchronous TEST 0")
        old_controller = controller_pid(cgroup_id)
        privileged("kill", "-9", str(old_controller))
        client.close()
        flowd.wait(timeout=8)
        flowd_log.close()
        flowd_log = None
        time.sleep(2.2)
        helper("assert-lease-stale", str(PIN_DIR), str(cgroup_id))
        checks["控制程序失联后会话失效"] = "通过"

        socket_path.unlink(missing_ok=True)
        flowd, flowd_log = start_flowd(
            session=session,
            root_pid=os.getpid(),
            socket_path=socket_path,
            log_path=output / "flowd-after-recovery.log",
        )
        helper("assert-no-tools", str(PIN_DIR), str(session_id), str(cgroup_id))
        checks["控制程序重新接管并清理旧状态"] = "通过"
        new_controller = controller_pid(cgroup_id)
        privileged("kill", f"-{signal.SIGINT.value}", str(new_controller))
        if flowd.wait(timeout=8) != 0:
            raise RuntimeError("恢复后的控制程序退出状态异常")
        flowd_log.close()
        flowd_log = None
        helper("assert-clean", str(PIN_DIR), str(session_id), str(cgroup_id))
        checks["会话关闭后状态清理"] = "通过"
        flowd = None

        # 根进程结束后，长驻控制程序停止更新时间并主动清理会话。
        root_worker = subprocess.Popen(["sleep", "30"], stdin=subprocess.DEVNULL)
        socket_path.unlink(missing_ok=True)
        flowd, flowd_log = start_flowd(
            session=session,
            root_pid=root_worker.pid,
            socket_path=socket_path,
            log_path=output / "flowd-root-exit.log",
        )
        root_worker.terminate()
        root_worker.wait(timeout=3)
        root_worker = None
        if flowd.wait(timeout=3) != 0:
            raise RuntimeError("根进程结束后的控制程序退出状态异常")
        flowd_log.close()
        flowd_log = None
        flowd = None
        helper("assert-clean", str(PIN_DIR), str(session_id), str(cgroup_id))
        checks["根进程异常消失后自动清理会话"] = "通过"

        result = {
            "schema": "flowcausal.v2.lifecycle-test.v1",
            "status": "pass",
            "session_id": session_id,
            "cgroup_id": cgroup_id,
            "cpu_set": "0-7",
            "online_runtime": "C + eBPF + sched_ext",
            "reused_pid_replacements": reused_replacements,
            "heartbeat_timeout_ns": 2_000_000_000,
            "checks": checks,
        }
        (output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (output / "结果摘要.md").write_text(
            "# FlowCausal v2 异常路径测试\n\n"
            + "在线路径使用 C、eBPF 和 sched_ext，不使用 Python。Python 只负责启动测试和核对结果。\n\n"
            + f"内核观察到 {reused_replacements} 次旧进程身份替换。\n\n"
            + "| 检查项 | 结果 |\n|---|---|\n"
            + "".join(f"| {name} | {value} |\n" for name, value in checks.items()),
            encoding="utf-8",
        )
        print(output)
        return 0
    finally:
        if flowd is not None and flowd.poll() is None:
            try:
                privileged("kill", "-9", str(controller_pid(cgroup_id)), check=False)
            except Exception:
                pass
        if flowd is not None:
            try:
                flowd.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        if flowd_log is not None:
            flowd_log.close()
        if root_worker is not None and root_worker.poll() is None:
            root_worker.kill()
            root_worker.wait(timeout=3)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGINT)
        scheduler_log.close()
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "disabled",
            timeout=10,
            description="scx_flowcausal_v2 未退出",
        )
        if session_cgroup.exists():
            privileged(
                "bash", "-c",
                f"printf '%s\\n' {os.getpid()} > {original_cgroup / 'cgroup.procs'}",
                check=False,
            )
            privileged("rmdir", str(session_cgroup), check=False)


if __name__ == "__main__":
    raise SystemExit(main())

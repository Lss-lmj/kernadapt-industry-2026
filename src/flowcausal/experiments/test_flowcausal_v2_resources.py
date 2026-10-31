#!/usr/bin/env python3
"""核对第二版会话公平、空闲借用、cpu.max 和 cpuset 边界。"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import signal
import statistics
import subprocess
import tempfile
import time
from dataclasses import dataclass

from test_flowcausal_v2_lifecycle import (
    FLOWD,
    PIN_DIR,
    SCX_STATE,
    SCHEDULER,
    privileged,
    stable_id,
    stop_process,
    wait_until,
)


def write_privileged(path: pathlib.Path, value: str) -> None:
    result = subprocess.run(
        ["sudo", "-n", "tee", str(path)],
        input=value,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"无法写入 {path}：{result.stderr}")


def cpu_usage_us(cgroup: pathlib.Path) -> int:
    for line in (cgroup / "cpu.stat").read_text(encoding="utf-8").splitlines():
        name, value = line.split()
        if name == "usage_usec":
            return int(value)
    raise RuntimeError(f"{cgroup}/cpu.stat 缺少 usage_usec")


@dataclass
class Session:
    name: str
    session_id: int
    cgroup: pathlib.Path
    process: subprocess.Popen[bytes]


class Environment:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.sessions: list[Session] = []
        self.counter = 0

    def create(
        self,
        *,
        threads: int,
        weight: int = 100,
        cpuset: str = "",
        cpu_max: str = "",
        timeout: int = 30,
    ) -> Session:
        self.counter += 1
        name = f"flowcausal-v2:resources:{os.getpid()}:{self.counter}"
        cgroup = self.root / f"session-{self.counter}"
        privileged("mkdir", str(cgroup))
        write_privileged(cgroup / "cpu.weight", f"{weight}\n")
        if cpuset:
            write_privileged(cgroup / "cpuset.cpus", cpuset + "\n")
        if cpu_max:
            write_privileged(cgroup / "cpu.max", cpu_max + "\n")
        process = subprocess.Popen(
            [
                "taskset", "-c", "0-7", "bash", "-c",
                f"read -r start; exec stress-ng --cpu {threads} --timeout {timeout}s --quiet",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        write_privileged(cgroup / "cgroup.procs", f"{process.pid}\n")
        result = privileged(
            str(FLOWD), "--pin-dir", str(PIN_DIR),
            "--operation", "session-open", "--session", name,
            "--cgroup", str(cgroup), "--root-pid", str(process.pid), "--once",
        )
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        session = Session(
            name=name, session_id=stable_id(name), cgroup=cgroup, process=process
        )
        self.sessions.append(session)
        return session

    @staticmethod
    def start(*sessions: Session) -> None:
        for session in sessions:
            if session.process.stdin is None:
                raise RuntimeError("任务启动管道不存在")
            session.process.stdin.write(b"start\n")
            session.process.stdin.close()

    @staticmethod
    def terminate(session: Session) -> None:
        if session.process.poll() is None:
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                session.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                session.process.wait(timeout=5)

    def close(self, session: Session) -> None:
        self.terminate(session)
        privileged(
            str(FLOWD), "--pin-dir", str(PIN_DIR),
            "--operation", "session-close", "--session", session.name,
            "--cgroup", str(session.cgroup), "--once", check=False,
        )
        privileged("rmdir", str(session.cgroup), check=False)
        if session in self.sessions:
            self.sessions.remove(session)

    def close_all(self) -> None:
        for session in list(reversed(self.sessions)):
            self.close(session)


def usage_deltas(
    sessions: list[Session], duration: float, warmup: float = 0.5
) -> tuple[list[int], float]:
    """启动负载并在预热后测量稳态处理器时间。

    stress-ng 会先创建工作进程。若从写启动管道前计时，不同线程数的
    创建开销会被误当成调度结果，尤其会干扰单线程与宽会话、cpu.max
    这两项短时间测试。
    """
    Environment.start(*sessions)
    time.sleep(warmup)
    before = [cpu_usage_us(session.cgroup) for session in sessions]
    started = time.monotonic()
    time.sleep(duration)
    elapsed = time.monotonic() - started
    after = [cpu_usage_us(session.cgroup) for session in sessions]
    return [end - start for start, end in zip(before, after)], elapsed


def steady_usage_samples(
    sessions: list[Session], *, rounds: int, duration: float, warmup: float = 0.5
) -> tuple[list[list[int]], list[float]]:
    """在同一次负载运行中重复测量，供容易受短时干扰的检查取中位数。"""
    Environment.start(*sessions)
    time.sleep(warmup)
    samples: list[list[int]] = []
    elapsed_samples: list[float] = []
    before = [cpu_usage_us(session.cgroup) for session in sessions]
    for _ in range(rounds):
        started = time.monotonic()
        time.sleep(duration)
        elapsed = time.monotonic() - started
        after = [cpu_usage_us(session.cgroup) for session in sessions]
        samples.append([end - start for start, end in zip(before, after)])
        elapsed_samples.append(elapsed)
        before = after
    return samples, elapsed_samples


def ratio(high: int, low: int) -> float:
    return high / max(low, 1)


def session_stats(session: Session) -> dict[str, object]:
    result = privileged(
        str(FLOWD), "--pin-dir", str(PIN_DIR), "--operation", "stats",
        "--session-id", str(session.session_id), "--once",
    )
    lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def sample_cpus(cgroup: pathlib.Path, duration: float) -> list[int]:
    observed: set[int] = set()
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        thread_file = cgroup / "cgroup.threads"
        for text in thread_file.read_text(encoding="utf-8").split():
            stat = pathlib.Path(f"/proc/{text}/stat")
            try:
                fields = stat.read_text(encoding="utf-8").split()
            except FileNotFoundError:
                continue
            if len(fields) >= 39:
                observed.add(int(fields[38]))
        time.sleep(0.02)
    return sorted(observed)


def main() -> int:
    parser = argparse.ArgumentParser(description="核对 FlowCausal v2 的会话公平与资源边界")
    parser.add_argument(
        "--scheduler-arg", action="append", default=[],
        help="附加调度器参数，可重复使用",
    )
    args = parser.parse_args()
    if SCX_STATE.read_text(encoding="utf-8").strip() != "disabled":
        raise SystemExit("已有 sched_ext 调度器在运行，拒绝覆盖")
    if not any((pathlib.Path(item) / "stress-ng").exists() for item in os.environ["PATH"].split(os.pathsep)):
        raise SystemExit("缺少 stress-ng")
    privileged("true")
    output = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-v2-resources-"))
    cgroup_root = pathlib.Path("/sys/fs/cgroup") / f"flowcausal-v2-resources-{os.getpid()}"
    scheduler_log = (output / "scheduler.log").open("w", encoding="utf-8")
    scheduler: subprocess.Popen[str] | None = None
    env: Environment | None = None
    rows: dict[str, object] = {}
    checks: dict[str, bool] = {}

    try:
        scheduler = subprocess.Popen(
            [
                "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
                "--quiet", *args.scheduler_arg,
            ],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled",
            timeout=15,
            description="等待第二版调度器启动超时",
        )
        privileged("mkdir", str(cgroup_root))
        write_privileged(cgroup_root / "cgroup.subtree_control", "+cpu +cpuset\n")
        env = Environment(cgroup_root)

        # 两个宽会话都饱和时，线程数不影响会话级分配，权重决定长期比例。
        for label, weights, bounds in (
            ("权重一比四", (100, 400), (2.6, 5.6)),
            ("权重一比一", (100, 100), (0.75, 1.34)),
            ("权重一比二", (100, 200), (1.45, 2.8)),
        ):
            first = env.create(threads=32, weight=weights[0])
            second = env.create(threads=32, weight=weights[1])
            deltas, elapsed = usage_deltas([first, second], 3.0)
            observed_ratio = ratio(deltas[1], deltas[0])
            rows[label] = {
                "weights": list(weights), "cpu_usage_us": deltas,
                "observed_ratio": observed_ratio, "elapsed_sec": elapsed,
                "scheduler_stats": [session_stats(first), session_stats(second)],
            }
            checks[label] = bounds[0] <= observed_ratio <= bounds[1]
            env.close(first)
            env.close(second)

        # 单线程无法使用四个处理器，其余空闲能力应由宽会话借用。
        one = env.create(threads=1)
        wide = env.create(threads=64)
        # 单线程进程对同一台机器上的短时中断较敏感。连续测三段并取
        # 中位数，既不忽略异常，也避免一次三秒抖动决定整个核对结果。
        samples, elapsed_samples = steady_usage_samples(
            [one, wide], rounds=3, duration=2.0
        )
        one_core_samples = [
            deltas[0] / (elapsed * 1_000_000)
            for deltas, elapsed in zip(samples, elapsed_samples)
        ]
        total_core_samples = [
            sum(deltas) / (elapsed * 1_000_000)
            for deltas, elapsed in zip(samples, elapsed_samples)
        ]
        one_cores = statistics.median(one_core_samples)
        total_cores = statistics.median(total_core_samples)
        rows["一线程与六十四线程"] = {
            "cpu_usage_us_samples": samples,
            "elapsed_sec_samples": elapsed_samples,
            "single_session_core_samples": one_core_samples,
            "total_core_samples": total_core_samples,
            "single_session_cores_median": one_cores,
            "total_cores_median": total_cores,
            "scheduler_stats": [session_stats(one), session_stats(wide)],
        }
        checks["单线程会话未被宽会话饿死"] = one_cores >= 0.70
        checks["空闲处理器被宽会话借用"] = total_cores >= 6.0
        env.close(one)
        env.close(wide)

        # 原始设计要求在 1、4、8 个会话下都能稳定运行。这里测量
        # 等权饱和会话的实际处理器时间，并同时检查公平误差和利用率。
        for count in (4, 8):
            equal_sessions = [env.create(threads=16) for _ in range(count)]
            deltas, elapsed = usage_deltas(equal_sessions, 3.0)
            expected = sum(deltas) / count
            relative_errors = [abs(value - expected) / max(expected, 1) for value in deltas]
            utilization = sum(deltas) / (elapsed * 1_000_000)
            label = f"{count}个等权会话"
            rows[label] = {
                "cpu_usage_us": deltas,
                "maximum_relative_error": max(relative_errors),
                "total_cores": utilization,
                "elapsed_sec": elapsed,
            }
            checks[f"{count}个等权会话公平误差"] = max(relative_errors) <= 0.25
            checks[f"{count}个等权会话使用处理器"] = utilization >= 6.0
            for equal_session in equal_sessions:
                env.close(equal_session)

        # 新会话加入后，只比较共同运行的时间段。
        old = env.create(threads=32)
        env.start(old)
        time.sleep(1.0)
        newcomer = env.create(threads=32)
        before = [cpu_usage_us(old.cgroup), cpu_usage_us(newcomer.cgroup)]
        env.start(newcomer)
        time.sleep(2.0)
        after = [cpu_usage_us(old.cgroup), cpu_usage_us(newcomer.cgroup)]
        deltas = [after[i] - before[i] for i in range(2)]
        arrival_ratio = ratio(deltas[1], deltas[0])
        rows["新会话加入"] = {"cpu_usage_us": deltas, "observed_ratio": arrival_ratio}
        checks["新会话及时获得处理器"] = 0.65 <= arrival_ratio <= 1.55
        env.close(old)
        env.close(newcomer)

        # 一个会话先独占运行，再空闲；恢复后不应凭空获得累计优势。
        sleeper = env.create(threads=32)
        peer = env.create(threads=32)
        env.start(sleeper)
        time.sleep(0.8)
        os.killpg(sleeper.process.pid, signal.SIGSTOP)
        env.start(peer)
        time.sleep(0.8)
        before = [cpu_usage_us(sleeper.cgroup), cpu_usage_us(peer.cgroup)]
        os.killpg(sleeper.process.pid, signal.SIGCONT)
        time.sleep(2.0)
        after = [cpu_usage_us(sleeper.cgroup), cpu_usage_us(peer.cgroup)]
        deltas = [after[i] - before[i] for i in range(2)]
        resumed_ratio = ratio(deltas[0], deltas[1])
        rows["空闲会话恢复"] = {"cpu_usage_us": deltas, "observed_ratio": resumed_ratio}
        checks["空闲会话没有累计异常优势"] = 0.65 <= resumed_ratio <= 1.55
        env.close(sleeper)
        env.close(peer)

        # cgroup cpuset 与管理员配置的处理器范围取交集。
        placed = env.create(threads=32, cpuset="0-3")
        env.start(placed)
        observed = sample_cpus(placed.cgroup, 2.0)
        rows["处理器范围"] = {"cpuset": "0-3", "observed_cpus": observed}
        checks["cpuset 未被突破"] = bool(observed) and set(observed) <= set(range(4))
        env.close(placed)

        # 严格处理器时间上限需要让最后一个可运行任务也回到派发路径。
        # 前面的会话公平和 cpuset 在普通方式下测试；这里单独重启调度器。
        stop_process(scheduler, signal.SIGTERM)
        scheduler = subprocess.Popen(
            [
                "sudo", "-n", str(SCHEDULER), "--managed-cpus", "0-7",
                "--enforce-cpu-max", "--quiet", *args.scheduler_arg,
            ],
            stdin=subprocess.DEVNULL,
            stdout=scheduler_log,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        wait_until(
            lambda: SCX_STATE.read_text(encoding="utf-8").strip() == "enabled",
            timeout=15,
            description="等待处理器时间限制方式启动超时",
        )

        # cgroup CPU 上限由调度器按周期核算，特殊路径也不能绕过。
        limited = env.create(threads=32, cpu_max="200000 100000")
        deltas, elapsed = usage_deltas([limited], 3.0)
        limited_cores = deltas[0] / (elapsed * 1_000_000)
        rows["处理器时间上限"] = {
            "cpu_usage_us": deltas[0], "average_cores": limited_cores,
            "cpu_max": "200000 100000", "elapsed_sec": elapsed,
        }
        stats_result = privileged(
            str(FLOWD), "--pin-dir", str(PIN_DIR), "--operation", "stats",
            "--session-id", str(limited.session_id), "--once",
        )
        stats_lines = [line for line in stats_result.stdout.splitlines() if line.startswith("{")]
        if stats_lines:
            rows["处理器时间上限"]["scheduler_stats"] = json.loads(stats_lines[-1])
        checks["cpu.max 未被突破"] = 1.5 <= limited_cores <= 2.35
        env.close(limited)

        result = {
            "schema": "flowcausal.v2.resources-test.v1",
            "status": "pass" if all(checks.values()) else "fail",
            "cpu_set": "0-7",
            "scheduler_args": args.scheduler_arg,
            "results": rows,
            "checks": checks,
        }
        (output / "结果.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(output)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if all(checks.values()) else 1
    finally:
        if env is not None:
            env.close_all()
        privileged("rmdir", str(cgroup_root), check=False)
        if scheduler is not None:
            stop_process(scheduler, signal.SIGTERM)
        scheduler_log.close()


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run multiple FlowCausal agent traces concurrently as a harness replay mix.

This is the first HarnessReplayBench colocated-runner.  It does not load a
scheduler by itself; instead it provides a repeatable multi-sandbox workload
shell that can be placed under Linux default, taskset/cgroup baselines, or a
flowcausal/sched_ext variant by an outer matrix runner.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

from harness_replay_cleanup import DEFAULT_SUCCESS_ARTIFACTS, compact_case_artifacts
from harness_replay_system_profile import SystemProfileRecorder, disabled_profile


HERE = pathlib.Path(__file__).resolve().parent
FLOWCAUSAL_ROOT = HERE.parent
WORKLOAD = HERE / "workloads" / "agent_trace_workflow.py"
STATIC_CGROUP_WRAPPER = HERE / "run_static_cgroup.py"
SCHEMA = "flowcausal.harness_replay_mix.v1"
DOCKER_REPLAY_DEFAULT_ENV = (
    "HOME=/tmp/flowcausal-home",
    "XDG_CACHE_HOME=/tmp/flowcausal-cache",
    "PIP_CACHE_DIR=/tmp/flowcausal-pip-cache",
)


@dataclass(frozen=True)
class ReplayCase:
    instance_id: str
    trace_file: pathlib.Path
    repo_dir: pathlib.Path
    start_offset_ms: int
    container_image: str = ""
    work_dir_aliases: tuple[str, ...] = ()
    container_env_entries: tuple[str, ...] = ()
    docker_user: str = ""


@dataclass
class RunningCase:
    case: ReplayCase
    run_dir: pathlib.Path
    stdout_path: pathlib.Path
    stderr_path: pathlib.Path
    process: subprocess.Popen[Any]
    started_monotonic: float
    started_wall_ns: int
    started_offset_sec: float
    docker_identity: dict[str, Any]
    controller_process: subprocess.Popen[Any] | None = None
    controller_cmd: list[str] | None = None
    controller_log: pathlib.Path | None = None
    controller_json_log: pathlib.Path | None = None
    controller_root_pid: int | None = None
    controller_started_monotonic: float | None = None
    controller_status: str = "disabled"
    flowd_process: subprocess.Popen[Any] | None = None
    flowd_cmd: list[str] | None = None
    flowd_log: pathlib.Path | None = None
    flowd_root_pid: int | None = None
    flowd_anchor_cpu: int = -1
    flowd_started_monotonic: float | None = None
    flowd_status: str = "disabled"
    system_profile: SystemProfileRecorder | None = None


def parse_env(entries: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for entry in entries:
        if "=" not in entry:
            raise SystemExit(f"--env expects KEY=VALUE, got {entry!r}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"--env has empty key: {entry!r}")
        env[key] = value
    return env


def expand_env_values(entries: list[str], *, flag: str) -> list[str]:
    expanded: list[str] = []
    for entry in entries:
        if "=" not in entry:
            raise SystemExit(f"{flag} expects KEY=VALUE, got {entry!r}")
        key, value = entry.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"{flag} has empty key: {entry!r}")
        expanded.append(f"{key}={os.path.expandvars(value)}")
    return expanded


def normalize_env_entries(value: Any, *, field: str, case_id: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, dict):
        entries = [f"{key}={val}" for key, val in value.items()]
    elif isinstance(value, list) and all(isinstance(entry, str) for entry in value):
        entries = list(value)
    else:
        raise SystemExit(f"manifest case {case_id} has invalid {field}; expected list[str] or object")
    parse_env(entries)
    return tuple(entries)


def merge_env_entries(*entry_groups: list[str] | tuple[str, ...]) -> list[str]:
    merged: dict[str, str] = {}
    order: list[str] = []
    for entries in entry_groups:
        for entry in entries:
            if "=" not in entry:
                raise SystemExit(f"--env expects KEY=VALUE, got {entry!r}")
            key, _value = entry.split("=", 1)
            key = key.strip()
            if not key:
                raise SystemExit(f"--env has empty key: {entry!r}")
            if key not in merged:
                order.append(key)
            merged[key] = entry
    return [merged[key] for key in order]


def effective_container_env_entries(
    *,
    container_path_env: str,
    env_entries: list[str],
    case_container_env_entries: tuple[str, ...],
    container_env_entries: list[str],
) -> list[str]:
    path_env = (f"PATH={container_path_env}",) if container_path_env else ()
    return merge_env_entries(
        DOCKER_REPLAY_DEFAULT_ENV,
        path_env,
        env_entries,
        case_container_env_entries,
        container_env_entries,
    )


def validate_nice_value(value: int) -> int:
    if value < -20 or value > 19:
        raise SystemExit(f"--nice expects a Linux nice value in [-20, 19], got {value}")
    return value


def validate_cgroup_cpu_weight(value: int) -> int:
    if value == 0:
        return value
    if value < 1 or value > 10000:
        raise SystemExit(f"--cgroup-cpu-weight expects 0 or a cgroup-v2 cpu.weight in [1, 10000], got {value}")
    return value


def parse_cpu_list(value: str) -> list[int]:
    """Parse a taskset-style CPU list, preserving order and removing repeats."""

    cpus: list[int] = []
    seen: set[int] = set()
    text = value.strip()
    if not text:
        return cpus
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            raise SystemExit(f"CPU list has an empty element: {value!r}")
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError as exc:
                raise SystemExit(f"CPU list has invalid range {part!r}: {value!r}") from exc
            if start < 0 or end < 0 or end < start:
                raise SystemExit(f"CPU list has invalid range {part!r}: {value!r}")
            values = range(start, end + 1)
        else:
            try:
                cpu = int(part)
            except ValueError as exc:
                raise SystemExit(f"CPU list has invalid CPU {part!r}: {value!r}") from exc
            if cpu < 0:
                raise SystemExit(f"CPU list has invalid CPU {part!r}: {value!r}")
            values = (cpu,)
        for cpu in values:
            if cpu not in seen:
                cpus.append(cpu)
                seen.add(cpu)
    return cpus


def resolve_flowd_anchor_cpu(args: argparse.Namespace, case_index: int) -> int:
    policy = getattr(args, "flowd_anchor_policy", "fixed")
    if policy == "none":
        return -1
    if policy == "fixed":
        return int(getattr(args, "flowd_anchor_cpu", -1))
    if policy == "round-robin":
        source = getattr(args, "flowd_anchor_cpus", "") or getattr(args, "taskset", "")
        cpus = parse_cpu_list(source)
        if not cpus:
            raise SystemExit("--flowd-anchor-policy round-robin needs --flowd-anchor-cpus or --taskset")
        return cpus[case_index % len(cpus)]
    raise SystemExit(f"unsupported --flowd-anchor-policy: {policy!r}")


def parse_cgroup_cpu_max(value: str) -> tuple[str, int] | None:
    text = value.strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2:
        raise SystemExit("--cgroup-cpu-max expects cpu.max format: 'max PERIOD' or 'QUOTA PERIOD'")
    quota, period_text = parts
    try:
        period = int(period_text)
    except ValueError as exc:
        raise SystemExit(f"--cgroup-cpu-max has invalid period: {value!r}") from exc
    if period <= 0:
        raise SystemExit(f"--cgroup-cpu-max period must be positive: {value!r}")
    if quota != "max":
        try:
            quota_value = int(quota)
        except ValueError as exc:
            raise SystemExit(f"--cgroup-cpu-max has invalid quota: {value!r}") from exc
        if quota_value <= 0:
            raise SystemExit(f"--cgroup-cpu-max quota must be positive or 'max': {value!r}")
    return quota, period


def cgroup_weight_to_docker_cpu_shares(weight: int) -> int:
    """Convert cgroup-v2 cpu.weight to Docker's cpu-shares scale.

    Docker exposes the older shares interface on its CLI.  With a cgroup-v2
    host, runtimes translate shares to weight; use the inverse of the common
    OCI mapping so the configured value remains close to the requested
    cpu.weight.  This is provenance for a static Docker CPU baseline, not a
    claim that Docker exposes an exact cpu.weight flag.
    """

    validate_cgroup_cpu_weight(weight)
    if weight == 0:
        return 0
    return max(2, min(262144, 2 + ((weight - 1) * 262142) // 9999))


def cgroup_settings_provenance(
    case: ReplayCase,
    *,
    cpu_weight: int,
    cpu_max: str,
    cgroup_wrapper: str,
    cgroup_sudo: str,
    cgroup_name_prefix: str,
) -> dict[str, Any]:
    cpu_max_parsed = parse_cgroup_cpu_max(cpu_max)
    enabled = bool(cpu_weight or cpu_max_parsed)
    if not enabled:
        return {"enabled": False}
    payload: dict[str, Any] = {
        "enabled": True,
        "cpu_weight": cpu_weight,
        "cpu_max": cpu_max.strip(),
    }
    if case.container_image:
        payload["mode"] = "docker_cpu_flags"
        shares = cgroup_weight_to_docker_cpu_shares(cpu_weight)
        payload["docker_cpu_shares"] = shares
        if cpu_max_parsed:
            quota, period = cpu_max_parsed
            payload["docker_cpu_period"] = period
            payload["docker_cpu_quota"] = None if quota == "max" else int(quota)
    else:
        payload.update(
            {
                "mode": "host_static_cgroup_wrapper",
                "wrapper": str(pathlib.Path(cgroup_wrapper).resolve()),
                "sudo": cgroup_sudo,
                "name_prefix": cgroup_name_prefix,
            }
        )
    return payload


def safe_name(value: str) -> str:
    out = []
    for char in value:
        if char.isalnum() or char in {"-", "_", "."}:
            out.append(char)
        else:
            out.append("_")
    return "".join(out).strip("._") or "case"


def cleanup_case_artifacts(run_dir: pathlib.Path) -> None:
    """Remove stale per-case control/profile files before a new trial.

    ``--overwrite`` allows reusing an output directory, but stale stage-state or
    frontier-label files can mislead live controllers and system profilers
    before the new workload publishes its first stage marker.  Only known
    per-case metadata files are removed here; work tree cleanup remains the
    workload wrapper's responsibility.
    """

    for name in (
        "stage-state.json",
        "frontier-labels.jsonl",
        "controller.log",
        "controller.ndjson",
        "flowd.log",
        "system-profile.ndjson",
        "system-profile-summary.json",
        "system-profile-summary.md",
        "container.cid",
    ):
        try:
            (run_dir / name).unlink()
        except FileNotFoundError:
            continue


def parse_case_spec(spec: str, *, index: int, stagger_ms: int) -> ReplayCase:
    if "=" not in spec or "::" not in spec:
        raise SystemExit(
            "--case expects INSTANCE=TRACE::REPO or INSTANCE=TRACE::REPO::IMAGE, for example "
            "django__django-14608=traces/django.json::repos/django::swebench/image:latest"
        )
    instance_id, rest = spec.split("=", 1)
    parts = rest.split("::")
    if len(parts) not in {2, 3}:
        raise SystemExit(f"--case expects two or three :: fields: {spec!r}")
    trace_text, repo_text = parts[0], parts[1]
    image = parts[2] if len(parts) == 3 else ""
    instance_id = instance_id.strip()
    if not instance_id:
        raise SystemExit(f"--case has empty instance id: {spec!r}")
    return ReplayCase(
        instance_id=instance_id,
        trace_file=pathlib.Path(trace_text).resolve(),
        repo_dir=pathlib.Path(repo_text).resolve(),
        start_offset_ms=index * stagger_ms,
        container_image=image.strip(),
        work_dir_aliases=(),
        container_env_entries=(),
        docker_user="",
    )


def manifest_path_base(data: Any, path: pathlib.Path) -> pathlib.Path:
    if not isinstance(data, dict):
        return pathlib.Path.cwd()
    base = str(data.get("path_base") or "").strip()
    if not base:
        return pathlib.Path.cwd()
    if base == "benchmark_root":
        return (FLOWCAUSAL_ROOT / "benchmarks" / "harness-replaybench").resolve()
    if base == "flowcausal_root":
        return FLOWCAUSAL_ROOT.resolve()
    if base == "manifest_dir":
        return path.parent.resolve()
    raw = pathlib.Path(base)
    if raw.is_absolute():
        return raw.resolve()
    return (path.parent / raw).resolve()


def resolve_manifest_path(value: Any, base_dir: pathlib.Path) -> pathlib.Path:
    path = pathlib.Path(str(value))
    if path.is_absolute():
        return path.resolve()
    return (base_dir / path).resolve()


def load_manifest(path: pathlib.Path, *, stagger_ms: int) -> list[ReplayCase]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases_raw = data.get("cases", data.get("instances")) if isinstance(data, dict) else data
    if not isinstance(cases_raw, list):
        raise SystemExit("manifest expects a list or an object with cases/instances")
    base_dir = manifest_path_base(data, path)
    cases: list[ReplayCase] = []
    for index, item in enumerate(cases_raw):
        if not isinstance(item, dict):
            raise SystemExit(f"manifest case {index} is not an object")
        instance_id = str(item.get("instance_id") or item.get("id") or f"case-{index}")
        trace_file = item.get("trace_file") or item.get("trace") or item.get("normalized_trace")
        repo_dir = item.get("repo_dir") or item.get("repo")
        image = str(item.get("container_image") or item.get("image") or "")
        aliases_raw = item.get("work_dir_aliases") or item.get("work_dir_alias") or []
        if isinstance(aliases_raw, str):
            work_dir_aliases = (aliases_raw,)
        elif isinstance(aliases_raw, list) and all(isinstance(alias, str) for alias in aliases_raw):
            work_dir_aliases = tuple(aliases_raw)
        else:
            raise SystemExit(f"manifest case {instance_id} has invalid work_dir_aliases")
        container_env_entries = normalize_env_entries(
            item.get("container_env"),
            field="container_env",
            case_id=instance_id,
        )
        case_docker_user = str(item.get("docker_user") or item.get("user") or "").strip()
        if not trace_file or not repo_dir:
            raise SystemExit(f"manifest case {instance_id} needs trace_file and repo_dir")
        offset = int(item.get("start_offset_ms", index * stagger_ms) or 0)
        cases.append(
            ReplayCase(
                instance_id=instance_id,
                trace_file=resolve_manifest_path(trace_file, base_dir),
                repo_dir=resolve_manifest_path(repo_dir, base_dir),
                start_offset_ms=offset,
                container_image=image,
                work_dir_aliases=work_dir_aliases,
                container_env_entries=container_env_entries,
                docker_user=case_docker_user,
            )
        )
    return cases


def validate_cases(cases: list[ReplayCase]) -> None:
    if not cases:
        raise SystemExit("no replay cases configured")
    for case in cases:
        if not case.trace_file.is_file():
            raise SystemExit(f"trace not found for {case.instance_id}: {case.trace_file}")
        if not case.repo_dir.is_dir():
            raise SystemExit(f"repo dir not found for {case.instance_id}: {case.repo_dir}")


def load_trace_command_count(path: pathlib.Path) -> int:
    data = json.loads(path.read_text(encoding="utf-8"))
    commands = data.get("commands") if isinstance(data, dict) else data
    return len(commands) if isinstance(commands, list) else 0


def relative_to_flowcausal(path: pathlib.Path) -> str:
    try:
        return str(path.resolve().relative_to(FLOWCAUSAL_ROOT.resolve()))
    except ValueError as exc:
        raise SystemExit(
            f"Docker replay currently expects trace files under {FLOWCAUSAL_ROOT}: {path}"
        ) from exc


def docker_user_arg(value: str) -> str:
    if value == "current":
        return f"{os.getuid()}:{os.getgid()}"
    if value == "root":
        return "0:0"
    return value


def sudo_prefix(*, dry_run: bool, mode: str) -> list[str]:
    if mode == "never" or dry_run or os.geteuid() == 0:
        return []
    if mode == "always" or mode == "auto":
        return ["sudo", "-n"]
    raise SystemExit(f"unsupported sudo mode: {mode}")


def build_workload_cmd(
    case: ReplayCase,
    run_dir: pathlib.Path,
    *,
    taskset: str,
    nice: int,
    allow_failure_stages: list[str],
    env_entries: list[str],
    host_env_entries: list[str],
    container_env_entries: list[str],
    copy_keep_git: bool,
    copy_exclude: list[str],
    container_python: str,
    container_path_env: str,
    docker_user: str,
    cgroup_cpu_weight: int,
    cgroup_cpu_max: str,
    cgroup_wrapper: str,
    cgroup_sudo: str,
    cgroup_name_prefix: str,
    start_gate_file: str | None,
) -> list[str]:
    cmd = [
        sys.executable,
        str(WORKLOAD),
        "--trace-file",
        str(case.trace_file),
        "--repo-dir",
        str(case.repo_dir),
        "--work-dir",
        str(run_dir / "work"),
        "--overwrite",
        "--stage-state-file",
        str(run_dir / "stage-state.json"),
        "--frontier-label-file",
        str(run_dir / "frontier-labels.jsonl"),
        "--stage-out-dir",
        str(run_dir / "stage-out"),
        "--pre-sleep-ms",
        "0",
        "--post-sleep-ms",
        "0",
    ]
    if start_gate_file:
        cmd.extend(["--start-gate-file", start_gate_file])
    if copy_keep_git:
        cmd.append("--copy-keep-git")
    for pattern in copy_exclude:
        cmd.extend(["--copy-exclude", pattern])
    for stage in allow_failure_stages:
        cmd.extend(["--allow-failure-stage", stage])
    for alias in case.work_dir_aliases:
        cmd.extend(["--work-dir-alias", alias])
    for entry in env_entries:
        cmd.extend(["--env", entry])
    if not case.container_image:
        for entry in host_env_entries:
            cmd.extend(["--env", entry])
    if case.container_image:
        container_start_gate_file = None
        if start_gate_file:
            container_start_gate_file = f"/out/{pathlib.Path(start_gate_file).name}"
        trace_rel = relative_to_flowcausal(case.trace_file)
        container_cmd = [
            container_python,
            "/flowcausal/experiments/workloads/agent_trace_workflow.py",
            "--trace-file",
            f"/flowcausal/{trace_rel}",
            "--repo-dir",
            "/repo",
            "--work-dir",
            "/out/work",
            "--overwrite",
            "--stage-state-file",
            "/out/stage-state.json",
            "--frontier-label-file",
            "/out/frontier-labels.jsonl",
            "--stage-out-dir",
            "/out/stage-out",
            "--pre-sleep-ms",
            "0",
            "--post-sleep-ms",
            "0",
        ]
        if start_gate_file:
            container_cmd.extend(["--start-gate-file", container_start_gate_file or start_gate_file])
        if copy_keep_git:
            container_cmd.append("--copy-keep-git")
        for pattern in copy_exclude:
            container_cmd.extend(["--copy-exclude", pattern])
        for stage in allow_failure_stages:
            container_cmd.extend(["--allow-failure-stage", stage])
        for alias in case.work_dir_aliases:
            container_cmd.extend(["--work-dir-alias", alias])
        effective_container_env = effective_container_env_entries(
            container_path_env=container_path_env,
            env_entries=env_entries,
            case_container_env_entries=case.container_env_entries,
            container_env_entries=container_env_entries,
        )
        for entry in effective_container_env:
            container_cmd.extend(["--env", entry])
        if nice != 0:
            container_cmd = ["nice", "-n", str(nice), *container_cmd]
        effective_docker_user = case.docker_user or docker_user
        cmd = [
            "docker",
            "run",
            "--rm",
            "--cidfile",
            str(run_dir / "container.cid"),
            "--user",
            docker_user_arg(effective_docker_user),
        ]
        if taskset:
            cmd.extend(["--cpuset-cpus", taskset])
        if cgroup_cpu_weight:
            cmd.extend(["--cpu-shares", str(cgroup_weight_to_docker_cpu_shares(cgroup_cpu_weight))])
        cpu_max_parsed = parse_cgroup_cpu_max(cgroup_cpu_max)
        if cpu_max_parsed:
            quota, period = cpu_max_parsed
            if quota != "max":
                cmd.extend(["--cpu-quota", str(int(quota)), "--cpu-period", str(period)])
        cmd.extend(
            [
            "-v",
            f"{FLOWCAUSAL_ROOT.resolve()}:/flowcausal",
            "-v",
            f"{case.repo_dir}:/repo:ro",
            "-v",
            f"{run_dir}:/out",
            "-w",
            "/flowcausal",
            case.container_image,
            *container_cmd,
            ]
        )
    if taskset and not case.container_image:
        cmd = ["taskset", "-c", taskset, *cmd]
    if nice != 0 and not case.container_image:
        cmd = ["nice", "-n", str(nice), *cmd]
    if (cgroup_cpu_weight or cgroup_cpu_max.strip()) and not case.container_image:
        cgroup_cmd = [
            sys.executable,
            str(pathlib.Path(cgroup_wrapper).resolve()),
            "--name-prefix",
            cgroup_name_prefix,
        ]
        if cgroup_cpu_weight:
            cgroup_cmd.extend(["--cpu-weight", str(cgroup_cpu_weight)])
        if cgroup_cpu_max.strip():
            cgroup_cmd.extend(["--cpu-max", cgroup_cpu_max.strip()])
        cmd = [*sudo_prefix(dry_run=False, mode=cgroup_sudo), *cgroup_cmd, "--", *cmd]
    return cmd


def read_text_if_exists(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return ""


def collect_docker_identity(
    run_dir: pathlib.Path,
    proc: subprocess.Popen[Any],
    *,
    timeout_sec: float,
) -> dict[str, Any]:
    """Capture the host-visible identity of one Docker replay case.

    The scheduler sees host tasks, not just trace instance ids.  A cidfile plus
    ``docker inspect`` gives later tooling a concrete bridge from replay case
    -> Docker CLI process -> container id -> host init pid.  Short-lived
    containers may exit before inspect succeeds; in that case the cidfile and
    CLI pid are still useful evidence for debugging/plumbing.
    """

    cidfile = run_dir / "container.cid"
    identity: dict[str, Any] = {
        "docker_cli_pid": int(proc.pid),
        "container_cidfile": str(cidfile),
        "container_id": "",
        "container_host_pid": None,
        "status": "cidfile_pending",
    }
    deadline = time.monotonic() + max(0.0, timeout_sec)
    while time.monotonic() <= deadline:
        cid = read_text_if_exists(cidfile)
        if cid:
            identity["container_id"] = cid
            inspect = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Pid}}", cid],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if inspect.returncode == 0:
                text = inspect.stdout.strip()
                try:
                    host_pid = int(text)
                except ValueError:
                    host_pid = 0
                identity["container_host_pid"] = host_pid if host_pid > 0 else None
                identity["status"] = "pass" if host_pid > 0 else "container_not_running"
                return identity
            identity["status"] = "inspect_failed"
            identity["inspect_stderr"] = inspect.stderr.strip()
            if proc.poll() is not None:
                return identity
        if proc.poll() is not None and not cid:
            identity["status"] = "process_exited_before_cidfile"
            return identity
        time.sleep(0.05)
    if read_text_if_exists(cidfile):
        identity["container_id"] = read_text_if_exists(cidfile)
        identity["status"] = "inspect_timeout"
    return identity


def build_flowctl_cmd(args: argparse.Namespace, case: ReplayCase, run_dir: pathlib.Path, root_pid: int) -> list[str]:
    base_cmd = [
        sys.executable,
        "-u",
        args.flowctl,
        "--root-pid",
        str(root_pid),
        "--session",
        f"harness:{case.instance_id}",
        "--watch",
        "--stage-kind",
        args.flowctl_stage_kind,
        "--stage-state-file",
        str(run_dir / "stage-state.json"),
        "--frontier-width",
        str(args.flowctl_frontier_width),
        "--min-frontier-score",
        str(args.flowctl_min_frontier_score),
        "--frontier-stability-samples",
        str(args.flowctl_frontier_stability_samples),
        "--frontier-hold-ms",
        str(args.flowctl_frontier_hold_ms),
        "--frontier-bypass-score",
        str(args.flowctl_frontier_bypass_score),
        "--frontier-stability-action",
        args.flowctl_frontier_stability_action,
        "--frontier-action",
        args.flowctl_frontier_action,
        "--build-frontier-admission",
        args.flowctl_build_frontier_admission,
        "--confidence",
        str(args.flowctl_confidence),
        "--ttl-ms",
        str(args.flowctl_ttl_ms),
        "--policy-window-ms",
        str(args.flowctl_policy_window_ms),
        "--boost-budget-ms",
        str(args.flowctl_boost_budget_ms),
        "--max-vtime-boost-us",
        str(args.flowctl_max_vtime_boost_us),
        "--interval-ms",
        str(args.flowctl_interval_ms),
        "--pin-dir",
        args.flowctl_pin_dir,
        "--json-log",
        str(run_dir / "controller.ndjson"),
    ]
    if args.flowctl_width_control:
        base_cmd.extend(["--width-control", "--max-frontier-width", str(args.flowctl_max_frontier_width)])
    if args.flowctl_frontier_propagation:
        base_cmd.extend(["--frontier-propagation", "--frontier-propagation-depth", str(args.flowctl_frontier_propagation_depth)])
    if args.flowctl_stage_aware_frontier_propagation_depth:
        base_cmd.append("--stage-aware-frontier-propagation-depth")
    if args.flowctl_selected_stability:
        base_cmd.append("--frontier-selected-stability")
    if args.flowctl_policy_fallback:
        base_cmd.append("--policy-fallback")
    if getattr(args, "flowctl_no_background_hints", False):
        base_cmd.append("--no-background-hints")
    if args.flowctl_dry_run:
        base_cmd.append("--dry-run")

    launcher: list[str] = [] if args.flowctl_dry_run else sudo_prefix(dry_run=False, mode=args.flowctl_sudo)
    flowctl_taskset = getattr(args, "flowctl_taskset", "")
    flowctl_nice = int(getattr(args, "flowctl_nice", 0) or 0)
    if flowctl_taskset:
        launcher.extend(["taskset", "-c", flowctl_taskset])
    if flowctl_nice:
        launcher.extend(["nice", "-n", str(flowctl_nice)])
    cmd = launcher + base_cmd
    return cmd


def start_case_controller(
    args: argparse.Namespace,
    case: ReplayCase,
    run_dir: pathlib.Path,
    root_pid: int | None,
) -> tuple[subprocess.Popen[Any] | None, dict[str, Any]]:
    log_path = run_dir / "controller.log"
    json_path = run_dir / "controller.ndjson"
    if not args.per_case_flowctl:
        return None, {"status": "disabled"}
    if not root_pid or root_pid <= 0:
        return None, {
            "status": "skipped_no_root_pid",
            "controller_log": str(log_path),
            "controller_ndjson": str(json_path),
        }
    json_path.write_text("", encoding="utf-8")
    cmd = build_flowctl_cmd(args, case, run_dir, root_pid)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    return proc, {
        "status": "running",
        "root_pid": int(root_pid),
        "session": f"harness:{case.instance_id}",
        "cmd": cmd,
        "controller_log": str(log_path),
        "controller_ndjson": str(json_path),
        "started_monotonic": time.monotonic(),
    }


def build_flowd_cmd(
    args: argparse.Namespace,
    case: ReplayCase,
    run_dir: pathlib.Path,
    root_pid: int,
    *,
    anchor_cpu: int,
) -> list[str]:
    cmd = sudo_prefix(dry_run=False, mode=args.flowd_sudo) + [
        args.flowd,
        "--root-pid",
        str(root_pid),
        "--session",
        f"harness:{case.instance_id}",
        "--pin-dir",
        args.flowd_pin_dir,
        "--confidence",
        str(args.flowd_confidence),
        "--max-frontier-width",
        str(args.flowd_max_frontier_width),
        "--max-vtime-boost-us",
        str(args.flowd_max_vtime_boost_us),
        "--policy-window-ms",
        str(args.flowd_policy_window_ms),
        "--boost-budget-ms",
        str(args.flowd_boost_budget_ms),
        "--ttl-ms",
        str(args.flowd_ttl_ms),
    ]
    if anchor_cpu >= 0:
        cmd.extend(["--anchor-cpu", str(anchor_cpu)])
    if args.flowd_frontier:
        cmd.append("--frontier")
    if args.flowd_propagation_depth > 0:
        cmd.extend(["--propagation-depth", str(args.flowd_propagation_depth)])
    if args.flowd_width_control:
        cmd.append("--width-control")
    if getattr(args, "flowd_control_plane", False):
        cmd.append("--control-plane")
    if getattr(args, "flowd_assist_bpf_auto", False):
        cmd.append("--assist-bpf-auto")
    if getattr(args, "flowd_cxx_only_session_policy", False):
        cmd.append("--cxx-only-session-policy")
    if getattr(args, "flowd_affinity_by_session", False):
        cmd.append("--affinity-by-session")
    if getattr(args, "flowd_python_affinity_by_session", False):
        cmd.append("--python-affinity-by-session")
    if getattr(args, "flowd_critical_affinity_by_session", False):
        cmd.append("--critical-affinity-by-session")
    if getattr(args, "flowd_balanced_affinity_by_session", False):
        cmd.append("--balanced-affinity-by-session")
    if getattr(args, "flowd_affinity_release_after_first_fork", False):
        cmd.append("--affinity-release-after-first-fork")
    if getattr(args, "flowd_affinity_release_package_only", False):
        cmd.append("--affinity-release-package-only")
    if getattr(args, "flowd_stage_state", False):
        cmd.extend(["--stage-state-file", str(run_dir / "stage-state.json")])
        cmd.extend(["--stage-state-poll-ms", str(args.flowd_stage_state_poll_ms)])
    if args.flowd_events:
        cmd.append("--events")
    elif (
        not getattr(args, "flowd_control_plane", False)
        and not getattr(args, "flowd_assist_bpf_auto", False)
        and not getattr(args, "flowd_affinity_release_after_first_fork", False)
        and not getattr(args, "flowd_stage_state", False)
    ):
        cmd.append("--once")
    return cmd


def start_case_flowd(
    args: argparse.Namespace,
    case: ReplayCase,
    run_dir: pathlib.Path,
    root_pid: int | None,
    *,
    anchor_cpu: int,
) -> tuple[subprocess.Popen[Any] | None, dict[str, Any]]:
    log_path = run_dir / "flowd.log"
    if not args.per_case_flowd:
        return None, {"status": "disabled", "anchor_cpu": anchor_cpu}
    if not root_pid or root_pid <= 0:
        return None, {
            "status": "skipped_no_root_pid",
            "log": str(log_path),
            "anchor_cpu": anchor_cpu,
        }
    cmd = build_flowd_cmd(args, case, run_dir, int(root_pid), anchor_cpu=anchor_cpu)
    log = log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    log.close()
    return proc, {
        "status": "running",
        "root_pid": int(root_pid),
        "session": f"harness:{case.instance_id}",
        "cmd": cmd,
        "log": str(log_path),
        "anchor_cpu": anchor_cpu,
        "started_monotonic": time.monotonic(),
    }


def wait_for_flowd_ready(log_path: pathlib.Path, timeout_sec: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    needle = "flowd registered session="
    while time.monotonic() < deadline:
        if log_path.exists():
            try:
                text = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if needle in text:
                return True
        time.sleep(0.02)
    return False


def stop_process_group(proc: subprocess.Popen[Any] | None, timeout_sec: float = 5.0) -> int | None:
    if proc is None:
        return None
    if proc.poll() is not None:
        return int(proc.returncode)
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.send_signal(signal.SIGINT)
    try:
        return int(proc.wait(timeout=timeout_sec))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.terminate()
    try:
        return int(proc.wait(timeout=timeout_sec))
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return proc.poll()
    except OSError:
        proc.kill()
    return int(proc.wait())


def iter_json_records(path: pathlib.Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def parse_workload_log(path: pathlib.Path) -> dict[str, Any]:
    records = iter_json_records(path)
    stage_counts: dict[str, int] = {}
    stage_seconds: dict[str, float] = {}
    returncodes: dict[str, int] = {}
    unexpected_returncodes: dict[str, int] = {}
    command_count = 0
    critical_sec: float | None = None
    frontier_label_count = 0
    workload_returncode: int | None = None
    workload_begin_time_ns: int | None = None
    workload_end_time_ns: int | None = None
    cleanup_begin_time_ns: int | None = None
    cleanup_end_time_ns: int | None = None
    allowed_failure_commands = 0
    unexpected_failed_commands = 0
    for record in records:
        rtype = record.get("type")
        time_ns = record.get("time_ns")
        event_time_ns = int(time_ns) if time_ns is not None else None
        if rtype == "workload_begin":
            command_count = int(record.get("command_count", command_count) or 0)
            if workload_begin_time_ns is None:
                workload_begin_time_ns = event_time_ns
        elif rtype == "agent_stage_end":
            stage = str(record.get("stage") or "UNKNOWN")
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            stage_seconds[stage] = stage_seconds.get(stage, 0.0) + float(record.get("elapsed_sec") or 0.0)
            rc = str(int(record.get("returncode", 0) or 0))
            returncodes[rc] = returncodes.get(rc, 0) + 1
            allow_failure = bool(record.get("allow_failure", False))
            if rc != "0" and allow_failure:
                allowed_failure_commands += 1
            elif rc != "0":
                unexpected_failed_commands += 1
                unexpected_returncodes[rc] = unexpected_returncodes.get(rc, 0) + 1
        elif rtype == "flowcausal_metric" and record.get("name") == "critical_sec":
            critical_sec = float(record.get("value") or 0.0)
        elif rtype == "frontier_label":
            frontier_label_count += 1
        elif rtype == "workload_end":
            workload_end_time_ns = event_time_ns
            workload_returncode = int(record.get("returncode", 0) or 0)
        elif rtype == "cleanup_begin":
            if cleanup_begin_time_ns is None:
                cleanup_begin_time_ns = event_time_ns
        elif rtype == "cleanup_end":
            cleanup_end_time_ns = event_time_ns
    return {
        "json_records": len(records),
        "command_count": command_count,
        "stage_counts": stage_counts,
        "stage_seconds": stage_seconds,
        "stage_returncodes": returncodes,
        "stage_unexpected_returncodes": unexpected_returncodes,
        "allowed_failure_commands": allowed_failure_commands,
        "unexpected_failed_commands": unexpected_failed_commands,
        "critical_sec": critical_sec,
        "frontier_label_count": frontier_label_count,
        "workload_returncode": workload_returncode,
        "workload_begin_time_ns": workload_begin_time_ns,
        "workload_end_time_ns": workload_end_time_ns,
        "cleanup_begin_time_ns": cleanup_begin_time_ns,
        "cleanup_end_time_ns": cleanup_end_time_ns,
    }


def derive_case_timing(
    parsed: dict[str, Any],
    *,
    elapsed_sec: float,
    started_wall_ns: int,
) -> dict[str, float | None]:
    stage_sum_sec = sum(float(value) for value in parsed.get("stage_seconds", {}).values())
    critical_sec = parsed.get("critical_sec")
    workload_begin_time_ns = parsed.get("workload_begin_time_ns")
    workload_end_time_ns = parsed.get("workload_end_time_ns")
    cleanup_begin_time_ns = parsed.get("cleanup_begin_time_ns")
    cleanup_end_time_ns = parsed.get("cleanup_end_time_ns")

    setup_sec: float | None = None
    if workload_begin_time_ns is not None:
        setup_sec = max(0.0, (int(workload_begin_time_ns) - started_wall_ns) / 1_000_000_000.0)

    logged_workload_sec: float | None = None
    if workload_begin_time_ns is not None and workload_end_time_ns is not None:
        logged_workload_sec = max(
            0.0,
            (int(workload_end_time_ns) - int(workload_begin_time_ns)) / 1_000_000_000.0,
        )

    logged_cleanup_sec: float | None = None
    if cleanup_begin_time_ns is not None and cleanup_end_time_ns is not None:
        logged_cleanup_sec = max(
            0.0,
            (int(cleanup_end_time_ns) - int(cleanup_begin_time_ns)) / 1_000_000_000.0,
        )

    post_workload_sec: float | None = None
    if setup_sec is not None and logged_workload_sec is not None:
        post_workload_sec = max(0.0, elapsed_sec - setup_sec - logged_workload_sec)

    post_critical_sec: float | None = None
    if critical_sec is not None:
        post_critical_sec = max(0.0, elapsed_sec - float(critical_sec))

    end_to_end_critical_sec: float | None = None
    if setup_sec is not None and critical_sec is not None:
        end_to_end_critical_sec = setup_sec + float(critical_sec)

    return {
        "setup_sec": setup_sec,
        "logged_workload_sec": logged_workload_sec,
        "logged_cleanup_sec": logged_cleanup_sec,
        "post_workload_sec": post_workload_sec,
        "post_critical_sec": post_critical_sec,
        "end_to_end_critical_sec": end_to_end_critical_sec,
        "stage_sum_sec": stage_sum_sec,
        "nonstage_sec": max(0.0, elapsed_sec - stage_sum_sec),
    }


def write_summary_csv(path: pathlib.Path, cases: list[dict[str, Any]]) -> None:
    fieldnames = [
        "instance_id",
        "returncode",
        "command_count",
        "critical_sec",
        "elapsed_sec",
        "setup_sec",
        "logged_workload_sec",
        "logged_cleanup_sec",
        "post_workload_sec",
        "post_critical_sec",
        "end_to_end_critical_sec",
        "stage_sum_sec",
        "nonstage_sec",
        "stage_counts",
        "stage_returncodes",
        "allowed_failure_commands",
        "unexpected_failed_commands",
        "system_profile_enabled",
        "system_profile_samples",
        "system_profile_max_process_count",
        "system_profile_unique_pids_seen",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for case in cases:
            writer.writerow(
                {
                    "instance_id": case["instance_id"],
                    "returncode": case["returncode"],
                    "command_count": case["parsed"].get("command_count", 0),
                    "critical_sec": case["parsed"].get("critical_sec"),
                    "elapsed_sec": case.get("elapsed_sec"),
                    "setup_sec": case.get("timing_breakdown", {}).get("setup_sec"),
                    "logged_workload_sec": case.get("timing_breakdown", {}).get("logged_workload_sec"),
                    "logged_cleanup_sec": case.get("timing_breakdown", {}).get("logged_cleanup_sec"),
                    "post_workload_sec": case.get("timing_breakdown", {}).get("post_workload_sec"),
                    "post_critical_sec": case.get("timing_breakdown", {}).get("post_critical_sec"),
                    "end_to_end_critical_sec": case.get("timing_breakdown", {}).get("end_to_end_critical_sec"),
                    "stage_sum_sec": case.get("timing_breakdown", {}).get("stage_sum_sec"),
                    "nonstage_sec": case.get("timing_breakdown", {}).get("nonstage_sec"),
                    "stage_counts": json.dumps(case["parsed"].get("stage_counts", {}), sort_keys=True),
                    "stage_returncodes": json.dumps(case["parsed"].get("stage_returncodes", {}), sort_keys=True),
                    "allowed_failure_commands": case["parsed"].get("allowed_failure_commands", 0),
                    "unexpected_failed_commands": case["parsed"].get("unexpected_failed_commands", 0),
                    "system_profile_enabled": case.get("system_profile", {}).get("enabled", False),
                    "system_profile_samples": case.get("system_profile", {}).get("samples", 0),
                    "system_profile_max_process_count": case.get("system_profile", {}).get("max_process_count", 0),
                    "system_profile_unique_pids_seen": case.get("system_profile", {}).get("unique_pids_seen", 0),
                }
            )


def write_summary_md(path: pathlib.Path, payload: dict[str, Any]) -> None:
    aggregate = payload.get("aggregate", {})
    lines = [
        "# Harness replay mix summary",
        "",
        f"- status: `{payload['status']}`",
        f"- cases: {len(payload['cases'])}",
        f"- makespan_sec: {payload['makespan_sec']:.6f}",
        f"- mean_critical_sec: {aggregate.get('mean_critical_sec', '')}",
        f"- p95_critical_sec: {aggregate.get('p95_critical_sec', '')}",
        f"- mean_setup_sec: {aggregate.get('mean_setup_sec', '')}",
        f"- mean_logged_workload_sec: {aggregate.get('mean_logged_workload_sec', '')}",
        f"- mean_post_workload_sec: {aggregate.get('mean_post_workload_sec', '')}",
        f"- mean_post_critical_sec: {aggregate.get('mean_post_critical_sec', '')}",
        f"- mean_end_to_end_critical_sec: {aggregate.get('mean_end_to_end_critical_sec', '')}",
        f"- p95_end_to_end_critical_sec: {aggregate.get('p95_end_to_end_critical_sec', '')}",
        f"- elapsed_jain_fairness: {aggregate.get('elapsed_jain_fairness', '')}",
        f"- critical_jain_fairness: {aggregate.get('critical_jain_fairness', '')}",
        f"- failed_stage_commands: {aggregate.get('failed_stage_commands', '')}",
        f"- allowed_failure_commands: {aggregate.get('allowed_failure_commands', '')}",
        "",
        "| instance | rc | commands | critical_sec | e2e_critical_sec | elapsed_sec | setup_sec | workload_sec | post_workload_sec | post_critical_sec | sys samples | max proc | unique pids | stages |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for case in payload["cases"]:
        parsed = case["parsed"]
        critical = parsed.get("critical_sec")
        critical_text = "" if critical is None else f"{critical:.6f}"
        timing = case.get("timing_breakdown", {})
        setup = timing.get("setup_sec")
        workload = timing.get("logged_workload_sec")
        post_workload = timing.get("post_workload_sec")
        post_critical = timing.get("post_critical_sec")
        e2e_critical = timing.get("end_to_end_critical_sec")
        stages = ", ".join(f"{key} {value}" for key, value in sorted(parsed.get("stage_counts", {}).items()))
        profile = case.get("system_profile", {})
        lines.append(
            f"| `{case['instance_id']}` | {case['returncode']} | {parsed.get('command_count', 0)} | "
            f"{critical_text} | {'' if e2e_critical is None else f'{e2e_critical:.6f}'} | "
            f"{case['elapsed_sec']:.6f} | "
            f"{'' if setup is None else f'{setup:.6f}'} | "
            f"{'' if workload is None else f'{workload:.6f}'} | "
            f"{'' if post_workload is None else f'{post_workload:.6f}'} | "
            f"{'' if post_critical is None else f'{post_critical:.6f}'} | "
            f"{profile.get('samples', 0)} | {profile.get('max_process_count', 0)} | "
            f"{profile.get('unique_pids_seen', 0)} | {stages} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def percentile_nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, int((percentile / 100.0) * len(ordered) + 0.999999))
    return ordered[min(len(ordered) - 1, rank - 1)]


def jain_fairness(values: list[float]) -> float | None:
    if not values:
        return None
    square_sum = sum(value * value for value in values)
    if square_sum <= 0:
        return None
    total = sum(values)
    return (total * total) / (len(values) * square_sum)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a colocated mix of normalized FlowCausal harness replay traces",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--case",
        action="append",
        default=[],
        help="case spec INSTANCE=TRACE::REPO or INSTANCE=TRACE::REPO::IMAGE; may be repeated",
    )
    parser.add_argument("--manifest", help="JSON manifest with cases/instances")
    parser.add_argument("--out-dir", required=True, help="output directory")
    parser.add_argument("--overwrite", action="store_true", help="allow non-empty output directory")
    parser.add_argument("--stagger-ms", type=int, default=0, help="start offset added per case")
    parser.add_argument("--taskset", default="", help="optional cpuset applied to every replay process")
    parser.add_argument(
        "--nice",
        type=int,
        default=0,
        help=(
            "optional Linux nice value applied to every replay workload; Docker cases apply it "
            "inside the container after the image name"
        ),
    )
    parser.add_argument(
        "--cgroup-cpu-weight",
        type=int,
        default=0,
        help="optional cgroup-v2 cpu.weight baseline value; 0 leaves the kernel/Docker default",
    )
    parser.add_argument(
        "--cgroup-cpu-max",
        default="",
        help="optional cgroup-v2 cpu.max baseline value, e.g. '50000 100000' or 'max 100000'",
    )
    parser.add_argument(
        "--cgroup-wrapper",
        default=str(STATIC_CGROUP_WRAPPER),
        help="wrapper script used for host replay static cgroup baselines",
    )
    parser.add_argument(
        "--cgroup-sudo",
        choices=("auto", "always", "never"),
        default="auto",
        help="sudo mode for host replay static cgroup wrapper; Docker cases use Docker CPU flags instead",
    )
    parser.add_argument(
        "--cgroup-name-prefix",
        default="flowcausal-harness",
        help="temporary cgroup name prefix for host replay static cgroup baselines",
    )
    parser.add_argument(
        "--copy-keep-git",
        action="store_true",
        help="preserve .git metadata in every replay work dir for traces that execute git commands",
    )
    parser.add_argument(
        "--copy-exclude",
        action="append",
        default=[],
        help="extra copy ignore pattern forwarded to agent_trace_workflow.py; repeatable",
    )
    parser.add_argument(
        "--allow-failure-stage",
        action="append",
        default=["RETRY_PROBE"],
        help="stage whose nonzero commands should not fail a workload",
    )
    parser.add_argument("--env", action="append", default=[], help="extra KEY=VALUE forwarded to every replay")
    parser.add_argument(
        "--host-env",
        action="append",
        default=[],
        help="extra KEY=VALUE forwarded only to host replay cases",
    )
    parser.add_argument(
        "--container-env",
        action="append",
        default=[],
        help="extra KEY=VALUE forwarded only to Docker replay cases",
    )
    parser.add_argument(
        "--container-python",
        default="/opt/miniconda3/envs/testbed/bin/python",
        help="Python interpreter used inside Docker replay cases",
    )
    parser.add_argument(
        "--container-path-env",
        default="/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        help="PATH forwarded to commands inside Docker replay cases",
    )
    parser.add_argument(
        "--docker-user",
        default="current",
        help="Docker --user value for container cases; use current, root, or UID:GID",
    )
    parser.add_argument(
        "--docker-identity-timeout-sec",
        type=float,
        default=2.0,
        help="seconds to wait for Docker cidfile/host pid capture after starting each container case",
    )
    parser.add_argument(
        "--per-case-flowctl",
        action="store_true",
        help="start one flowctl watcher per replay case, rooted at host process pid or Docker container host pid",
    )
    parser.add_argument("--flowctl", default=str(FLOWCAUSAL_ROOT / "src" / "controller" / "flowctl.py"))
    parser.add_argument("--flowctl-pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--flowctl-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--flowctl-dry-run", action="store_true")
    parser.add_argument("--flowctl-stage-kind", default="AUTO")
    parser.add_argument("--flowctl-frontier-width", type=int, default=1)
    parser.add_argument("--flowctl-max-frontier-width", type=int, default=1)
    parser.add_argument("--flowctl-min-frontier-score", type=int, default=50)
    parser.add_argument("--flowctl-confidence", type=int, default=75)
    parser.add_argument("--flowctl-ttl-ms", type=int, default=1000)
    parser.add_argument("--flowctl-policy-window-ms", type=int, default=100)
    parser.add_argument("--flowctl-boost-budget-ms", type=int, default=10)
    parser.add_argument("--flowctl-max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--flowctl-interval-ms", type=int, default=100)
    parser.add_argument("--flowctl-width-control", action="store_true")
    parser.add_argument("--flowctl-frontier-propagation", action="store_true")
    parser.add_argument("--flowctl-frontier-propagation-depth", type=int, default=2)
    parser.add_argument("--flowctl-stage-aware-frontier-propagation-depth", action="store_true")
    parser.add_argument("--flowctl-frontier-stability-samples", type=int, default=1)
    parser.add_argument("--flowctl-frontier-hold-ms", type=int, default=0)
    parser.add_argument("--flowctl-frontier-bypass-score", type=int, default=140)
    parser.add_argument("--flowctl-selected-stability", action="store_true")
    parser.add_argument(
        "--flowctl-frontier-stability-action",
        choices=("observe", "suppress"),
        default="suppress",
    )
    parser.add_argument(
        "--flowctl-frontier-action",
        choices=("advance", "auto", "observe", "stage-aware"),
        default="advance",
    )
    parser.add_argument(
        "--flowctl-build-frontier-admission",
        choices=("advance", "observe", "tail-only"),
        default="advance",
    )
    parser.add_argument("--flowctl-policy-fallback", action="store_true")
    parser.add_argument("--flowctl-no-background-hints", action="store_true")
    parser.add_argument(
        "--flowctl-taskset",
        default="",
        help="optional CPU list for per-case flowctl controllers, e.g. '2' to keep Python off workload CPUs",
    )
    parser.add_argument(
        "--flowctl-nice",
        type=int,
        default=0,
        help="optional nice value for per-case flowctl controllers",
    )
    parser.add_argument(
        "--per-case-flowd",
        action="store_true",
        help="start one no-Python C/libbpf flowd registrar per replay case",
    )
    parser.add_argument(
        "--delay-workload-until-flowd",
        action="store_true",
        help="wait for flowd registration before the workload prepares its work tree",
    )
    parser.add_argument("--flowd", default="/tmp/flowd")
    parser.add_argument("--flowd-pin-dir", default="/sys/fs/bpf/flowcausal")
    parser.add_argument("--flowd-sudo", choices=("auto", "always", "never"), default="auto")
    parser.add_argument("--flowd-events", action="store_true")
    parser.add_argument(
        "--flowd-control-plane",
        action="store_true",
        help="consume ringbuf events and update task/policy maps in C",
    )
    parser.add_argument(
        "--flowd-assist-bpf-auto",
        action="store_true",
        help="BPF-main/C-assist mode: let flowd consume BPF events and add conservative cmdline hints",
    )
    parser.add_argument(
        "--flowd-cxx-only-session-policy",
        action="store_true",
        help="let C flowd apply FlowCausal frontier/locality hints only to the C++ build-heavy harness session",
    )
    parser.add_argument(
        "--flowd-affinity-by-session",
        action="store_true",
        help="set each harness root affinity once in flowd according to its session name",
    )
    parser.add_argument(
        "--flowd-python-affinity-by-session",
        action="store_true",
        help="set root affinity only for the Python TEST-straggler harness session",
    )
    parser.add_argument(
        "--flowd-critical-affinity-by-session",
        action="store_true",
        help="set flowd affinity only for known critical-stage sessions; package remains elastic",
    )
    parser.add_argument(
        "--flowd-balanced-affinity-by-session",
        action="store_true",
        help="set coordinated C++/Python/package affinity masks; package avoids CPU3-4",
    )
    parser.add_argument(
        "--flowd-affinity-release-after-first-fork",
        action="store_true",
        help="restore the root task's original CPU mask after the first direct child fork",
    )
    parser.add_argument(
        "--flowd-affinity-release-package-only",
        action="store_true",
        help="with flowd affinity release, release only package-heavy sessions and keep critical sessions pinned",
    )
    parser.add_argument(
        "--flowd-stage-state",
        action="store_true",
        help="let C flowd poll this case's stage-state.json and mark final cleanup without Python control plane",
    )
    parser.add_argument(
        "--flowd-stage-state-poll-ms",
        type=int,
        default=50,
        help="poll interval for C flowd stage-state cleanup detection",
    )
    parser.add_argument("--flowd-confidence", type=int, default=75)
    parser.add_argument("--flowd-max-frontier-width", type=int, default=1)
    parser.add_argument("--flowd-max-vtime-boost-us", type=int, default=500)
    parser.add_argument("--flowd-policy-window-ms", type=int, default=100)
    parser.add_argument("--flowd-boost-budget-ms", type=int, default=10)
    parser.add_argument("--flowd-ttl-ms", type=int, default=1000)
    parser.add_argument("--flowd-anchor-cpu", type=int, default=-1)
    parser.add_argument(
        "--flowd-anchor-policy",
        choices=("none", "fixed", "round-robin"),
        default="fixed",
        help=(
            "per-case flowd locality anchor assignment; fixed preserves the legacy "
            "--flowd-anchor-cpu behavior, round-robin assigns cases over --flowd-anchor-cpus "
            "or --taskset"
        ),
    )
    parser.add_argument(
        "--flowd-anchor-cpus",
        default="",
        help="taskset-style CPU list used by --flowd-anchor-policy round-robin, e.g. 0-7",
    )
    parser.add_argument("--flowd-frontier", action="store_true")
    parser.add_argument("--flowd-propagation-depth", type=int, default=0)
    parser.add_argument("--flowd-width-control", action="store_true")
    parser.add_argument(
        "--system-profile",
        action="store_true",
        help="sample each replay case's host-visible process tree and schedstat while it runs",
    )
    parser.add_argument(
        "--system-profile-interval-ms",
        type=int,
        default=25,
        help="sampling interval for --system-profile",
    )
    cleanup_group = parser.add_mutually_exclusive_group()
    cleanup_group.add_argument(
        "--cleanup-success-artifacts",
        dest="cleanup_success_artifacts",
        action="store_true",
        default=True,
        help=(
            "delete reproducible heavy per-case artifacts after successful cases; "
            "failed cases keep full work dirs"
        ),
    )
    cleanup_group.add_argument(
        "--keep-success-artifacts",
        dest="cleanup_success_artifacts",
        action="store_false",
        help="keep successful case work/stage-output artifacts for debugging",
    )
    parser.add_argument(
        "--cleanup-artifact",
        action="append",
        default=list(DEFAULT_SUCCESS_ARTIFACTS),
        help="direct child directory/file removed from successful case run dirs; repeatable",
    )
    sudo_cleanup = parser.add_mutually_exclusive_group()
    sudo_cleanup.add_argument(
        "--cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_true",
        default=True,
        help="retry successful-artifact cleanup with sudo -n if Docker/root-owned files block normal removal",
    )
    sudo_cleanup.add_argument(
        "--no-cleanup-use-sudo",
        dest="cleanup_use_sudo",
        action="store_false",
        help="do not use sudo while compacting successful case artifacts",
    )
    parser.add_argument("--summary-json", help="summary JSON path")
    parser.add_argument("--summary-csv", help="summary CSV path")
    parser.add_argument("--summary-md", help="summary Markdown path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    parse_env(args.env)
    args.host_env = expand_env_values(args.host_env, flag="--host-env")
    parse_env(args.host_env)
    parse_env(args.container_env)
    validate_nice_value(args.nice)
    validate_cgroup_cpu_weight(args.cgroup_cpu_weight)
    parse_cgroup_cpu_max(args.cgroup_cpu_max)
    out_dir = pathlib.Path(args.out_dir).resolve()
    if out_dir.exists() and any(out_dir.iterdir()) and not args.overwrite:
        raise SystemExit(f"{out_dir} exists and is non-empty; pass --overwrite")
    out_dir.mkdir(parents=True, exist_ok=True)

    cases: list[ReplayCase] = []
    if args.manifest:
        cases.extend(load_manifest(pathlib.Path(args.manifest).resolve(), stagger_ms=args.stagger_ms))
    for index, spec in enumerate(args.case, start=len(cases)):
        cases.append(parse_case_spec(spec, index=index, stagger_ms=args.stagger_ms))
    validate_cases(cases)

    start = time.monotonic()
    running: list[RunningCase] = []
    commands_manifest: list[dict[str, Any]] = []
    for case_index, case in enumerate(cases):
        target_offset = case.start_offset_ms / 1000.0
        while time.monotonic() - start < target_offset:
            time.sleep(min(0.05, target_offset - (time.monotonic() - start)))
        run_dir = out_dir / "runs" / safe_name(case.instance_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        cleanup_case_artifacts(run_dir)
        stdout_path = run_dir / "workload.log"
        stderr_path = run_dir / "workload.stderr.log"
        flowd_anchor_cpu = resolve_flowd_anchor_cpu(args, case_index)
        cmd = build_workload_cmd(
            case,
            run_dir,
            taskset=args.taskset,
            nice=args.nice,
            allow_failure_stages=args.allow_failure_stage,
            env_entries=args.env,
            host_env_entries=args.host_env,
            container_env_entries=args.container_env,
            copy_keep_git=args.copy_keep_git,
            copy_exclude=args.copy_exclude,
            container_python=args.container_python,
            container_path_env=args.container_path_env,
            docker_user=args.docker_user,
            cgroup_cpu_weight=args.cgroup_cpu_weight,
            cgroup_cpu_max=args.cgroup_cpu_max,
            cgroup_wrapper=args.cgroup_wrapper,
            cgroup_sudo=args.cgroup_sudo,
            cgroup_name_prefix=args.cgroup_name_prefix,
            start_gate_file=str(run_dir / ".flowcausal-start-gate") if args.delay_workload_until_flowd else None,
        )
        cgroup_provenance = cgroup_settings_provenance(
            case,
            cpu_weight=args.cgroup_cpu_weight,
            cpu_max=args.cgroup_cpu_max,
            cgroup_wrapper=args.cgroup_wrapper,
            cgroup_sudo=args.cgroup_sudo,
            cgroup_name_prefix=args.cgroup_name_prefix,
        )
        commands_manifest.append(
            {
                "instance_id": case.instance_id,
                "trace_file": str(case.trace_file),
                "repo_dir": str(case.repo_dir),
                "run_dir": str(run_dir),
                "start_offset_ms": case.start_offset_ms,
                "container_image": case.container_image,
                "work_dir_aliases": list(case.work_dir_aliases),
                "case_container_env": list(case.container_env_entries),
                "case_docker_user": case.docker_user,
                "effective_docker_user": case.docker_user or args.docker_user,
                "nice": args.nice,
                "cgroup": cgroup_provenance,
                "copy_exclude": args.copy_exclude,
                "env": args.env,
                "host_env": args.host_env if not case.container_image else [],
                "container_env": args.container_env if case.container_image else [],
                "effective_container_env": (
                    effective_container_env_entries(
                        container_path_env=args.container_path_env,
                        env_entries=args.env,
                        case_container_env_entries=case.container_env_entries,
                        container_env_entries=args.container_env,
                    )
                    if case.container_image
                    else []
                ),
                "docker_cidfile": str(run_dir / "container.cid") if case.container_image else "",
                "command_count": load_trace_command_count(case.trace_file),
                "flowd_anchor_cpu": flowd_anchor_cpu,
                "cmd": cmd,
            }
        )
        stdout_handle = stdout_path.open("w", encoding="utf-8")
        stderr_handle = stderr_path.open("w", encoding="utf-8")
        started_wall_ns = time.time_ns()
        started_monotonic = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=stdout_handle, stderr=stderr_handle, text=True)
        stdout_handle.close()
        stderr_handle.close()
        docker_identity = (
            collect_docker_identity(
                run_dir,
                proc,
                timeout_sec=args.docker_identity_timeout_sec,
            )
            if case.container_image
            else {}
        )
        controller_root_pid = (
            int(docker_identity.get("container_host_pid") or 0)
            if case.container_image
            else int(proc.pid)
        )
        system_profile: SystemProfileRecorder | None = None
        if args.system_profile and controller_root_pid > 0:
            system_profile = SystemProfileRecorder(
                root_pid=controller_root_pid,
                out_dir=run_dir,
                stage_state_file=run_dir / "stage-state.json",
                interval_ms=args.system_profile_interval_ms,
                provenance={
                    "instance_id": case.instance_id,
                    "trace_file": str(case.trace_file),
                    "repo_dir": str(case.repo_dir),
                    "container_image": case.container_image,
                    "runner": "flowcausal/experiments/run_harness_replay_mix.py",
                },
            )
            system_profile.start()
        controller_proc, controller_info = start_case_controller(
            args,
            case,
            run_dir,
            controller_root_pid,
        )
        flowd_proc, flowd_info = start_case_flowd(
            args,
            case,
            run_dir,
            controller_root_pid,
            anchor_cpu=flowd_anchor_cpu,
        )
        if args.delay_workload_until_flowd:
            gate_path = run_dir / ".flowcausal-start-gate"
            if flowd_info.get("log") and wait_for_flowd_ready(pathlib.Path(flowd_info["log"])):
                gate_path.touch(exist_ok=True)
            else:
                gate_path.touch(exist_ok=True)
        running.append(
            RunningCase(
                case=case,
                run_dir=run_dir,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                process=proc,
                started_monotonic=started_monotonic,
                started_wall_ns=started_wall_ns,
                started_offset_sec=started_monotonic - start,
                docker_identity=docker_identity,
                controller_process=controller_proc,
                controller_cmd=controller_info.get("cmd"),
                controller_log=pathlib.Path(controller_info["controller_log"]) if controller_info.get("controller_log") else None,
                controller_json_log=pathlib.Path(controller_info["controller_ndjson"]) if controller_info.get("controller_ndjson") else None,
                controller_root_pid=controller_info.get("root_pid"),
                controller_started_monotonic=controller_info.get("started_monotonic"),
                controller_status=str(controller_info.get("status", "disabled")),
                flowd_process=flowd_proc,
                flowd_cmd=flowd_info.get("cmd"),
                flowd_log=pathlib.Path(flowd_info["log"]) if flowd_info.get("log") else None,
                flowd_root_pid=flowd_info.get("root_pid"),
                flowd_anchor_cpu=int(flowd_info.get("anchor_cpu", -1)),
                flowd_started_monotonic=flowd_info.get("started_monotonic"),
                flowd_status=str(flowd_info.get("status", "disabled")),
                system_profile=system_profile,
            )
        )

    completed: dict[int, dict[str, Any]] = {}
    pending = {item.process.pid: item for item in running}
    while pending:
        for pid, item in list(pending.items()):
            returncode = item.process.poll()
            if returncode is None:
                continue
            completed[pid] = {
                "returncode": int(returncode),
                "finished_monotonic": time.monotonic(),
            }
            del pending[pid]
        if pending:
            time.sleep(0.005)

    workloads_finished_monotonic = (
        max(float(done["finished_monotonic"]) for done in completed.values())
        if completed
        else time.monotonic()
    )
    makespan = workloads_finished_monotonic - start

    results: list[dict[str, Any]] = []
    status = "pass"
    for item in running:
        done = completed.get(item.process.pid)
        if done is None:
            returncode = item.process.wait()
            finished_monotonic = time.monotonic()
        else:
            returncode = int(done["returncode"])
            finished_monotonic = float(done["finished_monotonic"])
            item.process.wait()
        elapsed = finished_monotonic - item.started_monotonic
        system_profile_summary = item.system_profile.stop() if item.system_profile is not None else disabled_profile()
        controller_returncode = stop_process_group(item.controller_process)
        controller_final_status = item.controller_status
        if item.controller_process is not None:
            controller_final_status = (
                "exited_after_root"
                if controller_returncode == 0
                else "stopped_after_workload"
            )
        flowd_returncode = stop_process_group(item.flowd_process)
        flowd_final_status = item.flowd_status
        if item.flowd_process is not None:
            if flowd_returncode == 0:
                flowd_final_status = "exited_after_register"
            else:
                flowd_final_status = "stopped_after_workload"
        parsed = parse_workload_log(item.stdout_path)
        timing_breakdown = derive_case_timing(
            parsed,
            elapsed_sec=elapsed,
            started_wall_ns=item.started_wall_ns,
        )
        if returncode != 0:
            status = "fail"
        cleanup_record = compact_case_artifacts(
            item.run_dir,
            enabled=bool(args.cleanup_success_artifacts),
            success=returncode == 0,
            artifact_names=args.cleanup_artifact,
            use_sudo=bool(args.cleanup_use_sudo),
        )
        results.append(
            {
                "instance_id": item.case.instance_id,
                "returncode": returncode,
                "elapsed_sec": elapsed,
                "finished_monotonic": finished_monotonic,
                "started_offset_sec": item.started_offset_sec,
                "started_wall_ns": item.started_wall_ns,
                "run_dir": str(item.run_dir),
                "stdout": str(item.stdout_path),
                "stderr": str(item.stderr_path),
                "trace_file": str(item.case.trace_file),
                "repo_dir": str(item.case.repo_dir),
                "container_image": item.case.container_image,
                "work_dir_aliases": list(item.case.work_dir_aliases),
                "process_pid": int(item.process.pid),
                "docker_identity": item.docker_identity,
                "system_profile": system_profile_summary,
                "cleanup": cleanup_record,
                "controller": {
                    "status": controller_final_status,
                    "start_status": item.controller_status,
                    "root_pid": item.controller_root_pid,
                    "returncode": controller_returncode,
                    "cmd": item.controller_cmd or [],
                    "log": str(item.controller_log) if item.controller_log else "",
                    "json_log": str(item.controller_json_log) if item.controller_json_log else "",
                    "started_delay_sec": (
                        item.controller_started_monotonic - item.started_monotonic
                        if item.controller_started_monotonic is not None
                        else None
                    ),
                },
                "flowd": {
                    "status": flowd_final_status,
                    "start_status": item.flowd_status,
                    "root_pid": item.flowd_root_pid,
                    "anchor_cpu": item.flowd_anchor_cpu,
                    "returncode": flowd_returncode,
                    "cmd": item.flowd_cmd or [],
                    "log": str(item.flowd_log) if item.flowd_log else "",
                    "started_delay_sec": (
                        item.flowd_started_monotonic - item.started_monotonic
                        if item.flowd_started_monotonic is not None
                        else None
                    ),
                },
                "parsed": parsed,
                "timing_breakdown": timing_breakdown,
            }
        )

    critical_values = [
        float(case["parsed"]["critical_sec"])
        for case in results
        if case["parsed"].get("critical_sec") is not None
    ]
    elapsed_values = [float(case["elapsed_sec"]) for case in results]
    setup_values = [
        float(case["timing_breakdown"]["setup_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("setup_sec") is not None
    ]
    logged_workload_values = [
        float(case["timing_breakdown"]["logged_workload_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("logged_workload_sec") is not None
    ]
    logged_cleanup_values = [
        float(case["timing_breakdown"]["logged_cleanup_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("logged_cleanup_sec") is not None
    ]
    post_workload_values = [
        float(case["timing_breakdown"]["post_workload_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("post_workload_sec") is not None
    ]
    post_critical_values = [
        float(case["timing_breakdown"]["post_critical_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("post_critical_sec") is not None
    ]
    end_to_end_critical_values = [
        float(case["timing_breakdown"]["end_to_end_critical_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("end_to_end_critical_sec") is not None
    ]
    stage_sum_values = [
        float(case["timing_breakdown"]["stage_sum_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("stage_sum_sec") is not None
    ]
    nonstage_values = [
        float(case["timing_breakdown"]["nonstage_sec"])
        for case in results
        if case.get("timing_breakdown", {}).get("nonstage_sec") is not None
    ]
    aggregate = {
        "mean_critical_sec": mean(critical_values),
        "p95_critical_sec": percentile_nearest_rank(critical_values, 95.0),
        "min_critical_sec": min(critical_values) if critical_values else None,
        "max_critical_sec": max(critical_values) if critical_values else None,
        "mean_setup_sec": mean(setup_values),
        "p95_setup_sec": percentile_nearest_rank(setup_values, 95.0),
        "mean_logged_workload_sec": mean(logged_workload_values),
        "mean_logged_cleanup_sec": mean(logged_cleanup_values),
        "mean_post_workload_sec": mean(post_workload_values),
        "mean_post_critical_sec": mean(post_critical_values),
        "mean_end_to_end_critical_sec": mean(end_to_end_critical_values),
        "p95_end_to_end_critical_sec": percentile_nearest_rank(end_to_end_critical_values, 95.0),
        "min_end_to_end_critical_sec": min(end_to_end_critical_values) if end_to_end_critical_values else None,
        "max_end_to_end_critical_sec": max(end_to_end_critical_values) if end_to_end_critical_values else None,
        "mean_stage_sum_sec": mean(stage_sum_values),
        "mean_nonstage_sec": mean(nonstage_values),
        "elapsed_jain_fairness": jain_fairness(elapsed_values),
        "critical_jain_fairness": jain_fairness(critical_values),
        "failed_stage_commands": sum(
            int(case["parsed"].get("unexpected_failed_commands") or 0)
            for case in results
        ),
        "allowed_failure_commands": sum(
            int(case["parsed"].get("allowed_failure_commands") or 0)
            for case in results
        ),
    }
    payload = {
        "schema": SCHEMA,
        "status": status,
        "out_dir": str(out_dir),
        "taskset": args.taskset,
        "nice": args.nice,
        "cgroup": {
            "enabled": bool(args.cgroup_cpu_weight or args.cgroup_cpu_max.strip()),
            "cpu_weight": args.cgroup_cpu_weight,
            "cpu_max": args.cgroup_cpu_max.strip(),
            "wrapper": str(pathlib.Path(args.cgroup_wrapper).resolve()),
            "sudo": args.cgroup_sudo,
            "name_prefix": args.cgroup_name_prefix,
        },
        "copy_keep_git": bool(args.copy_keep_git),
        "copy_exclude": args.copy_exclude,
        "env": args.env,
        "host_env": args.host_env,
        "container_env": args.container_env,
        "container_default_env": list(DOCKER_REPLAY_DEFAULT_ENV),
        "container_python": args.container_python,
        "container_path_env": args.container_path_env,
        "docker_user": args.docker_user,
        "docker_identity_timeout_sec": args.docker_identity_timeout_sec,
        "system_profile": bool(args.system_profile),
        "system_profile_interval_ms": args.system_profile_interval_ms,
        "cleanup_success_artifacts": bool(args.cleanup_success_artifacts),
        "cleanup_use_sudo": bool(args.cleanup_use_sudo),
        "cleanup_artifacts": args.cleanup_artifact,
        "per_case_flowctl": bool(args.per_case_flowctl),
        "flowctl_dry_run": bool(args.flowctl_dry_run),
        "flowctl_pin_dir": args.flowctl_pin_dir,
        "per_case_flowd": bool(args.per_case_flowd),
        "flowd": args.flowd,
        "flowd_pin_dir": args.flowd_pin_dir,
        "flowd_events": bool(args.flowd_events),
        "flowd_control_plane": bool(args.flowd_control_plane),
        "flowd_assist_bpf_auto": bool(args.flowd_assist_bpf_auto),
        "flowd_cxx_only_session_policy": bool(args.flowd_cxx_only_session_policy),
        "flowd_affinity_by_session": bool(args.flowd_affinity_by_session),
        "flowd_python_affinity_by_session": bool(args.flowd_python_affinity_by_session),
        "flowd_critical_affinity_by_session": bool(args.flowd_critical_affinity_by_session),
        "flowd_balanced_affinity_by_session": bool(args.flowd_balanced_affinity_by_session),
        "flowd_affinity_release_after_first_fork": bool(args.flowd_affinity_release_after_first_fork),
        "flowd_affinity_release_package_only": bool(args.flowd_affinity_release_package_only),
        "flowd_stage_state": bool(args.flowd_stage_state),
        "flowd_stage_state_poll_ms": int(args.flowd_stage_state_poll_ms),
        "flowd_anchor_policy": args.flowd_anchor_policy,
        "flowd_anchor_cpus": args.flowd_anchor_cpus,
        "flowd_anchor_cpu": args.flowd_anchor_cpu,
        "flowd_frontier": bool(args.flowd_frontier),
        "flowd_propagation_depth": args.flowd_propagation_depth,
        "flowd_width_control": bool(args.flowd_width_control),
        "stagger_ms": args.stagger_ms,
        "allow_failure_stage": args.allow_failure_stage,
        "makespan_sec": makespan,
        "aggregate": aggregate,
        "cases": results,
        "commands": commands_manifest,
    }
    summary_json = pathlib.Path(args.summary_json).resolve() if args.summary_json else out_dir / "harness-replay-mix-summary.json"
    summary_csv = pathlib.Path(args.summary_csv).resolve() if args.summary_csv else out_dir / "harness-replay-mix-summary.csv"
    summary_md = pathlib.Path(args.summary_md).resolve() if args.summary_md else out_dir / "harness-replay-mix-summary.md"
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    write_summary_csv(summary_csv, results)
    write_summary_md(summary_md, payload)
    print(json.dumps({"status": status, "cases": len(results), "summary_json": str(summary_json)}, sort_keys=True))
    return 0 if status == "pass" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

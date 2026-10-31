#!/usr/bin/env python3
"""Execute a benchmark under the scheduler chosen by LockAgent."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from runtime_guard import postflight, preflight


REPO_ROOT = Path(__file__).resolve().parents[2]
SCX_BIN_DIR = Path(os.environ.get("KERNADAPT_SCX_BIN_DIR", REPO_ROOT / "scx" / "build" / "bin"))
def privileged_cmd(*args: str) -> list[str]:
    if os.geteuid() == 0:
        return list(args)
    return ["sudo", "-n", *args]


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def sched_ext_state() -> str:
    state = Path("/sys/kernel/sched_ext/state")
    if not state.exists():
        return "unknown"
    try:
        return state.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def wait_for_state(enabled: bool, timeout_s: float = 5.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = sched_ext_state()
        if enabled and state != "disabled":
            return
        if not enabled and state == "disabled":
            return
        time.sleep(0.1)
    target = "enabled" if enabled else "disabled"
    raise TimeoutError(f"sched_ext did not become {target}; current state={sched_ext_state()}")


def start_scx(scheduler: str, outdir: Path, dry_run: bool) -> subprocess.Popen[str] | None:
    if scheduler == "default":
        return None
    binary = SCX_BIN_DIR / scheduler
    if not binary.exists() or not os.access(binary, os.X_OK):
        raise FileNotFoundError(f"scx binary not executable: {binary}")

    log_path = outdir / "scx.log"
    cmd = privileged_cmd(str(binary))
    if dry_run:
        print("[dry-run] start scx:", shlex.join(cmd), ">", log_path)
        return None

    log_f = log_path.open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
    finally:
        log_f.close()
    try:
        time.sleep(0.2)
        if proc.poll() is not None:
            raise RuntimeError(f"{scheduler} exited early with code {proc.returncode}; see {log_path}")
        wait_for_state(enabled=True)
        if proc.poll() is not None:
            raise RuntimeError(f"{scheduler} exited early with code {proc.returncode}; see {log_path}")
        return proc
    except Exception as startup_exc:
        terminate_scx_process(proc)
        try:
            wait_for_state(enabled=False, timeout_s=3.0)
        except Exception as cleanup_exc:
            raise RuntimeError(f"{startup_exc}; cleanup failed: {cleanup_exc}") from startup_exc
        raise


def terminate_scx_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=3)


def stop_scx(proc: subprocess.Popen[str] | None, dry_run: bool) -> None:
    if proc is None:
        return
    if dry_run:
        print("[dry-run] stop scx")
        return
    terminate_scx_process(proc)
    wait_for_state(enabled=False)


def run_benchmark(command: str, outdir: Path, timeout_s: int, dry_run: bool) -> int:
    log_path = outdir / "benchmark.log"
    if dry_run:
        print("[dry-run] benchmark:", command)
        return 0
    with log_path.open("w", encoding="utf-8") as log_f:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=REPO_ROOT,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
    return proc.returncode


def classify_executor_exception(exc: Exception) -> str:
    if isinstance(exc, subprocess.TimeoutExpired):
        return "benchmark_timeout"
    if isinstance(exc, (FileNotFoundError, RuntimeError, TimeoutError)):
        return "scheduler_startup_failure"
    return "executor_failure"


def write_manifest(outdir: Path, manifest: dict[str, Any]) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--decision", type=Path, required=True)
    ap.add_argument("--benchmark-cmd", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--timeout-s", type=int, default=600)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ledger", type=Path, help="State ledger JSON required by RuntimeGuard")
    ap.add_argument("--profile", type=Path, help="Workload profile JSON used by RuntimeGuard policy rules")
    ap.add_argument(
        "--runtime-policy-preflight",
        type=Path,
        help="Optional runner-level runtime_policy_preflight.json to consume before re-running guard policy.",
    )
    ap.add_argument("--intent", default="online", choices=["online", "reproduction", "comparison"])
    args = ap.parse_args(argv)

    decision = read_json(args.decision)
    scheduler = decision.get("scheduler")

    args.out.mkdir(parents=True, exist_ok=True)
    manifest_path = args.out / "manifest.json"
    write_manifest(
        args.out,
        {
            "decision": decision,
            "scheduler": scheduler,
            "benchmark_cmd": args.benchmark_cmd,
            "timeout_s": args.timeout_s,
            "dry_run": args.dry_run,
            "intent": args.intent,
            "state_ledger": str(args.ledger) if args.ledger else None,
            "runtime_policy_preflight": str(args.runtime_policy_preflight) if args.runtime_policy_preflight else None,
            "policy_enforcement": {
                "model": "PolicyEngine",
                "mode": (
                    "consume_and_reexecute_policy_engine"
                    if args.runtime_policy_preflight
                    else "reexecute_policy_engine"
                ),
            },
            "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "initial_sched_ext_state": sched_ext_state(),
        },
    )

    guard_preflight: dict[str, Any] | None = None
    guard_preflight = preflight(
        decision_path=args.decision,
        ledger_path=args.ledger or (args.out / "missing_state_ledger.json"),
        profile_path=args.profile,
        manifest_path=manifest_path,
        dry_run=args.dry_run,
        intent=args.intent,
        upstream_policy_preflight_path=args.runtime_policy_preflight,
    )
    write_json(args.out / "guard_preflight.json", guard_preflight)
    if guard_preflight["verdict"] == "block":
        status_path = args.out / "status.json"
        write_json(
            status_path,
            {
                "status": "guard_blocked",
                "returncode": 2,
                "policy_enforcement": {
                    "model": "PolicyEngine",
                    "mode": (
                        "consume_and_reexecute_policy_engine"
                        if args.runtime_policy_preflight
                        else "reexecute_policy_engine"
                    ),
                    "guard_preflight": str(args.out / "guard_preflight.json"),
                    "runtime_policy_preflight": str(args.runtime_policy_preflight) if args.runtime_policy_preflight else None,
                },
                "final_sched_ext_state": sched_ext_state(),
                "end_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )
        guard_postflight = postflight(status_path, None)
        write_json(args.out / "guard_postflight.json", guard_postflight)
        return 2

    proc: subprocess.Popen[str] | None = None
    status = "unknown"
    returncode = 1
    executor_failure_class: str | None = None
    executor_error: str | None = None
    try:
        proc = start_scx(str(scheduler), args.out, args.dry_run)
        returncode = run_benchmark(args.benchmark_cmd, args.out, args.timeout_s, args.dry_run)
        status = "ok" if returncode == 0 else "benchmark_failed"
        if returncode != 0:
            executor_failure_class = "benchmark_failure"
    except subprocess.TimeoutExpired:
        status = "timeout"
        returncode = 124
        executor_failure_class = "benchmark_timeout"
        executor_error = "benchmark command timed out"
    except Exception as exc:
        status = f"error: {exc}"
        returncode = 1
        executor_failure_class = classify_executor_exception(exc)
        executor_error = str(exc)
    finally:
        stop_scx(proc, args.dry_run)
        status_path = args.out / "status.json"
        write_json(
            status_path,
            {
                "status": status,
                "returncode": returncode,
                "executor_failure_class": executor_failure_class,
                "executor_error": executor_error,
                "policy_enforcement": {
                    "model": "PolicyEngine",
                    "mode": (
                        "consume_and_reexecute_policy_engine"
                        if args.runtime_policy_preflight
                        else "reexecute_policy_engine"
                    ),
                    "guard_preflight": str(args.out / "guard_preflight.json"),
                    "runtime_policy_preflight": str(args.runtime_policy_preflight) if args.runtime_policy_preflight else None,
                },
                "final_sched_ext_state": sched_ext_state(),
                "end_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
        )
        guard_postflight = postflight(status_path, args.out / "benchmark.log")
        write_json(args.out / "guard_postflight.json", guard_postflight)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

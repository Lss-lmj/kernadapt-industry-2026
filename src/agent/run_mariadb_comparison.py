#!/usr/bin/env python3
"""Run same-batch MariaDB baselines and KernAdapt-online trials.

The goal is to make paper tables come from one controlled protocol instead of
mixing a fresh online result with older CSV baselines.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = Path(__file__).resolve().parent
KERNADAPT_ROOT = REPO_ROOT
DEFAULT_OUT = KERNADAPT_ROOT / "results" / "mariadb_comparison"


def shell_quote_env(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def run(cmd: list[str], *, dry_run: bool = False) -> int:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    if dry_run:
        return 0
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def privileged_cmd(*args: str) -> list[str]:
    if os.geteuid() == 0:
        return list(args)
    return ["sudo", "-n", *args]


def run_logged(cmd: list[str], log: Path, *, dry_run: bool = False) -> int:
    print("+", " ".join(str(c) for c in cmd), ">", log, flush=True)
    if dry_run:
        return 0
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8") as f:
        return subprocess.run(cmd, cwd=REPO_ROOT, stdout=f, stderr=subprocess.STDOUT, text=True).returncode


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def command_result(cmd: list[str], *, timeout_s: float = 3.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except Exception as exc:
        return {"returncode": 127, "stdout": "", "stderr": str(exc)}
    return {"returncode": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()}


def sched_ext_state() -> str:
    state = Path("/sys/kernel/sched_ext/state")
    if not state.exists():
        return "unknown"
    try:
        return state.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def add_readiness_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    passed: bool,
    severity: str,
    evidence: Any,
    remediation: str,
    failure_category: str | None = None,
) -> None:
    checks.append(
        {
            "name": name,
            "passed": bool(passed),
            "severity": severity,
            "evidence": evidence,
            "remediation": remediation,
            "failure_category": failure_category,
        }
    )


def build_readiness(args: argparse.Namespace) -> dict[str, Any]:
    """Preflight host capabilities before starting same-batch trials."""

    checks: list[dict[str, Any]] = []
    sysbench_path = shutil.which("sysbench")
    mysql_path = shutil.which("mysql")
    fixed_scx_required = any(arm in {"fixed_locknest", "fixed_affinity"} for arm in args.arms) and args.fixed_scheduler != "default"
    privileged_required = not args.dry_run

    add_readiness_check(
        checks,
        name="sysbench_available",
        passed=bool(sysbench_path),
        severity="block" if not args.dry_run else "warn",
        evidence=sysbench_path,
        remediation="Install sysbench before running non-dry-run MariaDB comparisons.",
        failure_category="benchmark_failure",
    )
    add_readiness_check(
        checks,
        name="mysql_client_available",
        passed=bool(mysql_path),
        severity="block" if not args.dry_run else "warn",
        evidence=mysql_path,
        remediation="Install mysql client tools before running non-dry-run MariaDB comparisons.",
        failure_category="environment_mismatch",
    )

    mariadb = command_result(["systemctl", "is-active", "mariadb"])
    mariadb_active = mariadb.get("stdout") == "active"
    add_readiness_check(
        checks,
        name="mariadb_active",
        passed=bool(mariadb_active),
        severity="block" if not args.dry_run else "warn",
        evidence=mariadb,
        remediation="Start MariaDB before running same-batch trials.",
        failure_category="environment_mismatch",
    )

    if privileged_required:
        if os.geteuid() == 0:
            sudo_check = {"returncode": 0, "stdout": "root", "stderr": ""}
            sudo_ok = True
        else:
            sudo_check = command_result(["sudo", "-n", "true"])
            sudo_ok = sudo_check.get("returncode") == 0
        add_readiness_check(
            checks,
            name="sudo_noninteractive_available",
            passed=bool(sudo_ok),
            severity="block",
            evidence=sudo_check,
            remediation="Enable passwordless sudo for systemctl/scx execution or run the comparison manually in an approved shell.",
            failure_category="executor_failure",
        )
    else:
        add_readiness_check(
            checks,
            name="sudo_noninteractive_available",
            passed=True,
            severity="info",
            evidence="not_required_for_dry_run",
            remediation="No action required for dry-run.",
            failure_category=None,
        )

    state_value = sched_ext_state()
    add_readiness_check(
        checks,
        name="sched_ext_state_readable",
        passed=state_value != "unknown" or not fixed_scx_required,
        severity="block" if fixed_scx_required and not args.dry_run else "warn",
        evidence=state_value,
        remediation="Use a kernel with sched_ext enabled before running fixed scx arms.",
        failure_category="scheduler_startup_failure",
    )

    if fixed_scx_required:
        binary = Path(os.environ.get("KERNADAPT_SCX_BIN_DIR", REPO_ROOT / "scx" / "build" / "bin")) / args.fixed_scheduler
        add_readiness_check(
            checks,
            name=f"scx_binary_executable:{args.fixed_scheduler}",
            passed=binary.exists() and os.access(binary, os.X_OK),
            severity="block" if not args.dry_run else "warn",
            evidence=str(binary),
            remediation=f"Build sched_ext binary {args.fixed_scheduler} before running the fixed-scx treatment arm.",
            failure_category="scheduler_startup_failure",
        )

    blocking = [check for check in checks if check["severity"] == "block" and not check["passed"]]
    return {
        "schema_version": "lockagent.same_batch_readiness.v0.1",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": args.dry_run,
        "arms": args.arms,
        "fixed_scheduler": args.fixed_scheduler,
        "status": "ready" if not blocking else "blocked",
        "blocking_reasons": [check["name"] for check in blocking],
        "checks": checks,
    }


def batch_ledger(args: argparse.Namespace, batch_id: str) -> dict[str, Any]:
    return {
        "run_id": batch_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": {"collection_status": "comparison_runner_minimal"},
        "sched_ext": {
            "initial_state": "unknown",
            "allowed_schedulers": [
                "default",
                "scx_simple",
                "scx_lockboost",
                "scx_lockcoop",
                "scx_hybrid",
                "scx_toponest",
                "scx_cfl",
                "scx_lcas",
            ],
        },
        "database": {
            "engine": "mariadb",
            "db_name": args.db_name,
            "collection_status": "ok",
            "source": "run_mariadb_comparison_minimal_ledger",
        },
        "benchmark": {
            "workload": "mariadb-sysbench",
            "threads": args.threads,
            "profile_s": args.profile_s,
            "run_s": args.run_s,
            "restart_protocol": "restart_before_each_trial",
        },
    }


def fixed_arm_profile(args: argparse.Namespace, scheduler: str, arm: str) -> dict[str, Any]:
    return {
        "profile_id": f"same_batch_{arm}_t{args.threads}",
        "workload": {
            "name": "MariaDB Sysbench OLTP",
            "threads": args.threads,
            "duration_s": args.run_s,
            "phase": "same_batch_fixed_arm",
        },
        "signals": {"lock_wait": 1.0, "hot_lock_fanin": 1 if scheduler == "default" else 8},
        "current_scheduler": "default",
        "candidate_schedulers": [
            "default",
            "scx_simple",
            "scx_lockboost",
            "scx_lockcoop",
            "scx_hybrid",
            "scx_toponest",
            "scx_cfl",
            "scx_lcas",
        ],
        "objective": {"primary": "same-batch measurement"},
        "data_provenance": {
            "contains_oracle_best": False,
            "same_batch_fixed_arm": arm,
            "note": "Fixed-arm comparison profile enables RuntimeGuard policy checks; it is not an LLM policy input.",
        },
    }


def decision_for(scheduler: str, label: str) -> dict[str, Any]:
    return {
        "scheduler": scheduler,
        "confidence": 1.0,
        "rationale": f"Fixed {label} comparison arm; no LLM policy decision is used.",
        "evidence_used": ["profile.same_batch_comparison_protocol", "ledger.benchmark"],
        "expected_benefit": {
            "throughput": "measurement target",
            "latency": "measurement target",
            "fairness": "measurement target",
        },
        "risks": ["Fixed-arm result is a baseline, not an online policy decision."],
        "validation_plan": ["record sysbench TPS and p95 latency", "verify sched_ext cleanup"],
        "rollback_conditions": ["executor failure", "sched_ext does not return to disabled"],
    }


def benchmark_cmd(args: argparse.Namespace, outdir: Path) -> str:
    return (
        f"{sys.executable} {shell_quote_env(str(AGENT_DIR / 'run_mariadb_sysbench.py'))} "
        f"--threads {args.threads} --time {args.run_s} "
        f"--db-name {shell_quote_env(args.db_name)} "
        f"--db-user {shell_quote_env(args.db_user)} "
        f"--db-pass {shell_quote_env(args.db_pass)} "
        f"--no-restart-mariadb "
        f"--out {shell_quote_env(str(outdir / 'benchmark'))}"
    )


def restart_mariadb_for_trial(trial_dir: Path, dry_run: bool) -> int:
    rc = run_logged(privileged_cmd("systemctl", "restart", "mariadb"), trial_dir / "mariadb_restart.log", dry_run=dry_run)
    if rc == 0 and not dry_run:
        time.sleep(5)
    return rc


def run_fixed_arm(
    *,
    args: argparse.Namespace,
    batch_dir: Path,
    arm: str,
    scheduler: str,
    rep: int,
    dry_run: bool,
) -> dict[str, Any]:
    trial_dir = batch_dir / arm / f"rep_{rep:02d}"
    decision = trial_dir / "decision.json"
    profile = trial_dir / "profile.json"
    execution = trial_dir / "execution"
    write_json(decision, decision_for(scheduler, arm))
    write_json(profile, fixed_arm_profile(args, scheduler=scheduler, arm=arm))
    restart_rc = restart_mariadb_for_trial(trial_dir, dry_run)
    if restart_rc != 0:
        return {
            "arm": arm,
            "scheduler": scheduler,
            "rep": rep,
            "trial_dir": str(trial_dir),
            "returncode": restart_rc,
            "failed_stage": "restart_mariadb",
        }
    cmd = [
        sys.executable,
        str(AGENT_DIR / "scx_executor.py"),
        "--decision",
        str(decision),
        "--benchmark-cmd",
        benchmark_cmd(args, execution),
        "--out",
        str(execution),
        "--timeout-s",
        str(max(args.run_s + 180, 300)),
        "--ledger",
        str(batch_dir / "state_ledger.json"),
        "--profile",
        str(profile),
    ]
    if dry_run:
        cmd.append("--dry-run")
    rc = run(cmd, dry_run=False)
    return {
        "arm": arm,
        "scheduler": scheduler,
        "rep": rep,
        "trial_dir": str(trial_dir),
        "returncode": rc,
    }


def run_online_arm(*, args: argparse.Namespace, batch_dir: Path, rep: int, dry_run: bool) -> dict[str, Any]:
    out = batch_dir / "kernadapt_online"
    memory_root = batch_dir / "kernadapt_memory"
    cmd = [
        sys.executable,
        str(AGENT_DIR / "run_kernadapt_online.py"),
        "--workload",
        "mariadb-sysbench",
        "--threads",
        str(args.threads),
        "--profile-s",
        str(args.profile_s),
        "--run-s",
        str(args.run_s),
        "--out",
        str(out),
    ]
    if not args.shared_kernadapt_memory:
        cmd.extend(
            [
                "--global-memory",
                str(memory_root / "global_memory.jsonl"),
                "--global-memory-summary",
                str(memory_root / "global_memory_summary.json"),
                "--systemic-overrides",
                str(memory_root / "systemic_overrides.json"),
                "--system-evolution-state",
                str(memory_root / "system_evolution_state.json"),
            ]
        )
    if not args.update_kernadapt_skills:
        cmd.append("--no-update-skills")
    if dry_run:
        cmd.append("--dry-run")
    before = {p.name for p in out.glob("run_*")} if out.exists() else set()
    rc = run(cmd, dry_run=False)
    after = sorted(p for p in out.glob("run_*") if p.name not in before)
    trial_dir = after[-1] if after else None
    memory_scope = "shared" if args.shared_kernadapt_memory else "batch_local"
    update_skills = bool(args.update_kernadapt_skills)
    return {
        "arm": "kernadapt_online",
        "legacy_arm": "lockagent_online",
        "scheduler": "llm_selected",
        "rep": rep,
        "trial_dir": str(trial_dir) if trial_dir else None,
        "returncode": rc,
        "kernadapt_memory_scope": memory_scope,
        "kernadapt_update_skills": update_skills,
        "lockagent_memory_scope": memory_scope,
        "lockagent_update_skills": update_skills,
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threads", type=int, default=256)
    ap.add_argument("--run-s", type=int, default=60)
    ap.add_argument("--profile-s", type=int, default=10)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument(
        "--arms",
        nargs="+",
        choices=["default", "fixed_locknest", "fixed_affinity", "kernadapt_online", "lockagent_online"],
        default=["default", "fixed_locknest", "kernadapt_online"],
    )
    ap.add_argument("--fixed-scheduler", default="scx_lockcoop")
    ap.add_argument("--db-user", default=os.environ.get("KERNADAPT_DB_USER", os.environ.get("LOCKAGENT_DB_USER", "sbuser")))
    ap.add_argument("--db-pass", default=os.environ.get("KERNADAPT_DB_PASS", os.environ.get("LOCKAGENT_DB_PASS", "password")))
    ap.add_argument("--db-name", default=os.environ.get("KERNADAPT_DB_NAME", os.environ.get("LOCKAGENT_DB_NAME", "sbtest")))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-only", action="store_true", help="Only write readiness/manifest artifacts; do not run trials")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument(
        "--shared-kernadapt-memory",
        "--shared-lockagent-memory",
        dest="shared_kernadapt_memory",
        action="store_true",
        help="Let KernAdapt-online read/write the global memory and systemic overrides. Default is batch-local isolation.",
    )
    ap.add_argument(
        "--update-kernadapt-skills",
        "--update-lockagent-skills",
        dest="update_kernadapt_skills",
        action="store_true",
        help="Allow KernAdapt-online to mutate the scheduler skill repository. Default keeps same-batch smoke runs read-only.",
    )
    args = ap.parse_args(argv)
    args.arms = ["kernadapt_online" if arm == "lockagent_online" else arm for arm in args.arms]

    batch_id = time.strftime("batch_%Y%m%d_%H%M%S")
    batch_dir = args.out / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    write_json(batch_dir / "state_ledger.json", batch_ledger(args, batch_id))
    readiness = build_readiness(args)
    readiness_path = batch_dir / "batch_readiness.json"
    write_json(readiness_path, readiness)

    manifest: dict[str, Any] = {
        "batch_id": batch_id,
        "threads": args.threads,
        "run_s": args.run_s,
        "profile_s": args.profile_s,
        "reps": args.reps,
        "arms": args.arms,
        "fixed_scheduler": args.fixed_scheduler,
        "dry_run": args.dry_run,
        "check_only": args.check_only,
        "kernadapt_memory_scope": "shared" if args.shared_kernadapt_memory else "batch_local",
        "kernadapt_update_skills": bool(args.update_kernadapt_skills),
        "kernadapt_memory_root": str(batch_dir / "kernadapt_memory") if not args.shared_kernadapt_memory else None,
        "lockagent_memory_scope": "shared" if args.shared_kernadapt_memory else "batch_local",
        "lockagent_update_skills": bool(args.update_kernadapt_skills),
        "lockagent_memory_root": str(batch_dir / "kernadapt_memory") if not args.shared_kernadapt_memory else None,
        "readiness": str(readiness_path),
        "readiness_status": readiness["status"],
        "readiness_blocking_reasons": readiness["blocking_reasons"],
        "start_time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "trials": [],
    }
    write_json(batch_dir / "batch_manifest.json", manifest)

    if args.check_only or readiness["status"] == "blocked":
        manifest["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        manifest["returncode"] = 0 if readiness["status"] == "ready" else 2
        manifest["status"] = readiness["status"]
        write_json(batch_dir / "batch_manifest.json", manifest)
        print(batch_dir)
        return int(manifest["returncode"])

    overall_rc = 0
    try:
        for rep in range(1, args.reps + 1):
            for arm in args.arms:
                if arm == "default":
                    trial = run_fixed_arm(
                        args=args,
                        batch_dir=batch_dir,
                        arm=arm,
                        scheduler="default",
                        rep=rep,
                        dry_run=args.dry_run,
                    )
                elif arm in {"fixed_locknest", "fixed_affinity"}:
                    materialized_arm = "fixed_locknest" if arm == "fixed_affinity" else arm
                    trial = run_fixed_arm(
                        args=args,
                        batch_dir=batch_dir,
                        arm=materialized_arm,
                        scheduler=args.fixed_scheduler,
                        rep=rep,
                        dry_run=args.dry_run,
                    )
                else:
                    trial = run_online_arm(args=args, batch_dir=batch_dir, rep=rep, dry_run=args.dry_run)

                manifest["trials"].append(trial)
                write_json(batch_dir / "batch_manifest.json", manifest)
                if trial["returncode"] != 0:
                    overall_rc = int(trial["returncode"])
                    if args.fail_fast:
                        return overall_rc
    finally:
        manifest["end_time"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        manifest["returncode"] = overall_rc
        manifest["status"] = "ok" if overall_rc == 0 else "failed"
        write_json(batch_dir / "batch_manifest.json", manifest)

    print(batch_dir)
    return overall_rc


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

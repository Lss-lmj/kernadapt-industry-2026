#!/usr/bin/env python3
"""Replay a code-agent style sandbox workflow on a real process tree.

This workload is intentionally *not* an LLM controller.  It models the part of
Codex/Claude Code/OpenHands-style systems that FlowCausal can actually
schedule: the sandboxed tool execution trace.  A replay creates a temporary C
repository and runs several agent turns:

  inspect/search -> edit helper -> build -> test -> optional retry probe

Each step is executed as ordinary subprocesses (`bash`, `find`, `grep`, `wc`,
`python3`, `make`, compiler, linker, and test workers) so the FlowCausal
controller observes the same kind of fork/exec/wait tree it would see inside a
real sandbox.  The workload emits `flowcausal_metric: critical_sec` after the
last user-visible test stage; optional package/post-sleep work happens after
that marker and is therefore reported separately by `run_repo_ci_matrix.py`.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any

from buildtest_sandbox import require_tool, write_project


def emit(event: str, **fields: Any) -> None:
    record = {"type": event, "time_ns": time.time_ns(), **fields}
    print(json.dumps(record, sort_keys=True), flush=True)


def emit_metric(name: str, value: float, **fields: Any) -> None:
    record = {
        "type": "flowcausal_metric",
        "name": name,
        "value": value,
        "time_ns": time.time_ns(),
        **fields,
    }
    print(json.dumps(record, sort_keys=True), flush=True)


def run_stage(
    stage: str,
    cmd: list[str],
    *,
    cwd: pathlib.Path,
    turn: int | None = None,
    allow_failure: bool = False,
) -> tuple[int, float]:
    start = time.monotonic()
    proc = subprocess.Popen(cmd, cwd=str(cwd))
    emit(
        "agent_stage_begin",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        argv=cmd,
        cwd=str(cwd),
    )
    returncode = proc.wait()
    elapsed = time.monotonic() - start
    emit(
        "agent_stage_end",
        stage=stage,
        turn=turn,
        pid=proc.pid,
        returncode=returncode,
        elapsed_sec=elapsed,
    )
    if returncode != 0 and not allow_failure:
        raise SystemExit(returncode)
    return returncode, elapsed


def write_agent_helpers(work_dir: pathlib.Path) -> None:
    tools = work_dir / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    helper = r'''#!/usr/bin/env python3
from __future__ import annotations

import argparse
import pathlib
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True)
    parser.add_argument("--turn", type=int, required=True)
    parser.add_argument("--files", type=int, required=True)
    parser.add_argument("--edits", type=int, default=1)
    args = parser.parse_args()

    src = pathlib.Path(args.src)
    if args.files <= 0:
        raise SystemExit("--files must be positive")
    touched = []
    for offset in range(max(1, args.edits)):
        index = (args.turn + offset) % args.files
        path = src / f"unit_{index:03d}.c"
        text = path.read_text(encoding="utf-8")
        marker = f"\n/* agent-turn={args.turn} edit={offset} ts={time.time_ns()} */\n"
        path.write_text(text.rstrip() + marker, encoding="utf-8")
        touched.append(str(path))
    print("agent_edit_touched=" + ",".join(touched), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    (tools / "agent_edit.py").write_text(
        helper,
        encoding="utf-8",
    )
    os.chmod(tools / "agent_edit.py", 0o755)


def shell_inspect_command(turn: int, inspect_repeats: int) -> str:
    # Use common Unix tools instead of doing inspection in the Python parent, so
    # the workload exposes the shell-driven process tree FlowCausal is meant to
    # observe.
    repeats = max(1, inspect_repeats)
    return "\n".join(
        [
            "set -e",
            "mkdir -p build",
            f"for i in $(seq 1 {repeats}); do",
            f"  find src -type f \\( -name '*.c' -o -name '*.h' \\) -print | sort | xargs wc -l > build/agent-inspect-t{turn}-$i.log",
            f"  grep -R \"fc_accumulate\\|FC_RUNTIME_SPIN\\|uint64_t\" -n src > build/agent-grep-t{turn}-$i.log || true",
            f"  awk 'END {{ print NR }}' build/agent-grep-t{turn}-$i.log > build/agent-count-t{turn}-$i.log",
            "done",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay a sandboxed code-agent inspect/edit/build/test workflow",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--work-dir", help="directory for the generated repository; defaults to a temporary directory")
    parser.add_argument("--keep-work-dir", action="store_true", help="do not delete an auto-created temporary directory")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing an existing --work-dir")
    parser.add_argument("--turns", type=int, default=3, help="number of agent tool-call turns to replay")
    parser.add_argument("--files", type=int, default=40, help="number of generated C translation units")
    parser.add_argument("--statements", type=int, default=180, help="generated arithmetic statements per translation unit")
    parser.add_argument("--jobs", type=int, default=max(2, min(8, os.cpu_count() or 2)), help="make -j parallelism")
    parser.add_argument("--runtime-spin", type=int, default=512, help="runtime loop count compiled into generated units")
    parser.add_argument("--test-workers", type=int, default=4, help="parallel test_runner children spawned by make test")
    parser.add_argument("--test-iters", type=int, default=12000, help="iterations per test_runner")
    parser.add_argument(
        "--test-straggler-factor",
        type=int,
        default=1,
        help="multiply the last test worker's iterations to create a barrier-tail straggler",
    )
    parser.add_argument("--inspect-repeats", type=int, default=2, help="search/inspect command repetitions per turn")
    parser.add_argument("--edits-per-turn", type=int, default=1, help="number of C files touched by the edit helper per turn")
    parser.add_argument("--retry-probe", action="store_true", help="run an expected-failure probe before the final test in each turn")
    parser.add_argument("--package-after-critical", action="store_true", help="run make package after emitting critical_sec")
    parser.add_argument("--pre-sleep-ms", type=int, default=200, help="delay before the first tool command so observers can attach")
    parser.add_argument("--post-sleep-ms", type=int, default=200, help="delay after final optional work so observers can flush")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"), help="C compiler passed to make")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_tool("bash")
    require_tool("find")
    require_tool("grep")
    require_tool("wc")
    require_tool("awk")
    require_tool("make")
    require_tool("tar")
    cc_path = require_tool(args.cc)

    if args.turns < 1:
        raise SystemExit("--turns must be positive")
    if args.files < 1:
        raise SystemExit("--files must be positive")
    if args.jobs < 1:
        raise SystemExit("--jobs must be positive")

    auto_temp = args.work_dir is None
    if auto_temp:
        work_dir = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-agent-replay-"))
    else:
        work_dir = pathlib.Path(args.work_dir).resolve()
        if work_dir.exists():
            if not args.overwrite:
                raise SystemExit(f"work directory exists; pass --overwrite: {work_dir}")
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

    emit(
        "workload_begin",
        workload="agent_workflow_replay",
        work_dir=str(work_dir),
        turns=args.turns,
        files=args.files,
        statements=args.statements,
        jobs=args.jobs,
        cc=cc_path,
        runtime_spin=args.runtime_spin,
        test_workers=args.test_workers,
        test_iters=args.test_iters,
        test_straggler_factor=args.test_straggler_factor,
        inspect_repeats=args.inspect_repeats,
        edits_per_turn=args.edits_per_turn,
    )

    start = time.monotonic()
    timings: dict[str, float] = {}
    try:
        write_project(work_dir, args.files, args.statements)
        write_agent_helpers(work_dir)

        if args.pre_sleep_ms > 0:
            time.sleep(args.pre_sleep_ms / 1000.0)

        env_make = [
            f"CC={cc_path}",
            f"RUNTIME_SPIN={args.runtime_spin}",
            f"TEST_WORKERS={args.test_workers}",
            f"TEST_ITERS={args.test_iters}",
            f"TEST_STRAGGLER_FACTOR={args.test_straggler_factor}",
        ]

        for turn in range(args.turns):
            _, elapsed = run_stage(
                "INSPECT",
                ["bash", "-lc", shell_inspect_command(turn, args.inspect_repeats)],
                cwd=work_dir,
                turn=turn,
            )
            timings[f"turn_{turn}_inspect"] = elapsed

            _, elapsed = run_stage(
                "EDIT",
                [
                    sys.executable,
                    "tools/agent_edit.py",
                    "--src",
                    "src",
                    "--turn",
                    str(turn),
                    "--files",
                    str(args.files),
                    "--edits",
                    str(args.edits_per_turn),
                ],
                cwd=work_dir,
                turn=turn,
            )
            timings[f"turn_{turn}_edit"] = elapsed

            _, elapsed = run_stage(
                "BUILD",
                ["make", f"-j{args.jobs}", *env_make, "all"],
                cwd=work_dir,
                turn=turn,
            )
            timings[f"turn_{turn}_build"] = elapsed

            if args.retry_probe:
                # Real code agents often run a command that fails, inspect the
                # failure, then retry.  This expected-failure probe creates the
                # same fork/wait shape without failing the whole workload.
                _, elapsed = run_stage(
                    "RETRY_PROBE",
                    ["bash", "-lc", "grep -R '__flowcausal_missing_symbol__' -n src"],
                    cwd=work_dir,
                    turn=turn,
                    allow_failure=True,
                )
                timings[f"turn_{turn}_retry_probe"] = elapsed

            _, elapsed = run_stage(
                "TEST",
                ["make", f"-j{args.jobs}", *env_make, "test"],
                cwd=work_dir,
                turn=turn,
            )
            timings[f"turn_{turn}_test"] = elapsed

        critical_sec = time.monotonic() - start
        emit_metric("critical_sec", critical_sec, meaning="last_user_visible_test_complete")
        emit("flowcausal_critical_path", elapsed_sec=critical_sec)

        if args.package_after_critical:
            _, elapsed = run_stage(
                "PACKAGE_BACKGROUND",
                ["make", f"-j{args.jobs}", *env_make, "package"],
                cwd=work_dir,
                turn=None,
            )
            timings["package_after_critical"] = elapsed

        if args.post_sleep_ms > 0:
            time.sleep(args.post_sleep_ms / 1000.0)

        emit("workload_end", workload="agent_workflow_replay", returncode=0, timings=timings)
        return 0
    finally:
        if auto_temp and not args.keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

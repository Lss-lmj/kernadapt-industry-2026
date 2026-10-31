#!/usr/bin/env python3
"""Run one mini-swe-agent SWE-bench instance and write stable capture files.

``mini-extra swebench-single`` writes a trajectory file, but FlowCausal's real
agent evidence pipeline also expects an endpoint JSON path.  This wrapper keeps
the run executable from ``capture_real_agent_run.py`` by writing both:

* the raw trajectory path passed to ``mini-extra``; and
* a conservative endpoint JSON containing instance id, elapsed time, returncode,
  and ``resolved=null`` unless a future evaluator fills it in.

The wrapper does not claim the instance was solved.  It is a real harness runner
adapter, not an evidence upgrader.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
import time
from typing import Any


SCHEMA = "flowcausal.mini_swebench_single_endpoint.v1"


def command_from_args(args: argparse.Namespace) -> list[str]:
    mini_extra = shlex.split(args.mini_extra)
    if not mini_extra:
        raise SystemExit("--mini-extra parsed to an empty command")
    cmd = [
        *mini_extra,
        "swebench-single",
        "--subset",
        args.subset,
        "--split",
        args.split,
        "--instance",
        args.instance_id,
        "--output",
        str(pathlib.Path(args.trajectory).expanduser().resolve()),
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.config:
        for item in args.config:
            cmd.extend(["--config", item])
    if args.yolo:
        cmd.append("--yolo")
    if args.cost_limit is not None:
        cmd.extend(["--cost-limit", str(args.cost_limit)])
    if args.model_class:
        cmd.extend(["--model-class", args.model_class])
    if args.agent_class:
        cmd.extend(["--agent-class", args.agent_class])
    if args.environment_class:
        cmd.extend(["--environment-class", args.environment_class])
    if args.exit_immediately:
        cmd.append("--exit-immediately")
    return cmd


def read_trajectory_hint(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {"trajectory_exists": True, "trajectory_json": False}
    out: dict[str, Any] = {"trajectory_exists": True, "trajectory_json": True}
    if isinstance(data, dict):
        for key in ("instance_id", "status", "result", "resolved", "exit_status"):
            if key in data:
                out[key] = data.get(key)
        trajectory = data.get("trajectory")
        if isinstance(trajectory, list):
            out["trajectory_steps"] = len(trajectory)
        messages = data.get("messages")
        if isinstance(messages, list):
            out["messages"] = len(messages)
    return out


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run mini-extra swebench-single and write FlowCausal endpoint metadata",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--trajectory", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--subset", default="lite")
    parser.add_argument("--split", default="test")
    parser.add_argument("--model", default=os.environ.get("FLOWCAUSAL_MINI_MODEL", ""))
    parser.add_argument("--config", action="append", default=[])
    parser.add_argument("--cost-limit", type=float, default=3.0)
    parser.add_argument("--yolo", action="store_true")
    parser.add_argument("--model-class", default="")
    parser.add_argument("--agent-class", default="")
    parser.add_argument("--environment-class", default="")
    parser.add_argument("--exit-immediately", action="store_true")
    parser.add_argument("--mini-extra", default="mini-extra")
    parser.add_argument("--allow-command-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    trajectory = pathlib.Path(args.trajectory).expanduser().resolve()
    endpoint = pathlib.Path(args.endpoint).expanduser().resolve()
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    endpoint.parent.mkdir(parents=True, exist_ok=True)
    cmd = command_from_args(args)
    started = time.time()
    proc = subprocess.run(cmd, check=False)
    ended = time.time()
    hint = read_trajectory_hint(trajectory)
    resolved = hint.get("resolved") if isinstance(hint.get("resolved"), bool) else None
    payload = {
        "schema": SCHEMA,
        "instance_id": args.instance_id,
        "resolved": resolved,
        "elapsed_sec": ended - started,
        "status": "completed" if proc.returncode == 0 else "command_failed",
        "harness_returncode": proc.returncode,
        "source": "mini-extra swebench-single",
        "provenance": {
            "command": cmd,
            "trajectory": str(trajectory),
            "trajectory_hint": hint,
            "notes": [
                "resolved=null means no official SWE-bench resolved evaluation was inferred",
                "use a framework/evaluator-produced endpoint before making endpoint improvement claims",
            ],
        },
    }
    endpoint.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if proc.returncode != 0 and not args.allow_command_failure:
        return proc.returncode
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

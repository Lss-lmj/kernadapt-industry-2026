#!/usr/bin/env python3
"""Validate Python sources and compile the two unprivileged C workloads."""
import ast
import os
from pathlib import Path
import shlex
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    sources = sorted((ROOT / "src").rglob("*.py"))
    for path in sources:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path.relative_to(ROOT)))
    print(f"Python syntax: {len(sources)} source files passed", flush=True)
    out = ROOT / "build" / "bin"
    out.mkdir(parents=True, exist_ok=True)
    compiler = shlex.split(os.environ.get("CC", "cc"))
    for source, name in [
        ("src/lockcoop/benchmarks/owner_progress_bench.c", "owner_progress_bench"),
        ("src/flowcausal/experiments/flowbench/flowbench.c", "flowbench"),
    ]:
        subprocess.run(compiler + ["-O2", "-Wall", "-Wextra", "-pthread", source, "-o", str(out / name)], cwd=ROOT, check=True)
        print(f"Built build/bin/{name}", flush=True)
    print("Control plane is ready. Kernel scheduler builds: docs/构建与运行.md")


if __name__ == "__main__":
    main()

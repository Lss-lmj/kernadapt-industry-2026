#!/usr/bin/env python3
"""Generate and run a reproducible build/test sandbox workload.

The workload is intentionally small enough for smoke tests but structured like a
real CI tool call:

  1. generate a temporary C project with many translation units;
  2. run `make -jN all`, which creates make -> cc/cc1/as/ar/link barriers;
  3. run `make -jN test`, which fans out test_runner children and waits;
  4. optionally package the tree through tar/gzip.

FlowCausal's online controller should see the *tool process tree* rather than a
synthetic oracle.  Stage markers are printed only for audit logs; they are not
consumed by flowctl.py today.
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


def emit(event: str, **fields: Any) -> None:
    record = {"type": event, "time_ns": time.time_ns(), **fields}
    print(json.dumps(record, sort_keys=True), flush=True)


def require_tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise SystemExit(f"required tool not found in PATH: {name}")
    return path


def c_ident(index: int) -> str:
    return f"fc_unit_{index:03d}"


def unit_source(index: int, statements: int) -> str:
    lines = [
        '#include "common.h"',
        "",
        f"uint64_t {c_ident(index)}(uint64_t x)",
        "{",
        f"    uint64_t v = x ^ UINT64_C(0x{(0x9E3779B97F4A7C15 + index * 0x100000001B3) & ((1 << 64) - 1):016x});",
        "    for (uint64_t r = 0; r < FC_RUNTIME_SPIN; r++) {",
        f"        v ^= (v << 7) + (v >> 3) + UINT64_C({index + 17});",
        "        v *= UINT64_C(11400714819323198485);",
        "    }",
    ]
    for offset in range(statements):
        salt = (index + 1) * (offset + 3)
        shift_l = 5 + (offset % 17)
        shift_r = 3 + (offset % 13)
        lines.append(
            f"    v ^= (v << {shift_l}) ^ (v >> {shift_r}) ^ UINT64_C({salt});"
        )
        if offset % 3 == 0:
            lines.append(
                f"    v += UINT64_C(0x{(salt * 2654435761) & 0xFFFFFFFF:08x});"
            )
    lines.extend(
        [
            "    return v;",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def write_project(work_dir: pathlib.Path, files: int, statements: int) -> None:
    src = work_dir / "src"
    build = work_dir / "build"
    src.mkdir(parents=True, exist_ok=True)
    build.mkdir(parents=True, exist_ok=True)

    declarations = [
        "#ifndef FLOWCAUSAL_BUILDTEST_COMMON_H",
        "#define FLOWCAUSAL_BUILDTEST_COMMON_H",
        "#include <stdint.h>",
        "#ifndef FC_RUNTIME_SPIN",
        "#define FC_RUNTIME_SPIN 256",
        "#endif",
        "",
    ]
    declarations.extend(f"uint64_t {c_ident(i)}(uint64_t x);" for i in range(files))
    declarations.append("uint64_t fc_accumulate(uint64_t seed, unsigned iters);")
    declarations.append("#endif")
    (src / "common.h").write_text("\n".join(declarations) + "\n", encoding="utf-8")

    for index in range(files):
        (src / f"unit_{index:03d}.c").write_text(
            unit_source(index, statements),
            encoding="utf-8",
        )

    switch_cases = "\n".join(
        f"        case {i}: seed ^= {c_ident(i)}(seed + i + UINT64_C({i + 1})); break;"
        for i in range(files)
    )
    (src / "lib.c").write_text(
        f"""#include "common.h"

uint64_t fc_accumulate(uint64_t seed, unsigned iters)
{{
    for (unsigned i = 0; i < iters; i++) {{
        switch (i % {files}) {{
{switch_cases}
        default: break;
        }}
        seed ^= seed >> 29;
        seed *= UINT64_C(14029467366897019727);
    }}
    return seed;
}}
""",
        encoding="utf-8",
    )

    (src / "main.c").write_text(
        """#include "common.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    unsigned iters = argc > 1 ? (unsigned)strtoul(argv[1], 0, 10) : 2000;
    uint64_t value = fc_accumulate(UINT64_C(0x123456789abcdef0), iters);
    printf("%llu\\n", (unsigned long long)value);
    return 0;
}
""",
        encoding="utf-8",
    )

    (src / "test_main.c").write_text(
        """#include "common.h"
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    unsigned iters = argc > 1 ? (unsigned)strtoul(argv[1], 0, 10) : 5000;
    uint64_t a = fc_accumulate(UINT64_C(0xfeedfacecafebeef), iters);
    uint64_t b = fc_accumulate(UINT64_C(0xfeedfacecafebeef), iters);
    if (a != b) {
        fprintf(stderr, "nondeterministic result: %llu != %llu\\n",
                (unsigned long long)a, (unsigned long long)b);
        return 1;
    }
    printf("ok %llu\\n", (unsigned long long)a);
    return 0;
}
""",
        encoding="utf-8",
    )

    (work_dir / "Makefile").write_text(
        """CC ?= cc
CFLAGS ?= -O2 -pipe -Wall -Wextra -std=c11 -DFC_RUNTIME_SPIN=$(RUNTIME_SPIN)
RUNTIME_SPIN ?= 256
TEST_WORKERS ?= 4
TEST_ITERS ?= 8000
TEST_STRAGGLER_FACTOR ?= 1

UNIT_SRCS := $(wildcard src/unit_*.c)
UNIT_OBJS := $(patsubst src/%.c,build/%.o,$(UNIT_SRCS))
LIB_OBJS := $(UNIT_OBJS) build/lib.o

.PHONY: all test package clean

all: build/app build/test_runner

build:
	mkdir -p $@

build/%.o: src/%.c src/common.h | build
	$(CC) $(CFLAGS) -c $< -o $@

build/libflow.a: $(LIB_OBJS)
	ar rcs $@ $^

build/app: build/main.o build/libflow.a
	$(CC) $(CFLAGS) $^ -o $@

build/test_runner: build/test_main.o build/libflow.a
	$(CC) $(CFLAGS) $^ -o $@

test: build/test_runner
	@echo '{"type":"make_stage","stage":"TEST","workers":$(TEST_WORKERS)}'
	@i=0; while [ $$i -lt $(TEST_WORKERS) ]; do \
		iters=$(TEST_ITERS); \
		if [ $$i -eq $$(($(TEST_WORKERS) - 1)) ]; then \
			iters=$$(( $(TEST_ITERS) * $(TEST_STRAGGLER_FACTOR) )); \
		fi; \
		./build/test_runner $$iters > build/test-$$i.log & \
		i=$$((i + 1)); \
	done; \
	wait

package: all
	tar -czf build/flowcausal-buildtest.tar.gz src Makefile

clean:
	rm -rf build
""",
        encoding="utf-8",
    )


def run_stage(stage: str, cmd: list[str], cwd: pathlib.Path) -> float:
    emit("stage_begin", stage=stage, argv=cmd, cwd=str(cwd))
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd))
    elapsed = time.monotonic() - start
    emit("stage_end", stage=stage, returncode=proc.returncode, elapsed_sec=elapsed)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return elapsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate and run a deterministic C build/test sandbox workload",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--work-dir", help="directory for the generated project; defaults to a temporary directory")
    parser.add_argument("--keep-work-dir", action="store_true", help="do not delete an auto-created temporary directory")
    parser.add_argument("--overwrite", action="store_true", help="allow replacing an existing --work-dir")
    parser.add_argument("--files", type=int, default=32, help="number of generated C translation units")
    parser.add_argument("--statements", type=int, default=160, help="generated arithmetic statements per translation unit")
    parser.add_argument("--jobs", type=int, default=max(2, min(8, os.cpu_count() or 2)), help="make -j parallelism")
    parser.add_argument("--runtime-spin", type=int, default=256, help="runtime loop count compiled into generated units")
    parser.add_argument("--test-workers", type=int, default=4, help="parallel test_runner children spawned by make test")
    parser.add_argument("--test-iters", type=int, default=8000, help="iterations per test_runner")
    parser.add_argument(
        "--test-straggler-factor",
        type=int,
        default=1,
        help="multiply the last test worker's iterations to create a barrier-tail straggler",
    )
    parser.add_argument("--pre-sleep-ms", type=int, default=200, help="delay before build so observers can attach")
    parser.add_argument("--post-sleep-ms", type=int, default=200, help="delay after final stage so observers can flush")
    parser.add_argument("--package", action="store_true", help="also run make package after tests")
    parser.add_argument("--cc", default=os.environ.get("CC", "cc"), help="C compiler passed to make")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_tool("make")
    cc_path = require_tool(args.cc)
    require_tool("tar")

    if args.files < 1:
        raise SystemExit("--files must be positive")
    if args.jobs < 1:
        raise SystemExit("--jobs must be positive")

    auto_temp = args.work_dir is None
    if auto_temp:
        work_dir = pathlib.Path(tempfile.mkdtemp(prefix="flowcausal-buildtest-"))
    else:
        work_dir = pathlib.Path(args.work_dir).resolve()
        if work_dir.exists():
            if not args.overwrite:
                raise SystemExit(f"work directory exists; pass --overwrite: {work_dir}")
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True)

    emit(
        "workload_begin",
        work_dir=str(work_dir),
        files=args.files,
        statements=args.statements,
        jobs=args.jobs,
        cc=cc_path,
        runtime_spin=args.runtime_spin,
        test_workers=args.test_workers,
        test_iters=args.test_iters,
        test_straggler_factor=args.test_straggler_factor,
    )

    try:
        write_project(work_dir, args.files, args.statements)
        if args.pre_sleep_ms > 0:
            time.sleep(args.pre_sleep_ms / 1000.0)

        env_make = [
            f"CC={cc_path}",
            f"RUNTIME_SPIN={args.runtime_spin}",
            f"TEST_WORKERS={args.test_workers}",
            f"TEST_ITERS={args.test_iters}",
            f"TEST_STRAGGLER_FACTOR={args.test_straggler_factor}",
        ]
        timings: dict[str, float] = {}
        timings["build"] = run_stage("BUILD", ["make", f"-j{args.jobs}", *env_make, "all"], work_dir)
        timings["test"] = run_stage("TEST", ["make", f"-j{args.jobs}", *env_make, "test"], work_dir)
        if args.package:
            timings["package"] = run_stage("PACKAGE", ["make", f"-j{args.jobs}", *env_make, "package"], work_dir)

        if args.post_sleep_ms > 0:
            time.sleep(args.post_sleep_ms / 1000.0)
        emit("workload_end", returncode=0, timings=timings)
        return 0
    finally:
        if auto_temp and not args.keep_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

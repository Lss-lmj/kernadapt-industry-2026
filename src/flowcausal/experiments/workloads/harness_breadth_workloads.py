#!/usr/bin/env python3
"""Small repo-backed workloads for HarnessReplayBench breadth.

These helpers intentionally avoid external services and package downloads.
They create CPU-visible BUILD / TEST-straggler / PACKAGE-heavy stages under
``$FLOWCAUSAL_STAGE_OUT`` so repo-stage traces can be replayed deterministically
by HarnessReplayBench.  They are benchmark-breadth seeds, not live agent traces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pathlib
import shutil
import subprocess
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any


def ensure_dir(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(cmd: list[str], *, cwd: pathlib.Path | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=True)


def file_sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_tools(args: argparse.Namespace) -> int:
    tools = {
        "gcc": shutil.which("gcc"),
        "g++": shutil.which("g++"),
        "rustc": shutil.which("rustc"),
        "cargo": shutil.which("cargo"),
        "python3": shutil.which("python3"),
        "tar": shutil.which("tar"),
    }
    out = ensure_dir(pathlib.Path(args.out))
    write_json(
        out / "tool-inspect.json",
        {
            "status": "pass",
            "tools": tools,
            "cwd": os.getcwd(),
            "pid": os.getpid(),
        },
    )
    missing_required = [name for name in ("gcc", "g++", "rustc", "python3", "tar") if not tools.get(name)]
    if missing_required:
        raise SystemExit(f"missing required benchmark tools: {missing_required}")
    return 0


def cxx_rust_build(args: argparse.Namespace) -> int:
    out = ensure_dir(pathlib.Path(args.out))
    src = ensure_dir(out / "src")
    build = ensure_dir(out / "build")

    c_sources: list[pathlib.Path] = []
    for idx in range(args.c_units):
        path = src / f"unit_{idx:02d}.c"
        path.write_text(
            "\n".join(
                [
                    "#include <stdint.h>",
                    f"uint64_t flow_unit_{idx}(uint64_t x) {{",
                    "    uint64_t acc = x;",
                    f"    for (uint64_t i = 0; i < {args.loop_iters}ULL; i++) {{",
                    f"        acc = (acc * 1103515245ULL + 12345ULL + {idx}ULL + i) & 0xffffffffULL;",
                    "    }",
                    "    return acc;",
                    "}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        c_sources.append(path)

    main_c = src / "main.c"
    decls = "\n".join(f"uint64_t flow_unit_{idx}(uint64_t);" for idx in range(args.c_units))
    calls = "\n".join(f"    acc ^= flow_unit_{idx}(acc + {idx}ULL);" for idx in range(args.c_units))
    main_c.write_text(
        f"""#include <stdint.h>
#include <stdio.h>
{decls}
int main(void) {{
    uint64_t acc = 0xC0FFEEULL;
{calls}
    printf("%llu\\n", (unsigned long long)acc);
    return 0;
}}
""",
        encoding="utf-8",
    )

    objects: list[pathlib.Path] = []
    start = time.monotonic()
    for source in c_sources + [main_c]:
        obj = build / (source.stem + ".o")
        run(["gcc", "-O2", "-c", str(source), "-o", str(obj)])
        objects.append(obj)
    run(["gcc", "-O2", *[str(obj) for obj in objects], "-o", str(build / "c_workload")])

    cpp = src / "template_workload.cpp"
    cpp.write_text(
        f"""#include <array>
#include <cstdint>
#include <iostream>
template <int N>
struct Worker {{
    static uint64_t run(uint64_t x) {{
        for (int i = 0; i < {args.cpp_iters}; ++i)
            x = (x * 1664525ULL + 1013904223ULL + N + i) & 0xffffffffULL;
        return Worker<N - 1>::run(x ^ static_cast<uint64_t>(N));
    }}
}};
template <>
struct Worker<0> {{
    static uint64_t run(uint64_t x) {{ return x; }}
}};
int main() {{
    std::cout << Worker<{args.cpp_depth}>::run(7) << "\\n";
    return 0;
}}
""",
        encoding="utf-8",
    )
    run(["g++", "-O2", "-std=c++17", str(cpp), "-o", str(build / "cpp_workload")])

    rust = src / "rust_workload.rs"
    rust.write_text(
        f"""fn step(mut x: u64, salt: u64) -> u64 {{
    for i in 0..{args.rust_iters}u64 {{
        x = x.wrapping_mul(6364136223846793005).wrapping_add(1 + salt + i);
    }}
    x
}}
fn main() {{
    let mut acc = 0x1234_5678u64;
    for salt in 0..{args.rust_units}u64 {{
        acc ^= step(acc + salt, salt);
    }}
    println!("{{}}", acc);
}}
""",
        encoding="utf-8",
    )
    run(["rustc", "-O", str(rust), "-o", str(build / "rust_workload")])

    elapsed = time.monotonic() - start
    write_json(
        out / "build-summary.json",
        {
            "status": "pass",
            "elapsed_sec": elapsed,
            "c_units": args.c_units,
            "outputs": sorted(path.name for path in build.iterdir()),
        },
    )
    return 0


def cxx_rust_test(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out)
    build = out / "build"
    binaries = [build / "c_workload", build / "cpp_workload", build / "rust_workload"]
    results: dict[str, str] = {}
    start = time.monotonic()
    for binary in binaries:
        output = subprocess.check_output([str(binary)], text=True).strip()
        if not output:
            raise SystemExit(f"{binary} produced empty output")
        results[binary.name] = output
    write_json(
        out / "test-summary.json",
        {
            "status": "pass",
            "elapsed_sec": time.monotonic() - start,
            "results": results,
        },
    )
    return 0


def busy_ms(ms: int, salt: int) -> dict[str, Any]:
    deadline = time.perf_counter() + ms / 1000.0
    acc = float(salt + 1)
    loops = 0
    while time.perf_counter() < deadline:
        acc = math.sin(acc + loops % 17) * math.cos(acc / 3.0 + salt)
        loops += 1
    return {"salt": salt, "loops": loops, "value": acc}


def test_straggler_prepare(args: argparse.Namespace) -> int:
    out = ensure_dir(pathlib.Path(args.out))
    write_json(
        out / "test-straggler-plan.json",
        {
            "status": "pass",
            "workers": args.workers,
            "short_ms": args.short_ms,
            "tail_ms": args.tail_ms,
            "style": "pytest-tox-nox-like-multiworker-tail",
            "note": "pytest/tox/nox are not required; this preserves the CPU-visible test straggler shape with stdlib workers.",
        },
    )
    return 0


def test_straggler_run(args: argparse.Namespace) -> int:
    out = ensure_dir(pathlib.Path(args.out))
    durations = [args.short_ms for _ in range(args.workers)]
    if durations:
        durations[-1] = args.tail_ms
    start = time.monotonic()
    results: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(busy_ms, ms, idx) for idx, ms in enumerate(durations)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.monotonic() - start
    write_json(
        out / "test-straggler-summary.json",
        {
            "status": "pass",
            "elapsed_sec": elapsed,
            "workers": args.workers,
            "short_ms": args.short_ms,
            "tail_ms": args.tail_ms,
            "result_count": len(results),
            "total_loops": sum(int(item["loops"]) for item in results),
        },
    )
    return 0


def package_prepare(args: argparse.Namespace) -> int:
    out = ensure_dir(pathlib.Path(args.out))
    data = ensure_dir(out / "package-input")
    for idx in range(args.files):
        path = data / f"log_{idx:04d}.txt"
        seed = hashlib.sha256(f"{idx}:{args.size_kb}".encode()).hexdigest()
        line = f"{idx:04d} {seed} FlowCausal package-heavy artifact line\n"
        repeats = max(1, (args.size_kb * 1024) // len(line))
        path.write_text(line * repeats, encoding="utf-8")
    write_json(out / "package-prepare-summary.json", {"status": "pass", "files": args.files, "size_kb": args.size_kb})
    return 0


def package_run(args: argparse.Namespace) -> int:
    out = ensure_dir(pathlib.Path(args.out))
    data = out / "package-input"
    if not data.is_dir():
        package_prepare(args)
    archive = out / "package-heavy-artifacts.tar.gz"
    start = time.monotonic()
    with tarfile.open(archive, "w:gz", compresslevel=args.compresslevel) as tar:
        tar.add(data, arcname="package-input")
    elapsed = time.monotonic() - start
    write_json(
        out / "package-summary.json",
        {
            "status": "pass",
            "elapsed_sec": elapsed,
            "archive": str(archive),
            "archive_size": archive.stat().st_size,
            "archive_sha256": file_sha256(archive),
        },
    )
    return 0


def package_validate(args: argparse.Namespace) -> int:
    out = pathlib.Path(args.out)
    summary = out / "package-summary.json"
    if not summary.is_file():
        raise SystemExit(f"missing package summary: {summary}")
    payload = json.loads(summary.read_text(encoding="utf-8"))
    archive = pathlib.Path(payload["archive"])
    if not archive.is_file() or archive.stat().st_size <= 0:
        raise SystemExit(f"invalid archive: {archive}")
    return 0


def add_common_out(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", required=True, help="stage output directory, usually $FLOWCAUSAL_STAGE_OUT/<case>")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect-tools")
    add_common_out(p)
    p.set_defaults(func=inspect_tools)

    p = sub.add_parser("cxx-rust-build")
    add_common_out(p)
    p.add_argument("--c-units", type=int, default=24)
    p.add_argument("--loop-iters", type=int, default=1800)
    p.add_argument("--cpp-depth", type=int, default=48)
    p.add_argument("--cpp-iters", type=int, default=1200)
    p.add_argument("--rust-units", type=int, default=16)
    p.add_argument("--rust-iters", type=int, default=1800)
    p.set_defaults(func=cxx_rust_build)

    p = sub.add_parser("cxx-rust-test")
    add_common_out(p)
    p.set_defaults(func=cxx_rust_test)

    p = sub.add_parser("test-straggler-prepare")
    add_common_out(p)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--short-ms", type=int, default=80)
    p.add_argument("--tail-ms", type=int, default=600)
    p.set_defaults(func=test_straggler_prepare)

    p = sub.add_parser("test-straggler-run")
    add_common_out(p)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--short-ms", type=int, default=80)
    p.add_argument("--tail-ms", type=int, default=600)
    p.set_defaults(func=test_straggler_run)

    p = sub.add_parser("package-prepare")
    add_common_out(p)
    p.add_argument("--files", type=int, default=160)
    p.add_argument("--size-kb", type=int, default=8)
    p.set_defaults(func=package_prepare)

    p = sub.add_parser("package-run")
    add_common_out(p)
    p.add_argument("--files", type=int, default=160)
    p.add_argument("--size-kb", type=int, default=8)
    p.add_argument("--compresslevel", type=int, default=6)
    p.set_defaults(func=package_run)

    p = sub.add_parser("package-validate")
    add_common_out(p)
    p.set_defaults(func=package_validate)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())

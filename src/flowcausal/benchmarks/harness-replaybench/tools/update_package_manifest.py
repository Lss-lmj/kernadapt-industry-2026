#!/usr/bin/env python3
"""Rebuild PACKAGE_MANIFEST.json from the benchmark tree."""
from __future__ import annotations

import hashlib
import json
import pathlib


ROOT = pathlib.Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "PACKAGE_MANIFEST.json"
EXCLUDED_TREES = {
    ("derived",),
    ("repos", "matplotlib__matplotlib-23314", ".pytest_cache"),
    ("repos", "matplotlib__matplotlib-23314", "build"),
    ("repos", "matplotlib__matplotlib-23314", "tmp"),
    ("repos", "sympy__sympy-21171", ".pytest_cache"),
}


def is_excluded(parts: tuple[str, ...]) -> bool:
    if "__pycache__" in parts or ".pytest_cache" in parts:
        return True
    if parts[:2] == ("capture", "build"):
        return True
    return any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_TREES)


def digest(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main() -> int:
    files = []
    for path in sorted(ROOT.rglob("*")):
        rel_parts = path.relative_to(ROOT).parts
        if (
            not path.is_file()
            or path == OUTPUT
            or is_excluded(rel_parts)
            or path.suffix == ".pyc"
        ):
            continue
        files.append(
            {
                "bytes": path.stat().st_size,
                "path": path.relative_to(ROOT).as_posix(),
                "sha256": digest(path),
            }
        )
    payload = {
        "file_count": len(files),
        "files": files,
        "schema": "harness_replaybench.package_manifest.v1",
        "version": (ROOT / "VERSION").read_text(encoding="utf-8").strip(),
    }
    OUTPUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run cfdm's complete test suite in a stable discovery order.

cfdm 1.8 deliberately randomizes both test-case and test-method order.  That is
useful for its own repeated testing, but it makes a recorded agent command
unsuitable for deterministic replay.  This wrapper changes only suite
construction: it preserves unittest's discovery order and then calls the same
three suite entry points as ``run_tests.py``.  The process-wide random module
is never modified.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from collections.abc import Callable
from typing import Any


def load_suite(path: pathlib.Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8")
    import_line = "from random import choice, shuffle"
    if source.count(import_line) != 1:
        raise RuntimeError("unexpected cfdm random-order implementation")
    # Replace only the test driver's two local ordering functions.  The
    # process-wide random module remains untouched while cfdm and its
    # dependencies import and while the tests execute.
    source = source.replace(
        import_line,
        "def choice(_items):\n"
        "    return 0\n\n"
        "def shuffle(_items):\n"
        "    return None",
        1,
    )
    namespace: dict[str, Any] = {
        "__name__": "hrb_cfdm_test_suite",
        "__file__": str(path),
        "__package__": None,
        "__cached__": None,
    }
    exec(compile(source, str(path), "exec"), namespace)
    return namespace


def suite_entry(namespace: dict[str, Any], name: str) -> Callable[[], None]:
    value = namespace.get(name)
    if not callable(value):
        raise RuntimeError(f"{name} is not callable in the cfdm test driver")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="run the complete cfdm suite deterministically")
    parser.add_argument("driver", type=pathlib.Path)
    args = parser.parse_args()

    driver = args.driver.resolve()
    sys.argv = [str(driver)]
    namespace = load_suite(driver)
    suite_entry(namespace, "run_test_suite_setup_0")()
    suite_entry(namespace, "run_test_suite_setup_1")()
    suite_entry(namespace, "run_test_suite")()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

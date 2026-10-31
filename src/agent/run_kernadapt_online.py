#!/usr/bin/env python3
"""Run the KernAdapt online control loop.

This is the active entry point.  ``run_lockagent_online.py`` remains as a
compatibility module while the historical LockAgent implementation is being
ported into mechanism-neutral KernAdapt controller terminology.
"""

from __future__ import annotations

import sys

from run_lockagent_online import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

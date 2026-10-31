"""Replay-only Python compatibility hooks for captured agent traces.

The normalized mini-swe-agent traces preserve the original shell commands.
Some historical traces were captured against older Python package stacks.  This
module is imported automatically when ``tool_shims/python`` is used and keeps
those commands runnable on the local harness venv without editing the traces.
"""

try:
    import numpy as _np

    if not hasattr(_np, "Inf"):
        _np.Inf = _np.inf
except Exception:
    pass

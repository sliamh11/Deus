"""Shared best-effort desktop notifier for maintenance probes (no-op off macOS)."""
from __future__ import annotations

import json
import subprocess
import sys


def macos_notify(title: str, message: str) -> None:
    """Best-effort macOS banner. Never raises; no-op on non-Darwin platforms."""
    if sys.platform != "darwin":
        return
    try:
        # json.dumps escapes the " and \ that AppleScript string literals need —
        # the correct sanitizer for embedding arbitrary text in the `-e` script
        # (shlex.quote would be wrong here). ensure_ascii=False is required: the
        # default \uXXXX escaping mangles non-ASCII (e.g. the em dash in a WARN
        # message), which AppleScript does not decode; UTF-8 passes through fine.
        def _lit(s: str) -> str:
            return json.dumps(s, ensure_ascii=False)
        subprocess.run(
            ["osascript", "-e",
             f"display notification {_lit(message)} with title {_lit(title)}"],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass

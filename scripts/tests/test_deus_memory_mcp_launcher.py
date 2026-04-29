"""Tests for the deus-memory MCP launcher wrapper."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
_LAUNCHER = _ROOT / "scripts" / "deus-memory-mcp"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_launcher_uses_override_python_when_it_has_mcp(tmp_path):
    fake_python = tmp_path / "python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env sh
if [ "$1" = "-c" ]; then
  exit 0
fi
printf '%s\\n' "$@"
""",
    )

    env = {
        **os.environ,
        "DEUS_MEMORY_MCP_PYTHON": str(fake_python),
    }
    result = subprocess.run(
        [str(_LAUNCHER), "--probe"],
        check=True,
        env=env,
        text=True,
        capture_output=True,
    )

    lines = result.stdout.splitlines()
    assert lines[0] == str(_ROOT / "scripts" / "memory_mcp_server.py")
    assert lines[1] == "--probe"


def test_launcher_rejects_override_python_without_mcp(tmp_path):
    fake_python = tmp_path / "python"
    _write_executable(
        fake_python,
        """#!/usr/bin/env sh
if [ "$1" = "-c" ]; then
  exit 1
fi
exit 99
""",
    )

    env = {
        **os.environ,
        "DEUS_MEMORY_MCP_PYTHON": str(fake_python),
    }
    result = subprocess.run(
        [str(_LAUNCHER)],
        check=False,
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 1
    assert "DEUS_MEMORY_MCP_PYTHON does not provide" in result.stderr
    assert str(fake_python) in result.stderr

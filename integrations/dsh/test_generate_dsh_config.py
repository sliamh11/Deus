#!/usr/bin/env python3
"""Regression tests for dsh config emission.

Run: python3 integrations/dsh/test_generate_dsh_config.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_dsh_config import emit_hooks, normalize_if_handlers  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


gate = "/Users/example/.claude/hooks/bash-command-gate.sh"
conditioned = [
    {"type": "command", "command": gate, "if": "Bash(git *)", "timeout": 5},
    {"type": "command", "command": gate, "if": "Bash(gh pr create *)", "timeout": 3},
    {"type": "command", "command": gate, "if": "Bash(rm *)", "timeout": 3},
    {"type": "command", "command": gate, "if": "Bash(find * -delete)", "timeout": 3},
    {"type": "command", "command": "/hooks/other.sh", "timeout": 2},
]

normalized, report = normalize_if_handlers(conditioned)
check("conditioned handler count", len(normalized), 2)
check("collapsed command", normalized[0]["command"], gate)
check("collapsed timeout", normalized[0]["timeout"], 5)
check("collapsed if removed", "if" in normalized[0], False)
if not any("4 ignored `if:` rows -> 1" in line for line in report):
    failures.append("condition collapse is not reported")

hooks, emitted_report = emit_hooks(
    {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": conditioned}]}},
    {},
)
check("emitted matcher", hooks["PreToolUse"][0]["matcher"], "bash")
check("emitted handler count", len(hooks["PreToolUse"][0]["hooks"]), 2)
if not any("DEDUP conditioned" in line for line in emitted_report):
    failures.append("emit_hooks dropped the condition-collapse report")

unknown = [{"type": "command", "command": "/hooks/unknown.sh", "if": "Bash(git *)"}]
kept, unknown_report = normalize_if_handlers(unknown)
check("unknown condition preserved", kept, unknown)
if not any(line.startswith("LOSS") for line in unknown_report):
    failures.append("unknown ignored condition is not reported as a loss")

if failures:
    print(f"FAIL: {len(failures)} assertion(s)")
    for line in failures:
        print(f"  - {line}")
    raise SystemExit(1)

print("PASS: all config-emission assertions hold")

"""Regression tests for the matcher translator.

Two silent-widening bugs have already been found in this module, both on the
SUCCESS path rather than the failure path:

1. `map_matcher` returned a bare `None` for both "no matcher, fires for
   everything" and "nothing maps, fires for nothing". The caller conflated
   them and dropped every match-all group.
2. A degenerate matcher (`"|"`) translated to `""`, which is one of the
   bridge's own match-all sentinels, turning a matcher that should select
   nothing into one that fires on every tool.

Both produced a plausible-looking result and no error, which is why they are
pinned here rather than left to review.

Run: python3 integrations/dsh/test_tool_name_map.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_name_map import _MATCH_ALL, map_matcher, map_tool_list, selects_a_real_tool  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


# --- the degenerate-matcher regression (bug 2) --------------------------------
# Every one of these must be `dead`. None may yield a match-all sentinel,
# because that is fire-on-everything for a matcher that should select nothing.
for pattern in ("|", "||", "|||"):
    status, matcher, _ = map_matcher(pattern)
    check(f"degenerate {pattern!r} status", status, "dead")
    check(f"degenerate {pattern!r} matcher", matcher, None)
    if matcher in _MATCH_ALL and matcher is not None:
        failures.append(f"degenerate {pattern!r} leaked match-all sentinel {matcher!r}")

# --- match-all vs dead must stay distinct (bug 1) -----------------------------
for sentinel in (None, "", "*"):
    status, matcher, _ = map_matcher(sentinel)
    check(f"sentinel {sentinel!r} status", status, "match-all")
    check(f"sentinel {sentinel!r} matcher", matcher, sentinel)

# A matcher naming only unmapped tools is dead, NOT match-all.
for pattern in ("NotebookEdit", "NotebookEdit|SlashCommand", "KillShell"):
    status, matcher, _ = map_matcher(pattern)
    check(f"unmapped {pattern!r} status", status, "dead")
    check(f"unmapped {pattern!r} matcher", matcher, None)

# --- ordinary translation ------------------------------------------------------
check("Bash", map_matcher("Bash")[:2], ("translated", "bash"))
check("Write|Edit", map_matcher("Write|Edit")[:2], ("translated", "write|edit"))
check("Task|Agent dedups", map_matcher("Task|Agent")[:2], ("translated", "subagent"))
check("partial drop", map_matcher("Bash|NotebookEdit")[:2], ("translated", "bash"))

# An approximation must translate AND announce itself.
status, matcher, notes = map_matcher("MultiEdit")
check("MultiEdit status", status, "translated")
check("MultiEdit matcher", matcher, "str_replace_editor")
if not any("APPROXIMATE" in n for n in notes):
    failures.append("MultiEdit: approximation was not reported in notes")

# Both approximations collapsing to one tool must not emit a duplicate.
check("MultiEdit|apply_patch", map_matcher("MultiEdit|apply_patch")[1], "str_replace_editor")

# --- regex passthrough ---------------------------------------------------------
regex = "mcp__srv__(alpha|beta)"
check("regex status", map_matcher(regex)[:2], ("unchanged", regex))

# An already-dsh-native matcher is unchanged, not "translated".
check("native bash", map_matcher("bash")[0], "unchanged")

# --- regex matchers that NAME Claude tools (bug 3) ----------------------------
# `(Bash|Write)` and `Bash.*` are regexes by the bridge's discriminator, so they
# skip literal translation. Passed through untouched they match nothing, because
# dsh's tools are lowercase.
check("regex (Bash|Write)", map_matcher("(Bash|Write)")[1], "(bash|write)")
check("regex Bash.*", map_matcher("Bash.*")[1], "bash.*")
check("regex status is translated", map_matcher("(Bash|Write)")[0], "translated")

# Word boundaries: an MCP tool name must NOT be rewritten by the embedded-name
# pass, even though it can contain substrings of tool names.
mcp = "mcp__claude_ai_Gmail__(create_draft|label_message)"
check("mcp regex untouched", map_matcher(mcp)[1], mcp)
check("mcp regex status", map_matcher(mcp)[0], "unchanged")

# Longest-first ordering: `MultiEdit` must not be mangled into `MultiEdit`->
# `edit` by matching the `Edit` suffix first.
check("regex MultiEdit", map_matcher("(MultiEdit)")[1], "(str_replace_editor)")

# --- allow-list translation (bug 4) -------------------------------------------
# An allow-list that matches nothing grants NOTHING -- the opposite failure to a
# matcher that matches nothing, and a total capability loss for that agent.
allow, _ = map_tool_list(["Read", "Grep", "Glob"])
check("allow-list translated", allow, ["read", "grep", "glob"])

allow, notes = map_tool_list(["Read", "NotebookEdit"])
check("allow-list drops unmapped", allow, ["read"])
if not any("NotebookEdit" in n for n in notes):
    failures.append("allow-list: dropped tool was not reported")

allow, _ = map_tool_list(["MultiEdit", "apply_patch"])
check("allow-list dedups approximations", allow, ["str_replace_editor"])

allow, _ = map_tool_list(["NotebookEdit"])
check("allow-list all-unmapped is empty", allow, [])

# --- regex naming ONLY unmapped tools (bug 5) ---------------------------------
# These rewrite to nothing, stay byte-identical, and without the catalog check
# would be emitted as `unchanged` and escape --check entirely.
for pattern in ("(NotebookEdit)", "^NotebookEdit$", "NotebookEdit|SlashCommand.*"):
    status, matcher, _ = map_matcher(pattern)
    check(f"unmapped regex {pattern!r} status", status, "dead")
    check(f"unmapped regex {pattern!r} matcher", matcher, None)

# A regex naming a real dsh tool alongside an unmapped one still lives.
check("mixed regex survives", map_matcher("(Bash|NotebookEdit)")[0], "translated")

# --- the catalog check itself --------------------------------------------------
check("literal hits catalog", selects_a_real_tool("bash"), True)
check("literal misses catalog", selects_a_real_tool("no_such_tool"), False)
check("regex hits catalog", selects_a_real_tool("^ba.h$"), True)
check("regex misses catalog", selects_a_real_tool("^zzz$"), False)
# MCP names are registered at runtime, so their absence proves nothing.
check("mcp exempt", selects_a_real_tool("mcp__srv__anything"), True)
# An uncompilable pattern cannot select anything.
check("broken regex", selects_a_real_tool("(unclosed"), False)

# --- report --------------------------------------------------------------------
if failures:
    print(f"FAIL: {len(failures)} assertion(s)")
    for line in failures:
        print(f"  - {line}")
    raise SystemExit(1)

print("PASS: all matcher translation assertions hold")

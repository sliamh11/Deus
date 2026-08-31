"""Claude Code tool names -> DeepSeek Harness tool names.

Why this module exists
----------------------
The dsh hook bridge (`@deepseek-ai/dsh-hooks-claude-code`) runs an existing
Claude Code `hooks.json` unchanged. It also reproduces Claude Code's matcher
semantics faithfully -- including the part that breaks the port:

    packages/hooks/hook-protocol/src/matcher.ts
        if (mode === 'claude-code' && CLAUDE_LITERAL.test(pattern)) {
          return pattern.split('|').includes(query)
        }

That is an exact, case-sensitive membership test. dsh's tools are snake_case
(`bash`, `write`, `edit`), so a Claude Code matcher of `Bash` never matches
and its handlers register successfully and then never fire. Measured against
one host's live config: 11 of 21 matcher groups dead, 28 of 41 handlers --
every `PreToolUse` and `PostToolUse` gate. The 10 surviving groups are the
matcher-less `SessionStart` / `UserPromptSubmit` events plus one MCP regex,
which are unaffected because they never compare a tool name.

A dead handler raises no error, which is the failure mode this module exists
to prevent.

Design
------
A lookup registry of three *structurally distinct* dicts rather than one
merged table. Keeping equivalences, approximations and drops in separate
containers means an approximation cannot be silently read as an equivalence
by a later maintainer -- the distinction survives in the type, not in a
comment. Membership is O(1).

Verified against dsh 0.1.2-alpha.2's generated `docs/tool-catalog.md`.
"""

from __future__ import annotations

# Direct equivalents: same capability, different spelling.
TOOL_MAP: dict[str, str] = {
    "Bash": "bash",
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Glob": "glob",
    "Grep": "grep",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "ExitPlanMode": "exit_plan_mode",
    "Task": "subagent",
    "Agent": "subagent",
    "Skill": "skill",
    "TodoWrite": "todo_write",
    "AskUserQuestion": "ask_user_question",
}

# NOT equivalents. `str_replace_editor` is a different tool with different
# semantics; mapping onto it WIDENS the matcher rather than translating it.
# Kept separate from TOOL_MAP so the difference cannot be lost in a refactor,
# and every use is reported in the notes returned by `map_matcher`.
APPROXIMATED: dict[str, str] = {
    "MultiEdit": "str_replace_editor",
    "apply_patch": "str_replace_editor",
}

# Claude Code tools with no dsh counterpart. A matcher naming only these
# cannot be ported; its handlers stay dead and `map_matcher` says so.
UNMAPPED: frozenset[str] = frozenset({
    "NotebookEdit",
    "SlashCommand",
    "KillShell",
    "BashOutput",
})

# The bridge's own literal-vs-regex discriminator, mirrored:
#   const CLAUDE_LITERAL = /^[A-Za-z0-9_|]+$/
_LITERAL_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789_|"
)

# Absent / empty / '*' are the bridge's match-all sentinels.
_MATCH_ALL = (None, "", "*")


def is_claude_literal(pattern: str) -> bool:
    """True when the bridge would treat `pattern` as literal alternatives.

    Mirrors `CLAUDE_LITERAL.test(pattern)`. A pattern of only word characters
    and `|` is literal; anything else is an unanchored regex.
    """
    return bool(pattern) and all(c in _LITERAL_CHARS for c in pattern)


def map_tool_name(name: str) -> tuple[str | None, str | None]:
    """Translate ONE Claude Code tool name.

    Returns `(dsh_name, note)`. `dsh_name` is None when the tool has no dsh
    counterpart and must be dropped.

    Anywhere a Claude Code tool name is written down it needs this translation,
    not just in hook matchers: an agent's `tools:` allow-list carries the same
    names, and an allow-list that matches nothing grants NOTHING rather than
    everything -- a silent total capability loss for that agent.
    """
    if name in TOOL_MAP:
        return TOOL_MAP[name], None
    if name in APPROXIMATED:
        return APPROXIMATED[name], (
            f"{name} -> {APPROXIMATED[name]} (APPROXIMATE: widens, not equivalent)"
        )
    if name in UNMAPPED:
        return None, f"{name} dropped (no dsh tool)"
    return name, f"{name} kept verbatim (unrecognised)"


def map_tool_list(names: list[str]) -> tuple[list[str], list[str]]:
    """Translate an allow/deny list of Claude Code tool names.

    Returns `(dsh_names, notes)`, order-preserving and deduplicated.
    """
    out: list[str] = []
    notes: list[str] = []
    seen: set[str] = set()
    for name in names:
        mapped, note = map_tool_name(name)
        if note:
            notes.append(note)
        if mapped is not None and mapped not in seen:
            seen.add(mapped)
            out.append(mapped)
    return out, notes


# dsh's model-visible tool names, from its generated `docs/tool-catalog.md`
# (v0.1.2-alpha.2). This is the ground truth a matcher is finally checked
# against: name-level translation can be individually correct and still yield a
# pattern that matches nothing, so the last check is "does this actually select
# a tool that exists".
DSH_TOOLS: frozenset[str] = frozenset({
    "ask_user_question", "bash", "edit", "exit_plan_mode", "glob", "grep",
    "read", "read_image", "report", "run_code", "skill", "str_replace_editor",
    "subagent", "todo_write", "web_fetch", "web_search", "workflow", "write",
    "lsp", "pwsh", "ralph", "interrupt_agent", "list_agents", "wait_agent",
    "followup_task", "create_goal", "get_goal", "update_goal",
    "job_kill", "job_list", "job_output", "send_message", "spawn_teammate",
    "schedule_create", "schedule_delete", "schedule_list",
    "terminal_close", "terminal_list", "terminal_open", "terminal_read",
    "terminal_send", "terminal_signal", "list_subagent_models",
    "team_task_create", "team_task_get", "team_task_list", "team_task_update",
    "session_event_read", "session_event_search", "session_event_trace",
    "session_search", "session_trace",
})

# MCP tools are contributed at runtime as `mcp__<server>__<tool>`, so they are
# NOT in the static catalog. A pattern referencing one cannot be checked against
# `DSH_TOOLS` and must be exempted rather than reported dead.
_MCP_PREFIX = "mcp__"


def selects_a_real_tool(pattern: str) -> bool:
    """Whether `pattern` can select at least one tool that actually exists.

    The final safety net. Every per-name translation can be correct while the
    assembled pattern still matches nothing -- which is the silent-dead-hook
    failure this module exists to detect, arriving one level up from the names.

    Patterns referencing `mcp__` are exempt: those tool names are registered at
    runtime by whichever MCP servers are mounted, so their absence from the
    static catalog proves nothing.
    """
    if _MCP_PREFIX in pattern:
        return True
    if is_claude_literal(pattern):
        return any(part in DSH_TOOLS for part in pattern.split("|"))
    import re
    try:
        compiled = re.compile(pattern)
    except re.error:
        return False
    return any(compiled.search(tool) for tool in DSH_TOOLS)


# Whole-word Claude tool names, longest first so `MultiEdit` is matched before
# `Edit` would be. Used to translate names embedded in a REGEX matcher.
_EMBEDDED = sorted(
    list(TOOL_MAP) + list(APPROXIMATED),
    key=len,
    reverse=True,
)


def map_regex_matcher(pattern: str) -> tuple[str, list[str]]:
    """Translate Claude tool names appearing inside a regex matcher.

    A regex matcher is not a literal alternation, so it cannot be split on `|`
    and translated per-part -- but it can still NAME Claude tools. A pattern
    like `(Bash|Write)` or `Bash.*` is a regex by the bridge's discriminator,
    passes through untouched, and then matches nothing, because dsh's tools are
    `bash` and `write`. Passing every regex through unexamined is therefore its
    own silent-death path.

    Names are replaced on word boundaries only, so an MCP tool name such as
    `mcp__claude_ai_Gmail__create_draft` is untouched.
    """
    import re

    out = pattern
    notes: list[str] = []
    for claude_name in _EMBEDDED:
        dsh_name = TOOL_MAP.get(claude_name) or APPROXIMATED[claude_name]
        new, n = re.subn(rf"\b{re.escape(claude_name)}\b", dsh_name, out)
        if n:
            out = new
            notes.append(f"regex: {claude_name} -> {dsh_name} ({n} occurrence(s))")
    return out, notes


def map_matcher(matcher: str | None) -> tuple[str, str | None, list[str]]:
    """Translate one matcher pattern from Claude Code names to dsh names.

    Returns `(status, new_matcher, notes)` where status is one of:

        "match-all"   the bridge's absent/empty/`*` sentinel. `new_matcher` is
                      the original. The group fires for every subject and MUST
                      be emitted -- `UserPromptSubmit` and `Stop` groups always
                      land here, since those events ignore matchers entirely.
        "unchanged"   a regex matcher, passed through verbatim. MCP tool names
                      keep their `mcp__<server>__<tool>` shape in dsh and both
                      harnesses apply such a pattern through the same regex path.
        "translated"  a literal matcher rewritten to dsh tool names.
        "dead"        nothing in the pattern maps. `new_matcher` is None; the
                      group cannot fire under dsh and the caller must surface
                      it rather than emit it.

    The status is explicit because a `None` return previously meant BOTH "no
    matcher, fires for everything" and "nothing maps, fires for nothing" --
    opposite meanings behind one value. A caller that conflated them dropped
    every match-all group, which is precisely the silent-no-op this module
    exists to prevent.

    `notes` records every approximation, drop and passthrough so the generator
    reports them instead of hiding them.
    """
    if matcher in _MATCH_ALL:
        return "match-all", matcher, []

    assert matcher is not None  # narrowed by the sentinel check above
    if not is_claude_literal(matcher):
        # A regex matcher cannot be split on `|` and translated per-part, but
        # it can still NAME Claude tools -- `(Bash|Write)` and `Bash.*` are both
        # regexes by the bridge's discriminator. Passing every regex through
        # unexamined is its own silent-death path, so embedded tool names are
        # translated on word boundaries (which leaves `mcp__*` names alone).
        rewritten, notes = map_regex_matcher(matcher)
        # Ground-truth check. A regex naming only tools with no dsh counterpart
        # (`(NotebookEdit)`) rewrites to nothing, stays byte-identical, and would
        # otherwise be emitted as `unchanged` and escape --check entirely.
        if not selects_a_real_tool(rewritten):
            notes.append(
                f"regex {rewritten!r} matches no tool in dsh's catalog: "
                f"reclassified as dead"
            )
            return "dead", None, notes
        if rewritten != matcher:
            return "translated", rewritten, notes
        return "unchanged", matcher, ["regex matcher: no Claude tool names found, passed through"]

    mapped: list[str] = []
    notes: list[str] = []

    for part in matcher.split("|"):
        if part in TOOL_MAP:
            mapped.append(TOOL_MAP[part])
        elif part in APPROXIMATED:
            mapped.append(APPROXIMATED[part])
            notes.append(
                f"{part} -> {APPROXIMATED[part]} (APPROXIMATE: widens the "
                f"matcher, not an equivalent tool)"
            )
        elif part in UNMAPPED:
            notes.append(f"{part} dropped (no dsh tool)")
        else:
            # Unknown name: keep it, so a custom, MCP-provided or
            # future dsh tool still matches. Reported, never silent.
            mapped.append(part)
            notes.append(f"{part} kept verbatim (unrecognised)")

    if not mapped:
        notes.append("no dsh tool matches: these handlers would never fire")
        return "dead", None, notes

    # Preserve order, drop duplicates the approximation map can introduce
    # (e.g. `MultiEdit|apply_patch` both becoming `str_replace_editor`).
    seen: set[str] = set()
    unique: list[str] = []
    for name in mapped:
        if name not in seen:
            seen.add(name)
            unique.append(name)

    translated = "|".join(unique)

    # A degenerate input ("|", "||") splits into empty alternatives that are
    # each "unrecognised, kept verbatim", so `unique` becomes [""] and the join
    # is "". Empty string is one of the bridge's OWN match-all sentinels, so
    # emitting it would turn a matcher that should select nothing into one that
    # fires on every tool. That is the silent widening this module exists to
    # prevent, arriving through the success path instead of the failure path --
    # so the check belongs here, after the join, not on the input.
    if translated in _MATCH_ALL:
        notes.append(
            f"translation produced {translated!r}, a match-all sentinel: "
            f"reclassified as dead rather than emitted as fire-on-everything"
        )
        return "dead", None, notes

    # Same ground-truth check the regex path gets: every alternative may have
    # translated correctly and still name nothing dsh actually registers.
    if not selects_a_real_tool(translated):
        notes.append(
            f"{translated!r} matches no tool in dsh's catalog: "
            f"reclassified as dead"
        )
        return "dead", None, notes

    status = "translated" if translated != matcher else "unchanged"
    return status, translated, notes

#!/usr/bin/env python3
"""Generate a DeepSeek Harness (dsh) config patch from a live Claude Code setup.

Reads the host's Claude Code configuration and emits the two files dsh needs:

    generated/hooks-merged.json   the two settings scopes' `hooks` keys merged
                                  into one, every matcher translated to dsh
                                  tool names
    generated/deus-dsh.patch.yml  a dsh `- insert:` patch mounting the hook
                                  bridge, the MCP servers, the skill root and
                                  one subagent row per warden

Both outputs are gitignored: they are built from the host's own config and
embed personal hook commands, MCP server rows and warden bodies.

READ-ONLY with respect to ~/.claude and ~/.claude.json. Nothing here modifies
the live Claude Code setup; the port is additive and rolling back means not
launching dsh.

Usage:
    python3 integrations/dsh/generate_dsh_config.py [--out DIR] [--check]

    --check   exit non-zero if any matcher group would be dead under dsh
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from tool_name_map import map_matcher, map_tool_list  # noqa: E402

HOME = Path.home()

# This file lives at <repo>/integrations/dsh/, so the repo root is two levels
# up. Derived rather than hardcoded so the generator works from a clone at any
# path and from a linked worktree -- where a hardcoded `~/Deus` would read the
# primary checkout's settings while claiming to describe this tree.
REPO_ROOT = Path(__file__).resolve().parents[2]

# Claude Code hook sources. Both scopes are UNIONED, not layered: Claude Code
# runs user-scope and project-scope hooks additively, so neither overrides the
# other and every handler from both files is emitted. The dsh bridge accepts
# ONE `configPath` and performs no scope merge of its own, which is why the
# union has to happen here.
USER_SETTINGS = HOME / ".claude" / "settings.json"
PROJECT_SETTINGS = REPO_ROOT / ".claude" / "settings.json"
MCP_CONFIG = HOME / ".claude.json"
SKILL_ROOT = HOME / ".claude" / "skills"
AGENT_ROOT = HOME / ".claude" / "agents"

# Events the bridge implements. Config for any other event is ignored before
# group parsing, so emitting it would be silently inert.
SUPPORTED_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SubagentStart",
    "SubagentStop",
)

# Claude Code model aliases -> the reasoning effort/model hint carried on a
# dsh subagent row. dsh resolves the concrete model through its own provider
# route, so only the tier travels.
MODEL_TIERS = {"opus", "sonnet", "haiku"}

FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


# --------------------------------------------------------------------------
# readers
# --------------------------------------------------------------------------

def read_json(path: Path) -> dict:
    """Parse a JSON file, or return {} when it is absent."""
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split leading YAML frontmatter from a markdown body.

    Deliberately a flat scalar parser, not a YAML load: agent frontmatter here
    is flat `key: value`, and avoiding a YAML dependency keeps the generator
    runnable from a bare checkout. Nested blocks (`hooks:`) are skipped rather
    than half-parsed -- see `emit_subagents` for how that is reported.
    """
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line or line[0].isspace() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip().strip("'\"")
    return fields, text[match.end():]


# --------------------------------------------------------------------------
# emitters -- each is pure: inputs in, rows out, no shared mutable state
# --------------------------------------------------------------------------

def emit_hooks(user: dict, project: dict) -> tuple[dict, list[str]]:
    """Merge both scopes' hooks and translate every matcher.

    Returns `(merged_hooks, report_lines)`. A group whose matcher maps to
    nothing is DROPPED rather than emitted, because emitting it would
    reproduce the exact silent-no-op this port exists to avoid.
    """
    merged: dict[str, list] = {}
    report: list[str] = []
    dead = 0

    for scope, settings in (("user", user), ("deus", project)):
        for event, groups in (settings.get("hooks") or {}).items():
            if event not in SUPPORTED_EVENTS:
                for group in groups:
                    n = len(group.get("hooks", []))
                    report.append(f"SKIP  [{scope}] {event}: unsupported by the bridge ({n} handler(s))")
                continue

            for group in groups:
                handlers = group.get("hooks", [])
                if not handlers:
                    continue

                original = group.get("matcher")
                status, translated, notes = map_matcher(original)

                shown = original if original is not None else "<none>"
                label = f"[{scope}] {event} {shown!r} ({len(handlers)} handler(s))"

                if status == "dead":
                    dead += len(handlers)
                    report.append(f"DEAD  {label}: dropped, would never fire")
                elif status == "translated":
                    report.append(f"MAP   {label} -> {translated!r}")
                elif status == "match-all":
                    report.append(f"ALL   {label}: matcher-less event, fires unconditionally")
                else:
                    report.append(f"KEEP  {label}: unchanged")

                # Notes are emitted under their own group's label, never
                # trailing a neighbouring one.
                for note in notes:
                    report.append(f"        - {note}")

                if status == "dead":
                    continue

                new_group = dict(group)
                if original is not None:
                    new_group["matcher"] = translated
                merged.setdefault(event, []).append(new_group)

    report.append(f"TOTAL dead handlers after translation: {dead}")
    return merged, report


def emit_mcp_rows(mcp_config: dict) -> tuple[list[dict], list[str]]:
    """One `dsh-mcp-client` row per stdio MCP server.

    dsh supports stdio and streamable HTTP. Anything else is reported, never
    dropped in silence -- a silently skipped server is the jcode failure mode.
    """
    rows: list[dict] = []
    report: list[str] = []

    for name, spec in (mcp_config.get("mcpServers") or {}).items():
        transport = spec.get("type") or ("stdio" if "command" in spec else None)
        if transport != "stdio":
            report.append(f"SKIP  mcp {name}: transport {transport!r} not emitted")
            continue

        config: dict = {
            "serverName": name,
            "transport": "stdio",
            "command": spec["command"],
        }
        if spec.get("args"):
            config["args"] = spec["args"]
        if spec.get("env"):
            config["env"] = spec["env"]

        rows.append({"id": f"mcp-{name}", "name": "@deepseek-ai/dsh-mcp-client", "config": config})
        report.append(f"MCP   {name}: stdio")

    return rows, report


def emit_skill_row(root: Path) -> tuple[list[dict], list[str]]:
    """One `dsh-skill-filesystem` row pointed at the Claude Code skill root.

    `watchFollowSymlinks` defaults true, which matters because most entries
    under ~/.claude/skills are symlinks into other repositories.
    """
    if not root.is_dir():
        return [], [f"SKIP  skills: {root} is not a directory"]

    count = sum(1 for _ in root.glob("*/SKILL.md"))
    row = {
        "id": "skills-claude-code",
        "name": "@deepseek-ai/dsh-skill-filesystem",
        "config": {
            # The shipped profiles ALREADY mount a `dsh-skill-filesystem` row
            # under the default provider name `filesystem`. Registering a second
            # provider under that name is fatal at plugin load -- and it is
            # invisible to `--dump-config`, which composes the tree but never
            # instantiates it, so the duplicate row appears in a clean dump and
            # then refuses to boot.
            "providerName": "claude-code",
            # The sibling row owns the default project/user roots. This one adds
            # only the Claude Code skill directory, so the two do not rescan the
            # same trees and produce duplicate catalog entries.
            "includeDefaultRoots": False,
            "customSkillDirs": [str(root)],
            "watchFollowSymlinks": True,
        },
    }
    return [row], [f"SKILL {count} skill(s) from {root} (provider 'claude-code')"]


def emit_subagents(root: Path) -> tuple[list[dict], list[str]]:
    """One `dsh-tool-subagent` row per warden markdown file.

    Maps the Claude Code agent file onto dsh's subagent config:
        name:        -> toolName
        <body>       -> persona
        model:       -> agentOptions.model (tier; dsh resolves the route)
        tools:       -> toolFilter.allow
    """
    rows: list[dict] = []
    report: list[str] = []

    if not root.is_dir():
        return [], [f"SKIP  agents: {root} is not a directory"]

    seen_ids: set[str] = set()

    for path in sorted(root.glob("*.md")):
        raw = path.read_text(encoding="utf-8", errors="replace")
        fields, body = parse_frontmatter(raw)
        name = fields.get("name") or path.stem
        persona = body.strip()
        if not persona:
            report.append(f"SKIP  agent {name}: empty body, no persona to mount")
            continue

        config: dict = {
            "provider": "claude-code",
            "toolName": name,
            "persona": persona,
        }

        model = fields.get("model")
        if model in MODEL_TIERS:
            config["agentOptions"] = {"model": model}
        elif model:
            report.append(f"NOTE  agent {name}: unrecognised model {model!r}, left to the deployment route")

        tools = fields.get("tools")
        if tools and tools.lower() not in ("all tools", "*"):
            claude_names = [t.strip() for t in tools.split(",") if t.strip()]
            # An allow-list carries Claude Code tool names too, and needs the
            # same translation as a hook matcher. Untranslated, `Read, Grep,
            # Glob` matches none of dsh's `read`/`grep`/`glob` -- and an
            # allow-list matching nothing grants NOTHING, so the agent silently
            # loses every tool rather than gaining every tool.
            allow, notes = map_tool_list(claude_names)
            for note in notes:
                report.append(f"        - {name} tools: {note}")
            if allow:
                config["toolFilter"] = {"allow": allow}
            elif claude_names:
                report.append(
                    f"LOSS  agent {name}: tools {claude_names} map to no dsh tool; "
                    f"allow-list omitted so the agent keeps default tools "
                    f"rather than being granted none"
                )

        # `hooks:` in agent frontmatter is a Claude Code per-agent hook block.
        # dsh subagent rows carry no equivalent, so it is reported as a real
        # capability loss rather than dropped quietly. Scoped to the
        # frontmatter: a persona body may legitimately contain a `hooks:` line
        # (several of these wardens discuss hooks), and matching that would
        # report a loss that is not one.
        if "hooks" in fields:
            report.append(f"LOSS  agent {name}: per-agent `hooks:` block has no dsh equivalent")

        # Two agent files resolving to the same `name:` would emit duplicate
        # row ids, which dsh resolves by last-wins -- silently dropping one
        # warden. Report instead.
        row_id = f"subagent-{name}"
        if row_id in seen_ids:
            report.append(f"DUP   agent {name}: duplicate row id {row_id!r}, {path.name} skipped")
            continue
        seen_ids.add(row_id)

        rows.append({"id": row_id, "name": "@deepseek-ai/dsh-tool-subagent", "config": config})

    report.append(f"AGENT {len(rows)} subagent row(s) from {root}")
    return rows, report


# --------------------------------------------------------------------------
# writer
# --------------------------------------------------------------------------

def yaml_scalar(value) -> str:
    """Render a scalar for the patch file without a YAML dependency."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def yaml_block(value, indent: int) -> list[str]:
    """Render a nested value as YAML lines at `indent` spaces."""
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(yaml_block(val, indent + 2))
            else:
                lines.append(f"{pad}{key}: {yaml_scalar(val)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.extend(yaml_block(item, indent + 2))
            else:
                lines.append(f"{pad}- {yaml_scalar(item)}")
    return lines


def render_patch(rows: list[dict]) -> str:
    """Render the dsh `- insert:` patch.

    A bare row carrying an `id` is a REPLACE in dsh and errors when the id is
    absent from the composed tree; `- insert:` is what ADDS rows.
    """
    lines = [
        "# GENERATED by integrations/dsh/generate_dsh_config.py -- do not edit by hand.",
        "# Built from this host's live Claude Code config; gitignored deliberately.",
        "- insert:",
    ]
    for row in rows:
        # `id` goes through the same escaping as `name`. It is derived from an
        # agent's `name:` frontmatter, which is trusted config today and always
        # a plain slug -- but an unescaped interpolation beside an escaped one
        # is the inconsistency that stops being harmless the day a name grows a
        # colon.
        lines.append(f"    - id: {yaml_scalar(row['id'])}")
        lines.append(f"      name: {yaml_scalar(row['name'])}")
        if row.get("config"):
            lines.append("      config:")
            lines.extend(yaml_block(row["config"], 8))
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parent / "generated")
    parser.add_argument("--check", action="store_true", help="fail if any matcher group would be dead")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)

    merged_hooks, hook_report = emit_hooks(read_json(USER_SETTINGS), read_json(PROJECT_SETTINGS))
    hooks_path = args.out / "hooks-merged.json"
    hooks_path.write_text(json.dumps({"hooks": merged_hooks}, indent=2) + "\n", encoding="utf-8")

    rows: list[dict] = [{
        "id": "cc-hooks-bridge",
        "name": "@deepseek-ai/dsh-hooks-claude-code",
        # projectDir must be the SAME tree the settings were read from. The
        # bridge substitutes it for ${CLAUDE_PROJECT_DIR} and exports it to
        # every hook process, so pointing it at a different checkout would run
        # this tree's hooks against another tree's files.
        "config": {"configPath": str(hooks_path), "projectDir": str(REPO_ROOT)},
    }]

    report = list(hook_report)
    for emit, arg in ((emit_mcp_rows, read_json(MCP_CONFIG)), (emit_skill_row, SKILL_ROOT), (emit_subagents, AGENT_ROOT)):
        new_rows, new_report = emit(arg)
        rows.extend(new_rows)
        report.extend(new_report)

    patch_path = args.out / "deus-dsh.patch.yml"
    patch_path.write_text(render_patch(rows), encoding="utf-8")

    print("\n".join(report))
    print(f"\nwrote {hooks_path}")
    print(f"wrote {patch_path}  ({len(rows)} row(s))")

    print(
        "\nNOTE: `dsh --dump-config` proves the tree COMPOSES, not that it LOADS.\n"
        "      Drive a real boot to prove the plugins instantiate:\n"
        f"        dsh --profile headless --patch {patch_path} \"say only OK\"\n"
        "      Getting as far as MISSING_CREDENTIAL means the tree loaded."
    )

    dead = [line for line in report if line.startswith("DEAD")]
    if args.check and dead:
        print(f"\nFAIL: {len(dead)} matcher group(s) would be dead under dsh", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

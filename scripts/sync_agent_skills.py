#!/usr/bin/env python3
"""
Generate the local `.agents/` compatibility tree from `.claude/`.

`.claude/skills/`, `.claude/agents/`, and `.claude/wardens/` are the repo-owned
sources of truth. `.agents/` is a local Codex-facing projection with
deterministic markdown rewrites so that wardens, agent definitions, and skills
are discoverable from any backend (Claude Code or Codex).
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / ".claude" / "skills"
DEST_ROOT = PROJECT_ROOT / ".agents" / "skills"

_STRING_REPLACEMENTS: list[tuple[str, str]] = [
    ("Claude Agent SDK", "Codex Agent SDK"),
    ("Claude Code", "Codex"),
    ("Claude.ai", "Codex.ai"),
    ("CLAUDE-Archive.md", "Codex-Archive.md"),
    ("CLAUDE.md", "AGENTS.md"),
    ("/home/node/.claude/", "/home/node/.Codex/"),
    ("~/.claude/", "~/.Codex/"),
    (".claude/", ".Codex/"),
]

_REGEX_REPLACEMENTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bclaude\b"), "Codex"),
    (re.compile(r"\bClaude\b"), "Codex"),
]


def transform_markdown(text: str) -> str:
    """Rewrite Claude-specific wording into the local Codex compatibility form."""
    for old, new in _STRING_REPLACEMENTS:
        text = text.replace(old, new)
    for pattern, replacement in _REGEX_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


SYNC_DIRS: list[tuple[str, str]] = [
    ("skills", "skills"),
    ("agents", "agents"),
    ("wardens", "wardens"),
]


def render_agents_tree(project_root: Path) -> dict[str, bytes]:
    """Return the expected `.agents/` file map keyed by relative path."""
    rendered: dict[str, bytes] = {}
    found_any = False

    for src_name, dest_name in SYNC_DIRS:
        source_dir = project_root / ".claude" / src_name
        if not source_dir.exists():
            continue
        found_any = True
        for src in sorted(source_dir.rglob("*")):
            if not src.is_file():
                continue
            rel = f"{dest_name}/{src.relative_to(source_dir).as_posix()}"
            if src.suffix.lower() == ".md":
                rendered[rel] = transform_markdown(src.read_text()).encode("utf-8")
            else:
                rendered[rel] = src.read_bytes()

    if not found_any:
        raise FileNotFoundError(
            f"No source dirs found under {project_root / '.claude'}"
        )
    return rendered


def _read_tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    if not root.exists():
        return files
    for path in sorted(root.rglob("*")):
        if path.is_file():
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _skill_inventory(source_root: Path) -> tuple[list[str], list[Path]]:
    names: list[str] = []
    invalid: list[Path] = []
    if not source_root.exists():
        return names, invalid

    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.name.lower() != "skill.md":
            continue

        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            invalid.append(path)
            continue

        end = text.find("\n---", 4)
        if end == -1:
            invalid.append(path)
            continue

        found_name = False
        for line in text[4:end].splitlines():
            if line.startswith("name: "):
                names.append(line.removeprefix("name: ").strip())
                found_name = True
                break

        if not found_name:
            invalid.append(path)

    return sorted(set(names)), invalid


def check_skill_inventory(project_root: Path) -> int:
    """Verify every repo-owned skill is documented in AGENTS.md."""
    agents_path = project_root / "AGENTS.md"
    agents_text = agents_path.read_text(encoding="utf-8")
    names, invalid = _skill_inventory(SOURCE_ROOT)
    missing = [
        name
        for name in names
        if f"| `/{name}` |" not in agents_text
    ]

    if not invalid and not missing:
        print("Skill inventory documented.")
        return 0

    if invalid:
        print("INVALID SKILL FILES — missing YAML frontmatter or name:")
        for path in invalid:
            print(f"  {path.relative_to(project_root)}")

    if missing:
        print("SKILL INVENTORY DRIFT — AGENTS.md is missing skill command(s):")
        for name in missing:
            print(f"  /{name}")

    print(
        "\nFIX: add valid YAML frontmatter, then list each skill in "
        "AGENTS.md#commands-and-skills."
    )
    return 1


def check_exploration_protocol(project_root: Path) -> int:
    agents_dir = project_root / ".claude" / "agents"
    if not agents_dir.exists():
        return 0

    missing: list[Path] = []
    for path in sorted(agents_dir.rglob("*.md")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            continue
        end = text.find("\n---", 4)
        if end == -1:
            continue
        frontmatter = text[4:end]
        if "explores_code: true" not in frontmatter:
            continue
        body = text[end + 4 :]
        if "core-behavioral-rules.md \u00a7 Code Exploration" not in body:
            missing.append(path)

    if not missing:
        print("Exploration protocol enforced.")
        return 0

    print(
        "EXPLORATION PROTOCOL DRIFT \u2014 agents with explores_code: true "
        "missing protocol pointer:"
    )
    for path in missing:
        print(f"  {path.relative_to(project_root)}")
    print(
        "\nFIX: add the three-stage protocol pointer line referencing "
        "core-behavioral-rules.md \u00a7 Code Exploration."
    )
    return 1


def check_agents_tree(project_root: Path, dest_root: Path | None = None) -> int:
    """Verify that the local `.agents/` tree matches generated output."""
    inventory_status = check_skill_inventory(project_root)
    protocol_status = check_exploration_protocol(project_root)
    if protocol_status:
        inventory_status = max(inventory_status, protocol_status)
    dest_root = dest_root or (project_root / ".agents")
    if not dest_root.exists():
        print("SKIP: .agents/ not present (local generated Codex compatibility tree).")
        return inventory_status

    expected = render_agents_tree(project_root)
    actual = _read_tree(dest_root)

    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(
        rel for rel in set(expected) & set(actual) if expected[rel] != actual[rel]
    )

    if not missing and not extra and not changed:
        print(f"Agent tree synced ({len(expected)} files).")
        return inventory_status

    print("AGENT TREE DRIFT — `.agents/` no longer matches generated output.")
    if missing:
        print("Missing files:")
        for rel in missing[:20]:
            print(f"  {rel}")
    if extra:
        print("Extra files:")
        for rel in extra[:20]:
            print(f"  {rel}")
    if changed:
        print("Changed files:")
        for rel in changed[:20]:
            print(f"  {rel}")
    print("\nFIX: run `python3 scripts/sync_agent_skills.py` (or `npm run sync:agent-skills`).")
    return 1


def sync_agents_tree(project_root: Path, dest_root: Path | None = None) -> int:
    """Rewrite `.agents/` from `.claude/{skills,agents,wardens}/`."""
    dest_root = dest_root or (project_root / ".agents")
    rendered = render_agents_tree(project_root)

    for dest_name in [d for _, d in SYNC_DIRS]:
        subdir = dest_root / dest_name
        if subdir.exists():
            shutil.rmtree(subdir)

    dest_root.mkdir(parents=True, exist_ok=True)

    for rel, data in rendered.items():
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    dirs_synced = ", ".join(f".claude/{s}" for s, _ in SYNC_DIRS)
    print(
        f"Synced {len(rendered)} files from {dirs_synced} to "
        f"{dest_root.relative_to(project_root)}/."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the local `.agents/skills/` compatibility tree."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify `.agents/skills/` matches generated output instead of rewriting it.",
    )
    args = parser.parse_args()

    if args.check:
        return check_agents_tree(PROJECT_ROOT)
    return sync_agents_tree(PROJECT_ROOT)


if __name__ == "__main__":
    sys.exit(main())

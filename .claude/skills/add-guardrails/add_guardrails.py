#!/usr/bin/env python3
"""Installer for the /add-guardrails skill.

Copies a self-contained warden quality-gate kit into a target repository and
merges the hook wiring into its Claude Code settings — idempotently and without
clobbering anything the repo already has.

  python3 add_guardrails.py --target /path/to/repo --dry-run
  python3 add_guardrails.py --target /path/to/repo --modules design-logs,codegraph-first
  python3 add_guardrails.py --target /path/to/repo --update

Run with --dry-run first to preview every action, then re-run without it to
apply. Re-running is safe: existing settings hooks and CLAUDE.md content are
merged, never replaced, and a second run is a no-op.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
TEMPLATES = SKILL_DIR / "templates"

#: (template-relative path, target-relative path) copied verbatim. These files
#: are owned by this kit; an existing copy is left untouched unless --update.
BASELINE_FILES: list[tuple[str, str]] = [
    ("agents/plan-reviewer.md", ".claude/agents/plan-reviewer.md"),
    ("agents/code-reviewer.md", ".claude/agents/code-reviewer.md"),
    ("agents/verification-gate.md", ".claude/agents/verification-gate.md"),
    ("wardens/standards.md", ".claude/wardens/standards.md"),
    ("wardens/plan-review-rules.md", ".claude/wardens/plan-review-rules.md"),
    ("wardens/code-review-rules.md", ".claude/wardens/code-review-rules.md"),
    ("hooks/warden-gate.py", ".claude/hooks/warden-gate.py"),
    ("rules/dev-process.md", ".claude/rules/dev-process.md"),
]

#: Opt-in modules: name -> list of (template-relative, target-relative) files.
MODULES: dict[str, list[tuple[str, str]]] = {
    "design-logs": [("rules/design-logs.md", ".claude/rules/design-logs.md")],
    "codegraph-first": [("rules/codegraph-first.md", ".claude/rules/codegraph-first.md")],
}

SETTINGS_FRAGMENT = TEMPLATES / "settings.hooks.json"
CLAUDE_SECTION = TEMPLATES / "CLAUDE.guardrails.md"

CLAUDE_START = "<!-- guardrails:start -->"
CLAUDE_END = "<!-- guardrails:end -->"

#: Transient, per-developer gate state; must never be committed.
GITIGNORE_HEADER = "# warden gate runtime state (transient, per-developer)"
GITIGNORE_ENTRIES = [
    ".claude/.plan-reviewed",
    ".claude/.code-reviewed",
    ".claude/.verified",
    ".claude/.warden-verdicts.json",
]


# --- file copy -------------------------------------------------------------


def _copy(src_rel: str, dst_rel: str, target: Path, update: bool, dry_run: bool) -> str:
    src = TEMPLATES / src_rel
    dst = target / dst_rel
    new = src.read_text(encoding="utf-8")
    if dst.exists():
        current = dst.read_text(encoding="utf-8")
        if current == new:
            return f"unchanged  {dst_rel}"
        if not update:
            return f"differs    {dst_rel} (kept; pass --update to overwrite)"
        verb = "update"
    else:
        verb = "create"
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(new, encoding="utf-8")
    return f"{verb:<10} {dst_rel}"


# --- settings.json deep-merge ----------------------------------------------


def _merge_settings(target: Path, dry_run: bool) -> list[str]:
    fragment = json.loads(SETTINGS_FRAGMENT.read_text(encoding="utf-8"))
    settings_path = target / ".claude" / "settings.json"
    actions: list[str] = []

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return [f"ERROR      .claude/settings.json is not valid JSON; left untouched"]
        if not isinstance(existing, dict):
            return [f"ERROR      .claude/settings.json is not a JSON object; left untouched"]

    hooks = existing.setdefault("hooks", {})
    for event, groups in fragment["hooks"].items():
        event_groups = hooks.setdefault(event, [])
        for group in groups:
            matcher = group.get("matcher")
            target_group = next(
                (g for g in event_groups if g.get("matcher") == matcher), None
            )
            label = matcher or "(all)"
            if target_group is None:
                event_groups.append(json.loads(json.dumps(group)))
                actions.append(f"add        hooks.{event} [{label}]")
                continue
            bucket = target_group.get("hooks")
            if not isinstance(bucket, list):
                return [
                    f"ERROR      .claude/settings.json hooks.{event} [{label}] has a "
                    "non-list 'hooks'; left untouched"
                ]
            existing_cmds = {h.get("command") for h in bucket if isinstance(h, dict)}
            for hook in group.get("hooks", []):
                if hook.get("command") in existing_cmds:
                    continue
                bucket.append(json.loads(json.dumps(hook)))
                actions.append(f"add        hooks.{event} [{label}] command")

    if not actions:
        actions.append("unchanged  .claude/settings.json (hooks already wired)")
    elif not dry_run:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps(existing, indent=2) + "\n", encoding="utf-8"
        )
    return actions


# --- CLAUDE.md section merge ----------------------------------------------


def _merge_claude_md(target: Path, dry_run: bool) -> str:
    section = CLAUDE_SECTION.read_text(encoding="utf-8").strip()
    block = f"{CLAUDE_START}\n{section}\n{CLAUDE_END}\n"
    path = target / "CLAUDE.md"

    if not path.exists():
        if not dry_run:
            path.write_text(block, encoding="utf-8")
        return "create     CLAUDE.md (guardrails section)"

    content = path.read_text(encoding="utf-8")
    if CLAUDE_START in content and CLAUDE_END in content:
        start = content.index(CLAUDE_START)
        end = content.rindex(CLAUDE_END) + len(CLAUDE_END)
        updated = content[:start] + block.rstrip("\n") + content[end:]
        if updated == content:
            return "unchanged  CLAUDE.md (guardrails section current)"
        if not dry_run:
            path.write_text(updated, encoding="utf-8")
        return "update     CLAUDE.md (guardrails section)"

    updated = content.rstrip("\n") + "\n\n" + block
    if not dry_run:
        path.write_text(updated, encoding="utf-8")
    return "append     CLAUDE.md (guardrails section)"


# --- .gitignore sync -------------------------------------------------------


def _ensure_gitignore(target: Path, dry_run: bool) -> str:
    path = target / ".gitignore"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = set(existing.splitlines())
    missing = [entry for entry in GITIGNORE_ENTRIES if entry not in present]
    if not missing:
        return "unchanged  .gitignore (gate state already ignored)"

    chunk = ([GITIGNORE_HEADER] if GITIGNORE_HEADER not in present else []) + missing
    if not dry_run:
        body = existing.rstrip("\n")
        blocks = [b for b in (body, "\n".join(chunk)) if b]
        path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    verb = "create" if not existing else "update"
    return f"{verb:<10} .gitignore (+{len(missing)} gate-state entries)"


# --- driver ----------------------------------------------------------------


def install(target: Path, modules: list[str], update: bool, dry_run: bool) -> int:
    files = list(BASELINE_FILES)
    for name in modules:
        files.extend(MODULES[name])

    actions = [_copy(src, dst, target, update, dry_run) for src, dst in files]
    actions.extend(_merge_settings(target, dry_run))
    actions.append(_merge_claude_md(target, dry_run))
    actions.append(_ensure_gitignore(target, dry_run))

    header = "Planned actions (dry run):" if dry_run else "Applied:"
    print(header)
    for line in actions:
        print(f"  {line}")

    if any(line.startswith("ERROR") for line in actions):
        return 1
    if dry_run:
        print("\nRe-run without --dry-run to apply.")
    else:
        print(
            "\nGuardrails installed. Review the changes (git diff), then commit them "
            "so they travel with the repo. New sessions in this repo will enforce the gates."
        )
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", default=".", help="target repo root (default: cwd)")
    parser.add_argument(
        "--modules",
        default="",
        help=f"comma-separated opt-in modules ({', '.join(sorted(MODULES))})",
    )
    parser.add_argument(
        "--update", action="store_true", help="overwrite kit files that already differ"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print actions without writing"
    )
    parser.add_argument(
        "--list-modules", action="store_true", help="list opt-in modules and exit"
    )
    args = parser.parse_args(argv)

    if args.list_modules:
        for name in sorted(MODULES):
            print(name)
        return 0

    modules = [m.strip() for m in args.modules.split(",") if m.strip()]
    unknown = [m for m in modules if m not in MODULES]
    if unknown:
        print(
            f"unknown module(s): {', '.join(unknown)} "
            f"(available: {', '.join(sorted(MODULES))})",
            file=sys.stderr,
        )
        return 2

    target = Path(args.target).resolve()
    if not target.is_dir():
        print(f"target is not a directory: {target}", file=sys.stderr)
        return 2

    return install(target, modules, args.update, args.dry_run)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

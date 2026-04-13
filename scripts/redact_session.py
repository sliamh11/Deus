#!/usr/bin/env python3
"""
redact_session.py — Post-process a Deus session log to strip sensitive patterns.

Used by /compress in external project standard-memory mode. Runs after the
session log is written by the AI and before it is indexed. Idempotent: running
twice produces the same result.

Strips:
  - Fenced code blocks (```...```) — replaced with [redacted - standard memory level]
  - <internal>...</internal> tags that leaked through — replaced with [redacted - standard memory level]
  - Lines that look like "path/to/file:" followed by indented content blocks

Preserves:
  - YAML frontmatter (between the two --- markers at top of file)
  - ## Decisions Made section content
  - ## Key Learnings section content
  - tldr field in frontmatter
  - ## Pending Tasks section content
  - Any line that is already a redaction marker (idempotency)

Usage:
  python3 redact_session.py <session_log_path>
"""

import re
import sys
from pathlib import Path

REDACT_MARKER = "[redacted - standard memory level]"

# Sections that are safe to keep intact (case-insensitive heading match)
SAFE_SECTIONS = {
    "decisions made",
    "key learnings",
    "pending tasks",
}


def _is_safe_section(heading: str) -> bool:
    return heading.strip().lstrip("#").strip().lower() in SAFE_SECTIONS


def redact(text: str) -> str:
    """Apply all redaction passes to the text. Returns the redacted text."""

    # ── Pass 1: Strip <internal>…</internal> blocks (may span multiple lines) ──
    # These should never appear in session logs but handle any leak defensively.
    text = re.sub(
        r"<internal>.*?</internal>",
        REDACT_MARKER,
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # ── Pass 2: Process line by line, tracking frontmatter and safe sections ──
    lines = text.splitlines(keepends=True)
    result: list[str] = []

    in_frontmatter = False
    frontmatter_done = False
    frontmatter_fence_count = 0

    in_code_block = False
    code_fence_pattern = re.compile(r"^(`{3,}|~{3,})")

    in_safe_section = False
    heading_pattern = re.compile(r"^#{1,6}\s+(.*)")

    # File-path-followed-by-content pattern:
    # A line like "/path/to/file.py:", "./src/foo.ts:", or "src/foo.ts:" at
    # the start of a line followed by indented lines. We detect the trigger
    # line and skip until a blank line or next heading.
    file_path_trigger = re.compile(
        r"^[./]?[\w][\w./\-]*/[\w./\-]+(\.[\w]+)?:\s*$"  # e.g. "src/foo.ts:", "/abs/path.py:"
    )
    in_file_content_block = False

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.rstrip("\n")

        # ── Frontmatter detection ──
        if not frontmatter_done:
            if i == 0 and stripped == "---":
                in_frontmatter = True
                frontmatter_fence_count = 1
                result.append(line)
                i += 1
                continue
            elif in_frontmatter:
                if stripped == "---":
                    frontmatter_fence_count += 1
                    if frontmatter_fence_count == 2:
                        in_frontmatter = False
                        frontmatter_done = True
                result.append(line)
                i += 1
                continue

        # ── Already a redaction marker — preserve as-is (idempotency) ──
        if REDACT_MARKER in stripped:
            result.append(line)
            i += 1
            continue

        # ── Section heading detection ──
        heading_match = heading_pattern.match(stripped)
        if heading_match:
            section_name = heading_match.group(1)
            in_safe_section = _is_safe_section(section_name)
            in_file_content_block = False  # reset on any new heading
            result.append(line)
            i += 1
            continue

        # ── Safe section: pass through without any redaction ──
        if in_safe_section:
            result.append(line)
            i += 1
            continue

        # ── Code fence detection (outside safe sections and frontmatter) ──
        fence_match = code_fence_pattern.match(stripped)
        if fence_match:
            if not in_code_block:
                # Opening fence — consume until closing fence, replace whole block
                fence_char = fence_match.group(1)
                close_fence = re.compile(r"^" + re.escape(fence_char[0]) + r"{3,}\s*$")
                in_code_block = True
                # Scan ahead for the closing fence
                j = i + 1
                while j < len(lines) and not close_fence.match(lines[j].rstrip("\n")):
                    j += 1
                # j now points to closing fence or EOF
                result.append(REDACT_MARKER + "\n")
                i = j + 1  # skip past closing fence
                in_code_block = False
                in_file_content_block = False
            else:
                # Orphaned closing fence (shouldn't happen) — skip
                in_code_block = False
                i += 1
            continue

        # ── File path block detection ──
        if file_path_trigger.match(stripped):
            # Replace this line and consume following indented lines
            in_file_content_block = True
            result.append(REDACT_MARKER + "\n")
            i += 1
            # Skip indented continuation lines
            while i < len(lines):
                next_stripped = lines[i].rstrip("\n")
                if next_stripped == "" or next_stripped[0] in (" ", "\t"):
                    i += 1  # consume blank/indented lines
                else:
                    break
            in_file_content_block = False
            continue

        # ── Default: pass through ──
        result.append(line)
        i += 1

    return "".join(result)


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <session_log_path>", file=sys.stderr)
        sys.exit(1)

    log_path = Path(sys.argv[1])
    if not log_path.exists():
        print(f"Error: file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    original = log_path.read_text(encoding="utf-8")
    redacted = redact(original)

    if redacted == original:
        print(f"redact_session: no changes needed — {log_path.name}")
    else:
        # Backup original before irreversible redaction
        backup_path = log_path.with_suffix(".pre-redact.md")
        backup_path.write_text(original, encoding="utf-8")
        log_path.write_text(redacted, encoding="utf-8")
        # Count redacted sections for feedback
        count = redacted.count(REDACT_MARKER)
        print(
            f"redact_session: {count} section(s) redacted from {log_path.name} (backup: {backup_path.name})"
        )


if __name__ == "__main__":
    main()

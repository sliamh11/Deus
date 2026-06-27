#!/usr/bin/env python3
"""SessionStart hook: inject persistent working standards.

Scans auto-memory atoms for kind=standard, formats them as condensed
one-liners, and emits as additionalContext with directive framing.
Cached by content-hash signature (SHA-256 over all .md files in the
auto-memory dir) so frontmatter edits invalidate the cache. Directory
mtime is unreliable because POSIX `stat.st_mtime` does not update on
file-content changes of existing directory members on macOS or ext4.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from auto_memory_dir import resolve_auto_memory_dir  # noqa: E402


def _default_auto_mem_dir() -> Path:
    # Must match memory_tree + memory_query — see auto_memory_dir.py (LIA-341).
    return resolve_auto_memory_dir()


AUTO_MEM_DIR = _default_auto_mem_dir()

CACHE_PATH = Path(os.environ.get(
    "DEUS_STANDARDS_CACHE",
    os.path.expanduser("~/.deus/standards_pack_cache.json"),
))

# Cap on the total tokens of `kind: standard` atom one-liners injected as
# always-on context at SessionStart. On overrun, atoms are dropped in
# directory-sort order and a stderr WARN is emitted — silent drops have
# historically lost non-negotiable methodology rules.
#
# Not to be confused with `memory_tree.py:ROOT_TOKEN_BUDGET` — that caps
# MEMORY_TREE.md size for the navigation root, a different system.
TOKEN_BUDGET = int(os.environ.get("DEUS_STANDARDS_TOKEN_BUDGET", "1200"))

_FM_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n", re.DOTALL)


def _parse_kind(content: str) -> str | None:
    m = _FM_RE.match(content)
    if not m:
        return None
    km = re.search(r"^kind:\s*(.+)$", m.group(1), re.MULTILINE)
    return km.group(1).strip() if km else None


# Priority is declarative. `priority: high` requires a citation to
# .claude/rules/core-behavioral-rules.md per docs/decisions/standards-pack-priority.md.
_PRIORITY_RANK = {"high": 0, "med": 1, "low": 2}


def _parse_priority(content: str, filename: str = "") -> int:
    """Return priority rank: 0=high, 1=med (default), 2=low.

    Unknown values emit a stderr WARN naming the file and default to med.
    Atoms without a `priority:` field behave as med (rank 1) — the contract
    is "explicit declaration, otherwise medium."
    """
    m = _FM_RE.match(content)
    if not m:
        return _PRIORITY_RANK["med"]
    pm = re.search(r"^priority:\s*(.+)$", m.group(1), re.MULTILINE)
    if not pm:
        return _PRIORITY_RANK["med"]
    val = pm.group(1).strip().lower()
    if val in _PRIORITY_RANK:
        return _PRIORITY_RANK[val]
    print(
        f"[standards_pack] WARN: unknown priority '{val}' in {filename} "
        "— defaulting to 'med'. Expected: high | med | low.",
        file=sys.stderr,
    )
    return _PRIORITY_RANK["med"]


def _parse_name_desc(content: str) -> tuple[str, str]:
    m = _FM_RE.match(content)
    if not m:
        return ("", "")
    fm = m.group(1)
    name = ""
    desc = ""
    nm = re.search(r"^name:\s*(.+)$", fm, re.MULTILINE)
    if nm:
        name = nm.group(1).strip()
    dm = re.search(r"^description:\s*>?\s*\n?\s*(.+?)(?=\n\S|\n---|\Z)", fm, re.MULTILINE | re.DOTALL)
    if dm:
        desc = re.sub(r"\n\s+", " ", dm.group(1)).strip()
    return (name, desc)


def _token_estimate(text: str) -> int:
    return int(len(text.split()) * 1.3)


def _content_signature(d: Path) -> str:
    """SHA-256 over every .md file's name + bytes, in sorted order.

    Cost: ~3-5ms over ~140 atom files at ~5KB avg on Apple Silicon.
    Rationale for hashing content (vs. cheaper mtime) is in the module
    docstring.
    """
    h = hashlib.sha256()
    for f in sorted(d.glob("*.md")):
        h.update(f.name.encode("utf-8"))
        h.update(b"\x00")
        try:
            h.update(f.read_bytes())
        except OSError:
            # File vanished mid-scan — skip silently. Next call will
            # see a consistent post-vanish state and rebuild from that.
            continue
        h.update(b"\x00")
    return h.hexdigest()


def load_standards(auto_mem_dir: Path | None = None, token_budget: int = TOKEN_BUDGET) -> str:
    """Load kind=standard atoms as condensed one-liners within token budget."""
    d = auto_mem_dir or AUTO_MEM_DIR
    if not d.is_dir():
        # Fail-loud to stderr (never stdout — would pollute the JSON hook output).
        # Helps catch the silent-empty-pack failure mode that masked PR #380's gap.
        print(
            f"[standards_pack] WARN: auto-memory dir not found: {d}. "
            "Standards pack will be empty. Check DEUS_AUTO_MEMORY_DIR or CLAUDE_PROJECT_DIR.",
            file=sys.stderr,
        )
        return ""

    sig = _content_signature(d)
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text())
            # Pre-M0.5 caches stored "mtime" instead of "signature"; the
            # missing-key compare returns None != sig and rebuilds cleanly.
            if cached.get("signature") == sig and cached.get("budget") == token_budget:
                return cached.get("context", "")
        except (json.JSONDecodeError, OSError):
            pass

    lines: list[str] = []
    total_tokens = 0

    # Tuple shape: (priority_rank, filename, name, desc).
    # Natural tuple sort orders by (priority_rank, filename) — name/desc are
    # never compared when rank or filename differ. Priority sort puts `high`
    # atoms first; alphabetical filename is the stable tiebreaker within tier.
    atoms: list[tuple[int, str, str, str]] = []
    for f in sorted(d.glob("*.md")):
        content = f.read_text(encoding="utf-8", errors="replace")
        kind = _parse_kind(content)
        if kind != "standard":
            continue
        name, desc = _parse_name_desc(content)
        if not name:
            continue
        priority_rank = _parse_priority(content, filename=f.name)
        atoms.append((priority_rank, f.name, name, desc))

    atoms.sort()

    dropped: list[str] = []
    dropped_high: list[str] = []
    dropped_tokens = 0
    truncated_at: int | None = None
    for idx, (_priority_rank, _filename, name, desc) in enumerate(atoms):
        oneliner = f"- {name}: {desc}" if desc else f"- {name}"
        cost = _token_estimate(oneliner)
        if total_tokens + cost > token_budget:
            truncated_at = idx
            break
        lines.append(oneliner)
        total_tokens += cost

    if truncated_at is not None:
        # Collect every atom that didn't make it in (not just the first overrun).
        # Second pass over the already-built `atoms` list keeps the include set
        # exactly what the first-fit loop produced — we only iterate past the
        # cutoff to report names.
        for priority_rank, _filename, name, desc in atoms[truncated_at:]:
            oneliner = f"- {name}: {desc}" if desc else f"- {name}"
            dropped.append(name)
            dropped_tokens += _token_estimate(oneliner)
            if priority_rank == _PRIORITY_RANK["high"]:
                dropped_high.append(name)
        # Fail-loud: silent drops previously hid the loss of non-negotiable
        # methodology rules (e.g. `feedback_warden_loop`). Emit to stderr so
        # the hook output stays JSON-clean on stdout.
        print(
            f"[standards_pack] WARN: budget exceeded — dropped {len(dropped)} atom(s) "
            f"totalling {dropped_tokens} tokens: {', '.join(dropped)}. "
            f"Raise DEUS_STANDARDS_TOKEN_BUDGET (current: {token_budget}) or "
            f"reduce atom count.",
            file=sys.stderr,
        )
        # Additive CRITICAL emit when high-priority atoms are dropped.
        # See docs/decisions/standards-pack-priority.md — inclusion is NOT
        # unconditionally guaranteed; this is the loudness contract on overflow.
        if dropped_high:
            print(
                f"[standards_pack] CRITICAL: priority=high atom(s) dropped — "
                f"non-negotiable rules lost! Names: {', '.join(dropped_high)}",
                file=sys.stderr,
            )

    if not lines:
        # Fail-loud: dir exists but no kind=standard atoms found. Likely classification gap.
        print(
            f"[standards_pack] WARN: 0 kind=standard atoms in {d} "
            f"(scanned {sum(1 for _ in d.glob('*.md'))} .md files). "
            "Standards pack will be empty.",
            file=sys.stderr,
        )
        return ""

    context = (
        "=== Working Standards (apply to ALL actions this session) ===\n"
        "These are verified methodology rules. Follow them reflexively.\n"
        + "\n".join(lines)
        + "\n=== End Working Standards ==="
    )

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({
            "signature": sig,
            "budget": token_budget,
            "context": context,
            "atom_count": len(lines),
            "tokens": total_tokens,
            "dropped": dropped,
            "dropped_tokens": dropped_tokens,
            "dropped_high": dropped_high,
        }))
    except OSError:
        pass

    return context


def main() -> None:
    try:
        sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        pass

    context = load_standards()
    if not context:
        return

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"[deus standards-pack] {e}\n")
    sys.exit(0)

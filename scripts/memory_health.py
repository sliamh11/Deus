#!/usr/bin/env python3
"""Session-start memory-system health check (#769).

Cheap, embedding-free probes that catch CATASTROPHIC memory degradation at
session start — the failure mode where retrieval silently dies and nothing
alarms: vault unmounted/unwritable, the tree DB gone, the navigation root lost,
or the graph wiped of edges. This is the missing alarm behind the "memory recall
died for 4 days after a vault migration" incident (every component had failed
*gracefully*, so nothing surfaced).

Deliberately NOT a quality gate. Routine hygiene (a few missing descriptions, a
single stray unreachable node) stays SILENT so the check never cries wolf — only
catastrophic, recall-killing states escalate to a loud banner.

Platform: macOS/Linux. The memory subsystem (sqlite_vec + Ollama) is POSIX-only;
`memory_query.py` fails fast on Windows, so the writability probe is not exercised
there.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = Path("~/.deus/memory_tree.db").expanduser()
_PROBE_NAME = ".deus-health-probe"
# Derived from this file's location so remediation commands are correct
# regardless of where the repo is cloned (user-agnostic).
_MEMORY_TREE = Path(__file__).resolve().parent / "memory_tree.py"


def _probe_vault_writable(vault: Path | None) -> tuple[bool, str]:
    """(ok, reason). A touch + unlink confirms the volume is mounted AND
    writable — `is_dir()` alone misses the macOS Full-Disk-Access case where the
    vault reads fine but writes (session logs) silently fail."""
    if vault is None:
        return False, "vault path is not configured"
    if not vault.is_dir():
        return False, f"vault not mounted at {vault}"
    probe = vault / _PROBE_NAME
    writable = True
    try:
        probe.write_text("ok", encoding="utf-8")
    except OSError:
        writable = False
    finally:
        # Always attempt cleanup, even if the write half-succeeded then raced.
        try:
            probe.unlink()
        except OSError:
            pass
    if not writable:
        return False, f"vault not writable at {vault} (grant Full Disk Access?)"
    return True, ""


def _tree_health(db_path: Path) -> list[str]:
    """Catastrophic-only signals from a direct, read-only DB read (no embeddings,
    no sqlite_vec, no memory_tree import). Returns a list of degradation lines;
    empty when healthy."""
    if not db_path.exists():
        return [f"memory tree DB missing at {db_path}"]
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return [f"memory tree DB unreadable at {db_path}"]
    try:
        nodes = con.execute(
            "SELECT COUNT(*) FROM nodes "
            "WHERE orphaned_at IS NULL AND path NOT LIKE 'auto-memory/%'"
        ).fetchone()[0]
        edges = con.execute(
            "SELECT COUNT(*) FROM edges WHERE kind = 'child' AND expired_at IS NULL"
        ).fetchone()[0]
        root = con.execute(
            "SELECT 1 FROM nodes WHERE path = 'MEMORY_TREE.md' AND orphaned_at IS NULL"
        ).fetchone()
    except sqlite3.Error:
        # A schema/probe error must never block startup — treat as "can't tell".
        return []
    finally:
        con.close()

    if nodes == 0:
        return ["memory tree is empty (0 active nodes)"]
    lines: list[str] = []
    if root is None:
        lines.append("navigation root MEMORY_TREE.md is missing from the tree")
    if edges == 0:
        lines.append(f"memory graph has 0 edges across {nodes} nodes (ranking dead)")
    return lines


def assess_memory_health(
    vault: Path | None, db_path: Path | str | None = None
) -> tuple[bool, str, list[str]]:
    """Assess memory-system health with cheap probes.

    Returns (ok, severity, lines):
      - (True, "ok", [])            healthy — caller emits nothing
      - (False, "degraded", [...])  catastrophic — caller surfaces a loud banner
    """
    db = Path(db_path).expanduser() if db_path is not None else DEFAULT_DB_PATH
    lines: list[str] = []

    vault_ok, reason = _probe_vault_writable(vault)
    if not vault_ok:
        lines.append(reason)

    lines.extend(_tree_health(db))

    if lines:
        return False, "degraded", lines
    return True, "ok", []


def render_degraded_section(lines: list[str]) -> str:
    """Render the loud session-start banner with targeted remediation."""
    out = [
        "=== MEMORY SYSTEM DEGRADED ===",
        "Memory recall may be silently failing. Detected:",
    ]
    out.extend(f"  - {line}" for line in lines)
    blob = " ".join(lines).lower()
    remedies: list[str] = []
    if "not writable" in blob or "not mounted" in blob or "not configured" in blob:
        remedies.append(
            "vault unavailable: remount / grant Full Disk Access, then restart the session"
        )
    if "root" in blob:
        remedies.append(f"lost root: python3 {_MEMORY_TREE} scaffold-root --force --reindex")
    if "db missing" in blob or "empty" in blob or "0 edges" in blob or "unreadable" in blob:
        remedies.append(f"rebuild tree: python3 {_MEMORY_TREE} build")
    remedies.append(f"verify: python3 {_MEMORY_TREE} check")
    out.append("")
    out.append("Remediation:")
    out.extend(f"  - {r}" for r in remedies)
    return "\n".join(out)

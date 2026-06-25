"""Single source of truth for the auto-memory directory.

The auto-memory population (memory_indexer-promoted atoms, feedback, and
procedures) lives in the Claude project memory dir. ``memory_indexer`` writes
here, ``memory_tree`` indexes from here, ``memory_query`` reads node content
from here, and ``standards_pack`` loads standard atoms from here. They MUST
resolve the SAME directory or a node indexes under an ``auto-memory/`` namespace
path that recall cannot read back (LIA-341) — the live symptom was
``memory_query`` defaulting to a non-existent ``~/.deus/auto-memory`` and
returning ``None`` for every promoted feedback node.

Kept dependency-free (os + pathlib only) so SessionStart-critical importers like
``standards_pack`` add no import weight.
"""

from __future__ import annotations

import os
from pathlib import Path

EXTERNAL_DIR_ENV = "DEUS_AUTO_MEMORY_DIR"


def _encode_project_dir(project_dir: str) -> str:
    """Encode a project path the way Claude Code names its project memory dir:
    path separators become dashes, with a leading dash.

    Windows ``CLAUDE_PROJECT_DIR`` uses backslashes, so collapse those first —
    ``standards_pack``'s original encoding only replaced ``/`` and would leave a
    Windows path with raw backslashes, silently missing the directory.
    """
    encoded = project_dir.replace("\\", "-").replace("/", "-")
    if not encoded.startswith("-"):
        encoded = "-" + encoded
    return encoded


def resolve_auto_memory_dir() -> Path:
    """Resolve the canonical auto-memory directory.

    Priority: explicit ``DEUS_AUTO_MEMORY_DIR`` override -> the
    ``CLAUDE_PROJECT_DIR``-derived project memory dir -> this repo's project
    memory dir (derived from the module's location) -> ``~/.deus/auto-memory``
    fallback. Mirrors ``memory_indexer.py``'s promotion target. The two derived
    steps only match when the candidate directory exists; otherwise resolution
    falls through to the next step.
    """
    env = os.environ.get(EXTERNAL_DIR_ENV)
    if env:
        return Path(env).expanduser()

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidate = Path(
            os.path.expanduser(
                f"~/.claude/projects/{_encode_project_dir(project_dir)}/memory"
            )
        )
        if candidate.is_dir():
            return candidate

    repo_root = Path(__file__).resolve().parent.parent
    legacy = Path(
        os.path.expanduser(
            f"~/.claude/projects/{_encode_project_dir(repo_root.as_posix())}/memory"
        )
    )
    if legacy.is_dir():
        return legacy

    return Path(os.path.expanduser("~/.deus/auto-memory"))

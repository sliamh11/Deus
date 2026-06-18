"""Verdict-store I/O capsule (LIA-306).

Reads/writes the per-worktree ``.warden-verdicts.json`` plus the global
``.warden-log`` audit trail and ``~/.claude/.warden-bypass-log``. This is the
store the code-review + verification gates decide on (not the marker files).

Unlike the pure leaf capsules (``globs``, ``command_parse``), these functions are
NOT zero-coupling: they call entry-module helpers that tests monkeypatch
(``_claude_marker_dir``, ``_git``) plus non-patched entry helpers
(``_marker_dir_for_worktree``, ``_write_atomic``, ``_debug``) and the
``MARKER_NAMES`` dispatch table. To keep those monkeypatches effective WITHOUT
re-importing the ~4000-line entry on the hot hook path (the entry runs as
``__main__`` at runtime, so ``import codex_warden_hooks`` would re-parse it), the
entry injects itself via :func:`bind_entry`; every entry-owned reference is then
resolved through the live (possibly monkeypatched) module at CALL time. Resolution
is deferred to call time, so helpers defined later than the bind site are fine.

Intra-capsule calls stay direct; only the 6 distinct entry-owned symbols
(``_claude_marker_dir``, ``_marker_dir_for_worktree``, ``_git``,
``_write_atomic``, ``_debug``, ``MARKER_NAMES`` — 9 call sites) go through
``_entry.``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

#: The live entry module (``codex_warden_hooks``), injected by :func:`bind_entry`
#: at entry-module import time. Entry-owned helpers are resolved through this so
#: test monkeypatches on the entry module are honored at call time.
_entry: Any = None


def bind_entry(mod: Any) -> None:
    """Bind the entry module so late-resolved helpers honor test monkeypatches.

    Called once by ``codex_warden_hooks`` immediately after it imports this
    capsule, with ``sys.modules[__name__]`` (the live module object). Storing the
    reference (not the individual functions) means ``monkeypatch.setattr(h,
    "_claude_marker_dir", ...)`` is seen here, and there is no re-import cost on
    the hot hook path.
    """
    global _entry
    if mod is None:
        # Fail fast on mis-wiring: every capsule function dereferences ``_entry``,
        # so a None bind would surface only later as an opaque AttributeError.
        raise RuntimeError("verdict_store.bind_entry() requires the live entry module, got None")
    _entry = mod


def _verdicts_path(repo_root: Path) -> Path:
    # Per-worktree: the code-review + verification gates decide on this store
    # (not the marker files), so it must be isolated alongside the markers.
    # Main repo resolves to the flat .claude/.warden-verdicts.json (back-compat).
    return _entry._claude_marker_dir(repo_root) / ".warden-verdicts.json"


def _verdicts_path_for_worktree(repo_root: Path, worktree_root: Path) -> Path:
    # Deterministic verdict store for an EXPLICIT worktree (the admin-merge
    # standing gate resolves the cwd worktree itself rather than relying on
    # _current_worktree()'s os.getcwd() derivation). Mirrors _verdicts_path.
    return _entry._marker_dir_for_worktree(repo_root, worktree_root) / ".warden-verdicts.json"


def _audit_log_path(repo_root: Path) -> Path:
    # Deliberately GLOBAL (flat), not per-worktree: this is an append-only audit
    # trail that aggregates verdicts across every worktree. Do not namespace it.
    return repo_root / ".claude" / ".warden-log"


def _bypass_log_path() -> Path:
    override = os.environ.get("DEUS_WARDEN_BYPASS_LOG")
    if override:
        return Path(override)
    return Path.home() / ".claude" / ".warden-bypass-log"


def _write_bypass_log(
    warden: str,
    verdict: str,
    session_type: str,
    reason: str,
    cwd: Path,
) -> None:
    try:
        diff_stats = _entry._git(cwd, "diff", "--stat", "HEAD")
        entry = {
            "timestamp": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "warden": warden,
            "verdict": verdict,
            "session_type": session_type,
            "reason": reason,
            "cwd": str(cwd),
            "diff_stats": diff_stats,
        }
        path = _bypass_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        _entry._debug("bypass log write failed")


def _read_verdicts_at(path: Path) -> dict[str, Any]:
    """Read a .warden-verdicts.json at an EXPLICIT path (no cwd derivation)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_verdicts(repo_root: Path) -> dict[str, Any]:
    return _read_verdicts_at(_verdicts_path(repo_root))


def _read_verdict(marker_name: str, repo_root: Path) -> str | None:
    """Return the verdict string for *marker_name* from .warden-verdicts.json.

    Maps the marker name (e.g. ``"code-reviewed"``) to the warden key used in
    the JSON (e.g. ``"code-reviewer"``) via ``MARKER_NAMES``.  Returns ``None``
    if the file is absent, malformed, or the entry is missing.
    """
    warden = _entry.MARKER_NAMES.get(marker_name)
    if not warden:
        return None
    data = _read_verdicts(repo_root)
    entry = data.get(warden)
    if not isinstance(entry, dict):
        return None
    v = entry.get("verdict")
    return v if isinstance(v, str) else None


def _clear_verdict(marker_name: str, repo_root: Path) -> None:
    """Remove the *marker_name* entry from .warden-verdicts.json.

    Maps the marker name to the warden key via ``MARKER_NAMES``.  Silently
    skips if the file is absent or the key is not present.
    """
    warden = _entry.MARKER_NAMES.get(marker_name)
    if not warden:
        return
    path = _verdicts_path(repo_root)
    data = _read_verdicts(repo_root)
    if warden not in data:
        return
    del data[warden]
    try:
        _entry._write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError:
        _entry._debug(f"_clear_verdict: failed to write {path}")


def _write_verdict(repo_root: Path, warden: str, verdict: str, reason: str, source: str = "manual") -> None:
    path = _verdicts_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_verdicts(repo_root)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data[warden] = {"verdict": verdict, "ts": stamp, "reason": reason, "source": source}
    _entry._write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    log = _audit_log_path(repo_root)
    safe_reason = reason.replace("|", "/").replace("\n", " ").strip()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} | {warden:<15} | {verdict:<7} | {safe_reason}\n")


def _last_verdict(repo_root: Path, warden: str) -> str | None:
    data = _read_verdicts(repo_root)
    entry = data.get(warden)
    if isinstance(entry, dict):
        v = entry.get("verdict")
        return v if isinstance(v, str) else None
    return None


def _last_verdict_is_blocking(repo_root: Path, warden: str) -> bool:
    v = _last_verdict(repo_root, warden)
    return v in ("REVISE", "BLOCK")


def record_script_verdict(
    repo_root: Path, store_key: str, verdict: str, reason: str, source: str = "script",
) -> None:
    """Record a model-backend verdict (SHIP/REVISE/BLOCK/COULD_NOT_RUN) under ``store_key``
    (the ``<role>@<backend>`` warden key). Unlike ``mark_warden`` (human CLI, SHIP/TRIVIAL
    only), a script records the real verdict — COULD_NOT_RUN is written verbatim so the
    audit log distinguishes an infra failure from a genuine SHIP."""
    _write_verdict(repo_root, store_key, verdict, reason, source=source)

"""Session-scoped dedup keys + seen-store for memory injections (LIA-355).

53% of memory-hook injections are within-session duplicates — the same vault
files re-injected turn after turn. This module provides the content-hash key
and the per-session seen-store used by ``memory_query.recall(dedup_store=...)``.

Data-integrity contract: dedup suppresses ONLY exact already-shown content —
the key binds path + body hash, so a changed file re-injects; callers must
persist keys only for blocks that fully survived truncation into the final
emitted context (mark-only-what-survives). Everything here fails open: a
missing/corrupt store means "nothing seen", a failed save is silent.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from pathlib import Path

# Same sanitization as session_concepts._concepts_path — the established
# per-session tempdir-state precedent.
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")

_STORE_PREFIX = ".deus-memseen-"
_EVICT_AGE_SECONDS = 7 * 86400


def block_key(path: str, body: str) -> str:
    """Content-hash dedup key: same path with CHANGED content gets a new key."""
    digest = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{path}:{digest}"


def store_path_for_session(session_id: str) -> Path:
    safe_id = _SAFE_ID_RE.sub("", session_id) or "unknown"
    return Path(tempfile.gettempdir()) / f"{_STORE_PREFIX}{safe_id}.json"


def load_seen(store_path: Path) -> set[str]:
    """Seen keys from disk; missing or corrupt store fails open to empty."""
    try:
        data = json.loads(Path(store_path).read_text(encoding="utf-8"))
        keys = data.get("keys", [])
        return {k for k in keys if isinstance(k, str)}
    except (OSError, ValueError, AttributeError):
        return set()


def save_seen(store_path: Path, keys: set[str]) -> None:
    """Best-effort persist; also evicts stale sibling stores (first real
    cross-session cleanup for this family — .deus-concepts-* is untouched)."""
    try:
        Path(store_path).write_text(
            json.dumps({"keys": sorted(keys)}), encoding="utf-8"
        )
    except OSError:
        return
    _evict_old_stores()


def _evict_old_stores() -> None:
    cutoff = time.time() - _EVICT_AGE_SECONDS
    try:
        for f in Path(tempfile.gettempdir()).glob(f"{_STORE_PREFIX}*.json"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except OSError:
        pass

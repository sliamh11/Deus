"""Verdict-store I/O capsule (LIA-306).

Reads/writes the per-worktree ``.warden-verdicts.json`` plus the global
``.warden-log`` audit trail and ``~/.claude/.warden-bypass-log``. This is the
store the code-review + verification gates decide on (not the marker files).

Unlike the pure leaf capsules (``globs``, ``command_parse``), these functions are
NOT zero-coupling: they call entry-module helpers that tests monkeypatch
(``_claude_marker_dir``, ``_git``) plus non-patched entry helpers
(``_marker_dir_for_worktree``, ``_write_atomic``, ``_debug``, ``_resolve_verdict_worktree``)
and the ``MARKER_NAMES`` dispatch table. To keep those monkeypatches effective WITHOUT
re-importing the ~4000-line entry on the hot hook path (the entry runs as
``__main__`` at runtime, so ``import codex_warden_hooks`` would re-parse it), the
entry injects itself via :func:`bind_entry`; every entry-owned reference is then
resolved through the live (possibly monkeypatched) module at CALL time. Resolution
is deferred to call time, so helpers defined later than the bind site are fine.

Intra-capsule calls stay direct; only the 7 distinct entry-owned symbols
(``_claude_marker_dir``, ``_marker_dir_for_worktree``, ``_resolve_verdict_worktree``,
``_git``, ``_write_atomic``, ``_debug``, ``MARKER_NAMES``) go through ``_entry.``.

LIA-382 (verdict staleness): SHIP/TRIVIAL entries carry a ``head_sha``/``diff_hash``
fingerprint of the worktree's code state at write time (see ``_compute_state_fingerprint``
and ``_write_verdict``). Gate reads (``_read_verdict``/``_last_verdict``) treat a SHIP/TRIVIAL
entry whose fingerprint no longer matches the worktree's CURRENT state as absent, via
``_fresh_entry``. REVISE/BLOCK/COULD_NOT_RUN entries are NEVER filtered by staleness — see
``_fresh_entry``'s docstring for why (the short version: hiding a stale REVISE would defeat
the post-REVISE TRIVIAL-bypass guard in ``mark_warden``). Note the mechanism's actual coverage
is narrower than "closes cross-worktree misroutes" — see the plan's "Scope correction" for why
a misrouted verdict's fingerprint is self-consistent with its (wrong) destination bucket.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable

#: Verdict values eligible for staleness filtering. Every other value (REVISE, BLOCK,
#: COULD_NOT_RUN, and any future value) always passes through _fresh_entry unfiltered —
#: staleness protection exists to stop an OLD APPROVAL from being trusted against NEW
#: code, never to make an old REJECTION disappear (core-behavioral-rules.md's "REVISE...
#: no exceptions" depends on REVISE staying visible regardless of subsequent edits).
_STALENESS_ELIGIBLE_VERDICTS = frozenset({"SHIP", "TRIVIAL"})

# OS-SPECIFIC (flagged per core-rules): fcntl is POSIX-only. The warden machinery
# runs dev-host-only (macOS/Linux); on Windows fcntl is absent and the verdict-store
# lock degrades to a NO-OP, which is correct because no concurrent marks occur there.
try:
    import fcntl
except ImportError:  # pragma: no cover - Windows; warden never runs marks there
    fcntl = None  # type: ignore[assignment]

#: Sentinel distinguishing "key was absent" from a real value in locked mutations.
_VERDICT_UNSET: Any = object()

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


def _compute_state_fingerprint(worktree: Path) -> tuple[str | None, str | None]:
    """Fingerprint *worktree*'s current code state: ``(head_sha, diff_hash)``.

    ``head_sha`` is the worktree's HEAD commit. ``diff_hash`` is a sha256 of
    ``git diff HEAD`` (tracked-file content, staged + unstaged) combined with the
    sorted list of untracked file paths from ``git status --porcelain
    --untracked-files=normal`` (existence only, not content — hashing untracked
    file content on every gate read was judged too expensive relative to the
    narrowness of the edge case it would additionally catch: a second edit to an
    already-untracked new file with nothing else in the tree changing).

    ``head_sha`` alone would miss any uncommitted edit (HEAD doesn't move for a
    working-tree change made by ANY tool, including Bash) — ``diff_hash`` is what
    actually detects "the code changed since this verdict was written" for the
    invalidator-blind-edit gap this exists to close.

    EVERY untracked path under ``.claude/`` is excluded from the untracked-file
    list — not just the verdict store's own artifacts. The store's own files
    (``.warden-verdicts.json``, its lockfile, ``.warden-log``, per-worktree marker
    buckets) are what originally motivated this: they're WRITTEN by
    ``_write_verdict`` itself, so without excluding them the act of recording a
    verdict would change the untracked-file list and self-invalidate the entry
    the moment it's written (confirmed: a brand-new ``.claude/`` dir reports as a
    single ``?? .claude/`` porcelain line — git doesn't descend into a wholly-new
    untracked directory in porcelain output). But the exclusion is intentionally
    broader than just those specific paths: ``.claude/`` as a whole is this
    project's dev-tooling/config directory (markers, warden config, skills,
    scratch state), not "the code under review" — a brand-new file appearing
    there is tooling housekeeping, not a code change this fingerprint needs to
    react to. This repo's own ``.gitignore`` already excludes the store's own
    files specifically, but the exclusion here doesn't rely on that (nor does it
    try to enumerate every warden-state filename, which would drift out of sync
    with ``.gitignore`` over time) — other repos adopting this warden system
    (e.g. via add-guardrails) may not replicate every one of these gitignore
    entries, and a self-invalidating gate would be a much worse failure mode than
    this blind spot. Known, accepted consequence: a genuinely new *reviewable*
    file landing directly under ``.claude/`` (e.g. a new skill or hook script,
    not yet ``git add``ed) would NOT stale an existing SHIP on its own — see
    ``test_new_untracked_file_directly_under_claude_dir_does_not_stale_ship`` in
    the oracle test suite for the boundary this documents.

    Fail-open at the primitive level: if any of the three git calls fails (not a
    git repo, git missing, etc.), returns ``(None, None)`` — callers decide
    fail-open vs fail-closed from there (see ``_fresh_entry``).
    """
    head_sha = _entry._git(worktree, "rev-parse", "HEAD")
    if head_sha is None:
        return (None, None)
    diff_output = _entry._git(worktree, "diff", "HEAD")
    if diff_output is None:
        return (None, None)
    status_output = _entry._git(worktree, "status", "--porcelain", "--untracked-files=normal")
    if status_output is None:
        return (None, None)
    untracked = sorted(
        line[3:] for line in status_output.splitlines()
        if line.startswith("??") and not line[3:].startswith(".claude/")
    )
    composite = diff_output + "\x00" + "\x00".join(untracked)
    diff_hash = hashlib.sha256(composite.encode("utf-8")).hexdigest()
    return (head_sha, diff_hash)  # _git already .strip()s its return value


def _fresh_entry(
    data: dict[str, Any], warden: str, worktree: Path, fail_open: bool = True,
    check_fingerprint: bool = True,
) -> dict[str, Any] | None:
    """Return ``data[warden]`` if it should be trusted right now, else ``None``.

    - Missing/non-dict entry: ``None`` (nothing to trust).
    - ``verdict`` not in ``_STALENESS_ELIGIBLE_VERDICTS`` (i.e. REVISE, BLOCK,
      COULD_NOT_RUN): returned UNCHANGED, always — never filtered by staleness.
      This is the critical invariant LIA-382 protects: a stale-looking REVISE
      must stay visible and blocking, or ``mark_warden``'s post-REVISE
      TRIVIAL-bypass guard (which reads ``_last_verdict``) would be silently
      defeated by the very edit someone makes to FIX the REVISE.
    - ``check_fingerprint=False`` (LIA-516): a SHIP/TRIVIAL entry is returned
      UNCHANGED without ever comparing fingerprints — same outcome as the
      legacy-entry branch below, but explicit and role-driven. Exists for
      ``plan-reviewer``'s model-backend reads (``run_plan_review_gate``'s
      ``_evaluate_backends(..., skip_claude=True)``): that SHIP approves the
      *plan text* (intent), not a diff snapshot, so a ``diff_hash`` change on
      the first implementation edit must NOT stale it — correct invalidation
      for that role already happens via ``run_session_init`` and
      ``run_plan_mode_invalidator`` explicitly clearing
      ``plan-reviewer@<backend>`` on session start / a new plan, not via this
      fingerprint.
    - SHIP/TRIVIAL entry with no ``head_sha``/``diff_hash`` recorded (a legacy,
      pre-LIA-382 entry): returned UNCHANGED — the staleness check is skipped,
      not treated as automatically stale, so this fix doesn't retroactively
      invalidate everything the moment it ships.
    - SHIP/TRIVIAL entry with a recorded fingerprint: compared against
      *worktree*'s CURRENT fingerprint. Match (or *worktree*'s current
      fingerprint can't be computed AND ``fail_open`` is True) → returned
      unchanged. Mismatch (or can't-compute with ``fail_open=False``) → ``None``.

    ``fail_open`` defaults to the base policy (matches the existing ``_git``
    caller convention throughout this codebase: an infra hiccup shouldn't newly
    block a gate that worked before this fix existed). The one exception is the
    admin-merge standing-grant check, which passes ``fail_open=False`` — a stale
    SHIP there would let a merge skip per-command approval entirely, so an
    unverifiable fingerprint must NOT be trusted there.
    """
    entry = data.get(warden)
    if not isinstance(entry, dict):
        return None
    if entry.get("verdict") not in _STALENESS_ELIGIBLE_VERDICTS:
        return entry
    if not check_fingerprint:
        return entry
    stored_head = entry.get("head_sha")
    stored_diff = entry.get("diff_hash")
    if stored_head is None or stored_diff is None:
        return entry  # legacy entry, no fingerprint recorded — trust as before
    current_head, current_diff = _compute_state_fingerprint(worktree)
    if current_head is None or current_diff is None:
        return entry if fail_open else None
    if current_head == stored_head and current_diff == stored_diff:
        return entry
    return None  # stale


def _read_verdict(
    marker_name: str, repo_root: Path, *, check_fingerprint: bool = True,
) -> str | None:
    """Return the verdict string for *marker_name* from .warden-verdicts.json.

    Maps the marker name (e.g. ``"code-reviewed"``) to the warden key used in
    the JSON (e.g. ``"code-reviewer"``) via ``MARKER_NAMES``.  Returns ``None``
    if the file is absent, malformed, the entry is missing, or (LIA-382) a
    SHIP/TRIVIAL entry's fingerprint no longer matches the worktree's current
    state — see ``_fresh_entry``.

    ``check_fingerprint=False`` (LIA-516) skips that fingerprint comparison
    entirely — see ``_fresh_entry``'s docstring for when this is appropriate
    (plan-reviewer's model-backend reads only; every other caller keeps the
    default).
    """
    warden = _entry.MARKER_NAMES.get(marker_name)
    if not warden:
        return None
    data = _read_verdicts(repo_root)
    worktree = _entry._resolve_verdict_worktree(repo_root)
    entry = _fresh_entry(data, warden, worktree, check_fingerprint=check_fingerprint)
    if not isinstance(entry, dict):
        return None
    v = entry.get("verdict")
    return v if isinstance(v, str) else None


@contextlib.contextmanager
def _verdict_file_lock(path: Path):
    """Exclusive cross-process lock guarding a read-modify-write of *path*.

    Held on a SIDECAR lockfile (``<path>.lock``), not on *path* itself, so the
    lock survives ``_write_atomic``'s ``os.replace`` (which swaps the target inode
    while the lockfile's inode is untouched). NO-OP when ``fcntl`` is unavailable
    (Windows) — see the import-site note; that branch is exercised in tests by
    monkeypatching ``fcntl = None``.
    """
    if fcntl is None:  # Windows / no fcntl — no concurrent marks occur there
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    # O_CREAT|O_RDWR (no O_TRUNC) = create-if-absent without truncating, the safe
    # opener for a long-lived lockfile.
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _locked_verdict_update(path: Path, mutate: Callable[[dict[str, Any]], bool]) -> bool:
    """Lock-guarded read-modify-write of the verdict store at *path*.

    ``mutate(data) -> bool`` mutates the dict in place and returns whether it
    changed anything. The store is re-read FRESH inside the lock, so a concurrent
    writer's key (already on disk) is merged rather than clobbered. The write — and
    its ``.bak`` backup — is skipped when ``mutate`` reports no change.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with _verdict_file_lock(path):
        data = _read_verdicts_at(path)
        changed = mutate(data)
        if changed:
            _entry._write_atomic(
                path, json.dumps(data, indent=2, sort_keys=True) + "\n"
            )
    return changed


def _clear_verdict(marker_name: str, repo_root: Path) -> None:
    """Remove the *marker_name* entry from .warden-verdicts.json.

    Maps the marker name to the warden key via ``MARKER_NAMES``.  Silently
    skips if the file is absent or the key is not present.
    """
    warden = _entry.MARKER_NAMES.get(marker_name)
    if not warden:
        return
    path = _verdicts_path(repo_root)

    def _pop(data: dict[str, Any]) -> bool:
        # Absent key → no change → caller skips the write (no spurious .bak).
        return data.pop(warden, _VERDICT_UNSET) is not _VERDICT_UNSET

    try:
        _locked_verdict_update(path, _pop)
    except OSError:
        _entry._debug(f"_clear_verdict: failed to write {path}")


def _write_verdict(repo_root: Path, warden: str, verdict: str, reason: str, source: str = "manual") -> None:
    path = _verdicts_path(repo_root)
    # Submission time, not write time: under lock contention the actual write can lag.
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    # LIA-382: fingerprint the SAME worktree _verdicts_path resolved the bucket
    # against, so a later read comparing against that worktree's current state is
    # comparing like-for-like. Computed uniformly for every verdict value (not
    # just SHIP/TRIVIAL) — harmless to store on a REVISE/BLOCK entry even though
    # _fresh_entry never reads it back for those; keeps write-time logic simple,
    # with all the verdict-value scoping living in one place (read time).
    worktree = _entry._resolve_verdict_worktree(repo_root)
    head_sha, diff_hash = _compute_state_fingerprint(worktree)

    def _set(data: dict[str, Any]) -> bool:
        entry: dict[str, Any] = {
            "verdict": verdict,
            "ts": stamp,
            "reason": reason,
            "source": source,
        }
        if head_sha is not None:
            entry["head_sha"] = head_sha
        if diff_hash is not None:
            entry["diff_hash"] = diff_hash
        data[warden] = entry
        return True

    # Lock-guarded RMW: re-reads inside the lock so a concurrent writer's key is
    # merged, not clobbered (LIA-332).
    _locked_verdict_update(path, _set)

    log = _audit_log_path(repo_root)
    safe_reason = reason.replace("|", "/").replace("\n", " ").strip()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} | {warden:<15} | {verdict:<7} | {safe_reason}\n")


def _last_verdict(repo_root: Path, warden: str) -> str | None:
    data = _read_verdicts(repo_root)
    worktree = _entry._resolve_verdict_worktree(repo_root)
    entry = _fresh_entry(data, warden, worktree)
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

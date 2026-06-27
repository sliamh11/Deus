"""Oracle tests for LIA-332: verdict-store concurrent-write safety.

Derived FROM THE SPEC (the LIA-332 bug report), blind to the implementation.
Tests are RED against the current (unfixed) code and GREEN only once a
cross-process lock is added to ``_write_verdict`` / ``_clear_verdict``.

Run:
    python3 -m pytest scripts/tests/test_verdict_store_race.py -v

Oracle tagging convention (oracle-rules.md § oracle-tagged):
    # @oracle LIA-332: <one-line spec reference>

Setup notes for the implementer
--------------------------------
* These tests use ``multiprocessing.get_context("fork")`` (POSIX-only).
  They are skipped on Windows via the ``skipif`` markers.
* The ``store_root`` fixture patches ``h._claude_marker_dir`` to return a
  temp ``.claude`` dir, exactly as ``test_phase3_cogate_oracle.py`` does.
  The fork'd worker inherits the patched module state — no re-wiring needed.
* ``_write_verdict`` also appends to ``.warden-log``; concurrent appends to
  an O_APPEND file are atomic on POSIX, so the log is not part of any assertion.
* The fix must NOT remove the ``if warden not in data: return`` guard in
  ``_clear_verdict`` (invariant 2 regresses if it does).
"""
from __future__ import annotations

import json
import multiprocessing
import sys
import time
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_warden_hooks as h
from warden_hooks import verdict_store as _vs

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Number of concurrent processes per round.  20 processes all racing to
# read-modify-write the same file reliably exposes the clobber on current code.
_RACE_PROCESSES = 20

# Multiple rounds multiply exposure probability.  Even one round typically
# drops 1–19 keys; three rounds make false-GREEN essentially impossible.
_RACE_ROUNDS = 3


# ---------------------------------------------------------------------------
# Module-level worker (must be importable by name for the fork context)
# ---------------------------------------------------------------------------

def _worker_write_verdict(repo_root_str: str, warden_key: str) -> None:
    """Fork worker: write one verdict using the inherited (monkeypatched) module state.

    Called in a fork'd subprocess.  The subprocess inherits:
      * ``_vs._entry`` bound to ``h`` (from the parent's ``bind_entry`` call)
      * ``h._claude_marker_dir`` monkeypatched to return the temp .claude dir
    No re-import or re-wiring needed.
    """
    _vs._write_verdict(
        Path(repo_root_str), warden_key, "SHIP", f"oracle-race-{warden_key}"
    )


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """Isolated verdict store under ``tmp_path/.claude``.

    Patches ``_claude_marker_dir`` on the live entry module so every
    ``verdict_store`` call routes to ``tmp_path/.claude`` instead of
    resolving the worktree via git.  Mirrors the harness pattern in
    ``test_phase3_cogate_oracle.py``.
    """
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    monkeypatch.setattr(h, "_claude_marker_dir", lambda root: cdir)
    # Suppress the git-backed worktree derivation that would run in fork'd children.
    monkeypatch.setattr(h, "_worktree_for_cwd", lambda cwd, root: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Invariant 1 oracle — concurrent RMW preserves ALL keys
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="fork() is POSIX-only; lock primitive is POSIX")
def test_concurrent_writes_preserve_all_keys(store_root):
    # @oracle LIA-332: N concurrent _write_verdict calls into the same store must land ALL N keys
    #
    # Contract (from spec):
    #   If N processes each call _write_verdict(repo_root, "role@b{i}", "SHIP", ...)
    #   concurrently into the SAME store, after all complete the store MUST contain
    #   ALL N keys (none clobbered).
    #
    # Falsifies: the silent key-loss bug where the later writer's whole-dict
    #   os.replace overwrites an earlier writer's key.  Concretely: two processes each
    #   read {"A": ...}, each set their own key ("A"+"B{i}" or "A"+"B{j}"), and the
    #   last os.replace wins — the other's key silently vanishes.  This was the live
    #   failure mode: a code-reviewer@gpt SHIP vanished when it raced the claude
    #   auto-mark.
    #
    # RED on current code:
    #   _write_verdict reads the full dict, sets one key, writes the whole dict back
    #   with os.replace.  Two (or more) concurrent writers each capture a stale
    #   snapshot before the other has written; the later os.replace silently clobbers
    #   earlier keys.  With 20 processes: typically 1–19 keys are lost per round.
    #
    # GREEN after fix:
    #   A cross-process lock (e.g. fcntl.flock, lockfile, or any POSIX IPC primitive)
    #   serialises the RMW so every writer's key survives.  The fix must not assume
    #   any particular mechanism — this test asserts only observable state.

    repo_root = store_root
    ctx = multiprocessing.get_context("fork")
    total_lost = 0

    for round_idx in range(_RACE_ROUNDS):
        # Fresh key set per round so rounds do not interfere with each other.
        warden_keys = [
            f"oracle-{round_idx}-warden{i}@gpt" for i in range(_RACE_PROCESSES)
        ]

        procs = [
            ctx.Process(target=_worker_write_verdict, args=(str(repo_root), key))
            for key in warden_keys
        ]
        # Start all at once to maximise concurrency.
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0, (
                f"worker exited with code {p.exitcode} — subprocess crashed; "
                "check that the fork'd module state is properly wired"
            )

        verdicts_path = store_root / ".claude" / ".warden-verdicts.json"
        data: dict = {}
        if verdicts_path.exists():
            try:
                data = json.loads(verdicts_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass  # partial/corrupt write — all keys from this round count as lost

        missing = [k for k in warden_keys if k not in data]
        total_lost += len(missing)

    assert total_lost == 0, (
        f"{total_lost} verdict key(s) clobbered across {_RACE_ROUNDS} rounds of "
        f"{_RACE_PROCESSES} concurrent _write_verdict calls.\n"
        "Root cause: the read-modify-write sequence (read dict → mutate one key → "
        "os.replace whole dict) is not atomic across processes.  Each concurrent "
        "writer captures a stale snapshot before others' writes land; the last "
        "os.replace wins and all preceding writes lose their keys.\n"
        "Fix: add a cross-process lock (e.g. fcntl.flock on a lockfile) around the "
        "RMW in _write_verdict (and _clear_verdict) so writes are serialised.\n"
        "This assertion is RED on the current code and GREEN once a lock is in place."
    )


# ---------------------------------------------------------------------------
# Invariant 2 oracle — clear-of-absent-key writes nothing
# ---------------------------------------------------------------------------

def test_clear_absent_key_writes_nothing(store_root):
    # @oracle LIA-332: _clear_verdict for a key NOT present must not rewrite the file
    #
    # Contract (from spec):
    #   Calling _clear_verdict for a marker whose warden key is NOT present in the
    #   store must leave the file completely untouched: no new .bak-<stamp> backup
    #   created, mtime unchanged.
    #
    # Falsifies: an implementation that removes or bypasses the ``if warden not in
    #   data: return`` early-exit guard, causing an unnecessary write on every
    #   no-op clear call.  Such a spurious write would (a) update mtime, (b)
    #   create a .bak-<stamp> backup (because _write_atomic backs up before
    #   overwriting), and (c) introduce unnecessary I/O and a write race surface.
    #
    # Expected GREEN on current code (the guard already exists).  Present to
    # catch regressions if the fix refactors _clear_verdict and accidentally
    # drops the early-exit.

    repo_root = store_root
    verdicts_path = store_root / ".claude" / ".warden-verdicts.json"
    claude_dir = store_root / ".claude"

    # Pre-populate the store with a key OTHER than "code-reviewer" (the key
    # "code-reviewed" maps to via MARKER_NAMES).  The file must exist so the
    # test is realistic: _clear_verdict reads the dict and finds the key absent.
    initial_data = {
        "some-other-warden": {
            "verdict": "SHIP",
            "ts": "2026-01-01T00:00:00Z",
            "reason": "seed",
            "source": "test",
        }
    }
    verdicts_path.write_text(
        json.dumps(initial_data, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    # Record filesystem state before the call.
    # Use nanosecond mtime for precision (APFS / ext4 / tmpfs all support it).
    mtime_ns_before = verdicts_path.stat().st_mtime_ns
    bak_before: set[Path] = set(claude_dir.glob(".warden-verdicts.json.bak-*"))

    # Sleep long enough that any write would produce a distinguishably later mtime.
    time.sleep(0.05)

    # "code-reviewed" → "code-reviewer" via real MARKER_NAMES.
    # "code-reviewer" is NOT a key in initial_data, so _clear_verdict must return early.
    h._clear_verdict("code-reviewed", repo_root)

    mtime_ns_after = verdicts_path.stat().st_mtime_ns
    bak_after: set[Path] = set(claude_dir.glob(".warden-verdicts.json.bak-*"))
    new_baks = bak_after - bak_before

    assert mtime_ns_after == mtime_ns_before, (
        "_clear_verdict for an absent key rewrote .warden-verdicts.json "
        "(mtime changed from "
        f"{mtime_ns_before} to {mtime_ns_after}). "
        "The no-op early-return guard (``if warden not in data: return``) was "
        "bypassed or removed.  The fix must preserve this guard."
    )
    assert not new_baks, (
        f"_clear_verdict for an absent key created unexpected backup file(s): "
        f"{[str(p.name) for p in sorted(new_baks)]}. "
        "_write_atomic creates a .bak-<stamp> copy before overwriting — a new "
        "backup proves an unwanted write occurred.  The no-op guard must remain."
    )

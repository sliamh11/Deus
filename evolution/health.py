"""Durable health records for evolution subsystems (LIA-551).

Why this exists: `_maybe_auto_optimize` wrapped its whole body in
`except Exception: log.warning(...)`, so an optimizer that could not run at all
was indistinguishable from one with nothing to do. It stayed broken from
~2026-03 to 2026-08 while the surrounding judge/reflexion loop kept working —
1,766 judged interactions, 0 prompt artifacts, no signal anywhere.

Escalation deliberately does NOT go through the logger. Two independent reasons,
both measured:
  1. `src/evolution-client.ts` flattens all Python child stderr to a hardcoded
     `logger.warn(...)`, so severity does not survive the process boundary.
  2. The auto-issue job reads `logs/deus.error.log`, which held 20 warn/error
     lines while `logs/deus.log` held 8,255 (LIA-553).
Raising a log level here would therefore change nothing. The durable row below
and the `health` CLI subcommand's exit code are the escalation surface.

Two design choices worth keeping:

* This does not go through `StorageProvider`. Adding abstract methods would
  break `FakeStorageProvider` in the tests, and more importantly a mechanism
  that reports whether the storage-consuming subsystem works should not depend
  on the abstraction it monitors. It also skips `sqlite_vec` for the same
  reason — fewer things that can break underneath it.
* SKIPPED is not a status. `last_status` only ever holds OK, FAILED, or NULL.
  An earlier design made SKIPPED a third status that preserved the failure
  counters but still overwrote `last_status`; since the rollup and the exit
  code both read `last_status`, one below-threshold cycle after a real failure
  flipped the system back to "healthy" with the failure unresolved. A skip now
  cannot write a status at all, so it cannot mask a failure — the sharp edge is
  removed rather than guarded.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import config as _config

log = logging.getLogger(__name__)

STATUS_OK = "OK"
STATUS_FAILED = "FAILED"

# Rank for worst-of aggregation: FAILED > never-attempted > OK.
#
# Never-attempted ranks ABOVE OK deliberately. An earlier ordering put it at
# the bottom, which meant one healthy sibling outranked it and the rollup
# reported OK while other components had never run at all — a newly added
# MODULE_REGISTRY entry would have been invisible behind its neighbours. The
# invariant is that never-attempted is not healthy, so it must survive
# aggregation. It still does not trigger the failure exit code; only FAILED
# does, so honesty here costs no false alarms.
# The absence of a third key is intentional: never-attempted is represented by
# `last_status` being NULL, and callers reach _RANK_NEVER through the `.get()`
# default. Do not add `None: 1` here — the lookup is by status value, and a
# literal None key would shadow nothing while implying the default is unused.
_STATUS_RANK = {STATUS_FAILED: 2, STATUS_OK: 0}
_RANK_NEVER = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subsystem_health (
    component            TEXT PRIMARY KEY,
    last_status          TEXT,
    last_reason          TEXT,
    last_attempt_at      TEXT,
    last_skipped_at      TEXT,
    last_ok_at           TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    first_failed_at      TEXT
)
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db_path():
    """Resolved per call so test monkeypatching of config works, matching the
    storage provider's lazy-read convention."""
    return _config.EVOLUTION_DB_PATH


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout=30 + WAL mirror the storage provider: this writes the same file
    # from a second connection on the batch-judge hot path, immediately after
    # the provider's own writes.
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(_SCHEMA)
    return db


def record_attempt(component: str, status: str, reason: Optional[str] = None) -> None:
    """Record a real optimization attempt. `status` must be OK or FAILED.

    OK is the only thing that clears a failure streak. FAILED increments it and
    pins `first_failed_at` to the start of the streak.
    """
    if status not in (STATUS_OK, STATUS_FAILED):
        raise ValueError(f"status must be {STATUS_OK} or {STATUS_FAILED}, got {status!r}")

    now = _now()
    try:
        db = _connect()
        try:
            if status == STATUS_OK:
                db.execute(
                    """
                    INSERT INTO subsystem_health
                        (component, last_status, last_reason, last_attempt_at,
                         last_ok_at, consecutive_failures, first_failed_at)
                    VALUES (?, 'OK', ?, ?, ?, 0, NULL)
                    ON CONFLICT(component) DO UPDATE SET
                        last_status          = 'OK',
                        last_reason          = excluded.last_reason,
                        last_attempt_at      = excluded.last_attempt_at,
                        last_ok_at           = excluded.last_ok_at,
                        consecutive_failures = 0,
                        first_failed_at      = NULL
                    """,
                    (component, reason, now, now),
                )
            else:
                db.execute(
                    """
                    INSERT INTO subsystem_health
                        (component, last_status, last_reason, last_attempt_at,
                         consecutive_failures, first_failed_at)
                    VALUES (?, 'FAILED', ?, ?, 1, ?)
                    ON CONFLICT(component) DO UPDATE SET
                        last_status          = 'FAILED',
                        last_reason          = excluded.last_reason,
                        last_attempt_at      = excluded.last_attempt_at,
                        consecutive_failures = subsystem_health.consecutive_failures + 1,
                        first_failed_at      = COALESCE(subsystem_health.first_failed_at,
                                                        excluded.first_failed_at)
                    """,
                    (component, reason, now, now),
                )
            db.commit()
        finally:
            db.close()
    except Exception:
        # Never break the caller: this runs on the batch-judge hot path and the
        # live judge/reflexion loop must keep working. Logged at ERROR with a
        # traceback rather than swallowed — the very pattern this module exists
        # to eliminate. (Reaching a human still depends on LIA-553.)
        log.error("health: failed to record attempt for %s", component, exc_info=True)


def record_skip(component: str) -> None:
    """Record that a cycle ran but deliberately did no work.

    Touches `last_skipped_at` and nothing else — not `last_status`, not
    `last_reason`, not the failure counters, not `last_ok_at`. That is what
    makes a skip structurally incapable of damaging an unresolved failure's
    record: it can neither flip the status nor overwrite the diagnostic cause.
    Both were real bugs before this shape.

    Deliberately takes no `reason` argument, and callers must not grow one.
    Every skip site is an early return from its own explicit gate, so the gate
    condition IS the explanation and it is constant per site — derivable from
    the config or the query that guards it, never varying call to call. Current
    sites, illustrative rather than exhaustive: the below-threshold return in
    `_maybe_auto_optimize` (cli.py), the empty-queue return in
    `judge_pending_interactions` (maintenance.py), and the `min_new` and
    usable-examples gates reached through `_maybe_auto_extract_principles`
    (cli.py). Storing a reason in a column was tried and produced three further
    defects — `rollup()` reads `last_reason` only and would have reported None
    for a skip-only component; the column needed an ALTER TABLE that every
    reader would execute, adding DDL lock exposure on a concurrently-written
    file; and it deviated from patterns/eval-change.md. The field is simply not
    written.

    On the INSERT branch `last_status` is left as SQL NULL, so a
    never-attempted component is never confused with a healthy one.
    """
    now = _now()
    try:
        db = _connect()
        try:
            db.execute(
                """
                INSERT INTO subsystem_health (component, last_skipped_at)
                VALUES (?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    last_skipped_at = excluded.last_skipped_at
                """,
                (component, now),
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        log.error("health: failed to record skip for %s", component, exc_info=True)


def get(component: str) -> Optional[Dict[str, Any]]:
    """Return one component's row, or None if it has no row at all."""
    try:
        db = _connect()
        try:
            row = db.execute(
                "SELECT * FROM subsystem_health WHERE component = ?", (component,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            db.close()
    except Exception:
        log.error("health: failed to read %s", component, exc_info=True)
        return None


def list_all(prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all rows, optionally restricted to a component prefix."""
    try:
        db = _connect()
        try:
            if prefix:
                rows = db.execute(
                    "SELECT * FROM subsystem_health WHERE component LIKE ? ORDER BY component",
                    (f"{prefix}%",),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM subsystem_health ORDER BY component"
                ).fetchall()
            return [dict(r) for r in rows]
        finally:
            db.close()
    except Exception:
        log.error("health: failed to list rows", exc_info=True)
        return []


def rollup(prefix: str) -> Dict[str, Any]:
    """Derive a worst-of verdict across every row under `prefix`.

    Computed live and never persisted, so there is no second copy of the truth
    to drift. Precedence is FAILED > never-attempted > OK, per `_STATUS_RANK`
    — never-attempted deliberately outranks OK so a component that has never
    run cannot hide behind a healthy sibling. Do not "correct" this to put OK
    last without reading the rationale at `_STATUS_RANK`; that ordering was
    tried and it reintroduced the masking bug.
    """
    rows = list_all(prefix)
    if not rows:
        return {
            "component": prefix.rstrip("."),
            "status": None,
            "reason": "no health records",
            "worst_component": None,
            "consecutive_failures": 0,
            "rows": [],
        }

    worst = max(rows, key=lambda r: _STATUS_RANK.get(r["last_status"], _RANK_NEVER))
    # `status` stays None for a never-attempted worst row, same as for zero rows
    # — changing that would move _STATUS_RANK precedence and the exit-code
    # contract. Only the reason distinguishes them, because "nothing has ever
    # been recorded" and "cycles ran but none ever attempted work" are different
    # facts and the caller could not previously tell them apart (LIA-556).
    reason = worst["last_reason"]
    if worst["last_status"] is None and reason is None:
        never = sum(1 for r in rows if r["last_status"] is None)
        reason = f"{never} of {len(rows)} components recorded skips but never attempted work"
    return {
        "component": prefix.rstrip("."),
        "status": worst["last_status"],
        "reason": reason,
        "worst_component": worst["component"],
        "consecutive_failures": max(r["consecutive_failures"] or 0 for r in rows),
        "rows": rows,
    }


def has_failure(prefix: Optional[str] = None) -> bool:
    """True if any component under `prefix` is currently FAILED; with no
    prefix, across every component. This is what the `health` subcommand's
    exit code keys off — see `cmd_health`, which calls it rather than
    reimplementing the check.

    Known, accepted gap: `list_all()` returns `[]` if the health store itself
    is unreadable (corrupt DB, disk full), so this reports False — healthy —
    in that case. It is the read-side mirror of the write-side bug this module
    fixes. Accepted rather than overlooked: a broken `evolution.db` surfaces
    loudly through `StorageProvider` on the same hot path, so this is not the
    only signal. Revisit if that stops being true."""
    return any(r["last_status"] == STATUS_FAILED for r in list_all(prefix))

"""
LIA-109 follow-up: infra-error ingestion filter.

Covers the detector truth table (harness stubs match; legitimate terse agent
turns do NOT), the retag maintenance path (only stubs flip suite + lose score,
idempotent), and the judge-gate exclusion of eval_suite='infra_error'.
"""
import sqlite3

import pytest

from evolution.ingest_filter import INFRA_ERROR_SUITE, is_infra_error
from evolution.ilog.interaction_log import log_interaction
from evolution.storage import get_storage
import evolution.maintenance as maintenance


# ── Detector truth table ─────────────────────────────────────────────────────

INFRA_RESPONSES = [
    "API Error: Connection closed mid-response. The response above may be incomplete.",
    "API Error: 529 Overloaded. This is a server-side issue, usually temporary — try again",
    "API Error: 500 Internal server error.",
    "API Error: Unable to connect to API (ConnectionRefused)",
    "Not logged in · Please run /login",
    "   API Error: leading whitespace still caught   ",  # stripped before match
]

LEGIT_RESPONSES = [
    "Waiting on GPT's round 2 verdict.",           # terse but real
    "Complete. No further action on my end.",
    "Holding for PR3's PUT→Sent fix.",
    "Not logged in yet, but here's what I found in the auth flow…",  # prefix ≠ exact stub
    "The API Error you saw earlier was a transient 529; I retried and it worked.",  # discusses, doesn't start with
    "",
    "   ",
    None,
]


@pytest.mark.parametrize("resp", INFRA_RESPONSES)
def test_detects_infra_error(resp):
    assert is_infra_error(resp) is True


@pytest.mark.parametrize("resp", LEGIT_RESPONSES)
def test_passes_legit_response(resp):
    assert is_infra_error(resp) is False


# ── Judge-gate exclusion + NULL-safety ───────────────────────────────────────

def _raw(db_path, sql, params=()):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    rows = db.execute(sql, params).fetchall()
    db.close()
    return rows


def test_get_unjudged_excludes_infra_error(test_db):
    store = get_storage()
    log_interaction(prompt="p one, long enough", response="r one, long enough",
                    group_folder="g", interaction_id="normal-1")
    log_interaction(prompt="p two, long enough", response="API Error: Connection closed",
                    group_folder="g", interaction_id="infra-1", eval_suite=INFRA_ERROR_SUITE)
    ids = {r["id"] for r in store.get_unjudged_interactions(limit=50)}
    assert "normal-1" in ids
    assert "infra-1" not in ids


def test_get_unjudged_null_suite_row_not_returned(test_db):
    """Frozen prediction: a NULL eval_suite row is NOT returned. Both suite
    clauses (`!= 'maintenance'`, `!= 'infra_error'`) are non-null-safe by design
    — consistent style — and SQLite 3-valued logic (`NULL != x` → UNKNOWN) drops
    the row. eval_suite is DEFAULT 'runtime' so a NULL suite cannot occur in
    practice; this pins the behavior if one ever did."""
    log_interaction(prompt="p null-suite, long enough", response="r long enough",
                    group_folder="g", interaction_id="nullsuite-1")
    # Force the suite to NULL directly (log_interaction defaults it to 'runtime').
    db = sqlite3.connect(test_db)
    db.execute("UPDATE interactions SET eval_suite = NULL WHERE id = ?", ("nullsuite-1",))
    db.commit()
    db.close()
    ids = {r["id"] for r in get_storage().get_unjudged_interactions(limit=50)}
    assert "nullsuite-1" not in ids


# ── Retag maintenance path ───────────────────────────────────────────────────

def test_retag_infra_errors(test_db):
    store = get_storage()
    # Seed: two infra stubs (pre-scored, wrong suite) + one legit scored row.
    log_interaction(prompt="pa long enough", response="API Error: Connection closed mid-response",
                    group_folder="g", interaction_id="infra-a", eval_suite="claude_code")
    log_interaction(prompt="pb long enough", response="Not logged in · Please run /login",
                    group_folder="g", interaction_id="infra-b", eval_suite="claude_code")
    log_interaction(prompt="pc long enough", response="Waiting on CI, will check back.",
                    group_folder="g", interaction_id="legit-c", eval_suite="claude_code")
    # Give all three a (meaningless, for infra) judge score.
    for iid in ("infra-a", "infra-b", "legit-c"):
        store.update_interaction(iid, judge_score=0.4, judge_dims='{"quality": 0.0}')

    n = maintenance.retag_infra_error_interactions()
    assert n == 2

    rows = {r["id"]: r for r in _raw(test_db,
        "SELECT id, eval_suite, judge_score FROM interactions")}
    assert rows["infra-a"]["eval_suite"] == INFRA_ERROR_SUITE
    assert rows["infra-a"]["judge_score"] is None            # score nulled
    assert rows["infra-b"]["eval_suite"] == INFRA_ERROR_SUITE
    assert rows["infra-b"]["judge_score"] is None
    assert rows["legit-c"]["eval_suite"] == "claude_code"     # untouched
    assert rows["legit-c"]["judge_score"] == 0.4

    # Idempotent: second run retags nothing.
    assert maintenance.retag_infra_error_interactions() == 0

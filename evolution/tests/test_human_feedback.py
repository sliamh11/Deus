"""
LIA-109: human ground-truth feedback seam.

Covers the frozen decision matrix for process_human_feedback (direction-aware
routing + zone-alignment archival), the source_ref set-once upsert, NULL-safe
human-feedback idempotency, migration idempotency, and a completeness pin that
every interaction-keyed save_reflection call site threads polarity.
"""
import re
import sqlite3
from pathlib import Path

import pytest

import evolution.config as config_mod
import evolution.maintenance as maintenance_mod
from evolution.ilog.interaction_log import (
    get_interaction_by_source_ref,
    log_interaction,
    update_human_feedback,
    update_score,
)
from evolution.storage import get_storage

EVOLUTION_ROOT = Path(__file__).resolve().parents[1]


def _log(iid: str, source_ref=None, **kw):
    return log_interaction(
        prompt=f"prompt for {iid} long enough to be realistic",
        response=f"response for {iid} long enough to be realistic",
        group_folder="test-group",
        interaction_id=iid,
        source_ref=source_ref,
        **kw,
    )


def _columns(db_path, table):
    db = sqlite3.connect(db_path)
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})").fetchall()]
    db.close()
    return cols


def _insert_reflection(db_path, rid, iid, polarity, score_at_gen, category="style"):
    db = sqlite3.connect(db_path)
    db.execute(
        "INSERT INTO reflections (id, interaction_id, timestamp, group_folder,"
        " content, category, score_at_gen, polarity)"
        " VALUES (?, ?, datetime('now'), 'test-group', 'lesson', ?, ?, ?)",
        (rid, iid, category, score_at_gen, polarity),
    )
    db.commit()
    db.close()


def _reflection_state(db_path, rid):
    db = sqlite3.connect(db_path)
    row = db.execute(
        "SELECT archived_at FROM reflections WHERE id = ?", [rid]
    ).fetchone()
    db.close()
    assert row is not None, f"reflection {rid} missing"
    return "archived" if row[0] else "active"


def _row(db_path, iid):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM interactions WHERE id = ?", [iid]).fetchone()
    db.close()
    return dict(row) if row else None


@pytest.fixture
def fake_generators(monkeypatch):
    """Record generator + save calls without LLM or embedding dependencies."""
    calls = {"corrective": [], "positive": [], "saved": []}

    def fake_reflection(**kw):
        calls["corrective"].append(kw)
        return "corrective lesson content", "reasoning"

    def fake_positive(**kw):
        calls["positive"].append(kw)
        return "positive lesson content", "positive_pattern"

    def fake_save(**kw):
        calls["saved"].append(kw)
        return "fake-reflection-id"

    import evolution.reflexion.generator as gen_mod
    import evolution.reflexion.store as store_mod
    monkeypatch.setattr(gen_mod, "generate_reflection", fake_reflection)
    monkeypatch.setattr(gen_mod, "generate_positive_reflection", fake_positive)
    monkeypatch.setattr(store_mod, "save_reflection", fake_save)
    return calls


# ── Schema / migration ───────────────────────────────────────────────────────

def test_migration_adds_columns_and_is_idempotent(test_db):
    _log("mig-1")
    _log("mig-2")  # second write re-runs the ALTER loop path harmlessly
    icols = _columns(test_db, "interactions")
    for col in ("source_ref", "human_score", "human_comment",
                "human_scored_at", "human_processed_at"):
        assert col in icols
    assert "polarity" in _columns(test_db, "reflections")


# ── source_ref upsert semantics ──────────────────────────────────────────────

def test_source_ref_set_once_coalesce(test_db):
    _log("sr-1", source_ref="tracing:trace:abc123")
    _log("sr-1")  # re-log without a ref must preserve it
    assert _row(test_db, "sr-1")["source_ref"] == "tracing:trace:abc123"
    _log("sr-1", source_ref="tracing:trace:other")  # set-once: never replaced
    assert _row(test_db, "sr-1")["source_ref"] == "tracing:trace:abc123"


def test_get_interaction_by_source_ref_roundtrip(test_db):
    _log("sr-2", source_ref="tracing:trace:xyz")
    found = get_interaction_by_source_ref("tracing:trace:xyz")
    assert found is not None and found["id"] == "sr-2"
    assert get_interaction_by_source_ref("tracing:trace:missing") is None


# ── update_human_feedback ────────────────────────────────────────────────────

def test_human_feedback_validation(test_db):
    _log("hf-v")
    with pytest.raises(ValueError):
        update_human_feedback("hf-v", 1.5)
    with pytest.raises(ValueError):
        update_human_feedback("hf-v", -0.1)


def test_human_feedback_null_safe_idempotency(test_db):
    store = get_storage()
    _log("hf-1")
    update_human_feedback("hf-1", 0.4)  # no comment
    store.mark_human_feedback_processed("hf-1")

    # identical re-write: stays processed (no reprocessing storm)
    update_human_feedback("hf-1", 0.4)
    assert _row(test_db, "hf-1")["human_processed_at"] is not None

    # none -> comment transition must clear processed (the != NULL-poison trap)
    update_human_feedback("hf-1", 0.4, "now with a comment")
    assert _row(test_db, "hf-1")["human_processed_at"] is None

    store.mark_human_feedback_processed("hf-1")
    # comment -> same comment: stays processed
    update_human_feedback("hf-1", 0.4, "now with a comment")
    assert _row(test_db, "hf-1")["human_processed_at"] is not None

    # comment -> none transition must clear processed
    update_human_feedback("hf-1", 0.4, None)
    assert _row(test_db, "hf-1")["human_processed_at"] is None

    store.mark_human_feedback_processed("hf-1")
    # score change must clear processed
    update_human_feedback("hf-1", 0.9)
    assert _row(test_db, "hf-1")["human_processed_at"] is None


# ── process_human_feedback: frozen decision matrix ───────────────────────────
# thresholds: reflection=0.6, positive=0.85, disagreement=0.3 (module defaults)

MATRIX = [
    # (human, judge, expect_corrective, expect_positive)
    (0.5, 0.85, True, False),    # below threshold -> corrective
    (0.9, 0.5, False, True),     # positive override, diff 0.4
    (0.9, 0.7, False, False),    # diff 0.2 < 0.3 -> stamp only
    (0.7, 0.85, False, False),   # middle band -> stamp only
    (0.59, 0.9, True, False),    # boundary: 0.59 < 0.6 -> corrective
    (0.6, 0.9, False, False),    # boundary: 0.6 not < 0.6 -> stamp only
    (0.85, 0.55, False, True),   # double boundary; float-safe eps comparison
]


@pytest.mark.parametrize("human,judge,expect_corr,expect_pos", MATRIX)
def test_routing_matrix(test_db, fake_generators, human, judge, expect_corr, expect_pos):
    iid = f"mx-{human}-{judge}"
    _log(iid)
    update_score(iid, judge, {"quality": judge})
    update_human_feedback(iid, human)

    assert maintenance_mod.process_human_feedback() == 1
    assert bool(fake_generators["corrective"]) == expect_corr
    assert bool(fake_generators["positive"]) == expect_pos
    assert _row(test_db, iid)["human_processed_at"] is not None
    # human comment/rationale threads through; score_at_gen = human ground truth
    for saved in fake_generators["saved"]:
        assert saved["score_at_gen"] == human
        assert saved["polarity"] in ("corrective", "positive")


def test_unjudged_rows_not_selected(test_db, fake_generators):
    _log("mx-nojudge")
    update_human_feedback("mx-nojudge", 0.2)
    assert maintenance_mod.process_human_feedback() == 0
    assert _row(test_db, "mx-nojudge")["human_processed_at"] is None


def test_processed_rows_skipped(test_db, fake_generators):
    _log("mx-done")
    update_score("mx-done", 0.9, {})
    update_human_feedback("mx-done", 0.2)
    get_storage().mark_human_feedback_processed("mx-done")
    assert maintenance_mod.process_human_feedback() == 0


# ── zone-alignment archival ──────────────────────────────────────────────────

ARCHIVAL = [
    # (human, prior_polarity, prior_score_at_gen, expected_state)
    (0.9, "corrective", 0.4, "archived"),   # positive verdict retires corrective
    (0.5, "positive", 0.9, "archived"),     # corrective verdict retires positive
    (0.5, "corrective", 0.4, "active"),     # aligned corrective kept
    (0.7, "positive", 0.9, "archived"),     # middle band retires positive (stamp-only branch)
    (0.7, "corrective", 0.4, "archived"),   # middle band retires corrective too
    # decoupled-score rows: polarity column is authoritative, score is noise
    (0.5, "corrective", 0.9, "active"),     # user-signal corrective w/ high score: kept
    (0.9, "positive", 0.3, "active"),       # user-signal positive w/ low score: kept
    (0.5, None, 0.9, "active"),             # legacy NULL polarity: never archived
]


@pytest.mark.parametrize("human,polarity,score_at_gen,expected", ARCHIVAL)
def test_zone_alignment_archival(test_db, fake_generators, human, polarity, score_at_gen, expected):
    iid = f"za-{human}-{polarity}-{score_at_gen}"
    rid = f"refl-{iid}"
    _log(iid)
    update_score(iid, 0.75, {})
    _insert_reflection(test_db, rid, iid, polarity, score_at_gen)
    update_human_feedback(iid, human)

    assert maintenance_mod.process_human_feedback() == 1
    assert _reflection_state(test_db, rid) == expected


def test_archival_scoped_to_interaction(test_db, fake_generators):
    _log("za-a")
    _log("za-b")
    update_score("za-a", 0.75, {})
    _insert_reflection(test_db, "refl-a", "za-a", "corrective", 0.4)
    _insert_reflection(test_db, "refl-b", "za-b", "corrective", 0.4)
    update_human_feedback("za-a", 0.9)
    maintenance_mod.process_human_feedback()
    assert _reflection_state(test_db, "refl-a") == "archived"
    assert _reflection_state(test_db, "refl-b") == "active"  # other interaction untouched


# ── writer completeness pin ──────────────────────────────────────────────────

def test_every_interaction_keyed_save_reflection_threads_polarity():
    """A save_reflection call that passes interaction_id= must pass polarity=.

    Guards the zone-alignment model: an unthreaded writer creates NULL-polarity
    rows that can never be archived when later contradicted by human feedback.
    New writers must either thread polarity or use interaction_id=None.
    """
    offenders = []
    for path in EVOLUTION_ROOT.rglob("*.py"):
        if "tests" in path.parts:
            continue
        src = path.read_text()
        for m in re.finditer(r"save_reflection\(", src):
            # crude paren matcher: capture the call's argument span
            depth, i = 0, m.end() - 1
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            args = src[m.end():i]
            if "interaction_id=" in args and "interaction_id=None" not in args:
                if "polarity" not in args and "polarity" not in src[max(0, m.start()-200):m.start()]:
                    offenders.append(f"{path.relative_to(EVOLUTION_ROOT)}:{src[:m.start()].count(chr(10)) + 1}")
    assert not offenders, f"interaction-keyed save_reflection without polarity: {offenders}"

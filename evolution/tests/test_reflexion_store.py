"""
Tests for evolution/reflexion/store.py.
Mocks embed to avoid real API calls.
"""
import pytest

import evolution.db as db_mod
import evolution.providers.embeddings as embed_mod
from evolution.db import open_db
from evolution.reflexion.store import (
    archive_stale_reflections,
    increment_helpful,
    increment_retrieved,
    save_reflection,
)

EMBED_DIM = 768

# Two distinct fixed vectors for semantic distance tests
VECTOR_A = [1.0] + [0.0] * (EMBED_DIM - 1)  # unit vector along dim 0
VECTOR_B = [0.0] * (EMBED_DIM - 1) + [1.0]  # unit vector along dim 767 (far from A)


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    test_db = tmp_path / "test_reflexion.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    yield test_db


@pytest.fixture
def mock_embed_a(monkeypatch):
    """Patch embed to return VECTOR_A for all calls."""
    monkeypatch.setattr(embed_mod, "_provider", None)
    monkeypatch.setattr(
        "evolution.reflexion.store._embed",
        lambda text: VECTOR_A,
    )


@pytest.fixture
def mock_embed_b(monkeypatch):
    """Patch embed to return VECTOR_B for all calls."""
    monkeypatch.setattr(embed_mod, "_provider", None)
    monkeypatch.setattr(
        "evolution.reflexion.store._embed",
        lambda text: VECTOR_B,
    )


@pytest.fixture
def alternating_embed(monkeypatch):
    """Alternate between VECTOR_A and VECTOR_B on successive calls."""
    monkeypatch.setattr(embed_mod, "_provider", None)
    calls = [0]

    def _embed(_text):
        v = VECTOR_A if calls[0] % 2 == 0 else VECTOR_B
        calls[0] += 1
        return v

    monkeypatch.setattr("evolution.reflexion.store._embed", _embed)


# ── save_reflection ───────────────────────────────────────────────────────


def test_save_reflection_returns_id(mock_embed_a):
    rid = save_reflection(
        content="Always check error messages before logging.",
        category="reasoning",
        score_at_gen=0.4,
        group_folder="test-group",
    )
    assert rid is not None
    assert isinstance(rid, str)


def test_save_reflection_persists_to_db(mock_embed_a):
    content = "Use structured outputs for complex tasks."
    rid = save_reflection(content=content, category="tool_use", score_at_gen=0.5)
    conn = open_db()
    row = conn.execute("SELECT * FROM reflections WHERE id = ?", [rid]).fetchone()
    conn.close()
    assert row is not None
    assert row["content"] == content
    assert row["category"] == "tool_use"


def test_save_reflection_duplicate_returns_none(mock_embed_a):
    """Two reflections with the same vector (distance=0) should deduplicate."""
    content = "Verify assumptions before acting."
    rid1 = save_reflection(
        content=content, category="reasoning", score_at_gen=0.3, group_folder="g"
    )
    assert rid1 is not None

    # Same vector → should be detected as duplicate → returns None
    rid2 = save_reflection(
        content=content + " (duplicate)", category="reasoning", score_at_gen=0.3, group_folder="g"
    )
    assert rid2 is None


def test_save_reflection_distinct_vectors_both_saved(alternating_embed):
    """Two reflections with very different vectors (distance >> threshold) should both save."""
    rid1 = save_reflection(
        content="First distinct reflection", category="style", score_at_gen=0.4, group_folder="g"
    )
    rid2 = save_reflection(
        content="Second distinct reflection", category="style", score_at_gen=0.4, group_folder="g"
    )
    assert rid1 is not None
    assert rid2 is not None
    assert rid1 != rid2


def test_save_reflection_cross_group_when_no_folder(mock_embed_a):
    rid = save_reflection(content="Global lesson", category="safety", score_at_gen=0.2)
    conn = open_db()
    row = conn.execute("SELECT group_folder FROM reflections WHERE id = ?", [rid]).fetchone()
    conn.close()
    assert row["group_folder"] is None


# ── increment_retrieved ───────────────────────────────────────────────────


def test_increment_retrieved_increments_counter(mock_embed_a):
    rid = save_reflection(content="Test reflection", category="style", score_at_gen=0.4)
    assert rid is not None

    increment_retrieved(rid)
    increment_retrieved(rid)

    conn = open_db()
    row = conn.execute("SELECT times_retrieved FROM reflections WHERE id = ?", [rid]).fetchone()
    conn.close()
    assert row["times_retrieved"] == 2


# ── increment_helpful ─────────────────────────────────────────────────────


def test_increment_helpful_increments_counter(mock_embed_b):
    rid = save_reflection(content="Helpful reflection", category="reasoning", score_at_gen=0.5)
    assert rid is not None

    increment_helpful(rid)

    conn = open_db()
    row = conn.execute("SELECT times_helpful FROM reflections WHERE id = ?", [rid]).fetchone()
    conn.close()
    assert row["times_helpful"] == 1


# ── archive_stale_reflections ─────────────────────────────────────────────


def test_archive_stale_reflections_dry_run_returns_count(mock_embed_a):
    """With dry_run=True, count stale reflections without modifying them."""
    # Insert a reflection with old timestamp directly
    conn = open_db()
    conn.execute("""
        INSERT INTO reflections (id, timestamp, group_folder, content, category,
                                  score_at_gen, times_retrieved)
        VALUES ('stale-1', datetime('now', '-35 days'), 'g', 'Old lesson', 'style', 0.3, 0)
    """)
    conn.commit()
    conn.close()

    count = archive_stale_reflections(days=30, dry_run=True)
    assert count >= 1

    # Verify not actually archived
    conn = open_db()
    row = conn.execute("SELECT archived_at FROM reflections WHERE id = 'stale-1'").fetchone()
    conn.close()
    assert row["archived_at"] is None


def test_archive_stale_reflections_sets_archived_at(mock_embed_a):
    """With dry_run=False, archived_at is set."""
    conn = open_db()
    conn.execute("""
        INSERT INTO reflections (id, timestamp, group_folder, content, category,
                                  score_at_gen, times_retrieved)
        VALUES ('stale-2', datetime('now', '-40 days'), 'g', 'Very old', 'style', 0.2, 0)
    """)
    conn.commit()
    conn.close()

    count = archive_stale_reflections(days=30, dry_run=False)
    assert count >= 1

    conn = open_db()
    row = conn.execute("SELECT archived_at FROM reflections WHERE id = 'stale-2'").fetchone()
    conn.close()
    assert row["archived_at"] is not None


def test_archive_does_not_touch_retrieved_reflections(mock_embed_a):
    """Reflections with times_retrieved > 0 are NOT archived."""
    conn = open_db()
    conn.execute("""
        INSERT INTO reflections (id, timestamp, group_folder, content, category,
                                  score_at_gen, times_retrieved)
        VALUES ('active-1', datetime('now', '-40 days'), 'g', 'Used lesson', 'style', 0.4, 5)
    """)
    conn.commit()
    conn.close()

    archive_stale_reflections(days=30, dry_run=False)

    conn = open_db()
    row = conn.execute("SELECT archived_at FROM reflections WHERE id = 'active-1'").fetchone()
    conn.close()
    assert row["archived_at"] is None

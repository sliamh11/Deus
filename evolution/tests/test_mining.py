"""Tests for session-correction mining."""
import sqlite3
import pytest
from pathlib import Path

import evolution.config as config_mod
import evolution.db as db_mod
from evolution.storage.provider import StorageRegistry
from evolution.storage.providers.sqlite import SQLiteStorageProvider, _migrated_paths


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test gets a fresh registry, restored after."""
    StorageRegistry.reset()
    yield
    # Re-register the built-in provider so subsequent test files aren't affected
    StorageRegistry.reset()
    reg = StorageRegistry.default()
    reg.register(SQLiteStorageProvider())


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Create a temporary evolution DB with test interactions."""
    db_path = tmp_path / "test_evolution.db"
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", db_path)
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", db_path)

    db = sqlite3.connect(db_path)
    db.execute("""
        CREATE TABLE interactions (
            id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            group_folder TEXT NOT NULL,
            prompt TEXT NOT NULL,
            response TEXT,
            tools_used TEXT,
            latency_ms REAL,
            judge_score REAL,
            judge_dims TEXT,
            eval_suite TEXT DEFAULT 'runtime',
            session_id TEXT,
            domain_presets TEXT,
            user_signal TEXT,
            parse_error INTEGER DEFAULT 0,
            context_tokens INTEGER,
            has_code INTEGER DEFAULT 0,
            correction_mined_at TEXT
        )
    """)
    # Session with correction pattern
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i1", "2026-01-01T00:00:00", "test", "Write a function to sort a list", "def sort...", "[]", 100, 0.5, "{}", "runtime", "s1", None, None, 0, None, 0, None))
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i2", "2026-01-01T00:01:00", "test", "no, try again", None, "[]", 50, None, None, "runtime", "s1", None, None, 0, None, 0, None))
    # Session without correction
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i3", "2026-01-01T00:02:00", "test", "What is the weather?", "It's sunny", "[]", 100, 0.8, "{}", "runtime", "s2", None, None, 0, None, 0, None))
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i4", "2026-01-01T00:03:00", "test", "thanks", None, "[]", 50, None, None, "runtime", "s2", None, None, 0, None, 0, None))
    # Interaction with existing signal (should NOT be overwritten)
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i5", "2026-01-01T00:04:00", "test", "Do something", "done", "[]", 100, 0.3, "{}", "runtime", "s3", None, 'negative', 0, None, 0, None))
    db.execute("INSERT INTO interactions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
               ("i6", "2026-01-01T00:05:00", "test", "wrong, try again", None, "[]", 50, None, None, "runtime", "s3", None, None, 0, None, 0, None))
    db.commit()
    db.close()

    # Register the provider so get_storage() can resolve it
    reg = StorageRegistry.default()
    reg.register(SQLiteStorageProvider())
    _migrated_paths.discard(str(db_path))

    return db_path


def test_is_correction():
    """Test correction vocabulary matching."""
    from evolution.mining import _is_correction
    assert _is_correction("no, try again") is True
    assert _is_correction("actually, I meant something else") is True
    assert _is_correction("shorter") is True
    assert _is_correction("that's not what I wanted") is True
    assert _is_correction("Hello, how are you?") is False
    assert _is_correction("What is the weather?") is False
    assert _is_correction("thanks") is False


def test_mine_corrections_dry_run(test_db):
    """Test dry-run doesn't modify DB."""
    from evolution.mining import mine_corrections
    result = mine_corrections(dry_run=True)
    assert result["matched"] >= 1  # "no, try again" should match
    assert result["updated"] == 0  # dry-run: no updates

    # Verify DB unchanged
    db = sqlite3.connect(test_db)
    nulls = db.execute("SELECT COUNT(*) FROM interactions WHERE user_signal IS NULL").fetchone()[0]
    db.close()
    # i1, i2, i3, i4, i6 should still be NULL (i5 has 'negative')
    assert nulls == 5


def test_mine_corrections_updates(test_db):
    """Test actual mining updates user_signal."""
    from evolution.mining import mine_corrections
    result = mine_corrections(dry_run=False)
    assert result["matched"] >= 1
    assert result["updated"] >= 1

    # Verify i1 got labeled (its follow-up i2 says "try again")
    db = sqlite3.connect(test_db)
    row = db.execute("SELECT user_signal, correction_mined_at FROM interactions WHERE id = 'i1'").fetchone()
    assert row[0] == "correction"
    assert row[1] is not None  # correction_mined_at should be set
    db.close()


def test_mine_corrections_preserves_existing_signal(test_db):
    """Test that existing user_signal values are not overwritten."""
    from evolution.mining import mine_corrections
    mine_corrections(dry_run=False)

    db = sqlite3.connect(test_db)
    row = db.execute("SELECT user_signal FROM interactions WHERE id = 'i5'").fetchone()
    assert row[0] == "negative"  # Should NOT be overwritten to 'correction'
    db.close()

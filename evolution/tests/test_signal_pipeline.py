"""Tests for the has_code column and signal pipeline additions."""


def test_has_code_column_migration():
    """Verify has_code column exists after migration."""
    from evolution.storage import get_storage
    store = get_storage()
    db = store._connect()
    # Column should exist (migration adds it)
    row = db.execute("PRAGMA table_info(interactions)").fetchall()
    col_names = [r[1] for r in row]
    assert "has_code" in col_names
    db.close()


def test_has_code_updatable():
    """Verify has_code is in the updatable columns set."""
    from evolution.storage.providers.sqlite import _UPDATABLE_INTERACTION_COLS
    assert "has_code" in _UPDATABLE_INTERACTION_COLS


def test_user_signal_updatable():
    """Verify user_signal is in the updatable columns set (needed for Phase 5 mining)."""
    from evolution.storage.providers.sqlite import _UPDATABLE_INTERACTION_COLS
    assert "user_signal" in _UPDATABLE_INTERACTION_COLS

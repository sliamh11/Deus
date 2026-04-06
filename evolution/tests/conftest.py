"""
Shared test fixtures for evolution tests.

Patches DB_PATH in both evolution.db and evolution.config so that
the storage provider layer (which reads config.DB_PATH lazily)
and the legacy open_db() shim both use the test database.
"""
import pytest

import evolution.config as config_mod
import evolution.db as db_mod


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file for both db.py and the storage provider."""
    test_db_path = tmp_path / "test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db_path)
    monkeypatch.setattr(config_mod, "DB_PATH", test_db_path)
    return test_db_path

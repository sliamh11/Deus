"""
Tests for evolution/db.py — open_db, migration, and vector helpers.
Uses a real temp file (not :memory:) because sqlite_vec requires real files.
"""
import struct
from pathlib import Path

import pytest

import evolution.db as db_mod
from evolution.db import deserialize_vec, open_db, serialize_vec


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file for every test."""
    test_db = tmp_path / "test_memory.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    yield test_db


# ── open_db / migrate ─────────────────────────────────────────────────────


def test_open_db_creates_file(tmp_path, monkeypatch):
    test_db = tmp_path / "subdir" / "memory.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    conn = open_db()
    conn.close()
    assert test_db.exists()


def test_open_db_creates_interactions_table():
    conn = open_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "interactions" in tables


def test_open_db_creates_reflections_table():
    conn = open_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "reflections" in tables


def test_open_db_creates_prompt_artifacts_table():
    conn = open_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "prompt_artifacts" in tables


def test_open_db_is_idempotent():
    """Calling open_db twice on same path should not error."""
    conn1 = open_db()
    conn1.close()
    conn2 = open_db()
    conn2.close()


def test_interactions_has_domain_presets_column():
    conn = open_db()
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(interactions)").fetchall()
    }
    conn.close()
    assert "domain_presets" in cols


def test_interactions_has_user_signal_column():
    conn = open_db()
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(interactions)").fetchall()
    }
    conn.close()
    assert "user_signal" in cols


def test_reflections_has_archived_at_column():
    conn = open_db()
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(reflections)").fetchall()
    }
    conn.close()
    assert "archived_at" in cols


# ── Vector helpers ────────────────────────────────────────────────────────


def test_serialize_vec_roundtrip():
    original = [1.0, 2.5, -3.14, 0.0]
    blob = serialize_vec(original)
    recovered = deserialize_vec(blob)
    assert len(recovered) == len(original)
    for a, b in zip(original, recovered):
        assert abs(a - b) < 1e-5


def test_serialize_vec_returns_bytes():
    vec = [0.1, 0.2, 0.3]
    result = serialize_vec(vec)
    assert isinstance(result, bytes)
    # 4 bytes per float32
    assert len(result) == len(vec) * 4


def test_serialize_vec_length():
    dim = 768
    vec = [0.0] * dim
    blob = serialize_vec(vec)
    assert len(blob) == dim * 4

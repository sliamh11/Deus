"""
Tests for evolution/cli.py::cmd_log_interaction.

Mocks the judge (make_runtime_judge) and embed function to avoid real API calls.
Uses a temp DB via monkeypatching evolution.db.DB_PATH.
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import evolution.db as db_mod
import evolution.providers.embeddings as embed_mod
from evolution.db import open_db
from evolution.judge.base import JudgeResult


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    test_db = tmp_path / "cli_test.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    yield test_db


@pytest.fixture
def mock_judge(monkeypatch):
    """Return a fake judge that produces a known score."""
    result = JudgeResult(
        score=0.9,
        quality=0.9,
        safety=1.0,
        tool_use=1.0,
        personalization=0.8,
        rationale="Great response",
    )
    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(return_value=result)

    monkeypatch.setattr(
        "evolution.judge.gemini_judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )
    return judge_mock, result


@pytest.fixture
def mock_low_score_judge(monkeypatch):
    """Judge that produces a low score to trigger reflection generation."""
    result = JudgeResult(
        score=0.3,
        quality=0.3,
        safety=1.0,
        tool_use=0.5,
        personalization=0.4,
        rationale="Poor response",
    )
    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(return_value=result)

    monkeypatch.setattr(
        "evolution.judge.gemini_judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )
    return judge_mock, result


@pytest.fixture
def mock_embed(monkeypatch):
    """Return a fixed 768-float vector for all embed calls."""
    vec = [0.1] * 768
    monkeypatch.setattr(embed_mod, "_provider", None)
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: vec)
    return vec


@pytest.fixture
def mock_reflection_generator(monkeypatch):
    """Mock generate_reflection to avoid real API calls."""
    monkeypatch.setattr(
        "evolution.reflexion.generator.generate_reflection",
        lambda **kwargs: ("Generated reflection content", "reasoning"),
    )
    monkeypatch.setattr(
        "evolution.reflexion.generator.generate_positive_reflection",
        lambda **kwargs: ("Positive reflection content", "style"),
    )


# ── cmd_log_interaction tests ─────────────────────────────────────────────


def test_cmd_log_interaction_persists_interaction(mock_judge, mock_embed, mock_reflection_generator):
    """cmd_log_interaction should store the interaction in the DB."""
    from evolution.cli import cmd_log_interaction

    payload = json.dumps({
        "prompt": "What is 2 + 2?",
        "response": "4",
        "group_folder": "test-group",
        "latency_ms": 150.0,
    })
    cmd_log_interaction(payload)

    conn = open_db()
    rows = conn.execute("SELECT * FROM interactions WHERE group_folder = 'test-group'").fetchall()
    conn.close()
    assert len(rows) >= 1
    assert rows[0]["prompt"] == "What is 2 + 2?"


def test_cmd_log_interaction_invalid_json_does_not_crash():
    """Invalid JSON should be handled gracefully (no exception)."""
    from evolution.cli import cmd_log_interaction

    cmd_log_interaction("this is not json at all {{{")
    # If we reach here without exception, test passes


def test_cmd_log_interaction_writes_judge_score(mock_judge, mock_embed, mock_reflection_generator):
    """After running, the interaction should have a judge score."""
    from evolution.cli import cmd_log_interaction

    iid = "test-iid-score"
    payload = json.dumps({
        "id": iid,
        "prompt": "Explain recursion",
        "response": "A function that calls itself",
        "group_folder": "g",
    })
    cmd_log_interaction(payload)

    conn = open_db()
    row = conn.execute(
        "SELECT judge_score FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row is not None
    # Score should have been written
    assert row["judge_score"] is not None
    assert abs(row["judge_score"] - 0.9) < 0.01


def test_cmd_log_interaction_low_score_generates_reflection(
    mock_low_score_judge, mock_embed, mock_reflection_generator
):
    """Low-scoring interaction should trigger a reflection."""
    from evolution.cli import cmd_log_interaction

    iid = "test-iid-low"
    payload = json.dumps({
        "id": iid,
        "prompt": "How do I delete all files?",
        "response": "rm -rf /",
        "group_folder": "g",
    })
    cmd_log_interaction(payload)

    conn = open_db()
    refs = conn.execute(
        "SELECT * FROM reflections WHERE interaction_id = ?", [iid]
    ).fetchall()
    conn.close()
    assert len(refs) >= 1


def test_cmd_log_interaction_stores_domain_presets(mock_judge, mock_embed, mock_reflection_generator):
    """Domain presets should be stored in the interaction row."""
    from evolution.cli import cmd_log_interaction

    iid = "test-iid-domains"
    payload = json.dumps({
        "id": iid,
        "prompt": "Debug my code",
        "response": "Fixed",
        "group_folder": "g",
        "domain_presets": ["engineering"],
    })
    cmd_log_interaction(payload)

    conn = open_db()
    row = conn.execute(
        "SELECT domain_presets FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row is not None
    parsed = json.loads(row["domain_presets"])
    assert "engineering" in parsed


def test_cmd_log_interaction_with_explicit_interaction_id(mock_judge, mock_embed, mock_reflection_generator):
    """Explicit interaction ID should be preserved."""
    from evolution.cli import cmd_log_interaction

    explicit_id = "explicit-iid-001"
    payload = json.dumps({
        "id": explicit_id,
        "prompt": "Test",
        "response": "Response",
        "group_folder": "g",
    })
    cmd_log_interaction(payload)

    conn = open_db()
    row = conn.execute(
        "SELECT id FROM interactions WHERE id = ?", [explicit_id]
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["id"] == explicit_id

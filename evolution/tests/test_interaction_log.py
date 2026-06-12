"""
Tests for evolution/ilog/interaction_log.py.
"""
import json

import pytest

import evolution.config as config_mod
import evolution.db as db_mod
from evolution.db import open_db
from evolution.ilog.interaction_log import (
    get_previous_in_session,
    get_recent,
    log_interaction,
    score_trend,
    update_score,
)


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    test_db = tmp_path / "test_ilog.db"
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")
    yield test_db


def test_log_interaction_returns_id():
    iid = log_interaction(
        prompt="Hello",
        response="Hi",
        group_folder="test-group",
        latency_ms=100.0,
    )
    assert isinstance(iid, str)
    assert len(iid) > 0


def test_log_interaction_persists_to_db():
    iid = log_interaction(
        prompt="What is 2+2?",
        response="4",
        group_folder="test-group",
    )
    conn = open_db()
    row = conn.execute(
        "SELECT * FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row is not None
    assert row["prompt"] == "What is 2+2?"
    assert row["response"] == "4"
    assert row["group_folder"] == "test-group"


def test_log_interaction_with_explicit_id():
    explicit_id = "my-custom-id-001"
    returned = log_interaction(
        prompt="Test",
        response=None,
        group_folder="g",
        interaction_id=explicit_id,
    )
    assert returned == explicit_id


def test_log_interaction_stores_domain_presets():
    iid = log_interaction(
        prompt="Debug this code",
        response="Fixed",
        group_folder="g",
        domain_presets=["engineering"],
    )
    conn = open_db()
    row = conn.execute(
        "SELECT domain_presets FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    parsed = json.loads(row["domain_presets"])
    assert "engineering" in parsed


def test_log_interaction_stores_user_signal():
    iid = log_interaction(
        prompt="perfect",
        response=None,
        group_folder="g",
        user_signal="positive",
    )
    conn = open_db()
    row = conn.execute(
        "SELECT user_signal FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row["user_signal"] == "positive"


def test_log_interaction_stores_metrics():
    iid = log_interaction(
        prompt="Run the tests",
        response="12 passed",
        group_folder="g",
        metrics={"tests_passed": 12, "breaks": ["expected"]},
    )
    conn = open_db()
    row = conn.execute(
        "SELECT metrics FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert json.loads(row["metrics"]) == {"tests_passed": 12, "breaks": ["expected"]}


def test_log_interaction_without_metrics_stores_null():
    iid = log_interaction(prompt="p", response="r", group_folder="g")
    conn = open_db()
    row = conn.execute(
        "SELECT metrics FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row["metrics"] is None


def test_log_interaction_rejects_invalid_metrics():
    with pytest.raises(ValueError, match="scalar"):
        log_interaction(
            prompt="p", response="r", group_folder="g",
            metrics={"nested": {"a": 1}},
        )


def test_update_score_writes_score_and_dims():
    iid = log_interaction(prompt="Test", response="Resp", group_folder="g")
    dims = {"quality": 0.9, "safety": 1.0, "tool_use": 0.8, "personalization": 0.7}
    update_score(iid, 0.85, dims)

    conn = open_db()
    row = conn.execute(
        "SELECT judge_score, judge_dims FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert abs(row["judge_score"] - 0.85) < 1e-5
    parsed_dims = json.loads(row["judge_dims"])
    assert parsed_dims["quality"] == 0.9


# ── LIA-214: credit retrieved reflections at scoring time ────────────────────


def test_update_score_credits_retrieved_reflections_once(monkeypatch):
    """score >= POSITIVE_THRESHOLD credits each retrieved reflection exactly once,
    even when update_score runs again (re-score)."""
    credited: list[str] = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda rid: credited.append(rid),
    )
    iid = log_interaction(
        prompt="p", response="r", group_folder="g",
        retrieved_reflection_ids=["ref-a", "ref-b"],
    )
    update_score(iid, 0.9, {})
    assert sorted(credited) == ["ref-a", "ref-b"]

    # Re-score (e.g. a later judge re-run): the one-shot guard blocks re-credit.
    update_score(iid, 0.95, {})
    assert sorted(credited) == ["ref-a", "ref-b"]


def test_update_score_credits_at_exact_threshold(monkeypatch):
    """Boundary: a score EXACTLY at POSITIVE_THRESHOLD (0.85) credits — the gate
    is strict less-than, so the threshold value itself is positive."""
    credited: list[str] = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda rid: credited.append(rid),
    )
    iid = log_interaction(
        prompt="p", response="r", group_folder="g",
        retrieved_reflection_ids=["ref-edge"],
    )
    update_score(iid, config_mod.POSITIVE_THRESHOLD, {})
    assert credited == ["ref-edge"]


def test_update_score_no_credit_below_threshold(monkeypatch):
    credited: list[str] = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda rid: credited.append(rid),
    )
    iid = log_interaction(
        prompt="p", response="r", group_folder="g",
        retrieved_reflection_ids=["ref-low"],
    )
    update_score(iid, 0.5, {})
    assert credited == []
    # credited_at stays NULL so a later positive score can still credit.
    conn = open_db()
    row = conn.execute(
        "SELECT credited_at FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row["credited_at"] is None


def test_update_score_sub_then_super_threshold_credits_once(monkeypatch):
    """A sub-threshold score followed by a >= threshold re-score credits once."""
    credited: list[str] = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda rid: credited.append(rid),
    )
    iid = log_interaction(
        prompt="p", response="r", group_folder="g",
        retrieved_reflection_ids=["ref-x"],
    )
    update_score(iid, 0.4, {})
    assert credited == []  # below threshold — no claim, no credit
    update_score(iid, 0.92, {})
    assert credited == ["ref-x"]  # now credited
    update_score(iid, 0.99, {})
    assert credited == ["ref-x"]  # still exactly once


def test_update_score_no_credit_when_no_retrieved_ids(monkeypatch):
    """A high score with no retrieved reflections credits nothing and never crashes."""
    credited: list[str] = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda rid: credited.append(rid),
    )
    iid = log_interaction(prompt="p", response="r", group_folder="g")
    update_score(iid, 0.95, {})
    assert credited == []
    # No retrieved IDs → credited_at remains NULL (no spurious claim write).
    conn = open_db()
    row = conn.execute(
        "SELECT credited_at FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert row["credited_at"] is None


def test_log_interaction_persists_retrieved_reflection_ids():
    iid = log_interaction(
        prompt="p", response="r", group_folder="g",
        retrieved_reflection_ids=["a", "b", "c"],
    )
    conn = open_db()
    row = conn.execute(
        "SELECT retrieved_reflection_ids FROM interactions WHERE id = ?", [iid]
    ).fetchone()
    conn.close()
    assert json.loads(row["retrieved_reflection_ids"]) == ["a", "b", "c"]


def test_get_recent_returns_logged_interactions():
    log_interaction(prompt="First", response="A", group_folder="g1")
    log_interaction(prompt="Second", response="B", group_folder="g2")
    results = get_recent(eval_suite="runtime")
    assert len(results) >= 2


def test_get_recent_filters_by_group():
    log_interaction(prompt="For g1", response="A", group_folder="g1")
    log_interaction(prompt="For g2", response="B", group_folder="g2")
    results = get_recent(group_folder="g1", eval_suite=None)
    assert all(r["group_folder"] == "g1" for r in results)


def test_get_recent_filters_by_min_score():
    iid = log_interaction(prompt="Good", response="Resp", group_folder="g")
    update_score(iid, 0.9, {})
    iid2 = log_interaction(prompt="Bad", response="Resp2", group_folder="g")
    update_score(iid2, 0.3, {})

    results = get_recent(min_score=0.7, eval_suite=None)
    scores = [r["judge_score"] for r in results]
    assert all(s >= 0.7 for s in scores)


def test_get_previous_in_session_returns_none_for_empty_session():
    result = get_previous_in_session("nonexistent-session", "some-id")
    assert result is None


def test_get_previous_in_session_returns_previous_interaction():
    sid = "session-abc"
    iid1 = log_interaction(
        prompt="First message", response="R1", group_folder="g", session_id=sid
    )
    iid2 = log_interaction(
        prompt="Second message", response="R2", group_folder="g", session_id=sid
    )
    prev = get_previous_in_session(sid, iid2)
    assert prev is not None
    assert prev["id"] == iid1


def test_score_trend_returns_list():
    result = score_trend(days=30)
    assert isinstance(result, list)

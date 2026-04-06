"""
Tests for evolution/cli.py::cmd_log_interaction.

Mocks the judge (make_runtime_judge) and embed function to avoid real API calls.
Uses a temp DB via monkeypatching evolution.db.DB_PATH.
"""
import asyncio
import json
from pathlib import Path
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
        "evolution.judge.make_runtime_judge",
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
        "evolution.judge.make_runtime_judge",
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


# ── New tests: missing code paths ────────────────────────────────────────────


@pytest.fixture
def mock_high_score_judge(monkeypatch):
    """Judge that produces a high score (>= POSITIVE_THRESHOLD) to trigger positive reflection."""
    result = JudgeResult(
        score=0.92,
        quality=0.95,
        safety=1.0,
        tool_use=0.9,
        personalization=0.85,
        rationale="Excellent response",
    )
    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "evolution.judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )
    return judge_mock, result


@pytest.fixture
def mock_mid_score_judge(monkeypatch):
    """Judge that produces a score between thresholds (no reflection triggered by score alone)."""
    result = JudgeResult(
        score=0.75,
        quality=0.75,
        safety=1.0,
        tool_use=0.7,
        personalization=0.7,
        rationale="Decent response",
    )
    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "evolution.judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )
    return judge_mock, result


# 1. Positive reflection path: score >= POSITIVE_THRESHOLD → generate_positive_reflection called

def test_cmd_log_interaction_positive_score_generates_positive_reflection(
    mock_high_score_judge, mock_embed, monkeypatch
):
    """High-scoring interaction (>= POSITIVE_THRESHOLD) should call generate_positive_reflection."""
    from evolution.cli import cmd_log_interaction

    positive_gen_called = []

    def fake_positive_reflection(**kwargs):
        positive_gen_called.append(kwargs)
        return ("Positive content", "style")

    def fake_reflection(**kwargs):
        return ("Negative content", "reasoning")

    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", fake_reflection)
    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", fake_positive_reflection)
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)

    iid = "test-iid-positive"
    payload = json.dumps({
        "id": iid,
        "prompt": "Great prompt",
        "response": "Stellar answer",
        "group_folder": "g",
    })
    cmd_log_interaction(payload)

    assert len(positive_gen_called) >= 1, "generate_positive_reflection should have been called"

    # Verify a reflection row was saved with positive content
    conn = open_db()
    refs = conn.execute(
        "SELECT content FROM reflections WHERE interaction_id = ?", [iid]
    ).fetchall()
    conn.close()
    assert any("Positive" in r["content"] for r in refs)


# 2. User signal — positive: marks previous interaction positive

def test_cmd_log_interaction_user_signal_positive_generates_positive_reflection_for_prev(
    mock_mid_score_judge, mock_embed, monkeypatch
):
    """user_signal='positive' should call generate_positive_reflection on the previous interaction."""
    from evolution.ilog.interaction_log import log_interaction
    from evolution.cli import cmd_log_interaction

    # Seed a previous interaction with a known judge_score
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)
    session_id = "session-signal-pos"
    prev_iid = log_interaction(
        prompt="Previous prompt",
        response="Previous response",
        group_folder="g",
        session_id=session_id,
    )
    # Manually set the judge_score on the previous interaction so the guard passes
    conn = open_db()
    conn.execute("UPDATE interactions SET judge_score = 0.8 WHERE id = ?", [prev_iid])
    conn.commit()
    conn.close()

    positive_calls = []

    def fake_positive(**kwargs):
        positive_calls.append(kwargs)
        return ("Pos content", "style")

    def fake_neg(**kwargs):
        return ("Neg content", "reasoning")

    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", fake_positive)
    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", fake_neg)

    payload = json.dumps({
        "prompt": "Current prompt",
        "response": "Current response",
        "group_folder": "g",
        "session_id": session_id,
        "user_signal": "positive",
    })
    cmd_log_interaction(payload)

    # generate_positive_reflection should have been called at least once for the previous interaction
    assert len(positive_calls) >= 1


# 3. User signal — negative/null: guard only fires if previous has judge_score

def test_cmd_log_interaction_user_signal_negative_generates_reflection_for_prev(
    mock_mid_score_judge, mock_embed, monkeypatch
):
    """user_signal='negative' should call generate_reflection on the previous interaction when it has a judge_score."""
    from evolution.ilog.interaction_log import log_interaction
    from evolution.cli import cmd_log_interaction

    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)
    session_id = "session-signal-neg"
    prev_iid = log_interaction(
        prompt="Prev prompt neg",
        response="Prev response neg",
        group_folder="g",
        session_id=session_id,
    )
    # Give the previous interaction a judge_score to pass the guard
    conn = open_db()
    conn.execute("UPDATE interactions SET judge_score = 0.4 WHERE id = ?", [prev_iid])
    conn.commit()
    conn.close()

    neg_calls = []

    def fake_neg(**kwargs):
        neg_calls.append(kwargs)
        return ("Neg content", "reasoning")

    def fake_pos(**kwargs):
        return ("Pos content", "style")

    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", fake_neg)
    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", fake_pos)

    payload = json.dumps({
        "prompt": "Current prompt",
        "response": "Current response",
        "group_folder": "g",
        "session_id": session_id,
        "user_signal": "negative",
    })
    cmd_log_interaction(payload)

    # generate_reflection should have been called for the previous interaction (user signal path)
    assert len(neg_calls) >= 1


def test_cmd_log_interaction_user_signal_no_prev_judge_score_uses_fallback(
    mock_mid_score_judge, mock_embed, monkeypatch
):
    """user_signal present but previous interaction has no judge_score → uses fallback score."""
    from evolution.ilog.interaction_log import log_interaction
    from evolution.cli import cmd_log_interaction

    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)
    session_id = "session-signal-no-score"
    prev_iid = log_interaction(
        prompt="Prev prompt",
        response="Prev response",
        group_folder="g",
        session_id=session_id,
    )
    # Leave judge_score as NULL — code uses fallback score (0.8 for positive)

    signal_gen_calls = []

    def tracking_fake_pos(**kwargs):
        signal_gen_calls.append(("positive", kwargs))
        return ("Pos", "style")

    def tracking_fake_neg(**kwargs):
        signal_gen_calls.append(("negative", kwargs))
        return ("Neg", "reasoning")

    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", tracking_fake_pos)
    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", tracking_fake_neg)

    payload = json.dumps({
        "prompt": "Current",
        "response": "Response",
        "group_folder": "g",
        "session_id": session_id,
        "user_signal": "positive",
    })
    cmd_log_interaction(payload)

    # Positive signal should still generate a reflection for prev interaction using fallback score
    prev_calls = [
        (sig, kw) for sig, kw in signal_gen_calls if kw.get("prompt") == "Prev prompt"
    ]
    assert len(prev_calls) >= 1, "expected reflection for prev interaction with fallback score"
    assert prev_calls[0][0] == "positive"
    assert prev_calls[0][1]["score"] == 0.8  # fallback score for positive signal


# 4. Feedback loop — increment_helpful called for each retrieved_reflection_id when score is high

def test_cmd_log_interaction_feedback_loop_increments_helpful(
    mock_high_score_judge, mock_embed, monkeypatch
):
    """retrieved_reflection_ids + high score → increment_helpful called for each id."""
    from evolution.cli import cmd_log_interaction

    incremented = []

    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", lambda **kw: ("c", "cat"))
    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", lambda **kw: ("c", "cat"))
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda ref_id: incremented.append(ref_id),
    )

    ref_ids = ["ref-aaa", "ref-bbb", "ref-ccc"]
    payload = json.dumps({
        "prompt": "Some prompt",
        "response": "Some response",
        "group_folder": "g",
        "retrieved_reflection_ids": ref_ids,
    })
    cmd_log_interaction(payload)

    for rid in ref_ids:
        assert rid in incremented, f"increment_helpful not called for {rid}"


def test_cmd_log_interaction_feedback_loop_not_called_for_low_score(
    mock_low_score_judge, mock_embed, mock_reflection_generator, monkeypatch
):
    """retrieved_reflection_ids + LOW score → increment_helpful NOT called."""
    from evolution.cli import cmd_log_interaction

    incremented = []
    monkeypatch.setattr(
        "evolution.reflexion.store.increment_helpful",
        lambda ref_id: incremented.append(ref_id),
    )
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)

    payload = json.dumps({
        "prompt": "Some prompt",
        "response": "Bad answer",
        "group_folder": "g",
        "retrieved_reflection_ids": ["ref-zzz"],
    })
    cmd_log_interaction(payload)

    assert "ref-zzz" not in incremented, "increment_helpful should not be called for low-score interactions"


# 5. Judge exception: interaction still saved, no crash

def test_cmd_log_interaction_judge_exception_does_not_crash(monkeypatch, mock_embed):
    """If judge raises an exception, the interaction is still saved and no exception propagates."""
    from evolution.cli import cmd_log_interaction
    import evolution.providers.embeddings as embed_mod_inner

    monkeypatch.setattr(embed_mod_inner, "_provider", None)
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)

    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(side_effect=RuntimeError("Judge API exploded"))
    monkeypatch.setattr(
        "evolution.judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )

    iid = "test-iid-judge-exc"
    payload = json.dumps({
        "id": iid,
        "prompt": "Some prompt",
        "response": "Some response",
        "group_folder": "g",
    })

    # Should not raise
    cmd_log_interaction(payload)

    # Interaction still saved
    conn = open_db()
    row = conn.execute("SELECT id, judge_score FROM interactions WHERE id = ?", [iid]).fetchone()
    conn.close()
    assert row is not None, "Interaction should be persisted even when judge raises"
    assert row["judge_score"] is None, "Judge score should remain NULL after exception"


# 6. Output format verification

def test_cmd_log_interaction_outputs_ok_json(
    mock_judge, mock_embed, mock_reflection_generator, capsys
):
    """cmd_log_interaction prints {"id": ..., "status": "ok"} to stdout on success."""
    from evolution.cli import cmd_log_interaction

    iid = "test-iid-output"
    payload = json.dumps({
        "id": iid,
        "prompt": "Hello",
        "response": "Hi",
        "group_folder": "g",
    })
    cmd_log_interaction(payload)

    captured = capsys.readouterr()
    # Last line is the JSON status — earlier lines may contain auto-trigger status messages
    output = json.loads(captured.out.strip().split('\n')[-1])
    assert output["id"] == iid
    assert output["status"] == "ok"


def test_cmd_log_interaction_outputs_error_json_on_invalid_input(capsys):
    """cmd_log_interaction prints {"error": "..."} to stdout on JSON parse failure."""
    from evolution.cli import cmd_log_interaction

    cmd_log_interaction("{not valid json}")

    captured = capsys.readouterr()
    output = json.loads(captured.out.strip())
    assert "error" in output


# ── main() dispatch tests ─────────────────────────────────────────────────────


def test_main_status_exits_zero():
    """python -m evolution.cli status should exit with code 0."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "evolution.cli", "status"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, (
        f"Expected exit 0, got {result.returncode}.\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_main_log_interaction_dispatch(monkeypatch, tmp_path):
    """main() dispatches log_interaction subcommand and prints ok JSON."""
    import evolution.db as db_mod_inner
    import evolution.providers.embeddings as embed_mod_inner

    # We can't easily monkeypatch across subprocess, so call main() directly
    test_db = tmp_path / "main_dispatch_test.db"
    monkeypatch.setattr(db_mod_inner, "DB_PATH", test_db)
    monkeypatch.setattr(embed_mod_inner, "_provider", None)
    monkeypatch.setattr("evolution.reflexion.store._embed", lambda text: [0.1] * 768)

    result = JudgeResult(
        score=0.88,
        quality=0.88,
        safety=1.0,
        tool_use=0.9,
        personalization=0.85,
        rationale="Good",
    )
    judge_mock = MagicMock()
    judge_mock.a_evaluate = AsyncMock(return_value=result)
    monkeypatch.setattr(
        "evolution.judge.make_runtime_judge",
        lambda *args, **kwargs: judge_mock,
    )
    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", lambda **kw: ("c", "cat"))
    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", lambda **kw: ("c", "cat"))

    import sys
    from io import StringIO
    from evolution.cli import main

    iid = "main-dispatch-iid"
    payload = json.dumps({"id": iid, "prompt": "Hello", "response": "Hi", "group_folder": "g"})

    captured = StringIO()
    monkeypatch.setattr("sys.argv", ["evolution.cli", "log_interaction", payload])
    monkeypatch.setattr("sys.stdout", captured)

    main()

    output_text = captured.getvalue()
    # restore stdout before assertions so pytest can display output
    monkeypatch.undo()
    # Last line is the JSON status — earlier lines may contain auto-trigger status messages
    output = json.loads(output_text.strip().split('\n')[-1])
    assert output["id"] == iid
    assert output["status"] == "ok"


def test_main_unknown_subcommand_exits_nonzero():
    """main() with an unknown subcommand should exit non-zero (argparse behaviour)."""
    import subprocess, sys
    result = subprocess.run(
        [sys.executable, "-m", "evolution.cli", "nonexistent_subcommand"],
        capture_output=True,
        text=True,
        cwd=str(Path(__file__).resolve().parents[2]),
    )
    assert result.returncode != 0

"""
Tests for evolution/backfill.py — exchange-pair chunking, chunk_stats, context_window.
"""
import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _make_entry(role: str, text: str) -> dict:
    return {"type": role, "message": {"content": text}}


# ── _extract_pairs ────────────────────────────────────────────────────────


def test_extract_pairs_yields_exchange_pairs(tmp_path):
    """Each yielded pair contains exactly one user turn and its following assistant turn."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "What is the capital of France?"),
        _make_entry("assistant", "The capital of France is Paris."),
        _make_entry("user", "What is the capital of Germany?"),
        _make_entry("assistant", "The capital of Germany is Berlin."),
    ])

    pairs = list(_extract_pairs(fpath))
    assert len(pairs) == 2
    assert pairs[0]["prompt"] == "What is the capital of France?"
    assert "Paris" in pairs[0]["response"]
    assert pairs[1]["prompt"] == "What is the capital of Germany?"
    assert "Berlin" in pairs[1]["response"]


def test_extract_pairs_pair_index_is_sequential(tmp_path):
    """pair_index increments from 0 for each yielded pair."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "First question here please"),
        _make_entry("assistant", "First answer to the question."),
        _make_entry("user", "Second question here please"),
        _make_entry("assistant", "Second answer to the question."),
    ])

    pairs = list(_extract_pairs(fpath))
    assert [p["pair_index"] for p in pairs] == [0, 1]


def test_extract_pairs_skips_short_prompts(tmp_path):
    """Prompts shorter than _MIN_PROMPT_LEN are skipped."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "hi"),                          # too short
        _make_entry("assistant", "Hello! How can I help?"),
        _make_entry("user", "What is the weather today?"),  # valid
        _make_entry("assistant", "I don't have real-time weather data."),
    ])

    pairs = list(_extract_pairs(fpath))
    assert len(pairs) == 1
    assert "weather" in pairs[0]["prompt"]


def test_extract_pairs_skips_error_responses(tmp_path):
    """Responses starting with error prefixes are skipped."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "Please do something for me today"),
        _make_entry("assistant", "API Error: rate limit exceeded"),  # skip
        _make_entry("user", "What is two plus two exactly?"),
        _make_entry("assistant", "Two plus two equals four."),
    ])

    pairs = list(_extract_pairs(fpath))
    assert len(pairs) == 1
    assert "two plus two" in pairs[0]["prompt"].lower()


def test_extract_pairs_with_context_window(tmp_path):
    """context_window > 0 includes preceding messages as 'context' field."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "Tell me about Python programming language"),
        _make_entry("assistant", "Python is a high-level programming language."),
        _make_entry("user", "What about its type system?"),
        _make_entry("assistant", "Python uses dynamic typing by default."),
    ])

    pairs = list(_extract_pairs(fpath, context_window=2))
    assert len(pairs) == 2

    # First pair has no prior context
    assert pairs[0].get("context") == [] or "context" not in pairs[0] or pairs[0]["context"] == []

    # Second pair should include the first exchange as context
    assert "context" in pairs[1]
    ctx_texts = [c["text"] for c in pairs[1]["context"]]
    assert any("Python" in t for t in ctx_texts)


def test_extract_pairs_no_context_window_by_default(tmp_path):
    """Without context_window, no 'context' key in yielded pairs."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "Tell me about Python programming language"),
        _make_entry("assistant", "Python is a high-level programming language."),
    ])

    pairs = list(_extract_pairs(fpath))
    assert len(pairs) == 1
    assert "context" not in pairs[0]


def test_extract_pairs_handles_corrupt_jsonl(tmp_path):
    """Corrupt .jsonl files return zero pairs (no exception)."""
    from evolution.backfill import _extract_pairs

    fpath = tmp_path / "session.jsonl"
    fpath.write_text("not valid json\n{broken")

    pairs = list(_extract_pairs(fpath))
    assert pairs == []


# ── collect_pairs + chunk_stats ───────────────────────────────────────────


def _make_session_dir(base: Path, project: str = "proj") -> Path:
    """Create the expected .claude/projects/<proj>/ directory structure."""
    d = base / "sessions" / "group" / ".claude" / "projects" / project
    d.mkdir(parents=True)
    return d


def test_chunk_stats_prints_summary(tmp_path, capsys):
    """--chunk-stats prints file count, pair count, and avg lengths."""
    from evolution.backfill import collect_pairs

    session_dir = _make_session_dir(tmp_path)
    fpath = session_dir / "abc123.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "What is the capital of France?"),
        _make_entry("assistant", "The capital of France is Paris."),
    ])

    collect_pairs(tmp_path / "sessions", chunk_stats=True)
    output = capsys.readouterr().out

    assert "Exchange-pair chunk stats" in output
    assert "files scanned" in output
    assert "total pairs extracted" in output
    assert "avg prompt length" in output


def test_chunk_stats_shows_zero_for_empty_dir(tmp_path, capsys):
    """--chunk-stats with no sessions prints zero counts."""
    from evolution.backfill import collect_pairs

    (tmp_path / "sessions").mkdir()
    collect_pairs(tmp_path / "sessions", chunk_stats=True)
    output = capsys.readouterr().out

    assert "0" in output


def test_collect_pairs_threads_context_window(tmp_path):
    """context_window parameter is forwarded from collect_pairs to _extract_pairs."""
    from evolution.backfill import collect_pairs

    session_dir = _make_session_dir(tmp_path)
    fpath = session_dir / "ctx_test.jsonl"
    _write_jsonl(fpath, [
        _make_entry("user", "Tell me about Python programming language"),
        _make_entry("assistant", "Python is a high-level programming language."),
        _make_entry("user", "What about its type system in detail?"),
        _make_entry("assistant", "Python uses dynamic typing by default."),
    ])

    # Without context_window: no context field
    pairs_no_ctx = collect_pairs(tmp_path / "sessions", context_window=0)
    assert all("context" not in p for p in pairs_no_ctx)

    # With context_window=2: second pair should have context from prior exchange
    pairs_with_ctx = collect_pairs(tmp_path / "sessions", context_window=2)
    assert len(pairs_with_ctx) == 2
    # First pair may have empty context (nothing before it)
    # Second pair should have context
    second = pairs_with_ctx[1]
    assert "context" in second
    ctx_texts = [c["text"] for c in second["context"]]
    assert any("Python" in t for t in ctx_texts)

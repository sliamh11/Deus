"""
Tests for scripts/memory_benchmark.py

Covers: metric calculations, output parsing, result formatting.
Subprocess calls are mocked throughout — no real indexer or DB needed.
"""
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Import target module ──────────────────────────────────────────────────────

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
for _p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import memory_benchmark as mb


# ── recall_at_k ──────────────────────────────────────────────────────────────


def test_recall_at_k_all_hits():
    hits = [True, True, True]
    assert mb.recall_at_k(hits, k=3) == pytest.approx(1.0)


def test_recall_at_k_no_hits():
    hits = [False, False, False]
    assert mb.recall_at_k(hits, k=3) == pytest.approx(0.0)


def test_recall_at_k_partial():
    hits = [True, False, True, False]
    assert mb.recall_at_k(hits, k=3) == pytest.approx(0.5)


def test_recall_at_k_empty():
    assert mb.recall_at_k([], k=5) == pytest.approx(0.0)


def test_recall_at_k_single_hit():
    assert mb.recall_at_k([True], k=1) == pytest.approx(1.0)


# ── mean_reciprocal_rank ──────────────────────────────────────────────────────


def test_mrr_all_rank_one():
    ranks = [1, 1, 1]
    assert mb.mean_reciprocal_rank(ranks) == pytest.approx(1.0)


def test_mrr_with_nones():
    # 1/1 + 1/2 + 0 = 0.5
    ranks = [1, 2, None]
    assert mb.mean_reciprocal_rank(ranks) == pytest.approx((1.0 + 0.5) / 3)


def test_mrr_all_none():
    assert mb.mean_reciprocal_rank([None, None]) == pytest.approx(0.0)


def test_mrr_empty():
    assert mb.mean_reciprocal_rank([]) == pytest.approx(0.0)


def test_mrr_mixed():
    # ranks: 2, 5, None → (0.5 + 0.2 + 0) / 3
    ranks = [2, 5, None]
    expected = (0.5 + 0.2 + 0.0) / 3
    assert mb.mean_reciprocal_rank(ranks) == pytest.approx(expected, rel=1e-5)


# ── _parse_query_output ───────────────────────────────────────────────────────


def test_parse_query_output_single_result():
    output = (
        "## Relevant Past Sessions\n"
        "- [2024-06-15 | my session] — some tldr\n"
        "  (full log: /vault/Session-Logs/my-session.md)\n"
    )
    paths = mb._parse_query_output(output)
    assert paths == ["/vault/Session-Logs/my-session.md"]


def test_parse_query_output_multiple_results():
    output = (
        "## Relevant Past Sessions\n"
        "- [2024-06-15 | session a] — tldr\n"
        "  (full log: /vault/Session-Logs/session-a.md)\n"
        "- [2024-06-16 | session b] — tldr\n"
        "  (full log: /vault/Session-Logs/session-b.md)\n"
    )
    paths = mb._parse_query_output(output)
    assert len(paths) == 2
    assert "/vault/Session-Logs/session-a.md" in paths
    assert "/vault/Session-Logs/session-b.md" in paths


def test_parse_query_output_empty():
    assert mb._parse_query_output("") == []


def test_parse_query_output_no_full_log_lines():
    output = "## Relevant Past Sessions\n- [2024-06-15 | session a] — tldr\n"
    assert mb._parse_query_output(output) == []


# ── _session_stem_to_id ───────────────────────────────────────────────────────


def test_session_stem_to_id_match():
    result_paths = ["/tmp/bm/session_0001.md", "/tmp/bm/session_0003.md"]
    session_stems = ["session_0000", "session_0001", "session_0002", "session_0003"]
    ids = mb._session_stem_to_id(result_paths, session_stems)
    assert ids == [1, 3]


def test_session_stem_to_id_no_match():
    ids = mb._session_stem_to_id(["/tmp/unrelated.md"], ["session_0000", "session_0001"])
    assert ids == []


def test_session_stem_to_id_empty_results():
    ids = mb._session_stem_to_id([], ["session_0000"])
    assert ids == []


# ── run_outbound (subprocess mocked) ─────────────────────────────────────────


def _make_completed(stdout: str = "", returncode: int = 0):
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    return proc


def _fake_examples(n: int = 3) -> list[dict]:
    """All examples use answer_session_ids=['sid_0'] so session_0000 is always the hit."""
    examples = []
    for i in range(n):
        haystack_ids = [f"sid_{j}" for j in range(3)]
        examples.append({
            "question_id": f"q_{i:04d}",
            "question": f"What happened in session {i}?",
            "answer": f"Answer {i}",
            "question_type": "single-session-user",
            "question_date": "2024-03-15",
            "haystack_sessions": [
                [{"role": "user", "content": f"session content {j}"}]
                for j in range(3)
            ],
            "haystack_session_ids": haystack_ids,
            "haystack_dates": ["2024-03-10", "2024-03-11", "2024-03-12"],
            "answer_session_ids": ["sid_0"],
        })
    return examples


def test_run_outbound_computes_recall(tmp_path):
    """Stub subprocess so the 'hit' session stem always appears in query output."""

    examples = _fake_examples(n=3)

    def fake_download():
        return examples

    # Query output will contain "session_0000" (the answer session for each example)
    def fake_run_with_home(args, fake_home, vault_path):
        if "--query" in args:
            # Simulate a hit: the first result is always session_0000
            output = (
                "## Relevant Past Sessions\n"
                "- [2024-03-15 | session 0000] — tldr\n"
                f"  (full log: {vault_path}/Session-Logs/session_0000.md)\n"
            )
            return _make_completed(stdout=output)
        return _make_completed()

    with (
        patch.object(mb, "_download_longmemeval", fake_download),
        patch.object(mb, "_run_indexer_with_home", fake_run_with_home),
    ):
        result = mb.run_outbound(limit=3, ks=[1, 3])

    assert result["n"] == 3
    assert result["mode"] == "outbound"
    # All examples have answer_session_ids=[0] and session_0000 is in results
    assert result["recall"][1] == pytest.approx(1.0)
    assert result["recall"][3] == pytest.approx(1.0)
    assert result["mrr"] == pytest.approx(1.0)


def test_run_outbound_no_hits(tmp_path):
    """All queries return empty output → recall should be 0."""
    examples = _fake_examples(n=2)

    with (
        patch.object(mb, "_download_longmemeval", lambda: examples),
        patch.object(mb, "_run_indexer_with_home", lambda *a, **kw: _make_completed()),
    ):
        result = mb.run_outbound(limit=2, ks=[1, 3])

    assert result["recall"][1] == pytest.approx(0.0)
    assert result["mrr"] == pytest.approx(0.0)


# ── run_internal (subprocess mocked) ─────────────────────────────────────────


def test_run_internal_token_efficiency():
    """Token efficiency section uses char lengths from --recent output."""
    full_output = "x" * 2000
    compact_output = "x" * 600

    def fake_real(args):
        if "--compact" in args:
            return _make_completed(stdout=compact_output)
        return _make_completed(stdout=full_output)

    with (
        patch.object(mb, "_run_indexer_real", fake_real),
        patch.object(mb, "_sample_real_sessions", return_value=[]),
    ):
        result = mb.run_internal(limit=0)

    te = result["token_efficiency"]
    assert te["full_chars"] == 2000
    assert te["compact_chars"] == 600
    assert te["reduction_pct"] == pytest.approx(70.0)
    assert "pending_accuracy" not in result


def test_run_internal_local_recall_hit():
    """Local recall: session stem found in query output -> hit, rank=1, mrr=1.0."""
    session = {"path": "/vault/Session-Logs/my-great-session.md", "query": "algebra exam"}
    query_output = (
        "## Relevant Past Sessions\n"
        "- [2024-06-15 | my great session] — tldr\n"
        "  (full log: /vault/Session-Logs/my-great-session.md)\n"
    )

    def fake_real(args):
        if "--query" in args:
            return _make_completed(stdout=query_output)
        return _make_completed()

    with (
        patch.object(mb, "_run_indexer_real", fake_real),
        patch.object(mb, "_sample_real_sessions", return_value=[session]),
    ):
        result = mb.run_internal(limit=1)

    assert result["local_recall"]["hits"] == 1
    assert result["local_recall"]["total"] == 1
    assert result["local_recall"]["rate"] == pytest.approx(1.0)
    assert result["local_recall"]["mrr"] == pytest.approx(1.0)
    assert result["local_recall"]["ranks"] == [1]


def test_run_internal_local_recall_miss():
    """Local recall: unrelated path in query output -> miss, rank=None, mrr=0.0."""
    session = {"path": "/vault/Session-Logs/my-session.md", "query": "algebra exam"}
    query_output = (
        "## Relevant Past Sessions\n"
        "- [2024-06-15 | other session] — tldr\n"
        "  (full log: /vault/Session-Logs/other-session.md)\n"
    )

    def fake_real(args):
        if "--query" in args:
            return _make_completed(stdout=query_output)
        return _make_completed()

    with (
        patch.object(mb, "_run_indexer_real", fake_real),
        patch.object(mb, "_sample_real_sessions", return_value=[session]),
    ):
        result = mb.run_internal(limit=1)

    assert result["local_recall"]["hits"] == 0
    assert result["local_recall"]["rate"] == pytest.approx(0.0)
    assert result["local_recall"]["mrr"] == pytest.approx(0.0)
    assert result["local_recall"]["ranks"] == [None]


def test_run_internal_no_sessions():
    """With no vault sessions, local recall reports 0/0 gracefully, mrr=0.0."""
    with (
        patch.object(mb, "_run_indexer_real", lambda args: _make_completed()),
        patch.object(mb, "_sample_real_sessions", return_value=[]),
    ):
        result = mb.run_internal(limit=0)

    assert result["local_recall"]["total"] == 0
    assert result["local_recall"]["rate"] == pytest.approx(0.0)
    assert result["local_recall"]["mrr"] == pytest.approx(0.0)
    assert result["local_recall"]["ranks"] == []


# ── print helpers (smoke tests) ───────────────────────────────────────────────


def test_print_outbound_results_runs(capsys):
    result = {
        "mode": "outbound",
        "n": 10,
        "ks": [1, 3, 5, 10],
        "recall": {1: 0.5, 3: 0.7, 5: 0.8, 10: 0.9},
        "mrr": 0.63,
        "total_time_s": 42.0,
        "per_example_s": 4.2,
    }
    mb.print_outbound_results(result)
    out = capsys.readouterr().out
    assert "LongMemEval" in out
    assert "Recall@1" in out
    assert "MRR" in out


def test_print_internal_results_runs(capsys):
    result = {
        "mode": "internal",
        "token_efficiency": {
            "full_chars": 7000,
            "compact_chars": 2500,
            "reduction_pct": 64.3,
            "sessions": 5,
        },
        "local_recall": {"hits": 17, "total": 20, "rate": 0.85, "mrr": 0.72, "ranks": []},
    }
    mb.print_internal_results(result)
    out = capsys.readouterr().out
    assert "Internal Benchmarks" in out
    assert "Token efficiency" in out
    assert "recall@3" in out.lower()
    assert "MRR" in out


# ── save_results ──────────────────────────────────────────────────────────────


def test_save_results_appends_jsonl(tmp_path):
    results_log = tmp_path / "results.jsonl"
    result = {"mode": "internal", "n": 5}
    # Patch both RESULTS_LOG and BENCHMARK_DIR to use tmp_path (already exists)
    with (
        patch.object(mb, "RESULTS_LOG", results_log),
        patch.object(mb, "BENCHMARK_DIR", tmp_path),
    ):
        mb.save_results(result)
    lines = results_log.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["mode"] == "internal"
    assert "timestamp" in record


def test_save_results_appends_multiple(tmp_path):
    results_log = tmp_path / "results.jsonl"
    with (
        patch.object(mb, "RESULTS_LOG", results_log),
        patch.object(mb, "BENCHMARK_DIR", tmp_path),
    ):
        mb.save_results({"mode": "outbound", "n": 50})
        mb.save_results({"mode": "internal", "n": 20})
    lines = results_log.read_text().strip().splitlines()
    assert len(lines) == 2


# ── _extract_session_tldr ─────────────────────────────────────────────────────


def test_extract_session_tldr_user_turns():
    session = [
        {"role": "user", "content": "I graduated with a degree in Business Administration."},
        {"role": "assistant", "content": "Congratulations!"},
        {"role": "user", "content": "Thanks. Any tips for the job search?"},
    ]
    tldr = mb._extract_session_tldr(session)
    assert "Business Administration" in tldr
    assert "job search" in tldr


def test_extract_session_tldr_empty():
    assert mb._extract_session_tldr([]) == "conversation session"


def test_extract_session_tldr_no_user_turns():
    session = [{"role": "assistant", "content": "Hello!"}]
    assert mb._extract_session_tldr(session) == "conversation session"


def test_extract_session_tldr_string_session():
    assert mb._extract_session_tldr("some string session") == "conversation session"


def test_extract_session_tldr_truncated_to_300():
    long_content = "x" * 500
    session = [{"role": "user", "content": long_content}]
    tldr = mb._extract_session_tldr(session)
    assert len(tldr) <= 300


# ── _sample_real_sessions block-scalar tldr parsing ───────────────────────────


def test_sample_real_sessions_block_scalar_tldr(tmp_path):
    """Sessions with block-scalar tldr: | should use topics as query fallback."""
    session_logs = tmp_path / "Session-Logs"
    session_logs.mkdir()
    md = session_logs / "my-session.md"
    md.write_text(
        "---\n"
        "type: session\n"
        "date: 2026-04-07\n"
        "topics: [evolution, memory, indexer]\n"
        "tldr: |\n"
        "  Shipped the evolution batch-judge PR and fixed silent failures.\n"
        "---\n\n"
        "## Key Learnings\n- Something useful\n"
    )
    with patch.object(mb, "_load_vault_root", return_value=tmp_path):
        sessions = mb._sample_real_sessions(5)
    assert len(sessions) == 1
    assert "evolution" in sessions[0]["query"]


def test_sample_real_sessions_inline_tldr(tmp_path):
    """Sessions with inline tldr should use tldr as query (topics still preferred)."""
    session_logs = tmp_path / "Session-Logs"
    session_logs.mkdir()
    md = session_logs / "my-session.md"
    md.write_text(
        "---\n"
        "type: session\n"
        "date: 2026-01-01\n"
        "topics: [linear-algebra, feynman]\n"
        "tldr: Reviewed chapter 5 and chapter 6 of the textbook.\n"
        "---\n\n"
        "## Key Learnings\n- Something useful\n"
    )
    with patch.object(mb, "_load_vault_root", return_value=tmp_path):
        sessions = mb._sample_real_sessions(5)
    assert len(sessions) == 1
    # topics takes priority but either way the query must be non-empty
    assert len(sessions[0]["query"]) > 10


# ── _run_indexer_real fail-loud ───────────────────────────────────────────────


def test_run_indexer_real_raises_on_nonzero_rc():
    proc = MagicMock()
    proc.returncode = 1
    proc.stderr = "boom"
    proc.stdout = ""
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(RuntimeError) as exc_info:
            mb._run_indexer_real(["--query", "test"])
    msg = str(exc_info.value)
    assert "boom" in msg
    assert "memory_indexer.py" in msg


def test_run_indexer_real_ok_on_zero_rc():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "ok"
    proc.stderr = ""
    with patch("subprocess.run", return_value=proc):
        result = mb._run_indexer_real(["--query", "test"])
    assert result.stdout == "ok"


# ── _run_indexer_with_home fail-loud ─────────────────────────────────────────


def test_run_indexer_with_home_raises_on_nonzero_rc():
    proc = MagicMock()
    proc.returncode = 1
    proc.stderr = "boom"
    proc.stdout = ""
    with patch("subprocess.run", return_value=proc):
        with pytest.raises(RuntimeError) as exc_info:
            mb._run_indexer_with_home(["--query", "test"], fake_home="/tmp/fake", vault_path="/tmp/vault")
    msg = str(exc_info.value)
    assert "boom" in msg
    assert "memory_indexer.py" in msg


def test_run_indexer_with_home_ok_on_zero_rc():
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "ok"
    proc.stderr = ""
    with patch("subprocess.run", return_value=proc):
        result = mb._run_indexer_with_home(["--query", "test"], fake_home="/tmp/fake", vault_path="/tmp/vault")
    assert result.stdout == "ok"

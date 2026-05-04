"""Tests for scripts/memory_query.py — offline, stubbed retrieve()."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

if "memory_query" in sys.modules:
    mq = sys.modules["memory_query"]
else:
    _SPEC = importlib.util.spec_from_file_location(
        "memory_query", _ROOT / "scripts" / "memory_query.py"
    )
    mq = importlib.util.module_from_spec(_SPEC)
    sys.modules["memory_query"] = mq
    _SPEC.loader.exec_module(mq)

mt = sys.modules["memory_tree"]


FAKE_RETRIEVE_HIT = {
    "results": [
        {"id": "n1", "path": "CLAUDE.md", "score": 0.72, "route": "flat"},
        {"id": "n2", "path": "INFRA.md", "score": 0.65, "route": "rrf"},
    ],
    "confidence": 0.72,
    "fell_back": False,
    "trace": ["flat_top=CLAUDE.md:0.720"],
}

FAKE_RETRIEVE_ABSTAIN = {
    "results": [],
    "confidence": 0.20,
    "fell_back": True,
    "trace": ["flat_top=X:0.200"],
}


@pytest.fixture
def fake_vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "CLAUDE.md").write_text("name: Liam", encoding="utf-8")
    (v / "INFRA.md").write_text("memory: vault", encoding="utf-8")
    return v


@pytest.fixture
def fake_auto_mem(tmp_path):
    d = tmp_path / "auto_mem"
    d.mkdir()
    (d / "feedback_test.md").write_text("some feedback", encoding="utf-8")
    return d


@pytest.fixture
def log_file(tmp_path):
    return tmp_path / "retrieval.jsonl"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, fake_vault, fake_auto_mem, log_file):
    monkeypatch.setattr(mq, "LOG_FILE", log_file)
    monkeypatch.setattr(mq, "AUTO_MEM_DIR", fake_auto_mem)
    monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
    monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "tree_queries.jsonl")
    monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "tree_audit.jsonl")
    monkeypatch.setenv("DEUS_VAULT_PATH", str(fake_vault))


class TestRecall:
    def test_hit_returns_context_and_paths(self, fake_vault):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            result = mq.recall("what timezone?", source="test")

        assert not result["fell_back"]
        assert result["confidence"] == 0.72
        assert result["paths"] == ["CLAUDE.md", "INFRA.md"]
        assert "Auto-retrieved memory" in result["context"]
        assert "name: Liam" in result["context"]

    def test_abstain_returns_empty_context(self):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_ABSTAIN), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            result = mq.recall("gibberish xyz", source="test")

        assert result["fell_back"]
        assert result["context"] == ""
        assert result["paths"] == []

    def test_default_threshold_uses_memory_tree(self):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_ABSTAIN) as mock_ret, \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            mq.recall("test", source="test")

        _, kwargs = mock_ret.call_args
        assert kwargs["abstain_threshold"] == mt.DEFAULT_ABSTAIN_THRESHOLD

    def test_explicit_threshold_overrides_default(self):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_ABSTAIN) as mock_ret, \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            mq.recall("test", abstain_threshold=0.99, source="test")

        _, kwargs = mock_ret.call_args
        assert kwargs["abstain_threshold"] == 0.99

    def test_db_closed_after_recall(self):
        closed = []
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_ABSTAIN), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: closed.append(True)
            mq.recall("test", source="test")

        assert closed

    def test_db_closed_on_retrieve_error(self):
        closed = []
        with patch.object(mt, "retrieve", side_effect=RuntimeError("boom")), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: closed.append(True)
            with pytest.raises(RuntimeError, match="boom"):
                mq.recall("test", source="test")

        assert closed


class TestFileReading:
    def test_reads_vault_path(self, fake_vault):
        content = mq._read_node_file("CLAUDE.md")
        assert content == "name: Liam"

    def test_reads_auto_memory_path(self, fake_auto_mem):
        content = mq._read_node_file("auto-memory/feedback_test.md")
        assert content == "some feedback"

    def test_missing_file_returns_none(self):
        assert mq._read_node_file("nonexistent.md") is None

    def test_missing_auto_memory_returns_none(self):
        assert mq._read_node_file("auto-memory/nonexistent.md") is None


class TestContextFormatting:
    def test_empty_on_fell_back(self):
        assert mq._format_context([], fell_back=True) == ""

    def test_empty_on_no_results(self):
        assert mq._format_context([], fell_back=False) == ""

    def test_includes_header_and_footer(self, fake_vault):
        ctx = mq._format_context(FAKE_RETRIEVE_HIT["results"], fell_back=False)
        assert ctx.startswith("=== Auto-retrieved memory")
        assert ctx.endswith("=== End auto-retrieved memory ===")

    def test_includes_path_and_score(self, fake_vault):
        ctx = mq._format_context(FAKE_RETRIEVE_HIT["results"], fell_back=False)
        assert "--- CLAUDE.md (score: 0.7200) ---" in ctx

    def test_skips_unreadable_files(self):
        results = [{"path": "nonexistent.md", "score": 0.5}]
        ctx = mq._format_context(results, fell_back=False)
        assert "nonexistent" not in ctx


class TestLogging:
    def test_writes_log_entry_with_source(self, log_file):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            mq.recall("what timezone?", source="mcp")

        entries = [json.loads(line) for line in log_file.read_text().splitlines()]
        assert len(entries) == 1
        assert entries[0]["source"] == "mcp"
        assert entries[0]["confidence"] == 0.72
        assert "ts" in entries[0]
        assert "prompt_hash" in entries[0]

    def test_log_survives_write_failure(self, monkeypatch):
        monkeypatch.setattr(mq, "LOG_FILE", Path("/nonexistent/dir/log.jsonl"))
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            result = mq.recall("test", source="test")

        assert result["confidence"] == 0.72


class TestCLI:
    def test_json_output(self, capsys):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            code = mq.main(["test query", "--json", "--source", "test"])

        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["confidence"] == 0.72

    def test_context_only_output(self, capsys, fake_vault):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            code = mq.main(["test query", "--context-only"])

        assert code == 0
        out = capsys.readouterr().out
        assert "Auto-retrieved memory" in out

    def test_abstain_exit_code(self):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_ABSTAIN), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            code = mq.main(["gibberish"])

        assert code == 1

    def test_default_source_is_cli(self, log_file):
        with patch.object(mt, "retrieve", return_value=FAKE_RETRIEVE_HIT), \
             patch.object(mt, "open_db") as mock_db:
            mock_db.return_value.close = lambda: None
            mq.main(["test query"])

        entry = json.loads(log_file.read_text().strip())
        assert entry["source"] == "cli"

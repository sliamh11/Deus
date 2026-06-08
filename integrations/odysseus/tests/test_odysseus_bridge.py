"""Tests for the Odysseus curated-memory bridge.

Network-free: the Ollama embedder is monkeypatched to a deterministic stub, so
both the builder and the sidecar produce comparable vectors without a live model.

Run:  pytest integrations/odysseus/tests/test_odysseus_bridge.py
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_DIR))

import _embed  # noqa: E402
import build_share_index  # noqa: E402
import share_mcp_server  # noqa: E402


def _fake_embed(text: str) -> list[float]:
    """Deterministic 768-dim vector seeded by the text (distinct text → distinct vec)."""
    h = hashlib.sha256(text.encode()).digest()
    return [h[i % len(h)] / 255.0 for i in range(_embed.EMBED_DIM)]


# ── _normalize_vec ──────────────────────────────────────────────────────────

def test_normalize_truncates_when_too_long():
    assert len(_embed._normalize_vec([0.1] * (_embed.EMBED_DIM + 10))) == _embed.EMBED_DIM


def test_normalize_pads_when_too_short():
    out = _embed._normalize_vec([0.1] * 10)
    assert len(out) == _embed.EMBED_DIM
    assert out[-1] == 0.0  # zero-padded tail


def test_normalize_passes_exact_length():
    vec = [0.5] * _embed.EMBED_DIM
    assert _embed._normalize_vec(vec) == vec


# ── recall error branches ───────────────────────────────────────────────────

def test_recall_missing_db(monkeypatch, tmp_path):
    monkeypatch.setattr(share_mcp_server, "DB_PATH", tmp_path / "nope.db")
    monkeypatch.setattr(share_mcp_server, "RECALL_LOG", tmp_path / "log")
    out = share_mcp_server.recall("anything")
    assert out["results"] == []
    assert "not built" in out["error"].lower()


def test_recall_embed_failure(monkeypatch, tmp_path):
    db = tmp_path / "share.db"
    db.write_text("")  # exists so we pass the missing-db gate
    monkeypatch.setattr(share_mcp_server, "DB_PATH", db)
    monkeypatch.setattr(share_mcp_server, "RECALL_LOG", tmp_path / "log")

    def boom(_q):
        raise RuntimeError("ollama down")

    monkeypatch.setattr(_embed, "embed", boom)
    out = share_mcp_server.recall("anything")
    assert out["results"] == []
    assert "embedding failed" in out["error"].lower()


# ── recall success + ranking ────────────────────────────────────────────────

def _build_index(tmp_path, monkeypatch, files: dict[str, str]) -> Path:
    monkeypatch.setattr(_embed, "embed", _fake_embed)
    share = tmp_path / "Shareable"
    share.mkdir()
    for name, body in files.items():
        (share / name).write_text(body, encoding="utf-8")
    db = tmp_path / "share.db"
    build_share_index.build(share, db)
    return db


def test_recall_success_returns_entry(monkeypatch, tmp_path):
    db = _build_index(
        tmp_path, monkeypatch, {"coffee.md": "the user likes flat white coffee"}
    )
    monkeypatch.setattr(share_mcp_server, "DB_PATH", db)
    monkeypatch.setattr(share_mcp_server, "RECALL_LOG", tmp_path / "log")
    out = share_mcp_server.recall("coffee", k=3)
    assert out.get("results"), out
    assert out["results"][0]["path"] == "coffee.md"


def test_recall_k_is_clamped(monkeypatch, tmp_path):
    db = _build_index(tmp_path, monkeypatch, {"a.md": "alpha", "b.md": "beta"})
    monkeypatch.setattr(share_mcp_server, "DB_PATH", db)
    monkeypatch.setattr(share_mcp_server, "RECALL_LOG", tmp_path / "log")
    # k far above the clamp ceiling must not error; returns at most what's indexed.
    out = share_mcp_server.recall("alpha", k=9999)
    assert "error" not in out
    assert len(out["results"]) <= 2


# ── model-mismatch guard ────────────────────────────────────────────────────

def test_recall_model_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(_embed, "OLLAMA_EMBED_MODEL", "model-a")
    db = _build_index(tmp_path, monkeypatch, {"a.md": "alpha"})  # meta.model = model-a
    monkeypatch.setattr(share_mcp_server, "DB_PATH", db)
    monkeypatch.setattr(share_mcp_server, "RECALL_LOG", tmp_path / "log")
    # server now configured for a different model than the index was built with
    monkeypatch.setattr(_embed, "OLLAMA_EMBED_MODEL", "model-b")
    out = share_mcp_server.recall("alpha")
    assert out["results"] == []
    assert "model-a" in out["error"] and "model-b" in out["error"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

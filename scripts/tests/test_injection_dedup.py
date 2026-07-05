"""Tests for scripts/injection_dedup.py + recall(dedup_store=...) (LIA-355).

Session-scoped dedup of memory-hook injections: exact-content-hash only,
mark-only-what-survives-truncation, fail-open everywhere.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


dedup = _load("injection_dedup")
mq = _load("memory_query")
mt = sys.modules["memory_tree"]


# ── block_key ────────────────────────────────────────────────────────────────


class TestBlockKey:
    def test_stable_for_same_content(self):
        assert dedup.block_key("a.md", "hello") == dedup.block_key("a.md", "hello")

    def test_changes_when_content_changes(self):
        assert dedup.block_key("a.md", "hello") != dedup.block_key("a.md", "hello2")

    def test_changes_when_path_changes(self):
        assert dedup.block_key("a.md", "hello") != dedup.block_key("b.md", "hello")

    def test_key_carries_path_prefix(self):
        assert dedup.block_key("a.md", "x").startswith("a.md:")


# ── seen-store ───────────────────────────────────────────────────────────────


class TestSeenStore:
    def test_round_trip(self, tmp_path):
        store = tmp_path / ".deus-memseen-s1.json"
        dedup.save_seen(store, {"k1", "k2"})
        assert dedup.load_seen(store) == {"k1", "k2"}

    def test_missing_store_loads_empty(self, tmp_path):
        assert dedup.load_seen(tmp_path / "nope.json") == set()

    def test_corrupt_store_loads_empty(self, tmp_path):
        store = tmp_path / ".deus-memseen-bad.json"
        store.write_text("{not json", encoding="utf-8")
        assert dedup.load_seen(store) == set()

    def test_save_is_best_effort_on_unwritable_dir(self, tmp_path):
        # Nonexistent parent: save must not raise.
        dedup.save_seen(tmp_path / "no" / "such" / "dir" / "s.json", {"k"})

    def test_store_path_sanitizes_session_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dedup.tempfile, "gettempdir", lambda: str(tmp_path))
        p = dedup.store_path_for_session("ab/../..zz!!")
        assert p.parent == tmp_path
        assert "/.." not in p.name and "!" not in p.name
        assert p.name.startswith(".deus-memseen-")

    def test_eviction_deletes_only_old_store_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dedup.tempfile, "gettempdir", lambda: str(tmp_path))
        old = tmp_path / ".deus-memseen-old.json"
        fresh = tmp_path / ".deus-memseen-fresh.json"
        other = tmp_path / ".deus-concepts-x.json"
        for f in (old, fresh, other):
            f.write_text("{}", encoding="utf-8")
        stale = time.time() - 8 * 86400
        import os

        os.utime(old, (stale, stale))
        os.utime(other, (stale, stale))
        dedup.save_seen(tmp_path / ".deus-memseen-s.json", {"k"})
        assert not old.exists()  # old memseen evicted
        assert fresh.exists()  # fresh memseen kept
        assert other.exists()  # other families never touched


# ── recall(dedup_store=...) integration ─────────────────────────────────────

FAKE_HIT = {
    "results": [
        {"id": "n1", "path": "CLAUDE.md", "score": 0.72, "route": "flat"},
        {"id": "n2", "path": "INFRA.md", "score": 0.65, "route": "rrf"},
    ],
    "confidence": 0.72,
    "fell_back": False,
    "trace": [],
}


def _fake_hit():
    return {**FAKE_HIT, "results": list(FAKE_HIT["results"]), "trace": []}


@pytest.fixture
def fake_vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    (v / "CLAUDE.md").write_text("name: Liam", encoding="utf-8")
    (v / "INFRA.md").write_text("memory: vault", encoding="utf-8")
    return v


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, fake_vault):
    monkeypatch.setattr(mq, "LOG_FILE", tmp_path / "retrieval.jsonl")
    monkeypatch.setattr(mq, "AUTO_MEM_DIR", tmp_path / "auto_mem")
    monkeypatch.setattr(mt, "DB_PATH", tmp_path / "tree.db")
    monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "tq.jsonl")
    monkeypatch.setattr(mt, "_AUDIT_PATH", tmp_path / "ta.jsonl")
    monkeypatch.setenv("DEUS_VAULT_PATH", str(fake_vault))


def _recall(store: Path, **kw) -> dict:
    with patch.object(mt, "retrieve", return_value=_fake_hit()), patch.object(
        mt, "open_db", return_value=type("D", (), {"close": lambda self: None})()
    ):
        return mq.recall("q", source="test", dedup_store=str(store), **kw)


class TestRecallDedup:
    def test_default_none_is_unchanged(self):
        with patch.object(mt, "retrieve", return_value=_fake_hit()), patch.object(
            mt, "open_db", return_value=type("D", (), {"close": lambda self: None})()
        ):
            out = mq.recall("q", source="test")
        assert "CLAUDE.md" in out["context"] and "INFRA.md" in out["context"]
        assert out["paths"] == ["CLAUDE.md", "INFRA.md"]

    def test_second_call_dedups_to_empty(self, tmp_path):
        store = tmp_path / "seen.json"
        first = _recall(store)
        assert first["paths"] == ["CLAUDE.md", "INFRA.md"]
        second = _recall(store)
        assert second["context"] == ""
        assert second["paths"] == []

    def test_changed_content_reinjects(self, tmp_path, fake_vault):
        store = tmp_path / "seen.json"
        _recall(store)
        (fake_vault / "CLAUDE.md").write_text("name: Liam v2", encoding="utf-8")
        out = _recall(store)
        assert "CLAUDE.md" in out["context"]
        assert "INFRA.md" not in out["context"]
        assert out["paths"] == ["CLAUDE.md"]

    def test_truncated_block_not_marked_seen(self, tmp_path, fake_vault):
        # Budget fits the first block but cuts the second: the cut block must
        # NOT be marked seen and must re-inject on the next call.
        store = tmp_path / "seen.json"
        (fake_vault / "CLAUDE.md").write_text("A" * 50, encoding="utf-8")
        (fake_vault / "INFRA.md").write_text("B" * 500, encoding="utf-8")
        first = _recall(store, max_context_chars=120)
        assert "A" * 50 in first["context"]
        assert "B" * 500 not in first["context"]  # cut by the budget
        second = _recall(store, max_context_chars=1000)
        assert "B" * 500 in second["context"]  # cut block re-injects
        assert "A" * 50 not in second["context"]  # surviving block was marked

    def test_dedup_trace_and_log_fields(self, tmp_path):
        store = tmp_path / "seen.json"
        _recall(store)
        _recall(store)
        entries = [
            json.loads(l)
            for l in (mq.LOG_FILE).read_text(encoding="utf-8").splitlines()
        ]
        last = entries[-1]
        assert last["deduped"] == "2_of_2"
        assert last["paths"] == []  # post-filter paths

    def test_unreadable_file_never_marked(self, tmp_path, fake_vault):
        store = tmp_path / "seen.json"
        (fake_vault / "CLAUDE.md").unlink()  # unreadable result
        first = _recall(store)
        assert first["paths"] == ["CLAUDE.md", "INFRA.md"]  # today's behavior kept
        seen = dedup.load_seen(store)
        assert not any(k.startswith("CLAUDE.md:") for k in seen)

    def test_corrupt_store_fails_open(self, tmp_path):
        store = tmp_path / "seen.json"
        store.write_text("{broken", encoding="utf-8")
        out = _recall(store)
        assert "CLAUDE.md" in out["context"]  # injects normally

    def test_identical_body_in_two_blocks_only_survivor_marked(
        self, tmp_path, fake_vault
    ):
        # Two results with byte-identical bodies; the budget cuts the second.
        # Positional survival must mark ONLY the first — a substring check
        # would falsely mark both (the cut block's text appears inside the
        # surviving block), silently suppressing content never fully shown.
        store = tmp_path / "seen.json"
        (fake_vault / "CLAUDE.md").write_text("SAME" * 20, encoding="utf-8")
        (fake_vault / "INFRA.md").write_text("SAME" * 20, encoding="utf-8")
        # Budget fits block 1 (delim ~36 + 80 body) but not block 2.
        _recall(store, max_context_chars=130)
        seen = dedup.load_seen(store)
        assert any(k.startswith("CLAUDE.md:") for k in seen)
        assert not any(k.startswith("INFRA.md:") for k in seen)
        out = _recall(store, max_context_chars=1000)
        assert "INFRA.md" in out["context"]  # cut block re-injects

    def test_cli_dedup_store_flag_reaches_recall(self, tmp_path):
        # The argparse flag must actually be passed into recall() — round-1
        # code review found it parsed-but-discarded.
        store = tmp_path / "cli-seen.json"
        with patch.object(mt, "retrieve", return_value=_fake_hit()), patch.object(
            mt, "open_db", return_value=type("D", (), {"close": lambda self: None})()
        ), patch.object(sys, "stdout", new_callable=__import__("io").StringIO):
            mq.main(["q", "--json", "--dedup-store", str(store)])
        assert dedup.load_seen(store)  # keys persisted via the CLI path

    def test_format_context_bodies_cache_miss_falls_back_to_read(self):
        # bodies is a cache, not an authority: a path missing from the cache
        # (transient pre-read failure) must still render via a fresh read —
        # never silently dropped from the emitted context.
        results = [{"id": "n1", "path": "CLAUDE.md", "score": 0.7, "route": "flat"}]
        out = mq._format_context(results, False, bodies={})
        assert "CLAUDE.md" in out
        assert "name: Liam" in out

    def test_wrap_overhead_constant_covers_real_wrapper(self):
        # WRAP_OVERHEAD_CHARS must upper-bound the actual wrapper skeleton,
        # or the hook's reduced budget doesn't guarantee wrapped <= cap.
        wrapped = mq._wrap_untrusted("X", label="may not be relevant to your task")
        assert len(wrapped) - 1 <= mq.WRAP_OVERHEAD_CHARS

    def test_hook_boundary_body_never_exceeds_hook_cap(self, tmp_path, fake_vault):
        # Round-4 boundary: a body between (cap - overhead) and cap used to
        # survive recall's truncation yet exceed the cap once wrapped — the
        # hook's slice then chopped content whose key was already persisted.
        # With the reduced budget, recall truncates it (NOT marked seen) and
        # the wrapped total stays <= the hook cap (hook slice = no-op).
        hook_cap = 4096
        budget = hook_cap - mq.WRAP_OVERHEAD_CHARS
        store = tmp_path / "seen.json"
        (fake_vault / "CLAUDE.md").write_text("C" * 3900, encoding="utf-8")
        (fake_vault / "INFRA.md").write_text("D" * 10, encoding="utf-8")
        out = _recall(store, max_context_chars=budget)
        assert len(out["context"]) <= hook_cap  # hook slice is a true no-op
        seen = dedup.load_seen(store)
        assert not any(k.startswith("CLAUDE.md:") for k in seen)  # truncated → unmarked
        again = _recall(store, max_context_chars=budget)
        assert "C" * 100 in again["context"]  # re-injects, nothing lost

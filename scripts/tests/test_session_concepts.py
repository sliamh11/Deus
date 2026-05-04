"""Tests for session_concepts.py — rolling keyword extractor."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import session_concepts as sc


class TestExtractTerms:
    def test_basic_extraction(self):
        terms = sc.extract_terms("optimize the vector search SIMD layout")
        keys = [t for t, _ in terms]
        assert "vector" in keys
        assert "search" in keys
        assert "simd" in keys
        assert "layout" in keys
        assert "optimize" in keys

    def test_stop_words_removed(self):
        terms = sc.extract_terms("what is the best approach for this")
        keys = [t for t, _ in terms]
        assert "the" not in keys
        assert "what" not in keys
        assert "this" not in keys
        assert "best" in keys
        assert "approach" in keys

    def test_capitalized_boost(self):
        terms = sc.extract_terms("simd and SIMD")
        by_key = dict(terms)
        assert by_key["simd"] > 1.0

    def test_snake_case_boost(self):
        terms = sc.extract_terms("memory_tree is important")
        by_key = dict(terms)
        assert by_key["memory_tree"] >= 1.5

    def test_camel_case_boost(self):
        terms = sc.extract_terms("vectorSearch function")
        by_key = dict(terms)
        assert by_key["vectorsearch"] >= 1.5

    def test_length_boost(self):
        terms = sc.extract_terms("optimization opt")
        by_key = dict(terms)
        assert by_key["optimization"] > by_key["opt"]

    def test_empty_input(self):
        assert sc.extract_terms("") == []

    def test_only_stop_words(self):
        assert sc.extract_terms("the is at which on") == []

    def test_short_tokens_filtered(self):
        terms = sc.extract_terms("a b cd ef")
        assert len(terms) == 0


class TestLoadConcepts:
    def test_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        result = sc.load_concepts("nonexistent-session")
        assert result == {}

    def test_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        p = tmp_path / ".deus-concepts-bad.json"
        p.write_text("not json")
        result = sc.load_concepts("bad")
        assert result == {}

    def test_valid_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        p = tmp_path / ".deus-concepts-good.json"
        p.write_text(json.dumps({"concepts": {"vector": 2.0, "search": 1.5}}))
        result = sc.load_concepts("good")
        assert result == {"vector": 2.0, "search": 1.5}


class TestUpdateConcepts:
    def test_accumulation(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        sid = "test-accum"

        sc.update_concepts(sid, [("vector", 1.0), ("search", 1.0)])
        result = sc.update_concepts(sid, [("vector", 1.5), ("layout", 1.0)])

        concepts = sc.load_concepts(sid)
        assert concepts["vector"] == 2.5
        assert concepts["search"] == 1.0
        assert concepts["layout"] == 1.0

    def test_eviction_at_cap(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        sid = "test-evict"

        terms = [(f"term{i}", float(i)) for i in range(25)]
        sc.update_concepts(sid, terms)

        concepts = sc.load_concepts(sid)
        assert len(concepts) == sc.MAX_CONCEPTS
        assert "term0" not in concepts
        assert "term24" in concepts

    def test_returns_sorted_keywords(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        sid = "test-sort"

        result = sc.update_concepts(sid, [("low", 1.0), ("high", 5.0), ("mid", 3.0)])
        assert result[0] == "high"
        assert result[1] == "mid"
        assert result[2] == "low"

    def test_idempotent_weight(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "CONCEPTS_DIR", tmp_path)
        sid = "test-idem"

        sc.update_concepts(sid, [("term", 1.0)])
        sc.update_concepts(sid, [("term", 1.0)])

        concepts = sc.load_concepts(sid)
        assert concepts["term"] == 2.0

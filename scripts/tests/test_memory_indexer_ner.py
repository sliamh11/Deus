"""
Tests for GLiNER-based entity extraction in scripts/memory_indexer.py.

Covers:
  - _extract_entities_gliner: entity extraction, dedup logic, entity count cap
  - _extract_relationships_ollama: Ollama API call, JSON parsing, error paths
  - extract_entities_and_relations routing based on DEUS_ENTITY_PROVIDER
"""
import importlib
import json
import sys
import types
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ── Path setup ────────────────────────────────────────────────────────────────

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
for _p in (_PROJECT_ROOT, _SCRIPTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Google genai stub (required for memory_indexer module-level import) ───────

def _install_google_genai_stub():
    """Install a minimal stub for google.genai so memory_indexer can import."""
    if "google" not in sys.modules:
        google_mod = types.ModuleType("google")
        sys.modules["google"] = google_mod
    else:
        google_mod = sys.modules["google"]

    if not hasattr(google_mod, "genai"):
        genai_mod = types.ModuleType("google.genai")

        class _FakeClient:
            def __init__(self, **kwargs):
                pass

        genai_mod.Client = _FakeClient
        setattr(google_mod, "genai", genai_mod)
        sys.modules["google.genai"] = genai_mod

    if "google.genai.types" not in sys.modules:
        types_mod = types.ModuleType("google.genai.types")
        types_mod.EmbedContentConfig = object
        types_mod.GenerateContentConfig = lambda **kwargs: kwargs
        sys.modules["google.genai.types"] = types_mod

    genai_mod = sys.modules.get("google.genai")
    if genai_mod and not hasattr(genai_mod, "types"):
        setattr(genai_mod, "types", sys.modules["google.genai.types"])


_install_google_genai_stub()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_vault(tmp_path, monkeypatch):
    """Point DEUS_VAULT_PATH to a temp vault; reload memory_indexer clean."""
    vault = tmp_path / "vault"
    (vault / "Session-Logs").mkdir(parents=True)
    (vault / "Atoms").mkdir()
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))
    if "memory_indexer" in sys.modules:
        del sys.modules["memory_indexer"]
    yield vault




@pytest.fixture
def mi(tmp_path, fresh_vault, monkeypatch):
    """Load memory_indexer with a temp DB and default provider."""
    mod = importlib.import_module("memory_indexer")
    test_db = tmp_path / "memory.db"
    monkeypatch.setattr(mod, "DB_PATH", test_db)
    return mod


@pytest.fixture
def mi_auto(mi, monkeypatch):
    """memory_indexer with ENTITY_PROVIDER = 'auto'."""
    monkeypatch.setattr(mi, "ENTITY_PROVIDER", "auto")
    return mi


@pytest.fixture
def mi_gemini(mi, monkeypatch):
    """memory_indexer with ENTITY_PROVIDER = 'gemini'."""
    monkeypatch.setattr(mi, "ENTITY_PROVIDER", "gemini")
    return mi


@pytest.fixture
def mi_gliner(mi, monkeypatch):
    """memory_indexer with ENTITY_PROVIDER = 'gliner'."""
    monkeypatch.setattr(mi, "ENTITY_PROVIDER", "gliner")
    return mi


# ── GLiNER extraction tests ───────────────────────────────────────────────────

class _FakeGLiNER:
    """Minimal GLiNER stub."""
    _instance = None

    @classmethod
    def from_pretrained(cls, model_name):
        obj = cls()
        obj.model_name = model_name
        return obj

    def predict_entities(self, text, labels, threshold=0.5):
        return [
            {"text": "Alice", "label": "person"},
            {"text": "Docker", "label": "tool"},
            {"text": "alice", "label": "person"},   # duplicate (lowercased)
            {"text": "PyTorch", "label": "tool"},
        ]


def _make_gliner_mod(fake_cls=None):
    """Return a fake gliner module with optional custom GLiNER class."""
    mod = types.ModuleType("gliner")
    mod.GLiNER = fake_cls or _FakeGLiNER
    return mod


def test_gliner_entity_extraction_returns_entities(mi_auto, monkeypatch):
    """_extract_entities_gliner returns entities with entity_type key."""
    monkeypatch.setattr(mi_auto, "_gliner_model", None)
    gliner_mod = _make_gliner_mod()
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        result = mi_auto._extract_entities_gliner("Alice uses Docker for deployment.")
    assert result is not None
    entities = result["entities"]
    assert len(entities) >= 1
    for e in entities:
        assert "name" in e
        assert "entity_type" in e, "must use 'entity_type', not 'type'"


def test_gliner_dedup_removes_case_insensitive_duplicates(mi_auto, monkeypatch):
    """_extract_entities_gliner deduplicates (text.lower(), label) pairs."""
    monkeypatch.setattr(mi_auto, "_gliner_model", None)
    class _DupGLiNER:
        @classmethod
        def from_pretrained(cls, _name):
            return cls()

        def predict_entities(self, text, labels, threshold=0.5):
            return [
                {"text": "Docker", "label": "tool"},
                {"text": "docker", "label": "tool"},   # same key after lower()
                {"text": "Docker", "label": "tool"},   # exact dup
                {"text": "Alice", "label": "person"},
            ]

    gliner_mod = _make_gliner_mod(_DupGLiNER)
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        result = mi_auto._extract_entities_gliner("docker Alice")
    names = [e["name"] for e in result["entities"]]
    # "Docker" (first seen) should appear exactly once; "Alice" once
    assert names.count("Docker") == 1
    assert names.count("Alice") == 1
    assert len(names) == 2


def test_gliner_entity_count_capped_at_10(mi_auto, monkeypatch):
    """_extract_entities_gliner returns at most 10 entities."""
    monkeypatch.setattr(mi_auto, "_gliner_model", None)
    class _ManyGLiNER:
        @classmethod
        def from_pretrained(cls, _name):
            return cls()

        def predict_entities(self, text, labels, threshold=0.5):
            return [{"text": f"Entity{i}", "label": "concept"} for i in range(20)]

    gliner_mod = _make_gliner_mod(_ManyGLiNER)
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        result = mi_auto._extract_entities_gliner("many entities")
    assert len(result["entities"]) == 10


def test_gliner_returns_empty_relationships(mi_auto, monkeypatch):
    """_extract_entities_gliner always returns an empty relationships list."""
    monkeypatch.setattr(mi_auto, "_gliner_model", None)
    gliner_mod = _make_gliner_mod()
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        result = mi_auto._extract_entities_gliner("Alice uses Docker.")
    assert result["relationships"] == []


def test_gliner_import_error_returns_none(mi_auto):
    """_extract_entities_gliner returns None when gliner is not installed."""
    # Ensure "gliner" is NOT in sys.modules so the import raises ImportError
    saved = sys.modules.pop("gliner", None)
    try:
        result = mi_auto._extract_entities_gliner("some content")
        assert result is None
    finally:
        if saved is not None:
            sys.modules["gliner"] = saved


# ── Ollama relationship extraction tests ─────────────────────────────────────

def _mock_ollama_response(relationships: list):
    """Build a fake urllib response yielding the given relationship list."""
    body = json.dumps({"response": json.dumps(relationships)}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_ollama_extracts_relationships(mi_auto):
    """_extract_relationships_ollama returns parsed relationship dicts."""
    rels = [
        {"source": "alice", "target": "docker", "rel_type": "uses"},
        {"source": "alice", "target": "pytorch", "rel_type": "uses"},
    ]
    entities = [{"name": "alice", "entity_type": "person"},
                {"name": "docker", "entity_type": "tool"}]

    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(rels)):
        result = mi_auto._extract_relationships_ollama("Alice uses Docker and PyTorch.", entities)

    assert len(result) == 2
    assert result[0]["rel_type"] == "uses"
    for r in result:
        assert "source" in r and "target" in r and "rel_type" in r


def test_ollama_returns_empty_on_fewer_than_2_entities(mi_auto):
    """_extract_relationships_ollama returns [] when fewer than 2 entities."""
    result = mi_auto._extract_relationships_ollama("some text", [{"name": "alice", "entity_type": "person"}])
    assert result == []

    result2 = mi_auto._extract_relationships_ollama("some text", [])
    assert result2 == []


def test_ollama_caps_relationships_at_10(mi_auto):
    """_extract_relationships_ollama returns at most 10 relationships."""
    rels = [{"source": f"e{i}", "target": f"e{i+1}", "rel_type": "related_to"} for i in range(15)]
    entities = [{"name": f"e{i}", "entity_type": "concept"} for i in range(16)]

    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(rels)):
        result = mi_auto._extract_relationships_ollama("text", entities)

    assert len(result) == 10


def test_ollama_returns_empty_on_network_error(mi_auto, capsys):
    """_extract_relationships_ollama silently returns [] when Ollama isn't running."""
    entities = [{"name": "alice", "entity_type": "person"},
                {"name": "docker", "entity_type": "tool"}]
    with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
        result = mi_auto._extract_relationships_ollama("text", entities)
    assert result == []
    captured = capsys.readouterr()
    assert captured.err == ""


def test_ollama_warns_on_unexpected_error(mi_auto, capsys):
    """_extract_relationships_ollama logs a warning for non-connection errors."""
    entities = [{"name": "alice", "entity_type": "person"},
                {"name": "docker", "entity_type": "tool"}]
    with patch("urllib.request.urlopen", side_effect=ValueError("unexpected")):
        result = mi_auto._extract_relationships_ollama("text", entities)
    assert result == []
    captured = capsys.readouterr()
    assert "WARN" in captured.err


def test_ollama_returns_empty_on_invalid_json(mi_auto):
    """_extract_relationships_ollama returns [] when Ollama returns bad JSON."""
    bad_body = json.dumps({"response": "not json at all"}).encode()
    resp = MagicMock()
    resp.read.return_value = bad_body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)

    entities = [{"name": "alice", "entity_type": "person"},
                {"name": "docker", "entity_type": "tool"}]
    with patch("urllib.request.urlopen", return_value=resp):
        result = mi_auto._extract_relationships_ollama("text", entities)
    assert result == []


def test_ollama_strips_markdown_fencing(mi_auto):
    """_extract_relationships_ollama handles ```json fenced responses."""
    rels = [{"source": "alice", "target": "docker", "rel_type": "uses"}]
    fenced_json = "```json\n" + json.dumps(rels) + "\n```"
    body = json.dumps({"response": fenced_json}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)

    entities = [{"name": "alice", "entity_type": "person"},
                {"name": "docker", "entity_type": "tool"}]
    with patch("urllib.request.urlopen", return_value=resp):
        result = mi_auto._extract_relationships_ollama("alice uses docker", entities)
    assert len(result) == 1
    assert result[0]["source"] == "alice"


# ── Provider routing tests ────────────────────────────────────────────────────

def test_provider_gemini_skips_gliner(mi_gemini):
    """ENTITY_PROVIDER=gemini bypasses GLiNER and goes straight to Gemini."""
    gliner_mod = _make_gliner_mod()
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        with patch.object(mi_gemini, "_extract_entities_and_relations_gemini",
                          return_value={"entities": [{"name": "x", "entity_type": "tool"}],
                                        "relationships": []}) as mock_gemini:
            with patch.object(mi_gemini, "_extract_entities_gliner") as mock_gliner:
                result = mi_gemini.extract_entities_and_relations("some content")

    mock_gliner.assert_not_called()
    mock_gemini.assert_called_once()
    assert result["entities"][0]["name"] == "x"


def test_provider_auto_uses_gliner_when_available(mi_auto):
    """ENTITY_PROVIDER=auto prefers GLiNER when installed."""
    gliner_mod = _make_gliner_mod()
    gliner_entities = [{"name": "alice", "entity_type": "person"}]
    with patch.dict(sys.modules, {"gliner": gliner_mod}):
        with patch.object(mi_auto, "_extract_entities_gliner",
                          return_value={"entities": gliner_entities, "relationships": []}) as mock_gliner:
            with patch.object(mi_auto, "_extract_relationships_ollama",
                              return_value=[]) as mock_ollama:
                with patch.object(mi_auto, "_extract_entities_and_relations_gemini") as mock_gemini:
                    result = mi_auto.extract_entities_and_relations("alice does stuff")

    mock_gliner.assert_called_once()
    mock_ollama.assert_called_once()
    mock_gemini.assert_not_called()
    assert result["entities"] == gliner_entities


def test_provider_auto_falls_back_to_gemini_when_gliner_unavailable(mi_auto):
    """ENTITY_PROVIDER=auto falls back to Gemini when GLiNER returns None."""
    gemini_result = {"entities": [{"name": "gemini-entity", "entity_type": "tool"}], "relationships": []}
    with patch.object(mi_auto, "_extract_entities_gliner", return_value=None):
        with patch.object(mi_auto, "_extract_entities_and_relations_gemini",
                          return_value=gemini_result) as mock_gemini:
            result = mi_auto.extract_entities_and_relations("some content")

    mock_gemini.assert_called_once()
    assert result["entities"][0]["name"] == "gemini-entity"


def test_provider_gliner_strict_returns_empty_when_not_installed(mi_gliner):
    """ENTITY_PROVIDER=gliner returns empty dict (no Gemini fallback) when GLiNER missing."""
    with patch.object(mi_gliner, "_extract_entities_gliner", return_value=None):
        with patch.object(mi_gliner, "_extract_entities_and_relations_gemini") as mock_gemini:
            result = mi_gliner.extract_entities_and_relations("some content")

    mock_gemini.assert_not_called()
    assert result == {"entities": [], "relationships": []}


def test_provider_gliner_strict_uses_gliner_when_available(mi_gliner):
    """ENTITY_PROVIDER=gliner uses GLiNER + Ollama when GLiNER is installed."""
    gliner_entities = [{"name": "torch", "entity_type": "tool"},
                       {"name": "alice", "entity_type": "person"}]
    rels = [{"source": "alice", "target": "torch", "rel_type": "uses"}]
    with patch.object(mi_gliner, "_extract_entities_gliner",
                      return_value={"entities": gliner_entities, "relationships": []}):
        with patch.object(mi_gliner, "_extract_relationships_ollama", return_value=rels):
            result = mi_gliner.extract_entities_and_relations("alice uses torch")

    assert result["entities"] == gliner_entities
    assert result["relationships"] == rels

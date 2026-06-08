"""
Tests for Ollama-based entity extraction in scripts/memory_indexer.py.

Covers:
  - _extract_entities_ollama: entity extraction, field validation, error paths
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
def mi_ollama(mi, monkeypatch):
    """memory_indexer with ENTITY_PROVIDER = 'ollama'."""
    monkeypatch.setattr(mi, "ENTITY_PROVIDER", "ollama")
    return mi


# ── Helpers ──────────────────────────────────────────────────────────────────

def _mock_ollama_response(result_dict: dict):
    """Build a fake urllib response yielding the given result dict."""
    body = json.dumps({"response": json.dumps(result_dict)}).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ── _extract_entities_ollama tests ───────────────────────────────────────────

def test_ollama_extracts_entities_and_relationships(mi_auto):
    result_dict = {
        "entities": [
            {"name": "deus", "entity_type": "project", "summary": "AI assistant"},
            {"name": "ollama", "entity_type": "tool", "summary": "LLM runner"},
        ],
        "relationships": [
            {"source": "deus", "target": "ollama", "rel_type": "uses", "confidence": 0.9},
        ],
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        result = mi_auto._extract_entities_ollama("Deus uses Ollama for inference.")

    assert result is not None
    assert len(result["entities"]) == 2
    assert len(result["relationships"]) == 1
    assert result["entities"][0]["name"] == "deus"
    assert result["relationships"][0]["rel_type"] == "uses"


def test_ollama_caps_entities_at_10(mi_auto):
    result_dict = {
        "entities": [{"name": f"e{i}", "entity_type": "concept", "summary": ""} for i in range(15)],
        "relationships": [],
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        result = mi_auto._extract_entities_ollama("many entities")

    assert len(result["entities"]) == 10


def test_ollama_caps_relationships_at_10(mi_auto):
    result_dict = {
        "entities": [{"name": "a", "entity_type": "concept", "summary": ""}],
        "relationships": [
            {"source": f"e{i}", "target": f"e{i+1}", "rel_type": "related_to"}
            for i in range(15)
        ],
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        result = mi_auto._extract_entities_ollama("text")

    assert len(result["relationships"]) == 10


def test_ollama_filters_invalid_entities(mi_auto):
    result_dict = {
        "entities": [
            {"name": "valid", "entity_type": "tool", "summary": "ok"},
            {"name": "", "entity_type": "tool", "summary": "empty name"},
            {"name": None, "entity_type": "tool", "summary": "null name"},
            {"name": "no_type"},
            {"name": 42, "entity_type": "tool", "summary": "int name"},
            "not a dict",
        ],
        "relationships": [],
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        result = mi_auto._extract_entities_ollama("text")

    assert len(result["entities"]) == 1
    assert result["entities"][0]["name"] == "valid"


def test_ollama_filters_invalid_relationships(mi_auto):
    result_dict = {
        "entities": [{"name": "a", "entity_type": "concept", "summary": ""}],
        "relationships": [
            {"source": "a", "target": "b", "rel_type": "uses"},
            {"source": "a", "target": "b"},
            {"source": "a"},
            "not a dict",
        ],
    }
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        result = mi_auto._extract_entities_ollama("text")

    assert len(result["relationships"]) == 1
    assert result["relationships"][0]["rel_type"] == "uses"


def test_ollama_returns_none_on_connection_refused(mi_auto):
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
        result = mi_auto._extract_entities_ollama("text")
    assert result is None


def test_ollama_returns_none_on_os_error(mi_auto):
    with patch("urllib.request.urlopen", side_effect=OSError("network unreachable")):
        result = mi_auto._extract_entities_ollama("text")
    assert result is None


def test_ollama_returns_empty_on_json_decode_error(mi_auto, capsys):
    bad_body = json.dumps({"response": "not json at all"}).encode()
    resp = MagicMock()
    resp.read.return_value = bad_body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=resp):
        result = mi_auto._extract_entities_ollama("text")

    assert result == {"entities": [], "relationships": []}
    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert "malformed JSON" in captured.err


def test_ollama_returns_empty_on_http_error(mi_auto, capsys):
    with patch("urllib.request.urlopen",
               side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None)):
        result = mi_auto._extract_entities_ollama("text")

    assert result == {"entities": [], "relationships": []}
    captured = capsys.readouterr()
    assert "WARN" in captured.err
    assert "HTTP 404" in captured.err


def test_ollama_returns_empty_on_unexpected_error(mi_auto, capsys):
    with patch("urllib.request.urlopen", side_effect=ValueError("unexpected")):
        result = mi_auto._extract_entities_ollama("text")

    assert result == {"entities": [], "relationships": []}
    captured = capsys.readouterr()
    assert "WARN" in captured.err


# ── Provider routing tests ────────────────────────────────────────────────────

def test_provider_gemini_skips_ollama(mi_gemini):
    gemini_result = {"entities": [{"name": "x", "entity_type": "tool"}], "relationships": []}
    with patch.object(mi_gemini, "_extract_entities_and_relations_gemini",
                      return_value=gemini_result) as mock_gemini:
        with patch.object(mi_gemini, "_extract_entities_ollama") as mock_ollama:
            result = mi_gemini.extract_entities_and_relations("some content")

    mock_ollama.assert_not_called()
    mock_gemini.assert_called_once()
    assert result["entities"][0]["name"] == "x"


def test_provider_auto_uses_ollama_when_available(mi_auto):
    ollama_result = {"entities": [{"name": "deus", "entity_type": "project"}], "relationships": []}
    with patch.object(mi_auto, "_extract_entities_ollama", return_value=ollama_result) as mock_ollama:
        with patch.object(mi_auto, "_extract_entities_and_relations_gemini") as mock_gemini:
            result = mi_auto.extract_entities_and_relations("deus does stuff")

    mock_ollama.assert_called_once()
    mock_gemini.assert_not_called()
    assert result["entities"][0]["name"] == "deus"


def test_provider_auto_falls_back_to_gemini_when_ollama_unavailable(mi_auto):
    gemini_result = {"entities": [{"name": "gemini-entity", "entity_type": "tool"}], "relationships": []}
    with patch.object(mi_auto, "_extract_entities_ollama", return_value=None):
        with patch.object(mi_auto, "_extract_entities_and_relations_gemini",
                          return_value=gemini_result) as mock_gemini:
            result = mi_auto.extract_entities_and_relations("some content")

    mock_gemini.assert_called_once()
    assert result["entities"][0]["name"] == "gemini-entity"


def test_provider_ollama_strict_returns_empty_when_not_reachable(mi_ollama, capsys):
    with patch.object(mi_ollama, "_extract_entities_ollama", return_value=None):
        with patch.object(mi_ollama, "_extract_entities_and_relations_gemini") as mock_gemini:
            result = mi_ollama.extract_entities_and_relations("some content")

    mock_gemini.assert_not_called()
    assert result == {"entities": [], "relationships": []}
    captured = capsys.readouterr()
    assert "WARN" in captured.err


def test_provider_ollama_strict_uses_ollama_when_available(mi_ollama):
    ollama_result = {
        "entities": [{"name": "torch", "entity_type": "tool"}],
        "relationships": [{"source": "alice", "target": "torch", "rel_type": "uses"}],
    }
    with patch.object(mi_ollama, "_extract_entities_ollama", return_value=ollama_result):
        result = mi_ollama.extract_entities_and_relations("alice uses torch")

    assert result["entities"][0]["name"] == "torch"
    assert result["relationships"][0]["rel_type"] == "uses"


# ── Ollama atom extraction (LIA-170) ─────────────────────────────────────────

def test_ollama_extracts_atoms(mi):
    # Ollama returns the object-wrapper {"atoms": [...]} shape.
    result_dict = {"atoms": [
        {"text": "Deus uses sqlite-vec for vector storage", "category": "fact"},
        {"text": "Vitest is the monorepo test runner", "category": "decision"},
    ]}
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        atoms = mi._extract_atoms_ollama("Deus uses sqlite-vec; Vitest runs tests.")
    assert atoms is not None
    assert len(atoms) == 2
    assert atoms[0]["category"] == "fact"


def test_ollama_atoms_filters_malformed(mi):
    # Entries missing "text" or "category" are dropped (mirrors the Gemini filter).
    result_dict = {"atoms": [
        {"text": "valid", "category": "fact"},
        {"text": "no category"},
        {"category": "no text"},
        "not a dict",
    ]}
    with patch("urllib.request.urlopen", return_value=_mock_ollama_response(result_dict)):
        atoms = mi._extract_atoms_ollama("x")
    assert atoms == [{"text": "valid", "category": "fact"}]


def test_ollama_atoms_connection_refused_returns_none(mi):
    # Unreachable Ollama → None, so the router can fall back to Gemini.
    with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
        assert mi._extract_atoms_ollama("x") is None


def test_ollama_atoms_request_body_shape(mi):
    """Guard the Gemma4 fixes: think:false must be sent TOP-LEVEL (not under
    options), and the items schema must require both text+category — otherwise
    Gemma4 constrained decoding omits category and the post-parse filter drops
    100% of atoms (LIA-185 / LIA-187)."""
    captured = {}
    result_dict = {"atoms": [{"text": "x", "category": "fact"}]}

    def _capture(req, *args, **kwargs):
        captured["body"] = json.loads(req.data.decode())
        return _mock_ollama_response(result_dict)

    with patch("urllib.request.urlopen", side_effect=_capture):
        mi._extract_atoms_ollama("some session content")

    body = captured["body"]
    # LIA-185: thinking suppressed at the request top level, not under "options".
    assert body.get("think") is False
    assert "think" not in body.get("options", {})
    # Structured-output parsing depends on a non-streamed response.
    assert body.get("stream") is False
    # LIA-187: items schema forces both fields so category is always emitted.
    items = body["format"]["properties"]["atoms"]["items"]
    assert set(items["required"]) == {"text", "category"}


def test_ollama_entities_request_suppresses_thinking(mi_auto):
    """The entity path shares the Gemma4 think:false fix (top-level, not under
    options). It deliberately does NOT force `required` on items — Gemma4 emits
    all entity fields without it (verified), so that would be unproven hardening."""
    captured = {}
    result_dict = {"entities": [], "relationships": []}

    def _capture(req, *args, **kwargs):
        captured["body"] = json.loads(req.data.decode())
        return _mock_ollama_response(result_dict)

    with patch("urllib.request.urlopen", side_effect=_capture):
        mi_auto._extract_entities_ollama("Deus uses Ollama.")

    body = captured["body"]
    assert body.get("think") is False
    assert "think" not in body.get("options", {})


def test_extract_atoms_auto_falls_back_to_gemini_when_ollama_down(mi, monkeypatch):
    monkeypatch.setattr(mi, "ATOM_PROVIDER", "auto")
    monkeypatch.setattr(mi, "_extract_atoms_ollama", lambda c: None)  # Ollama down
    sentinel = [{"text": "from gemini", "category": "fact"}]
    monkeypatch.setattr(mi, "_extract_atoms_gemini", lambda c: sentinel)
    assert mi.extract_atoms("x") == sentinel


def test_extract_atoms_gemini_provider_skips_ollama(mi, monkeypatch):
    monkeypatch.setattr(mi, "ATOM_PROVIDER", "gemini")
    called = {"ollama": False}
    def _boom(c):
        called["ollama"] = True
        return None
    monkeypatch.setattr(mi, "_extract_atoms_ollama", _boom)
    monkeypatch.setattr(mi, "_extract_atoms_gemini", lambda c: [{"text": "g", "category": "fact"}])
    result = mi.extract_atoms("x")
    assert called["ollama"] is False
    assert result == [{"text": "g", "category": "fact"}]


def test_extract_atoms_ollama_provider_unreachable_returns_empty(mi, monkeypatch):
    monkeypatch.setattr(mi, "ATOM_PROVIDER", "ollama")
    monkeypatch.setattr(mi, "_extract_atoms_ollama", lambda c: None)
    # ollama-only + unreachable → [] (must NOT fall back to Gemini)
    def _fail_gemini(c):
        raise AssertionError("ollama-only provider must not fall back to Gemini")
    monkeypatch.setattr(mi, "_extract_atoms_gemini", _fail_gemini)
    assert mi.extract_atoms("x") == []

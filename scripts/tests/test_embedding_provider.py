"""Tests for the embedding provider selection and defaults."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest


def _reset_provider():
    """Reset the module-level singleton so each test starts clean."""
    import evolution.providers.embeddings as mod
    mod._provider = None


@pytest.fixture(autouse=True)
def _clean_provider():
    _reset_provider()
    yield
    _reset_provider()


class TestDefaults:
    """Verify default model and auto-selection priority."""

    def test_default_ollama_embed_model_is_embeddinggemma(self):
        from evolution.providers.embeddings import OLLAMA_EMBED_MODEL
        assert OLLAMA_EMBED_MODEL == "embeddinggemma"

    def test_auto_resolves_to_ollama(self):
        from evolution.providers.embeddings import (
            OllamaEmbeddingProvider,
            get_embedding_provider,
        )
        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "auto"}, clear=False):
            _reset_provider()
            provider = get_embedding_provider()
            assert isinstance(provider, OllamaEmbeddingProvider)

    def test_auto_never_falls_back_to_gemini_even_with_a_key(self):
        """Regression for the auto->Gemini fallback (embedding-model-selection.md gate 5).

        `auto` must resolve to the model that produced the stored vectors, no
        matter what else the environment offers. Gemini and EmbeddingGemma are
        both 768-dim but occupy different vector spaces, and no per-node
        provider is recorded, so a fallback corrupts the corpus silently and
        unattributably.

        The environment here is the one that used to trigger the fallback: a key
        present, OLLAMA_HOST unset, and Gemini fully constructible. Patching
        GeminiEmbeddingProvider to explode makes the assertion discriminating —
        the test fails loudly if anything ever routes `auto` back to Gemini,
        rather than quietly returning the wrong provider type.
        """
        from evolution.providers.embeddings import (
            OllamaEmbeddingProvider,
            get_embedding_provider,
        )
        env = {"EMBEDDING_PROVIDER": "auto", "GEMINI_API_KEY": "test-key"}
        with patch.dict("os.environ", env, clear=False):
            os.environ.pop("OLLAMA_HOST", None)
            with patch(
                "evolution.providers.embeddings.GeminiEmbeddingProvider",
                side_effect=AssertionError("auto must never construct Gemini"),
            ):
                _reset_provider()
                provider = get_embedding_provider()
                assert isinstance(provider, OllamaEmbeddingProvider)

    def test_explicit_ollama_backend(self):
        from evolution.providers.embeddings import (
            OllamaEmbeddingProvider,
            get_embedding_provider,
        )
        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "ollama"}, clear=False):
            _reset_provider()
            provider = get_embedding_provider()
            assert isinstance(provider, OllamaEmbeddingProvider)

    def test_explicit_gemini_backend(self):
        from evolution.providers.embeddings import (
            GeminiEmbeddingProvider,
            get_embedding_provider,
        )
        mock_client = MagicMock()
        with patch.dict("os.environ", {"EMBEDDING_PROVIDER": "gemini"}, clear=False):
            with patch("google.genai.Client", return_value=mock_client):
                _reset_provider()
                provider = get_embedding_provider()
                assert isinstance(provider, GeminiEmbeddingProvider)


class TestOllamaEmbeddingProvider:
    """Test OllamaEmbeddingProvider vector handling."""

    @staticmethod
    def _mock_http_response(body: bytes):
        """Create a mock HTTPConnection whose getresponse() returns body."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = body
        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp
        return mock_conn

    def test_truncates_long_vectors(self):
        from evolution.providers.embeddings import OllamaEmbeddingProvider
        from evolution.config import EMBED_DIM

        long_vec = list(range(EMBED_DIM + 100))
        fake_response = json.dumps({"embeddings": [long_vec]}).encode()

        provider = OllamaEmbeddingProvider(model="test")
        mock_conn = self._mock_http_response(fake_response)
        with patch.object(provider, "_get_conn", return_value=mock_conn):
            result = provider.embed("test")
            assert len(result) == EMBED_DIM

    def test_pads_short_vectors(self):
        from evolution.providers.embeddings import OllamaEmbeddingProvider
        from evolution.config import EMBED_DIM

        short_vec = [1.0] * 10
        fake_response = json.dumps({"embeddings": [short_vec]}).encode()

        provider = OllamaEmbeddingProvider(model="test")
        mock_conn = self._mock_http_response(fake_response)
        with patch.object(provider, "_get_conn", return_value=mock_conn):
            result = provider.embed("test")
            assert len(result) == EMBED_DIM
            assert result[:10] == [1.0] * 10
            assert all(v == 0.0 for v in result[10:])

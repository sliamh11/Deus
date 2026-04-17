"""
Pluggable embedding provider.

Default: Ollama with EmbeddingGemma (local, ~7x faster than Gemini API).
Override via EMBEDDING_PROVIDER env var: 'gemini', 'ollama', or 'auto'.
'auto' (default) tries Ollama first, falls back to Gemini if unavailable.
"""
import json
import logging
import os
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod

from ..config import EMBED_DIM, EMBED_MODELS, load_api_key

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "embeddinggemma")


class EmbeddingProvider(ABC):
    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed a single text string. Returns a vector of EMBED_DIM floats."""

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts. Default: sequential calls to embed()."""
        return [self.embed(t) for t in texts]


class GeminiEmbeddingProvider(EmbeddingProvider):
    def __init__(self) -> None:
        from google import genai
        self._client = genai.Client(api_key=load_api_key())

    def embed(self, text: str) -> list[float]:
        from google.genai import types as genai_types
        last_exc = None
        for model in EMBED_MODELS:
            try:
                result = self._client.models.embed_content(
                    model=model,
                    contents=text,
                    config=genai_types.EmbedContentConfig(output_dimensionality=EMBED_DIM),
                )
                return result.embeddings[0].values
            except Exception as exc:
                last_exc = exc
        raise RuntimeError(f"All embedding models failed. Last: {last_exc}")


class OllamaEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model: str = OLLAMA_EMBED_MODEL, host: str = OLLAMA_HOST) -> None:
        self._model = model
        self._url = f"{host.rstrip('/')}/api/embed"

    _MAX_ATTEMPTS = 3
    _BACKOFF_BASE = 1.0  # seconds; doubles each retry

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        """Return True if exc represents a transient socket timeout."""
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return True
        return False

    def embed(self, text: str) -> list[float]:
        payload = json.dumps({"model": self._model, "input": text}).encode()
        req = urllib.request.Request(
            self._url,
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        last_exc: BaseException | None = None
        for attempt in range(1, self._MAX_ATTEMPTS + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = json.loads(resp.read())
                break
            except BaseException as exc:
                if not self._is_timeout(exc):
                    raise
                last_exc = exc
                if attempt < self._MAX_ATTEMPTS:
                    delay = self._BACKOFF_BASE * (2 ** (attempt - 1))
                    logger.warning(
                        "Ollama embed timeout (attempt %d/%d), retrying in %.0fs: %s",
                        attempt,
                        self._MAX_ATTEMPTS,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        else:
            raise last_exc  # type: ignore[misc]
        vec = data["embeddings"][0]
        # Truncate or pad to EMBED_DIM for compatibility with existing vec0 tables
        if len(vec) > EMBED_DIM:
            vec = vec[:EMBED_DIM]
        elif len(vec) < EMBED_DIM:
            vec = vec + [0.0] * (EMBED_DIM - len(vec))
        return vec

    def warmup(self) -> None:
        """Send a throwaway embed to warm up the Ollama model.

        If Ollama is cold-starting or loading the model, the first real embed
        call can hit the per-attempt timeout.  Calling warmup() before the main
        workload absorbs that startup latency.  If the warmup itself fails after
        all retries, the exception propagates — caller decides how to handle it.
        """
        self.embed("warmup")


def _is_ollama_available() -> bool:
    try:
        req = urllib.request.Request(f"{OLLAMA_HOST.rstrip('/')}/api/tags")
        with urllib.request.urlopen(req, timeout=2):
            return True
    except Exception:
        return False


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return the configured embedding provider (singleton)."""
    global _provider
    if _provider is not None:
        return _provider

    backend = os.environ.get("EMBEDDING_PROVIDER", "auto").lower()

    if backend == "gemini":
        _provider = GeminiEmbeddingProvider()
    elif backend == "ollama":
        _provider = OllamaEmbeddingProvider()
    elif backend == "auto":
        if _is_ollama_available():
            _provider = OllamaEmbeddingProvider()
        else:
            try:
                _provider = GeminiEmbeddingProvider()
            except Exception:
                raise RuntimeError(
                    "No embedding provider available. Start Ollama or set GEMINI_API_KEY."
                )
    else:
        raise ValueError(
            f"Unknown EMBEDDING_PROVIDER={backend!r}. Supported: auto, gemini, ollama"
        )
    return _provider


def embed(text: str) -> list[float]:
    """Convenience: embed a single text using the configured provider."""
    return get_embedding_provider().embed(text)


def warmup_embedding_provider() -> None:
    """Warm up the embedding provider if it supports warmup.

    Uses duck-typing so that Gemini and other providers that don't implement
    warmup() are silently skipped.  Only OllamaEmbeddingProvider defines
    warmup() today.

    Call this once before a batch embedding workload to absorb Ollama cold-start
    latency.  Any exception from warmup() propagates to the caller.
    """
    provider = get_embedding_provider()
    if hasattr(provider, "warmup"):
        provider.warmup()

"""
Pluggable embedding provider.

Default: Gemini (uses EMBED_MODELS from config with automatic fallback).
Override via EMBEDDING_PROVIDER env var (currently only 'gemini' is implemented).
"""
import os
from abc import ABC, abstractmethod

from ..config import EMBED_DIM, EMBED_MODELS, load_api_key


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


_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    """Return the configured embedding provider (singleton)."""
    global _provider
    if _provider is None:
        backend = os.environ.get("EMBEDDING_PROVIDER", "gemini").lower()
        if backend == "gemini":
            _provider = GeminiEmbeddingProvider()
        else:
            raise ValueError(
                f"Unknown EMBEDDING_PROVIDER={backend!r}. Supported: gemini"
            )
    return _provider


def embed(text: str) -> list[float]:
    """Convenience: embed a single text using the configured provider."""
    return get_embedding_provider().embed(text)

"""
Pluggable embedding provider.

Default: Ollama with EmbeddingGemma (local, ~7x faster than Gemini API).
Override via EMBEDDING_PROVIDER env var: 'gemini', 'ollama', or 'auto'.
'auto' (default) tries Ollama first, falls back to Gemini if unavailable.

Long-running workloads: the Ollama provider supports batch embedding
(`embed_batch`) and reuses a persistent HTTP connection. Both matter when
indexing hundreds of chunks — sequential + one-shot HTTP per call is what
caused the LongMemEval hang at ~30 examples on 2026-04-18.
"""
import http.client
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod

from ..config import EMBED_DIM, EMBED_MODELS, load_api_key

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "embeddinggemma")
# keep_alive: how long Ollama keeps the embed model loaded between requests.
# "30m" keeps it resident across a long bench run; "0" evicts immediately.
OLLAMA_EMBED_KEEP_ALIVE = os.environ.get("OLLAMA_EMBED_KEEP_ALIVE", "30m")
# Max batch size per embed call. Too large = single request approaches HTTP
# read timeout; too small = HTTP overhead dominates.
OLLAMA_EMBED_BATCH_MAX = int(os.environ.get("OLLAMA_EMBED_BATCH_MAX", "32"))


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
        parsed = urllib.parse.urlparse(host)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or (443 if parsed.scheme == "https" else 11434)
        self._scheme = parsed.scheme or "http"
        self._path = "/api/embed"
        self._conn: http.client.HTTPConnection | None = None
        self._conn_lock = threading.Lock()

    _MAX_ATTEMPTS = 3
    _BACKOFF_BASE = 1.0  # seconds; doubles each retry
    _REQUEST_TIMEOUT = float(os.environ.get("OLLAMA_EMBED_TIMEOUT", "60"))

    @staticmethod
    def _is_timeout(exc: BaseException) -> bool:
        """Return True if exc represents a transient socket timeout or broken
        HTTP connection (incl. peer-reset from a keep-alive dropped server-side).
        """
        if isinstance(exc, (TimeoutError, socket.timeout)):
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, (TimeoutError, socket.timeout)
        ):
            return True
        if isinstance(exc, (http.client.HTTPException, ConnectionError)):
            return True
        return False

    def _get_conn(self) -> http.client.HTTPConnection:
        if self._conn is None:
            if self._scheme == "https":
                self._conn = http.client.HTTPSConnection(
                    self._host, self._port, timeout=self._REQUEST_TIMEOUT
                )
            else:
                self._conn = http.client.HTTPConnection(
                    self._host, self._port, timeout=self._REQUEST_TIMEOUT
                )
        return self._conn

    def _reset_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _post_embed(self, inputs: list[str]) -> list[list[float]]:
        """POST one batch to /api/embed and return the embeddings list.

        Uses a persistent HTTP/1.1 keep-alive connection. On any exception the
        connection is dropped so the next call reconnects cleanly — avoids the
        "half-closed socket" class of hangs seen under sustained load.
        """
        payload = json.dumps(
            {
                "model": self._model,
                "input": inputs,
                "keep_alive": OLLAMA_EMBED_KEEP_ALIVE,
            }
        ).encode()
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        with self._conn_lock:
            try:
                conn = self._get_conn()
                conn.request("POST", self._path, body=payload, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
                if resp.status != 200:
                    self._reset_conn()
                    raise RuntimeError(
                        f"Ollama embed returned HTTP {resp.status}: {body[:200]!r}"
                    )
            except Exception:
                self._reset_conn()
                raise
        data = json.loads(body)
        return data["embeddings"]

    @staticmethod
    def _normalize_vec(vec: list[float]) -> list[float]:
        if len(vec) > EMBED_DIM:
            return vec[:EMBED_DIM]
        if len(vec) < EMBED_DIM:
            return vec + [0.0] * (EMBED_DIM - len(vec))
        return vec

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Batch-embed in a single HTTP call, chunked by OLLAMA_EMBED_BATCH_MAX.

        Each sub-batch retries independently on socket timeout with exponential
        backoff. The persistent connection is reset between retries.
        """
        if not texts:
            return []

        out: list[list[float]] = []
        for start in range(0, len(texts), OLLAMA_EMBED_BATCH_MAX):
            chunk = texts[start : start + OLLAMA_EMBED_BATCH_MAX]
            last_exc: BaseException | None = None
            for attempt in range(1, self._MAX_ATTEMPTS + 1):
                try:
                    vecs = self._post_embed(chunk)
                    break
                except BaseException as exc:
                    if not self._is_timeout(exc):
                        raise
                    last_exc = exc
                    if attempt < self._MAX_ATTEMPTS:
                        delay = self._BACKOFF_BASE * (2 ** (attempt - 1))
                        logger.warning(
                            "Ollama embed timeout (attempt %d/%d, batch=%d), retrying in %.0fs: %s",
                            attempt,
                            self._MAX_ATTEMPTS,
                            len(chunk),
                            delay,
                            exc,
                        )
                        time.sleep(delay)
            else:
                raise last_exc  # type: ignore[misc]
            out.extend(self._normalize_vec(v) for v in vecs)
        return out

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


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Convenience: batch-embed a list of texts using the configured provider.

    Falls through to the provider's `embed_batch`, which is natively batched for
    Ollama (single HTTP call per sub-batch) and sequential for Gemini (the Gemini
    SDK doesn't expose a matching batch primitive at the dim we use).
    """
    return get_embedding_provider().embed_batch(texts)


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

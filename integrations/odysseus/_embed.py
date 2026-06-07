"""Shared Ollama embedder for the Odysseus curated-memory bridge.

Used by BOTH the build-time indexer (``build_share_index.py``) and the runtime
sidecar (``share_mcp_server.py``) so query vectors and stored vectors are
produced by the exact same code path — guaranteeing parity.

Deliberately stdlib-only and self-contained: it does NOT import any Deus
package. That keeps the Docker sidecar image tiny and, more importantly, means
the sidecar never imports code that could reach the vault or other databases.
It only knows how to turn text into a 768-dim vector via Ollama.

API shape mirrors Deus production (``evolution/providers/embeddings.py``):
    POST {OLLAMA_HOST}/api/embed
    body  {"model": "embeddinggemma", "input": [text], "keep_alive": "30m"}
    resp  {"embeddings": [[float, ... 768]]}   # read ["embeddings"][0]
Vectors are normalized to EMBED_DIM (truncate if longer, zero-pad if shorter),
identical to the production ``_normalize_vec``.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

EMBED_DIM = 768
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_EMBED_MODEL = os.environ.get("OLLAMA_EMBED_MODEL", "embeddinggemma")
OLLAMA_EMBED_KEEP_ALIVE = os.environ.get("OLLAMA_EMBED_KEEP_ALIVE", "30m")
_TIMEOUT = float(os.environ.get("OLLAMA_EMBED_TIMEOUT", "60"))


def _normalize_vec(vec: list[float]) -> list[float]:
    """Coerce to exactly EMBED_DIM floats (matches production behavior)."""
    if len(vec) > EMBED_DIM:
        return vec[:EMBED_DIM]
    if len(vec) < EMBED_DIM:
        return vec + [0.0] * (EMBED_DIM - len(vec))
    return vec


def embed(text: str) -> list[float]:
    """Return a normalized EMBED_DIM embedding for ``text``.

    Raises on any failure (HTTP error, missing key, empty vector) so callers —
    the build smoke test and the sidecar's fail-fast path — surface problems
    loudly instead of silently storing/querying a garbage vector.
    """
    url = f"{OLLAMA_HOST.rstrip('/')}/api/embed"
    payload = json.dumps(
        {
            "model": OLLAMA_EMBED_MODEL,
            "input": [text],
            "keep_alive": OLLAMA_EMBED_KEEP_ALIVE,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    # urlopen raises HTTPError on any non-2xx, so there is no success path with a
    # non-200 status to check — convert it to a clear message for the caller.
    # Uniform error surface: HTTP status errors and connection/DNS/timeout
    # failures both become RuntimeError so callers see one exception type.
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ollama /api/embed returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama /api/embed unreachable: {exc.reason}") from exc

    vectors = data.get("embeddings")
    if not vectors or not isinstance(vectors, list) or not vectors[0]:
        raise RuntimeError(
            f"Ollama /api/embed returned no embedding (keys={list(data)}); "
            "is the model pulled and OLLAMA_HOST reachable?"
        )
    return _normalize_vec([float(x) for x in vectors[0]])


if __name__ == "__main__":
    # Manual smoke test: python _embed.py "some text"
    import sys

    sample = sys.argv[1] if len(sys.argv) > 1 else "hello world"
    v = embed(sample)
    print(f"embed({sample!r}) -> len={len(v)} first3={v[:3]}")
    assert len(v) == EMBED_DIM, f"expected {EMBED_DIM} dims, got {len(v)}"
    print("OK")

"""Tests for OllamaEmbeddingProvider batched + retry-with-backoff logic."""
import http.client
import json
import socket
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from evolution.providers.embeddings import (
    OllamaEmbeddingProvider,
    warmup_embedding_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMBED_DIM = 768  # must match config.EMBED_DIM
_FAKE_VEC = [0.1] * _EMBED_DIM


def _ok_response(vecs: list[list[float]] | None = None) -> MagicMock:
    """Mock http.client HTTPResponse returning a 200 with embeddings body."""
    if vecs is None:
        vecs = [_FAKE_VEC]
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps({"embeddings": vecs}).encode()
    return resp


def _mock_conn(responses: list, exceptions_on_request: list | None = None) -> MagicMock:
    """Mock HTTPConnection whose getresponse() returns each element of `responses`
    in order. If `exceptions_on_request` is provided, those are raised by
    `request()` instead of producing a response.
    """
    conn = MagicMock(spec=http.client.HTTPConnection)
    if exceptions_on_request:
        conn.request.side_effect = exceptions_on_request + [None] * len(responses)
    conn.getresponse.side_effect = responses
    return conn


# ---------------------------------------------------------------------------
# Happy-path: no retry needed
# ---------------------------------------------------------------------------


def test_embed_success_no_retry():
    """Single successful call returns the vector without any retry."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    mock = _mock_conn([_ok_response()])
    with patch.object(provider, "_get_conn", return_value=mock):
        result = provider.embed("hello")
    mock.request.assert_called_once()
    assert result == _FAKE_VEC


def test_embed_batch_single_http_call():
    """embed_batch for a small list issues exactly one http request."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    texts = ["a", "b", "c"]
    mock = _mock_conn([_ok_response(vecs=[_FAKE_VEC] * 3)])
    with patch.object(provider, "_get_conn", return_value=mock):
        out = provider.embed_batch(texts)
    assert mock.request.call_count == 1
    assert len(out) == 3


def test_embed_batch_chunked_when_over_batch_max(monkeypatch):
    """Over OLLAMA_EMBED_BATCH_MAX triggers multiple requests."""
    monkeypatch.setattr(
        "evolution.providers.embeddings.OLLAMA_EMBED_BATCH_MAX", 2
    )
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    texts = ["a", "b", "c", "d", "e"]
    responses = [
        _ok_response(vecs=[_FAKE_VEC, _FAKE_VEC]),
        _ok_response(vecs=[_FAKE_VEC, _FAKE_VEC]),
        _ok_response(vecs=[_FAKE_VEC]),
    ]
    mock = _mock_conn(responses)
    with patch.object(provider, "_get_conn", return_value=mock):
        out = provider.embed_batch(texts)
    assert mock.request.call_count == 3
    assert len(out) == 5


# ---------------------------------------------------------------------------
# Retry: TimeoutError twice then success
# ---------------------------------------------------------------------------


def test_embed_retries_on_timeout_error():
    """getresponse() raises TimeoutError twice, then succeeds on attempt 3."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    # Each attempt rebuilds the connection, so we need a fresh mock per attempt.
    call_seq = iter([
        TimeoutError("timed out"),
        TimeoutError("timed out"),
        _ok_response(),
    ])

    def fake_get_conn():
        item = next(call_seq)
        conn = MagicMock(spec=http.client.HTTPConnection)
        if isinstance(item, Exception):
            conn.getresponse.side_effect = item
        else:
            conn.getresponse.return_value = item
        return conn

    with patch.object(provider, "_get_conn", side_effect=fake_get_conn), \
         patch("time.sleep") as mock_sleep:
        result = provider.embed("hello")

    assert mock_sleep.call_count == 2
    assert mock_sleep.call_args_list == [call(1.0), call(2.0)]
    assert result == _FAKE_VEC


def test_embed_retries_on_socket_timeout():
    """socket.timeout is also treated as a transient timeout."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    call_seq = iter([socket.timeout("timed out"), _ok_response()])

    def fake_get_conn():
        item = next(call_seq)
        conn = MagicMock(spec=http.client.HTTPConnection)
        if isinstance(item, Exception):
            conn.getresponse.side_effect = item
        else:
            conn.getresponse.return_value = item
        return conn

    with patch.object(provider, "_get_conn", side_effect=fake_get_conn), \
         patch("time.sleep"):
        result = provider.embed("hello")
    assert result == _FAKE_VEC


def test_embed_retries_on_http_client_exception():
    """http.client.HTTPException (e.g., BadStatusLine) is treated as transient —
    a keep-alive connection dropped server-side hits this path."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    call_seq = iter([
        http.client.BadStatusLine("connection dropped"),
        _ok_response(),
    ])

    def fake_get_conn():
        item = next(call_seq)
        conn = MagicMock(spec=http.client.HTTPConnection)
        if isinstance(item, Exception):
            conn.getresponse.side_effect = item
        else:
            conn.getresponse.return_value = item
        return conn

    with patch.object(provider, "_get_conn", side_effect=fake_get_conn), \
         patch("time.sleep"):
        result = provider.embed("hello")
    assert result == _FAKE_VEC


# ---------------------------------------------------------------------------
# Exhaust retries: final error is re-raised (fail-loud rule)
# ---------------------------------------------------------------------------


def test_embed_raises_after_all_retries_exhausted():
    """After MAX_ATTEMPTS timeouts the original exception propagates (fail-loud)."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")

    def fake_get_conn():
        conn = MagicMock(spec=http.client.HTTPConnection)
        conn.getresponse.side_effect = TimeoutError("timed out")
        return conn

    with patch.object(provider, "_get_conn", side_effect=fake_get_conn), \
         patch("time.sleep"):
        with pytest.raises(TimeoutError):
            provider.embed("hello")


# ---------------------------------------------------------------------------
# Non-timeout errors are NOT retried
# ---------------------------------------------------------------------------


def test_embed_does_not_retry_on_http_500():
    """HTTP 500 surfaces as RuntimeError on first attempt — no retry."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    err_resp = MagicMock()
    err_resp.status = 500
    err_resp.read.return_value = b"internal error"
    mock = _mock_conn([err_resp])

    with patch.object(provider, "_get_conn", return_value=mock), \
         patch("time.sleep") as mock_sleep:
        with pytest.raises(RuntimeError):
            provider.embed("hello")
    mock_sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Connection reset on exception — the post-timeout conn is dropped so the
# next call reconnects cleanly (prevents the half-closed-socket hang class).
# ---------------------------------------------------------------------------


def test_connection_reset_after_exception():
    """After any _post_embed exception, _conn is reset to None."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    mock = MagicMock(spec=http.client.HTTPConnection)
    mock.getresponse.side_effect = TimeoutError("timed out")
    provider._conn = mock

    with pytest.raises(TimeoutError):
        provider._post_embed(["hello"])
    assert provider._conn is None


# ---------------------------------------------------------------------------
# keep_alive + batched payload shape
# ---------------------------------------------------------------------------


def test_payload_includes_keep_alive_and_batch_input():
    """Payload sent to /api/embed contains keep_alive and an input array."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    mock = _mock_conn([_ok_response(vecs=[_FAKE_VEC] * 2)])
    with patch.object(provider, "_get_conn", return_value=mock):
        provider.embed_batch(["a", "b"])

    kwargs = mock.request.call_args.kwargs
    body = kwargs.get("body")
    if body is None:
        body = mock.request.call_args.args[2]
    payload = json.loads(body)
    assert payload["input"] == ["a", "b"]
    assert payload["model"] == "test-model"
    assert "keep_alive" in payload


# ---------------------------------------------------------------------------
# warmup() on OllamaEmbeddingProvider
# ---------------------------------------------------------------------------


def test_warmup_calls_embed_with_warmup_string():
    """warmup() must call embed() exactly once with the string 'warmup'."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")
    mock = _mock_conn([_ok_response()])
    with patch.object(provider, "_get_conn", return_value=mock), \
         patch("time.sleep"):
        provider.warmup()
    mock.request.assert_called_once()
    body = mock.request.call_args.kwargs.get("body") or mock.request.call_args.args[2]
    assert '"warmup"' in body.decode()


def test_warmup_raises_on_failure_after_retries():
    """warmup() must propagate the exception if embed() exhausts all retries."""
    provider = OllamaEmbeddingProvider(model="test-model", host="http://localhost:11434")

    def fake_get_conn():
        conn = MagicMock(spec=http.client.HTTPConnection)
        conn.getresponse.side_effect = TimeoutError("timed out")
        return conn

    with patch.object(provider, "_get_conn", side_effect=fake_get_conn), \
         patch("time.sleep"):
        with pytest.raises(TimeoutError):
            provider.warmup()


# ---------------------------------------------------------------------------
# warmup_embedding_provider() facade
# ---------------------------------------------------------------------------


def test_warmup_embedding_provider_calls_warmup_on_ollama():
    """warmup_embedding_provider() calls warmup() when provider has that method."""
    mock_provider = MagicMock(spec=OllamaEmbeddingProvider)

    with patch(
        "evolution.providers.embeddings.get_embedding_provider",
        return_value=mock_provider,
    ):
        warmup_embedding_provider()
    mock_provider.warmup.assert_called_once_with()


def test_warmup_embedding_provider_skips_providers_without_warmup():
    """warmup_embedding_provider() is a no-op for providers that don't define warmup()."""
    from evolution.providers.embeddings import GeminiEmbeddingProvider

    mock_provider = MagicMock(spec=GeminiEmbeddingProvider)
    assert not hasattr(mock_provider, "warmup")

    with patch(
        "evolution.providers.embeddings.get_embedding_provider",
        return_value=mock_provider,
    ):
        warmup_embedding_provider()  # should not raise

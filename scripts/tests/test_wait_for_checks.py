"""Unit tests for scripts/ci/wait_for_checks.py.

The poll loop is exercised by faking `_query_checks` (the gh-checks JSON
parser) and no-op'ing `time.sleep`; timeout paths drive a fake monotonic clock.
No real subprocess or wall-clock waits.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

_CI_DIR = Path(__file__).resolve().parents[1] / "ci"


def load_wfc():
    if "wait_for_checks" in sys.modules:
        return sys.modules["wait_for_checks"]
    spec = importlib.util.spec_from_file_location(
        "wait_for_checks", _CI_DIR / "wait_for_checks.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["wait_for_checks"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def wfc():
    return load_wfc()


def _advancing_clock(step=5.0):
    state = {"v": 0.0}

    def clock():
        state["v"] += step
        return state["v"]

    return clock


def _sequence(*values):
    """Fake _query_checks that yields each value once, then repeats the last."""
    calls = {"i": 0}

    def fake(pr, *, required):
        v = values[min(calls["i"], len(values) - 1)]
        calls["i"] += 1
        return v

    return fake


# ── terminal states ──────────────────────────────────────────────────────────


def test_all_required_green(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "_query_checks",
        lambda pr, *, required: [
            {"name": "ci", "bucket": "pass"},
            {"name": "lint", "bucket": "skipping"},  # skipped == pass
        ],
    )
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is True
    assert "green" in detail


def test_required_failure_is_not_green(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "_query_checks",
        lambda pr, *, required: [
            {"name": "ci", "bucket": "pass"},
            {"name": "test-windows", "bucket": "fail"},
        ],
    )
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is False
    assert "not green" in detail
    assert "test-windows" in detail


def test_unknown_bucket_fails_closed(wfc, monkeypatch):
    """An unrecognized terminal bucket (gh output drift) must NOT be green —
    green is a positive pass/skipping allowlist, not a fail/cancel blocklist."""
    monkeypatch.setattr(
        wfc, "_query_checks",
        lambda pr, *, required: [{"name": "ci", "bucket": "neutral"}],
    )
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is False
    assert "ci" in detail


def test_pending_then_green(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "_query_checks",
        _sequence([{"name": "ci", "bucket": "pending"}], [{"name": "ci", "bucket": "pass"}]),
    )
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    green, _ = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is True


# ── zero-registered-checks disambiguation ────────────────────────────────────


def test_no_checks_registered_is_not_green(wfc, monkeypatch):
    """Required `[]` + unfiltered `[]` → checks not registered yet → retried to
    timeout, never reported green."""
    monkeypatch.setattr(wfc, "_query_checks", lambda pr, *, required: [])
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    monkeypatch.setattr(wfc.time, "monotonic", _advancing_clock())
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=1)
    assert green is False
    assert "no checks registered" in detail


def test_checks_exist_but_none_required_fails_closed(wfc, monkeypatch):
    """Required `[]` while unfiltered has checks → none are *required* → False."""

    def fake(pr, *, required):
        return [] if required else [{"name": "advisory", "bucket": "pass"}]

    monkeypatch.setattr(wfc, "_query_checks", fake)
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is False
    assert "none required" in detail


# ── timeout & retry ──────────────────────────────────────────────────────────


def test_timeout_while_pending(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "_query_checks", lambda pr, *, required: [{"name": "ci", "bucket": "pending"}]
    )
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    monkeypatch.setattr(wfc.time, "monotonic", _advancing_clock())
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=1)
    assert green is False
    assert "still pending" in detail


def test_transient_read_failure_recovers(wfc, monkeypatch):
    """Two unreadable polls (None) then a green list → recovers (retries reset)."""
    monkeypatch.setattr(
        wfc, "_query_checks",
        _sequence(None, None, [{"name": "ci", "bucket": "pass"}]),
    )
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    green, _ = wfc.wait_for_required_checks(1, interval=0, timeout=600, retries=5)
    assert green is True


def test_retries_exhausted(wfc, monkeypatch):
    monkeypatch.setattr(wfc, "_query_checks", lambda pr, *, required: None)
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=600, retries=3)
    assert green is False
    assert "unreadable" in detail


# ── bucket parsing helper ────────────────────────────────────────────────────


def test_bucket_prefers_bucket_then_state(wfc):
    assert wfc._bucket({"bucket": "PASS"}) == "pass"
    assert wfc._bucket({"state": "FAILURE"}) == "failure"
    assert wfc._bucket({}) == ""

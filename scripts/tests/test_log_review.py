"""Tests for the LIA-195 evolution-logging heartbeat in log_review.py.

The pure verdict is the load-bearing logic (unit-tested exhaustively); the IO
wrappers get light coverage (skip-group exclusion, missing-DB fail-safe, wiring).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import log_review as lr  # noqa: E402

HOUR = 3600.0
NOW = 1_000_000.0  # arbitrary fixed "now" epoch


# ── pure verdict: truth table ────────────────────────────────────────────────

def test_verdict_no_dispatch_proxy_not_stale():
    # no activity signal at all -> can't judge -> not stale
    assert lr._heartbeat_verdict(None, NOW - 100 * HOUR, NOW, 48, 48) == (False, None)


def test_verdict_idle_system_not_stale_even_if_log_old():
    # last dispatch 100h ago (> 48h window) -> idle -> never alarm
    stale, _ = lr._heartbeat_verdict(NOW - 100 * HOUR, NOW - 200 * HOUR, NOW, 48, 48)
    assert stale is False


def test_verdict_recent_dispatch_fresh_log_not_stale():
    # dispatch 2h ago, last interaction 2h ago -> healthy
    stale, _ = lr._heartbeat_verdict(NOW - 2 * HOUR, NOW - 2 * HOUR, NOW, 48, 48)
    assert stale is False


def test_verdict_recent_dispatch_stale_log_is_stale():
    # dispatch 5h ago but last interaction 60h ago (> 48h) -> STALE
    stale, reason = lr._heartbeat_verdict(NOW - 5 * HOUR, NOW - 60 * HOUR, NOW, 48, 48)
    assert stale is True
    assert reason and '60h' in reason


def test_verdict_recent_dispatch_no_log_is_stale():
    # dispatch 5h ago, evolution.db has no interactions -> STALE
    stale, reason = lr._heartbeat_verdict(NOW - 5 * HOUR, None, NOW, 48, 48)
    assert stale is True
    assert reason and 'no logged interactions' in reason


def test_verdict_low_traffic_healthy_day_not_stale():
    # reviewer's false-positive vector: recent dispatch + last interaction 30h ago,
    # within the 48h stale window -> NOT stale (the day's real interaction logged fine).
    stale, _ = lr._heartbeat_verdict(NOW - 1 * HOUR, NOW - 30 * HOUR, NOW, 48, 48)
    assert stale is False


def test_verdict_boundary_at_stale_threshold():
    # exactly at the threshold is not yet stale (> is strict)
    assert lr._heartbeat_verdict(NOW, NOW - 48 * HOUR, NOW, 48, 48)[0] is False
    assert lr._heartbeat_verdict(NOW, NOW - 48.1 * HOUR, NOW, 48, 48)[0] is True


# ── IO wrappers ──────────────────────────────────────────────────────────────

def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('x')
    import os
    os.utime(path, (mtime, mtime))


def test_last_dispatch_activity_excludes_skip_groups(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, 'GROUPS_DIR', tmp_path)
    monkeypatch.setenv('EVOLUTION_SKIP_GROUPS', 'skipgrp')
    # skip group has the NEWEST activity; it must be ignored
    _touch(tmp_path / 'skipgrp' / 'logs' / 'usage.jsonl', NOW)
    _touch(tmp_path / 'realgrp' / 'logs' / 'container-2026.log', NOW - 10 * HOUR)
    assert lr._last_dispatch_activity() == NOW - 10 * HOUR


def test_last_dispatch_activity_none_when_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, 'GROUPS_DIR', tmp_path)
    monkeypatch.setenv('EVOLUTION_SKIP_GROUPS', '')
    assert lr._last_dispatch_activity() is None


def test_last_interaction_ts_missing_db_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(lr, 'EVOLUTION_DB', tmp_path / 'nonexistent.db')
    assert lr._last_interaction_ts() is None


def test_last_interaction_ts_reads_max(tmp_path, monkeypatch):
    db_path = tmp_path / 'evo.db'
    db = sqlite3.connect(db_path)
    db.execute('CREATE TABLE interactions (timestamp TEXT)')
    db.executemany(
        'INSERT INTO interactions VALUES (?)',
        [('2026-06-01T00:00:00+00:00',), ('2026-06-07T20:51:34.217044+00:00',)],
    )
    db.commit()
    db.close()
    monkeypatch.setattr(lr, 'EVOLUTION_DB', db_path)
    from datetime import datetime
    expected = datetime.fromisoformat('2026-06-07T20:51:34.217044+00:00').timestamp()
    assert lr._last_interaction_ts() == expected


# ── wiring ───────────────────────────────────────────────────────────────────

def test_check_evolution_heartbeat_wiring_stale(monkeypatch):
    monkeypatch.setattr(lr, '_last_dispatch_activity', lambda: NOW - 1 * HOUR)
    monkeypatch.setattr(lr, '_last_interaction_ts', lambda: NOW - 100 * HOUR)
    monkeypatch.setattr(lr, 'HEARTBEAT_ACTIVITY_WINDOW_H', 48.0)
    monkeypatch.setattr(lr, 'HEARTBEAT_STALE_H', 48.0)

    class _FixedNow:
        @staticmethod
        def timestamp():
            return NOW

    monkeypatch.setattr(lr, 'utc_now', lambda: _FixedNow())
    stale, reason = lr.check_evolution_heartbeat()
    assert stale is True and reason


def test_check_evolution_heartbeat_failsafe(monkeypatch):
    def _boom():
        raise RuntimeError('db blew up')

    monkeypatch.setattr(lr, '_last_dispatch_activity', _boom)
    # must never propagate — heartbeat can't break the log review
    assert lr.check_evolution_heartbeat() == (False, None)


# ── run_review integration: silent outage must report + pin (once) ───────────

def test_run_review_pins_on_silent_heartbeat_outage(tmp_path, monkeypatch):
    # No log entries at all (the LIA-194 silent-outage shape) + heartbeat stale:
    # run_review must NOT early-return "healthy"; it must write a DEGRADED report
    # and pin EXACTLY once (heartbeat folded into the single existing pin path).
    monkeypatch.setattr(lr, 'LOGS_DIR', tmp_path / 'logs')          # absent -> no entries
    monkeypatch.setattr(lr, 'GROUPS_DIR', tmp_path / 'groups')      # absent -> no container logs
    monkeypatch.setattr(lr, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(lr, 'REPORTS_DIR', tmp_path / 'reviews')
    monkeypatch.setattr(
        lr, 'check_evolution_heartbeat', lambda: (True, 'last interaction 120h ago')
    )
    monkeypatch.setattr(lr, '_ollama_available', lambda: False)
    pins: list[str] = []
    monkeypatch.setattr(
        lr, '_pin_issue', lambda date, analysis, path: pins.append(analysis)
    )

    report_path = lr.run_review()

    assert report_path is not None  # did NOT early-return as healthy
    content = report_path.read_text()
    assert '## Health: DEGRADED' in content
    assert 'heartbeat' in content.lower() and '120h' in content
    assert len(pins) == 1                       # single pin, not double
    assert 'heartbeat' in pins[0].lower()       # the pinned analysis names it

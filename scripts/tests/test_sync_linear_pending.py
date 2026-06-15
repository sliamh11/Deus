"""Tests for scripts/sync_linear_pending.py — multi-team Scatter-Gather fetch.

The GraphQL layer is fully mocked: no network, no Linear credentials (CI has
none). Covers the all-teams discovery, per-team merge + identifier dedup, the
partial-success path (one flaky team must not blank the block), the fail-loud
auth/rate paths, and the zero-success → INTERNAL_ERROR contract that tells the
caller to KEEP its existing pending block.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

_SPEC = importlib.util.spec_from_file_location(
    "sync_linear_pending", _SCRIPTS / "sync_linear_pending.py"
)
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def _issue(identifier: str, title: str = "t", state: str = "Todo") -> dict:
    return {
        "title": title,
        "identifier": identifier,
        "state": {"name": state, "type": "unstarted"},
    }


def _make_graphql(teams: list[str], per_team: dict):
    """Fake _graphql: the teams-discovery query returns `teams`; each per-team
    issues query returns per_team[teamId] (a node list) or raises it if it is
    an Exception instance."""

    def fake(token, query, variables=None):
        if "teams" in query and "issues" not in query:
            return {"data": {"teams": {"nodes": [{"id": t, "name": t} for t in teams]}}}
        tid = (variables or {}).get("teamId")
        val = per_team.get(tid, [])
        if isinstance(val, Exception):
            raise val
        return {"data": {"issues": {"nodes": val}}}

    return fake


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://api.linear.app/graphql", code, "err", None, None)


# ── _get_team_ids precedence ──────────────────────────────────────────────


def test_get_team_ids_csv_override(monkeypatch):
    monkeypatch.setattr(mod, "_read_env_file", lambda: {})
    monkeypatch.setenv("LINEAR_TEAM_IDS", "a, b ,c")
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    assert mod._get_team_ids() == ["a", "b", "c"]


def test_get_team_ids_legacy_single(monkeypatch):
    monkeypatch.setattr(mod, "_read_env_file", lambda: {})
    monkeypatch.delenv("LINEAR_TEAM_IDS", raising=False)
    monkeypatch.setenv("LINEAR_TEAM_ID", "solo")
    assert mod._get_team_ids() == ["solo"]


def test_get_team_ids_empty_signals_discover_all(monkeypatch):
    monkeypatch.setattr(mod, "_read_env_file", lambda: {})
    monkeypatch.delenv("LINEAR_TEAM_IDS", raising=False)
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    assert mod._get_team_ids() == []


def test_discover_team_ids_returns_all(monkeypatch):
    monkeypatch.setattr(mod, "_graphql", _make_graphql(["t1", "t2", "t3"], {}))
    assert mod._discover_team_ids("tok") == ["t1", "t2", "t3"]


# ── main() Scatter-Gather ─────────────────────────────────────────────────


@pytest.fixture
def run_main(tmp_path, monkeypatch):
    cache = tmp_path / "cache.md"
    db = tmp_path / "messages.db"  # absent → cache never counts as fresh
    monkeypatch.setattr(mod, "_cache_path", lambda: cache)
    monkeypatch.setattr(mod, "_db_path", lambda: db)
    monkeypatch.setattr(mod, "_read_env_file", lambda: {})
    monkeypatch.setenv("LINEAR_API_TOKEN", "tok")
    monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
    monkeypatch.delenv("LINEAR_TEAM_IDS", raising=False)

    def _run(teams, per_team):
        monkeypatch.setattr(mod, "_graphql", _make_graphql(teams, per_team))
        code = mod.main()
        out = cache.read_text() if cache.exists() else None
        return code, out

    return _run


def test_main_merges_multiple_teams(run_main):
    code, out = run_main(["t1", "t2"], {"t1": [_issue("LIA-1")], "t2": [_issue("FOR-9")]})
    assert code == mod.SUCCESS
    assert "LIA-1" in out and "FOR-9" in out


def test_main_dedups_identifier_across_teams(run_main):
    code, out = run_main(["t1", "t2"], {"t1": [_issue("LIA-1")], "t2": [_issue("LIA-1")]})
    assert code == mod.SUCCESS
    assert out.count("LIA-1") == 1


def test_main_partial_success_skips_flaky_team(run_main):
    # One team errors with a transient network failure — the other still lands.
    code, out = run_main(
        ["t1", "t2"], {"t1": [_issue("LIA-1")], "t2": URLError("boom")}
    )
    assert code == mod.SUCCESS
    assert "LIA-1" in out


def test_main_auth_error_is_fail_loud(run_main):
    code, out = run_main(
        ["t1", "t2"], {"t1": _http_error(401), "t2": [_issue("LIA-1")]}
    )
    assert code == mod.AUTH_ERROR
    assert out is None  # cache untouched → caller keeps existing block


def test_main_rate_limit_is_fail_loud(run_main):
    code, out = run_main(["t1"], {"t1": _http_error(429)})
    assert code == mod.RATE_LIMIT


def test_main_zero_success_preserves_block(run_main):
    code, out = run_main(["t1", "t2"], {"t1": URLError("x"), "t2": URLError("y")})
    assert code == mod.INTERNAL_ERROR
    assert out is None  # nothing written, existing pending block survives


def test_main_excluded_states_filtered(run_main):
    code, out = run_main(
        ["t1"], {"t1": [_issue("LIA-1", state="Done"), _issue("LIA-2", state="Todo")]}
    )
    assert code == mod.SUCCESS
    assert "LIA-2" in out and "LIA-1" not in out


def test_main_icebox_state_filtered(run_main):
    # Icebox = someday/maybe ideas; kept in Linear but excluded from the block.
    code, out = run_main(
        ["t1"],
        {"t1": [_issue("LIA-1", state="Icebox"), _issue("LIA-2", state="Todo")]},
    )
    assert code == mod.SUCCESS
    assert "LIA-2" in out and "LIA-1" not in out


def test_main_single_team_output_stays_flat(run_main):
    # Back-compat: a single-team workspace renders exactly as before — header
    # line + one issue line, no per-project sub-headers.
    code, out = run_main(["t1"], {"t1": [_issue("LIA-2", title="hello")]})
    assert code == mod.SUCCESS
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[1] == "  - [ ] hello (LIA-2)"


# ── pagination ────────────────────────────────────────────────────────────


def test_fetch_team_issues_follows_cursor(monkeypatch):
    # Two pages: page 1 (hasNextPage, cursor c1) → page 2 (end). Both must be
    # returned, proving the endCursor is threaded into the next request.
    def paging(token, query, variables=None):
        after = (variables or {}).get("after")
        if after is None:
            return {
                "data": {
                    "issues": {
                        "nodes": [_issue("LIA-1")],
                        "pageInfo": {"hasNextPage": True, "endCursor": "c1"},
                    }
                }
            }
        assert after == "c1"  # the cursor was carried forward
        return {
            "data": {
                "issues": {
                    "nodes": [_issue("LIA-2")],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        }

    monkeypatch.setattr(mod, "_graphql", paging)
    got = mod._fetch_team_issues("tok", "t1")
    assert [n["identifier"] for n in got] == ["LIA-1", "LIA-2"]


def test_fetch_team_issues_missing_pageinfo_is_single_page(monkeypatch):
    # Back-compat: a response without pageInfo (the shape the other mocks use)
    # is treated as a single page — no infinite loop, no second request.
    calls = {"n": 0}

    def one_page(token, query, variables=None):
        calls["n"] += 1
        return {"data": {"issues": {"nodes": [_issue("LIA-1")]}}}

    monkeypatch.setattr(mod, "_graphql", one_page)
    got = mod._fetch_team_issues("tok", "t1")
    assert [n["identifier"] for n in got] == ["LIA-1"]
    assert calls["n"] == 1


def test_fetch_team_issues_respects_page_cap(monkeypatch, capsys):
    # A team that never stops paginating must terminate at MAX_PAGES_PER_TEAM
    # and emit a visible stderr warning rather than looping forever.
    def always_more(token, query, variables=None):
        return {
            "data": {
                "issues": {
                    "nodes": [_issue("LIA-1")],
                    "pageInfo": {"hasNextPage": True, "endCursor": "c"},
                }
            }
        }

    monkeypatch.setattr(mod, "_graphql", always_more)
    got = mod._fetch_team_issues("tok", "t1")
    assert len(got) == mod.MAX_PAGES_PER_TEAM
    err = capsys.readouterr().err
    assert (
        f"warning: team t1 hit page cap "
        f"(MAX_PAGES_PER_TEAM={mod.MAX_PAGES_PER_TEAM}); results may be truncated"
        in err
    )

"""Tests for scripts/sync_linear_pending.py.

The GraphQL layer is fully mocked -- no network, no Linear creds (CI has none).
Covers the multi-team Scatter-Gather fetch, partial-success handling, and the
fail-loud auth/rate paths.
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

# Ensure scripts/ is importable
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import sync_linear_pending as slp
from _exit_codes import AUTH_ERROR, INTERNAL_ERROR, RATE_LIMIT, SUCCESS


# ── helpers ───────────────────────────────────────────────────────────────────

def _issue(title: str, identifier: str, state: str = "Todo", team: str | None = None) -> dict:
    node = {"title": title, "identifier": identifier, "state": {"name": state, "type": "x"}}
    if team is not None:
        node["team"] = {"name": team}
    return node


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://api.linear.app/graphql", code, "err", {}, None)


def _is_discovery(query: str) -> bool:
    return "teams {" in query


def _make_graphql(per_team: dict[str, list[dict]],
                  discover: list[str] | None = None,
                  raise_for: dict[str, BaseException] | None = None):
    """Build a fake _graphql.

    per_team   : teamId -> list of issue nodes the issues query returns.
    discover   : team ids the discovery query returns (defaults to per_team keys).
    raise_for  : teamId -> exception to raise instead of returning issues;
                 key "__discover__" raises on the discovery query.
    """
    raise_for = raise_for or {}
    discover_ids = discover if discover is not None else list(per_team.keys())

    def _fake(token, query, variables=None):
        if _is_discovery(query):
            if "__discover__" in raise_for:
                raise raise_for["__discover__"]
            return {"data": {"teams": {"nodes": [{"id": t, "name": t} for t in discover_ids]}}}
        tid = (variables or {}).get("teamId")
        if tid in raise_for:
            raise raise_for[tid]
        return {"data": {"issues": {"nodes": per_team.get(tid, [])}}}

    return _fake


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Run slp.main() with cache disabled, a tmp cache dir, and a fake token."""
    cache_file = tmp_path / "linear-pending-cache.md"
    monkeypatch.setattr(slp, "_cache_is_fresh", lambda *a, **k: False)
    monkeypatch.setattr(slp, "_cache_path", lambda: cache_file)
    monkeypatch.setattr(slp, "_get_api_token", lambda: "fake-token")
    # Default: no explicit team ids -> exercise discovery unless a test overrides.
    monkeypatch.setattr(slp, "_get_team_ids", lambda: [])

    def _run():
        return slp.main()

    return _run


# ── _get_team_ids parsing ───────────────────────────────────────────────────

class TestGetTeamIds:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        # Isolate from real env + ~/deus/.env
        monkeypatch.delenv("LINEAR_TEAM_IDS", raising=False)
        monkeypatch.delenv("LINEAR_TEAM_ID", raising=False)
        monkeypatch.setattr(slp, "_read_env_file", lambda: {})

    def test_parses_comma_list(self, monkeypatch):
        monkeypatch.setenv("LINEAR_TEAM_IDS", "a, b ,c")
        assert slp._get_team_ids() == ["a", "b", "c"]

    def test_drops_empty_entries(self, monkeypatch):
        monkeypatch.setenv("LINEAR_TEAM_IDS", "a,,  ,b")
        assert slp._get_team_ids() == ["a", "b"]

    def test_falls_back_to_single(self, monkeypatch):
        monkeypatch.setenv("LINEAR_TEAM_ID", "solo")
        assert slp._get_team_ids() == ["solo"]

    def test_multi_wins_over_single(self, monkeypatch):
        monkeypatch.setenv("LINEAR_TEAM_IDS", "a,b")
        monkeypatch.setenv("LINEAR_TEAM_ID", "solo")
        assert slp._get_team_ids() == ["a", "b"]

    def test_empty_when_unset(self):
        assert slp._get_team_ids() == []

    def test_empty_multi_falls_back_to_single(self, monkeypatch):
        monkeypatch.setenv("LINEAR_TEAM_IDS", "  ,  ")
        monkeypatch.setenv("LINEAR_TEAM_ID", "solo")
        assert slp._get_team_ids() == ["solo"]

    def test_reads_from_env_file(self, monkeypatch):
        monkeypatch.setattr(slp, "_read_env_file", lambda: {"LINEAR_TEAM_IDS": "x,y"})
        assert slp._get_team_ids() == ["x", "y"]


# ── multi-team merge ────────────────────────────────────────────────────────

class TestMultiTeamMerge:
    def test_both_teams_appear(self, run_main, monkeypatch, capsys):
        per_team = {
            "T1": [_issue("Team-A task", "ACME-9", "Todo", team="Acme")],
            "T2": [_issue("Team-B task", "BETA-5", "In Progress", team="Beta")],
        }
        monkeypatch.setattr(slp, "_graphql", _make_graphql(per_team, discover=["T1", "T2"]))
        assert run_main() == SUCCESS
        out = capsys.readouterr().out
        assert "ACME-9" in out
        assert "BETA-5" in out

    def test_excluded_states_dropped(self, run_main, monkeypatch, capsys):
        per_team = {
            "T1": [_issue("Open", "ACME-1", "Todo"), _issue("Closed", "ACME-2", "Done")],
        }
        monkeypatch.setattr(slp, "_graphql", _make_graphql(per_team, discover=["T1"]))
        assert run_main() == SUCCESS
        out = capsys.readouterr().out
        assert "ACME-1" in out
        assert "ACME-2" not in out

    def test_dedup_by_identifier(self, run_main, monkeypatch, capsys):
        # Same id discovered twice (e.g. repeated team) -> appears once.
        per_team = {"T1": [_issue("Dup", "ACME-3", "Todo")]}
        monkeypatch.setattr(slp, "_graphql", _make_graphql(per_team, discover=["T1", "T1"]))
        assert run_main() == SUCCESS
        out = capsys.readouterr().out
        assert out.count("ACME-3") == 1

    def test_explicit_team_ids_skip_discovery(self, run_main, monkeypatch, capsys):
        monkeypatch.setattr(slp, "_get_team_ids", lambda: ["T2"])
        per_team = {"T1": [_issue("a", "ACME-1")], "T2": [_issue("b", "BETA-5")]}
        monkeypatch.setattr(slp, "_graphql", _make_graphql(per_team, discover=["T1", "T2"]))
        assert run_main() == SUCCESS
        out = capsys.readouterr().out
        assert "BETA-5" in out
        assert "ACME-1" not in out


# ── grouping by project ──────────────────────────────────────────────────────

class TestGrouping:
    def _item_lines(self, out: str) -> list[str]:
        return [ln for ln in out.splitlines() if ln.strip().startswith("- [ ]")]

    def _header_lines(self, out: str) -> list[str]:
        # group headers are indented "# X" comments, excluding the source-of-truth line
        return [
            ln for ln in out.splitlines()
            if ln.strip().startswith("#") and "Source of truth" not in ln
        ]

    def test_groups_contiguous_and_alpha_ordered(self):
        issues = [
            _issue("b1", "BETA-5", "In Progress", team="Beta"),
            _issue("a1", "ACME-9", "Todo", team="Acme"),
            _issue("a2", "ACME-1", "Todo", team="Acme"),
            _issue("b2", "BETA-2", "Todo", team="Beta"),
        ]
        out = slp._format_pending(issues)
        items = self._item_lines(out)
        # All ACME items precede all BETA items (alpha group order), no interleaving.
        prefixes = ["ACME" if "ACME-" in ln else "BETA" for ln in items]
        assert prefixes == ["ACME", "ACME", "BETA", "BETA"]

    def test_intra_group_urgency_sort(self):
        issues = [
            _issue("low", "ACME-2", "Todo", team="Acme"),
            _issue("high", "ACME-9", "In Progress", team="Acme"),
        ]
        out = slp._format_pending(issues)
        # In Progress (priority 0) sorts before Todo (priority 4) within the group.
        assert out.index("ACME-9") < out.index("ACME-2")

    def test_single_group_no_header(self):
        issues = [
            _issue("a", "ACME-1", "Todo", team="Acme"),
            _issue("b", "ACME-2", "Todo", team="Acme"),
        ]
        out = slp._format_pending(issues)
        # Back-compat: a single project emits no group header.
        assert self._header_lines(out) == []

    def test_group_header_format(self):
        issues = [
            _issue("a", "ACME-1", "Todo", team="Acme Corp"),
            _issue("b", "BETA-1", "Todo", team="Beta Team"),
        ]
        out = slp._format_pending(issues)
        lines = out.splitlines()
        assert "  # Acme Corp" in lines
        assert "  # Beta Team" in lines
        # No blank lines between groups (would break the consumer hook regex).
        assert "" not in lines

    def test_header_falls_back_to_prefix_when_team_missing(self):
        issues = [
            _issue("a", "ACME-1", "Todo"),   # no team field
            _issue("b", "BETA-1", "Todo"),
        ]
        out = slp._format_pending(issues)
        lines = out.splitlines()
        assert "  # ACME" in lines
        assert "  # BETA" in lines

    def test_node_missing_identifier_skipped(self):
        issues = [
            _issue("ghost", "", "Todo", team="Acme"),
            _issue("real", "ACME-1", "Todo", team="Acme"),
        ]
        out = slp._format_pending(issues)  # must not raise
        items = self._item_lines(out)
        assert len(items) == 1
        assert "ACME-1" in out
        assert "ghost" not in out


# ── partial success & failure ───────────────────────────────────────────────

class TestPartialAndFailure:
    def test_partial_success_on_transient_error(self, run_main, monkeypatch, capsys):
        per_team = {"T1": [_issue("ok", "ACME-1")], "T2": [_issue("never", "BETA-5")]}
        gql = _make_graphql(per_team, discover=["T1", "T2"],
                            raise_for={"T2": URLError("boom")})
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == SUCCESS
        out = capsys.readouterr().out
        assert "ACME-1" in out
        assert "BETA-5" not in out

    def test_all_teams_fail_is_internal_error(self, run_main, monkeypatch):
        per_team = {"T1": [], "T2": []}
        gql = _make_graphql(per_team, discover=["T1", "T2"],
                            raise_for={"T1": URLError("a"), "T2": URLError("b")})
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == INTERNAL_ERROR

    def test_auth_error_during_fetch(self, run_main, monkeypatch):
        per_team = {"T1": [_issue("x", "ACME-1")]}
        gql = _make_graphql(per_team, discover=["T1"], raise_for={"T1": _http_error(401)})
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == AUTH_ERROR

    def test_rate_limit_during_fetch(self, run_main, monkeypatch):
        per_team = {"T1": [_issue("x", "ACME-1")]}
        gql = _make_graphql(per_team, discover=["T1"], raise_for={"T1": _http_error(429)})
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == RATE_LIMIT

    def test_auth_error_during_discovery(self, run_main, monkeypatch):
        gql = _make_graphql({}, raise_for={"__discover__": _http_error(401)})
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == AUTH_ERROR

    def test_no_teams_discovered_is_internal_error(self, run_main, monkeypatch):
        gql = _make_graphql({}, discover=[])
        monkeypatch.setattr(slp, "_graphql", gql)
        assert run_main() == INTERNAL_ERROR

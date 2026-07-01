"""Tests for scripts/sync_linear_pending.py — multi-team Scatter-Gather fetch.

The GraphQL layer is fully mocked: no network, no Linear credentials (CI has
none). Covers the all-teams discovery, per-team merge + identifier dedup, the
partial-success path (one flaky team must not blank the block), the fail-loud
auth/rate paths, and the zero-success → INTERNAL_ERROR contract that tells the
caller to KEEP its existing pending block.
"""
from __future__ import annotations

import importlib.util
import os
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


# ---------------------------------------------------------------------------
# Safe pending splice + body-key guard (regression for the 2026-06-18
# /compress body-deletion bug: vault CLAUDE.md has no closing `---`, so the
# rule body is bare column-0 keys directly after the pending list).
# ---------------------------------------------------------------------------

_CLAUDE_FIXTURE = """\
---
critical:
  - project
previous:
  - "prior session note"
pending:
  # Source of truth: Linear.
  - [ ] old item (LIA-1)
project: Deus | path: ~/deus
style: concise, direct
index: see Persona/INDEX.md
"""


def test_safe_replace_preserves_body_keys():
    new_body = "  # Source of truth: Linear.\n  - [ ] fresh (LIA-2)\n"
    out = mod._safe_replace_pending(_CLAUDE_FIXTURE, new_body)
    assert "LIA-2" in out and "old item" not in out  # pending swapped
    for key in ("project:", "style:", "index:"):  # body survives
        assert f"\n{key}" in out
    assert "Deus | path: ~/deus" in out


def test_safe_replace_raises_without_pending_block():
    with pytest.raises(ValueError):
        mod._safe_replace_pending("project: Deus\nstyle: x\n", "  - [ ] a\n")


def test_safe_replace_guard_fires_on_body_loss(monkeypatch):
    # Simulate a regex regression that greedily eats to EOF; the column-0
    # key-preservation guard MUST refuse to return a body-dropping result.
    import re as _re

    monkeypatch.setattr(
        mod, "_PENDING_BLOCK_RE", _re.compile(r"^pending:\n[\s\S]*", _re.MULTILINE)
    )
    with pytest.raises(ValueError, match="drop body keys"):
        mod._safe_replace_pending(_CLAUDE_FIXTURE, "  - [ ] fresh (LIA-2)\n")


def test_main_write_splices_in_place_preserving_body(tmp_path, monkeypatch):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(_CLAUDE_FIXTURE, encoding="utf-8")
    cache = tmp_path / "cache.md"
    cache.write_text(
        "  # Source of truth: Linear.\n  - [ ] fresh (LIA-2)\n", encoding="utf-8"
    )
    monkeypatch.setattr(mod, "_cache_path", lambda: cache)
    monkeypatch.setattr(mod, "_db_path", lambda: tmp_path / "messages.db")
    monkeypatch.setattr(mod, "_cache_is_fresh", lambda c, d: True)  # no network
    monkeypatch.setattr(mod, "_resolve_vault", lambda: tmp_path)
    code = mod.main(["--write"])
    assert code == mod.SUCCESS
    out = claude_md.read_text()
    assert "LIA-2" in out and "old item" not in out
    for key in ("project:", "style:", "index:"):
        assert f"\n{key}" in out


# LIA-316: once vault CLAUDE.md gets a real closing `---`, the rule body lives
# BELOW it as markdown. The splice must still replace only the pending block and
# stop at the `---`, leaving the body untouched.
_CLAUDE_FIXTURE_CLOSED = """\
---
critical:
  - project
pending:
  # Source of truth: Linear.
  - [ ] old item (LIA-1)
---

project: Deus | path: ~/deus
style: concise, direct
index: see Persona/INDEX.md
"""


def test_safe_replace_preserves_body_below_closing_marker():
    new_body = "  # Source of truth: Linear.\n  - [ ] fresh (LIA-2)\n"
    out = mod._safe_replace_pending(_CLAUDE_FIXTURE_CLOSED, new_body)
    assert "LIA-2" in out and "old item" not in out
    assert "\n---\n" in out  # closing marker preserved
    for key in ("project:", "style:", "index:"):
        assert f"\n{key}" in out
    # body still sits below the closing marker, not absorbed into the splice
    assert out.index("\n---\n") < out.index("\nproject:")


# ── previous: atomic, lock-serialized splice (LIA-284 surface #3) ─────────────
_PREV_MULTILINE = """\
---
id: x
previous:
  - "2026-06-26: one"
  - "2026-06-25: two"
pending:
  - [ ] a (LIA-1)
project: Deus
style: concise
index: x
"""

_PREV_INLINE = """\
---
id: x
previous: "2026-06-20: solo line"
pending:
  - [ ] a (LIA-1)
project: Deus
index: x
"""

_PREV_ABSENT = """\
---
id: y
pending:
  - [ ] a (LIA-1)
project: Deus
index: x
"""


class TestParsePreviousEntries:
    def test_multiline(self):
        assert mod._parse_previous_entries(_PREV_MULTILINE) == [
            "2026-06-26: one",
            "2026-06-25: two",
        ]

    def test_inline_single_line(self):
        assert mod._parse_previous_entries(_PREV_INLINE) == ["2026-06-20: solo line"]

    def test_absent(self):
        assert mod._parse_previous_entries(_PREV_ABSENT) == []


class TestSafeReplacePrevious:
    def test_replace_preserves_body_and_single_key(self):
        out = mod._safe_replace_previous(_PREV_MULTILINE, ["2026-06-28: new", "2026-06-26: one"])
        assert mod._COL0_KEY_RE.findall(out).count("previous") == 1
        assert "2026-06-28: new" in out
        for key in ("pending:", "project:", "style:", "index:"):
            assert f"\n{key}" in out

    def test_inline_converted_no_duplicate_key(self):
        out = mod._safe_replace_previous(_PREV_INLINE, ["2026-06-28: new", "2026-06-20: solo line"])
        # the inline form must be consumed, not left behind as a 2nd previous: key
        assert mod._COL0_KEY_RE.findall(out).count("previous") == 1
        assert '  - "2026-06-28: new"' in out

    def test_insert_before_pending_when_absent(self):
        out = mod._safe_replace_previous(_PREV_ABSENT, ["2026-06-28: first"])
        assert mod._COL0_KEY_RE.findall(out).count("previous") == 1
        assert out.index("\nprevious:") < out.index("\npending:")
        for key in ("pending:", "project:", "index:"):
            assert f"\n{key}" in out

    def test_body_loss_guard_fires(self, monkeypatch):
        import re as _re

        # Greedy regex that would eat the body -> guard must refuse.
        monkeypatch.setattr(mod, "_PREVIOUS_BLOCK_RE", _re.compile(r"^previous:[\s\S]*", _re.MULTILINE))
        with pytest.raises(ValueError, match="drop body keys"):
            mod._safe_replace_previous(_PREV_MULTILINE, ["x"])

    def test_dup_key_guard_fires(self, monkeypatch):
        import re as _re

        # Regex that never matches -> falls to insert path, producing a 2nd
        # previous: key alongside the existing one -> count guard must refuse.
        monkeypatch.setattr(mod, "_PREVIOUS_BLOCK_RE", _re.compile(r"^ZZZNOMATCH:", _re.MULTILINE))
        with pytest.raises(ValueError, match="previous:. keys"):
            mod._safe_replace_previous(_PREV_MULTILINE, ["x"])


class TestWritePreviousCLI:
    def _vault(self, tmp_path, content, monkeypatch):
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(content, encoding="utf-8")
        monkeypatch.setattr(mod, "_resolve_vault", lambda: tmp_path)
        return claude_md

    def test_prepend_and_trim_to_max(self, tmp_path, monkeypatch):
        full = _PREV_MULTILINE.replace(
            '  - "2026-06-25: two"\n', '  - "2026-06-25: two"\n  - "2026-06-24: three"\n'
        )
        claude_md = self._vault(tmp_path, full, monkeypatch)
        assert mod.main(["--write-previous", "2026-06-28: newest"]) == mod.SUCCESS
        entries = mod._parse_previous_entries(claude_md.read_text())
        assert entries == ["2026-06-28: newest", "2026-06-26: one", "2026-06-25: two"]
        assert len(entries) == mod.MAX_PREVIOUS_ENTRIES

    def test_inline_form_converts(self, tmp_path, monkeypatch):
        claude_md = self._vault(tmp_path, _PREV_INLINE, monkeypatch)
        assert mod.main(["--write-previous", "2026-06-28: top"]) == mod.SUCCESS
        out = claude_md.read_text()
        assert mod._COL0_KEY_RE.findall(out).count("previous") == 1
        assert mod._parse_previous_entries(out)[0] == "2026-06-28: top"

    def test_insert_when_absent(self, tmp_path, monkeypatch):
        claude_md = self._vault(tmp_path, _PREV_ABSENT, monkeypatch)
        assert mod.main(["--write-previous", "2026-06-28: first"]) == mod.SUCCESS
        assert mod._parse_previous_entries(claude_md.read_text()) == ["2026-06-28: first"]

    def test_idempotent_when_top_unchanged(self, tmp_path, monkeypatch):
        claude_md = self._vault(tmp_path, _PREV_MULTILINE, monkeypatch)
        mod.main(["--write-previous", "2026-06-28: top"])
        before = claude_md.read_text()
        assert mod.main(["--write-previous", "2026-06-28: top"]) == mod.SUCCESS
        assert claude_md.read_text() == before  # no second prepend

    def test_empty_entry_is_usage_error(self, tmp_path, monkeypatch):
        self._vault(tmp_path, _PREV_MULTILINE, monkeypatch)
        assert mod.main(["--write-previous", "   "]) == mod.USAGE_ERROR

    def test_missing_vault_returns_internal_error(self, monkeypatch):
        monkeypatch.setattr(mod, "_resolve_vault", lambda: None)
        assert mod.main(["--write-previous", "x"]) == mod.INTERNAL_ERROR

    def test_lock_degrades_when_fcntl_absent(self, tmp_path, monkeypatch):
        # Simulate a platform without fcntl: the write must still succeed (no raise).
        claude_md = self._vault(tmp_path, _PREV_MULTILINE, monkeypatch)
        import builtins

        real_import = builtins.__import__

        def no_fcntl(name, *a, **k):
            if name == "fcntl":
                raise ImportError("no fcntl")
            return real_import(name, *a, **k)

        # fcntl is cached in sys.modules by earlier tests; the `import fcntl`
        # statement bypasses __import__ for cached modules, so evict it first to
        # actually drive the ImportError degradation branch.
        monkeypatch.delitem(sys.modules, "fcntl", raising=False)
        monkeypatch.setattr(builtins, "__import__", no_fcntl)
        assert mod.main(["--write-previous", "2026-06-28: top"]) == mod.SUCCESS
        assert mod._parse_previous_entries(claude_md.read_text())[0] == "2026-06-28: top"


def test_atomic_write_roundtrip(tmp_path):
    p = tmp_path / "f.txt"
    mod._atomic_write(p, "hello\nworld\n")
    assert p.read_text() == "hello\nworld\n"
    # no leftover temp files in the dir
    assert [x.name for x in tmp_path.iterdir()] == ["f.txt"]


def test_concurrent_write_previous_no_corruption(tmp_path):
    """Two concurrent --write-previous processes must not corrupt the file:
    exactly one well-formed previous: block, no body-key loss (lost-update of one
    entry is acceptable; structural corruption is not)."""
    import subprocess

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(_PREV_MULTILINE, encoding="utf-8")
    script = str(_SCRIPTS / "sync_linear_pending.py")
    env = {**os.environ, "DEUS_VAULT_PATH": str(tmp_path)}
    procs = [
        subprocess.Popen(
            ["python3", script, "--write-previous", f"2026-06-28: writer {i}"], env=env
        )
        for i in range(6)
    ]
    for p in procs:
        p.wait()
    out = claude_md.read_text()
    assert mod._COL0_KEY_RE.findall(out).count("previous") == 1  # structurally intact
    for key in ("pending", "project", "style", "index"):
        assert key in mod._COL0_KEY_RE.findall(out)  # no body-key loss
    # the previous: block has no more than MAX entries and all are well-formed
    entries = mod._parse_previous_entries(out)
    assert 1 <= len(entries) <= mod.MAX_PREVIOUS_ENTRIES

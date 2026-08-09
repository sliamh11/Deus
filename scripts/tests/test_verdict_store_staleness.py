"""Oracle tests for LIA-382: hash-tied warden verdict staleness.

Derived FROM THE SPEC (LIA-382), blind to the implementation — none of this fix
exists in this worktree yet. Tests are RED against the current (unfixed) code,
where ``.warden-verdicts.json`` entries have no provenance binding at all (no
``head_sha``/``diff_hash``), and GREEN only once a SHIP/TRIVIAL verdict is tied
to the git state it was written against and every read site respects that.

Run:
    python3 -m pytest scripts/tests/test_verdict_store_staleness.py -v

Oracle tagging convention (oracle-rules.md § oracle-tagged):
    # @oracle LIA-382: <one-line spec reference>

Design notes for the implementer
---------------------------------
* Real git repos, not mocked git (except test 6, where mocking ``_git`` to fail
  IS the point). Each repo gets one real commit so ``git rev-parse HEAD`` /
  ``git diff HEAD`` / ``git status --porcelain`` all work for real.
* The ``store_root`` fixture patches ``_claude_marker_dir``/``_worktree_for_cwd``
  to an IDENTITY resolution (``lambda root: root / ".claude"`` and
  ``lambda cwd, root: root``) rather than a fixed lambda ignoring the argument —
  this generalizes to the multi-repo (per-worktree-isolation) test, where a
  second, independent real git repo is passed as its own ``repo_root`` and gets
  its own isolated bucket, without needing a second fixture.
* "Different worktree" is stood in for by a wholly separate real git repo
  (its own ``git init``, its own HEAD). Production worktrees share a common
  ``.git`` (linked via ``git worktree add``) and are distinguished by
  ``_worktree_for_cwd``'s ``--git-common-dir`` check — but that resolution
  layer is exactly what these tests bypass (via the identity patch above) to
  isolate the FINGERPRINT-SCOPING behavior under test. Two independent repos
  give two genuinely independent git histories, which is what actually matters
  for proving fingerprints aren't cross-contaminated.
* Several tests below use a "control" assertion (write a SHIP, edit, confirm it
  goes stale) BEFORE the real assertion under test (e.g. REVISE survives the
  same kind of edit). This is required for red-green-ability: on the current
  (absent) implementation, nothing ever goes stale, so "REVISE survives an
  edit" or "a legacy entry survives an edit" would be trivially true already —
  not discriminating. The control forces the whole test to fail on unfixed
  code (the control assertion itself fails), while still asserting the real
  spec requirement once the control's precondition holds.
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_warden_hooks as h
from warden_hooks import verdict_store as _vs
from warden_review.constants import BACKEND_GPT, store_key

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Create a real git repo at *path* with one real commit (a tracked file).

    Real git (not mocked) so ``git rev-parse HEAD`` / ``git diff HEAD`` /
    ``git status --porcelain`` all behave exactly as they would in production.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"], cwd=path, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.email", "oracle-lia382@example.invalid"],
        cwd=path, check=True, stdout=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "config", "user.name", "Oracle LIA-382"],
        cwd=path, check=True, stdout=subprocess.DEVNULL,
    )
    (path / "tracked.txt").write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    (path / ".claude").mkdir()


def _bash_edit_tracked_file(repo: Path, content: str) -> None:
    """Plain ``Path.write_text`` on the already-tracked ``tracked.txt``.

    Deliberately NOT through any Edit/Write hook — this is exactly the
    Bash-invisible-write gap LIA-382 exists to close (a raw filesystem write,
    e.g. from a Bash ``sed``/script, that the existing edit-triggered marker
    invalidator never sees).
    """
    (repo / "tracked.txt").write_text(content, encoding="utf-8")


@pytest.fixture(autouse=True)
def _resync_h_with_live_entry():
    """Rebind the module-level ``h`` name to whatever ``verdict_store._entry``
    currently targets, before every test in this file.

    Why this is needed: ``test_codex_warden_hooks.py``'s ``load_hooks()`` helper
    does a raw (non-monkeypatch, never auto-reverted) ``sys.modules["codex_warden_hooks"]
    = module; spec.loader.exec_module(module)`` to get an isolated copy for its own
    tests — and ``codex_warden_hooks.py``'s own module body calls
    ``_verdict_store.bind_entry(sys.modules[__name__])`` at exec time, so that re-exec
    repoints ``verdict_store._entry`` to a DIFFERENT module object than whatever this
    file's own ``import codex_warden_hooks as h`` captured at collection time. If that
    happens before this file's tests run (confirmed: it does, since
    ``test_codex_warden_hooks.py`` collects/runs before this file alphabetically),
    every ``monkeypatch.setattr(h, ...)`` in this file would patch the WRONG (stale)
    module object — silently no-op against what ``_fresh_entry``/
    ``_compute_state_fingerprint`` actually call through ``_entry.``. Confirmed via
    minimal repro during code review: this desync made
    ``test_fingerprint_computation_failure_fails_open_except_at_standing_grant``'s
    ``_git`` patch invisible to the real code path, so the admin-merge fail-CLOSED
    assertion was passing for the wrong reason (real git succeeded instead of the
    simulated failure). Rebinding here, once per test, makes every ``h.`` reference in
    this file target the live object regardless of what ran before it — a local fix
    scoped to this file, not a change to the shared ``load_hooks()`` test
    infrastructure other test files depend on.
    """
    global h
    h = _vs._entry


@pytest.fixture
def store_root(tmp_path, monkeypatch) -> Path:
    """A real git repo used as both the "worktree" and the ``repo_root`` for
    verdict-store calls.

    Bucket/worktree resolution is patched to IDENTITY (``root`` is trusted
    directly, no cwd-based git derivation), so the same fixture/patches work
    for a second, independently-created real repo passed as its own
    ``repo_root`` — see ``test_staleness_is_scoped_per_worktree``.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(h, "_claude_marker_dir", lambda root: root / ".claude")
    monkeypatch.setattr(h, "_worktree_for_cwd", lambda cwd, root: root)
    return repo


def _utc_iso(offset_hours: float = 0.0) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=offset_hours)).isoformat()


def _enable_standing(repo: Path, expiry_hours: float = 24) -> None:
    wardens = repo / ".claude" / "wardens"
    wardens.mkdir(parents=True, exist_ok=True)
    (wardens / "config.json").write_text(
        json.dumps({"admin-merge-gate": {"standing_grant": {"enabled": True, "expiry_hours": expiry_hours}}}),
        encoding="utf-8",
    )


def _write_standing_marker(repo: Path, created_at: str) -> Path:
    marker = repo / ".claude" / ".admin-merge-standing"
    marker.write_text(
        json.dumps({"worktree_root": str(repo), "created_at": created_at}),
        encoding="utf-8",
    )
    return marker


def bash_event(repo: Path, command: str) -> dict:
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_use_id": "tool",
        "transcript_path": None,
        "turn_id": "turn",
        "tool_input": {"command": command},
    }


def _green_ci(monkeypatch) -> None:
    monkeypatch.setattr(h, "_check_ci_status", lambda *a, **k: (h._CI_STATUS_GREEN, "ok"))


# ---------------------------------------------------------------------------
# 1) Worktree isolation — staleness is scoped per-worktree, not global
# ---------------------------------------------------------------------------


def test_staleness_is_scoped_per_worktree(store_root, tmp_path):
    # @oracle LIA-382: staleness is per-worktree — an edit in worktree B must not
    # invalidate worktree A's SHIP, and must invalidate B's own (spec point 1 / item 1)
    #
    # Falsifies: a fingerprint implementation that hashes something global
    # (e.g. cwd-independent, or accidentally shared state) instead of the specific
    # worktree passed to _write_verdict/_read_verdict.
    repo_a = store_root
    repo_b = tmp_path / "other_worktree"
    _init_git_repo(repo_b)

    _vs._write_verdict(repo_a, "code-reviewer", "SHIP", "ship-a")
    _vs._write_verdict(repo_b, "code-reviewer", "SHIP", "ship-b")

    # A tracked-file edit that happens ONLY in worktree B.
    _bash_edit_tracked_file(repo_b, "changed only in worktree B\n")

    verdict_a = _vs._read_verdict("code-reviewed", repo_a)
    verdict_b = _vs._read_verdict("code-reviewed", repo_b)

    assert verdict_a == "SHIP", (
        "worktree A's fresh SHIP was invalidated by an edit that happened in a "
        "DIFFERENT worktree (B) — staleness must be scoped per-worktree, not global."
    )
    assert verdict_b is None, (
        "worktree B's SHIP survived a tracked-file edit made in ITS OWN worktree — "
        "either staleness isn't implemented at all, or it isn't wired into "
        "_read_verdict."
    )


# ---------------------------------------------------------------------------
# 2) Tracked-file edit via plain Path.write_text (Bash-invisible-write gap)
# ---------------------------------------------------------------------------


def test_ship_goes_stale_after_tracked_file_edit_via_bash(store_root):
    # @oracle LIA-382: a Bash-invisible tracked-file edit stales a SHIP (spec point 1
    # "diff_hash" / item 2 — this is the exact gap ("Bash-based file writes entirely")
    # named in the problem statement)
    repo = store_root
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "ship")
    assert _vs._read_verdict("code-reviewed", repo) == "SHIP"  # sanity: fresh right after write

    _bash_edit_tracked_file(repo, "edited via plain write_text, bypassing all hooks\n")

    verdict = _vs._read_verdict("code-reviewed", repo)
    assert verdict is None, (
        "a SHIP verdict survived a tracked-file edit made via plain Path.write_text "
        "(no Edit/Write hook involved) — the diff-hash fingerprint did not detect "
        "the change, or staleness filtering isn't wired into _read_verdict."
    )


# ---------------------------------------------------------------------------
# 3) New untracked file appearing (no tracked-file changes at all)
# ---------------------------------------------------------------------------


def test_ship_goes_stale_after_new_untracked_file(store_root):
    # @oracle LIA-382: a NEW untracked file stales a SHIP even with zero changes to
    # already-tracked files (spec point 1 — diff_hash combines `git diff HEAD` with
    # `??` entries from `git status --porcelain`; item 3)
    repo = store_root
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "ship")
    assert _vs._read_verdict("code-reviewed", repo) == "SHIP"

    (repo / "new_untracked.txt").write_text("brand new file, never added to git\n", encoding="utf-8")

    verdict = _vs._read_verdict("code-reviewed", repo)
    assert verdict is None, (
        "a SHIP verdict survived a NEW untracked file appearing in the worktree — "
        "the fingerprint must include `git status --porcelain --untracked-files=normal` "
        "`??` entries, not just `git diff HEAD`."
    )


def test_new_untracked_file_directly_under_claude_dir_does_not_stale_ship(store_root):
    # NOT part of the original oracle-author spec — added post-implementation after
    # code-reviewer flagged that _compute_state_fingerprint's `.claude/`-prefix
    # exclusion (a self-invalidation fix discovered during implementation: the
    # verdict store's own JSON/lock files live under `.claude/` and would otherwise
    # invalidate every verdict the instant it's written) is broader than its first-draft
    # docstring claimed — it excludes EVERY untracked path under `.claude/`, not just
    # the store's own artifact files. This test documents and locks in that boundary
    # so it's an explicit, verified design decision rather than an unverified claim.
    #
    # Contrast with test_ship_goes_stale_after_new_untracked_file above: a new
    # untracked file OUTSIDE `.claude/` DOES stale a SHIP; a new untracked file
    # INSIDE `.claude/` does NOT — `.claude/` is this project's dev-tooling/config
    # directory, not "the code under review."
    repo = store_root
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "ship")
    assert _vs._read_verdict("code-reviewed", repo) == "SHIP"

    new_dir = repo / ".claude" / "some-new-skill"
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "SKILL.md").write_text("brand new skill file, never added to git\n", encoding="utf-8")

    verdict = _vs._read_verdict("code-reviewed", repo)
    assert verdict == "SHIP", (
        "a SHIP verdict was invalidated by a new untracked file appearing under "
        "`.claude/` — the fingerprint's untracked-file scan must exclude `.claude/` "
        "entirely (see _compute_state_fingerprint's docstring for why), so this is "
        "expected to be a no-op, not a staleness trigger."
    )


# ---------------------------------------------------------------------------
# 4) THE CRITICAL TEST — REVISE/BLOCK are NEVER filtered by staleness
# ---------------------------------------------------------------------------


def test_revise_survives_staleness_and_still_blocks_trivial_bypass(store_root, monkeypatch):
    # @oracle LIA-382: staleness filtering applies ONLY to SHIP/TRIVIAL — a REVISE
    # must stay fully visible and still block a TRIVIAL bypass no matter how stale its
    # fingerprint looks (spec point 4, the single most important invariant this ticket
    # protects; item 4)
    #
    # Falsifies: an implementation that applies the staleness filter uniformly to all
    # verdict values, which would let a stale REVISE be read as None — and then
    # mark_warden's post-REVISE TRIVIAL-bypass guard (`_last_verdict_is_blocking`)
    # would wrongly succeed, silently defeating "REVISE means re-run until SHIP, no
    # exceptions."
    repo = store_root
    monkeypatch.setattr(h, "_is_bg_session", lambda: False)

    # Control: prove the SAME kind of edit WOULD stale a SHIP in this exact repo state.
    # Without this, "REVISE survives an edit" is trivially true on the unfixed code
    # too (which never stales anything) — this control is what makes the test RED on
    # absent code and genuinely discriminating once the fix lands.
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "control-ship")
    _bash_edit_tracked_file(repo, "control edit\n")
    assert _vs._read_verdict("code-reviewed", repo) is None, (
        "setup precondition failed: a normal fingerprinted SHIP did not go stale "
        "after a tracked-file edit, so this test cannot meaningfully demonstrate the "
        "REVISE-is-never-filtered distinction (spec point 4)."
    )

    # Real assertion: write REVISE fresh, then make the SAME kind of edit that just
    # staled the control SHIP above.
    _vs._write_verdict(repo, "code-reviewer", "REVISE", "found real issues")
    _bash_edit_tracked_file(repo, "edit made after REVISE was recorded\n")

    assert _vs._last_verdict(repo, "code-reviewer") == "REVISE", (
        "_last_verdict hid a REVISE verdict via staleness filtering — REVISE/BLOCK "
        "must NEVER be filtered (only SHIP/TRIVIAL are in scope per the spec)."
    )
    assert _vs._read_verdict("code-reviewed", repo) == "REVISE", (
        "_read_verdict hid a REVISE verdict via staleness filtering."
    )
    assert _vs._last_verdict_is_blocking(repo, "code-reviewer") is True, (
        "_last_verdict_is_blocking returned False for a REVISE that staleness "
        "filtering incorrectly hid — this would let mark_warden's TRIVIAL bypass "
        "wrongly succeed after a stale-looking REVISE."
    )

    # The real-world consequence: a TRIVIAL-bypass mark attempt must still be BLOCKED,
    # exactly as it would be with no staleness mechanism at all.
    rc = h.mark_warden("code-reviewed", "TRIVIAL", "trying to bypass after edit", repo)
    assert rc == 2, (
        f"mark_warden TRIVIAL bypass returned rc={rc} (expected 2/BLOCKED) after a "
        "stale-looking REVISE — a stale REVISE must never be treated as absent."
    )
    assert _vs._last_verdict(repo, "code-reviewer") == "REVISE", (
        "the blocked TRIVIAL bypass attempt still mutated the stored verdict."
    )


def test_block_survives_staleness_and_still_blocks_trivial_bypass(store_root, monkeypatch):
    # @oracle LIA-382: BLOCK gets the same NEVER-filter guarantee as REVISE (spec
    # point 4, explicitly named alongside REVISE; item 4's "also test ... BLOCK")
    repo = store_root
    monkeypatch.setattr(h, "_is_bg_session", lambda: False)

    # Control (same rationale as the REVISE test above).
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "control-ship")
    _bash_edit_tracked_file(repo, "control edit\n")
    assert _vs._read_verdict("code-reviewed", repo) is None, (
        "setup precondition failed: a normal fingerprinted SHIP did not go stale, so "
        "this test cannot meaningfully demonstrate BLOCK's staleness-scoping "
        "distinction."
    )

    _vs._write_verdict(repo, "code-reviewer", "BLOCK", "blocked: security issue")
    _bash_edit_tracked_file(repo, "edit made after BLOCK was recorded\n")

    assert _vs._last_verdict(repo, "code-reviewer") == "BLOCK", (
        "_last_verdict hid a BLOCK verdict via staleness filtering."
    )
    assert _vs._read_verdict("code-reviewed", repo) == "BLOCK", (
        "_read_verdict hid a BLOCK verdict via staleness filtering."
    )
    assert _vs._last_verdict_is_blocking(repo, "code-reviewer") is True, (
        "_last_verdict_is_blocking returned False for a stale-looking BLOCK."
    )

    rc = h.mark_warden("code-reviewed", "TRIVIAL", "trying to bypass after edit", repo)
    assert rc == 2, (
        f"mark_warden TRIVIAL bypass returned rc={rc} (expected 2/BLOCKED) after a "
        "stale-looking BLOCK."
    )


# ---------------------------------------------------------------------------
# 5) Legacy entries (no head_sha/diff_hash at all) are treated as fresh
# ---------------------------------------------------------------------------


def test_legacy_entry_without_fingerprint_fields_is_treated_as_fresh(store_root):
    # @oracle LIA-382: an entry with no head_sha/diff_hash keys (pre-fix shape) has
    # the staleness check SKIPPED, not treated as automatically stale (spec point 3;
    # item 5)
    repo = store_root

    # Control: prove a normal fingerprinted SHIP DOES go stale under this edit —
    # otherwise "the legacy entry survived" would be trivially true on unfixed code
    # too (which writes ONLY legacy-shaped entries, so this scenario would be
    # indistinguishable from "no fingerprinting exists at all").
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "control-ship")
    _bash_edit_tracked_file(repo, "control edit\n")
    assert _vs._read_verdict("code-reviewed", repo) is None, (
        "setup precondition failed: a normal fingerprinted SHIP did not go stale, so "
        "this test cannot meaningfully demonstrate the legacy-entry-is-skipped "
        "distinction."
    )

    # Now write a LEGACY-shaped entry directly (bypassing _write_verdict entirely) —
    # exactly the shape a pre-LIA-382 write produced: no head_sha/diff_hash keys.
    verdicts_path = repo / ".claude" / ".warden-verdicts.json"
    legacy_data = {
        "code-reviewer": {
            "verdict": "SHIP",
            "ts": "2026-01-01T00:00:00Z",
            "reason": "pre-fix entry, no fingerprint fields at all",
            "source": "test",
        }
    }
    assert "head_sha" not in legacy_data["code-reviewer"]  # sanity: genuinely legacy-shaped
    assert "diff_hash" not in legacy_data["code-reviewer"]
    verdicts_path.write_text(json.dumps(legacy_data), encoding="utf-8")

    _bash_edit_tracked_file(repo, "edit made after the legacy entry was written\n")

    verdict = _vs._read_verdict("code-reviewed", repo)
    assert verdict == "SHIP", (
        "a legacy verdict entry (no head_sha/diff_hash keys) was treated as "
        "automatically stale instead of having the staleness check SKIPPED — legacy "
        "entries must be trusted exactly as before the fix."
    )


# ---------------------------------------------------------------------------
# 6) Fail-open (ordinary sites) vs fail-closed (admin-merge standing grant)
# ---------------------------------------------------------------------------


def test_fingerprint_computation_failure_fails_open_except_at_standing_grant(
    store_root, monkeypatch, capsys
):
    # @oracle LIA-382: fingerprint-computation failure fails OPEN at ordinary read
    # sites but fails CLOSED at the admin-merge standing-grant check (spec point 5;
    # item 6)
    repo = store_root
    _vs._write_verdict(repo, "code-reviewer", "SHIP", "ship")
    _vs._write_verdict(repo, "verification-gate", "SHIP", "verified")

    real_git = h._git

    def _git_fingerprint_fails(cwd, *args, **kwargs):
        # Fail exactly the git invocations the spec assigns to fingerprint
        # computation (`git rev-parse HEAD`, `git diff HEAD`, `git status
        # --porcelain --untracked-files=normal`) — every OTHER _git call (e.g.
        # branch resolution for PR-matching) keeps working normally, so this
        # isolates "fingerprint computation failed" from "git is globally broken".
        if args[:2] == ("rev-parse", "HEAD"):
            return None
        if args[:1] == ("diff",) and "HEAD" in args:
            return None
        if args[:1] == ("status",):
            return None
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(h, "_git", _git_fingerprint_fails)

    # Ordinary read site: fails OPEN — still reads as fresh.
    assert _vs._read_verdict("code-reviewed", repo) == "SHIP", (
        "_read_verdict did not fail OPEN when the current-state fingerprint could "
        "not be computed (git failure) — spec point 2/5's default is fail-open at "
        "ordinary read sites."
    )

    # Admin-merge standing-grant: fails CLOSED on the identical failure.
    _enable_standing(repo)
    _write_standing_marker(repo, _utc_iso())
    _green_ci(monkeypatch)

    rc = h.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    raw_out = capsys.readouterr().out
    assert raw_out, (
        "the admin-merge standing-grant fast path printed NOTHING (i.e. it ALLOWED "
        "the merge silently) when the mandatory SHIP verdicts' fingerprints could "
        "not be verified (git failure) — spec point 5 requires this ONE check to "
        "fail CLOSED. Expected a deny JSON block on stdout; got no output at all."
    )
    out = json.loads(raw_out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", (
        "the admin-merge standing-grant fast path ALLOWED a merge whose mandatory "
        "SHIP verdicts' fingerprints could not be verified (git failure) — spec "
        "point 5 requires this ONE check to fail CLOSED (an uncomputable-fingerprint "
        "SHIP is treated as not-fresh), unlike every other read site. A stale SHIP "
        "here would let a merge skip per-command approval entirely."
    )


# ---------------------------------------------------------------------------
# 7) Admin-merge standing grant — direct staleness test (own read path)
# ---------------------------------------------------------------------------


def test_standing_grant_blocks_on_stale_ship_verdict(store_root, monkeypatch, capsys):
    # @oracle LIA-382: the admin-merge standing-grant check reads verdicts via its
    # OWN path (not _last_verdict, per the spec) — a genuinely stale SHIP (not a git
    # failure — real content drift) must block it too, proving the grant path was
    # explicitly wired to check staleness rather than inheriting the fix "for free"
    # (spec point 6; item 7)
    repo = store_root
    _green_ci(monkeypatch)
    _enable_standing(repo)
    _write_standing_marker(repo, _utc_iso())

    _vs._write_verdict(repo, "code-reviewer", "SHIP", "ship")
    _vs._write_verdict(repo, "verification-gate", "SHIP", "verified")

    # Real drift (no git mocking here) — distinguishes this test from test 6's
    # git-computation-failure case.
    _bash_edit_tracked_file(repo, "edit after standing-grant verdicts were SHIPped\n")

    rc = h.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    raw_out = capsys.readouterr().out
    assert raw_out, (
        "the admin-merge standing-grant fast path printed NOTHING (i.e. it ALLOWED "
        "the merge silently) using a STALE mandatory SHIP verdict (worktree edited "
        "after the SHIP was recorded) — the standing-grant read path must "
        "independently check staleness, not just verdict == SHIP. Expected a deny "
        "JSON block on stdout; got no output at all."
    )
    out = json.loads(raw_out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny", (
        "the admin-merge standing-grant fast path ALLOWED a merge using a STALE "
        "mandatory SHIP verdict (worktree edited after the SHIP was recorded) — the "
        "standing-grant read path must independently check staleness, not just "
        "verdict == SHIP."
    )


# ---------------------------------------------------------------------------
# 8) cogate.py's GPT-verdict read site
# ---------------------------------------------------------------------------


def test_cogate_gpt_verdict_read_respects_staleness(store_root, monkeypatch):
    # @oracle LIA-382: cogate.py's GPT-verdict read (currently
    # `_read_verdicts(marker_root).get(store_key(role, "gpt")).get("verdict")`, a raw
    # dict passthrough with no staleness awareness) must not report a stale GPT SHIP
    # as a live PASS (spec point 6; item 8)
    repo = store_root

    gpt_key = store_key("code-reviewer", BACKEND_GPT)
    _vs._write_verdict(repo, gpt_key, "SHIP", "gpt ship")

    # Stale it via the Bash-invisible-write gap this ticket exists to close.
    _bash_edit_tracked_file(repo, "edit after the GPT SHIP was recorded\n")

    import cogate  # local import: relies on _SCRIPTS_DIR already on sys.path

    # Stub the GPT half so no network/codex-CLI call happens — this test targets
    # the READ site, not the GPT invocation.
    monkeypatch.setattr(cogate.codex_warden, "main", lambda argv: 0)
    monkeypatch.setattr(cogate.whooks, "primary_repo_root", lambda wt: wt)

    rc = cogate.main([
        "--role", "code-reviewer",
        "--claude-verdict", "SHIP",
        "--claude-reason", "claude ship",
        "--worktree-root", str(repo),
    ])

    assert rc != 0, (
        f"cogate exited {rc} (SUCCESS) after reading a STALE GPT SHIP verdict as a "
        "live PASS — cogate.py's GPT-verdict read site must respect the same "
        "staleness filtering as every other read site (spec point 6)."
    )


# ---------------------------------------------------------------------------
# 9) LIA-516 — check_fingerprint=False exempts plan-reviewer model-backend
#    reads from the LIA-382 diff-hash staleness check
# ---------------------------------------------------------------------------


def test_check_fingerprint_false_exempts_entry_from_staleness(store_root):
    # @oracle LIA-516: plan-reviewer's SHIP approves plan TEXT (intent), not a
    # diff snapshot — a tracked-file edit (the first implementation edit after
    # a genuine plan SHIP) must NOT stale it when check_fingerprint=False, even
    # though the identical write+edit DOES stale the entry under the default
    # check_fingerprint=True (the control, proving this test is discriminating).
    repo = store_root
    from warden_review.constants import BACKEND_GPT, store_key

    gpt_key = store_key("plan-reviewer", BACKEND_GPT)
    _vs._write_verdict(repo, gpt_key, "SHIP", "plan approved")

    _bash_edit_tracked_file(repo, "first implementation edit after plan SHIP\n")

    # Control: the default (check_fingerprint=True) still stales this entry —
    # proves the edit above is the same kind of change every other test in this
    # file uses to demonstrate staleness, so the real assertion below is real.
    assert _vs._read_verdict(gpt_key, repo) is None, (
        "setup precondition failed: a normal fingerprinted SHIP did not go "
        "stale after a tracked-file edit under the default check_fingerprint=True "
        "— this test cannot meaningfully demonstrate the check_fingerprint=False "
        "exemption without this control holding."
    )

    # Real assertion: the SAME entry, read with check_fingerprint=False, survives.
    assert _vs._read_verdict(gpt_key, repo, check_fingerprint=False) == "SHIP", (
        "a plan-reviewer@gpt SHIP was invalidated by a tracked-file edit even "
        "with check_fingerprint=False — the LIA-516 exemption must skip the "
        "fingerprint comparison entirely for this read, not just widen the "
        "match tolerance."
    )


def test_check_fingerprint_false_does_not_resurrect_revise(store_root, monkeypatch):
    # @oracle LIA-516: check_fingerprint=False must NOT change REVISE/BLOCK
    # handling — those are already unconditionally unfiltered (spec point 4 of
    # LIA-382), and this new parameter must not accidentally widen or narrow
    # that invariant.
    repo = store_root
    from warden_review.constants import BACKEND_GPT, store_key

    gpt_key = store_key("plan-reviewer", BACKEND_GPT)
    _vs._write_verdict(repo, gpt_key, "REVISE", "found issues in the plan")
    _bash_edit_tracked_file(repo, "edit after REVISE was recorded\n")

    assert _vs._read_verdict(gpt_key, repo, check_fingerprint=False) == "REVISE", (
        "check_fingerprint=False hid a REVISE verdict — REVISE/BLOCK must stay "
        "visible regardless of this parameter, exactly as with the default."
    )
    assert _vs._read_verdict(gpt_key, repo, check_fingerprint=True) == "REVISE", (
        "sanity: REVISE is unfiltered under the default too (already covered by "
        "the LIA-382 oracle, re-asserted here for contrast)."
    )

"""Fast, static/regression tests for `_deus_auto_sync` (LIA-529+).

Complements, not duplicates, `scripts/tests/test_deus_cmd_auto_sync_oracle.py` — the
oracle suite is the authoritative, independently-authored behavioral coverage (39 real
`git init` fixture cases spanning every invariant in the SHIP'd plan). This file covers
what that suite doesn't need a full git fixture to check: that the sentinel-wrapped
block exists and is wired into the top-level dispatch (matching `test_deus_cmd_deploy.py`'s
`test_deploy_arm_wiring_present` convention), and a standalone, fixture-free regression
guard for the public-repo-generic invariant (LIA-529+ plan rounds 2/4/5 all caught a
personal-identity leak in a different location each time — this check is cheap enough to
run on every CI pass without spinning up a git fixture, so it stays a permanent guard).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deus-cmd.sh"


def _extract_auto_sync() -> str:
    text = SCRIPT.read_text()
    m = re.search(r"# >>> auto-sync\n(.*?)# <<< auto-sync", text, re.DOTALL)
    assert m, "auto-sync sentinel markers not found in deus-cmd.sh"
    return m.group(1)


# --- static: the sentinel block exists and contains the documented contract ----


def test_auto_sync_sentinel_block_present_and_extractable():
    block = _extract_auto_sync()
    assert "_deus_auto_sync_worker" in block
    assert "_auto_sync_step_a" in block
    assert "_auto_sync_step_b" in block
    assert "_auto_sync_stash_run" in block


def test_wired_into_top_level_dispatch_after_freshness_check():
    text = SCRIPT.read_text()
    assert '_deus_freshness_check "$@"' in text
    assert '_deus_auto_sync "$@"' in text
    # auto-sync fires strictly after the freshness check, same call-site convention.
    assert text.index('_deus_freshness_check "$@"') < text.index('_deus_auto_sync "$@"')


def test_ff_only_never_a_real_merge_or_reset():
    """`_auto_sync_fetch_merge` (the divergence-handling fetch/merge step) must
    never fall back to a real merge, rebase, or hard reset when the branches have
    diverged -- ff-only or nothing. Scoped to that function specifically, not the
    whole block, so this stays correct regardless of what other, unrelated `git`
    invocations exist elsewhere in the file (e.g. the stash-conflict-recovery
    path uses `reset --merge`, a completely different mechanism, for a different
    purpose, on the current local HEAD -- not a divergence-clobbering reset
    against the remote)."""
    block = _extract_auto_sync()
    m = re.search(r"_auto_sync_fetch_merge\(\) \{(.*?)\n\}", block, re.DOTALL)
    assert m, "_auto_sync_fetch_merge function not found"
    fetch_merge_body = m.group(1)
    assert "merge --ff-only" in fetch_merge_body
    assert "--hard" not in fetch_merge_body
    assert re.search(r"git\b[^\n]*\brebase\b", fetch_merge_body) is None


def test_stash_helper_never_uses_bare_stash_or_pop():
    block = _extract_auto_sync()
    # "stash push"/"stash apply"/"stash list"/"stash drop" are fine; a bare
    # `git ... stash` (no subcommand) or `stash pop` must never appear.
    assert "stash pop" not in block
    assert re.search(r"git -C \"\$repo\" stash($|[^ ])", block) is None


def test_conflict_recovery_uses_reset_merge():
    block = _extract_auto_sync()
    assert "reset --merge" in block


def test_linked_worktree_guard_present_matching_deploy_precedent():
    block = _extract_auto_sync()
    assert "git-common-dir" in block


def test_restart_hook_is_build_and_restart_quiet():
    block = _extract_auto_sync()
    assert "_build_and_restart --quiet" in block


def test_kill_switches_present():
    block = _extract_auto_sync()
    assert "DEUS_AUTO_SYNC" in block
    assert "auto_sync_enabled" in block


def test_separate_stamp_file_from_freshness_check():
    block = _extract_auto_sync()
    # Throttle is an mkdir-based atomic lock (see test_atomic_throttle_lock_present),
    # whose own mtime is the stamp -- own name, independent of freshness-stamp.
    assert ".auto-sync.lock" in block
    assert "freshness-stamp" not in block


def test_atomic_throttle_lock_present():
    """Co-gate GPT finding: a separate read-stamp/compare/write-stamp sequence is
    not atomic -- two concurrent `deus` invocations could both pass the throttle
    check and spawn overlapping mutating work. Fixed via `mkdir`, which is
    POSIX-atomic (only one concurrent caller can ever succeed at creating a given
    directory)."""
    block = _extract_auto_sync()
    assert 'mkdir "$lock"' in block


def test_stash_apply_preserves_index():
    """Co-gate GPT finding: `stash apply` without `--index` restores file contents
    but drops the original staged/unstaged split -- a silent mutation of the
    user's own git state beyond what the helper promises."""
    block = _extract_auto_sync()
    assert "stash apply --index" in block


# --- fixture-free public-repo-generic regression guard -------------------------


def test_no_hardcoded_personal_identity_in_script_source():
    """Round 2 baked a personal fork path/identity into executable logic; round 4's
    fix baked the same identity into a test file instead; round 5 flagged the docs
    bullet as a third possible location. This is the permanent guard against a
    fourth recurrence, run on the actual shipped `deus-cmd.sh` text directly (no
    git fixture needed) so it can't silently regress on a future edit."""
    text = SCRIPT.read_text()
    # The one sanctioned exception (this project's own canonical upstream, needed
    # generically by every fork's `deus sync upstream`) must still be present —
    # this test must never conflict with it.
    assert "sliamh11/Deus" in text
    # No real Hermes-fork identity anywhere in the file.
    assert "sliamh11/hermes-agent" not in text
    assert "NousResearch/hermes-agent" not in text


def test_step_b_reads_identity_from_config_not_literal():
    block = _extract_auto_sync()
    assert "_read_config_key secondary_sync_path" in block
    assert "_read_config_key secondary_sync_upstream_identity" in block
    assert "_read_config_key secondary_sync_fork_identity" in block


# --- real-fixture behavioral guard: ignored-file overwrite (GPT co-gate finding) ---
#
# Confirmed empirically (not assumed) before writing this fix: `git merge --ff-only`
# refuses to clobber a plain untracked file ("would be overwritten by merge", exit 1,
# file left untouched) but does NOT extend that protection to a gitignored file at the
# same path -- it silently overwrites it, exit 0, no warning, the instant an incoming
# commit starts tracking that path. The dirty-tree check in _auto_sync_stash_run
# deliberately excludes ignored files, so without a dedicated guard in
# _auto_sync_fetch_merge, nothing else catches this. This test runs the real extracted
# function under zsh (deus-cmd.sh's actual shebang) against real git fixtures -- not a
# static string check -- to prove the guard actually fires.

import subprocess
import tempfile


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=check)


def test_fetch_merge_refuses_to_overwrite_ignored_file(tmp_path):
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("v1\n")
    (work / ".gitignore").write_text("secret.txt\n")
    _git(work, "add", "README.md", ".gitignore")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    # The user's local ignored file, with content they'd be upset to lose.
    (work / "secret.txt").write_text("MY LOCAL SECRET\n")

    # Simulate upstream: someone removes the ignore rule and starts tracking
    # secret.txt with different content -- a real, plausible collision.
    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / ".gitignore").write_text("")
    (writer / "secret.txt").write_text("UPSTREAM VERSION\n")
    _git(writer, "add", ".gitignore", "secret.txt")
    _git(writer, "commit", "-q", "-m", "start tracking secret.txt")
    _git(writer, "push", "-q", "origin", "main")

    before_head = _git(work, "rev-parse", "HEAD").stdout.strip()

    block = _extract_auto_sync()
    script = block + '\n_auto_sync_fetch_merge "$1" "$2"\n'
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(
        ["zsh", script_path, str(work), "origin"],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "must refuse the merge, not silently overwrite"
    assert "MY LOCAL SECRET" in (work / "secret.txt").read_text(), (
        "the user's ignored file must survive untouched -- got: "
        f"{(work / 'secret.txt').read_text()!r}"
    )
    assert _git(work, "rev-parse", "HEAD").stdout.strip() == before_head, (
        "HEAD must not move when the merge was refused for safety"
    )
    assert "would silently overwrite" in result.stderr


# --- real-fixture behavioral guard: mixed tracked+untracked conflict cleanup ---
#
# Confirmed empirically (not assumed) before writing this fix: a failed
# `stash apply --index` (tracked-file conflict) can already have partially
# restored UNTRACKED files from the stash before hitting that conflict --
# `git reset --merge` cleans the tracked/index state back up but does NOT
# remove those leftover untracked files, even though the full stash (which
# still contains the same content) is correctly preserved. So without a
# dedicated cleanup step, the "tree restored... clean" claim in the conflict
# warning is false whenever a stashed dirty tree mixed tracked and untracked
# changes together.


def test_stash_conflict_cleanup_never_deletes_leftover_untracked_file(tmp_path):
    """Code-review co-gate CRITICAL finding: a hash-verified check followed by
    `rm -f` is still two separate operations -- a concurrent process can replace
    the exact path with its own new content in the gap between the check and
    the delete, and `rm -f` would destroy it. No amount of "check right before
    acting" closes this (POSIX has no atomic "delete only if content still
    matches X"). Fixed by never auto-deleting at all -- purely informational
    logging instead. Proves: the leftover file survives, matching content is
    still confirmed via the log message, and nothing is silently lost either
    way (content also stays recoverable from the preserved stash)."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "tracked.txt").write_text("base\n")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    # Dirty tree: a tracked-file edit (will conflict with upstream) PLUS an
    # unrelated untracked file (should never conflict with anything).
    (work / "tracked.txt").write_text("MY LOCAL EDIT\n")
    (work / "untracked.txt").write_text("MY UNTRACKED CONTENT\n")

    # Simulate upstream: a colliding edit to the same tracked file.
    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / "tracked.txt").write_text("UPSTREAM EDIT\n")
    _git(writer, "add", "tracked.txt")
    _git(writer, "commit", "-q", "-m", "upstream change")
    _git(writer, "push", "-q", "origin", "main")

    block = _extract_auto_sync()
    script = block + '\n_auto_sync_stash_run "$1" _auto_sync_fetch_merge "$1" origin\n'
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(
        ["zsh", script_path, str(work)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0, "the stash-run wrapper must report the conflict"
    # No longer genuinely "clean" -- the leftover untracked file is deliberately
    # preserved, never auto-deleted.
    assert (work / "untracked.txt").exists(), (
        "the leftover untracked file must survive -- never auto-deleted, "
        "even when content-verified as matching the stash's own residue"
    )
    assert (work / "untracked.txt").read_text() == "MY UNTRACKED CONTENT\n"
    assert "still matches the preserved stash's own content" in result.stderr
    stash_list = _git(work, "stash", "list").stdout
    assert len(stash_list.strip().splitlines()) == 1, (
        f"expected exactly one preserved stash entry, got: {stash_list!r}"
    )
    # The content must still be fully recoverable from the preserved stash too.
    show = subprocess.run(
        ["git", "-C", str(work), "stash", "show", "-p", "--include-untracked", "stash@{0}"],
        capture_output=True, text=True,
    ).stdout
    assert "MY UNTRACKED CONTENT" in show


def test_stash_conflict_cleanup_handles_non_ascii_filename(tmp_path):
    """Code-review round 6 finding: plain `ls-tree --name-only` (no -z) C-quotes
    non-ASCII filenames (core.quotepath) -- confirmed empirically this silently
    broke matching for names like Hebrew/accented text (a real pattern in this
    vault). Same scenario as test_stash_conflict_cleanup_never_deletes_leftover_untracked_file
    but with a non-ASCII untracked filename, to prove the -z/NUL-delimited fix."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "tracked.txt").write_text("base\n")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    (work / "tracked.txt").write_text("MY LOCAL EDIT\n")
    non_ascii_name = "héllo world.txt"
    (work / non_ascii_name).write_text("MY UNTRACKED CONTENT\n")

    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / "tracked.txt").write_text("UPSTREAM EDIT\n")
    _git(writer, "add", "tracked.txt")
    _git(writer, "commit", "-q", "-m", "upstream change")
    _git(writer, "push", "-q", "origin", "main")

    block = _extract_auto_sync()
    script = block + '\n_auto_sync_stash_run "$1" _auto_sync_fetch_merge "$1" origin\n'
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(
        ["zsh", script_path, str(work)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert (work / non_ascii_name).exists(), (
        "the non-ASCII-named leftover file must survive too, not just plain-ASCII ones "
        "(never auto-deleted, matching or not)"
    )
    assert (work / non_ascii_name).read_text() == "MY UNTRACKED CONTENT\n"
    # The informational match-detection must correctly identify the non-ASCII
    # path too -- proving the -z/NUL-delimited fix still matters for the
    # logging path, even though nothing is deleted anymore.
    assert non_ascii_name in result.stderr, (
        f"expected the non-ASCII filename to appear correctly (not C-quoted/escaped) "
        f"in the info log -- stderr was: {result.stderr!r}"
    )


def test_stash_conflict_cleanup_reverts_non_conflicting_tracked_edit(tmp_path):
    """Code-review co-gate concern: a stash containing BOTH a conflicting tracked
    edit and a separate, non-conflicting tracked edit -- does the non-conflicting
    file's partial application (from the failed --index apply) get fully reverted,
    or does it linger as a dirty tracked change while the stash is also preserved?
    Encodes the concern directly regardless of exactly how reset --merge behaves
    on any given git version -- the safety-net verification step must make this
    true either way."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("line1\nline2\nline3\n")
    (work / "b.txt").write_text("base-b\n")
    _git(work, "add", "a.txt", "b.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    # a.txt will conflict with the incoming upstream edit; b.txt is a separate,
    # non-conflicting local tracked edit that upstream never touches.
    (work / "a.txt").write_text("MINE\nline2\nline3\n")
    (work / "b.txt").write_text("MY LOCAL B EDIT\n")

    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / "a.txt").write_text("THEIRS\nline2\nline3\n")
    _git(writer, "add", "a.txt")
    _git(writer, "commit", "-q", "-m", "upstream change to a.txt only")
    _git(writer, "push", "-q", "origin", "main")

    block = _extract_auto_sync()
    script = block + '\n_auto_sync_stash_run "$1" _auto_sync_fetch_merge "$1" origin\n'
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(
        ["zsh", script_path, str(work)],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert _git(work, "status", "--short").stdout == "", (
        "the non-conflicting file's partial application must be fully reverted, "
        f"got: {_git(work, 'status', '--short').stdout!r}"
    )
    assert (work / "b.txt").read_text() == "base-b\n", (
        "b.txt must be back to HEAD's content, not left with the partially-"
        f"applied local edit -- got: {(work / 'b.txt').read_text()!r}"
    )
    # Content still fully recoverable from the preserved stash either way.
    show = subprocess.run(
        ["git", "-C", str(work), "stash", "show", "-p", "stash@{0}"],
        capture_output=True, text=True,
    ).stdout
    assert "MY LOCAL B EDIT" in show


def test_no_unconditional_tracked_state_reset_after_conflict():
    """Code-review co-gate finding: an unconditional tracked-state "safety net"
    (first `checkout HEAD -- .`, then `reset --hard HEAD`) added in earlier
    rounds shares the exact same TOCTOU danger as the untracked-cleanup case
    below, but with no way to verify provenance per-path the way a single blob
    hash can for an untracked file (a partial merge result isn't one blob to
    check against). Never proven necessary on this git version either (`reset
    --merge` alone already fully reverts a non-conflicting tracked edit --
    confirmed by round 8's own ablation test). Removed rather than made
    unsafe-by-design. This is a static regression guard: no unconditional
    tracked-state discard may exist in the conflict-recovery branch. Command-
    shape checks, not bare substrings -- the explanatory comment above
    legitimately mentions both as prose (what was tried and removed)."""
    block = _extract_auto_sync()
    assert re.search(r"git -C \"\$repo\" reset --hard", block) is None
    assert re.search(r"git -C \"\$repo\" checkout HEAD -- \.", block) is None


def test_toctou_concurrent_write_before_check_never_gets_deleted(tmp_path):
    """The auto-sync worker runs detached in the background while the user keeps
    working -- a concurrent process can recreate/modify a file at one of the
    leftover-cleanup paths in the window before this cleanup even runs its
    content check. Proves: cleanup never deletes anything at all (a later
    co-gate CRITICAL finding established that even a hash-verified check
    immediately followed by `rm -f` is still racy -- the check and the delete
    are two separate operations, and a concurrent write landing BETWEEN them
    is a distinct, narrower race this test doesn't exercise; see
    `test_stash_conflict_cleanup_never_deletes_leftover_untracked_file` for the
    design decision that removes the delete action entirely, closing both
    windows at once). This test specifically covers the write-before-check
    case: the mismatched content must survive, and the info log must not
    falsely claim it "matches the preserved stash".

    Exercises the real, shipped `_auto_sync_stash_run` directly (not an
    extracted/rewritten snippet) -- the concurrent write is injected through
    its own command-hook parameter, at the exact point ("$@", run between the
    stash push and the apply/cleanup) where a real background race would land."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "tracked.txt").write_text("base\n")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    (work / "tracked.txt").write_text("MY LOCAL EDIT\n")
    (work / "untracked.txt").write_text("ORIGINAL STASHED CONTENT\n")

    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / "tracked.txt").write_text("UPSTREAM EDIT\n")
    _git(writer, "add", "tracked.txt")
    _git(writer, "commit", "-q", "-m", "upstream change")
    _git(writer, "push", "-q", "origin", "main")

    block = _extract_auto_sync()
    script = block + '''
_test_concurrent_write() {
  _auto_sync_fetch_merge "$1" origin
  local rc=$?
  printf 'CONCURRENT PROCESS WROTE THIS\\n' > "$1/untracked.txt"
  return $rc
}
_auto_sync_stash_run "$1" _test_concurrent_write "$1"
'''
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(["zsh", script_path, str(work)], capture_output=True, text=True)

    assert result.returncode != 0, "sanity check: the tracked.txt edit must conflict"
    assert (work / "untracked.txt").read_text() == "CONCURRENT PROCESS WROTE THIS\n", (
        "the concurrently-written content must survive untouched -- got: "
        f"{(work / 'untracked.txt').read_text()!r}"
    )
    assert "still matches the preserved stash's own content" not in result.stderr, (
        "content genuinely differs (concurrent write) -- must not be logged as a match"
    )
    # The original stashed content is still fully recoverable, concurrent
    # content was never silently merged with or lost either.
    show = subprocess.run(
        ["git", "-C", str(work), "stash", "show", "-p", "--include-untracked", "stash@{0}"],
        capture_output=True, text=True,
    ).stdout
    assert "ORIGINAL STASHED CONTENT" in show


def test_successful_apply_never_drops_a_concurrent_stash_entry(tmp_path):
    """Code-review co-gate CRITICAL finding: `git stash drop` only accepts the
    positional `stash@{N}` form (confirmed empirically: it rejects a raw commit
    SHA outright, unlike `apply`, which accepts one directly). The prior design
    resolved our own SHA to its position via a fresh `stash list`, then dropped
    that position -- but a concurrent process pushing a stash between those two
    commands shifts every existing entry's position, so the drop can delete
    THEIR entry instead of ours. Fixed by never auto-dropping at all. Proves the
    fix directly: an unrelated concurrent stash entry, pushed AFTER ours, must
    still exist after a fully successful auto-sync run."""
    bare = tmp_path / "bare.git"
    work = tmp_path / "work"
    writer = tmp_path / "writer"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True)

    work.mkdir()
    _git(work, "init", "-q", "-b", "main", ".")
    _git(work, "config", "user.email", "t@t.com")
    _git(work, "config", "user.name", "t")
    (work / "a.txt").write_text("base\n")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "main")

    (work / "a.txt").write_text("mine\n")  # non-conflicting dirty edit

    subprocess.run(["git", "clone", "-q", str(bare), str(writer)], check=True)
    (writer / "b.txt").write_text("incoming\n")
    _git(writer, "add", "b.txt")
    _git(writer, "commit", "-q", "-m", "unrelated upstream change")
    _git(writer, "push", "-q", "origin", "main")

    block = _extract_auto_sync()
    # Inject a concurrent stash push in the exact race window (between our own
    # push and the apply/drop-or-not decision), via _auto_sync_stash_run's own
    # command-hook parameter -- the same point a real background race would land.
    script = block + '''
_test_concurrent_stash_push() {
  _auto_sync_fetch_merge "$1" origin
  local rc=$?
  (cd "$1" && echo "intruder edit" > a.txt && git stash push -m "concurrent-intruder-entry" -- a.txt >/dev/null 2>&1)
  return $rc
}
_auto_sync_stash_run "$1" _test_concurrent_stash_push "$1"
'''
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        script_path = f.name
    result = subprocess.run(["zsh", script_path, str(work)], capture_output=True, text=True)

    assert result.returncode == 0, "the merge itself (unrelated to a.txt) must succeed cleanly"
    stash_list = _git(work, "stash", "list").stdout
    assert "concurrent-intruder-entry" in stash_list, (
        "the concurrent process's own stash entry must survive untouched -- "
        f"got: {stash_list!r}"
    )


def test_no_stash_drop_call_anywhere_in_block():
    """Static regression guard: no code path may call `stash drop` at all -- the
    only mechanism git offers for it (positional `stash@{N}`) is inherently
    unsafe against concurrent stash activity, which this repo's own shared
    stash stack explicitly has to tolerate. Command-shape check, not a bare
    substring -- the explanatory comment legitimately mentions "stash drop" as
    prose (describing what was removed and why)."""
    block = _extract_auto_sync()
    assert re.search(r"git -C \"\$repo\" stash drop", block) is None

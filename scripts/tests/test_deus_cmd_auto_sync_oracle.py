"""Independent oracle for `_deus_auto_sync` (LIA-529+), authored from the spec
BEFORE any implementation exists -- see the LIA-529 spec (passive background
auto-sync for `deus-cmd.sh`) for the full contract this suite encodes.

Authoring discipline (oracle-author independence): this file was written
without reading any implementation of `_deus_auto_sync` -- it does not exist
yet. Every assertion below traces to the spec, not to any chosen internal
design. It follows the `test_deus_cmd_deploy.py` convention: the testable
logic is wrapped in `# >>> auto-sync` / `# <<< auto-sync` sentinel comments,
extracted, and sourced into an isolated `zsh -c` -- matching deus-cmd.sh's
real `#!/usr/bin/env zsh` shebang, not `bash` -- alongside real throwaway
`git init`/`git init --bare` repos under pytest's `tmp_path` -- never mocks
of git itself. (An earlier revision of this file ran under `bash -c`; a
verification pass caught that this silently validated the wrong
interpreter and missed a real zsh-special-parameter bug in the
implementation -- see `_run()`'s comment for the mechanism.)

Until the implementation lands, `_extract_auto_sync()` raises immediately,
so every test in this file is expected to fail at setup (RED). That is
correct and by design: this is a discriminating test written ahead of the
code it will discriminate against.

--- Assumptions this oracle had to make where the spec leaves a gap --------
The spec names the entry point (`_deus_auto_sync "$@"`), the two hardcoded
remote names Step A/B depend on ("origin" for both, plus "fork" for Step B),
and one exact config key (`auto_sync_enabled`). It does NOT name:
  (a) the three Step-B config keys (just "a filesystem path", "an upstream
      identity string", "a fork identity string" in English prose), or
  (b) which existing helper implements "a rebuild+restart hook" for Step A.
For (a), this suite intercepts `_read_config_key` with a shell override that
pattern-matches the substrings "upstream" / "fork" / "path"|"dir"|"repo" in
the requested key name -- the natural vocabulary an implementer would reach
for given the spec's own wording. If the real implementation names these
keys with none of those substrings, the Step-B-focused tests below will
need their override patterns extended; that is a residual, spec-inherent
ambiguity, not a defect in this oracle.
For (b), this suite assumes the existing `_build_and_restart` helper
(deus-cmd.sh:79, already used identically by both `deus sync` and
`deus deploy` for the same "did HEAD move -> rebuild" contract) is reused,
since it is the only pre-existing rebuild+restart hook in the codebase and
predates this feature (public surface, not the new implementation).
-----------------------------------------------------------------------------
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deus-cmd.sh"

REAL_GIT = None
for _candidate in ("/usr/bin/git", "/usr/local/bin/git", "/opt/homebrew/bin/git"):
    if Path(_candidate).exists():
        REAL_GIT = _candidate
        break
if REAL_GIT is None:
    import shutil as _shutil

    REAL_GIT = _shutil.which("git")
assert REAL_GIT, "no git binary found -- required to run this real-git-fixture oracle suite"

SENTINEL_RE = re.compile(r"# >>> auto-sync\n(.*?)# <<< auto-sync", re.DOTALL)

UPSTREAM_ID = "example-org/example-oracle-fixture-upstream"
FORK_ID = "example-user/example-oracle-fixture-fork"


# =============================================================================
# Sentinel extraction (RED until the implementation adds the wrapper)
# =============================================================================


def _extract_auto_sync() -> str:
    text = SCRIPT.read_text()
    m = SENTINEL_RE.search(text)
    assert m, (
        "auto-sync sentinel markers (`# >>> auto-sync` / `# <<< auto-sync`) not "
        "found in deus-cmd.sh -- _deus_auto_sync has not been implemented yet "
        "(or was implemented without the required sentinel wrapper). This is "
        "the expected RED state: this oracle was authored from the LIA-529 "
        "spec before any implementation exists."
    )
    return m.group(1)


# =============================================================================
# Pre-existing helper stubs (public surface this file must supply, since only
# the sentinel-wrapped block is sourced in isolation -- these are copied
# verbatim from deus-cmd.sh's EXISTING, pre-dating-this-feature definitions,
# not from the new implementation.)
# =============================================================================

_REAL_READ_CONFIG_KEY = r'''
_read_config_key() {
  python3 -c "
import json; from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
d = json.loads(p.read_text()) if p.exists() else {}
print(d.get('$1', ''))" 2>/dev/null
}
'''

_REAL_WRITE_CONFIG_KEY = r'''
_write_config_key() {
  python3 -c "
import json, sys; from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
p.parent.mkdir(parents=True, exist_ok=True)
d = json.loads(p.read_text()) if p.exists() else {}
d[sys.argv[1]] = sys.argv[2]
p.write_text(json.dumps(d, indent=2))
" "$1" "$2"
}
'''

# Substring-matching override used only for Step-B-focused tests (see module
# docstring assumption (a) above) -- decouples this oracle from guessing the
# implementation's exact config key names for the secondary repo's path /
# upstream identity / fork identity.
_READ_CONFIG_OVERRIDE = r'''
_read_config_key() {
  case "$1" in
    *upstream*) printf '%s' "${AUTO_SYNC_TEST_UPSTREAM_ID:-}" ;;
    *fork*)     printf '%s' "${AUTO_SYNC_TEST_FORK_ID:-}" ;;
    *path*|*dir*|*repo*) printf '%s' "${AUTO_SYNC_TEST_SECOND_PATH:-}" ;;
    *) printf '%s' "" ;;
  esac
}
'''

# Spy on the rebuild+restart hook (see module docstring assumption (b)).
_BUILD_RESTART_STUB = r'''
_build_and_restart() {
  printf 'restart-invoked\n' >> "$AUTO_SYNC_RESTART_LOG"
}
'''


def _harness_script(auto_sync_block: str, *, override_config: bool) -> str:
    parts = [
        _READ_CONFIG_OVERRIDE if override_config else _REAL_READ_CONFIG_KEY,
        _REAL_WRITE_CONFIG_KEY,
        _BUILD_RESTART_STUB,
        auto_sync_block,
        '\n_deus_auto_sync "$@"\n',
    ]
    return "\n".join(parts)


# =============================================================================
# Real-git fixture builders (tmp_path-rooted, never mocked)
# =============================================================================

_GIT_FIXTURE_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Oracle Fixture",
    "GIT_AUTHOR_EMAIL": "oracle-fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Oracle Fixture",
    "GIT_COMMITTER_EMAIL": "oracle-fixture@example.invalid",
}


def _git(cwd, *args, check: bool = True):
    return subprocess.run(
        [REAL_GIT, "-C", str(cwd), *args],
        env=_GIT_FIXTURE_ENV,
        capture_output=True,
        text=True,
        check=check,
    )


def _init_bare(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [REAL_GIT, "init", "--quiet", "--bare", "-b", "main", str(path)],
        env=_GIT_FIXTURE_ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _init_self_repo(tmp_path: Path, name: str = "self"):
    """A working repo + its own bare 'origin', both at one shared initial
    commit, on main -- the Step A ("self"/Deus checkout) fixture."""
    bare = _init_bare(tmp_path / "remotes" / f"{name}-origin.git")
    work = tmp_path / name
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    (work / "README.md").write_text("v1\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "--quiet", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "--quiet", "origin", "main")
    return work, bare


def _push_new_commit(
    bare: Path, tmp_path: Path, tag: str, filename: str = "NEW.md",
    content: str = "new\n", msg: str = "progress",
) -> str:
    """Simulate upstream progress: add one commit to `bare`'s main via a
    throwaway clone, independent of any working repo under test."""
    writer = tmp_path / f"_writer_{tag}"
    _git(tmp_path, "clone", "--quiet", str(bare), str(writer))
    (writer / filename).write_text(content)
    _git(writer, "add", filename)
    _git(writer, "commit", "--quiet", "-m", msg)
    _git(writer, "push", "--quiet", "origin", "main")
    return _git(writer, "rev-parse", "HEAD").stdout.strip()


def _bare_path_for_identity(tmp_path: Path, identity: str, suffix: str) -> Path:
    """A local bare repo whose filesystem path literally contains `identity`
    as a substring -- git remote URLs for local repos ARE paths, so this
    gives us a real, non-mocked way to test 'the remote URL contains the
    configured identity string' without touching any real GitHub identity."""
    parts = identity.split("/")
    return tmp_path / "remotes" / Path(*parts[:-1]) / f"{parts[-1]}{suffix}"


def _init_secondary_repo(tmp_path: Path, upstream_id: str = UPSTREAM_ID, fork_id: str = FORK_ID):
    """The Step B ("secondary") fixture: a working repo with an 'origin'
    remote whose URL contains `upstream_id` and a 'fork' remote whose URL
    contains `fork_id`, both starting in sync at one shared commit."""
    bare_upstream = _init_bare(_bare_path_for_identity(tmp_path, upstream_id, "-upstream.git"))
    bare_fork = _init_bare(_bare_path_for_identity(tmp_path, fork_id, "-fork.git"))
    work = tmp_path / "secondary"
    work.mkdir()
    _git(work, "init", "--quiet", "-b", "main")
    (work / "README.md").write_text("v1\n")
    _git(work, "add", "README.md")
    _git(work, "commit", "--quiet", "-m", "initial")
    _git(work, "remote", "add", "origin", str(bare_upstream))
    _git(work, "push", "--quiet", "origin", "main")
    _git(work, "remote", "add", "fork", str(bare_fork))
    _git(work, "push", "--quiet", "fork", "main")
    return work, bare_upstream, bare_fork


def _diverge_bare(bare: Path, tmp_path: Path, tag: str) -> str:
    """Push a commit directly to `bare`'s main that the working repo under
    test has never seen -- makes a subsequent non-force push to `bare` a
    real, git-enforced non-fast-forward rejection."""
    writer = tmp_path / f"_diverge_{tag}"
    _git(tmp_path, "clone", "--quiet", str(bare), str(writer))
    (writer / "UNRELATED.md").write_text("fork-only content\n")
    _git(writer, "add", "UNRELATED.md")
    _git(writer, "commit", "--quiet", "-m", "fork-only unrelated commit")
    _git(writer, "push", "--quiet", "origin", "main")
    return _git(writer, "rev-parse", "HEAD").stdout.strip()


def _harmless_self_repo(tmp_path: Path) -> Path:
    """A self-repo fixture guaranteed to no-op Step A cleanly (wrong branch),
    so Step-B-focused tests aren't entangled with Step A's own behavior."""
    work, _bare = _init_self_repo(tmp_path, name="self_inert")
    _git(work, "checkout", "--quiet", "-b", "not-main")
    return work


# =============================================================================
# PATH-shim "spies"/fault-injectors for the real `git` binary -- these never
# change git's own behavior for anything they don't specifically target; they
# only observe (logging shim) or add a real extra git call (intruder shim) or
# add latency (slow-fetch shim). Only the harness's OWN `git` invocations
# (looked up via bare `git` on PATH, matching existing deus-cmd.sh style) go
# through these -- all fixture setup above uses the REAL_GIT absolute path
# and is unaffected regardless of PATH.
# =============================================================================


def _make_slow_fetch_shim(tmp_path: Path, delay: int = 4) -> Path:
    shim_dir = tmp_path / "_shim_slow"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{REAL_GIT}"\n'
        'for a in "$@"; do\n'
        '  if [ "$a" = "fetch" ]; then\n'
        f'    sleep {delay}\n'
        '    break\n'
        '  fi\n'
        'done\n'
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim_dir


def _make_intruder_shim(tmp_path: Path) -> Path:
    """Whenever the wrapped process runs a *successful* `git stash push`
    (with or without a leading `-C <dir>`), also push one extra REAL stash
    entry on top afterward, simulating a concurrent process racing in right
    after -- deterministically pushing the auto-sync's own entry down to
    stash@{1} for the rest of its run.

    Touches a fresh untracked file in target_dir immediately before its own
    push -- the wrapped `stash push` (correctly, per spec) leaves the tree
    fully clean, so without its own guaranteed-dirty content the intruder's
    push would itself be a no-op ("No local changes to save", verified
    directly against real git) and never create a second entry at all. The
    intruder's own file, not any leftover dirtiness from the wrapped call,
    is what the race actually needs."""
    shim_dir = tmp_path / "_shim_intruder"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{REAL_GIT}"\n'
        'target_dir=""\n'
        'prev=""\n'
        'stash_push=0\n'
        'prevarg=""\n'
        'for a in "$@"; do\n'
        '  if [ "$prev" = "-C" ]; then target_dir="$a"; fi\n'
        '  if [ "$prevarg" = "stash" ] && [ "$a" = "push" ]; then stash_push=1; fi\n'
        '  prev="$a"; prevarg="$a"\n'
        'done\n'
        '"$REAL_GIT" "$@"\n'
        'rc=$?\n'
        'if [ "$stash_push" = "1" ] && [ $rc -eq 0 ]; then\n'
        '  if [ -n "$target_dir" ]; then\n'
        '    echo "intruder-owned" > "$target_dir/.intruder-marker" 2>/dev/null\n'
        '    "$REAL_GIT" -C "$target_dir" stash push -u -m "concurrent-intruder-oracle-marker" >/dev/null 2>&1 || true\n'
        '  else\n'
        '    echo "intruder-owned" > .intruder-marker 2>/dev/null\n'
        '    "$REAL_GIT" stash push -u -m "concurrent-intruder-oracle-marker" >/dev/null 2>&1 || true\n'
        '  fi\n'
        'fi\n'
        'exit $rc\n'
    )
    shim.chmod(0o755)
    return shim_dir


def _make_logging_shim(tmp_path: Path, log_path: Path) -> Path:
    shim_dir = tmp_path / "_shim_log"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        f'REAL_GIT="{REAL_GIT}"\n'
        f'LOG="{log_path}"\n'
        "printf '%s\\n' \"$*\" >> \"$LOG\"\n"
        'exec "$REAL_GIT" "$@"\n'
    )
    shim.chmod(0o755)
    return shim_dir


# =============================================================================
# Harness runner + polling helpers
# =============================================================================


def _base_env(tmp_path: Path, config_overrides: dict | None = None) -> dict:
    home = tmp_path / "_home"
    home.mkdir(exist_ok=True)
    (home / ".config" / "deus").mkdir(parents=True, exist_ok=True)
    if config_overrides:
        cfg_path = home / ".config" / "deus" / "config.json"
        data: dict = {}
        if cfg_path.exists():
            data = json.loads(cfg_path.read_text())
        data.update(config_overrides)
        cfg_path.write_text(json.dumps(data))
    env = dict(os.environ)
    env.pop("DEUS_AUTO_SYNC", None)
    env.pop("DEUS_DEBUG", None)
    env["HOME"] = str(home)
    restart_log = tmp_path / "_restart.log"
    restart_log.write_text("")
    env["AUTO_SYNC_RESTART_LOG"] = str(restart_log)
    return env


def _run(
    tmp_path: Path,
    args: list[str],
    env: dict,
    *,
    self_repo: Path | None = None,
    path_prepend: Path | None = None,
    override_config: bool = False,
    timeout: int = 20,
) -> SimpleNamespace:
    script = _harness_script(_extract_auto_sync(), override_config=override_config)

    run_env = dict(env)
    if self_repo is not None:
        run_env["SCRIPT_DIR"] = str(self_repo)
    if path_prepend is not None:
        run_env["PATH"] = f"{path_prepend}{os.pathsep}{run_env.get('PATH', '')}"

    tag = time.monotonic_ns()
    out_path = tmp_path / f"_out_{tag}.log"
    err_path = tmp_path / f"_err_{tag}.log"
    start = time.monotonic()
    # File-redirected stdout/stderr (never PIPE): a properly-detached background
    # subshell that itself redirects its own fds away is invisible to us either
    # way; a NOT-properly-detached one would otherwise make Python's pipe-based
    # capture hang waiting for EOF from the lingering grandchild -- which is
    # itself part of what test_foreground_never_blocks_even_when_git_is_slow
    # exists to catch, decoupled from Python subprocess plumbing quirks.
    with open(out_path, "wb") as out_f, open(err_path, "wb") as err_f:
        proc = subprocess.run(
            # zsh, not bash: deus-cmd.sh's real shebang is `#!/usr/bin/env zsh`, and
            # zsh has special parameters (e.g. `path`, tied to `$PATH`) with no bash
            # equivalent -- a verification pass running this under bash silently
            # validated the wrong interpreter and missed a real `local path` bug
            # that made an entire code path a no-op in actual production use.
            ["zsh", "-c", script, "zsh", *args],
            env=run_env,
            stdout=out_f,
            stderr=err_f,
            timeout=timeout,
        )
    elapsed = time.monotonic() - start
    return SimpleNamespace(
        returncode=proc.returncode,
        elapsed=elapsed,
        stdout=out_path.read_text(errors="replace"),
        stderr=err_path.read_text(errors="replace"),
    )


def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return predicate()
        time.sleep(interval)


def _confirm_never(predicate, window: float = 2.0, interval: float = 0.05) -> bool:
    """Bounded confirmation that `predicate` never becomes true within
    `window` seconds. Used only where the spec demands something NOT
    happen -- not a substitute for a positive completion signal."""
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        if predicate():
            return False
        time.sleep(interval)
    return not predicate()


def _head(repo) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _stash_list(repo) -> list[str]:
    out = _git(repo, "stash", "list").stdout.strip()
    return [line for line in out.splitlines() if line.strip()]


def _restart_count(env: dict) -> int:
    p = Path(env["AUTO_SYNC_RESTART_LOG"])
    if not p.exists():
        return 0
    return len([line for line in p.read_text().splitlines() if line.strip()])


def _snapshot_tree(repo: Path):
    """(HEAD sha, `git status --short`, {relpath: bytes}) for byte-for-byte
    before/after comparison of a repo's entire tracked+untracked state."""
    head = _head(repo)
    status = _git(repo, "status", "--short").stdout
    files: dict[str, bytes] = {}
    for p in sorted(Path(repo).rglob("*")):
        if ".git" in p.parts:
            continue
        if p.is_file():
            files[str(p.relative_to(repo))] = p.read_bytes()
    return head, status, files


# =============================================================================
# Section 1 -- trigger / exclusion / throttle / kill-switch invariants
# =============================================================================


# @oracle: spec "deus root ... must NEVER spawn the background worker, under any circumstance"
def test_root_command_never_spawns_background_worker(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "root")
    env = _base_env(tmp_path)
    result = _run(tmp_path, ["root"], env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == new_sha), (
        "`deus root` must never spawn the auto-sync background worker -- other "
        "tooling depends on it staying instant and side-effect-free."
    )
    assert _restart_count(env) == 0
    assert result.elapsed < 2.0


# @oracle: spec "--print-identity (in ANY argument position) must NEVER spawn"
@pytest.mark.parametrize(
    "args",
    [["--print-identity"], ["home", "--print-identity"], ["--print-identity", "home"]],
)
def test_print_identity_never_spawns_in_any_position(tmp_path, args):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "pid-" + "-".join(args))
    env = _base_env(tmp_path)
    _run(tmp_path, args, env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == new_sha), (
        f"--print-identity at position {args!r} must stay side-effect-free -- "
        "auto-sync must never spawn."
    )
    assert _restart_count(env) == 0


# @oracle: spec "deus sync, deus deploy, ... help/-h/--help, and bare deus with no args
# ... also never trigger it (mirrors the existing _deus_freshness_check exclusion list)"
@pytest.mark.parametrize("args", [["sync"], ["deploy"], ["help"], ["-h"], ["--help"], []])
def test_standard_exclusion_list_never_spawns(tmp_path, args):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "excl-" + ("-".join(args) or "empty"))
    env = _base_env(tmp_path)
    _run(tmp_path, args, env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == new_sha), (
        f"args={args!r} is on the standard exclusion list -- auto-sync must "
        "never spawn for it."
    )
    assert _restart_count(env) == 0


# @oracle: spec "An env var DEUS_AUTO_SYNC=0 ... disables it entirely -- no spawn at all"
def test_env_var_disables_auto_sync_entirely(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "envkill")
    env = _base_env(tmp_path)
    env["DEUS_AUTO_SYNC"] = "0"
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == new_sha), (
        "DEUS_AUTO_SYNC=0 must disable auto-sync entirely."
    )
    assert _restart_count(env) == 0


# @oracle: spec 'a config key auto_sync_enabled set to the string "false" disables it entirely'
def test_config_key_disables_auto_sync_entirely(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "cfgkill")
    env = _base_env(tmp_path, config_overrides={"auto_sync_enabled": "false"})
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == new_sha), (
        'config key auto_sync_enabled="false" must disable auto-sync entirely.'
    )
    assert _restart_count(env) == 0


# @oracle: spec "Throttled to at most one spawn per 600s via a stamp file, independent
# of the pre-existing _deus_freshness_check's own stamp file (they must not share one)"
def test_throttle_blocks_second_spawn_and_is_independent_of_freshness_stamp(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    first_sha = _push_new_commit(bare, tmp_path, "throttle1")
    env = _base_env(tmp_path)
    # Seed a FRESH freshness-check stamp before the very first call. If
    # auto-sync wrongly shared/consulted this stamp, it would wrongly think
    # "already checked recently" and skip its own first, legitimate sync.
    freshness_stamp = Path(env["HOME"]) / ".config" / "deus" / "freshness-stamp"
    freshness_stamp.parent.mkdir(parents=True, exist_ok=True)
    freshness_stamp.write_text(str(int(time.time())))

    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _wait_until(lambda: _head(self_repo) == first_sha, timeout=8.0), (
        "first auto-sync call did not sync despite a pre-existing FRESH "
        "freshness-check stamp -- suggests auto-sync incorrectly shares it."
    )

    # filename must differ from the first _push_new_commit call above -- both use
    # the default "NEW.md"/"new\n", and the second writer clones `bare` AFTER the
    # first commit already landed there, so re-writing identical content leaves
    # nothing to commit (verified directly: `git commit` exits 1, "nothing to
    # commit, working tree clean" -- a test-fixture bug, not part of the spec
    # under test). Fixed by giving the second commit its own distinct file.
    second_sha = _push_new_commit(bare, tmp_path, "throttle2", filename="NEW2.md")
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _confirm_never(lambda: _head(self_repo) == second_sha), (
        "a second auto-sync call within the same 600s window must be "
        "throttled and must not merge the newly available commit."
    )
    assert _head(self_repo) == first_sha
    # HEAD-moved and restart-count-incremented are two separate observations (one
    # via `git rev-parse` on the repo, one via a log-file read) of the same
    # background worker's sequential progress -- a tiny window exists between the
    # merge landing and the restart stub's write becoming visible. Poll briefly
    # rather than asserting immediately (code-review round 4 finding: this exact
    # single-check pattern flaked under load elsewhere in this file).
    assert _wait_until(lambda: _restart_count(env) == 1, timeout=2.0)


# =============================================================================
# Section 2 -- non-blocking / fail-soft
# =============================================================================


# @oracle: spec "fires ONE detached, non-blocking background subshell ... Foreground
# deus must NEVER block, hang, or surface an error from this background work."
def test_foreground_never_blocks_even_when_git_is_slow(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _push_new_commit(bare, tmp_path, "slow")
    shim_dir = _make_slow_fetch_shim(tmp_path, delay=4)
    env = _base_env(tmp_path)
    result = _run(tmp_path, ["home"], env, self_repo=self_repo, path_prepend=shim_dir, timeout=20)
    assert result.elapsed < 2.0, (
        f"foreground `deus` call took {result.elapsed:.2f}s while `git fetch` "
        "was artificially slowed to 4s -- the sync work must run in a "
        "detached background subshell, never block the foreground call."
    )


# =============================================================================
# Section 3 -- Step A ("self" repo) guards, ff-only, restart gating
# =============================================================================


# @oracle: spec Step A guard #1 -- "Refuses to act if the repo is a linked git
# worktree ... This must be checked BEFORE any mutation."
def test_step_a_refuses_linked_worktree_before_any_mutation(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _push_new_commit(bare, tmp_path, "wt")  # origin diverges ahead
    wt_path = tmp_path / "self-worktree"
    _git(self_repo, "worktree", "add", "--quiet", str(wt_path), "-b", "wt-branch")
    (wt_path / "dirty.txt").write_text("uncommitted in worktree\n")
    before = _snapshot_tree(wt_path)
    before_stash = _stash_list(wt_path)

    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=wt_path)
    time.sleep(1.5)

    assert _snapshot_tree(wt_path) == before, (
        "a linked worktree must be left completely untouched by auto-sync."
    )
    assert _stash_list(wt_path) == before_stash
    assert _restart_count(env) == 0


# @oracle: spec Step A guard #2 -- "Refuses to act if not on branch main."
def test_step_a_refuses_when_not_on_main(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _push_new_commit(bare, tmp_path, "nonmain")
    _git(self_repo, "checkout", "--quiet", "-b", "feature")
    before = _snapshot_tree(self_repo)
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    time.sleep(1.5)
    assert _snapshot_tree(self_repo) == before
    assert _restart_count(env) == 0


# @oracle: spec Step A guard #3 -- "Refuses to act if no origin remote is configured."
def test_step_a_refuses_when_no_origin_remote(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _git(self_repo, "remote", "remove", "origin")
    before = _snapshot_tree(self_repo)
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    time.sleep(1.5)
    assert _snapshot_tree(self_repo) == before
    assert _restart_count(env) == 0


# @oracle: spec "must NEVER fall back to a real merge, rebase, or reset --hard when
# the branches have diverged ... leave the repo completely untouched, no restart."
def test_step_a_diverged_repo_left_byte_for_byte_untouched(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _push_new_commit(bare, tmp_path, "diverge-theirs")
    (self_repo / "LOCAL.md").write_text("local only\n")
    _git(self_repo, "add", "LOCAL.md")
    _git(self_repo, "commit", "--quiet", "-m", "local-only commit")
    before = _snapshot_tree(self_repo)

    env = _base_env(tmp_path)
    result = _run(tmp_path, ["home"], env, self_repo=self_repo)
    time.sleep(1.5)

    assert _snapshot_tree(self_repo) == before, (
        "a diverged self repo must be left byte-for-byte untouched -- no "
        "merge, no rebase, no reset --hard."
    )
    assert _restart_count(env) == 0
    assert result.elapsed < 2.0


# @oracle: spec "Only if HEAD's SHA actually changed (compare before/after, not
# 'did the merge command exit 0') ... If HEAD didn't move, the rebuild+restart
# hook must NOT be invoked."
def test_step_a_restart_not_invoked_when_already_up_to_date(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)  # self and origin already share HEAD
    before_head = _head(self_repo)
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    time.sleep(1.5)
    assert _head(self_repo) == before_head
    assert _restart_count(env) == 0


def test_step_a_restart_invoked_only_when_head_actually_moves(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "restart-yes")
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0), (
        "expected a clean fast-forward merge to land."
    )
    assert _wait_until(lambda: _restart_count(env) == 1, timeout=2.0), (
        "restart hook must be invoked exactly once when HEAD actually moved."
    )


# =============================================================================
# Section 4 -- stash-safety invariants (exercised on Step A's self repo)
# =============================================================================


# @oracle: SUPERSEDED spec "the dirty state must be safely stashed before merging
# and restored after ... drops that specific stash entry only if the apply
# SUCCEEDED" -- see the inline comment below for the CRITICAL finding
# (git stash drop has no safe, non-positional, race-free form) that overrides
# this: the entry is now deliberately never auto-dropped, even on success.
def test_stash_happy_path_dirty_changes_survive_sync(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "stash-happy", filename="OTHER.md", content="incoming\n")
    (self_repo / "README.md").write_text("v1-mine\n")  # dirty, non-conflicting path
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)

    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0)
    time.sleep(0.3)
    assert (self_repo / "README.md").read_text() == "v1-mine\n", (
        "the dirty README.md change must be restored after the sync."
    )
    assert (self_repo / "OTHER.md").read_text() == "incoming\n"
    # Deliberately NOT dropped, even on a successful apply -- a real GPT co-gate
    # CRITICAL finding after this suite was first authored: `git stash drop`
    # only accepts the positional `stash@{N}` form (confirmed empirically, not
    # a raw SHA), so any drop requires resolving a fresh position immediately
    # before dropping -- and a concurrent process pushing a stash in that exact
    # window shifts every position, making the drop delete THEIR entry instead
    # of ours. Real, in-scope threat (the shared stash stack is explicitly used
    # by concurrent sessions/tools in this repo), not theoretical. Leaving a
    # successfully-applied entry behind is harmless; the original "drop on
    # success" spec text is superseded by this safety fix.
    remaining = _stash_list(self_repo)
    assert len(remaining) == 1, f"expected our own entry left in place (never auto-dropped), got: {remaining!r}"
    assert _wait_until(lambda: _restart_count(env) == 1, timeout=2.0)


# @oracle: spec "If git stash apply FAILS (e.g. a real merge conflict ...): the
# implementation must run git reset --merge ... and the stash entry must be LEFT
# INTACT ... No restart, no push happens for that repo on this path."
def test_stash_merge_conflict_shape_recovers_clean_stash_preserved(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "stash-conflict", filename="README.md", content="v1-theirs\n")
    (self_repo / "README.md").write_text("v1-mine\n")  # same file/line -> conflicts on restore
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)

    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0), (
        "the ff-only merge from origin must still succeed even though the "
        "stash restore afterward is expected to conflict."
    )
    time.sleep(0.5)

    status = _git(self_repo, "status", "--short").stdout
    assert status.strip() == "", (
        f"working tree must end up clean after a failed stash apply -- got:\n{status}"
    )
    content = (self_repo / "README.md").read_text()
    for marker in ("<<<<<<<", "=======", ">>>>>>>"):
        assert marker not in content, f"conflict marker {marker!r} leaked into README.md: {content!r}"
    assert len(_stash_list(self_repo)) == 1, (
        "the user's dirty stash entry must survive a failed restore, fully recoverable."
    )
    assert _restart_count(env) == 0, "no restart may happen on the stash-conflict recovery path."


# @oracle: spec "OR a distinct untracked-file-collision failure ... these are two
# different git failure shapes ... but must both be handled the same way"
def test_stash_untracked_collision_shape_recovers_clean_stash_preserved(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "stash-untracked", filename="NEWFILE.md", content="theirs\n")
    (self_repo / "NEWFILE.md").write_text("mine\n")  # truly untracked, same path as incoming
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo)

    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0)
    time.sleep(0.5)

    status = _git(self_repo, "status", "--short").stdout
    assert status.strip() == "", f"working tree must end up clean -- got:\n{status}"
    assert (self_repo / "NEWFILE.md").read_text() == "theirs\n", (
        "on an untracked-collision failure, the incoming merged content must "
        "win cleanly -- the stashed untracked file must not partially apply."
    )
    assert len(_stash_list(self_repo)) == 1, (
        "the stash entry must survive this failure shape too -- the apply's "
        "non-zero exit must be checked, not assumed success from git's "
        "misleading 'working tree clean' message on this failure path."
    )
    assert _restart_count(env) == 0


# @oracle: spec "If the initial git stash push itself fails, abort that repo's sync
# entirely without attempting any fetch/merge (never merge over a tree that
# couldn't be safely snapshotted)."
def test_stash_push_failure_aborts_before_any_fetch_or_merge(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    _push_new_commit(bare, tmp_path, "stash-push-fail")  # origin ahead, ready if reached
    (self_repo / "README.md").write_text("dirty-but-unsnapshottable\n")
    before_head = _head(self_repo)

    lock = self_repo / ".git" / "index.lock"
    lock.write_text("")  # forces `git stash push` to fail with a real git error
    env = _base_env(tmp_path)
    try:
        _run(tmp_path, ["home"], env, self_repo=self_repo)
        time.sleep(1.5)  # bounded grace window for the background attempt to fail
    finally:
        lock.unlink(missing_ok=True)

    assert _head(self_repo) == before_head, (
        "if the initial stash push fails, no fetch/merge may be attempted."
    )
    assert (self_repo / "README.md").read_text() == "dirty-but-unsnapshottable\n"
    assert _stash_list(self_repo) == []
    assert _restart_count(env) == 0


# @oracle: spec "locates its own stash entry precisely (by matching the tag, not by
# position/index, since concurrent stash activity could shift positions)"
def test_stash_restore_targets_own_entry_not_stack_position(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "stash-position", filename="OTHER2.md", content="incoming\n")
    (self_repo / "README.md").write_text("mine-position-test\n")
    shim_dir = _make_intruder_shim(tmp_path)
    env = _base_env(tmp_path)
    _run(tmp_path, ["home"], env, self_repo=self_repo, path_prepend=shim_dir)

    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0)
    time.sleep(0.5)

    assert (self_repo / "README.md").read_text() == "mine-position-test\n", (
        "auto-sync's own dirty change must be restored correctly even though "
        "a concurrent process pushed another stash entry on top of it -- a "
        "position/index-based restore (bare `git stash pop` / stash@{0}) "
        "would have grabbed the wrong (intruder) entry instead."
    )
    remaining = _stash_list(self_repo)
    assert any("concurrent-intruder-oracle-marker" in line for line in remaining), (
        "the unrelated concurrent stash entry must be left untouched -- "
        "auto-sync must never drop/consume a stash entry that isn't its own."
    )
    assert _wait_until(lambda: _restart_count(env) == 1, timeout=2.0)


# =============================================================================
# Section 5 -- Step B ("secondary" repo): config gating, identity, ff-only, push
# =============================================================================


@pytest.mark.parametrize("missing", ["path", "upstream", "fork"])
def test_step_b_noop_when_any_config_key_missing(tmp_path, missing):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, f"b-missing-{missing}")
    fork_before = _git(bare_fork, "rev-parse", "main").stdout.strip()
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = "" if missing == "upstream" else UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = "" if missing == "fork" else FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = "" if missing == "path" else str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)
    time.sleep(1.5)
    assert _head(secondary) != new_sha, (
        f"missing config key ({missing}) must no-op Step B entirely."
    )
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_before


@pytest.mark.parametrize("scenario", ["nonexistent", "not_a_repo"])
def test_step_b_noop_when_path_invalid(tmp_path, scenario):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, f"b-invalid-{scenario}")
    fork_before = _git(bare_fork, "rev-parse", "main").stdout.strip()
    if scenario == "nonexistent":
        bad_path = tmp_path / "does-not-exist-at-all"
    else:
        bad_path = tmp_path / "plain-dir-not-a-repo"
        bad_path.mkdir()
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(bad_path)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)
    time.sleep(1.5)
    assert _head(secondary) != new_sha, f"{scenario} secondary path must no-op Step B."
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_before


# @oracle: spec "Verifies the origin remote's URL actually CONTAINS the configured
# upstream-identity string ... a mismatch on either one is a no-op."
def test_step_b_noop_when_origin_identity_mismatch(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "b-origin-mismatch")
    fork_before = _git(bare_fork, "rev-parse", "main").stdout.strip()
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = "totally-different-org/unrelated-repo"
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)
    time.sleep(1.5)
    assert _head(secondary) != new_sha, (
        "origin remote's URL does not contain the configured upstream "
        "identity -- Step B must no-op even though repo/path/fork are valid."
    )
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_before


# @oracle: spec "separately verifies the fork remote's URL actually contains the
# configured fork-identity string ... Both checks independently gate."
def test_step_b_noop_when_fork_identity_mismatch(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "b-fork-mismatch", filename="Y.md", content="v2\n")
    fork_before = _git(bare_fork, "rev-parse", "main").stdout.strip()
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = "totally-different-user/unrelated-fork"
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)
    time.sleep(1.5)
    assert _head(secondary) != new_sha, (
        "a fork identity mismatch must no-op Step B entirely, per spec "
        "('a mismatch on either one is a no-op') -- not merge-but-skip-push."
    )
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_before


# @oracle: spec "Only if HEAD actually moved: pushes ... to the fork remote's main
# branch ... Never triggers any rebuild/restart (that's Step A's concept only)."
def test_step_b_pushes_only_on_head_movement_never_triggers_restart(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "b-push-yes", filename="X.md", content="v2\n")
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)

    assert _wait_until(lambda: _head(secondary) == new_sha, timeout=8.0)
    time.sleep(0.5)
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == new_sha, (
        "when Step B's HEAD moves, the result must be pushed to fork's main."
    )
    assert _restart_count(env) == 0, "Step B must never trigger the restart hook."


def test_step_b_no_push_when_already_up_to_date(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    fork_before = _git(bare_fork, "rev-parse", "main").stdout.strip()
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)
    time.sleep(1.5)
    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_before, (
        "no push may happen when Step B's HEAD never moved."
    )
    assert _restart_count(env) == 0


# @oracle: spec "Never force-pushes. A push failure/rejection is logged, never
# retried within the same run."
def test_step_b_never_force_pushes_rejection_logged_not_retried(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "b-noforce", filename="README.md", content="v2\n")
    fork_tip_before = _diverge_bare(bare_fork, tmp_path, "b-noforce")

    log_path = tmp_path / "_git_calls.log"
    log_path.write_text("")
    shim_dir = _make_logging_shim(tmp_path, log_path)

    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(
        tmp_path, ["home"], env,
        self_repo=_harmless_self_repo(tmp_path),
        path_prepend=shim_dir,
        override_config=True,
    )

    assert _wait_until(lambda: _head(secondary) == new_sha, timeout=8.0), (
        "the origin-side merge for Step B must still succeed even though the "
        "fork push afterward is expected to be rejected."
    )
    time.sleep(1.0)

    assert _git(bare_fork, "rev-parse", "main").stdout.strip() == fork_tip_before, (
        "a rejected (non-fast-forward) push to fork must never be "
        "force-pushed -- the fork's main ref must stay exactly where it was."
    )
    push_lines = [
        line for line in log_path.read_text().splitlines()
        if "push" in line.split() and "fork" in line
    ]
    assert len(push_lines) <= 1, (
        "a rejected push to fork must be logged and never retried within the "
        f"same run -- observed {len(push_lines)} push-to-fork invocations: {push_lines}"
    )


# =============================================================================
# Section 6 -- repo independence (Step A / Step B failures never cross-contaminate)
# =============================================================================


# @oracle: spec "a failure or absence in one step must never affect the other"
# (direction 1: Step B absent/unconfigured must not block Step A)
def test_step_b_absence_does_not_block_step_a(tmp_path):
    self_repo, bare = _init_self_repo(tmp_path)
    new_sha = _push_new_commit(bare, tmp_path, "indep-a")
    env = _base_env(tmp_path)  # no Step B config at all -- must silently no-op
    _run(tmp_path, ["home"], env, self_repo=self_repo)
    assert _wait_until(lambda: _head(self_repo) == new_sha, timeout=8.0), (
        "Step A must run normally even though Step B has no configuration."
    )
    assert _wait_until(lambda: _restart_count(env) == 1, timeout=2.0)


# (direction 2: Step A guaranteed-refuse must not block Step B)
def test_step_a_failure_does_not_block_step_b(tmp_path):
    self_repo, _self_bare = _init_self_repo(tmp_path, name="self_broken")
    _git(self_repo, "checkout", "--quiet", "-b", "not-main")  # guarantees Step A no-ops

    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "indep-b", filename="Z.md", content="v2\n")

    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=self_repo, override_config=True)

    assert _wait_until(lambda: _head(secondary) == new_sha, timeout=8.0), (
        "Step B must run normally even though Step A is guaranteed to refuse."
    )
    assert _wait_until(
        lambda: _git(bare_fork, "rev-parse", "main").stdout.strip() == new_sha, timeout=8.0
    )
    assert _restart_count(env) == 0, "Step A never ran, so no restart is expected."


# =============================================================================
# Section 7 -- public-repo-generic invariant
# =============================================================================


# @oracle: spec "deus-cmd.sh itself ... must contain ZERO hardcoded references to
# any specific real repository, GitHub username, fork, or upstream project as
# part of Step B's logic ... Test this using FABRICATED placeholder identity
# strings only."
def test_no_hardcoded_identity_leak_in_script_source(tmp_path):
    secondary, bare_upstream, bare_fork = _init_secondary_repo(tmp_path)
    new_sha = _push_new_commit(bare_upstream, tmp_path, "no-leak", filename="LEAK.md", content="v2\n")
    env = _base_env(tmp_path)
    env["AUTO_SYNC_TEST_UPSTREAM_ID"] = UPSTREAM_ID
    env["AUTO_SYNC_TEST_FORK_ID"] = FORK_ID
    env["AUTO_SYNC_TEST_SECOND_PATH"] = str(secondary)
    _run(tmp_path, ["home"], env, self_repo=_harmless_self_repo(tmp_path), override_config=True)

    # Functional proof-by-use first: Step B must actually work end-to-end using
    # ONLY these fabricated placeholders, proving they're read at runtime.
    assert _wait_until(lambda: _head(secondary) == new_sha, timeout=8.0)
    assert _wait_until(
        lambda: _git(bare_fork, "rev-parse", "main").stdout.strip() == new_sha, timeout=8.0
    )

    source = SCRIPT.read_text()
    assert UPSTREAM_ID not in source, (
        f"deus-cmd.sh must never hardcode a specific repo identity for Step B "
        f"-- found the oracle's fabricated placeholder {UPSTREAM_ID!r} in the script."
    )
    assert FORK_ID not in source, (
        f"deus-cmd.sh must never hardcode a specific fork identity for Step B "
        f"-- found the oracle's fabricated placeholder {FORK_ID!r} in the script."
    )


# =============================================================================
# Section 8 -- top-level wiring (static)
# =============================================================================


# @oracle: spec "A new function _deus_auto_sync "$@", called on every plain deus
# invocation" -- every other test in this file calls _deus_auto_sync directly and
# so cannot catch a forgotten call site; this one checks the actual wiring.
def test_auto_sync_is_wired_into_top_level_invocation():
    _extract_auto_sync()  # still require the sentinel-wrapped definition to exist
    text = SCRIPT.read_text()
    assert re.search(r'^_deus_auto_sync\s+"\$@"', text, re.MULTILINE), (
        '_deus_auto_sync "$@" must be called unconditionally at the top level '
        'of deus-cmd.sh (mirroring _deus_freshness_check "$@"), not just defined.'
    )

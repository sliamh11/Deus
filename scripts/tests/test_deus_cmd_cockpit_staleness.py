"""Portability of the cockpit staleness check in `deus-cmd.sh` (#1250).

The block open-coded a `stat -f %m ... || stat -c %Y ...` fallback. On GNU
coreutils `-f` means "display filesystem status", not "format", so `%m` is read
as a second FILE operand: the command exits 1 having printed five lines of
filesystem info to *stdout* (which `2>/dev/null` does not suppress), and the
fallback then appends the real epoch to that. `_cockpit_mtime` ends up holding
six lines, so the `[ -z ... ]` guard cannot fire — the value is not empty, just
wrong.

`deus-cmd.sh`'s shebang is `#!/usr/bin/env zsh`, and zsh treats a malformed
arithmetic expression as fatal, so the real consequence on Linux is not a bad
staleness number: the block aborts and everything after it in the command is
silently lost. A correct `_file_mtime()` helper already existed in the same
file, used by three other call sites.

The behavioural tests run under zsh ONLY, never bash. This defect is
specifically zsh-arithmetic-fatal — bash does not abort on the malformed
expression — so a bash fallback would pass against the broken code. That is not
hypothetical: `test_deus_cmd_auto_sync_oracle.py` records an earlier revision
that ran under `bash -c`, silently validated the wrong interpreter, and missed a
real production bug.
"""

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deus-cmd.sh"

_HAS_ZSH = shutil.which("zsh") is not None
_NEEDS_ZSH = pytest.mark.skipif(
    not _HAS_ZSH,
    reason="deus-cmd.sh is #!/usr/bin/env zsh; running these under bash would "
           "validate the wrong interpreter (see module docstring)",
)


def _script_text() -> str:
    return SCRIPT.read_text()


def _extract_cockpit() -> str:
    m = re.search(
        r"# >>> cockpit-staleness\n(.*?)# <<< cockpit-staleness",
        _script_text(),
        re.DOTALL,
    )
    assert m, "cockpit-staleness sentinel markers not found in deus-cmd.sh"
    return m.group(1)


def _extract_file_mtime() -> str:
    m = re.search(r"(_file_mtime\(\) \{.*?\n\})", _script_text(), re.DOTALL)
    assert m, "_file_mtime helper not found in deus-cmd.sh"
    return m.group(1)


# ── static: no open-coded stat survives (AC4) ─────────────────────────────────
#
# Platform-independent, so this is the assertion that discriminates on a macOS
# dev host as well as in CI.


def test_every_stat_call_lives_inside_the_portable_helper():
    """Compares by LINE NUMBER, not by substring.

    A substring check would false-pass the moment an open-coded call happened to
    duplicate a line already inside the helper — and `stat -f %m "$1" 2>/dev/null`
    is exactly the idiom someone would re-type.
    """
    lines = _script_text().splitlines()

    # Line numbers spanned by the helper, so membership is positional.
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith("_file_mtime() {")
    )
    end = next(i for i in range(start, len(lines)) if lines[i] == "}")

    stat_hits = [
        (i, ln.strip()) for i, ln in enumerate(lines)
        if re.search(r"\bstat\s+-[fc]\b", ln)
    ]
    assert stat_hits, "expected the helper's own stat calls to be found"

    outside = [(i + 1, ln) for i, ln in stat_hits if not (start <= i <= end)]
    assert not outside, (
        "every mtime read must go through _file_mtime; found open-coded "
        f"stat call(s) at line(s): {outside}"
    )


def test_cockpit_block_calls_the_helper():
    block = _extract_cockpit()
    assert '_cockpit_mtime=$(_file_mtime "$_cockpit_line")' in block
    assert "stat -f" not in block and "stat -c" not in block


def test_helper_branches_on_ostype_rather_than_probing_flags():
    """The bug was a flag probe that guesses wrong on GNU. The helper must keep
    deciding by platform, not by trying `-f` and falling back."""
    helper = _extract_file_mtime()
    assert "$OSTYPE" in helper
    assert "||" not in helper, "a `-f || -c` probe is the defect this fixes"


# ── behavioural: the block actually runs on this platform (AC1/AC2/AC3) ───────


def _run_block(tmp_path, *, stub_mtime=None):
    """Run the real extracted block under zsh, with DEUS_HOME pointed at a temp dir."""
    parts = [_extract_file_mtime()]
    if stub_mtime is not None:
        # Override the helper to force the unreadable-timestamp path (AC3).
        parts.append(f"_file_mtime() {{ printf '%s' '{stub_mtime}'; }}")
    parts.append(f'DEUS_HOME="{tmp_path}"')
    parts.append(_extract_cockpit())
    return subprocess.run(
        ["zsh", "-c", "\n".join(parts)],
        capture_output=True, text=True, timeout=60,
    )


@_NEEDS_ZSH
def test_fresh_ok_result_is_silent_and_does_not_abort(tmp_path):
    """AC1: on GNU coreutils this aborted with `bad math expression`."""
    (tmp_path / "cockpit_health.line").write_text("OK\n")

    r = _run_block(tmp_path)

    assert r.returncode == 0, f"block aborted: {r.stderr}"
    assert "bad math expression" not in r.stderr
    assert r.stderr == "", f"unexpected stderr: {r.stderr}"
    # Quiet when healthy and fresh, or the report becomes noise.
    assert r.stdout == "", f"unexpected stdout: {r.stdout}"


@_NEEDS_ZSH
def test_stale_result_is_reported(tmp_path):
    """AC2: the staleness check still does its job — the fix must not silence
    the very check it lives in."""
    f = tmp_path / "cockpit_health.line"
    f.write_text("OK\n")
    old = time.time() - 200_000  # > 36h
    import os
    os.utime(f, (old, old))

    r = _run_block(tmp_path)

    assert r.returncode == 0
    assert "last result is" in r.stdout and "old" in r.stdout


@_NEEDS_ZSH
def test_non_ok_verdict_is_reported(tmp_path):
    (tmp_path / "cockpit_health.line").write_text("DEGRADED: scheduler down\n")

    r = _run_block(tmp_path)

    assert r.returncode == 0
    assert "DEGRADED: scheduler down" in r.stdout


@_NEEDS_ZSH
def test_empty_result_file_is_reported(tmp_path):
    (tmp_path / "cockpit_health.line").write_text("")

    r = _run_block(tmp_path)

    assert r.returncode == 0
    assert "empty" in r.stdout


@_NEEDS_ZSH
def test_missing_result_file_is_reported(tmp_path):
    r = _run_block(tmp_path)

    assert r.returncode == 0
    assert "no healthcheck result on record" in r.stdout


@_NEEDS_ZSH
def test_unreadable_timestamp_branch_can_fire(tmp_path):
    """AC3: with the open-coded fallback this branch was unreachable on Linux —
    the variable held non-empty garbage rather than being empty."""
    (tmp_path / "cockpit_health.line").write_text("OK\n")

    r = _run_block(tmp_path, stub_mtime="")

    assert r.returncode == 0
    assert "cannot read healthcheck timestamp" in r.stdout

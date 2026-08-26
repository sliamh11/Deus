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


# ── state (c): no required checks configured ─────────────────────────────────
#
# These lock in the ROUTING, not merely the outcome. The bug this section exists
# for was that `_query_checks` collapsed "gh says no required checks" into the
# same None as "gh could not be read", so the zero-checks branch was unreachable
# and every PR on a repo without branch protection died on the retry budget.
# Asserting only "green on a repo with no required checks" would still pass if
# someone bolted on a parallel state and left the original branch dead — so
# these assert which path was taken, via the `NO REQUIRED CHECKS CONFIGURED`
# stamp that only that path emits.


class _Proc:
    """Minimal stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _gh_no_required(branch="my-branch"):
    """gh's real signature for a branch with no required checks: non-zero exit,
    EMPTY stdout, message on stderr. Captured from a live run."""
    return _Proc(
        returncode=1,
        stdout="",
        stderr=f"no required checks reported on the '{branch}' branch\n",
    )


def test_query_checks_maps_gh_no_required_to_its_own_sentinel(wfc, monkeypatch):
    """The routing fix itself: gh's no-required-checks signature must NOT come
    back as None (the unreadable value), or the retry budget eats it."""
    monkeypatch.setattr(wfc, "_run", lambda argv, timeout=60: _gh_no_required())
    assert _query(wfc, required=True) is wfc.NO_REQUIRED_CHECKS
    assert _query(wfc, required=True) is not None


def _query(wfc, *, required):
    return wfc._query_checks(1, required=required)


def test_query_checks_keeps_other_failures_unreadable(wfc, monkeypatch):
    """Fail closed on anything that is not gh's exact wording — an auth error
    must stay `None` so it is retried, never mistaken for "none required"."""
    monkeypatch.setattr(
        wfc, "_run",
        lambda argv, timeout=60: _Proc(1, "", "gh: Bad credentials (HTTP 401)\n"),
    )
    assert _query(wfc, required=True) is None


def test_query_checks_ignores_the_signature_on_the_unfiltered_query(wfc, monkeypatch):
    """`--required` is the only query that can legitimately answer "none
    required"; the unfiltered one saying so would be nonsense, so stay closed."""
    monkeypatch.setattr(wfc, "_run", lambda argv, timeout=60: _gh_no_required())
    assert _query(wfc, required=False) is None


def test_query_checks_stays_unreadable_when_stdout_is_not_empty(wfc, monkeypatch):
    """Partial output alongside the message means something else went wrong."""
    monkeypatch.setattr(
        wfc, "_run",
        lambda argv, timeout=60: _Proc(1, "garbage", "no required checks reported\n"),
    )
    assert _query(wfc, required=True) is None


def _routed(wfc, monkeypatch, *, allchecks, merge_state, required=None):
    """Drive the loop from the sentinel through state (c)."""
    sentinel = wfc.NO_REQUIRED_CHECKS if required is None else required

    def fake(pr, *, required):
        return sentinel if required else allchecks

    monkeypatch.setattr(wfc, "_query_checks", fake)
    monkeypatch.setattr(wfc, "_merge_state", lambda pr: merge_state)
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    monkeypatch.setattr(wfc.time, "monotonic", _advancing_clock())
    return wfc.wait_for_required_checks(1, interval=0, timeout=1)


def test_no_required_checks_green_names_the_path(wfc, monkeypatch):
    """State (c) green: all unfiltered checks pass AND mergeStateStatus=CLEAN.
    Must never be a bare "green" — the detail names the fallback it took."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pass"}, {"name": "lint", "bucket": "pass"}],
        merge_state="CLEAN",
    )
    assert green is True
    assert "NO REQUIRED CHECKS CONFIGURED" in detail
    assert "mergeStateStatus=CLEAN" in detail


def test_readable_empty_required_list_takes_the_same_path(wfc, monkeypatch):
    """A readable `[]` from --required means the same thing as the sentinel, so
    it routes to state (c) too — not to the retry budget."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pass"}],
        merge_state="CLEAN",
        required=[],
    )
    assert green is True
    assert "NO REQUIRED CHECKS CONFIGURED" in detail


def test_no_required_checks_red_is_not_green(wfc, monkeypatch):
    """State (b) reached through the (c) path: a failing check still fails."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pass"}, {"name": "test-windows", "bucket": "fail"}],
        merge_state="CLEAN",
    )
    assert green is False
    assert "NO REQUIRED CHECKS CONFIGURED" in detail
    assert "test-windows" in detail


def test_no_required_checks_unclean_merge_state_fails_closed(wfc, monkeypatch):
    """All checks pass but the PR is not mergeable — the second half of the
    assertion is what stops this being a rubber stamp."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pass"}],
        merge_state="DIRTY",
    )
    assert green is False
    assert "mergeStateStatus=DIRTY" in detail


def test_no_required_checks_unreadable_merge_state_fails_closed(wfc, monkeypatch):
    """Cannot determine mergeability → not green. This is a merge gate."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pass"}],
        merge_state=None,
    )
    assert green is False
    assert "failing closed" in detail


def test_no_required_checks_unreadable_unfiltered_fails_closed(wfc, monkeypatch):
    """Sentinel reached but the fallback query itself is unreadable → not green,
    and it times out rather than guessing."""
    green, detail = _routed(wfc, monkeypatch, allchecks=None, merge_state="CLEAN")
    assert green is False
    assert "unreadable" in detail


def test_no_required_checks_pending_is_not_green(wfc, monkeypatch):
    """A pending check on the (c) path polls to the deadline, never green."""
    green, detail = _routed(
        wfc, monkeypatch,
        allchecks=[{"name": "ci", "bucket": "pending"}],
        merge_state="CLEAN",
    )
    assert green is False
    assert "still pending" in detail


def test_no_checks_registered_at_all_is_not_green(wfc, monkeypatch):
    """No checks whatsoever → not-yet-registered → retried to timeout, never
    reported green."""
    green, detail = _routed(wfc, monkeypatch, allchecks=[], merge_state="CLEAN")
    assert green is False
    assert "no checks registered" in detail


def test_end_to_end_from_raw_gh_output_to_green(wfc, monkeypatch):
    """The whole chain with nothing above `_run` faked: gh's real
    no-required-checks stderr in, a named state-(c) green out.

    The other state-(c) tests inject the sentinel directly, so they would still
    pass if `_query_checks` were left mapping gh's message to None. This one
    would not — it is the test that actually holds the dead branch open.
    """
    calls = []

    def fake_run(argv, timeout=60):
        calls.append(argv)
        if "--required" in argv:
            return _gh_no_required()
        if argv[1] == "pr" and argv[2] == "checks":
            return _Proc(0, '[{"name":"ci","bucket":"pass"}]', "")
        return _Proc(0, '{"mergeStateStatus":"CLEAN"}', "")

    monkeypatch.setattr(wfc, "_run", fake_run)
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=10)
    assert green is True
    assert "NO REQUIRED CHECKS CONFIGURED" in detail
    # And it got there without burning retries on a "transient" read.
    assert sum("--required" in a for a in calls) == 1


def test_gh_unreadable_is_still_retried_not_routed_to_state_c(wfc, monkeypatch):
    """The separation the whole fix rests on: a genuine unreadable read must NOT
    reach the no-required-checks path. Asserts by the message, since the (c)
    path would have stamped its own prefix."""
    monkeypatch.setattr(wfc, "_query_checks", lambda pr, *, required: None)
    monkeypatch.setattr(
        wfc, "_merge_state",
        lambda pr: pytest.fail("state (c) must not be reached on an unreadable read"),
    )
    monkeypatch.setattr(wfc.time, "sleep", lambda s: None)
    green, detail = wfc.wait_for_required_checks(1, interval=0, timeout=600, retries=2)
    assert green is False
    assert "unreadable after 2 retries" in detail
    assert "NO REQUIRED CHECKS CONFIGURED" not in detail


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


# ── CLI entry point (`main`) exit codes ─────────────────────────────────────
#
# The rest of this file exercises `wait_for_required_checks` directly, which
# leaves the actual `sys.exit` code returned by `main()` untested. A caller
# gating a merge script on `$?` cares about that mapping specifically, so
# lock it in explicitly: green -> 0, not-green/unreadable -> non-zero
# (INTERNAL_ERROR), usage error -> non-zero (USAGE_ERROR). Confirmed by direct
# invocation against a real PR (see PR body) that this already holds on
# origin/main; these tests guard against a future regression, not a fix.


def test_main_exit_code_zero_when_green(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "wait_for_required_checks",
        lambda pr, **kw: (True, "all 2 required checks green"),
    )
    assert wfc.main(["1"]) == wfc.SUCCESS


def test_main_exit_code_nonzero_when_not_green(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "wait_for_required_checks",
        lambda pr, **kw: (False, "required checks not green: ['ci(fail)']"),
    )
    rc = wfc.main(["1"])
    assert rc != wfc.SUCCESS
    assert rc == wfc.INTERNAL_ERROR


def test_main_exit_code_nonzero_when_unreadable_after_retries(wfc, monkeypatch):
    """Reproduces the exact reported scenario: gh unreadable after retries
    prints NOT GREEN — the exit code must not be 0."""
    monkeypatch.setattr(
        wfc, "wait_for_required_checks",
        lambda pr, **kw: (False, "gh pr checks unreadable after 5 retries"),
    )
    rc = wfc.main(["45"])
    assert rc != wfc.SUCCESS
    assert rc == wfc.INTERNAL_ERROR


def test_main_exit_code_usage_error_on_bad_args(wfc, monkeypatch):
    monkeypatch.setattr(
        wfc, "wait_for_required_checks",
        lambda pr, **kw: (True, "should never be called"),
    )
    rc = wfc.main(["1", "--interval", "0"])
    assert rc == wfc.USAGE_ERROR
    assert rc != wfc.SUCCESS

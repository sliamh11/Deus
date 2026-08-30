"""Tests for LIA-135 stale-process / resident-stack reaping
(scripts/maintenance/reap_stale.py).

Hermetic: every test builds its own synthetic process table, so nothing
inspects or signals a real process and no docker command is ever run. The one
end-to-end kill path is exercised against a process the TEST ITSELF spawns.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "maintenance" / "reap_stale.py"


def _load():
    spec = importlib.util.spec_from_file_location("reap_stale", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reap_stale"] = mod
    spec.loader.exec_module(mod)
    return mod


rs = _load()


def _proc(pid: int, ppid: int, age_s: float, command: str):
    return rs.Proc(pid=pid, ppid=ppid, age_s=age_s, command=command)


# -- etime parsing -----------------------------------------------------------
# macOS `ps` has no `etimes` keyword, so the POSIX `[[DD-]HH:]MM:SS` string is
# the only elapsed-time source. Every real form observed on this host.


@pytest.mark.parametrize(
    "value,expected",
    [
        ("04:31", 271),                 # MM:SS
        ("17:58:49", 64729),            # HH:MM:SS
        ("1-00:00:00", 86400),          # D-HH:MM:SS
        ("25-04:03:11", 2174591),       # DD-HH:MM:SS, the 25-day viewer
        ("  17:58:49  ", 64729),        # ps right-pads its columns
    ],
)
def test_parse_etime_accepts_every_real_ps_form(value, expected):
    assert rs.parse_etime(value) == expected


@pytest.mark.parametrize("value", ["", "bogus", "12", "a:b:c", "1:2:3:4"])
def test_parse_etime_rejects_garbage_rather_than_guessing(value):
    # None, not 0. A 0 would read as "brand new" and protect the process; a
    # wrong non-zero could read as ancient and reap it. Neither is a guess this
    # module is entitled to make.
    assert rs.parse_etime(value) is None


# -- selection guards --------------------------------------------------------


def test_pattern_is_required_there_is_no_match_everything_mode():
    procs = [_proc(100, 1, 10_000_000, "/usr/bin/something-important")]
    reapable, _ = rs.select_stale(procs, ["bin/claude agents"], 0, self_pid=999)
    assert reapable == []


@pytest.mark.parametrize("pattern", ["", "   ", "\t"])
def test_empty_pattern_is_refused_not_treated_as_match_everything(pattern):
    # `"" in anything` is True, so an empty pattern would silently make every
    # process past the age threshold reapable -- the exact opposite of the
    # documented "no match-everything mode" guarantee.
    procs = [_proc(100, 1, 10_000_000, "/usr/bin/something-important")]
    with pytest.raises(ValueError):
        rs.select_stale(procs, [pattern], 0, self_pid=999)


def test_no_patterns_at_all_is_refused():
    procs = [_proc(100, 1, 10_000_000, "/usr/bin/something-important")]
    with pytest.raises(ValueError):
        rs.select_stale(procs, [], 0, self_pid=999)


def test_an_empty_pattern_among_valid_ones_still_refuses():
    # The dangerous mixed case: one good pattern makes the call look sane.
    procs = [_proc(100, 1, 10_000_000, "/usr/bin/something-important")]
    with pytest.raises(ValueError):
        rs.select_stale(procs, ["bin/claude agents", ""], 0, self_pid=999)


def test_cli_rejects_an_empty_pattern_before_reaching_the_kill_path(monkeypatch):
    def _boom():
        raise AssertionError("reached the process sweep with an empty pattern")

    monkeypatch.setattr(rs, "snapshot_processes", _boom)
    with pytest.raises(SystemExit) as excinfo:
        rs.main(["--kill", "--pattern", "", "--no-stacks"], notifier=lambda t, m: None)
    assert excinfo.value.code != 0


def test_matching_and_old_enough_is_reapable():
    procs = [_proc(100, 1, 90_000, "/Users/x/.local/bin/claude agents")]
    reapable, _ = rs.select_stale(
        procs, ["bin/claude agents"], 12 * 3600, self_pid=999
    )
    assert [p.pid for p in reapable] == [100]


def test_younger_than_threshold_is_skipped_with_a_reason():
    procs = [_proc(100, 1, 3600, "/Users/x/.local/bin/claude agents")]
    reapable, skipped = rs.select_stale(
        procs, ["bin/claude agents"], 12 * 3600, self_pid=999
    )
    assert reapable == []
    assert [(p.pid, "younger" in reason) for p, reason in skipped] == [(100, True)]


def test_never_reaps_itself():
    procs = [_proc(100, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    reapable, skipped = rs.select_stale(procs, ["bin/claude agents"], 0, self_pid=100)
    assert reapable == []
    assert "self or ancestor" in skipped[0][1]


def test_never_reaps_an_ancestor_of_itself():
    # 300 (the reaper) <- 200 (a matching parent) <- 100 (init-ish). Killing
    # 200 would kill the reaper's own parent mid-sweep.
    procs = [
        _proc(100, 1, 10_000_000, "/sbin/launchd"),
        _proc(200, 100, 10_000_000, "/Users/x/.local/bin/claude agents"),
        _proc(300, 200, 60, "python3 reap_stale.py"),
    ]
    reapable, skipped = rs.select_stale(procs, ["bin/claude agents"], 0, self_pid=300)
    assert reapable == []
    assert "self or ancestor" in skipped[0][1]


def test_never_reaps_a_process_with_tmux_in_its_ancestry():
    # An attached pane is someone's live session, however old the leaf is.
    procs = [
        _proc(100, 1, 10_000_000, "tmux: server"),
        _proc(200, 100, 10_000_000, "-zsh"),
        _proc(300, 200, 10_000_000, "/Users/x/.local/bin/claude agents"),
    ]
    reapable, skipped = rs.select_stale(procs, ["bin/claude agents"], 0, self_pid=999)
    assert reapable == []
    assert "tmux" in skipped[0][1]


def test_reaps_an_equally_old_sibling_that_is_not_under_tmux():
    # Calibration for the test above: the tmux guard must be the discriminator,
    # not something else that would have skipped both.
    procs = [
        _proc(100, 1, 10_000_000, "tmux: server"),
        _proc(300, 100, 10_000_000, "/Users/x/.local/bin/claude agents"),
        _proc(400, 1, 10_000_000, "/Users/x/.local/bin/claude agents"),
    ]
    reapable, _ = rs.select_stale(procs, ["bin/claude agents"], 0, self_pid=999)
    assert [p.pid for p in reapable] == [400]


def test_ancestry_survives_a_ppid_cycle():
    # A corrupt/racy ps snapshot must not hang the sweep.
    procs = [_proc(100, 200, 1, "a"), _proc(200, 100, 1, "b")]
    by_pid = {p.pid: p for p in procs}
    assert rs.ancestry(100, by_pid) == {100, 200}


def test_multiple_patterns_are_ored():
    procs = [
        _proc(100, 1, 10_000_000, "/bin/alpha-daemon"),
        _proc(200, 1, 10_000_000, "/bin/beta-daemon"),
        _proc(300, 1, 10_000_000, "/bin/gamma-daemon"),
    ]
    reapable, _ = rs.select_stale(procs, ["alpha", "gamma"], 0, self_pid=999)
    assert sorted(p.pid for p in reapable) == [100, 300]


# -- report-only must be visible, not a silent log ---------------------------


def test_report_only_notifies_once_with_count_and_oldest_age(monkeypatch):
    procs = [
        _proc(100, 1, 2_174_591, "/Users/x/.local/bin/claude agents"),  # 25.2d
        _proc(200, 1, 90_000, "/Users/x/.local/bin/claude agents"),
    ]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)
    sent: list[tuple[str, str]] = []

    failures = rs.reap_processes(
        ["bin/claude agents"], 0, kill=False,
        notifier=lambda t, m: sent.append((t, m)),
    )

    assert failures == 0
    assert len(sent) == 1, "one banner for the sweep, not one per process"
    assert "2 stale process" in sent[0][1]
    assert "25.2d" in sent[0][1]


def test_clean_sweep_stays_silent(monkeypatch):
    # A banner on every clean run is noise a human learns to dismiss, which
    # would defeat the point of banner-ing at all.
    monkeypatch.setattr(rs, "snapshot_processes", lambda: [])
    sent = []
    rs.reap_processes(["bin/claude agents"], 0, kill=False, notifier=lambda t, m: sent.append(m))
    assert sent == []


def test_kill_mode_does_not_notify(monkeypatch):
    # Nothing left for a human to action once it has acted.
    procs = [_proc(100, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)
    monkeypatch.setattr(rs, "terminate", lambda pid, **kw: "terminated")
    sent = []
    rs.reap_processes(
        ["bin/claude agents"], 0, kill=True, notifier=lambda t, m: sent.append(m)
    )
    assert sent == []


def test_report_only_never_calls_terminate(monkeypatch):
    procs = [_proc(100, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)

    def _boom(pid, **kw):
        raise AssertionError(f"report-only mode signalled pid {pid}")

    monkeypatch.setattr(rs, "terminate", _boom)
    rs.reap_processes(["bin/claude agents"], 0, kill=False, notifier=lambda t, m: None)


def test_default_wiring_is_report_only(monkeypatch):
    # The nightly maintenance path calls main() with no arguments. If this ever
    # starts killing, that is the regression this test exists to catch.
    procs = [_proc(100, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)

    def _boom(pid, **kw):
        raise AssertionError("default (unattended) invocation killed a process")

    monkeypatch.setattr(rs, "terminate", _boom)
    rc = rs.main(["--no-stacks"], notifier=lambda t, m: None)
    assert rc == 0


# -- the real kill path, against a process this test spawned -----------------


def test_terminate_kills_a_process_we_spawned_and_sigterm_is_what_does_it():
    """SIGTERM is what actually ends the process; SIGKILL is only the backstop.

    Caveat this test had to be written around, and the reason it does not
    assert `outcome == "terminated"`: pytest is this child's PARENT, so between
    the child dying and `Popen.wait()` reaping it, it is a ZOMBIE -- and
    `os.kill(pid, 0)` succeeds on a zombie. `terminate()`'s liveness probe
    therefore cannot see the death, waits out its grace period and reports
    "killed". That is an artifact of the test being the parent, not a defect:
    the reaper never parents what it reaps, so nothing it targets can become a
    zombie it is responsible for.

    The returncode is the unambiguous evidence, so assert on that instead:
    -SIGTERM means the graceful signal ended it, and a SIGKILL that arrived
    afterwards changed nothing.
    """
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
    try:
        outcome = rs.terminate(child.pid)
        assert outcome in {"terminated", "killed"}
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGTERM, (
            "process did not die from SIGTERM; the graceful path is not working"
        )
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
    finally:
        if child.poll() is None:  # pragma: no cover - only on an assert failure
            child.kill()
            child.wait(timeout=10)


def test_terminate_escalates_to_sigkill_when_sigterm_is_ignored():
    """A process that traps SIGTERM must still be reaped."""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import signal, time\n"
         "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
         "time.sleep(300)"]
    )
    try:
        # Give the child a moment to install its handler before signalling.
        time.sleep(1)
        outcome = rs.terminate(child.pid, grace_s=2.0)
        assert outcome == "killed"
        child.wait(timeout=10)
        assert child.returncode == -signal.SIGKILL
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
            child.wait(timeout=10)


def test_terminate_on_a_dead_pid_reports_already_gone():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    # Reaped by Popen.wait, so the pid is no longer signallable by us.
    assert rs.terminate(child.pid) in {"already-gone", "terminated"}


def test_end_to_end_kill_of_a_self_spawned_sentinel(monkeypatch):
    """Selection + kill together, driven against a real process, via a unique
    pattern that cannot match anything else on the host."""
    marker = "lia135-reaper-sentinel-e2e"
    child = subprocess.Popen(
        [sys.executable, "-c", f"# {marker}\nimport time; time.sleep(300)"]
    )
    try:
        # Real ps snapshot, real pid, only the age is forced so the test does
        # not have to wait 12 hours.
        real = rs.snapshot_processes()
        aged = [
            rs.Proc(p.pid, p.ppid, 10_000_000 if p.pid == child.pid else p.age_s,
                    p.command)
            for p in real
        ]
        monkeypatch.setattr(rs, "snapshot_processes", lambda: aged)

        assert any(p.pid == child.pid for p in aged), "sentinel absent from ps"

        failures = rs.reap_processes(
            [marker], 12 * 3600, kill=True, notifier=lambda t, m: None
        )
        assert failures == 0

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.1)
        assert child.poll() is not None, "sentinel survived the reaper"
    finally:
        if child.poll() is None:  # pragma: no cover
            child.kill()
        child.wait(timeout=10)


# -- stack specs -------------------------------------------------------------


def test_malformed_stack_spec_is_a_failure_not_a_silent_skip():
    # A typo'd spec must not read as "nothing to tear down".
    assert rs.reap_stacks(["no-equals-sign"], 0, apply=True) == 1


def test_missing_compose_file_is_skipped_without_failing(tmp_path):
    spec = f"ghost={tmp_path / 'nope' / 'docker-compose.yml'}"
    assert rs.reap_stacks([spec], 0, apply=True) == 0


def test_stack_not_running_is_never_torn_down(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(rs, "stack_uptime_s", lambda f: None)

    def _boom(f):
        raise AssertionError("tore down a stack that was not running")

    monkeypatch.setattr(rs, "teardown_stack", _boom)
    assert rs.reap_stacks([f"x={compose}", ], 0, apply=True) == 0


def test_stack_under_threshold_is_left_alone(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(rs, "stack_uptime_s", lambda f: 3600)

    def _boom(f):
        raise AssertionError("tore down a stack under the age threshold")

    monkeypatch.setattr(rs, "teardown_stack", _boom)
    assert rs.reap_stacks([f"x={compose}"], 12 * 3600, apply=True) == 0


def test_stack_over_threshold_is_torn_down(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(rs, "stack_uptime_s", lambda f: 13 * 3600)
    called: list[str] = []
    monkeypatch.setattr(
        rs, "teardown_stack", lambda f: (called.append(f), (True, "down"))[1]
    )
    assert rs.reap_stacks([f"x={compose}"], 12 * 3600, apply=True) == 0
    assert called == [str(compose)]


def test_no_stack_teardown_reports_without_acting(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(rs, "stack_uptime_s", lambda f: 13 * 3600)

    def _boom(f):
        raise AssertionError("acted under --no-stack-teardown")

    monkeypatch.setattr(rs, "teardown_stack", _boom)
    assert rs.reap_stacks([f"x={compose}"], 12 * 3600, apply=False) == 0


def test_teardown_failure_is_surfaced_as_a_failure(tmp_path, monkeypatch):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    monkeypatch.setattr(rs, "stack_uptime_s", lambda f: 13 * 3600)
    monkeypatch.setattr(rs, "teardown_stack", lambda f: (False, "docker daemon down"))
    assert rs.reap_stacks([f"x={compose}"], 12 * 3600, apply=True) == 1


# -- ps must never truncate the command column --------------------------------
# Linux procps truncates `command` to 80 chars when stdout is not a tty; macOS
# BSD ps does not. So a pattern past that cut is invisible on Linux ONLY, and
# the reaper reports "nothing stale" while the target sits there. This was
# caught by CI, never locally, and is pinned here so it cannot come back.


def test_snapshot_asks_ps_for_unlimited_width():
    seen: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = "1 0 00:01 /sbin/launchd\n"
        stderr = ""

    def _run(args, **kw):
        seen.append(list(args))
        return _R()

    import unittest.mock as _mock
    with _mock.patch.object(rs.subprocess, "run", _run):
        rs.snapshot_processes()

    argv = seen[0]
    assert any("ww" in a for a in argv), f"ps would truncate at 80 cols: {argv}"


def test_identity_recheck_asks_ps_for_unlimited_width():
    seen: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = "some command\n"
        stderr = ""

    def _run(args, **kw):
        seen.append(list(args))
        return _R()

    import unittest.mock as _mock
    with _mock.patch.object(rs.subprocess, "run", _run):
        rs.still_matches(1234, ["some"])

    argv = seen[0]
    assert "-ww" in argv, f"ps would truncate at 80 cols: {argv}"


def test_a_marker_late_in_a_long_command_line_is_still_found():
    """End-to-end guard for the truncation bug, with a deliberately LONG argv.

    The padding pushes the marker well past column 80, which is exactly where
    the Linux default would have cut it. Passes on macOS either way; the point
    is that it FAILS on Linux without `-ww`.
    """
    marker = "lia135-late-marker-probe"
    padding = "x" * 200
    child = subprocess.Popen(
        [sys.executable, "-c", f"# {padding} {marker}\nimport time; time.sleep(60)"]
    )
    try:
        time.sleep(0.5)
        procs = rs.snapshot_processes()
        match = [p for p in procs if p.pid == child.pid]
        assert match, "sentinel absent from ps snapshot entirely"
        assert marker in match[0].command, (
            "command line was truncated before the marker; ps needs -ww"
        )
        assert rs.still_matches(child.pid, [marker]) is True
    finally:
        child.kill()
        child.wait(timeout=10)


# -- a ps failure must never read as "nothing stale" -------------------------
# Mirror of the docker case below. Turning an inspection failure into an empty
# list would make the sweep report a clean bill of health at exactly the moment
# it could not see anything.


def test_ps_nonzero_exit_raises_rather_than_reporting_empty(monkeypatch):
    monkeypatch.setattr(
        rs.subprocess, "run", _fake_run(1, stdout="", stderr="ps: illegal option")
    )
    with pytest.raises(rs.ProcessListUnavailable) as excinfo:
        rs.snapshot_processes()
    assert "illegal option" in str(excinfo.value)


def test_ps_timeout_raises_rather_than_reporting_empty(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ps", timeout=30)

    monkeypatch.setattr(rs.subprocess, "run", _timeout)
    with pytest.raises(rs.ProcessListUnavailable):
        rs.snapshot_processes()


def test_ps_missing_binary_raises_rather_than_reporting_empty(monkeypatch):
    def _oserror(*a, **kw):
        raise OSError("No such file or directory: 'ps'")

    monkeypatch.setattr(rs.subprocess, "run", _oserror)
    with pytest.raises(rs.ProcessListUnavailable):
        rs.snapshot_processes()


def test_ps_succeeding_with_no_rows_is_still_treated_as_a_failure(monkeypatch):
    # `ps -ax` on a live host always lists at least the calling process, so an
    # empty body is not a credible "nothing is running".
    monkeypatch.setattr(rs.subprocess, "run", _fake_run(0, stdout="   \n"))
    with pytest.raises(rs.ProcessListUnavailable):
        rs.snapshot_processes()


def test_real_ps_returns_a_populated_snapshot():
    # Calibration for the four tests above: the real command must still work,
    # or they would all pass against a permanently broken implementation.
    procs = rs.snapshot_processes()
    assert len(procs) > 5
    assert any(p.pid == os.getpid() for p in procs), "own pid absent from snapshot"


def test_ps_failure_is_counted_as_a_failure_by_reap_processes(monkeypatch):
    def _raise():
        raise rs.ProcessListUnavailable("ps timed out after 30s")

    monkeypatch.setattr(rs, "snapshot_processes", _raise)
    sent = []
    failures = rs.reap_processes(
        ["bin/claude agents"], 0, kill=False, notifier=lambda t, m: sent.append(m)
    )
    assert failures == 1
    assert sent == [], "must not banner a count it could not measure"


# -- PID reuse between the scan and the kill ---------------------------------
# The ps snapshot is stale the moment it is taken. If the pid is recycled in
# that window, signalling it kills an unrelated process that merely inherited
# the number -- so identity is re-confirmed immediately before the signal.


def test_still_matches_true_for_a_live_matching_process():
    marker = "lia135-still-matches-probe"
    child = subprocess.Popen(
        [sys.executable, "-c", f"print('{marker}')\nimport time; time.sleep(60)"]
    )
    try:
        time.sleep(0.5)
        assert rs.still_matches(child.pid, [marker]) is True
    finally:
        child.kill()
        child.wait(timeout=10)


def test_still_matches_false_when_the_pid_now_holds_something_else():
    # The reuse case: the pid is alive, but it is no longer our target.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.5)
        assert rs.still_matches(child.pid, ["a-pattern-this-process-does-not-have"]) is False
    finally:
        child.kill()
        child.wait(timeout=10)


def test_still_matches_false_for_a_dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    assert rs.still_matches(child.pid, ["python"]) is False


def test_kill_is_skipped_when_identity_no_longer_matches(monkeypatch):
    """The guard must PREVENT the signal, not merely report afterwards."""
    procs = [_proc(4242, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)
    monkeypatch.setattr(rs, "still_matches", lambda pid, pats: False)

    def _boom(pid, **kw):
        raise AssertionError(f"signalled pid {pid} after identity re-check failed")

    monkeypatch.setattr(rs, "terminate", _boom)
    failures = rs.reap_processes(
        ["bin/claude agents"], 0, kill=True, notifier=lambda t, m: None
    )
    assert failures == 0


def test_kill_proceeds_when_identity_still_matches(monkeypatch):
    # Calibration for the test above: the guard must not block the normal path.
    procs = [_proc(4242, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)
    monkeypatch.setattr(rs, "still_matches", lambda pid, pats: True)
    killed: list[int] = []
    monkeypatch.setattr(
        rs, "terminate", lambda pid, **kw: (killed.append(pid), "terminated")[1]
    )
    rs.reap_processes(["bin/claude agents"], 0, kill=True, notifier=lambda t, m: None)
    assert killed == [4242]


def test_report_only_does_not_bother_re_checking(monkeypatch):
    # Nothing is signalled, so there is no reuse window to guard.
    procs = [_proc(4242, 1, 10_000_000, "/Users/x/.local/bin/claude agents")]
    monkeypatch.setattr(rs, "snapshot_processes", lambda: procs)

    def _boom(pid, pats):
        raise AssertionError("re-checked identity in report-only mode")

    monkeypatch.setattr(rs, "still_matches", _boom)
    rs.reap_processes(["bin/claude agents"], 0, kill=False, notifier=lambda t, m: None)


# -- a docker failure must never read as "nothing running" -------------------
# `docker compose ps -q` reports its errors on stderr and leaves stdout empty,
# so a stopped daemon looks byte-for-byte like a stopped stack. Collapsing the
# two would let the reaper report success while doing nothing -- the same
# silent-safety-net failure this whole ticket exists to fix.


def _fake_run(returncode: int, stdout: str = "", stderr: str = ""):
    class _R:
        pass

    r = _R()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return lambda *a, **kw: r


def test_docker_nonzero_exit_raises_rather_than_reporting_empty(monkeypatch):
    monkeypatch.setattr(
        rs.subprocess, "run",
        _fake_run(1, stdout="", stderr="Cannot connect to the Docker daemon"),
    )
    with pytest.raises(rs.DockerUnavailable) as excinfo:
        rs.stack_uptime_s("/nonexistent/docker-compose.yml")
    assert "Docker daemon" in str(excinfo.value)


def test_docker_timeout_raises_rather_than_reporting_empty(monkeypatch):
    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=60)

    monkeypatch.setattr(rs.subprocess, "run", _timeout)
    with pytest.raises(rs.DockerUnavailable):
        rs.stack_uptime_s("/nonexistent/docker-compose.yml")


def test_docker_missing_binary_raises_rather_than_reporting_empty(monkeypatch):
    def _oserror(*a, **kw):
        raise OSError("No such file or directory: 'docker'")

    monkeypatch.setattr(rs.subprocess, "run", _oserror)
    with pytest.raises(rs.DockerUnavailable):
        rs.stack_uptime_s("/nonexistent/docker-compose.yml")


def test_genuinely_stopped_stack_returns_none_not_an_error(monkeypatch):
    # Calibration for the three tests above: a SUCCESSFUL query with no
    # containers is the one case that legitimately means "not running".
    monkeypatch.setattr(rs.subprocess, "run", _fake_run(0, stdout="\n"))
    assert rs.stack_uptime_s("/nonexistent/docker-compose.yml") is None


def test_partial_inspect_result_raises_rather_than_tearing_down(monkeypatch):
    # Two containers, one start time. Acting on the partial view could tear
    # down a stack whose other container had just restarted.
    calls = {"n": 0}

    def _run(args, **kw):
        calls["n"] += 1
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        r.stdout = "id1\nid2\n" if calls["n"] == 1 else "2026-08-26T10:00:00.000000000Z\n"
        return r

    monkeypatch.setattr(rs.subprocess, "run", _run)
    with pytest.raises(rs.DockerUnavailable) as excinfo:
        rs.stack_uptime_s("/nonexistent/docker-compose.yml")
    assert "1 start times for 2 containers" in str(excinfo.value)


def test_unparseable_start_time_raises_rather_than_being_skipped(monkeypatch):
    calls = {"n": 0}

    def _run(args, **kw):
        calls["n"] += 1
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        r.stdout = "id1\n" if calls["n"] == 1 else "not-a-timestamp\n"
        return r

    monkeypatch.setattr(rs.subprocess, "run", _run)
    with pytest.raises(rs.DockerUnavailable):
        rs.stack_uptime_s("/nonexistent/docker-compose.yml")


def test_docker_failure_is_counted_as_a_failure_by_reap_stacks(tmp_path, monkeypatch):
    # End of the chain: the raise must become a non-zero exit so the nightly
    # summary marks the task FAILED instead of printing "not running".
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")

    def _raise(f):
        raise rs.DockerUnavailable("Cannot connect to the Docker daemon")

    monkeypatch.setattr(rs, "stack_uptime_s", _raise)

    def _boom(f):
        raise AssertionError("tore down a stack whose state could not be read")

    monkeypatch.setattr(rs, "teardown_stack", _boom)
    assert rs.reap_stacks([f"x={compose}"], 0, apply=True) == 1


def test_rfc3339_nanosecond_start_time_parses_to_a_sane_age(monkeypatch):
    # The real docker format. A wrong parse here means either never tearing
    # down or tearing down early, and neither would fail loudly.
    started = datetime.now(timezone.utc) - timedelta(hours=13)
    stamp = started.strftime("%Y-%m-%dT%H:%M:%S.") + "123456789Z"
    calls = {"n": 0}

    def _run(args, **kw):
        calls["n"] += 1
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        r.stdout = "id1\n" if calls["n"] == 1 else stamp + "\n"
        return r

    monkeypatch.setattr(rs.subprocess, "run", _run)
    age = rs.stack_uptime_s("/nonexistent/docker-compose.yml")
    assert 12.9 * 3600 < age < 13.1 * 3600, f"parsed age {age}s is not ~13h"


def test_youngest_container_wins_not_oldest(monkeypatch):
    # Deliberate: a stack with one just-restarted service has not been resident
    # for the age of its oldest container.
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%S.") + "000000000Z"
    young = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.") + "000000000Z"
    calls = {"n": 0}

    def _run(args, **kw):
        calls["n"] += 1
        class _R:
            returncode = 0
            stderr = ""
        r = _R()
        r.stdout = "id1\nid2\n" if calls["n"] == 1 else f"{old}\n{young}\n"
        return r

    monkeypatch.setattr(rs.subprocess, "run", _run)
    age = rs.stack_uptime_s("/nonexistent/docker-compose.yml")
    assert age < 2 * 3600, "took the oldest container's age, not the youngest"


def test_teardown_never_passes_dash_v_so_volumes_survive(monkeypatch):
    """`down -v` DESTROYS the stack's volumes.

    Until now this was enforced only by a comment. The whole reason stack
    teardown is allowed to act unattended, while process killing is not, is
    that `down` without `-v` is reversible with `up -d`. Add `-v` and that
    justification evaporates silently, taking the Langfuse database with it.
    """
    seen: list[list[str]] = []

    class _R:
        returncode = 0
        stdout = ""
        stderr = ""

    def _run(args, **kw):
        seen.append(list(args))
        return _R()

    monkeypatch.setattr(rs.subprocess, "run", _run)
    ok, _ = rs.teardown_stack("/nonexistent/docker-compose.yml")

    assert ok
    assert seen, "teardown_stack ran no command at all"
    argv = seen[0]
    assert "down" in argv, f"not a teardown command: {argv}"
    assert "-v" not in argv, f"teardown would destroy volumes: {argv}"
    assert "--volumes" not in argv, f"teardown would destroy volumes: {argv}"


def test_langfuse_is_a_default_stack_and_the_threshold_is_at_most_12h():
    # The lane that owns judge_runner.sh brings the stack UP at 05:15 and
    # deliberately adds no teardown, relying on this reaper for the stop half.
    # Dropping langfuse from the defaults, or raising this threshold, silently
    # restores the 6-day-resident behaviour that caused the overload.
    assert any("langfuse" in s for s in rs.DEFAULT_STACKS)
    assert rs.DEFAULT_STACK_MAX_AGE_HOURS <= 12

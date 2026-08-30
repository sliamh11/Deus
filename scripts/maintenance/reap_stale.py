#!/usr/bin/env python3
"""
LIA-135: reap stale processes and tear down resident stacks nothing else owns.

The host reached load 50-74 on 2026-08-25. There was no runaway process. There
were three long-lived things with no owner for the question "is this still
needed": a Langfuse docker stack up 6 days with zero HTTP requests, ten
`claude agents` TUI viewers aged 1-25 days, and the Docker VM alive only
because those containers were. Every component was behaving correctly; the
defect was the absence of a reaper.

Design
------
Two reap classes sharing one select-then-act shape, with the default risk
posture chosen per resource type by a single test: is the action reversible?

  1. Stale PROCESSES -- irreversible, so REPORT-ONLY by default.
     `--kill` opts in. A report-only sweep that finds something raises one
     desktop banner (see "Not a silent log" below) rather than printing into a
     log nobody reads.

  2. Resident STACKS -- reversible, so ACT by default.
     `docker compose down` (never `-v`) stops containers and preserves
     volumes, so `docker compose up -d` restores it. Only stacks named
     explicitly on the command line are touched; there is no blanket
     `docker ps` sweep.

A third reap class picks its posture from that same reversibility test.

Not a silent log
----------------
The failure this ticket exists to fix is "no owner for is-this-still-needed".
A finding printed into a launchd stdout log nobody reads reproduces exactly
that, so a report-only sweep with findings fires ONE macOS banner carrying the
count and the oldest age. Silent when the sweep is clean, so it does not become
noise a human learns to dismiss. The stack half notifies nothing: it acts, so
there is nothing for a human to action.

`--kill` is a MANUAL path and stays one
---------------------------------------
The nightly `maintenance.py` wiring passes report-only, and flipping that is
not a reviewer's call. Age is not the same signal as idle: a plain, non-tmux
terminal in which someone is actively watching an 18-hour-old viewer passes all
four guards below. The tmux-ancestry guard covers multiplexed sessions and
nothing else.

Before `--kill` could ever graduate to the unattended nightly path it would
need an idle signal this module does not have:

  * the process's controlling TTY belongs to that terminal's ACTIVE foreground
    process group (or it has no controlling TTY at all), and
  * CPU time since the previous sample is ~zero.

Neither is implemented. Age-only selection is not sufficient for unattended
killing, and that is the reason -- read it before changing the default.

First run after a long gap
--------------------------
Repairing a job that has not run in weeks can itself be the incident: when
`com.deus.maintenance` was repaired on 2026-08-26, `prune` ran for the first
time in weeks and soft-deleted 847 of 2079 atoms whose files had merely moved.
So, explicitly, what THIS task does on its first successful scheduled run:

  * Processes: kills nothing. Report-only unless `--kill`. A first run finding
    viewers aged 25 days prints them, banners once, and exits 0.
  * Stacks: tears down any listed stack whose containers have all been up
    longer than `--stack-max-age-hours`. Worst case is that a human's open
    Langfuse UI stops responding until `langfuse up`. No volume, no database
    and no ingest state is touched.

Safety rules for process reaping (all four mattered during the manual cleanup)
  * Never the calling process, and never anything in its ancestry.
  * Never a process with `tmux` anywhere in its ancestry -- an attached pane
    is someone's live session.
  * Never anything whose command line does not contain a caller-supplied
    pattern. There is no default-match-everything mode.
  * Never anything younger than the age threshold.

Usage:
    python3 scripts/maintenance/reap_stale.py                  # defaults
    python3 scripts/maintenance/reap_stale.py --kill           # act on processes
    python3 scripts/maintenance/reap_stale.py --pattern foo --max-age-hours 1
    python3 scripts/maintenance/reap_stale.py --no-stacks
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Local helpers -- _notify.py lives next to this script, same import shape as
# judge_calibration.py and morning_report.py use.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _notify import macos_notify  # noqa: E402

# `claude agents` is a read-only TUI viewer that keeps running after its window
# closes. Observed accumulation rate: roughly one per hour of active use.
DEFAULT_PATTERNS = ["bin/claude agents"]
DEFAULT_MAX_AGE_HOURS = 12.0

# The Langfuse stack has never had an owner for stopping it. Verified
# 2026-08-26: judge_runner.sh contains no `docker` invocation at all -- it
# neither starts nor stops the stack. The lane that owns that script is adding
# the start half; this is the stop half. Data lives in docker volumes and
# survives `down`.
DEFAULT_STACKS = [f"langfuse={Path.home()}/.deus/langfuse/docker-compose.yml"]
DEFAULT_STACK_MAX_AGE_HOURS = 12.0

# `[[DD-]HH:]MM:SS` -- the POSIX `ps -o etime` format, identical on macOS and
# Linux. macOS has no `etimes` keyword, so the seconds form is not available.
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")


def parse_etime(value: str) -> float | None:
    """Parse `ps -o etime` into seconds. Returns None if unparseable."""
    m = _ETIME_RE.match(value.strip())
    if not m:
        return None
    days, hours, minutes, seconds = m.groups()
    return (
        int(days or 0) * 86400
        + int(hours or 0) * 3600
        + int(minutes) * 60
        + int(seconds)
    )


def format_age(seconds: float) -> str:
    """Human-readable age for a one-line banner."""
    if seconds >= 86400:
        return f"{seconds / 86400:.1f}d"
    return f"{seconds / 3600:.1f}h"


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    age_s: float
    command: str


class ProcessListUnavailable(Exception):
    """Listing processes FAILED, as distinct from finding none stale.

    Same trap as DockerUnavailable, on the other half: turning a `ps` failure
    into an empty list makes the sweep report a clean bill of health at exactly
    the moment it could not see anything. An empty list must mean "looked, host
    has no matching processes" and nothing else.
    """


def snapshot_processes() -> list[Proc]:
    """Every process on the host, as (pid, ppid, age, command).

    Raises ProcessListUnavailable if `ps` could not be run or failed. An empty
    return means the host genuinely reported no usable rows.
    """
    try:
        r = subprocess.run(
            # `-ww` means UNLIMITED WIDTH and is load-bearing, not cosmetic.
            # Linux procps truncates the command column to 80 characters when
            # stdout is not a tty -- which is always, here. A pattern appearing
            # past that cut is then invisible, so the reaper silently reports
            # nothing stale while the target sits there. macOS BSD ps does not
            # truncate, so this reproduces ONLY off-Darwin: caught by CI, never
            # locally. Accepted by both implementations.
            ["ps", "-axww", "-o", "pid=,ppid=,etime=,command="],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired:
        raise ProcessListUnavailable("ps timed out after 30s")
    except (OSError, subprocess.SubprocessError) as e:
        raise ProcessListUnavailable(str(e))
    if r.returncode != 0:
        detail = (r.stderr.strip() or f"exit {r.returncode}").splitlines()[0][:200]
        raise ProcessListUnavailable(detail)
    out = r.stdout
    if not out.strip():
        # `ps -ax` on a live host always lists at least this process. An empty
        # body means the query did not really succeed, whatever it exited with.
        raise ProcessListUnavailable("ps returned no rows at all")

    procs: list[Proc] = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime_s, command = parts
        age = parse_etime(etime_s)
        if age is None:
            continue
        try:
            procs.append(Proc(int(pid_s), int(ppid_s), age, command))
        except ValueError:
            continue
    return procs


def ancestry(pid: int, by_pid: dict[int, Proc]) -> set[int]:
    """Every pid from `pid` up to init, inclusive. Cycle-safe."""
    chain: set[int] = set()
    cur = pid
    while cur and cur not in chain:
        chain.add(cur)
        proc = by_pid.get(cur)
        if proc is None:
            break
        cur = proc.ppid
    return chain


def has_tmux_ancestor(pid: int, by_pid: dict[int, Proc]) -> bool:
    """True if `tmux` appears anywhere in this process's ancestry."""
    for anc in ancestry(pid, by_pid):
        proc = by_pid.get(anc)
        if proc and "tmux" in proc.command:
            return True
    return False


def select_stale(
    procs: list[Proc],
    patterns: list[str],
    max_age_s: float,
    self_pid: int,
) -> tuple[list[Proc], list[tuple[Proc, str]]]:
    """Split matching processes into (reapable, [(skipped, reason)]).

    Raises ValueError on an empty or whitespace-only pattern. `"" in anything`
    is True, so an empty pattern silently converts the pattern guard into
    match-everything and would make every process old enough to pass the age
    threshold reapable. Enforced HERE as well as at the CLI so no future caller
    can reach the kill path around the argument parser.
    """
    if not patterns:
        raise ValueError("at least one pattern is required")
    for pat in patterns:
        if not pat.strip():
            raise ValueError(
                "empty pattern would match every process; refusing to select"
            )

    by_pid = {p.pid: p for p in procs}
    protected = ancestry(self_pid, by_pid)

    reapable: list[Proc] = []
    skipped: list[tuple[Proc, str]] = []
    for p in procs:
        if not any(pat in p.command for pat in patterns):
            continue
        if p.pid in protected:
            skipped.append((p, "self or ancestor of the reaper"))
            continue
        if p.age_s < max_age_s:
            skipped.append((p, f"younger than threshold ({format_age(p.age_s)})"))
            continue
        if has_tmux_ancestor(p.pid, by_pid):
            skipped.append((p, "tmux in ancestry (live session)"))
            continue
        reapable.append(p)
    return reapable, skipped


def still_matches(pid: int, patterns: list[str]) -> bool:
    """Re-read this pid's command line NOW and confirm it still matches.

    NARROWS the PID-reuse window; it does not close it. A gap remains between
    this check and the `os.kill` that follows, and nothing short of a pidfd
    (Linux) or a process handle would eliminate it. Stated plainly so nobody
    reads this as a guarantee: it takes the exposure from "the whole duration
    of the sweep, which can be many seconds across hundreds of processes" down
    to a few milliseconds per process.

    Worth having anyway because the consequence is asymmetric. A reused pid
    means killing an unrelated, possibly important process that merely
    inherited the number; the cost of the check is one `ps` per reap candidate,
    and there are normally zero or a handful.

    Fails CLOSED: any error, a non-zero exit, or empty output returns False, so
    an unreadable process is never signalled. A process that has exited in the
    meantime also returns False, which is the correct answer -- there is
    nothing left to reap.
    """
    try:
        out = subprocess.run(
            # `-ww` for the same reason as snapshot_processes: without it,
            # Linux truncates at 80 columns and this re-check would report
            # "no longer matches" for a process that matches perfectly well,
            # turning the reuse guard into a blanket refusal to reap anything
            # with a long command line.
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if out.returncode != 0:
        return False
    command = out.stdout.strip()
    if not command:
        return False
    return any(pat in command for pat in patterns)


def terminate(pid: int, grace_s: float = 5.0) -> str:
    """SIGTERM, then SIGKILL if it is still alive. Returns an outcome word."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already-gone"
    except PermissionError:
        return "permission-denied"

    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "terminated"
        except PermissionError:
            return "permission-denied"

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return "terminated"
    except PermissionError:
        return "permission-denied"
    return "killed"


def reap_processes(
    patterns: list[str],
    max_age_s: float,
    kill: bool,
    notifier=macos_notify,
) -> int:
    """Sweep for stale processes. Returns the failure count."""
    failures = 0
    try:
        procs = snapshot_processes()
    except ProcessListUnavailable as e:
        # Loud, and a non-zero exit. A reaper that could not look is not a
        # reaper that found nothing -- reporting "clean" here would be the
        # silent safety net this ticket exists to remove.
        print(f"  proc: CANNOT LIST PROCESSES: {e}")
        return 1
    reapable, skipped = select_stale(procs, patterns, max_age_s, os.getpid())

    for p, reason in skipped:
        print(f"  proc {p.pid}: skipped -- {reason}")

    if not reapable:
        print(f"  proc: nothing stale matching {patterns} over {format_age(max_age_s)}")
        return 0

    for p in reapable:
        age = format_age(p.age_s)
        if not kill:
            print(f"  proc {p.pid}: age {age}, WOULD reap (report-only): {p.command[:80]}")
            continue
        # Re-confirm identity immediately before signalling. The ps snapshot is
        # already stale by the time we act on it, and a recycled pid would mean
        # killing an unrelated process that merely inherited the number.
        if not still_matches(p.pid, patterns):
            print(f"  proc {p.pid}: skipped -- exited or pid reused since the scan")
            continue
        outcome = terminate(p.pid)
        print(f"  proc {p.pid}: age {age}, {outcome}: {p.command[:80]}")
        if outcome == "permission-denied":
            failures += 1

    if not kill:
        # One banner, not one per process, and only when there is something to
        # act on. See "Not a silent log" in the module docstring.
        oldest = format_age(max(p.age_s for p in reapable))
        notifier(
            "Deus reaper",
            f"{len(reapable)} stale process(es), oldest {oldest} "
            f"-- run reap_stale.py --kill to clear",
        )
    return failures


# -- Resident stacks ---------------------------------------------------------


class DockerUnavailable(Exception):
    """A docker query FAILED, as distinct from reporting nothing running.

    These two must never collapse into one another. `docker compose ps -q`
    prints its errors on stderr and leaves stdout empty, so a stopped daemon,
    an unreadable compose file or a failed `inspect` produces exactly the same
    empty stdout as a legitimately-stopped stack. Reading that as "not running"
    would let the reaper report success while silently doing nothing -- which
    is precisely the silent-safety-net failure this ticket exists to fix.
    """


def _docker_out(args: list[str], timeout: int) -> str:
    """Run a docker query. Raises DockerUnavailable rather than masking a failure."""
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        raise DockerUnavailable(f"timed out after {timeout}s: {' '.join(args[:3])}")
    except (OSError, subprocess.SubprocessError) as e:
        raise DockerUnavailable(str(e))
    if r.returncode != 0:
        detail = (r.stderr.strip() or r.stdout.strip() or f"exit {r.returncode}")
        raise DockerUnavailable(detail.splitlines()[0][:200])
    return r.stdout


def stack_uptime_s(compose_file: str) -> float | None:
    """
    Seconds the whole stack has been up, or None if it is genuinely not running.

    Raises DockerUnavailable if the question could not be answered -- see that
    class. None means "asked successfully, nothing is up".

    Uses the YOUNGEST container's uptime deliberately: a stack that just had
    one service restarted has not been resident for the age of its oldest
    container, and tearing it down on that reading would kill live work. The
    error is always toward leaving a stack up, never toward killing it early.
    """
    ids = _docker_out(
        ["docker", "compose", "-f", compose_file, "ps", "-q"], timeout=60
    ).split()
    if not ids:
        return None

    stamps = _docker_out(
        ["docker", "inspect", "-f", "{{.State.StartedAt}}", *ids], timeout=60
    ).split()
    if len(stamps) != len(ids):
        # A short read means some container's state was not reported. Treating
        # the remainder as the whole stack could tear down on a partial view.
        raise DockerUnavailable(
            f"inspect returned {len(stamps)} start times for {len(ids)} containers"
        )

    now = time.time()
    ages: list[float] = []
    for stamp in stamps:
        # Docker emits RFC3339 with nanoseconds, which fromisoformat rejects
        # before 3.11; truncate to microseconds and normalise the trailing Z.
        s = stamp.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = re.sub(r"\.(\d{6})\d+", r".\1", s)
        try:
            ages.append(now - datetime.fromisoformat(s).timestamp())
        except ValueError:
            raise DockerUnavailable(f"unparseable container start time {stamp!r}")
    return min(ages)


def teardown_stack(compose_file: str) -> tuple[bool, str]:
    """`docker compose down` -- never `-v`, so volumes and data survive."""
    try:
        r = subprocess.run(
            ["docker", "compose", "-f", compose_file, "down"],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr.strip() or f"exit {r.returncode}")[:200]
    return True, "down"


def reap_stacks(specs: list[str], max_age_s: float, apply: bool) -> int:
    """Tear down listed stacks resident beyond the threshold. Returns failures."""
    failures = 0
    for spec in specs:
        name, _, compose_file = spec.partition("=")
        if not compose_file:
            print(f"  stack: malformed --stack spec {spec!r}, expected name=path")
            failures += 1
            continue
        if not Path(compose_file).is_file():
            print(f"  stack[{name}]: no compose file at {compose_file}, skipping")
            continue

        try:
            uptime = stack_uptime_s(compose_file)
        except DockerUnavailable as e:
            # Loud, and a non-zero exit, so the nightly summary marks this task
            # FAILED. A reaper that cannot see is not a reaper that found
            # nothing.
            print(f"  stack[{name}]: CANNOT DETERMINE STATE: {e}")
            failures += 1
            continue
        if uptime is None:
            print(f"  stack[{name}]: not running")
            continue
        age = format_age(uptime)
        if uptime < max_age_s:
            print(f"  stack[{name}]: up {age}, under threshold, leaving it")
            continue
        if not apply:
            print(f"  stack[{name}]: up {age}, WOULD tear down (report-only)")
            continue

        ok, detail = teardown_stack(compose_file)
        if ok:
            print(f"  stack[{name}]: up {age} -> torn down")
        else:
            print(f"  stack[{name}]: teardown FAILED: {detail}")
            failures += 1
    return failures


def main(argv: list[str] | None = None, notifier=macos_notify) -> int:
    ap = argparse.ArgumentParser(description="Reap stale processes and resident stacks")
    ap.add_argument(
        "--pattern", action="append", default=None,
        help="Substring a command line must contain to be reapable (repeatable)",
    )
    ap.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    ap.add_argument(
        "--kill", action="store_true",
        help="Actually terminate matching processes (default: report only)",
    )
    ap.add_argument("--no-processes", action="store_true", help="Skip the process sweep")
    ap.add_argument(
        "--stack", action="append", default=None,
        help="Resident stack as name=path/to/docker-compose.yml (repeatable)",
    )
    ap.add_argument(
        "--stack-max-age-hours", type=float, default=DEFAULT_STACK_MAX_AGE_HOURS
    )
    ap.add_argument("--no-stacks", action="store_true", help="Skip the stack sweep")
    ap.add_argument(
        "--no-stack-teardown", action="store_true",
        help="Report resident stacks without tearing them down",
    )
    args = ap.parse_args(argv)

    failures = 0

    if not args.no_processes:
        patterns = args.pattern or DEFAULT_PATTERNS
        # `"" in anything` is True, so an empty pattern is match-everything.
        # Refuse it outright rather than let --kill loose on the whole host.
        if any(not pat.strip() for pat in patterns):
            ap.error("--pattern may not be empty; it would match every process")
        failures += reap_processes(
            patterns,
            args.max_age_hours * 3600,
            kill=args.kill,
            notifier=notifier,
        )

    if not args.no_stacks:
        failures += reap_stacks(
            args.stack or DEFAULT_STACKS,
            args.stack_max_age_hours * 3600,
            apply=not args.no_stack_teardown,
        )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

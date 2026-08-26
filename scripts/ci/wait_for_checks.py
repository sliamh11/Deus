#!/usr/bin/env python3
"""Vetted poller for a PR's REQUIRED CI checks — a safe replacement for
``gh pr checks <pr> --required --watch``.

Why this exists: several call sites piped ``gh pr checks --watch`` through
``tail``/``grep``, which masks gh's non-zero exit and can wave a failing PR
through a merge gate. This helper owns the poll loop, parses the checks JSON
explicitly, and returns an authoritative ``(green: bool, detail: str)`` — never
a masked exit.

Three states are kept distinct; collapsing any two of them is how this gate
previously became unpassable:

(a) required checks exist and are green            -> exit 0
(b) required checks exist and are red or pending   -> not green
(c) NO required checks exist at all                -> the named
    ``NO REQUIRED CHECKS`` path below, never a silent green

State (c) is permanent on a private repo without GitHub Pro: branch protection
cannot be enabled there (``gh api repos/OWNER/REPO/branches/main/protection``
answers 403), so no branch ever has a required check. ``gh pr checks --required``
then exits non-zero with its explanation on STDERR and STDOUT EMPTY. That
signature is recognised explicitly and routed to (c); any OTHER unreadable
result stays an unreadable read and burns the retry budget. "gh could not be
read" and "gh says there are no required checks" are different facts and do not
share an exit code.

The (c) path re-queries WITHOUT ``--required`` and additionally asserts
``mergeStateStatus == CLEAN``, and says so in its detail string. It fails closed
on every ambiguity: an unreadable unfiltered query, an unreadable merge state,
or any merge state other than CLEAN is not green.

Granularity: ``gh pr checks`` reports individual check runs (jobs), which is the
level at which a verdict is actually decided. The run ROLLUP is not used and must
not be — a run reads ``failure`` when any job fails, including irrelevant ones —
and neither is ``gh run watch``, which prints step-level exit codes for steps
inside jobs that still conclude ``success``. The job-level equivalent for a raw
run is ``gh run view <id> --json jobs -q '.jobs[] | (.conclusion) + " " + (.name)'``.

Poll cadence/ceiling are env-overridable (``DEUS_CI_POLL_INTERVAL`` /
``DEUS_CI_POLL_TIMEOUT`` / ``DEUS_CI_POLL_RETRIES``) — operational tuning knobs
with safe defaults, not feature gates (and ``scripts/ci/`` is flag-lint
excluded). Cross-platform: arg-list subprocess, no shell.

Exit codes: 0 = green (required checks, or the explicit no-required-checks
path); 5 = not green / timeout / unreadable; 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _exit_codes import INTERNAL_ERROR, SUCCESS, USAGE_ERROR  # noqa: E402

# Buckets gh assigns a check. "pending" keeps us polling. Green is a POSITIVE
# allowlist — only pass/skipping count (a skipped required check is not a
# failure). Anything else terminal (fail, cancel, OR an unrecognized bucket from
# gh output drift) is NOT green: this gate must fail closed.
_PENDING = "pending"
_PASSING = frozenset({"pass", "skipping"})

# Distinct from None. None means "gh could not be read"; this means "gh was read
# fine and reported that the branch has no required checks at all" — state (c).
# Two different facts, so two different values; see the module docstring.
NO_REQUIRED_CHECKS = object()

# Prefix stamped on every state-(c) detail string so the path is named in the
# output rather than being inferred from a bare "green".
_NO_REQ = "NO REQUIRED CHECKS CONFIGURED: "

# gh's stderr when a branch has no required checks, e.g.
#   no required checks reported on the 'my-branch' branch
_NO_REQ_STDERR = "no required checks"


def _run(argv: list[str], timeout: int = 60):
    """Arg-list subprocess (no shell). Returns the CompletedProcess, or None on
    timeout/OSError so the caller can treat it as a transient read failure."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _query_checks(pr: int, *, required: bool):
    """Return the parsed checks list for a PR.

    Three possible returns, deliberately distinct:

    * ``list``               — gh was read; these are the checks (possibly ``[]``)
    * ``NO_REQUIRED_CHECKS`` — gh was read and says the branch has NO required
      checks (``--required`` only). A definitive answer, not a failure.
    * ``None``               — gh could not be read. Genuinely ambiguous, and
      the only value the caller retries on.

    The ``NO_REQUIRED_CHECKS`` signature is narrow on purpose: non-zero exit,
    EMPTY stdout, and gh's own wording on stderr. Anything else — partial
    output, an auth error, an unrecognised message — stays ``None`` and burns
    the retry budget, because misreading a real failure as "no required checks"
    would open the gate.
    """
    argv = ["gh", "pr", "checks", str(pr), "--json", "name,state,bucket"]
    if required:
        argv.append("--required")
    proc = _run(argv)
    if proc is None:
        return None
    out = (proc.stdout or "").strip()
    if not out.startswith("["):
        if required and not out and _NO_REQ_STDERR in (proc.stderr or "").lower():
            return NO_REQUIRED_CHECKS
        return None  # error text / unrecognised message, not a JSON array
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _merge_state(pr: int):
    """Return the PR's ``mergeStateStatus`` upper-cased, or None if unreadable.

    None is treated as failure by the caller — this is a merge gate, so an
    unreadable merge state is never waved through.
    """
    proc = _run(["gh", "pr", "view", str(pr), "--json", "mergeStateStatus"])
    if proc is None or proc.returncode != 0:
        return None
    try:
        payload = json.loads((proc.stdout or "").strip() or "{}")
    except json.JSONDecodeError:
        return None
    return str(payload.get("mergeStateStatus") or "").upper() or None


def _resolve_without_required(pr: int):
    """State (c): decide a PR that has NO required checks at all.

    Returns a terminal ``(green, detail)`` tuple, or a ``str`` reason meaning
    "not decidable yet, keep polling". Every detail string is prefixed so the
    caller's output names this path instead of implying a normal verdict.

    Green here needs BOTH halves: every unfiltered check passing AND
    ``mergeStateStatus == CLEAN``. Either half unreadable, or a merge state that
    is anything other than CLEAN, is not green.
    """
    allchecks = _query_checks(pr, required=False)
    if allchecks is None:
        return f"{_NO_REQ}unfiltered `gh pr checks` unreadable"
    if not allchecks:
        return f"{_NO_REQ}no checks registered yet"

    buckets = [_bucket(c) for c in allchecks]
    if _PENDING in buckets:
        pend = [c.get("name") for c in allchecks if _bucket(c) == _PENDING]
        return f"{_NO_REQ}still pending: {pend}"

    not_green = [
        f"{c.get('name')}({_bucket(c) or '?'})"
        for c in allchecks
        if _bucket(c) not in _PASSING
    ]
    if not_green:
        return False, f"{_NO_REQ}checks not green: {not_green}"

    state = _merge_state(pr)
    if state is None:
        return False, f"{_NO_REQ}all {len(allchecks)} checks pass but mergeStateStatus is unreadable — failing closed"
    if state != "CLEAN":
        return False, f"{_NO_REQ}all {len(allchecks)} checks pass but mergeStateStatus={state}, not CLEAN"
    return True, (
        f"{_NO_REQ}fell back to unfiltered `gh pr checks` — "
        f"all {len(allchecks)} checks green and mergeStateStatus=CLEAN"
    )


def _bucket(check: dict) -> str:
    return str(check.get("bucket") or check.get("state") or "").lower()


def wait_for_required_checks(
    pr: int,
    *,
    interval: int = 30,
    timeout: int = 1800,
    retries: int = 5,
) -> tuple[bool, str]:
    """Poll a PR's required checks until they settle. Returns ``(green, detail)``.

    ``green`` is True only when every required check is in a passing/skipping
    bucket — or, when the branch has NO required checks at all, via the explicit
    ``NO REQUIRED CHECKS CONFIGURED`` path, which additionally requires
    ``mergeStateStatus == CLEAN`` and names itself in ``detail``. An unreadable
    gh is never that path: it stays a retried unreadable read and fails closed.
    """
    deadline = time.monotonic() + timeout
    transient = 0
    while True:
        required = _query_checks(pr, required=True)

        if required is None:
            # gh genuinely unreadable — transient error, auth failure, drift.
            if time.monotonic() >= deadline:
                return False, f"timed out after {timeout}s (gh unreadable)"
            transient += 1
            if transient > retries:
                return False, f"gh pr checks unreadable after {retries} retries"
            time.sleep(interval)
            continue
        transient = 0

        if required is NO_REQUIRED_CHECKS or not required:
            # State (c). Reached two ways that mean the same thing: gh errored
            # with its no-required-checks message (private repo, no branch
            # protection — the permanent case), or it returned a readable `[]`.
            # Both are definitive "zero required checks", so neither is retried
            # as an unreadable read.
            outcome = _resolve_without_required(pr)
            if isinstance(outcome, tuple):
                return outcome
            if time.monotonic() >= deadline:
                return False, f"{outcome} — timed out after {timeout}s"
            time.sleep(interval)
            continue

        buckets = [_bucket(c) for c in required]
        if _PENDING in buckets:
            if time.monotonic() >= deadline:
                pend = [c.get("name") for c in required if _bucket(c) == _PENDING]
                return False, f"timed out after {timeout}s; still pending: {pend}"
            time.sleep(interval)
            continue

        # Positive allowlist: green only if EVERY required check passed/skipped.
        # fail/cancel and any unrecognized bucket are surfaced as not-green.
        not_green = [
            f"{c.get('name')}({_bucket(c) or '?'})"
            for c in required
            if _bucket(c) not in _PASSING
        ]
        if not_green:
            return False, f"required checks not green: {not_green}"
        return True, f"all {len(required)} required checks green"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Wait for a PR's required CI checks to settle; authoritative exit."
    )
    ap.add_argument("pr", type=int, help="PR number")
    ap.add_argument(
        "--interval", type=int,
        default=int(os.environ.get("DEUS_CI_POLL_INTERVAL", "30")),
        help="Seconds between polls (env: DEUS_CI_POLL_INTERVAL; default 30).",
    )
    ap.add_argument(
        "--timeout", type=int,
        default=int(os.environ.get("DEUS_CI_POLL_TIMEOUT", "1800")),
        help="Overall ceiling in seconds (env: DEUS_CI_POLL_TIMEOUT; default 1800).",
    )
    ap.add_argument(
        "--retries", type=int,
        default=int(os.environ.get("DEUS_CI_POLL_RETRIES", "5")),
        help="Consecutive gh read failures tolerated (env: DEUS_CI_POLL_RETRIES; default 5).",
    )
    ap.add_argument("--json", action="store_true", help="Emit JSON (agent-native).")
    ap.add_argument("--compact", action="store_true", help="Compact JSON (implies --json).")
    args = ap.parse_args(argv)

    if args.interval < 1 or args.timeout < 1:
        print("wait_for_checks: --interval and --timeout must be >= 1", file=sys.stderr)
        return USAGE_ERROR

    green, detail = wait_for_required_checks(
        args.pr, interval=args.interval, timeout=args.timeout, retries=args.retries
    )
    payload = {"pr": args.pr, "green": green, "detail": detail}
    if args.json or args.compact:
        print(
            json.dumps(payload, separators=(",", ":"))
            if args.compact
            else json.dumps(payload, indent=2)
        )
    else:
        print(f"PR #{args.pr}: {'GREEN' if green else 'NOT GREEN'} — {detail}")
    return SUCCESS if green else INTERNAL_ERROR


if __name__ == "__main__":
    raise SystemExit(main())

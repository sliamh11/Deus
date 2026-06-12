#!/usr/bin/env python3
"""Vetted poller for a PR's REQUIRED CI checks — a safe replacement for
``gh pr checks <pr> --required --watch``.

Why this exists: several call sites piped ``gh pr checks --watch`` through
``tail``/``grep``, which masks gh's non-zero exit and can wave a failing PR
through a merge gate. This helper owns the poll loop, parses the checks JSON
explicitly, and returns an authoritative ``(green: bool, detail: str)`` — never
a masked exit. The zero-registered-checks case (gh can report exit 0 + ``[]``)
is disambiguated against an unfiltered query so "no required checks" is never
mistaken for "all green" (fail-closed).

Poll cadence/ceiling are env-overridable (``DEUS_CI_POLL_INTERVAL`` /
``DEUS_CI_POLL_TIMEOUT`` / ``DEUS_CI_POLL_RETRIES``) — operational tuning knobs
with safe defaults, not feature gates (and ``scripts/ci/`` is flag-lint
excluded). Cross-platform: arg-list subprocess, no shell.

Exit codes: 0 = required checks green; 5 = not green / timeout / unreadable;
2 = usage error.
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


def _run(argv: list[str], timeout: int = 60):
    """Arg-list subprocess (no shell). Returns the CompletedProcess, or None on
    timeout/OSError so the caller can treat it as a transient read failure."""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _query_checks(pr: int, *, required: bool):
    """Return the parsed checks list for a PR, or None if gh produced no JSON.

    None is deliberately ambiguous — a transient gh failure OR a "no checks"
    message — and is disambiguated by the caller via the required/unfiltered
    pair plus the retry budget.
    """
    argv = ["gh", "pr", "checks", str(pr), "--json", "name,state,bucket"]
    if required:
        argv.append("--required")
    proc = _run(argv)
    if proc is None:
        return None
    out = (proc.stdout or "").strip()
    if not out.startswith("["):
        return None  # "no checks" message / error text, not a JSON array
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


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
    bucket. A PR with zero required checks is NEVER reported green (fail-closed):
    if other checks exist, none are *required* (definitive False); if no checks
    exist at all it is treated as not-yet-registered and retried until timeout.
    """
    deadline = time.monotonic() + timeout
    transient = 0
    while True:
        required = _query_checks(pr, required=True)

        if required is None:
            # No JSON from the required query — transient error or "no checks".
            if time.monotonic() >= deadline:
                return False, f"timed out after {timeout}s (gh unreadable)"
            transient += 1
            if transient > retries:
                return False, f"gh pr checks unreadable after {retries} retries"
            time.sleep(interval)
            continue
        transient = 0

        if not required:
            # Zero required checks. Disambiguate against an unfiltered query so
            # an empty `[]` is never mistaken for "all green".
            allchecks = _query_checks(pr, required=False)
            if allchecks:
                return False, "no required checks configured (checks exist but none required)"
            if time.monotonic() >= deadline:
                return False, f"no checks registered after {timeout}s"
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

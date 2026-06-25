#!/usr/bin/env python3
"""merge_train.py — land a sequence of PRs one at a time, safely (LIA-193).

Under strict-up-to-date branch protection every merge puts the next PR behind,
so PRs must land serially: rebase onto main, wait for CI, merge, repeat. This
automates the proven-safe sequence. Per PR, in the given order:

  1. Resolve the PR's branch → its checked-out git worktree.
  2. Rebase that worktree onto origin/main (dropping a stale auto-bump commit),
     then push (handling the pre-push drift hook's bump-and-abort with one
     re-push).
  3. Wait for the REQUIRED CI checks to go green (mirrors branch protection —
     advisory checks like TrueCourse never gate; see LIA-144).
  4. Verify the PR is MERGEABLE.
  5. Admin-merge (squash).

Safety / authorization:
  * DRY-RUN by default — prints the plan, performs NO rebase/push/merge.
  * ``--execute`` performs the rebase/push/CI-wait but STOPS before each merge
    unless ``--approve-admin-merge`` is ALSO given.
  * ``--approve-admin-merge`` authorizes the admin-merges. It carries the SAME
    authority as running ``gh pr merge --admin`` by hand: because this script
    calls ``gh`` via subprocess, the interactive PreToolUse admin-merge gate
    does NOT fire on it — so this flag, not that gate, is the authorization.
    Every merge is recorded in ``.claude/.warden-log``.
  * Stops the train on the first failure (no blind continue); never merges on
    red/failing required CI or a non-MERGEABLE PR.

Agent-native (docs/decisions/printing-press-adoption.md): typed exit codes,
``--json`` / ``--compact``.

Cross-platform: all git/gh calls go through subprocess argument lists (no shell
pipes); paths via pathlib. Tested on macOS; depends on the ``git`` and ``gh``
CLIs being on PATH.
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_io import agent_output, is_agent_context  # noqa: E402
from _exit_codes import INTERNAL_ERROR, SUCCESS, USAGE_ERROR  # noqa: E402

#: The exact subject the pre-push drift hook uses for its auto-bump commit.
AUTOBUMP_SUBJECT = "chore(patterns): auto-bump drifted patterns"
REPO_ROOT = Path(__file__).resolve().parents[1]
#: Generous ceiling for the blocking ``gh pr checks --watch`` (seconds).
_CI_WATCH_TIMEOUT = 1800


def _run(argv: list[str], cwd: Path | None = None, timeout: int | None = None):
    """subprocess.run wrapper — arg-list only (no shell), failures captured."""
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, stdout="", stderr=f"timed out after {timeout}s")
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr=str(exc))


def _tail(result, n: int = 300) -> str:
    text = (result.stderr or result.stdout or "").strip()
    return text[-n:]


def _gh_json(args: list[str], timeout: int = 30):
    result = _run(["gh", *args], timeout=timeout)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _pr_branch(pr: int) -> str | None:
    data = _gh_json(["pr", "view", str(pr), "--json", "headRefName"])
    if isinstance(data, dict):
        return data.get("headRefName")
    return None


def _worktree_for_branch(branch: str) -> Path | None:
    """Map a branch to its checked-out worktree via ``git worktree list --porcelain``."""
    result = _run(["git", "-C", str(REPO_ROOT), "worktree", "list", "--porcelain"])
    if result.returncode != 0:
        return None
    path: str | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
        elif line.startswith("branch ") and path:
            if line[len("branch "):].strip() == f"refs/heads/{branch}":
                return Path(path)
    return None


def _head_is_stale_autobump(wt: Path) -> bool:
    """True iff HEAD is exactly the drift auto-bump commit (subject + patterns-only).

    Both conditions are required so a real commit that happens to share the
    subject prefix but touches other files is never silently dropped.
    """
    subj = _run(["git", "-C", str(wt), "log", "-1", "--format=%s"])
    if subj.returncode != 0 or subj.stdout.strip() != AUTOBUMP_SUBJECT:
        return False
    files = _run(["git", "-C", str(wt), "show", "--name-only", "--format=", "HEAD"])
    if files.returncode != 0:
        return False
    paths = [f.strip() for f in files.stdout.splitlines() if f.strip()]
    return bool(paths) and all(p.startswith("patterns/") and p.endswith(".md") for p in paths)


def _classify_rebase_conflict(wt: Path) -> str:
    """Label a mid-rebase conflict by its unmerged files for the human-facing detail.

    Must be called BEFORE `rebase --abort`, which clears the unmerged set.
    """
    unmerged = _run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=U"])
    files = [f.strip() for f in unmerged.stdout.splitlines() if f.strip()]
    if not files:
        return "non-conflict failure"  # rebase failed without unmerged files
    non_patterns = [f for f in files if not (f.startswith("patterns/") and f.endswith(".md"))]
    if not non_patterns:
        return "patterns-only conflict — a real two-sided edit to drift patterns; resolve manually"
    return f"mixed conflict — non-patterns files need manual resolution: {', '.join(non_patterns)}"


def _rebase_and_push(wt: Path, branch: str) -> tuple[bool, str]:
    """Drop a stale bump, rebase onto origin/main, push (re-pushing once if the
    pre-push drift hook commits a fresh bump and aborts)."""
    _run(["git", "-C", str(wt), "fetch", "origin"], timeout=120)
    if _head_is_stale_autobump(wt):
        _run(["git", "-C", str(wt), "reset", "--hard", "HEAD~1"])
    rebase = _run(["git", "-C", str(wt), "rebase", "origin/main"], timeout=120)
    if rebase.returncode != 0:
        # Classify the conflict BEFORE aborting (abort clears the unmerged set), so the
        # human knows whether it's a drift-bump collision or a substantive merge. We STOP
        # either way — auto-resolving a surviving patterns conflict would silently drop a
        # real two-sided pattern edit (the bump itself is already dropped above).
        kind = _classify_rebase_conflict(wt)
        _run(["git", "-C", str(wt), "rebase", "--abort"])
        return False, f"rebase onto origin/main failed ({kind}): {_tail(rebase)}"
    refspec = f"{branch}:{branch}"
    push_argv = ["git", "-C", str(wt), "push", "--force-with-lease", "origin", refspec]
    first = _run(push_argv, timeout=180)
    if first.returncode == 0:
        return True, "pushed"
    # The pre-push drift hook commits a patterns bump and aborts; re-push once.
    second = _run(push_argv, timeout=180)
    if second.returncode == 0:
        return True, "pushed (after drift-bump re-push)"
    return False, f"push failed: {_tail(second)}"


def _wait_required_ci(pr: int, interval: int) -> tuple[bool, str]:
    """Poll required checks via the vetted ci.wait_for_checks helper.

    Replaces the old blocking ``gh pr checks --required --watch`` (whose exit
    code several call sites masked by piping through tail/grep). The helper owns
    the poll loop, parses the checks JSON authoritatively, and fail-closes on
    the zero-required-checks ambiguity — returning a definite (green, detail).
    """
    from ci.wait_for_checks import wait_for_required_checks

    return wait_for_required_checks(pr, interval=interval, timeout=_CI_WATCH_TIMEOUT)


def _verify_mergeable(pr: int, *, retries: int = 6, delay: int = 3) -> tuple[bool, str]:
    """Confirm the PR is MERGEABLE, polling through GitHub's async recompute.

    After a push GitHub recomputes `mergeable` asynchronously, reporting UNKNOWN
    (computed independently of, and often slower than, the CI checks) until it
    settles. Both UNKNOWN and a transient `gh` read failure are treated as
    retryable — they share the same just-pushed window; only a definitive
    CONFLICTING or non-OPEN state fails fast.

    Defaults (6 × 3s = 18s) give generous headroom over GitHub's typical
    few-second post-push settle without stalling the train; `retries`/`delay`
    are injectable so a caller can tune or disable the wait.
    """
    last_detail = "could not read PR state"
    for attempt in range(retries):
        data = _gh_json(["pr", "view", str(pr), "--json", "mergeable,reviewDecision,state"])
        if isinstance(data, dict):
            if data.get("state") != "OPEN":
                return False, f"PR is {data.get('state')}, not OPEN"
            mergeable = str(data.get("mergeable"))
            if mergeable == "MERGEABLE":
                return True, f"MERGEABLE (review={data.get('reviewDecision')})"
            if mergeable == "CONFLICTING":
                return False, "mergeable=CONFLICTING — rebase/resolve before merging"
            last_detail = f"mergeable={mergeable}"  # UNKNOWN → still computing
        else:
            last_detail = "could not read PR state (transient gh failure)"
        if attempt < retries - 1:
            time.sleep(delay)
    return False, f"{last_detail} after {retries} polls — GitHub did not settle"


def _audit(repo_root: Path, message: str) -> bool:
    """Append an audit line to .claude/.warden-log. Returns False on failure.

    This record is the authorization trail for a gate-bypassing admin-merge, so
    the caller treats a failed write as a hard stop — better to refuse the merge
    than to land one with no durable record.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} | {'merge-train':<15} | {'MERGE':<7} | {message}\n"
    log = repo_root / ".claude" / ".warden-log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return True
    except OSError:
        return False


def _admin_merge(pr: int) -> tuple[bool, str]:
    result = _run(["gh", "pr", "merge", str(pr), "--squash", "--admin"], timeout=120)
    if result.returncode == 0:
        return True, "merged (squash, admin)"
    return False, f"merge failed: {_tail(result)}"


def run_train(
    prs: list[int], *, execute: bool, approve: bool, repo_root: Path, ci_interval: int = 30
) -> list[dict]:
    """Run the train. Returns one result record per attempted PR; stops on first failure."""
    results: list[dict] = []
    for pr in prs:
        rec: dict = {"pr": pr, "branch": None, "phase": "resolve", "ok": True, "merged": False, "detail": ""}

        branch = _pr_branch(pr)
        if not branch:
            rec.update(ok=False, detail=f"could not resolve a branch for PR #{pr}")
            results.append(rec)
            break
        rec["branch"] = branch

        wt = _worktree_for_branch(branch)
        if wt is None:
            rec.update(
                ok=False,
                detail=f"no local worktree checked out for '{branch}' — "
                       f"`git worktree add` it before running merge-train",
            )
            results.append(rec)
            break

        if not execute:
            tail = "admin-merge" if approve else "STOP before merge (no --approve-admin-merge)"
            rec.update(phase="dry-run", detail=f"would rebase {wt} → wait required CI → verify → {tail}")
            results.append(rec)
            continue

        rec["phase"] = "rebase"
        ok, detail = _rebase_and_push(wt, branch)
        if not ok:
            rec.update(ok=False, detail=detail)
            results.append(rec)
            break

        rec["phase"] = "ci"
        ok, detail = _wait_required_ci(pr, ci_interval)
        if not ok:
            rec.update(ok=False, detail=detail)
            results.append(rec)
            break

        rec["phase"] = "verify"
        ok, detail = _verify_mergeable(pr)
        if not ok:
            rec.update(ok=False, detail=detail)
            results.append(rec)
            break

        if not approve:
            rec.update(
                phase="stop-before-merge",
                detail="rebased + required CI green + MERGEABLE; STOPPED before merge "
                       "(pass --approve-admin-merge to land it)",
            )
            results.append(rec)
            continue

        rec["phase"] = "merge"
        # The audit record is the authorization trail for this gate-bypassing
        # merge — if it can't be written, refuse to merge (fail closed).
        if not _audit(repo_root, f"PR #{pr} ({branch}): admin-merge squash — required CI green, MERGEABLE"):
            rec.update(
                ok=False,
                detail="could not write the .claude/.warden-log audit record; refusing to "
                       "admin-merge (--approve-admin-merge requires a durable audit trail)",
            )
            results.append(rec)
            break
        ok, detail = _admin_merge(pr)
        rec.update(ok=ok, merged=ok, detail=detail)
        results.append(rec)
        if not ok:
            break

    return results


def _print_human(payload: dict) -> None:
    print(f"merge-train [{payload['mode']}]  approve_admin_merge={payload['approve_admin_merge']}")
    for rec in payload["results"]:
        flag = "✓" if rec["ok"] else "✗"
        merged = " (MERGED)" if rec.get("merged") else ""
        print(f"  {flag} #{rec['pr']} [{rec['phase']}]{merged} — {rec['detail']}")
    if payload["merged"]:
        print(f"merged: {', '.join('#' + str(p) for p in payload['merged'])}")
    if payload["stopped_at"] is not None:
        print(f"stopped at #{payload['stopped_at']} — fix and re-run from there.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Land a sequence of PRs one at a time: rebase → required CI → admin-merge."
    )
    parser.add_argument("prs", nargs="+", type=int, help="PR numbers, in the order to merge")
    parser.add_argument(
        "--execute", action="store_true",
        help="Perform rebase/push/CI-wait. Default is dry-run (no mutations).",
    )
    parser.add_argument(
        "--approve-admin-merge", dest="approve", action="store_true",
        help="Authorize the admin-merges. Carries the SAME authority as a direct "
             "`gh pr merge --admin` (bypasses the interactive gate). Requires --execute. "
             "Every merge is recorded in .claude/.warden-log.",
    )
    parser.add_argument("--ci-interval", type=int, default=30, help="Seconds between CI polls (default 30).")
    parser.add_argument("--json", action="store_true", help="Emit JSON (agent-native).")
    parser.add_argument("--compact", action="store_true", help="Compact JSON output.")
    args = parser.parse_args(argv)

    if args.approve and not args.execute:
        print("merge-train: --approve-admin-merge requires --execute", file=sys.stderr)
        return USAGE_ERROR
    if args.ci_interval < 5:
        print("merge-train: --ci-interval must be >= 5 seconds", file=sys.stderr)
        return USAGE_ERROR

    results = run_train(
        args.prs,
        execute=args.execute,
        approve=args.approve,
        repo_root=REPO_ROOT,
        ci_interval=args.ci_interval,
    )

    payload = {
        "mode": "execute" if args.execute else "dry-run",
        "approve_admin_merge": args.approve,
        "results": results,
        "merged": [r["pr"] for r in results if r.get("merged")],
        "stopped_at": next((r["pr"] for r in results if not r.get("ok")), None),
    }

    use_json = args.json or is_agent_context()
    out = agent_output(payload, use_json=use_json, compact=args.compact, long_fields=("detail",))
    if out is not None:
        print(out)
    else:
        _print_human(payload)

    return INTERNAL_ERROR if any(not r.get("ok") for r in results) else SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())

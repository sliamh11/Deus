"""GitHub Actions CI-status polling for the admin-merge gate (LIA-306).

Classifies a PR's check state by shelling out to ``gh pr checks`` and maps it to
the ``_CI_STATUS_*`` enum the admin-merge gate decides on (it mirrors branch
protection — only branch-protection-*required* checks may block a merge). Fails
closed: any unverifiable status blocks rather than allowing an unreviewed merge.

Pure leaf (like ``globs`` / ``command_parse``): depends only on stdlib
(``subprocess`` + ``json``) and its own module-level constants, with no shared
entry-module state — so no ``bind_entry`` injection seam is needed. ``_check_ci_status``
IS monkeypatched by tests, but its callers (``approve_admin_merge`` /
``run_admin_merge_gate``) live in the entry module and reference the re-exported
name, so ``monkeypatch.setattr(hooks, "_check_ci_status", ...)`` rebinds exactly
what those callers see. Tests also read ``hooks._CI_STATUS_*``; the entry
re-exports every constant, so those reads resolve.
"""

from __future__ import annotations

import json
import subprocess

_CI_STATUS_GREEN = "green"
_CI_STATUS_RED = "red"
_CI_STATUS_PENDING = "pending"
_CI_STATUS_NO_CHECKS = "no-checks"
# Checks exist on the PR but none are branch-protection-required — an ambiguous
# state we fail closed on rather than silently allow an unverified admin-merge.
_CI_STATUS_NO_REQUIRED = "no-required"
_CI_STATUS_ERROR = "error"

# Bucket values returned by ``gh pr checks --json bucket``
_BUCKET_PASS = frozenset({"pass", "skipping"})
_BUCKET_PENDING = frozenset({"pending"})
_BUCKET_FAIL = frozenset({"fail", "cancel"})


def _query_gh_checks(
    pr_ref: str, *, required_only: bool, timeout: int = 3
) -> tuple[str, str, int]:
    """Run ``gh pr checks`` once and classify the result.

    Returns ``(status, message, num_checks)`` where status is one of the
    ``_CI_STATUS_*`` constants and num_checks is how many checks were returned.
    When *required_only* is set, the query is scoped with ``--required`` so the
    gate sees only branch-protection-required checks. Failure to query defaults
    to ``_CI_STATUS_ERROR`` so the caller blocks rather than falls open.
    """
    argv = ["gh", "pr", "checks", pr_ref, "--json", "bucket,name"]
    if required_only:
        argv.append("--required")
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return _CI_STATUS_ERROR, "gh CLI not found; cannot verify CI status", 0
    except subprocess.TimeoutExpired:
        return _CI_STATUS_ERROR, f"gh pr checks timed out after {timeout}s", 0
    except OSError as exc:
        return _CI_STATUS_ERROR, f"gh pr checks failed: {exc}", 0

    if result.returncode not in (0, 1, 8):
        # Exit code 1 = some checks failed (still parseable).
        # Exit code 8 = checks pending (still parseable).
        # Other codes indicate auth / network errors.
        stderr_snippet = result.stderr.strip()[:200]
        return (
            _CI_STATUS_ERROR,
            f"gh pr checks exited {result.returncode}: {stderr_snippet}",
            0,
        )

    raw = result.stdout.strip()
    if not raw:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR", 0

    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return _CI_STATUS_ERROR, "gh pr checks returned unparseable output", 0

    if not isinstance(checks, list):
        return _CI_STATUS_ERROR, "gh pr checks returned unexpected JSON shape", 0

    if not checks:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR", 0

    n = len(checks)
    buckets = {str(c.get("bucket", "")) for c in checks if isinstance(c, dict)}
    failed = [
        str(c.get("name", "?"))
        for c in checks
        if isinstance(c, dict) and str(c.get("bucket", "")) in _BUCKET_FAIL
    ]
    pending = [
        str(c.get("name", "?"))
        for c in checks
        if isinstance(c, dict) and str(c.get("bucket", "")) in _BUCKET_PENDING
    ]

    if failed:
        return _CI_STATUS_RED, f"failing checks: {', '.join(failed[:5])}", n
    if pending:
        return _CI_STATUS_PENDING, f"pending checks: {', '.join(pending[:5])}", n
    if buckets <= _BUCKET_PASS:
        return _CI_STATUS_GREEN, "all checks passed", n

    unknown = buckets - _BUCKET_PASS - _BUCKET_PENDING - _BUCKET_FAIL
    return _CI_STATUS_ERROR, f"unknown check buckets: {', '.join(sorted(unknown))}", n


def _check_ci_status(pr_ref: str, timeout: int = 3) -> tuple[str, str]:
    """Classify CI for *pr_ref*, scoped to branch-protection-required checks.

    The admin-merge gate must mirror branch protection — only checks the repo
    actually marks required (e.g. ``ci``) may block a merge, never
    advisory bots (TrueCourse, the platform test matrix, CodeQL) the repo
    deliberately left non-required. Applies to every caller of this function
    (the one-shot approve CLI, the PreToolUse hook, and merge_train).

    Falls closed: an unverifiable status — or a PR that has checks but none
    required — blocks rather than allowing an unreviewed admin-merge.
    """
    status, message, _ = _query_gh_checks(pr_ref, required_only=True, timeout=timeout)
    if status != _CI_STATUS_NO_CHECKS:
        return status, message

    # No REQUIRED checks reported. Disambiguate against the unfiltered set:
    # genuinely zero checks → allowed through (unchanged behaviour); checks
    # present but none required → ambiguous, fail closed.
    all_status, all_message, all_n = _query_gh_checks(
        pr_ref, required_only=False, timeout=timeout
    )
    if all_status == _CI_STATUS_ERROR:
        return all_status, all_message
    if all_n == 0:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR"
    # Thread the unfiltered status through so the operator sees WHAT is
    # outstanding (e.g. a failing advisory check), not just the ambiguity.
    return (
        _CI_STATUS_NO_REQUIRED,
        f"{all_n} check(s) present but none are branch-protection-required "
        f"(unfiltered: {all_status} — {all_message})",
    )


def _ci_block_reason(pr_ref: str, status: str, detail: str) -> str | None:
    """Return a block reason string if CI is not green, else ``None``."""
    if status == _CI_STATUS_GREEN:
        return None
    if status == _CI_STATUS_NO_CHECKS:
        return None
    if status == _CI_STATUS_RED:
        return (
            f"[admin-merge-gate] CI is red — autonomy grant is conditional on green. "
            f"Run `gh pr checks {pr_ref}` first.\n\n"
            f"Detail: {detail}"
        )
    if status == _CI_STATUS_PENDING:
        return (
            f"[admin-merge-gate] CI is pending — autonomy grant is conditional on green. "
            f"Run `gh pr checks {pr_ref}` first.\n\n"
            f"Detail: {detail}"
        )
    if status == _CI_STATUS_NO_REQUIRED:
        return (
            f"[admin-merge-gate] Branch protection reports no required checks for "
            f"{pr_ref}, yet the PR has checks — refusing admin-merge (fail-closed). "
            f"Inspect with `gh api repos/<owner>/<repo>/branches/main/protection` and "
            f"confirm the required-check names before merging.\n\n"
            f"Detail: {detail}"
        )
    # _CI_STATUS_ERROR — fail closed
    return (
        f"[admin-merge-gate] CI status could not be verified — blocking as a precaution. "
        f"Run `gh pr checks {pr_ref}` manually to confirm green, then retry.\n\n"
        f"Detail: {detail}"
    )

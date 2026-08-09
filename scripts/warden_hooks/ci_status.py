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

LIA-513 (2026-08-01 cross-repo incident, investigated — no code change needed
beyond the existing ``repo`` param above): two follow-up fixes were considered
and deliberately NOT implemented, to save a future reader from re-deriving why.
(a) Explicitly deriving ``--repo`` from a URL-shaped ``pr_ref`` — unnecessary:
``gh pr checks``/``gh pr view`` already resolve a fully-qualified PR URL
natively, independent of ``--repo``/cwd, which is exactly why the documented
full-URL workaround fixed the live incident with zero code changes. (b)
Passing an explicit ``cwd=`` to the internal ``subprocess.run`` calls here —
also doesn't help: the admin-merge gate only fires when the event's cwd is
already inside the *launch* repo's own worktree tree (see
``_worktree_for_cwd`` in ``codex_warden_hooks.py``), so for a genuinely
cross-repo target, no cwd value the gate could ever see resolves to the
correct repo. A bare PR number/branch with no explicit ``--repo`` flag,
targeting a different repo than the session's own worktree, is undecidable
from any information available to the gate; the documented
fully-qualified-URL workaround is the correct permanent mitigation for it,
not a bug to fix here.

One residual gap the URL workaround does NOT close:
``_branch_protection_plan_limited``'s ``gh api repos/{owner}/{repo}/...``
probe takes no ``pr_ref`` at all (``gh api`` has no ``--repo`` flag — repo
scoping goes directly in the endpoint path, see that function's own
docstring), so a URL-shaped ``pr_ref`` never reaches it. With ``repo=None``
it falls back to ``gh``'s ``{owner}/{repo}`` placeholder, resolved from the
process cwd's git remote — i.e. the launch repo, not the actual target.
Accepted as-is rather than fixed: the only consequence is that a plan-limited
*launch* repo can steer a cross-repo check onto the relaxed plan-limited
fallback path (which still fail-closes on any non-green check) instead of
the stricter no-required-checks path — never a silent pass-through of a red
target-repo check, since that fallback still demands the check set be green.
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


def _fetch_gh_checks_raw(
    pr_ref: str, *, required_only: bool, timeout: int = 3, repo: str | None = None
) -> tuple[str | None, list[dict] | None, str]:
    """Run ``gh pr checks`` once and return the raw parsed checks, unclassified.

    Returns ``(early_status, checks, message)``. ``early_status`` is ``None``
    and ``checks`` is a non-empty list when there's something to classify.
    Otherwise ``early_status`` is one of ``_CI_STATUS_ERROR``/
    ``_CI_STATUS_NO_CHECKS`` (checks is ``None``) — a terminal result reached
    before classification is possible.

    *repo* scopes the query to an explicit ``OWNER/REPO`` (via ``gh``'s own
    ``--repo`` flag) instead of letting ``gh`` resolve one from the current
    working directory's git remote — needed when the gated command already
    names an explicit repo different from the caller's own cwd (e.g. a
    worktree tree whose ambient remote belongs to a different repo). ``None``
    (the default) keeps today's cwd-based resolution, so the constructed argv
    is byte-identical when no explicit repo is given.
    """
    argv = ["gh", "pr", "checks", pr_ref, "--json", "bucket,name"]
    if required_only:
        argv.append("--required")
    if repo:
        argv.extend(["--repo", repo])
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
        return _CI_STATUS_ERROR, None, "gh CLI not found; cannot verify CI status"
    except subprocess.TimeoutExpired:
        return _CI_STATUS_ERROR, None, f"gh pr checks timed out after {timeout}s"
    except OSError as exc:
        return _CI_STATUS_ERROR, None, f"gh pr checks failed: {exc}"

    if result.returncode not in (0, 1, 8):
        # Exit code 1 = some checks failed (still parseable).
        # Exit code 8 = checks pending (still parseable).
        # Other codes indicate auth / network errors.
        stderr_snippet = result.stderr.strip()[:200]
        return (
            _CI_STATUS_ERROR,
            None,
            f"gh pr checks exited {result.returncode}: {stderr_snippet}",
        )

    raw = result.stdout.strip()
    if not raw:
        return _CI_STATUS_NO_CHECKS, None, "no checks found for this PR"

    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return _CI_STATUS_ERROR, None, "gh pr checks returned unparseable output"

    if not isinstance(checks, list):
        return _CI_STATUS_ERROR, None, "gh pr checks returned unexpected JSON shape"

    if not checks:
        return _CI_STATUS_NO_CHECKS, None, "no checks found for this PR"

    return None, checks, ""


def _classify_checks(
    checks: list[dict], exclude_names: frozenset[str] = frozenset()
) -> tuple[str, str, int]:
    """Classify an already-fetched checks list into a ``_CI_STATUS_*`` result.

    *exclude_names*: check names to drop before classification (e.g. checks
    known to be advisory-only across this codebase's CI workflows, never
    branch-protection-required). Empty (the default) is a no-op filter —
    byte-identical to classifying the unfiltered list.

    If filtering removes every check, re-checks emptiness explicitly and
    returns ``_CI_STATUS_NO_CHECKS`` — without this, ``set() <= _BUCKET_PASS``
    is vacuously true on an empty bucket set, which would otherwise produce a
    false ``_CI_STATUS_GREEN`` from an empty check list.
    """
    if exclude_names:
        checks = [
            c
            for c in checks
            if isinstance(c, dict) and str(c.get("name", "")) not in exclude_names
        ]
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


def _query_gh_checks(
    pr_ref: str, *, required_only: bool, timeout: int = 3, repo: str | None = None
) -> tuple[str, str, int]:
    """Run ``gh pr checks`` once and classify the result.

    Returns ``(status, message, num_checks)``. Thin wrapper over
    ``_fetch_gh_checks_raw`` + ``_classify_checks`` — external signature and
    behavior unchanged from before that split.
    """
    early_status, checks, message = _fetch_gh_checks_raw(
        pr_ref, required_only=required_only, timeout=timeout, repo=repo
    )
    if early_status is not None or checks is None:
        return early_status or _CI_STATUS_ERROR, message, 0
    return _classify_checks(checks)


# Checks known to be advisory-only across this codebase's CI workflows (never
# branch-protection-required, confirmed via sliamh11/Deus's real
# required_status_checks.contexts and documented at merge_train.py's "advisory
# checks like TrueCourse never gate" comment, LIA-144). GitHub's own bucket
# classification for a self-cancelled run of one of these is inconsistent
# (pass vs cancel for what appears to be the same cancellation reason), so
# name-based exclusion is more reliable than trusting the bucket when
# computing the plan-limited fallback's "must be green" set. Staleness here is
# fail-safe: a renamed check simply drops out of this set and reverts to
# blocking, never to falsely allowing.
_KNOWN_ADVISORY_CHECK_NAMES = frozenset({"TrueCourse --diff vs main"})

_PLAN_LIMITATION_MESSAGE = "Upgrade to GitHub Pro or make this repository public"


def _branch_protection_plan_limited(
    repo: str | None, branch: str, timeout: int = 3
) -> bool:
    """Detect the one specific, narrow case where a private repo's plan tier
    makes branch-protection required-checks structurally unknowable — not
    "unconfigured", but genuinely inaccessible via the API regardless of what
    the maintainer set up.

    ``gh api`` has no ``--repo`` flag (unlike ``gh pr checks``); repo scoping
    goes directly in the endpoint path. ``repo=None`` uses ``gh``'s own
    ``{owner}/{repo}`` placeholder syntax (resolved from cwd's git remote);
    an explicit ``repo`` is substituted into the path directly.

    Returns ``True`` only when the response is *exactly* GitHub's documented
    plan-limitation shape: JSON on stdout with a ``message`` field containing
    ``_PLAN_LIMITATION_MESSAGE``. Any other outcome — a real 200 response, a
    404 (branch protection genuinely unconfigured on a full-featured repo), a
    403 with a *different* message (e.g. an auth/permission failure, never to
    be conflated with plan-limitation), unparseable output, ``gh`` missing, or
    a timeout — returns ``False``, so the caller's existing fail-closed path
    is untouched.
    """
    repo_segment = repo if repo else "{owner}/{repo}"
    argv = ["gh", "api", f"repos/{repo_segment}/branches/{branch}/protection"]
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        # FileNotFoundError is an OSError subclass; no separate branch needed.
        return False

    try:
        payload = json.loads(result.stdout.strip())
    except ValueError:
        # json.JSONDecodeError is a ValueError subclass; no separate branch needed.
        return False

    if not isinstance(payload, dict):
        return False

    return _PLAN_LIMITATION_MESSAGE in str(payload.get("message", ""))


def _check_ci_status(
    pr_ref: str, timeout: int = 3, repo: str | None = None
) -> tuple[str, str]:
    """Classify CI for *pr_ref*, scoped to branch-protection-required checks.

    The admin-merge gate must mirror branch protection — only checks the repo
    actually marks required (e.g. ``ci``) may block a merge, never
    advisory bots (TrueCourse, the platform test matrix, CodeQL) the repo
    deliberately left non-required. Applies to every caller of this function
    (the one-shot approve CLI, the PreToolUse hook, and merge_train).

    Falls closed: an unverifiable status — or a PR that has checks but none
    required — blocks rather than allowing an unreviewed admin-merge.

    *repo*: see ``_query_gh_checks`` — scopes both queries below to an
    explicit ``OWNER/REPO`` instead of ``gh``'s cwd-based resolution.
    """
    status, message, _ = _query_gh_checks(
        pr_ref, required_only=True, timeout=timeout, repo=repo
    )
    if status != _CI_STATUS_NO_CHECKS:
        return status, message

    # No REQUIRED checks reported. Disambiguate against the unfiltered set:
    # genuinely zero checks → allowed through (unchanged behaviour); checks
    # present but none required → ambiguous, fail closed. Fetch once here so
    # both the unfiltered classification below and the plan-limited fallback's
    # advisory-excluded classification see the identical snapshot — a second
    # live `gh` call could observe a different bucket for the same check.
    early_status, checks, early_message = _fetch_gh_checks_raw(
        pr_ref, required_only=False, timeout=timeout, repo=repo
    )
    if early_status == _CI_STATUS_ERROR:
        return early_status, early_message
    if early_status == _CI_STATUS_NO_CHECKS or not checks:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR"

    all_status, all_message, all_n = _classify_checks(checks)
    if all_status == _CI_STATUS_ERROR:
        return all_status, all_message

    # Structurally unknowable required-checks (private repo, no GitHub Pro):
    # branch protection cannot exist on this repo at all, so the ambiguity
    # this branch normally fails closed on doesn't apply. Fall back to the
    # unfiltered result — strictly more conservative than real required-checks
    # (demands every check green, not just a subset) — except for checks
    # known to be advisory-only, whose inconsistent cancel/pass bucketing
    # would otherwise cause false blocks unrelated to real CI health.
    if _branch_protection_plan_limited(repo, "main", timeout):
        filtered_status, filtered_message, filtered_n = _classify_checks(
            checks, _KNOWN_ADVISORY_CHECK_NAMES
        )
        return (
            filtered_status,
            f"{filtered_message} (plan-limited fallback: branch protection "
            f"unavailable on this repo's plan tier — used {filtered_n} "
            f"check(s), excluding known-advisory checks, instead of "
            f"branch-protection-required ones)",
        )

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

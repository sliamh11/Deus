#!/usr/bin/env python3
"""CI-side re-query of ``deus.wardens.decision`` for the ``attestation-verify`` git-level backstop
(LIA-536, implementing ``docs/decisions/git-level-hard-backstop-design.md``).

Runs on the self-hosted Actions runner registered for this repo, calling the same OPA endpoint
(``scripts/warden_policy/opa_client.py``'s ``query_decision``) Hermes's own commit gate already
uses, via the Phase-4 ``attestation.verify`` Rego block (LIA-530,
``scripts/warden_policy/policy/guardrails.rego`` lines ~150-228). This is the "actual git object
observation point" the design doc's §1 names as the missing piece every earlier enforcement point
lacked -- unlike a PreToolUse hook, it runs server-side against the real merge candidate, not the
driving agent's own command text.

Two identifiers this script resolves DIFFERENTLY from every other caller in this package, and why
(``docs/decisions/git-level-hard-backstop-design.md`` and this ticket's own plan cover the full
rationale -- summarized here for anyone reading only this file):

- ``repo_id``: resolved from the canonical repo path named by the ``DEUS_CANONICAL_REPO``
  environment variable (set at self-hosted-runner registration time, operator-controlled, never
  PR- or workflow-file-influenceable), NOT from ``GITHUB_WORKSPACE`` (the Actions runner's own
  ephemeral checkout under ``_work/``, which has a different git-common-dir on every run and would
  make ``resolve_repo_id()`` compute a different hash every time -- a silent-forever-deny bug, not
  a security issue, but one indistinguishable from "OPA is down"). ``DEUS_CANONICAL_REPO`` must
  point at the SAME canonical repo every local Hermes/Claude-Code session already writes
  attestations against, or ``attestation-verify`` will never find a matching record.
- ``subject_key``: resolved from the PR's head commit tree, but the head commit is only ever
  ``git fetch``ed into the trusted base-branch checkout (never checked out as the working tree,
  never executed) -- see ``resolve_subject_key_for_commit`` below.

Fail-closed (LIA-515's Fog item, decided here): every non-``ok`` ``DecisionResult``
(``opa_client.DecisionResult(ok=False, ...)``) AND every ``ok=True, allow=False`` result both map
to conclusion ``"failure"``. There is no code path in this script that can return ``"success"``
without a verified ``ok=True, allow=True`` decision. ``evaluate()``'s outer catch-all makes this
hold even for an exception a future edit forgets to convert explicitly.

**Disclosed, NOT closed by this file**: this script's own OPA query is only as trustworthy as the
Check Run the calling workflow posts against it -- any OTHER same-repo workflow granted
``checks: write`` could forge a same-named, same-app-id ``attestation-verify`` Check Run directly
via the Checks API, bypassing this script and the actor guard entirely. See
``.github/workflows/attestation-verify.yml``'s own header comment and design decision #9 in
LIA-536's plan for the full mechanism and why it's deferred to LIA-531 (credential separation)
rather than attempted here.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from warden_policy.attestation_store import AttestationStore  # noqa: E402
from warden_policy.cc_attestations import CC_DOCUMENT_KEY, CC_LEDGER_PATH  # noqa: E402
from warden_policy.opa_client import DecisionResult, query_decision  # noqa: E402

LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"
OPA_URL = os.environ.get("DEUS_OPA_URL", "http://127.0.0.1:8181")
#: More generous than hermes_warden_gate.py's tight 0.75s/2.5s hook-latency budget -- this is a
#: CI job step, not a synchronous pre-tool-call hook, and per design decision #3 this script never
#: holds a ledger lock across this call, so a longer timeout here cannot stall a local session's
#: own commit gate the way it would if the lock-hold-across-query shape had been copied.
OPA_TIMEOUT_SECONDS = 10.0
#: One bounded retry on a detected generation race, never an unbounded loop.
MAX_GENERATION_RACE_RETRIES = 1

GATE = "code-review"
OPERATION = "attestation.verify"
CONTRACT_VERSION = 1


class RepoIdentityError(Exception):
    """Raised when repo_id or subject_key cannot be resolved -- always fail-closed."""


@dataclass(frozen=True)
class VerifyResult:
    conclusion: str  # "success" or "failure" -- never anything else
    title: str
    summary: str


def _run_git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def resolve_repo_id_from_env() -> str:
    """Resolve repo_id from ``DEUS_CANONICAL_REPO``, never from the Actions runner's own checkout.

    Deliberately does NOT call ``git_subject.resolve_repo_id`` against ``GITHUB_WORKSPACE`` --
    see this module's docstring. Fails closed via ``RepoIdentityError`` (never a bare exception
    escaping to a stack trace with no diagnosis) if the env var is unset or doesn't resolve to a
    real git repo, logging the missing/invalid var name -- not just the resulting hash -- so this
    failure mode is diagnosable rather than reading as "OPA is down."
    """
    canonical = os.environ.get("DEUS_CANONICAL_REPO")  # LIA-536
    if not canonical:
        raise RepoIdentityError(
            "DEUS_CANONICAL_REPO is not set -- cannot resolve repo_id. This must be set at "
            "self-hosted-runner registration time to the canonical repo path every local "
            "Hermes/Claude-Code session writes attestations against."
        )
    canonical_path = Path(canonical)
    if not canonical_path.is_dir():
        raise RepoIdentityError(
            f"DEUS_CANONICAL_REPO={canonical!r} does not exist or is not a directory."
        )
    from warden_policy.git_subject import GitSubjectError, resolve_repo_id

    try:
        return resolve_repo_id(canonical_path)
    except GitSubjectError as exc:
        raise RepoIdentityError(
            f"DEUS_CANONICAL_REPO={canonical!r} did not resolve to a valid git repo: {exc}"
        ) from exc


def resolve_subject_key_for_commit(repo_path: Path, head_sha: str) -> str:
    """Resolve subject_key for an already-fetched (never checked out) commit.

    Distinct from ``git_subject.resolve_subject_key``, which computes ``write-tree`` of the
    *current index* (correct for Hermes's pre-commit-hook context, where no commit exists yet).
    In CI the commit already exists -- fetched into ``repo_path``'s object database by the
    workflow's own ``git fetch`` step (never checked out as the working tree; see this module's
    docstring and design decision #2 in the LIA-536 plan) -- so this resolves the tree of that
    already-existing commit directly. Content-addressed and clone-independent: byte-identical to
    what ``resolve_subject_key`` would have produced locally at commit/SHIP time for the same tree.
    """
    try:
        object_format = _run_git("rev-parse", "--show-object-format", cwd=repo_path) or "sha1"
        tree_oid = _run_git("rev-parse", f"{head_sha}^{{tree}}", cwd=repo_path)
    except subprocess.CalledProcessError as exc:
        # An unresolvable/unfetched head_sha raises CalledProcessError, which is NOT a
        # RepoIdentityError -- converted explicitly so it can't escape evaluate()'s narrow
        # except clause, contradicting design decision #4's "script exception -> failure,
        # never neutral/skipped/pass" contract.
        raise RepoIdentityError(
            f"git rev-parse failed while resolving subject_key for {head_sha!r}: {exc.stderr!r}"
        ) from exc
    if not tree_oid:
        raise RepoIdentityError(f"could not resolve tree for fetched commit {head_sha!r}")
    return f"git-tree:{object_format}:{tree_oid}"


def _read_generations() -> tuple[int, int]:
    """Read (expected_generation, expected_cc_generation), each under its own brief shared lock,
    released immediately -- never both held at once. See design decision #3 / this script's
    entry in attestation_store.py's own module docstring for the full read-verify-recheck
    rationale."""
    hermes_store = AttestationStore(LEDGER_PATH)
    cc_store = AttestationStore(CC_LEDGER_PATH, document_key=CC_DOCUMENT_KEY)
    hermes_doc = hermes_store.read_locked()
    expected_generation = hermes_doc[hermes_store.document_key]["generation"]
    cc_doc = cc_store.read_locked()
    expected_cc_generation = cc_doc[cc_store.document_key]["generation"]
    return expected_generation, expected_cc_generation


def query_decision_with_race_check(
    repo_id: str, subject_key: str, opa_url: str = OPA_URL, timeout_seconds: float = OPA_TIMEOUT_SECONDS,
) -> DecisionResult:
    """Read-verify-recheck: read both ledger generations (locks released immediately), query OPA
    with NO lock held, then re-read both generations and confirm neither moved. One bounded retry
    on a detected race; a second consecutive race fails closed.

    This achieves the same never-trust-a-stale-snapshot guarantee ``hermes_warden_gate.py``'s
    hold-across-query pattern provides, without ever holding a ledger lock across the (here,
    deliberately more generous) OPA network call -- see design decision #3 in the LIA-536 plan for
    the full rationale (holding two locks across a longer CI-scale timeout would stall every local
    session's own commit-gate writes, which need the same locks in exclusive mode).
    """
    for attempt in range(MAX_GENERATION_RACE_RETRIES + 1):
        expected_generation, expected_cc_generation = _read_generations()
        opa_input = {
            "contract_version": CONTRACT_VERSION,
            "operation": OPERATION,
            "gate": GATE,
            "repo_id": repo_id,
            "subject_key": subject_key,
            "expected_generation": expected_generation,
            "expected_cc_generation": expected_cc_generation,
        }
        decision = query_decision(opa_url, opa_input, timeout_seconds=timeout_seconds)

        recheck_generation, recheck_cc_generation = _read_generations()
        if (
            recheck_generation == expected_generation
            and recheck_cc_generation == expected_cc_generation
        ):
            return decision

        if attempt < MAX_GENERATION_RACE_RETRIES:
            continue

        return DecisionResult(
            ok=False, allow=False, reason="",
            error=(
                "ledger generation changed during OPA query (stale-race retry exhausted) -- "
                "failing closed rather than trusting a possibly-stale snapshot"
            ),
        )

    raise AssertionError("unreachable: loop always returns")


def evaluate(repo_path: Path, head_sha: str) -> VerifyResult:
    """Top-level fail-closed evaluation. NEVER raises -- every expected failure mode
    (RepoIdentityError, a non-allow/non-ok DecisionResult) maps to an explicit failure
    VerifyResult, and an outer catch-all (below) converts any OTHER exception the same way, so a
    forgotten conversion anywhere in this call chain can never escape as a bare traceback. Only a
    fully-validated ok=True, allow=True result is "success"."""
    try:
        return _evaluate_inner(repo_path, head_sha)
    except Exception as exc:  # noqa: BLE001 -- deliberate last-resort backstop, see docstring
        # Without this, an exception any inner helper forgot to convert to RepoIdentityError would
        # propagate out of evaluate() as a bare traceback -- exactly the "script exception" case
        # design decision #4 explicitly promises maps to conclusion="failure", never a crash.
        return VerifyResult(
            conclusion="failure", title="unexpected script exception",
            summary=f"{type(exc).__name__}: {exc}",
        )


def _evaluate_inner(repo_path: Path, head_sha: str) -> VerifyResult:
    try:
        repo_id = resolve_repo_id_from_env()
    except RepoIdentityError as exc:
        return VerifyResult(conclusion="failure", title="repo identity unresolved", summary=str(exc))

    try:
        subject_key = resolve_subject_key_for_commit(repo_path, head_sha)
    except RepoIdentityError as exc:
        return VerifyResult(conclusion="failure", title="subject tree unresolved", summary=str(exc))

    decision = query_decision_with_race_check(repo_id, subject_key)

    if not decision.ok:
        return VerifyResult(
            conclusion="failure", title="guardrails policy engine unreachable or invalid",
            summary=f"OPA query failed: {decision.error} -- failing closed",
        )

    if not decision.allow:
        return VerifyResult(
            conclusion="failure", title="no valid attestation found",
            summary=decision.reason or "guardrails policy denied",
        )

    return VerifyResult(conclusion="success", title="attestation verified", summary=decision.reason)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-path", required=True,
        help="path to the checked-out repo (trusted base-branch checkout) whose object "
             "database has the PR head commit fetched into it",
    )
    parser.add_argument(
        "--head-sha", required=True,
        help="the PR's head commit sha (already fetched, never checked out, into --repo-path)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON result")
    args = parser.parse_args()

    start = time.monotonic()
    result = evaluate(Path(args.repo_path), args.head_sha)
    elapsed = time.monotonic() - start

    if args.json:
        print(json.dumps({
            "conclusion": result.conclusion, "title": result.title,
            "summary": result.summary, "elapsed_seconds": round(elapsed, 3),
        }))
    else:
        print(f"[{result.conclusion}] {result.title}: {result.summary}", file=sys.stderr)

    return 0 if result.conclusion == "success" else 1


if __name__ == "__main__":
    sys.exit(main())

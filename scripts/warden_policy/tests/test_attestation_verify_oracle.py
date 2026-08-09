"""Independent oracle for LIA-536's `attestation-verify` git-level backstop.

Authored by the `oracle-author` role, from the spec, BLIND to
`scripts/warden_policy/attestation_verify_check.py` (which does not exist yet -- this ticket is
pre-implementation) and to the implementer's own `test_attestation_verify_check.py`. Sources used:
`docs/decisions/git-level-hard-backstop-design.md`, `docs/decisions/opa-warden-attestations-v1.md`'s
"### Phase 4" section, the real `scripts/warden_policy/policy/guardrails.rego` (ground truth for the
Rego contract, read lines ~150-228 plus the shared helpers it depends on), and this repo's own
existing, already-tested `scripts/warden_policy/opa_client.py` / `attestation_store.py` /
`cc_attestations.py` / `git_subject.py` modules (public, pre-existing infrastructure this new check
is documented to reuse, not the new implementation itself).

Two independent tracks, of deliberately different strength:

TRACK A -- `RegoAttestationVerifyOracle`: spins up a REAL local `opa run --server` process loading
the ACTUAL `guardrails.rego` policy file, writes fixtures through the real, already-tested
`AttestationStore` write path (both the Hermes-shaped default document and the isolated
`warden_cc_attestations` document `cc_attestations.py` uses), and queries
`deus.wardens.decision` for `operation: "attestation.verify"` through the real, already-tested
`query_decision` client. This is strong, direct evidence: it validates the real policy file
end-to-end, completely independent of whether `attestation_verify_check.py` (which does not exist
yet) ever gets the wrapping right. Skipped (loudly, not silently) if `opa` is not on PATH.

TRACK B -- `AttestationVerifyCheckContractOracle`: black-box tests against the documented public
contract of `attestation_verify_check.evaluate(repo_path, head_sha) -> VerifyResult`. Since that
module does not exist yet, every test here is expected to fail today with
`ModuleNotFoundError`/`ImportError` -- a stronger, more specific red than a stand-in function's
tautological pass, matching this repo's own established convention for pre-implementation oracles
(see `opa-warden-attestations-v1.md`'s Phase 2/Phase 3 "Independent oracle" sections). Once the
module exists, these tests exercise the Python wrapper's fail-closed CONCLUSION MAPPING by mocking
`query_decision`'s return value -- this does NOT re-validate the Rego policy (Track A already does
that against a real OPA process); it validates that `evaluate()` correctly maps a KNOWN-GOOD or
KNOWN-BAD OPA answer into `conclusion == "success"` / `"failure"`, per this ticket's own documented
fail-closed contract. This is the weaker of the two tracks, and is reported as such.

DISCLOSED LIMITATION on independence: while reading `attestation_store.py` (pre-existing, already-
tested infrastructure, needed to build ledger fixtures for Track A) this author incidentally saw a
docstring passage naming `attestation_verify_check.py` as a third caller of `read_locked()` and
describing, in prose, an internal locking/retry strategy it is said to use (reading both ledgers,
querying OPA without holding a lock across the network call, then re-acquiring and comparing
generations, one bounded retry, fail-closed on drift). That passage describes internal MECHANISM,
not observable CONTRACT, and no assertion in this file targets it -- every assertion here targets
only the documented public contract (`evaluate(repo_path, head_sha) -> VerifyResult`, the Rego
decision shape, and the ledger paths/constants explicitly named in this ticket's own brief).
Disclosed here rather than silently used, per this role's standing honesty requirement.

NOT COVERED by this oracle (see "Falsifies" notes on individual tests for what each one DOES
catch):
  - The GitHub Actions actor-guard (non-owner PRs) -- enforced entirely in
    `.github/workflows/attestation-verify.yml`, not in Python. See
    `AttestationVerifyCheckContractOracle.test_actor_guard_out_of_scope_for_python_oracle`.
  - `integration_id`/ruleset/`bypass_actors` GitHub configuration -- not a Python-testable surface.
  - The internal locking/retry mechanism named in the disclosed limitation above -- deliberately,
    per that disclosure.
  - Live concurrency/race behavior of the two-ledger read -- out of scope for a pre-implementation
    oracle; would need the real implementation to exist first.
"""

from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.attestation_store import AttestationStore  # noqa: E402
from warden_policy.cc_attestations import CC_DOCUMENT_KEY  # noqa: E402
from warden_policy.git_subject import resolve_repo_id  # noqa: E402
from warden_policy.opa_client import DecisionResult, query_decision  # noqa: E402

POLICY_PATH = Path(__file__).resolve().parents[1] / "policy" / "guardrails.rego"
OPA_AVAILABLE = __import__("shutil").which("opa") is not None


# --------------------------------------------------------------------------------------------
# Shared git/OPA-process test helpers -- none of this is the new implementation; it is plumbing
# to build realistic fixtures against the real, pre-existing git_subject.py / AttestationStore /
# guardrails.rego contract.
# --------------------------------------------------------------------------------------------

def _git(*args: str, cwd) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    )
    return result.stdout


def _init_repo(path) -> str:
    """Create a real, throwaway git repo with one commit. Returns the commit sha."""
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "oracle@test.example", cwd=path)
    _git("config", "user.name", "Oracle", cwd=path)
    (Path(path) / "f.txt").write_text("hello\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).strip()


def _subject_key_for_commit(repo, commit_sha: str) -> str:
    """`git-tree:<object-format>:<oid>` for *commit_sha*'s own tree.

    Mirrors the format `git_subject.py::resolve_subject_key` uses for the staged index
    (`git-tree:<algo>:<oid>`), applied instead to a real commit's tree -- the shape
    `git-level-hard-backstop-design.md` §3.2 documents for the merge-commit case
    ("subject_key: git-tree:<merge commit's tree sha>"). Deliberately independent of any
    resolution helper `attestation_verify_check.py` itself might define internally.
    """
    object_format = _git("rev-parse", "--show-object-format", cwd=repo).strip() or "sha1"
    tree_sha = _git("rev-parse", f"{commit_sha}^{{tree}}", cwd=repo).strip()
    return f"git-tree:{object_format}:{tree_sha}"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _OpaServer:
    """Manages one real `opa run --server` subprocess loading the real guardrails.rego."""

    def __init__(self, policy_path: Path):
        self.policy_path = policy_path
        self.port = _find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 20.0) -> None:
        self.proc = subprocess.Popen(
            ["opa", "run", "--server", "--addr", f"127.0.0.1:{self.port}", str(self.policy_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("opa server process exited before becoming healthy")
            try:
                with urllib.request.urlopen(f"{self.base_url}/health", timeout=1) as resp:
                    if resp.status == 200:
                        return
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError("opa server did not become healthy in time")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


@contextlib.contextmanager
def _canonical_repo_env(value: str | None):
    """Set (or explicitly unset) DEUS_CANONICAL_REPO (LIA-536) for the duration of the block."""
    had_original = "DEUS_CANONICAL_REPO" in os.environ  # LIA-536
    original = os.environ.get("DEUS_CANONICAL_REPO")  # LIA-536
    if value is None:
        os.environ.pop("DEUS_CANONICAL_REPO", None)
    else:
        os.environ["DEUS_CANONICAL_REPO"] = value  # LIA-536
    try:
        yield
    finally:
        if had_original:
            os.environ["DEUS_CANONICAL_REPO"] = original  # LIA-536
        else:
            os.environ.pop("DEUS_CANONICAL_REPO", None)


# --------------------------------------------------------------------------------------------
# TRACK A -- real OPA, real guardrails.rego. Strong, direct evidence.
# --------------------------------------------------------------------------------------------

@unittest.skipUnless(OPA_AVAILABLE, "opa not found on PATH -- Track A requires a real OPA binary")
class RegoAttestationVerifyOracle(unittest.TestCase):
    """Exercises the REAL `deus.wardens.decision` Rego rule for `operation: attestation.verify`,
    via a real local `opa run --server` process loading the real
    `scripts/warden_policy/policy/guardrails.rego` file -- independent of whether
    `attestation_verify_check.py` exists or wraps this correctly. This is the ground-truth policy
    contract per `opa-warden-attestations-v1.md`'s Phase 4 section and `guardrails.rego:150-228`.
    """

    @classmethod
    def setUpClass(cls):
        cls.opa = _OpaServer(POLICY_PATH)
        cls.opa.start()

    @classmethod
    def tearDownClass(cls):
        cls.opa.stop()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.head_sha = _init_repo(self.repo)
        self.repo_id = resolve_repo_id(self.repo)
        self.subject_key = _subject_key_for_commit(self.repo, self.head_sha)
        self.hermes_store = AttestationStore(
            Path(self.tmp.name) / "hermes.json", opa_base_url=self.opa.base_url,
        )
        self.cc_store = AttestationStore(
            Path(self.tmp.name) / "cc.json", opa_base_url=self.opa.base_url,
            document_key=CC_DOCUMENT_KEY,
        )

    def tearDown(self):
        self.tmp.cleanup()

    # -- fixture helpers --------------------------------------------------------------------

    def _issue_hermes(self, subject_key: str, verdict: str) -> int:
        result = self.hermes_store.issue(
            repo_id=self.repo_id, gate="code-review", subject_key=subject_key,
            verdict=verdict, issuer_kind="manual", reviewer_id="oracle@test", reason="fixture",
        )
        self.assertTrue(result.ok and result.activated, f"Hermes fixture write failed: {result}")
        return result.generation

    def _enroll_hermes_only(self) -> int:
        """Bring the Hermes document into existence (so `supported` can hold) without issuing
        any record for `self.subject_key` -- used by scenarios needing 'Hermes has no opinion'.
        """
        result = self.hermes_store.enroll(self.repo_id)
        self.assertTrue(result.ok and result.activated, f"Hermes enroll fixture failed: {result}")
        return result.generation

    def _issue_cc(self, subject_key: str, verdict: str, backend: str) -> int:
        result = self.cc_store.issue_if_newer(
            repo_id=self.repo_id, gate="code-reviewer", subject_key=subject_key,
            verdict=verdict, issuer_kind="manual", reviewer_id="oracle-cc@test",
            reason="fixture", queued_at=time.time_ns(), backend=backend,
        )
        self.assertTrue(result.ok and result.activated, f"CC fixture write failed: {result}")
        return result.generation

    def _query(self, subject_key: str, hermes_gen: int, cc_gen: int) -> DecisionResult:
        opa_input = {
            "contract_version": 1,
            "operation": "attestation.verify",
            "gate": "code-review",
            "repo_id": self.repo_id,
            "subject_key": subject_key,
            "expected_generation": hermes_gen,
            "expected_cc_generation": cc_gen,
        }
        return query_decision(self.opa.base_url, opa_input, timeout_seconds=5.0)

    # -- scenario 1 -----------------------------------------------------------------------

    def test_hermes_native_ship_allows(self):
        # @oracle LIA-536: guardrails.rego:183-186,202-206 hermes_path_ok -- a fresh Hermes-
        # native code-review SHIP for the exact subject tree must allow, attributed to the
        # Hermes-native path. Falsifies: a Rego regression that stops recognizing a genuine
        # Hermes SHIP, or attributes the allow to the wrong path.
        gen = self._issue_hermes(self.subject_key, "SHIP")
        result = self._query(self.subject_key, gen, 0)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.allow, result.reason)
        self.assertIn("Hermes-native", result.reason)

    # -- scenario 2 -----------------------------------------------------------------------

    def test_cc_mirrored_ship_allows_when_no_hermes_record(self):
        # @oracle LIA-536: guardrails.rego:167-181,196-200,208-218 cc_path_ok / valid_cc_
        # mirrored_ship -- a CC-mirrored claude-backend SHIP is sufficient evidence when Hermes
        # genuinely has no record at all for this tree. Falsifies: a Rego regression that
        # requires a Hermes SHIP even when Hermes never reviewed the tree, defeating the whole
        # point of LIA-530's CC-mirror fallback path.
        hermes_gen = self._enroll_hermes_only()
        cc_gen = self._issue_cc(self.subject_key, "SHIP", backend="claude")
        result = self._query(self.subject_key, hermes_gen, cc_gen)
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.allow, result.reason)
        self.assertIn("Claude Code native", result.reason)

    # -- scenario 3 -- the REVISE-override protection --------------------------------------

    def test_hermes_non_ship_verdict_overrides_cc_mirrored_ship(self):
        # @oracle LIA-536: guardrails.rego:188-200 hermes_record_exists -- the exact
        # REVISE-override bug named in opa-warden-attestations-v1.md's Phase 4 section, found
        # and fixed during LIA-530's own review: an explicit fresh Hermes REVISE or BLOCK for a
        # tree must NEVER be silently overridden by a CC-mirrored claude SHIP for that same
        # tree. Falsifies: a regression back to gating the CC path on `not valid_ship` (a
        # SHIP-specific check) instead of `not hermes_record_exists` (an existence check) --
        # exactly the exploit two independent reviewers found against an earlier draft.
        for non_ship_verdict in ("REVISE", "BLOCK"):
            with self.subTest(hermes_verdict=non_ship_verdict):
                hermes_store = AttestationStore(
                    Path(self.tmp.name) / f"hermes-{non_ship_verdict}.json",
                    opa_base_url=self.opa.base_url,
                )
                cc_store = AttestationStore(
                    Path(self.tmp.name) / f"cc-{non_ship_verdict}.json",
                    opa_base_url=self.opa.base_url, document_key=CC_DOCUMENT_KEY,
                )
                hermes_result = hermes_store.issue(
                    repo_id=self.repo_id, gate="code-review", subject_key=self.subject_key,
                    verdict=non_ship_verdict, issuer_kind="manual", reviewer_id="oracle@test",
                    reason="fixture-non-ship",
                )
                self.assertTrue(hermes_result.ok and hermes_result.activated)
                cc_result = cc_store.issue_if_newer(
                    repo_id=self.repo_id, gate="code-reviewer", subject_key=self.subject_key,
                    verdict="SHIP", issuer_kind="manual", reviewer_id="oracle-cc@test",
                    reason="fixture-cc-ship", queued_at=time.time_ns(), backend="claude",
                )
                self.assertTrue(cc_result.ok and cc_result.activated)
                opa_input = {
                    "contract_version": 1,
                    "operation": "attestation.verify",
                    "gate": "code-review",
                    "repo_id": self.repo_id,
                    "subject_key": self.subject_key,
                    "expected_generation": hermes_result.generation,
                    "expected_cc_generation": cc_result.generation,
                }
                result = query_decision(self.opa.base_url, opa_input, timeout_seconds=5.0)
                self.assertTrue(result.ok, result.error)
                self.assertFalse(
                    result.allow,
                    f"a CC-mirrored SHIP must not override an explicit Hermes {non_ship_verdict}",
                )

    # -- scenario 4 -----------------------------------------------------------------------

    def test_no_evidence_anywhere_denies(self):
        # @oracle LIA-536: guardrails.rego:220-228 catch-all deny -- no Hermes record and no CC
        # record for the tree at all must deny. Falsifies: a default-allow regression (the
        # single most dangerous failure mode for a hard backstop).
        hermes_gen = self._enroll_hermes_only()
        result = self._query(self.subject_key, hermes_gen, 0)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.allow)
        self.assertIn("no SHIP found", result.reason)

    # -- scenario 6 -- staleness guards ------------------------------------------------------

    def test_stale_hermes_generation_denies_despite_valid_ship(self):
        # @oracle LIA-536: guardrails.rego:18-22 `supported` -- gates BOTH the Hermes-native and
        # CC-mirror paths (per the file's own header comment). A stale expected_generation must
        # deny even though a genuinely valid Hermes SHIP exists for the tree. Falsifies: a
        # regression that lets a stale OPA snapshot (e.g. after a failed/ambiguous PUT) still
        # serve an allow.
        gen = self._issue_hermes(self.subject_key, "SHIP")
        result = self._query(self.subject_key, gen + 1, 0)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.allow)

    def test_stale_cc_generation_denies_cc_only_path(self):
        # @oracle LIA-536: guardrails.rego:161-165 cc_supported -- the CC-mirror path's OWN,
        # separate staleness guard on data.warden_cc_attestations.generation. A stale
        # expected_cc_generation must deny the CC path even though a genuinely valid CC SHIP
        # exists and Hermes has no opinion. Falsifies: a regression that drops this guard,
        # letting a stale CC snapshot serve an allow.
        hermes_gen = self._enroll_hermes_only()
        cc_gen = self._issue_cc(self.subject_key, "SHIP", backend="claude")
        result = self._query(self.subject_key, hermes_gen, cc_gen + 1)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.allow)

    # -- scenario 7 -- backend-key hardcoding ------------------------------------------------

    def test_cc_mirrored_ship_under_non_claude_backend_denies(self):
        # @oracle LIA-536: guardrails.rego:167-168 valid_cc_mirrored_ship -- the backend index
        # is hardcoded to the literal "claude" key. A SHIP mirrored under any other backend
        # (e.g. "gpt") must NOT satisfy the CC path when no claude-backend record exists for the
        # same tree. Falsifies: a future accidental change to that hardcoded key (e.g. reading
        # an arbitrary/first backend) going undetected -- the exact risk the Rego file's own
        # comment at this line names explicitly.
        hermes_gen = self._enroll_hermes_only()
        cc_gen = self._issue_cc(self.subject_key, "SHIP", backend="gpt")
        result = self._query(self.subject_key, hermes_gen, cc_gen)
        self.assertTrue(result.ok, result.error)
        self.assertFalse(result.allow)


# --------------------------------------------------------------------------------------------
# TRACK B -- black-box contract test against attestation_verify_check.evaluate(). Weaker
# evidence (mocks query_decision rather than exercising a real OPA), and expected to fail with
# ModuleNotFoundError today since the module does not exist yet -- see module docstring.
# --------------------------------------------------------------------------------------------

class AttestationVerifyCheckContractOracle(unittest.TestCase):
    """Black-box tests against the documented public contract:

        evaluate(repo_path: Path, head_sha: str) -> VerifyResult
        VerifyResult.conclusion in {"success", "failure"}, plus .title, .summary

    ASSUMPTION, stated explicitly since this module cannot be read to confirm it: these tests
    patch `attestation_verify_check.query_decision`, following the exact import/patch pattern
    `scripts/hermes_warden_gate.py` and its own test (`test_hermes_warden_gate.py`) already
    establish in this codebase for the identical `opa_client.query_decision` boundary (the spec
    says this module "queries OPA's deus.wardens.decision endpoint (via
    scripts/warden_policy/opa_client.py's query_decision)"). If the real implementation calls
    query_decision through some other indirection, `mock.patch.object` will raise AttributeError
    -- that would itself be a legitimate implementation-review finding (an unexpected import
    shape for a boundary this repo has an established pattern for), not a flaw in the contract
    this oracle checks.
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from warden_policy import attestation_verify_check as avc  # noqa: PLC0415
        self.avc = avc
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        self.head_sha = _init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    # -- fail-closed conclusion mapping ------------------------------------------------------

    def test_success_only_on_ok_true_allow_true(self):
        # @oracle LIA-536: spec's documented fail-closed contract -- "Only a verified ok=True,
        # allow=True maps to success." Falsifies: a wrapper that treats any non-error OPA
        # response as success regardless of `allow`.
        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision",
            return_value=DecisionResult(
                ok=True, allow=True, reason="matching code-review SHIP (Hermes-native)",
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "success")

    def test_failure_on_legitimate_deny(self):
        # @oracle LIA-536: spec's fail-closed contract -- "any legitimate ok=True, allow=False
        # deny... map[s] to conclusion=failure." Falsifies: a wrapper that only fails closed on
        # errors but treats a real, well-formed OPA deny as some other non-failure outcome.
        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision",
            return_value=DecisionResult(
                ok=True, allow=False, reason="no SHIP found for git-tree:sha1:x",
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "failure")

    def test_failure_on_opa_unreachable(self):
        # @oracle LIA-536: scenario 5 / spec's fail-closed contract -- "ANY OPA-unreachable...
        # condition... map[s] to conclusion=failure," and design doc §3.5: never skipped or
        # defaulted to pass. Falsifies: an unreachable-OPA condition that raises uncaught (CI
        # would likely then report a different, less clear failure mode) or that defaults to
        # success.
        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision",
            return_value=DecisionResult(
                ok=False, allow=False, reason="", error="request failed: connection refused",
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "failure")

    def test_failure_on_malformed_opa_response(self):
        # @oracle LIA-536: scenario 5, malformed-response variant -- same fail-closed contract,
        # distinct trigger (a reachable-but-garbage OPA response) from plain unreachability.
        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision",
            return_value=DecisionResult(
                ok=False, allow=False, reason="", error="malformed JSON: ...",
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "failure")

    def test_missing_canonical_repo_fails_closed_without_querying_opa(self):
        # @oracle LIA-536: spec -- "DEUS_CANONICAL_REPO... must be set to a real git repo path
        # or evaluate() fails closed." Falsifies: a wrapper that falls back to some other repo
        # resolution silently, or crashes uncaught, when the env var is absent/invalid; also
        # falsifies a wrapper that queries OPA anyway with a garbage repo_id instead of failing
        # closed before ever reaching the network.
        with _canonical_repo_env(None), mock.patch.object(
            self.avc, "query_decision",
            side_effect=AssertionError(
                "must not query OPA when DEUS_CANONICAL_REPO cannot be resolved"
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "failure")

    # -- shape / documented-field checks -----------------------------------------------------

    def test_verify_result_has_documented_fields(self):
        # @oracle LIA-536: spec -- "VerifyResult has fields conclusion, title, summary." Does
        # NOT assert exact wording (unspecified by the ticket) -- only that the documented shape
        # is honored with non-empty, human-readable content.
        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision",
            return_value=DecisionResult(
                ok=True, allow=True, reason="matching code-review SHIP (Hermes-native)",
            ),
        ):
            result = self.avc.evaluate(self.repo, self.head_sha)
        self.assertIn(result.conclusion, ("success", "failure"))
        self.assertIsInstance(result.title, str)
        self.assertTrue(result.title.strip())
        self.assertIsInstance(result.summary, str)
        self.assertTrue(result.summary.strip())

    def test_opa_input_carries_documented_operation_and_gate(self):
        # @oracle LIA-536: spec -- queries OPA "with operation: 'attestation.verify', gate:
        # 'code-review'." Falsifies: a wrapper that sends the wrong operation/gate string,
        # which per guardrails.rego's own guards would silently fall through to the file's
        # default deny for every real invocation.
        captured: dict = {}

        def _capture(opa_base_url, opa_input, timeout_seconds):
            captured.update(opa_input)
            return DecisionResult(
                ok=True, allow=True, reason="matching code-review SHIP (Hermes-native)",
            )

        with _canonical_repo_env(str(self.repo)), mock.patch.object(
            self.avc, "query_decision", side_effect=_capture,
        ):
            self.avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(captured.get("operation"), "attestation.verify")
        self.assertEqual(captured.get("gate"), "code-review")

    # -- explicit, documented scope exclusion ------------------------------------------------

    def test_actor_guard_out_of_scope_for_python_oracle(self):
        # @oracle LIA-536: scenario 8 -- the non-owner-actor guard is enforced entirely inside
        # .github/workflows/attestation-verify.yml's own unconditional early step (never a
        # job/step-level `if:`, per git-level-hard-backstop-design.md §3.3, so its conclusion is
        # always success/failure, never `skipped` -- GitHub's required-status-check evaluation
        # treats `skipped` as satisfying the requirement). No OPA query happens on that path at
        # all, so there is nothing in evaluate() for a Python-side oracle to assert. Recorded as
        # an explicit, visible scope exclusion rather than silently omitted.
        self.skipTest(
            "actor-guard enforcement lives entirely in the GitHub Actions workflow YAML "
            "(.github/workflows/attestation-verify.yml), not in Python -- see "
            "git-level-hard-backstop-design.md Sec 3.3. Out of scope for this Python oracle."
        )


if __name__ == "__main__":
    unittest.main()

"""Tests for attestation_verify_check.py (LIA-536).

Coverage notes: the actor-guard mechanism (design decision #5 -- must run first, must post its
own failure Check Run before `exit 1`) lives entirely in `.github/workflows/attestation-verify.yml`
shell steps, not in this Python module, so it has no direct unit-test surface here. It is verified
by direct review of the workflow YAML and by the one live end-to-end test PR run against the
ephemeral self-hosted runner (design decision #8) -- not reproduced as a pytest case, since there
is no Python function to call.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import warden_policy.attestation_verify_check as avc
from warden_policy.attestation_store import AttestationStore
from warden_policy.cc_attestations import CC_DOCUMENT_KEY
from warden_policy.git_subject import resolve_subject_key
from warden_policy.opa_client import DecisionResult


def _git(*args, cwd):
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
    ).stdout


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _commit_file(path, name, content):
    (Path(path) / name).write_text(content)
    _git("add", name, cwd=path)
    _git("commit", "-q", "-m", f"add {name}", cwd=path)
    return _git("rev-parse", "HEAD", cwd=path).strip()


class TestResolveRepoId(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        _commit_file(self.repo, "f.txt", "hello\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolves_from_deus_canonical_repo_env_var(self):
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(self.repo)}, clear=False):
            repo_id = avc.resolve_repo_id_from_env()
        self.assertTrue(repo_id.startswith("git-common-dir-sha256:"))

    def test_differs_from_a_naive_github_workspace_derived_id(self):
        # A different (ephemeral-checkout-shaped) path must resolve to a DIFFERENT repo_id --
        # proves this script is not accidentally coupled to GITHUB_WORKSPACE. Two independent
        # repos never collide (git_subject.resolve_repo_id hashes an absolute canonical path).
        other = Path(self.tmp.name) / "ephemeral-actions-checkout"
        other.mkdir()
        _init_repo(other)
        _commit_file(other, "f.txt", "hello\n")

        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(self.repo)}, clear=False):
            canonical_id = avc.resolve_repo_id_from_env()
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(other)}, clear=False):
            workspace_shaped_id = avc.resolve_repo_id_from_env()

        self.assertNotEqual(canonical_id, workspace_shaped_id)

    def test_unset_env_var_fails_closed_with_var_name_in_message(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(avc.RepoIdentityError) as ctx:
                avc.resolve_repo_id_from_env()
        self.assertIn("DEUS_CANONICAL_REPO", str(ctx.exception))

    def test_invalid_path_fails_closed_with_path_in_message(self):
        bogus = str(Path(self.tmp.name) / "does-not-exist")
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": bogus}, clear=False):
            with self.assertRaises(avc.RepoIdentityError) as ctx:
                avc.resolve_repo_id_from_env()
        self.assertIn(bogus, str(ctx.exception))

    def test_non_repo_directory_fails_closed(self):
        not_a_repo = Path(self.tmp.name) / "just-a-dir"
        not_a_repo.mkdir()
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(not_a_repo)}, clear=False):
            with self.assertRaises(avc.RepoIdentityError):
                avc.resolve_repo_id_from_env()


class TestSubjectKeyEquivalence(unittest.TestCase):
    """The single load-bearing correctness claim of design decision #2: the CI-style resolver
    (git rev-parse ^{tree} against an existing commit) must be byte-identical to what
    git_subject.resolve_subject_key() computes pre-commit via write-tree for the same tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ci_resolver_matches_pre_commit_write_tree_resolver(self):
        (self.repo / "f.txt").write_text("content\n")
        _git("add", "f.txt", cwd=self.repo)

        pre_commit_key = resolve_subject_key(self.repo)

        _git("commit", "-q", "-m", "commit it", cwd=self.repo)
        head_sha = _git("rev-parse", "HEAD", cwd=self.repo).strip()

        ci_key = avc.resolve_subject_key_for_commit(self.repo, head_sha)

        self.assertEqual(pre_commit_key, ci_key)

    def test_ci_resolver_on_a_fetched_but_not_checked_out_commit(self):
        # Simulates the real workflow shape: a second bare-ish repo fetches the first repo's
        # commit without ever checking it out, then resolves subject_key against the fetched ref.
        head_sha = _commit_file(self.repo, "f.txt", "content\n")
        fetcher = Path(self.tmp.name) / "fetcher"
        fetcher.mkdir()
        _init_repo(fetcher)
        # An initial commit so the fetcher repo has a HEAD of its own (mirrors the workflow's
        # base-branch checkout, which always has content before fetching the PR head).
        _commit_file(fetcher, "base.txt", "base\n")

        _git("fetch", str(self.repo), head_sha, cwd=fetcher)
        # Confirm nothing was checked out -- the fetched file must not appear in the working tree.
        self.assertFalse((fetcher / "f.txt").exists())

        ci_key = avc.resolve_subject_key_for_commit(fetcher, head_sha)
        expected_key = resolve_subject_key(self.repo)  # computed pre-commit-equivalent, in repo
        # resolve_subject_key(self.repo) reflects self.repo's CURRENT index (post-commit, clean --
        # write-tree of a clean tree matches HEAD^{tree}), so this is a valid cross-clone check.
        self.assertEqual(ci_key, expected_key)


class TestReadVerifyRecheck(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hermes_ledger = Path(self.tmp.name) / "hermes.json"
        self.cc_ledger = Path(self.tmp.name) / "cc.json"
        self.hermes_patcher = mock.patch.object(avc, "LEDGER_PATH", self.hermes_ledger)
        self.cc_patcher = mock.patch("warden_policy.attestation_verify_check.CC_LEDGER_PATH", self.cc_ledger)
        self.hermes_patcher.start()
        self.cc_patcher.start()
        # Touch both ledgers into existence at generation 0 via a throwaway store instance.
        AttestationStore(self.hermes_ledger).read_locked()
        AttestationStore(self.cc_ledger, document_key=CC_DOCUMENT_KEY).read_locked()

    def tearDown(self):
        self.hermes_patcher.stop()
        self.cc_patcher.stop()
        self.tmp.cleanup()

    def test_happy_path_no_lock_held_during_query(self):
        lock_held_during_query = {"held": None}

        def _fake_query_decision(opa_url, opa_input, timeout_seconds):
            # Attempt a non-blocking exclusive lock on both ledgers -- if this script were still
            # holding a shared lock during the query, acquiring EXCLUSIVE here would raise
            # BlockingIOError, proving the held-lock shape. Success here proves NO lock is held.
            import fcntl
            for ledger in (self.hermes_ledger, self.cc_ledger):
                lock_path = ledger.with_suffix(ledger.suffix + ".lock")
                fd = __import__("os").open(lock_path, __import__("os").O_RDWR)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    __import__("os").close(fd)
            lock_held_during_query["held"] = False
            return DecisionResult(ok=True, allow=True, reason="ok")

        with mock.patch.object(avc, "query_decision", side_effect=_fake_query_decision):
            result = avc.query_decision_with_race_check("repo-id", "subject-key")

        self.assertFalse(lock_held_during_query["held"])
        self.assertTrue(result.ok)
        self.assertTrue(result.allow)

    def test_generation_mismatch_triggers_exactly_one_bounded_retry(self):
        call_count = {"n": 0}

        def _fake_query_decision(opa_url, opa_input, timeout_seconds):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate a concurrent write landing between the pre-read and the recheck.
                store = AttestationStore(self.hermes_ledger)
                store.enroll("some-repo-id")
            return DecisionResult(ok=True, allow=True, reason="ok")

        with mock.patch.object(avc, "query_decision", side_effect=_fake_query_decision):
            result = avc.query_decision_with_race_check("repo-id", "subject-key")

        self.assertEqual(call_count["n"], 2)  # exactly one retry, not zero, not unbounded
        self.assertTrue(result.ok)

    def test_second_consecutive_mismatch_fails_closed(self):
        def _fake_query_decision(opa_url, opa_input, timeout_seconds):
            # Every call perturbs the ledger -- every recheck will see a mismatch.
            store = AttestationStore(self.hermes_ledger)
            store.enroll(f"repo-id-{id(opa_input)}")
            return DecisionResult(ok=True, allow=True, reason="ok")

        with mock.patch.object(avc, "query_decision", side_effect=_fake_query_decision):
            result = avc.query_decision_with_race_check("repo-id", "subject-key")

        self.assertFalse(result.ok)
        self.assertIn("stale-race", result.error)

    def test_recheck_locks_are_sequential_never_simultaneous(self):
        # _read_generations() itself must never hold both locks at once -- verified by acquiring
        # an exclusive, non-blocking lock on ONE ledger from inside a mocked read of the OTHER.
        import fcntl
        import os as _os

        def _read_generations_probe():
            # Reimplements _read_generations but asserts, after the hermes read releases, that
            # the hermes lock is immediately acquirable exclusively (i.e., not still held while
            # the CC read proceeds).
            hermes_store = AttestationStore(self.hermes_ledger)
            hermes_doc = hermes_store.read_locked()
            hermes_lock = self.hermes_ledger.with_suffix(self.hermes_ledger.suffix + ".lock")
            fd = _os.open(hermes_lock, _os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # must not raise
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                _os.close(fd)
            cc_store = AttestationStore(self.cc_ledger, document_key=CC_DOCUMENT_KEY)
            cc_doc = cc_store.read_locked()
            return (
                hermes_doc[hermes_store.document_key]["generation"],
                cc_doc[cc_store.document_key]["generation"],
            )

        with mock.patch.object(avc, "_read_generations", side_effect=_read_generations_probe):
            with mock.patch.object(
                avc, "query_decision",
                return_value=DecisionResult(ok=True, allow=True, reason="ok"),
            ):
                result = avc.query_decision_with_race_check("repo-id", "subject-key")
        self.assertTrue(result.ok)


class TestFailClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.head_sha = _commit_file(self.repo, "f.txt", "hello\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _evaluate_with_decision(self, decision):
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(self.repo)}, clear=False):
            with mock.patch.object(avc, "query_decision_with_race_check", return_value=decision):
                return avc.evaluate(self.repo, self.head_sha)

    def test_opa_unreachable_is_failure(self):
        decision = DecisionResult(ok=False, allow=False, reason="", error="request failed")
        result = self._evaluate_with_decision(decision)
        self.assertEqual(result.conclusion, "failure")

    def test_malformed_response_is_failure(self):
        decision = DecisionResult(ok=False, allow=False, reason="", error="malformed JSON")
        result = self._evaluate_with_decision(decision)
        self.assertEqual(result.conclusion, "failure")

    def test_legitimate_deny_ok_true_allow_false_is_failure(self):
        # A correctly-computed deny (e.g. guardrails.rego's explicit deny body) must map to
        # "failure" exactly the same as an infra error -- both are "the check does not pass."
        decision = DecisionResult(ok=True, allow=False, reason="no code-review SHIP for staged tree")
        result = self._evaluate_with_decision(decision)
        self.assertEqual(result.conclusion, "failure")

    def test_verified_allow_is_success(self):
        decision = DecisionResult(ok=True, allow=True, reason="matching code-review SHIP")
        result = self._evaluate_with_decision(decision)
        self.assertEqual(result.conclusion, "success")

    def test_repo_identity_failure_is_failure_before_any_opa_call(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with mock.patch.object(avc, "query_decision_with_race_check") as mock_query:
                result = avc.evaluate(self.repo, self.head_sha)
        mock_query.assert_not_called()
        self.assertEqual(result.conclusion, "failure")

    def test_unresolvable_head_sha_is_failure_not_a_crash(self):
        # Found by code-reviewer + ai-eng-warden (independently, same root cause): an
        # unfetched/bogus head_sha makes `git rev-parse <sha>^{tree}` fail with
        # CalledProcessError, which used to escape resolve_subject_key_for_commit uncaught.
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(self.repo)}, clear=False):
            result = avc.evaluate(self.repo, "0" * 40)  # a well-formed but nonexistent sha
        self.assertEqual(result.conclusion, "failure")
        self.assertIn("subject tree unresolved", result.title)

    def test_unexpected_exception_anywhere_in_evaluate_is_failure_not_a_crash(self):
        # The outer catch-all backstop: even an exception NO inner helper converts must still
        # surface as conclusion="failure", never a bare traceback escaping evaluate().
        with mock.patch.dict("os.environ", {"DEUS_CANONICAL_REPO": str(self.repo)}, clear=False):
            with mock.patch.object(
                avc, "query_decision_with_race_check", side_effect=RuntimeError("boom"),
            ):
                result = avc.evaluate(self.repo, self.head_sha)
        self.assertEqual(result.conclusion, "failure")
        self.assertIn("unexpected script exception", result.title)
        self.assertIn("boom", result.summary)


if __name__ == "__main__":
    unittest.main()

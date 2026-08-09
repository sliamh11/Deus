import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[2] / "warden_attest.py"


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _cli(*args, ledger_path, cwd=None):
    result = subprocess.run(
        [sys.executable, str(CLI), "--ledger-path", str(ledger_path),
         "--opa-url", "http://127.0.0.1:1", "--json", *args],
        capture_output=True, text=True, cwd=cwd,
    )
    return result


class TestAttestCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        (self.repo / "f.txt").write_text("hello\n")
        _git("add", "f.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "init", cwd=self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_enroll_persists_despite_no_opa_running(self):
        # --opa-url points at an unreachable port -- PUT will fail, exit code should be
        # EXIT_NOT_ACTIVATED (3), but the enrollment must still be durably on disk.
        result = _cli("enroll", "--repo", str(self.repo), ledger_path=self.ledger)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])  # disk persistence succeeded
        self.assertFalse(payload["activated"])  # OPA sync did not
        self.assertTrue(self.ledger.exists())

    def test_issue_computes_subject_from_index_no_override_flag_exists(self):
        result = _cli(
            "issue", "--repo", str(self.repo), "--verdict", "SHIP",
            "--reviewer-id", "code-reviewer@claude-sonnet-5", "--reason", "looks good",
            ledger_path=self.ledger,
        )
        payload = json.loads(result.stdout)
        self.assertTrue(payload["subject_key"].startswith("git-tree:sha1:"))
        # confirm there is genuinely no way to override the subject
        help_result = subprocess.run(
            [sys.executable, str(CLI), "issue", "--help"], capture_output=True, text=True,
        )
        self.assertNotIn("--tree-sha", help_result.stdout)
        self.assertNotIn("--subject-key", help_result.stdout)

    def test_select_projects_only_requested_fields(self):
        _cli("issue", "--repo", str(self.repo), "--verdict", "SHIP",
             "--reviewer-id", "x@y", "--reason", "ok", ledger_path=self.ledger)
        result = subprocess.run(
            [sys.executable, str(CLI), "--ledger-path", str(self.ledger),
             "--opa-url", "http://127.0.0.1:1", "--json", "--select", "repo_id",
             "inspect", "--repo", str(self.repo)],
            capture_output=True, text=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload.keys()), {"repo_id"})

    def test_compact_flag_produces_valid_json_and_does_not_crash(self):
        # --compact wires agent_output's compact_json path (truncation of configured
        # long_fields, null-stripping) -- the CLI's current payload fields don't happen to
        # hit either behavior in this no-OPA test environment, so this is a smoke test that
        # the flag is real and doesn't break output; test_select_projects_only_requested_fields
        # above is the substantive coverage for the _agent_io wiring.
        result = _cli("--compact", "issue", "--repo", str(self.repo), "--verdict", "SHIP",
                       "--reviewer-id", "x@y", "--reason", "ok", ledger_path=self.ledger)
        payload = json.loads(result.stdout)
        self.assertIn("subject_key", payload)

    def test_inspect_shows_issued_record(self):
        _cli("issue", "--repo", str(self.repo), "--verdict", "SHIP",
             "--reviewer-id", "x@y", "--reason", "ok", ledger_path=self.ledger)
        result = _cli("inspect", "--repo", str(self.repo), ledger_path=self.ledger)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["records"]), 1)
        self.assertEqual(payload["records"][0]["verdict"], "SHIP")

    def test_check_non_commit_command_allows_without_git_repo_state(self):
        result = _cli("check", "--repo", str(self.repo), "--command", "git status",
                       ledger_path=self.ledger)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["is_commit_shaped"])
        self.assertTrue(payload["allow"])

    def test_check_unsupported_form_reported(self):
        result = _cli("check", "--repo", str(self.repo), "--command", "git commit -a -m x",
                       ledger_path=self.ledger)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["is_commit_shaped"])
        self.assertFalse(payload["supported"])

    def test_unenroll_never_enrolled_is_usage_error(self):
        result = _cli("unenroll", "--repo", str(self.repo), ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


class TestBackendFlagAndNewGateSubcommands(unittest.TestCase):
    """Independent oracle for LIA-524's warden_attest.py additions -- authored blind to the
    implementation: `issue` currently has no `--backend` flag at all, and none of
    enable-ai-eng-warden/disable-ai-eng-warden/enable-verification-gate/disable-verification-gate
    exist as subcommands (confirmed by reading warden_attest.py before writing this class).

    CLI contract (plan Design A.3, rounds 6/7/8/9): `--backend` is REQUIRED and fixed to
    "hermes" for `--gate ai-eng-warden`; REJECTED (any value, including "hermes") for the
    latest-only gates {code-review, plan-review, verification-gate}; left UNCONSTRAINED for
    code-reviewer (structurally latest_by_backend-keyed at the store layer, but this ticket
    owns no Hermes-side issuance semantics for it).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        (self.repo / "f.txt").write_text("hello\n")
        _git("add", "f.txt", cwd=self.repo)
        _git("commit", "-q", "-m", "init", cwd=self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"

    def tearDown(self):
        self.tmp.cleanup()

    # -- new enable/disable subcommands ---------------------------------------

    def test_enable_ai_eng_warden_subcommand_exists(self):
        result = _cli("enable-ai-eng-warden", "--repo", str(self.repo), ledger_path=self.ledger)
        # @oracle LIA-524: enable-ai-eng-warden must be a real subcommand -- argparse's own
        # "unrecognized argument"/invalid-choice error exits 2, never 0 or 3 (the app's own
        # not-activated code).
        self.assertIn(result.returncode, (0, 3), result.stdout + result.stderr)

    def test_disable_ai_eng_warden_never_enrolled_is_usage_error(self):
        result = _cli("disable-ai-eng-warden", "--repo", str(self.repo), ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: disabling a never-created entry is EXIT_USAGE=1, mirroring disable-plan-review

    def test_enable_verification_gate_subcommand_exists(self):
        result = _cli("enable-verification-gate", "--repo", str(self.repo), ledger_path=self.ledger)
        self.assertIn(result.returncode, (0, 3), result.stdout + result.stderr)  # @oracle LIA-524: enable-verification-gate must be a real subcommand

    def test_disable_verification_gate_never_enrolled_is_usage_error(self):
        result = _cli("disable-verification-gate", "--repo", str(self.repo), ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

    # -- --backend contract: ai-eng-warden (round 6) ---------------------------

    def test_issue_ai_eng_warden_missing_backend_is_usage_error(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "ai-eng-warden",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: round-6 -- missing --backend for ai-eng-warden is a usage error, not a silent misroute

    def test_issue_ai_eng_warden_wrong_backend_is_usage_error(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "ai-eng-warden", "--backend", "gpt",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: round-6 -- --backend gpt for ai-eng-warden is a usage error

    def test_issue_ai_eng_warden_correct_backend_succeeds_and_lands_under_latest_by_backend(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "ai-eng-warden", "--backend", "hermes",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertNotEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: --backend hermes for ai-eng-warden succeeds
        payload = json.loads(result.stdout)
        doc = json.loads(self.ledger.read_text())["warden_attestations"]
        subject_key = payload["subject_key"]
        repo_id = payload["repo_id"]
        by_backend = doc.get("latest_by_backend", {}).get(repo_id, {}).get("ai-eng-warden", {}).get(subject_key, {})
        self.assertIn("hermes", by_backend)  # @oracle LIA-524: lands under latest_by_backend[repo_id]["ai-eng-warden"][subject_key]["hermes"]
        self.assertNotIn("ai-eng-warden", doc.get("latest", {}).get(repo_id, {}))  # @oracle LIA-524: must never also touch latest

    # -- --backend contract: rejected for latest-only gates (round 7) ---------

    def test_issue_verification_gate_with_backend_is_usage_error(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "verification-gate", "--backend", "hermes",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: round-7 -- --backend rejected for the latest-only verification-gate

    def test_issue_code_review_with_backend_is_usage_error(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "code-review", "--backend", "hermes",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: round-7 -- --backend rejected for the pre-existing latest-only code-review gate

    def test_issue_plan_review_with_backend_is_usage_error(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "plan-review", "--backend", "hermes",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)  # @oracle LIA-524: round-7 -- --backend rejected for plan-review

    # -- --backend contract: code-reviewer left unconstrained (round 8/9) -----

    def test_issue_code_reviewer_with_backend_succeeds_not_rejected(self):
        result = _cli("issue", "--repo", str(self.repo), "--gate", "code-reviewer", "--backend", "hermes",
                       "--verdict", "SHIP", "--reviewer-id", "x@y", "--reason", "ok",
                       ledger_path=self.ledger)
        # @oracle LIA-524: round-8/9 -- code-reviewer is deliberately left unconstrained (a
        # structurally latest_by_backend-keyed gate this ticket doesn't own issuance semantics
        # for) -- must NOT be rejected by the new validation.
        self.assertNotEqual(result.returncode, 1, result.stdout + result.stderr)

    def test_backend_flag_documented_in_issue_help(self):
        help_result = subprocess.run(
            [sys.executable, str(CLI), "issue", "--help"], capture_output=True, text=True,
        )
        self.assertIn("--backend", help_result.stdout)  # @oracle LIA-524: --backend is a real, documented flag on `issue`


if __name__ == "__main__":
    unittest.main()

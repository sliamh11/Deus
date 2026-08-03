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


if __name__ == "__main__":
    unittest.main()

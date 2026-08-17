import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import hermes_warden_gate as gate
from warden_policy.attestation_store import AttestationStore
from warden_policy.opa_client import DecisionResult


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (Path(path) / "f.txt").write_text("hello\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


def _always_ok_put(self, inner_doc):
    return True, inner_doc["generation"], None


UNSUPPORTED_COMMIT = 'git commit -a -m x'  # missing required flags


class TestHermesWardenGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.log_path = Path(self.tmp.name) / "decisions.jsonl"
        # A real, empty hooks directory -- command_parser.classify() now validates that
        # `-c core.hooksPath=<value>` actually points to one (round 3 of adversarial plan
        # review), not just that the key is right.
        self.hooks_dir = Path(self.tmp.name) / "hooks"
        self.hooks_dir.mkdir()
        self.SUPPORTED_COMMIT = f'git -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        self.patchers = [
            mock.patch.object(gate, "LEDGER_PATH", self.ledger),
            # Isolate the decision log too -- found live: without this, running these tests
            # writes real entries into the actual user's ~/.config/deus/guardrails/logs/
            # decisions.jsonl, polluting a real operational log with test fixtures.
            mock.patch.object(gate, "LOG_PATH", self.log_path),
        ]
        for p in self.patchers:
            p.start()
        self.put_patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok_put)
        self.put_patcher.start()

    def tearDown(self):
        self.put_patcher.stop()
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def _payload(self, command):
        return {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.repo)}

    def test_non_terminal_tool_allows(self):
        result = gate.decide({"tool_name": "read_file", "tool_input": {}, "cwd": str(self.repo)})
        self.assertEqual(result, {})

    def test_non_commit_command_allows(self):
        result = gate.decide(self._payload("git status"))
        self.assertEqual(result, {})

    def test_unenrolled_repo_allows_even_with_unsupported_form(self):
        # THE ordering-fix proof: repo is never enrolled, so an ordinary (unsupported-form)
        # commit must still be allowed -- form validation must not run before enrollment check.
        result = gate.decide(self._payload(UNSUPPORTED_COMMIT))
        self.assertEqual(result, {})

    def test_enrolled_repo_blocks_unsupported_form(self):
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        result = gate.decide(self._payload(UNSUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")
        self.assertIn("unsupported commit form", result["message"])

    def test_enrolled_repo_with_ship_allows(self):
        from warden_policy.git_subject import resolve_repo_id, resolve_subject_key
        store = AttestationStore(self.ledger)
        repo_id = resolve_repo_id(self.repo)
        store.enroll(repo_id)
        subject_key = resolve_subject_key(self.repo)
        store.issue(repo_id=repo_id, gate="code-review", subject_key=subject_key,
                    verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="ok")
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=True, reason="matching code-review SHIP"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result, {})

    def test_opa_input_includes_gate_field(self):
        # Phase 0 of the Claude-Code-gate-to-OPA migration made guardrails.rego's
        # "code-review" decision bodies gate-scoped -- an omitted "gate" field would make
        # input.gate undefined in Rego, silently falling through to the file's own default
        # deny for every real Hermes commit. Confirm the shim actually sends it.
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        captured = {}

        def _capture(opa_url, opa_input, timeout_seconds):
            captured.update(opa_input)
            return DecisionResult(ok=True, allow=True, reason="matching code-review SHIP")

        with mock.patch.object(gate, "query_decision", side_effect=_capture):
            gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(captured.get("gate"), "code-review")

    def test_enrolled_repo_with_opa_deny_blocks(self):
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=False, reason="no code-review SHIP for staged tree X"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")
        self.assertIn("no code-review SHIP", result["message"])

    def test_opa_unreachable_blocks_enrolled_repo_fail_closed(self):
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="connection refused"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")
        self.assertIn("failing closed", result["message"])

    def test_opa_unreachable_still_allows_unenrolled_repo(self):
        # Outage-scoping: an unreachable OPA never blocks an unenrolled repo, because
        # enrollment is resolved from the local ledger (a pure disk read) BEFORE any OPA call.
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for an unenrolled repo"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result, {})

    def test_opa_timeout_blocks(self):
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="timed out"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")

    def test_opa_garbage_response_blocks(self):
        from warden_policy.git_subject import resolve_repo_id
        store = AttestationStore(self.ledger)
        store.enroll(resolve_repo_id(self.repo))
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="malformed JSON"),
        ):
            result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")

    def test_unreadable_ledger_blocks(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not valid json")
        result = gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result["action"], "block")

    def test_not_a_git_repo_blocks_commit_shaped_command(self):
        non_repo = Path(self.tmp.name) / "not-a-repo"
        non_repo.mkdir()
        payload = {"tool_name": "terminal", "tool_input": {"command": self.SUPPORTED_COMMIT}, "cwd": str(non_repo)}
        result = gate.decide(payload)
        self.assertEqual(result["action"], "block")

    def test_main_never_raises_on_malformed_stdin(self):
        # total exception containment: even garbage stdin must produce a valid block, exit 0
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            exit_code = gate.main()
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exit_code, 0)


class TestDashCAwareRepoResolution(unittest.TestCase):
    """Independent oracle for LIA-524 Design section D applied to the ALREADY-SHIPPED
    hermes_warden_gate.py -- authored from the plan's final design, blind to the implementation:
    `command_parser.classify()` has no `dash_c_target`/`dash_c_rejected` fields yet, and
    `hermes_warden_gate.py:87` still unconditionally does `repo_path = Path(cwd)`, never
    consulting `-C` at all (confirmed by reading both files before writing this class).

    The scenario this whole fix exists to close (round 13-15's live-verified, PRE-EXISTING
    bypass, independent of this ticket): an UNENROLLED cwd + a `-C <path>` targeting a
    DIFFERENT, enrolled-but-unattested repo currently resolves repo_id from cwd, finds
    "not enrolled", and allows BEFORE `classification.supported` is ever consulted -- the actual
    commit then executes against the real (enrolled, unattested) target repo.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd_repo = Path(self.tmp.name) / "cwd-repo"
        self.cwd_repo.mkdir()
        _init_repo(self.cwd_repo)
        self.target_repo = Path(self.tmp.name) / "target-repo"
        self.target_repo.mkdir()
        _init_repo(self.target_repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.log_path = Path(self.tmp.name) / "decisions.jsonl"
        self.hooks_dir = Path(self.tmp.name) / "hooks"
        self.hooks_dir.mkdir()
        self.patchers = [
            mock.patch.object(gate, "LEDGER_PATH", self.ledger),
            mock.patch.object(gate, "LOG_PATH", self.log_path),
        ]
        for p in self.patchers:
            p.start()
        self.put_patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok_put)
        self.put_patcher.start()
        self.store = AttestationStore(self.ledger)

    def tearDown(self):
        self.put_patcher.stop()
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def _dash_c_payload(self, target, extra=""):
        command = f'git -C {target} -c core.hooksPath={self.hooks_dir} commit --no-verify -m x {extra}'.strip()
        return {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.cwd_repo)}

    def test_dash_c_to_different_unenrolled_repo_allows(self):
        # scenario 1: enrolled-only-via-cwd; -C targets a DIFFERENT, UNenrolled repo -> must
        # ALLOW, evaluated against the -C TARGET's own not-enrolled status.
        from warden_policy.git_subject import resolve_repo_id
        self.store.enroll(resolve_repo_id(self.cwd_repo))
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for an unenrolled -C target"),
        ):
            result = gate.decide(self._dash_c_payload(self.target_repo))
        self.assertEqual(result, {})  # @oracle LIA-524: -C to a different unenrolled repo allows, not gated by cwd's own enrollment

    def test_dash_c_to_different_enrolled_attested_repo_allows(self):
        # scenario 2: UNenrolled-via-cwd; -C targets a DIFFERENT, enrolled+attested repo -> must
        # ALLOW -- the corrected, intended new behavior this whole fix exists to enable (the
        # scenario an earlier "reject -C outright" draft would have needlessly blocked).
        from warden_policy.git_subject import resolve_repo_id, resolve_subject_key
        target_repo_id = resolve_repo_id(self.target_repo)
        self.store.enroll(target_repo_id)
        subject_key = resolve_subject_key(self.target_repo)
        self.store.issue(repo_id=target_repo_id, gate="code-review", subject_key=subject_key,
                          verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="ok")
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=True, reason="matching code-review SHIP"),
        ):
            result = gate.decide(self._dash_c_payload(self.target_repo))
        self.assertEqual(result, {})  # @oracle LIA-524: -C to a different enrolled+attested repo allows

    def test_dash_c_to_different_enrolled_unattested_repo_blocks(self):
        # scenario 3: THE round-13 exploit -- UNenrolled-via-cwd, -C to a DIFFERENT enrolled
        # repo with NO valid SHIP -> must BLOCK. The regression test for the live, pre-existing
        # bypass this fix closes.
        from warden_policy.git_subject import resolve_repo_id
        self.store.enroll(resolve_repo_id(self.target_repo))
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=False, reason="no code-review SHIP for staged tree X"),
        ):
            result = gate.decide(self._dash_c_payload(self.target_repo))
        # @oracle LIA-524: round-13 exploit closed -- -C to an enrolled-but-unattested repo must
        # block, never allow via cwd's own (unenrolled) status.
        self.assertEqual(result.get("action"), "block")

    def test_unsafe_dash_c_blocks_unconditionally_regardless_of_any_enrollment(self):
        # scenario 4: identity unknowable -> fail closed UNCONDITIONALLY, before any repo/
        # enrollment lookup at all -- must never fall back to cwd's (here: never-enrolled)
        # status as if -C had never been present.
        command = f'git -C "$(touch /tmp/x)" -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        payload = {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.cwd_repo)}
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried once -C is rejected"),
        ):
            result = gate.decide(payload)
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: unsafe -C blocks unconditionally

    def test_no_dash_c_regression_unaffected(self):
        # scenario 5: regression safety -- no -C at all must behave exactly as before this fix.
        from warden_policy.git_subject import resolve_repo_id
        self.store.enroll(resolve_repo_id(self.cwd_repo))
        command = f'git -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        payload = {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.cwd_repo)}
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=True, reason="matching code-review SHIP"),
        ):
            result = gate.decide(payload)
        self.assertEqual(result, {})  # @oracle LIA-524: no -C at all -> unaffected, cwd-based resolution exactly as before

    def test_dash_c_to_enrolled_repo_blocks_on_unrelated_unsupported_form_evaluated_against_target(self):
        # ROUND-16 addition -- the exact scenario the round-16 placement fix closes: safe -C to
        # a DIFFERENT, ENROLLED repo, but the command is otherwise malformed for a reason
        # unrelated to -C (missing --no-verify / -c core.hooksPath) -> must BLOCK on the
        # unsupported-form reason, evaluated against the -C TARGET's own enrollment, never
        # silently falling back to cwd's (unenrolled) status just because the failure reason
        # wasn't the -C check itself.
        from warden_policy.git_subject import resolve_repo_id
        self.store.enroll(resolve_repo_id(self.target_repo))
        command = f'git -C {self.target_repo} commit -m x'  # missing -c core.hooksPath and --no-verify
        payload = {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.cwd_repo)}
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for an unsupported commit form"),
        ):
            result = gate.decide(payload)
        # @oracle LIA-524: round-16 -- unsupported form + a safe -C to an ENROLLED target must
        # block on form, not silently allow via cwd's own unenrolled status.
        self.assertEqual(result.get("action"), "block")
        self.assertIn("unsupported commit form", result.get("message", ""))


if __name__ == "__main__":
    unittest.main()

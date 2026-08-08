"""Independent oracle for LIA-524 -- Hermes-side ai-eng-warden trigger + verification-gate
wiring, plus the shared `-C`-aware repo-identity fix (Design section D) applied to the two NEW
gate scripts specifically (the ALREADY-SHIPPED hermes_warden_gate.py's own -C tests live in
test_hermes_warden_gate.py; the classifier-level -C field tests live in test_command_parser.py).

Authored FROM THE SPEC (the fully-reviewed, round-18-SHIPped plan), blind to the
implementation -- confirmed before writing a single test here that NONE of the following exist
yet: scripts/hermes_ai_eng_warden_gate.py, scripts/hermes_verification_gate.py,
scripts/warden_policy/llm_file_patterns.py; AttestationStore has no set_ai_eng_warden_enabled/
set_verification_gate_enabled; attestation-v1.schema.json's gate enum has no
"verification-gate" entry.

Interface assumed (justified by the spec's own explicit instructions -- see each section below
for the specific plan citation; a reviewer should check these against whatever actually gets
built):

  - scripts/hermes_ai_eng_warden_gate.py and scripts/hermes_verification_gate.py are each
    "a fully independent sibling script (NOT a modification of hermes_warden_gate.py, matching
    hermes_plan_review_gate.py's own documented precedent)" (plan, Design A.5) -- so BOTH are
    assumed to expose the exact same black-box contract hermes_warden_gate.py and
    hermes_plan_review_gate.py already do and are already tested through: importable as their
    own top-level module, a callable `decide(payload: dict) -> dict` returning `{}` (allow) or
    `{"action": "block", "message": str}` (block), a callable `main() -> int`, module-level
    `LEDGER_PATH`/`LOG_PATH` constants for test isolation, and `query_decision` imported into
    their own module namespace so it can be mocked at `<module>.query_decision` without a real
    OPA process. Design A.5 additionally names "own SHIM_SELF_DEADLINE_SECONDS budget" as part
    of hermes_ai_eng_warden_gate.py's independence -- assumed present as a module constant,
    mirroring hermes_warden_gate.py's own `SHIM_SELF_DEADLINE_SECONDS`.
  - scripts/warden_policy/llm_file_patterns.py holds "the two file-pattern constants, moved
    from codex_warden_hooks.py's private _AI_ENG_BASENAMES/_AI_ENG_DIR_PREFIXES" (plan, Design
    A.1) byte-identical in value. The plan doesn't state the new (necessarily public, since two
    separate consumer modules now import them) names explicitly -- assumed `AI_ENG_BASENAMES`
    (set) and `AI_ENG_DIR_PREFIXES` (tuple), the natural de-underscored names for constants
    moving from a private single-file scope into a shared module. Flagged explicitly here and
    in the confirmed-red report as the one interface assumption in this file with genuine
    naming ambiguity -- a correct implementation using different names would fail
    TestLlmFilePatternsInterface for the WRONG reason (an interface mismatch, not a behavior
    bug); every OTHER test in this file drives the two new gate scripts purely through their
    observable decide()/main() contract instead, and is robust to that ambiguity.

Every discriminating assertion is tagged `# @oracle LIA-524: ...`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError  # noqa: E402
from warden_policy.git_subject import resolve_repo_id  # noqa: E402
from warden_policy.opa_client import DecisionResult  # noqa: E402


def _git(*args, cwd):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)
    (path / "f.txt").write_text("hello\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)


def _always_ok_put(self, inner_doc):
    return True, inner_doc["generation"], None


ALLOW = {}


# ==========================================================================================
# llm_file_patterns.py -- moved constants (Design A.1)
# ==========================================================================================

class TestLlmFilePatternsInterface(unittest.TestCase):
    def test_module_exposes_basenames_and_dir_prefixes(self):
        import warden_policy.llm_file_patterns as patterns  # @oracle LIA-524: module must exist and be importable
        # Byte-identical to codex_warden_hooks.py's CURRENT (pre-move) private constants,
        # confirmed by reading codex_warden_hooks.py:1577-1582 before writing this assertion.
        self.assertEqual(
            patterns.AI_ENG_BASENAMES,  # @oracle LIA-524: interface assumption -- see module docstring
            {
                "linear-dispatcher.ts", "linear-webhook.ts", "linear-notifications.ts",
                "linear-gate-specs.ts", "memory_indexer.py", "memory_tree.py",
            },
        )
        self.assertEqual(patterns.AI_ENG_DIR_PREFIXES, ("evolution/", ".claude/agents/"))  # @oracle LIA-524: dir-prefix set moved byte-identical


# ==========================================================================================
# AttestationStore new enrollment toggles (Design A.2 / B.2)
# ==========================================================================================

class TestNewEnrollmentToggles(unittest.TestCase):
    """Mirrors the EXISTING test_attestation_store.py conventions for set_plan_review_enabled
    (read directly before writing this class) -- same additive-switch shape, applied to the two
    new gates."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "ledger.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok_put)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_set_ai_eng_warden_enabled_creates_fresh_entry_with_code_review_off(self):
        r = self.store.set_ai_eng_warden_enabled("repo-a", True)
        self.assertTrue(r.ok)  # @oracle LIA-524: AttestationStore.set_ai_eng_warden_enabled must exist
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry["enabled"])  # @oracle LIA-524: code-review stays off, not auto-enrolled
        self.assertIn("enrolled_at", entry)
        self.assertTrue(entry["ai_eng_warden_enabled"])

    def test_disable_ai_eng_warden_never_enrolled_raises(self):
        with self.assertRaises(AttestationStoreError):  # @oracle LIA-524: disabling a never-created entry raises, mirroring set_plan_review_enabled's own convention
            self.store.set_ai_eng_warden_enabled("never-enrolled", False)

    def test_set_verification_gate_enabled_creates_fresh_entry_with_code_review_off(self):
        r = self.store.set_verification_gate_enabled("repo-a", True)
        self.assertTrue(r.ok)  # @oracle LIA-524: AttestationStore.set_verification_gate_enabled must exist
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry["enabled"])
        self.assertTrue(entry["verification_gate_enabled"])

    def test_disable_verification_gate_never_enrolled_raises(self):
        with self.assertRaises(AttestationStoreError):
            self.store.set_verification_gate_enabled("never-enrolled", False)

    def test_ai_eng_warden_and_verification_gate_toggles_are_independent(self):
        self.store.set_ai_eng_warden_enabled("repo-a", True)
        self.store.set_verification_gate_enabled("repo-a", True)
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertTrue(entry["ai_eng_warden_enabled"])
        self.assertTrue(entry["verification_gate_enabled"])
        self.store.set_ai_eng_warden_enabled("repo-a", False)
        entry2 = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry2["ai_eng_warden_enabled"])
        # @oracle LIA-524: disabling ai-eng-warden must not disturb the independent verification-gate switch
        self.assertTrue(entry2["verification_gate_enabled"])


# ==========================================================================================
# Schema widening (Design B.1)
# ==========================================================================================

class TestSchemaGateEnumWidened(unittest.TestCase):
    SCHEMA = Path(__file__).resolve().parents[1] / "policy" / "attestation-v1.schema.json"

    def test_verification_gate_admitted_to_gate_enum(self):
        enum = json.loads(self.SCHEMA.read_text())["$defs"]["record"]["properties"]["gate"]["enum"]
        self.assertIn("verification-gate", enum)  # @oracle LIA-524: schema's record.gate enum must admit "verification-gate"

    def test_pre_existing_gates_still_admitted(self):
        # Regression: widening must be additive, never a replacement.
        enum = json.loads(self.SCHEMA.read_text())["$defs"]["record"]["properties"]["gate"]["enum"]
        for g in ("code-review", "code-reviewer", "ai-eng-warden", "plan-review"):
            self.assertIn(g, enum)  # @oracle LIA-524: widening must not drop any pre-existing gate value


# ==========================================================================================
# hermes_ai_eng_warden_gate.py -- the 7-step sequence (Design A.5)
# ==========================================================================================

class TestHermesAiEngWardenGateOracle(unittest.TestCase):
    """Drives the module purely through decide()/main() -- see the module docstring's
    "Interface assumed" section. The 7-step sequence tested here is copied verbatim from the
    plan's own Design A.5 numbered list."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.log_path = Path(self.tmp.name) / "decisions.jsonl"
        self.hooks_dir = Path(self.tmp.name) / "hooks"
        self.hooks_dir.mkdir()
        self.SUPPORTED_COMMIT = f'git -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        self.UNSUPPORTED_COMMIT = "git commit -a -m x"

        import hermes_ai_eng_warden_gate as gate  # @oracle LIA-524: module must be importable as hermes_ai_eng_warden_gate
        self.gate = gate
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

    def _payload(self, command):
        return {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.repo)}

    def _enable(self):
        self.store.set_ai_eng_warden_enabled(resolve_repo_id(self.repo), True)

    def _touch_llm_file_and_stage(self):
        (self.repo / "memory_indexer.py").write_text("# touched\n")
        _git("add", "memory_indexer.py", cwd=self.repo)

    # -- step 1/2: scope -----------------------------------------------------

    def test_non_terminal_tool_allows(self):
        result = self.gate.decide({"tool_name": "read_file", "tool_input": {}, "cwd": str(self.repo)})
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 1 -- a non-terminal tool_name is always allowed

    def test_non_commit_command_allows(self):
        result = self.gate.decide(self._payload("git status"))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 2 -- a non-commit-shaped command is always allowed

    # -- step 4: enrollment gates everything else -----------------------------

    def test_unenrolled_repo_allows_regardless_of_commit_form(self):
        result = self.gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 4 -- an unenrolled repo behaves normally, even for an unsupported commit form

    # -- step 5: THE round-12 TOCTOU test -- the single most important non-`-C`
    # security test in Design section A. --------------------------------------

    def test_enrolled_blocks_unsupported_form_even_when_diff_does_not_touch_llm_file(self):
        # Round 12 (see plan): commit-form validation must be UNCONDITIONAL once enrolled --
        # never skipped just because the diff-trigger doesn't (yet) match. A diff-check-first
        # ordering (rounds 10/11, later found unsafe) would let
        # `git commit -a -m "$(touch memory_indexer.py)"`-shaped attacks dirty an LLM-pattern
        # file via command substitution AFTER a clean-diff check, inside the SAME shell
        # invocation the hook is about to allow.
        self._enable()
        result = self.gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        # @oracle LIA-524: round-12 TOCTOU -- unsupported form blocks unconditionally, even with
        # a clean (non-LLM-touching) diff. Falsifies a diff-check-first ordering.
        self.assertEqual(result.get("action"), "block")
        self.assertIn("unsupported commit form", result.get("message", ""))

    # -- step 6: diff-trigger gates the OPA query ------------------------------

    def test_enrolled_supported_form_diff_does_not_touch_llm_file_allows_without_querying_opa(self):
        self._enable()
        with mock.patch.object(
            self.gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried when the diff doesn't touch an LLM file pattern"),
        ):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 6 -- a non-LLM-touching diff allows without ever reaching OPA

    # -- step 7: diff-trigger fires -> OPA query with gate/backend -------------

    def test_enrolled_supported_form_diff_touches_llm_file_queries_opa_with_hermes_backend(self):
        self._enable()
        self._touch_llm_file_and_stage()
        captured = {}

        def _capture(opa_url, opa_input, timeout_seconds):
            captured.update(opa_input)
            return DecisionResult(ok=True, allow=True, reason="matching ai-eng-warden SHIP")

        with mock.patch.object(self.gate, "query_decision", side_effect=_capture):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)
        self.assertEqual(captured.get("gate"), "ai-eng-warden")  # @oracle LIA-524: OPA input carries gate="ai-eng-warden"
        self.assertEqual(captured.get("backend"), "hermes")  # @oracle LIA-524: OPA input carries the fixed self-identifying backend="hermes"

    def test_enrolled_supported_form_diff_touches_llm_file_opa_deny_blocks(self):
        self._enable()
        self._touch_llm_file_and_stage()
        with mock.patch.object(
            self.gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=False, reason="no ai-eng-warden SHIP for staged tree X"),
        ):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: an OPA deny for an LLM-touching diff must block

    def test_opa_unreachable_blocks_enrolled_repo_with_llm_diff_fail_closed(self):
        self._enable()
        self._touch_llm_file_and_stage()
        with mock.patch.object(
            self.gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="connection refused"),
        ):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: OPA unreachable for an enrolled repo with an LLM-touching diff must fail closed

    def test_main_never_raises_on_malformed_stdin(self):
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            exit_code = self.gate.main()
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exit_code, 0)  # @oracle LIA-524: malformed stdin must still exit 0 (Hermes treats non-zero/crash as allow)

    # -- Design section D applied to THIS new script ---------------------------

    def test_dash_c_rejected_blocks_unconditionally_before_enrollment_lookup(self):
        # Design D step 3 (spelled out explicitly for A.5 as its own numbered step, round 18
        # review): identity unknowable -> fail closed UNCONDITIONALLY, before any repo/
        # enrollment lookup -- even for a repo (cwd) never enrolled at all.
        command = f'git -C "$(touch /tmp/x)" -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        with mock.patch.object(
            self.gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried once -C is rejected"),
        ):
            result = self.gate.decide(self._payload(command))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: unsafe -C blocks unconditionally, even from a never-enrolled cwd

    def test_dash_c_to_different_enrolled_unattested_repo_blocks(self):
        # Test-correctness fix (found during implementation, not by any review round): the
        # original version of this test never touched/staged an LLM-pattern file in
        # `other_repo`, so per Design A.5's own step 6 ("diff doesn't touch an LLM file
        # pattern -> allow, gate doesn't fire") the FAITHFUL, documented behavior for a
        # completely clean diff is ALLOW, not BLOCK -- confirmed by running the real
        # implementation against the original test, which correctly returned `{}` (allow),
        # not a block. The oracle's own intent (prove identity resolves from -C, not cwd) is
        # preserved by actually reaching the diff-trigger's positive branch: touch/stage an
        # LLM-pattern file in `other_repo` (the -C target) so the diff-touches-LLM-file check
        # has something real to find, then the reached-OPA/fail-closed-on-unreachable path is
        # what proves identity resolved correctly (a cwd-fallback would never even reach the
        # diff check, since `self.repo` is never enrolled).
        other_repo = Path(self.tmp.name) / "other-repo"
        other_repo.mkdir()
        _init_repo(other_repo)
        self.store.set_ai_eng_warden_enabled(resolve_repo_id(other_repo), True)
        (other_repo / "memory_indexer.py").write_text("# touched\n")
        _git("add", "memory_indexer.py", cwd=other_repo)
        command = f'git -C {other_repo} -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        result = self.gate.decide(self._payload(command))  # cwd = self.repo, never enrolled
        # @oracle LIA-524: -C to a different enrolled-but-unattested repo, with an LLM-pattern
        # file actually touched in the -C target's own diff, must block -- identity must
        # resolve from the -C TARGET (reaching the diff-trigger's positive branch, then OPA),
        # not this script's naive cwd-only inheritance of the same bug hermes_warden_gate.py
        # had before Design D (which would allow immediately at step 4, never reaching here).
        self.assertEqual(result.get("action"), "block")


# ==========================================================================================
# hermes_verification_gate.py -- the 6-step sequence (Design B.4)
# ==========================================================================================

class TestHermesVerificationGateOracle(unittest.TestCase):
    """verification-gate has NO diff-trigger condition -- unconditional once enrolled. One step
    shorter than A.5's sequence (no step analogous to A.5's steps 6/7)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.log_path = Path(self.tmp.name) / "decisions.jsonl"
        self.hooks_dir = Path(self.tmp.name) / "hooks"
        self.hooks_dir.mkdir()
        self.SUPPORTED_COMMIT = f'git -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        self.UNSUPPORTED_COMMIT = "git commit -a -m x"

        import hermes_verification_gate as gate  # @oracle LIA-524: module must be importable as hermes_verification_gate
        self.gate = gate
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

    def _payload(self, command):
        return {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.repo)}

    def _enable(self):
        self.store.set_verification_gate_enabled(resolve_repo_id(self.repo), True)

    def test_non_terminal_tool_allows(self):
        result = self.gate.decide({"tool_name": "read_file", "tool_input": {}, "cwd": str(self.repo)})
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 1 -- non-terminal tool always allowed

    def test_non_commit_command_allows(self):
        result = self.gate.decide(self._payload("git status"))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 2 -- non-commit-shaped command always allowed

    def test_unenrolled_repo_allows_regardless_of_commit_form(self):
        result = self.gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: step 4 -- unenrolled repo behaves normally

    def test_enrolled_blocks_unsupported_form_unconditionally(self):
        # No diff-trigger condition at all for this gate -- form is checked unconditionally
        # once enrolled, always (round-11 fix note: verification-gate never had a
        # diff-trigger-first bug to begin with, but must still be tested).
        self._enable()
        with mock.patch.object(
            self.gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for an unsupported commit form"),
        ):
            result = self.gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: enrolled + unsupported form blocks, no diff-trigger to skip it

    def test_enrolled_supported_form_queries_opa_with_verification_gate_and_no_backend(self):
        self._enable()
        captured = {}

        def _capture(opa_url, opa_input, timeout_seconds):
            captured.update(opa_input)
            return DecisionResult(ok=True, allow=True, reason="matching verification-gate SHIP")

        with mock.patch.object(self.gate, "query_decision", side_effect=_capture):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)
        self.assertEqual(captured.get("gate"), "verification-gate")  # @oracle LIA-524: OPA input carries gate="verification-gate"
        self.assertNotIn("backend", captured)  # @oracle LIA-524: verification-gate is latest-indexed, single-verdict -- no backend concept

    def test_enrolled_supported_form_opa_deny_blocks(self):
        self._enable()
        with mock.patch.object(
            self.gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=False, reason="no verification-gate SHIP for staged tree X"),
        ):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: an OPA deny must block

    def test_opa_unreachable_blocks_enrolled_repo_fail_closed(self):
        self._enable()
        with mock.patch.object(
            self.gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="connection refused"),
        ):
            result = self.gate.decide(self._payload(self.SUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: OPA unreachable for an enrolled repo must fail closed

    def test_main_never_raises_on_malformed_stdin(self):
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            exit_code = self.gate.main()
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exit_code, 0)  # @oracle LIA-524: malformed stdin must still exit 0

    # -- Design section D applied to THIS new script ---------------------------

    def test_dash_c_rejected_blocks_unconditionally_before_enrollment_lookup(self):
        command = f'git -C "$(touch /tmp/x)" -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        with mock.patch.object(
            self.gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried once -C is rejected"),
        ):
            result = self.gate.decide(self._payload(command))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: unsafe -C blocks unconditionally, even from a never-enrolled cwd

    def test_dash_c_to_different_enrolled_unattested_repo_blocks(self):
        other_repo = Path(self.tmp.name) / "other-repo"
        other_repo.mkdir()
        _init_repo(other_repo)
        self.store.set_verification_gate_enabled(resolve_repo_id(other_repo), True)
        command = f'git -C {other_repo} -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        result = self.gate.decide(self._payload(command))  # cwd = self.repo, never enrolled
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: -C to a different enrolled-but-unattested repo must block

    def test_dash_c_order_independence_gate_level(self):
        # The order-independence discriminator (round 18, the single most important test in
        # this ticket per the plan's own review history) applied end-to-end at the GATE level,
        # not just the classifier level (test_command_parser.py covers the classifier itself):
        # a malformed -c appearing BEFORE a safe -C targeting a DIFFERENT, enrolled-but-
        # unattested repo. If this script (or classify() underneath it) used any of the three
        # prior, order-DEPENDENT placements (rounds 15/16/17), the -c malformation would
        # short-circuit before -C is ever read, dash_c_target would stay None, and the script
        # would silently fall back to cwd (never enrolled here) -- wrongly ALLOWING instead of
        # blocking against the real (enrolled, unattested) -C target.
        other_repo = Path(self.tmp.name) / "other-repo"
        other_repo.mkdir()
        _init_repo(other_repo)
        self.store.set_verification_gate_enabled(resolve_repo_id(other_repo), True)
        command = f'git -c core.fsmonitor=x -C {other_repo} -c core.hooksPath={self.hooks_dir} commit --no-verify -m x'
        result = self.gate.decide(self._payload(command))
        # @oracle LIA-524: order-independence at the gate level -- must block on either the -c
        # malformation itself or (once identity correctly resolves to the enrolled-but-
        # unattested other_repo) the missing SHIP -- NEVER silently allow via a wrongly-
        # abandoned cwd fallback.
        self.assertEqual(result.get("action"), "block")


# ==========================================================================================
# Cross-script independence (Testing section: "true independence across the three sibling
# scripts... a block from one must never leak into another's; a repo enrolled in only one of
# the three must only be gated by that one")
# ==========================================================================================

class TestCrossScriptIndependence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        _init_repo(self.repo)
        self.ledger = Path(self.tmp.name) / "ledger.json"
        self.hooks_dir = Path(self.tmp.name) / "hooks"
        self.hooks_dir.mkdir()
        self.UNSUPPORTED_COMMIT = "git commit -a -m x"

        import hermes_ai_eng_warden_gate
        import hermes_verification_gate
        import hermes_warden_gate
        self.ai_gate = hermes_ai_eng_warden_gate
        self.verif_gate = hermes_verification_gate
        self.code_gate = hermes_warden_gate

        self.patchers = []
        for mod in (self.ai_gate, self.verif_gate, self.code_gate):
            log_path = Path(self.tmp.name) / f"{mod.__name__}.log.jsonl"
            self.patchers.append(mock.patch.object(mod, "LEDGER_PATH", self.ledger))
            self.patchers.append(mock.patch.object(mod, "LOG_PATH", log_path))
        for p in self.patchers:
            p.start()
        self.put_patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok_put)
        self.put_patcher.start()
        self.store = AttestationStore(self.ledger)
        self.repo_id = resolve_repo_id(self.repo)

    def tearDown(self):
        self.put_patcher.stop()
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def _payload(self, command):
        return {"tool_name": "terminal", "tool_input": {"command": command}, "cwd": str(self.repo)}

    def test_ai_eng_only_enrolled_repo_verification_gate_still_allows(self):
        self.store.set_ai_eng_warden_enabled(self.repo_id, True)
        with mock.patch.object(
            self.verif_gate, "query_decision",
            side_effect=AssertionError("verification-gate must not query OPA for a repo it's not enrolled in"),
        ):
            result = self.verif_gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        # @oracle LIA-524: a repo enrolled in ai-eng-warden ONLY must be allowed by
        # verification-gate, regardless of ai-eng-warden's own enrollment/attestation state.
        self.assertEqual(result, ALLOW)

    def test_verification_only_enrolled_repo_ai_eng_warden_still_allows(self):
        self.store.set_verification_gate_enabled(self.repo_id, True)
        with mock.patch.object(
            self.ai_gate, "query_decision",
            side_effect=AssertionError("ai-eng-warden must not query OPA for a repo it's not enrolled in"),
        ):
            result = self.ai_gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: a repo enrolled in verification-gate ONLY must be allowed by ai-eng-warden

    def test_new_toggles_do_not_affect_pre_existing_code_review_gate(self):
        # A repo enrolled ONLY in the two new gates (code-review's own `enabled` left false)
        # must still be allowed by the ALREADY-SHIPPED code-review gate -- confirms the new
        # toggles are genuinely additive, never accidentally coupled to `enabled`.
        self.store.set_ai_eng_warden_enabled(self.repo_id, True)
        self.store.set_verification_gate_enabled(self.repo_id, True)
        with mock.patch.object(
            self.code_gate, "query_decision",
            side_effect=AssertionError("code-review must not query OPA for a repo it's not enrolled in"),
        ):
            result = self.code_gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result, ALLOW)  # @oracle LIA-524: new toggles must not implicitly enroll the pre-existing code-review gate

    def test_ai_eng_warden_block_message_never_leaks_verification_gate_reason(self):
        self.store.set_ai_eng_warden_enabled(self.repo_id, True)
        self.store.set_verification_gate_enabled(self.repo_id, True)
        with mock.patch.object(
            self.verif_gate, "query_decision",
            side_effect=AssertionError("verification-gate's own query_decision must not be touched by ai-eng-warden's decide()"),
        ):
            result = self.ai_gate.decide(self._payload(self.UNSUPPORTED_COMMIT))
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-524: ai-eng-warden's own block must never invoke or reference verification-gate's machinery
        self.assertNotIn("verification-gate", result.get("message", ""))


if __name__ == "__main__":
    unittest.main()

"""Independent oracle for scripts/hermes_plan_review_gate.py (LIA-523).

Authored FROM THE SPEC, blind to the implementation -- the file under test
(``scripts/hermes_plan_review_gate.py``) does not exist yet at authoring time.
See the LIA-523 ticket / plan for the full spec; the interface assumptions
this file relies on are recorded below so a reviewer can check them against
whatever actually gets built, per this repo's independent-oracle pattern
(same precedent as ``test_cc_write_path_oracle.py`` for LIA-527).

Interface assumed (all justified by the spec's explicit "structurally
modeled on scripts/hermes_warden_gate.py" instruction, and by reading that
already-shipped sibling + its test file, ``test_hermes_warden_gate.py``):

  - ``scripts/hermes_plan_review_gate.py`` is importable as top-level module
    ``hermes_plan_review_gate`` once ``scripts/`` is on ``sys.path`` (exactly
    how the sibling gate is imported and tested).
  - It exposes ``decide(payload: dict) -> dict`` taking the full
    ``pre_tool_call`` payload (``tool_name``, ``tool_input``, ``session_id``,
    ``cwd``, ``extra``) and returning ``{}`` (allow) or
    ``{"action": "block", "message": str}`` (block) -- the spec's own
    "Input"/"Output" sections, verbatim.
  - It exposes ``main() -> int`` reading JSON from stdin, printing JSON to
    stdout, always returning 0 -- again the spec's own "Output" section.
  - It imports ``query_decision`` from ``warden_policy.opa_client`` into its
    own module namespace (as the sibling does), so the real OPA policy
    decision for "is this session's plan-review attestation valid" can be
    mocked at ``gate.query_decision`` without touching a real OPA process.
  - It has a module-level ``LEDGER_PATH`` (and, matching the sibling,
    ``LOG_PATH``) constant used to construct its ``AttestationStore`` --
    needed for test isolation from the real, on-disk production ledger,
    exactly as the sibling's own test suite already relies on.

Everything else -- internal function names like the spec's own
``resolve_and_decide``/``parse_v4a_patch_targets``/``resolve_repo_id_precisely``
pseudocode helpers -- is deliberately NOT depended on here. This suite drives
the module only through the black-box ``decide()``/``main()`` contract, using
real temp git repos + a real (temp, isolated) ``AttestationStore`` ledger for
enrollment/attestation state, and hand-written V4A patch text for the
multi-file `patch` case -- so it discriminates a wrong *behavior* rather than
a wrong *internal decomposition*, and keeps passing across any correct
re-implementation that keeps the same observable contract.

Every discriminating assertion is tagged ``# @oracle LIA-523: ...``.
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

import hermes_plan_review_gate as gate  # noqa: E402 -- see sys.path.insert above
from warden_policy.attestation_store import AttestationStore  # noqa: E402
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


def _v4a_single(path: str) -> str:
    return (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )


def _v4a_two(path_a: str, path_b: str) -> str:
    return (
        "*** Begin Patch\n"
        f"*** Update File: {path_a}\n"
        "@@\n"
        "-old-a\n"
        "+new-a\n"
        f"*** Update File: {path_b}\n"
        "@@\n"
        "-old-b\n"
        "+new-b\n"
        "*** End Patch\n"
    )


def _v4a_move(old_path: str, new_path: str) -> str:
    # Real Hermes V4A syntax (confirmed against tools/patch_parser.py's actual regex,
    # `re.match(r'\*\*\*\s*Move\s+File:\s*(.+?)\s*->\s*(.+)', line)`): a MOVE is a SINGLE
    # "*** Move File: <src> -> <dst>" line, not a separate "Move to:" line after "Update
    # File:" -- the original two-line form here was a fixture bug (never exercised a real
    # MOVE operation at all; Hermes's parser silently fell back to a plain UPDATE with the
    # "Move to:" text ignored as an unrecognized line), found and fixed by running this
    # oracle against a real implementation and discovering it never actually tested the
    # MOVE-dual-path property it claimed to.
    return (
        "*** Begin Patch\n"
        f"*** Move File: {old_path} -> {new_path}\n"
        "*** End Patch\n"
    )


ALLOW = {}
SESSION_ID = "sess-oracle-0001"


class TestHermesPlanReviewGateOracle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        # Three distinct real repos so "which repo does this target resolve to"
        # is answered by real `git rev-parse`, never a stub.
        self.repo_enrolled_unattested = self.root / "repo-a"  # enrolled, no valid SHIP
        self.repo_enrolled_attested = self.root / "repo-b"  # enrolled, valid SHIP
        self.repo_unenrolled = self.root / "repo-c"  # never enrolled for plan-review
        self.repo_enrolled_attested_2 = self.root / "repo-d"  # enrolled + attested, distinct from repo-b
        for repo in (
            self.repo_enrolled_unattested,
            self.repo_enrolled_attested,
            self.repo_unenrolled,
            self.repo_enrolled_attested_2,
        ):
            _init_repo(repo)

        self.ledger = self.root / "ledger.json"
        self.log_path = self.root / "decisions.jsonl"

        self.patchers = [
            mock.patch.object(gate, "LEDGER_PATH", self.ledger),
            mock.patch.object(gate, "LOG_PATH", self.log_path),
        ]
        for p in self.patchers:
            p.start()

        # Never let a real AttestationStore mutation try to PUT to a real OPA
        # process during ledger setup (mirrors test_hermes_warden_gate.py).
        self.put_patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok_put)
        self.put_patcher.start()

        self.store = AttestationStore(self.ledger)
        self.repo_id_a = resolve_repo_id(self.repo_enrolled_unattested)
        self.repo_id_b = resolve_repo_id(self.repo_enrolled_attested)
        self.repo_id_c = resolve_repo_id(self.repo_unenrolled)
        self.repo_id_d = resolve_repo_id(self.repo_enrolled_attested_2)

        self.store.set_plan_review_enabled(self.repo_id_a, True)
        self.store.set_plan_review_enabled(self.repo_id_b, True)
        self.store.set_plan_review_enabled(self.repo_id_d, True)
        # repo_id_c deliberately left with no plan_review_enabled entry at all.

        # Seed real SHIP records for the "attested" repos, for realism -- the
        # actual pass/fail in these tests is controlled by mocking
        # `gate.query_decision` (the OPA call), matching how
        # test_hermes_warden_gate.py tests its own OPA-backed decisions.
        for repo_id in (self.repo_id_b, self.repo_id_d):
            self.store.issue(
                repo_id=repo_id, gate="plan-review", subject_key=SESSION_ID,
                verdict="SHIP", issuer_kind="warden", reviewer_id="plan-reviewer@claude",
                reason="oracle fixture", kind="session",
            )

    def tearDown(self):
        self.put_patcher.stop()
        for p in self.patchers:
            p.stop()
        self.tmp.cleanup()

    def _payload(self, tool_name, tool_input, cwd, session_id=SESSION_ID):
        return {
            "tool_name": tool_name, "tool_input": tool_input,
            "session_id": session_id, "cwd": str(cwd), "extra": {},
        }

    def _allow_everything_opa(self):
        return mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=True, reason="matching plan-review SHIP"),
        )

    def _deny_everything_opa(self, reason="no valid plan-review SHIP for this session"):
        return mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=True, allow=False, reason=reason),
        )

    # -- interface sanity -------------------------------------------------

    def test_module_exposes_expected_entry_points(self):
        self.assertTrue(callable(gate.decide))  # @oracle LIA-523: module exposes a callable decide(payload)
        self.assertTrue(callable(gate.main))  # @oracle LIA-523: module exposes a callable main()

    # -- scope: which calls this gate even looks at ------------------------

    def test_non_write_shaped_tool_allows(self):
        result = gate.decide(self._payload("read_file", {"path": "x"}, self.repo_unenrolled))
        self.assertEqual(result, ALLOW)  # @oracle LIA-523: a non write/patch tool_name is out of scope -- always allow

    def test_patch_unknown_mode_allows(self):
        result = gate.decide(
            self._payload("patch", {"mode": "rename", "path": "x"}, self.repo_unenrolled)
        )
        self.assertEqual(result, ALLOW)  # @oracle LIA-523: an unrecognized patch mode is out of scope -- allow, not a crash/block

    # -- enrollment-before-form-validation, unenrolled repos behave normally --

    def test_relative_write_in_unenrolled_repo_allows(self):
        result = gate.decide(
            self._payload("write_file", {"path": "new_file.txt"}, self.repo_unenrolled)
        )
        # @oracle LIA-523: falsifies an over-broad "block every relative-path write"
        # implementation that forgot enrollment-before-form-validation ordering.
        self.assertEqual(result, ALLOW)

    def test_relative_write_in_plan_review_enrolled_repo_blocks(self):
        # A purely-relative-path call whose cwd resolves to an ENROLLED repo -- the actual
        # named v1 limitation the spec calls out (relative paths always block once the
        # cwd-guessed repo looks enrolled). No prior test exercised this branch in isolation
        # (existing relative-path tests use an unenrolled cwd, or pair the relative target
        # with an absolute one whose own block masks this code path) -- found missing by
        # code review.
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for the relative-path coarse pre-check"),
        ):
            result = gate.decide(
                self._payload("write_file", {"path": "new_file.txt"}, self.repo_enrolled_unattested)
            )
        # @oracle LIA-523: falsifies an implementation that never actually wires up the
        # relative-path cwd-based pre-check at all (e.g. one that always allows relative
        # paths regardless of cwd enrollment) -- and, via the query_decision spy above,
        # falsifies an implementation that answers this branch by querying OPA instead of
        # deciding purely from local enrollment state (the design's whole point: the
        # coarse pre-check must never grant/deny via an attestation match on a guessed repo).
        self.assertEqual(result.get("action"), "block")
        self.assertIn("absolute path", result.get("message", ""))

    def test_absolute_write_in_unenrolled_repo_allows(self):
        target = str(self.repo_unenrolled / "new_file.txt")
        result = gate.decide(self._payload("write_file", {"path": target}, self.repo_unenrolled))
        self.assertEqual(result, ALLOW)  # @oracle LIA-523: an unenrolled repo's absolute-path write is not gated at all

    # -- core positive-block cases ------------------------------------------

    def test_absolute_write_in_enrolled_unattested_repo_blocks(self):
        target = str(self.repo_enrolled_unattested / "new_file.txt")
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("write_file", {"path": target}, self.repo_unenrolled)
            )
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-523: enrolled + no valid attestation must block a write_file
        self.assertIsInstance(result.get("message"), str)  # @oracle LIA-523: a block always carries a human-readable message
        self.assertTrue(result["message"])  # @oracle LIA-523: the block message is non-empty

    def test_patch_replace_mode_absolute_enrolled_unattested_blocks(self):
        target = str(self.repo_enrolled_unattested / "new_file.txt")
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("patch", {"mode": "replace", "path": target}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies an implementation that only branches on
        # tool_name == "write_file" and forgets patch+mode=="replace" carries
        # the same single-path shape (which would fall through to the "else: allow"
        # branch and silently un-gate every replace-mode patch call).
        self.assertEqual(result.get("action"), "block")

    # -- item 1: per-target independence inside one patch call --------------

    def test_absolute_target_blocks_even_with_relative_sibling_in_same_patch(self):
        abs_target = str(self.repo_enrolled_unattested / "abs_target.txt")
        patch_text = _v4a_two("relative_sibling.txt", abs_target)
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("patch", {"mode": "patch", "patch": patch_text}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies "any relative target present -> whole call
        # downgrades to the weak cwd-based check" -- the enrolled+unattested
        # absolute target must independently force a block regardless of the
        # relative sibling, and regardless of cwd (repo_unenrolled) being unenrolled.
        self.assertEqual(result.get("action"), "block")

    # -- item 2: absolute-path enrollment must use the path's OWN repo, never cwd --

    def test_absolute_target_enrollment_uses_own_repo_not_cwd(self):
        # cwd resolves to an UNENROLLED repo; the sole absolute target resolves to
        # a DIFFERENT, enrolled-but-unattested repo.
        abs_target = str(self.repo_enrolled_unattested / "abs_target.txt")
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("write_file", {"path": abs_target}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies an implementation that checks
        # is_plan_review_enrolled(cwd's repo) before/instead of branching on path
        # shape -- cwd's repo (repo_unenrolled) is NOT enrolled, so that bug would
        # wrongly allow; the correct check resolves enrollment from the absolute
        # target's own repo (repo_enrolled_unattested) and must block.
        self.assertEqual(result.get("action"), "block")

    # -- item 3: tilde is relative, never expanded ---------------------------

    def test_tilde_path_never_expanded_treated_as_relative(self):
        fake_home = self.root / "fake-home"
        fake_home.mkdir()
        enrolled_under_home = fake_home / "enrolled-repo"
        _init_repo(enrolled_under_home)
        repo_id_home = resolve_repo_id(enrolled_under_home)
        self.store.set_plan_review_enabled(repo_id_home, True)  # enrolled, unattested

        tilde_path = "~/enrolled-repo/new_file.txt"
        with mock.patch.dict("os.environ", {"HOME": str(fake_home)}):
            with self._deny_everything_opa():
                result = gate.decide(
                    self._payload("write_file", {"path": tilde_path}, self.repo_unenrolled)
                )
        # @oracle LIA-523: falsifies an implementation that calls
        # os.path.expanduser()/Path.expanduser() on the tilde path. If expanded,
        # it resolves inside the enrolled-but-unattested `enrolled-repo` and must
        # (incorrectly, per this bug) block. Treated correctly as relative, it
        # only gets the weak cwd-based check -- cwd (repo_unenrolled) is not
        # enrolled, so the correct result is ALLOW.
        self.assertEqual(result, ALLOW)

    # -- MOVE ops: both file_path and new_path count as targets --------------

    def test_move_new_path_is_also_checked_as_target(self):
        abs_new_path = str(self.repo_enrolled_unattested / "moved_target.txt")
        patch_text = _v4a_move("old_relative_name.txt", abs_new_path)
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("patch", {"mode": "patch", "patch": patch_text}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies an implementation that only extracts the
        # "Update File:" file_path and ignores the "Move to:" new_path -- such a
        # bug would see only the relative old name, apply the weak cwd check
        # (repo_unenrolled, unenrolled) and wrongly allow.
        self.assertEqual(result.get("action"), "block")

    # -- item 5 / ambiguity: only ENROLLED repo_ids count -------------------

    def test_enrolled_and_unenrolled_absolute_targets_not_treated_as_ambiguous_allows(self):
        abs_enrolled_attested = str(self.repo_enrolled_attested / "a.txt")
        abs_unenrolled = str(self.repo_unenrolled / "b.txt")
        patch_text = _v4a_two(abs_enrolled_attested, abs_unenrolled)
        with self._allow_everything_opa():
            result = gate.decide(
                self._payload("patch", {"mode": "patch", "patch": patch_text}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies an implementation that counts every distinct
        # absolute-path repo_id (enrolled or not) toward the ambiguity check --
        # only repo_enrolled_attested is plan-review-enrolled here, so there is
        # exactly one enrolled repo_id in play, it is attested, and the result
        # must be ALLOW, not a fail-closed ambiguity block.
        self.assertEqual(result, ALLOW)

    def test_two_distinct_enrolled_repos_in_one_patch_is_ambiguous_and_blocks(self):
        abs_a = str(self.repo_enrolled_attested / "a.txt")
        abs_d = str(self.repo_enrolled_attested_2 / "d.txt")
        patch_text = _v4a_two(abs_a, abs_d)
        with self._allow_everything_opa():
            result = gate.decide(
                self._payload("patch", {"mode": "patch", "patch": patch_text}, self.repo_unenrolled)
            )
        # @oracle LIA-523: both targets are individually enrolled AND attested
        # (OPA mocked to allow=True unconditionally), so the ONLY possible cause
        # for a block here is the cross-repo ambiguity rule itself -- this
        # falsifies an implementation that has no ambiguity check at all (which
        # would wrongly allow a write spanning two distinct enrolled repos in
        # one call, exactly the "do not guess" case the spec calls out).
        self.assertEqual(result.get("action"), "block")

    def test_resolution_deadline_exhaustion_blocks_without_querying_opa(self):
        # A large multi-file patch could exceed the self-deadline just resolving targets
        # (a `git` subprocess + a ledger-lock acquisition per absolute target), before ever
        # reaching the OPA query -- an internal timeout here must fail closed itself, never
        # let Hermes's own hook-level timeout (which fails OPEN) be the thing that decides.
        # Simulated deterministically by making time.monotonic() report elapsed time past
        # the deadline on the SECOND call (the first call is decide()'s own `start`).
        abs_target = str(self.repo_enrolled_unattested / "a.txt")
        real_start = gate.time.monotonic()
        call_count = {"n": 0}

        def _fake_monotonic():
            call_count["n"] += 1
            if call_count["n"] == 1:
                return real_start
            return real_start + gate.SHIM_SELF_DEADLINE_SECONDS + 1.0

        with mock.patch.object(gate.time, "monotonic", side_effect=_fake_monotonic):
            with mock.patch.object(
                gate, "query_decision",
                side_effect=AssertionError("OPA must not be queried once the resolution deadline is exhausted"),
            ):
                result = gate.decide(
                    self._payload("write_file", {"path": abs_target}, self.repo_unenrolled)
                )
        # @oracle LIA-523: falsifies an implementation with no internal deadline check at
        # all (which would either proceed to query OPA past budget, or -- worse -- run long
        # enough that Hermes's own hook timeout intervenes and fails OPEN instead of closed).
        self.assertEqual(result.get("action"), "block")

    # -- happy path + OPA input shape ----------------------------------------

    def test_attested_absolute_write_allows(self):
        target = str(self.repo_enrolled_attested / "new_file.txt")
        with self._allow_everything_opa():
            result = gate.decide(
                self._payload("write_file", {"path": target}, self.repo_unenrolled)
            )
        self.assertEqual(result, ALLOW)  # @oracle LIA-523: enrolled + valid SHIP attestation must allow

    def test_opa_input_uses_plan_review_gate_and_correct_session_id(self):
        target = str(self.repo_enrolled_attested / "new_file.txt")
        captured = {}

        def _capture(opa_url, opa_input, timeout_seconds):
            captured.update(opa_input)
            return DecisionResult(ok=True, allow=True, reason="matching plan-review SHIP")

        with mock.patch.object(gate, "query_decision", side_effect=_capture):
            gate.decide(self._payload("write_file", {"path": target}, self.repo_unenrolled))
        # @oracle LIA-523: falsifies a copy-paste from hermes_warden_gate.py that
        # forgot to change the OPA gate name from "code-review" to "plan-review"
        # (guardrails.rego's plan-review rules are gate-scoped, so a wrong gate
        # value silently falls through to a default deny/allow that has nothing
        # to do with plan-review attestations).
        self.assertEqual(captured.get("gate"), "plan-review")
        self.assertEqual(captured.get("session_id"), SESSION_ID)  # @oracle LIA-523: the OPA query must carry THIS call's session_id, not a stale/cached one

    def test_opa_deny_blocks_despite_a_local_ledger_record_existing(self):
        # A real SHIP record for repo_id_b/SESSION_ID already exists on disk
        # (seeded in setUp) -- this simulates the "expired" scenario: the local
        # ledger has *a* record, but OPA (the sole source of truth for
        # attestation freshness/validity, per spec) says it is not currently
        # valid. The Python gate must defer to OPA's answer, never a local
        # existence-only shortcut.
        target = str(self.repo_enrolled_attested / "new_file.txt")
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(
                ok=True, allow=False, reason="plan-review SHIP for this session has expired",
            ),
        ) as spy:
            result = gate.decide(
                self._payload("write_file", {"path": target}, self.repo_unenrolled)
            )
            self.assertTrue(spy.called)  # @oracle LIA-523: the decision is routed through OPA, not answered from local ledger existence alone
        # @oracle LIA-523: falsifies "no local logic re-implements/short-circuits
        # freshness" -- an expired (OPA-denied) attestation must block exactly
        # like no attestation at all, never fall through to allow because *a*
        # record happens to exist on disk.
        self.assertEqual(result.get("action"), "block")

    # -- fail-closed: OPA unreachable ----------------------------------------

    def test_opa_unreachable_blocks_enrolled_repo(self):
        target = str(self.repo_enrolled_attested / "new_file.txt")
        with mock.patch.object(
            gate, "query_decision",
            return_value=DecisionResult(ok=False, allow=False, reason="", error="connection refused"),
        ):
            result = gate.decide(
                self._payload("write_file", {"path": target}, self.repo_unenrolled)
            )
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-523: OPA unreachable for an enrolled repo must fail closed, never allow

    def test_opa_unreachable_still_allows_unenrolled_repo(self):
        target = str(self.repo_unenrolled / "new_file.txt")
        with mock.patch.object(
            gate, "query_decision",
            side_effect=AssertionError("OPA must not be queried for an unenrolled repo"),
        ):
            result = gate.decide(
                self._payload("write_file", {"path": target}, self.repo_unenrolled)
            )
        self.assertEqual(result, ALLOW)  # @oracle LIA-523: an outage never blocks a repo this gate was never asked to enforce

    # -- fail-closed: repo resolution -----------------------------------------

    def test_absolute_target_with_no_git_repo_anywhere_blocks(self):
        non_repo_target = str(self.root / "not-a-repo-at-all" / "new_file.txt")
        result = gate.decide(
            self._payload("write_file", {"path": non_repo_target}, self.repo_unenrolled)
        )
        # @oracle LIA-523: falsifies "no repo found -> treat as unenrolled -> allow".
        # The spec requires failing closed when the walk to filesystem root never
        # finds a git repo at all.
        self.assertEqual(result.get("action"), "block")

    def test_symlink_into_enrolled_unattested_repo_blocks(self):
        # A real symlink living INSIDE the unenrolled repo, pointing at a file inside the
        # enrolled-but-unattested repo. A write that "targets" the symlink's own path
        # actually lands on the real file it points to -- repo identity must be resolved
        # from the REAL destination, not the symlink's own containing directory. Found by
        # the GPT co-gate: resolving from the symlink's own location instead of its target
        # lets an unenrolled-or-attested repo's symlink smuggle a write into a different,
        # protected repo.
        symlink_path = self.repo_unenrolled / "link_into_repo_a.txt"
        real_target = self.repo_enrolled_unattested / "real_file.txt"
        real_target.write_text("hello\n")
        symlink_path.symlink_to(real_target)
        with self._deny_everything_opa():
            result = gate.decide(
                self._payload("write_file", {"path": str(symlink_path)}, self.repo_unenrolled)
            )
        # @oracle LIA-523: falsifies an implementation that resolves repo_id from the
        # symlink's own parent directory (repo_unenrolled, which this call's cwd is also
        # set to -- so a symlink-blind bug would see BOTH cwd and "target" as unenrolled
        # and wrongly allow) instead of the real file it points to.
        self.assertEqual(result.get("action"), "block")

    # -- item 6: total exception containment ----------------------------------

    def test_corrupt_ledger_blocks(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("{not valid json")
        target = str(self.repo_enrolled_attested / "new_file.txt")
        result = gate.decide(
            self._payload("write_file", {"path": target}, self.repo_unenrolled)
        )
        self.assertEqual(result.get("action"), "block")  # @oracle LIA-523: an unreadable ledger must block, never crash or silently allow

    def test_main_never_raises_on_malformed_stdin(self):
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO("not json at all")
        try:
            exit_code = gate.main()
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exit_code, 0)  # @oracle LIA-523: malformed stdin must still exit 0 (Hermes treats non-zero/crash as allow)

    def test_main_never_raises_on_malformed_patch_payload(self):
        # mode == "patch" but the required "patch" key is missing entirely --
        # a plausible internal KeyError deep inside patch parsing.
        malformed = json.dumps({
            "tool_name": "patch",
            "tool_input": {"mode": "patch"},
            "session_id": SESSION_ID,
            "cwd": str(self.repo_enrolled_unattested),
            "extra": {},
        })
        import io
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(malformed)
        try:
            captured_stdout = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured_stdout
            try:
                exit_code = gate.main()
            finally:
                sys.stdout = old_stdout
        finally:
            sys.stdin = old_stdin
        self.assertEqual(exit_code, 0)  # @oracle LIA-523: an internal parsing error must still exit 0
        printed = json.loads(captured_stdout.getvalue())
        # @oracle LIA-523: an internal error must produce an EXPLICIT block, not an
        # accidental `{}` allow -- the fail-closed floor for any unexpected exception.
        self.assertEqual(printed.get("action"), "block")


if __name__ == "__main__":
    unittest.main()

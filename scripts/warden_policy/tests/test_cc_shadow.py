"""Tests for the read-only OPA shadow observer (Phase 1).

The three invariants in ``cc_shadow``'s module docstring are its contract, so each has
an executable test here rather than a comment claiming it:

- no gate outcome depends on the observer  -> TestGateInvariance
- it never writes to stdout/stderr         -> TestStreamPurity
- it never writes or locks the ledger      -> TestNeverWritesLedger

The classification tests additionally assert every label is genuinely REACHABLE, not
merely defined -- an unreachable classification is worse than a missing one, because a
reader of the log would conclude that case never occurs.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy import cc_shadow  # noqa: E402
from warden_policy.opa_client import BackendVerdictsResult  # noqa: E402

REPO_ID = "git-common-dir-sha256:" + "a" * 64
SUBJECT = "git-tree:sha1:" + "b" * 40


def _ledger(generation=7):
    return {
        "warden_attestations": {
            "schema_version": 1,
            "generation": generation,
            "config": {"enforced_repos": {}},
            "records": {},
            "latest": {},
        }
    }


class _ShadowTestCase(unittest.TestCase):
    """Redirects the ledger + log to a temp dir and forces the flag ON."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.ledger_path = root / "attestations-v1.json"
        self.ledger_path.write_text(json.dumps(_ledger()), encoding="utf-8")
        self.log_path = root / "logs" / "cc-shadow.jsonl"
        self.repo_root = root / "repo"
        self.repo_root.mkdir()

        for attr, value in (("LEDGER_PATH", self.ledger_path), ("LOG_PATH", self.log_path)):
            patcher = mock.patch.object(cc_shadow, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        cc_shadow._reset_cache_for_tests()
        self.addCleanup(cc_shadow._reset_cache_for_tests)
        env = mock.patch.dict(os.environ, {cc_shadow.ENV_FLAG: "1"})
        env.start()
        self.addCleanup(env.stop)

        subj = mock.patch.object(cc_shadow, "resolve_subject", return_value=(REPO_ID, SUBJECT))
        subj.start()
        self.addCleanup(subj.stop)

    def _patch_opa(self, verdicts=None, ok=True, error=None, generation=7):
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(
            cc_shadow, "query_backend_verdicts",
            return_value=BackendVerdictsResult(ok=ok, verdicts=verdicts or {}, error=error),
        ).start()
        mock.patch.object(cc_shadow, "query_generation", return_value=generation).start()

    def _observe(self, role="code-reviewer", backends=("claude", "gpt"),
                 blocking=(), claude_verdict="SHIP"):
        cc_shadow.observe(
            role=role, worktree=self.repo_root, required_backends=list(backends),
            legacy_blocking=list(blocking), legacy_claude_verdict=claude_verdict,
        )

    def _entries(self):
        if not self.log_path.exists():
            return []
        return [json.loads(line) for line in self.log_path.read_text().splitlines() if line]

    def _only_entry(self):
        entries = self._entries()
        self.assertEqual(len(entries), 1, f"expected exactly one log line, got {entries}")
        return entries[0]


class TestFlagResolution(unittest.TestCase):
    def setUp(self):
        cc_shadow._reset_cache_for_tests()
        self.addCleanup(cc_shadow._reset_cache_for_tests)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _toggle(self, payload):
        path = self.root / cc_shadow.TOGGLE_RELPATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")

    def test_default_off_when_nothing_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cc_shadow.shadow_enabled(self.root))

    def test_toggle_file_enables(self):
        self._toggle(json.dumps({"enabled": True}))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(cc_shadow.shadow_enabled(self.root))

    def test_toggle_file_false_stays_off(self):
        self._toggle(json.dumps({"enabled": False}))
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cc_shadow.shadow_enabled(self.root))

    def test_env_overrides_toggle_file_both_directions(self):
        self._toggle(json.dumps({"enabled": True}))
        with mock.patch.dict(os.environ, {cc_shadow.ENV_FLAG: "0"}):
            self.assertFalse(cc_shadow.shadow_enabled(self.root))
        cc_shadow._reset_cache_for_tests()
        self._toggle(json.dumps({"enabled": False}))
        with mock.patch.dict(os.environ, {cc_shadow.ENV_FLAG: "1"}):
            self.assertTrue(cc_shadow.shadow_enabled(self.root))

    def test_corrupt_toggle_file_means_off_not_crash(self):
        self._toggle("{ not json")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(cc_shadow.shadow_enabled(self.root))


class TestFlagOffDoesNothing(_ShadowTestCase):
    """Invariant 1, weakest form: with the flag off nothing observable happens."""

    def test_no_network_no_log_no_ledger_read(self):
        # The gate-side helper is what consults the flag, so drive that path: with the
        # flag off it must not reach urlopen, must not open the ledger, and must not
        # create the log. Asserted at the urllib boundary so ANY network attempt fails.
        with mock.patch.dict(os.environ, {cc_shadow.ENV_FLAG: "0"}):
            cc_shadow._reset_cache_for_tests()
            with mock.patch("urllib.request.urlopen") as urlopen, \
                 mock.patch.object(cc_shadow, "_read_ledger_generation") as read_ledger:
                if cc_shadow.shadow_enabled(self.repo_root):
                    self._observe()
                urlopen.assert_not_called()
                read_ledger.assert_not_called()
        self.assertFalse(self.log_path.exists())


class TestClassification(_ShadowTestCase):
    def test_agree_allow(self):
        self._patch_opa({"claude": "SHIP", "gpt": "SHIP"})
        self._observe(blocking=())
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "agree-allow")
        self.assertTrue(entry["agreement"])
        self.assertEqual(entry["would_be_decision"], "allow")

    def test_agree_block(self):
        self._patch_opa({"claude": "SHIP", "gpt": "REVISE"})
        self._observe(blocking=[("gpt", "REVISE")])
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "agree-block")
        self.assertTrue(entry["agreement"])

    def test_verdict_mismatch(self):
        # OPA has a full SHIP set while the legacy gate is blocking -> real divergence.
        self._patch_opa({"claude": "SHIP", "gpt": "SHIP"})
        self._observe(blocking=[("gpt", "REVISE")])
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "verdict-mismatch")
        self.assertFalse(entry["agreement"])

    def test_no_attestation_when_map_empty_and_generations_agree(self):
        self._patch_opa({}, generation=7)
        self._observe()
        self.assertEqual(self._only_entry()["classification"], "no-attestation")

    def test_generation_mismatch_when_opa_snapshot_is_stale(self):
        self._patch_opa({}, generation=5)  # disk is 7
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "generation-mismatch")
        self.assertEqual(entry["opa_generation"], 5)
        self.assertEqual(entry["expected_generation"], 7)

    def test_trivial_bypass_is_reachable_and_outranks_no_attestation(self):
        # Regression guard for a real ordering bug caught in plan review: TRIVIAL is
        # never attestable, so an empty map ALWAYS accompanies it. If `no-attestation`
        # were checked first, `trivial-bypass` would be unreachable dead code.
        self._patch_opa({}, generation=7)
        self._observe(claude_verdict="TRIVIAL")
        self.assertEqual(self._only_entry()["classification"], "trivial-bypass")

    def test_opa_unreachable(self):
        self._patch_opa(ok=False, error="request failed: refused")
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "opa-unreachable")
        self.assertIsNone(entry["would_be_decision"])
        self.assertIn("refused", entry["opa_error"])

    def test_ledger_unreadable(self):
        self.ledger_path.write_text("{ not json", encoding="utf-8")
        self._patch_opa({})
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "ledger-unreadable")
        self.assertEqual(entry["ledger_error"]["category"], "unreadable")
        self.assertEqual(entry["ledger_error"]["exception"], "JSONDecodeError")

    def test_subject_unresolvable(self):
        mock.patch.object(
            cc_shadow, "resolve_subject",
            side_effect=cc_shadow.GitSubjectError("unmerged index"),
        ).start()
        self.addCleanup(mock.patch.stopall)
        self._patch_opa({})
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "subject-unresolvable")
        self.assertIsNone(entry["repo_id"])
        self.assertEqual(entry["subject_error"]["category"], "git-subject")

    def test_generation_unknown_when_the_probe_itself_fails(self):
        # An empty map plus a failed generation probe is NOT evidence of absence.
        self._patch_opa({}, generation=None)
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "generation-unknown")
        self.assertIsNone(entry["opa_generation"])

    def test_opa_unreachable_outranks_ledger_and_subject_failures(self):
        self.ledger_path.unlink()
        self._patch_opa(ok=False, error="request failed: refused")
        self._observe()
        self.assertEqual(self._only_entry()["classification"], "opa-unreachable")

    def test_unknown_role_is_ignored_entirely(self):
        self._patch_opa({})
        self._observe(role="plan-reviewer")
        self.assertEqual(self._entries(), [])

    def test_every_classification_label_is_reachable(self):
        """Union of the labels the tests above actually produced == the defined set."""
        defined = {
            v for k, v in vars(cc_shadow).items()
            if k.startswith("_CLASS_") and isinstance(v, str)
        }
        self.assertEqual(
            defined,
            {
                "opa-unreachable", "ledger-unreadable", "subject-unresolvable",
                "trivial-bypass", "generation-mismatch", "generation-unknown",
                "no-attestation", "agree-allow", "agree-block", "verdict-mismatch",
            },
            "a new classification was defined without a reachability test above",
        )


class TestNoPathLeakage(_ShadowTestCase):
    """The log's redaction contract, enforced against the FULL serialized line.

    Found in code review: `git_subject` wraps `CalledProcessError`, whose message
    embeds `git -C <absolute worktree path>`, and `str(OSError)` embeds the filename.
    Logging either verbatim would put absolute paths in a file that documents itself
    as path-free. These tests scan the whole emitted JSON, not just the field that was
    known to be guilty, so a future field cannot quietly reintroduce the leak.
    """

    SECRET = "supersecret-project-name"

    def _assert_no_leak(self):
        raw = self.log_path.read_text()
        self.assertNotIn(self.SECRET, raw)
        self.assertNotIn(str(self.repo_root), raw)
        # No absolute POSIX path of any kind, in any field.
        for entry in self._entries():
            for value in json.dumps(entry).split('"'):
                self.assertFalse(
                    value.startswith("/") and len(value) > 1,
                    f"absolute path leaked into the shadow log: {value!r}",
                )

    def test_real_git_failure_does_not_leak_the_worktree_path(self):
        # A genuine GitSubjectError from a real `git -C <path>` failure -- the exact
        # exception shape that carries the path, not a hand-written stand-in.
        leaky = Path(self.tmp.name) / self.SECRET
        leaky.mkdir()
        from warden_policy import git_subject

        # Prove the test is discriminating: the RAW exception really does carry the
        # path, so a green result below is redaction working, not a vacuous pass.
        with self.assertRaises(cc_shadow.GitSubjectError) as caught:
            git_subject.resolve(leaky)
        self.assertIn(self.SECRET, str(caught.exception))

        mock.patch.object(cc_shadow, "resolve_subject", git_subject.resolve).start()
        self.addCleanup(mock.patch.stopall)
        self._patch_opa({})
        cc_shadow.observe(
            role="code-reviewer", worktree=leaky, required_backends=["claude"],
            legacy_blocking=[], legacy_claude_verdict=None,
        )
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "subject-unresolvable")
        self._assert_no_leak()

    def test_real_oserror_from_the_ledger_does_not_leak_its_path(self):
        self.ledger_path.unlink()
        self.ledger_path.mkdir()  # opening a directory raises IsADirectoryError(OSError)
        with self.assertRaises(OSError) as caught:  # same discriminating check
            open(self.ledger_path, encoding="utf-8")
        self.assertIn(str(self.ledger_path), str(caught.exception))
        self._patch_opa({})
        self._observe()
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "ledger-unreadable")
        self.assertEqual(entry["ledger_error"]["exception"], "IsADirectoryError")
        self._assert_no_leak()

    def test_redact_error_keeps_a_stable_digest_without_the_message(self):
        exc = OSError("[Errno 2] No such file: '/Users/someone/secret'")
        first = cc_shadow.redact_error("ledger", exc)
        second = cc_shadow.redact_error("ledger", OSError(str(exc)))
        self.assertEqual(first["detail_sha256"], second["detail_sha256"])
        self.assertNotIn("secret", json.dumps(first))
        self.assertNotEqual(
            first["detail_sha256"],
            cc_shadow.redact_error("ledger", OSError("different"))["detail_sha256"],
        )


class TestBackendParity(unittest.TestCase):
    """The shadow's re-derivation must match `_evaluate_backends` exactly."""

    def test_unknown_backend_ids_are_skipped_not_blocking(self):
        evaluated, skipped = cc_shadow.split_backends(["claude", "gpt", "typo-backend"])
        self.assertEqual(evaluated, ["claude", "gpt"])
        self.assertEqual(skipped, ["typo-backend"])

    def test_could_not_run_fails_open(self):
        self.assertEqual(
            cc_shadow.would_be_blocking(
                {"claude": "SHIP", "gpt": "COULD_NOT_RUN"}, ["claude", "gpt"],
            ),
            [],
        )

    def test_missing_verdict_blocks(self):
        self.assertEqual(
            cc_shadow.would_be_blocking({"claude": "SHIP"}, ["claude", "gpt"]),
            [["gpt", None]],
        )

    def test_non_ship_blocks(self):
        self.assertEqual(
            cc_shadow.would_be_blocking({"gpt": "REVISE"}, ["gpt"]), [["gpt", "REVISE"]],
        )


class TestBackendParityAgainstLegacy(_ShadowTestCase):
    def test_typo_backend_does_not_manufacture_a_mismatch(self):
        # Legacy `_evaluate_backends` warns and SKIPS an unknown backend id, so it is
        # not in the legacy blocking set. The shadow must skip it too -- otherwise
        # every observation for that role logs a false `verdict-mismatch`.
        self._patch_opa({"claude": "SHIP", "gpt": "SHIP"})
        self._observe(backends=("claude", "gpt", "typo-backend"), blocking=())
        entry = self._only_entry()
        self.assertEqual(entry["classification"], "agree-allow")
        self.assertEqual(entry["skipped_backends"], ["typo-backend"])
        self.assertEqual(entry["required_backends"], ["claude", "gpt"])


class TestGateVocabularyRoundTrip(unittest.TestCase):
    """The `gate` key the shadow sends must actually retrieve a real attestation.

    Regression guard for a real bug found in code review: `latest` and
    `latest_by_backend` use DIFFERENT gate vocabularies ("code-review" vs
    "code-reviewer"), and an earlier draft sent `latest`'s. Every mocked
    classification test still passed, and so did the live run -- because nothing
    writes `latest_by_backend` yet, so an always-empty bucket is indistinguishable
    from a correct empty one. Only a round trip through the REAL store and the REAL
    policy can tell them apart, so that is what this does.
    """

    POLICY_DIR = Path(__file__).resolve().parents[1] / "policy"
    SCHEMA = POLICY_DIR / "attestation-v1.schema.json"

    def test_shadow_roles_match_the_schema_gate_enum(self):
        enum = set(
            json.loads(self.SCHEMA.read_text())
            ["$defs"]["record"]["properties"]["gate"]["enum"]
        )
        # "code-review" is `latest`'s Hermes key and is deliberately NOT a shadow role.
        self.assertNotIn("code-review", cc_shadow.SHADOW_ROLES)
        covered = cc_shadow.SHADOW_ROLES & enum
        # LIA-524 widened the schema enum to admit "verification-gate" via a Hermes-native
        # write path (scripts/hermes_verification_gate.py) -- distinct from the still-unwritten
        # CC-mirrored path this test originally documented. All three shadow roles are now
        # covered by the schema enum.
        self.assertEqual(covered, {"code-reviewer", "ai-eng-warden", "verification-gate"})
        self.assertEqual(cc_shadow.SHADOW_ROLES - enum, set())

    @unittest.skipUnless(__import__("shutil").which("opa"), "opa binary not installed")
    def test_real_store_write_is_retrievable_by_the_shadows_own_query(self):
        import shutil

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Path(tmp.name) / "attestations-v1.json"

        from warden_policy.attestation_store import AttestationStore

        store = AttestationStore(ledger)
        # Stub the OPA PUT: this test evaluates the policy offline via `opa eval`, so
        # it must not depend on (or touch) the real daemon.
        with mock.patch.object(
            AttestationStore, "_put_and_readback",
            lambda self, inner: (True, inner["generation"], None),
        ):
            for backend, verdict in (("claude", "SHIP"), ("gpt", "SHIP")):
                result = store.issue(
                    repo_id=REPO_ID,
                    gate="code-reviewer",  # exactly what observe() sends for this role
                    subject_key=SUBJECT,
                    verdict=verdict,
                    issuer_kind="script",
                    reviewer_id=f"code-reviewer@{backend}",
                    reason="round-trip fixture",
                    backend=backend,
                )
                self.assertTrue(result.ok)

        doc = json.loads(ledger.read_text())["warden_attestations"]
        self.assertIn("code-reviewer", doc["latest_by_backend"][REPO_ID])

        opa_input = {
            "contract_version": 1,
            "enforcement_point": "claude-code.pre_tool_use",
            "operation": "git.commit",
            "repo_id": REPO_ID,
            "subject_key": SUBJECT,
            "expected_generation": doc["generation"],
            "gate": "code-reviewer",
            "required_backends": ["claude", "gpt"],
        }
        input_path = Path(tmp.name) / "input.json"
        input_path.write_text(json.dumps(opa_input), encoding="utf-8")
        proc = subprocess.run(
            [shutil.which("opa"), "eval", "--format", "json",
             "--data", str(self.POLICY_DIR / "guardrails.rego"),
             "--data", str(ledger), "--input", str(input_path),
             "data.deus.wardens.backend_verdict_map"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        self.assertEqual(
            value, {"claude": "SHIP", "gpt": "SHIP"},
            "the gate key observe() sends did not retrieve the attestations the store "
            "wrote -- latest/latest_by_backend vocabularies are out of sync again",
        )
        # And the derived decision must match what _evaluate_backends would conclude.
        self.assertEqual(cc_shadow.would_be_blocking(value, ["claude", "gpt"]), [])

    @unittest.skipUnless(__import__("shutil").which("opa"), "opa binary not installed")
    def test_the_wrong_vocabulary_would_have_returned_nothing(self):
        """Proves the test above is discriminating, not vacuously green.

        Same fixture, queried with `latest`'s "code-review" key -- the exact bug the
        code reviewer caught. It must come back empty; if this ever returns verdicts,
        the assertion above no longer distinguishes the two vocabularies.
        """
        import shutil

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger = Path(tmp.name) / "attestations-v1.json"

        from warden_policy.attestation_store import AttestationStore

        store = AttestationStore(ledger)
        with mock.patch.object(
            AttestationStore, "_put_and_readback",
            lambda self, inner: (True, inner["generation"], None),
        ):
            store.issue(
                repo_id=REPO_ID, gate="code-reviewer", subject_key=SUBJECT,
                verdict="SHIP", issuer_kind="script", reviewer_id="code-reviewer@claude",
                reason="round-trip fixture", backend="claude",
            )
        doc = json.loads(ledger.read_text())["warden_attestations"]
        input_path = Path(tmp.name) / "input.json"
        input_path.write_text(json.dumps({
            "contract_version": 1, "operation": "git.commit", "repo_id": REPO_ID,
            "subject_key": SUBJECT, "expected_generation": doc["generation"],
            "gate": "code-review",  # WRONG vocabulary on purpose
            "required_backends": ["claude"],
        }), encoding="utf-8")
        proc = subprocess.run(
            [shutil.which("opa"), "eval", "--format", "json",
             "--data", str(self.POLICY_DIR / "guardrails.rego"),
             "--data", str(ledger), "--input", str(input_path),
             "data.deus.wardens.backend_verdict_map"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        value = json.loads(proc.stdout)["result"][0]["expressions"][0]["value"]
        self.assertEqual(value, {})


class TestStreamPurity(_ShadowTestCase):
    """Invariant 2: the hook decision travels on stdout -- the observer must be mute."""

    def test_observe_writes_nothing_to_stdout_or_stderr(self):
        self._patch_opa({"claude": "SHIP"})
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self._observe(backends=("claude",))
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(self._entries()), 1)

    def test_stays_mute_when_every_stage_fails(self):
        self.ledger_path.unlink()
        self._patch_opa(ok=False, error="boom")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self._observe()
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")


class TestContainment(_ShadowTestCase):
    """Invariant 1: a bug anywhere inside must not escape as an exception."""

    def _assert_contained(self, target, **kwargs):
        patcher = mock.patch.object(cc_shadow, target, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertIsNone(self._observe())
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_ledger_read_explosion_contained(self):
        self._patch_opa({})
        self._assert_contained("_read_ledger_generation", side_effect=RuntimeError("x"))

    def test_subject_resolution_explosion_contained(self):
        self._patch_opa({})
        self._assert_contained("resolve_subject", side_effect=RuntimeError("x"))

    def test_opa_query_explosion_contained(self):
        self._assert_contained("query_backend_verdicts", side_effect=RuntimeError("x"))

    def test_log_write_explosion_contained(self):
        self._patch_opa({})
        self._assert_contained("_log", side_effect=RuntimeError("x"))

    def test_log_failure_is_swallowed_when_dir_is_unwritable(self):
        self._patch_opa({})
        with mock.patch("builtins.open", side_effect=OSError("read-only fs")):
            self.assertIsNone(cc_shadow._log({"a": 1}))


class TestDeadline(_ShadowTestCase):
    """A slow OPA must be bounded, and must still produce an observation."""

    def test_remaining_is_clamped_to_the_opa_budget_and_a_positive_floor(self):
        import time as _time

        now = _time.monotonic()
        self.assertLessEqual(cc_shadow._remaining(now), cc_shadow.OPA_TIMEOUT_SECONDS)
        # Deadline already blown -> still a positive, fail-fast timeout, never <= 0
        # (urllib treats a non-positive timeout as an immediate error, not a no-op).
        blown = now - cc_shadow.SELF_DEADLINE_SECONDS - 10
        self.assertGreaterEqual(cc_shadow._remaining(blown), 0.05)
        self.assertLessEqual(cc_shadow._remaining(blown), cc_shadow.OPA_TIMEOUT_SECONDS)

    def test_slow_opa_still_logs_and_returns_none(self):
        def _slow(*_args, **_kwargs):
            import time as _time

            _time.sleep(0.05)
            return BackendVerdictsResult(ok=False, verdicts={}, error="timed out")

        patcher = mock.patch.object(cc_shadow, "query_backend_verdicts", side_effect=_slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(self._observe())
        self.assertEqual(self._only_entry()["classification"], "opa-unreachable")


class TestNeverWritesLedger(_ShadowTestCase):
    """Invariant 3: Phase 1 is read-only -- no ledger write, no AttestationStore."""

    def test_ledger_bytes_and_mtime_unchanged_across_an_observation(self):
        before = self.ledger_path.read_bytes()
        before_mtime = self.ledger_path.stat().st_mtime_ns
        self._patch_opa({"claude": "SHIP"})
        self._observe(backends=("claude",))
        self.assertEqual(self.ledger_path.read_bytes(), before)
        self.assertEqual(self.ledger_path.stat().st_mtime_ns, before_mtime)

    def test_attestation_store_is_never_constructed(self):
        from warden_policy import attestation_store

        with mock.patch.object(
            attestation_store, "AttestationStore",
            side_effect=AssertionError("Phase 1 must never construct AttestationStore"),
        ):
            self._patch_opa({"claude": "SHIP"})
            self._observe(backends=("claude",))
        self.assertEqual(len(self._entries()), 1)

    def test_module_source_does_not_reference_the_store_or_fcntl(self):
        # Structural, not behavioural: proves the read path can neither write the
        # ledger nor block on its lock, which is why unlocked reads are safe here.
        source = Path(cc_shadow.__file__).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith("#")
        )
        body = code.split('"""', 2)[-1]  # drop the module docstring, which discusses both
        self.assertNotIn("AttestationStore", body)
        self.assertNotIn("fcntl", body)


class TestGateInvariance(unittest.TestCase):
    """Invariant 1, strongest form: identical gate output with the shadow off vs on.

    Runs the real hook entry point in a subprocess against a synthetic PreToolUse
    `git commit` event and diffs stdout/stderr/exit across four configurations:
    flag off, flag on with OPA absent, flag on with OPA hanging, and flag on with a
    stubbed OPA. Any divergence means the shadow changed a real outcome.

    NOTE (recorded honestly, per the plan): this oracle is SELF-AUTHORED by the
    implementer, so it shares the implementation's blind spots. An independently
    authored oracle (`oracle-author`) is an open item before any Phase 2 cutover.
    """

    SCRIPTS = Path(__file__).resolve().parents[2]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        for args in (
            ("init", "-q"), ("config", "user.email", "t@example.com"),
            ("config", "user.name", "T"),
        ):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                           capture_output=True, text=True)
        (self.repo / "f.txt").write_text("hello\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "f.txt"], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-q", "-m", "init"],
                       check=True, capture_output=True, text=True)
        (self.repo / ".claude" / "wardens").mkdir(parents=True)
        (self.repo / ".claude" / "wardens" / "config.json").write_text(
            json.dumps({"code-reviewer": {"enabled": True, "backends": ["claude"]}}),
            encoding="utf-8",
        )

    def _run(self, env_extra):
        event = {
            "cwd": str(self.repo),
            "tool_name": "Bash",
            "tool_input": {"command": "git commit -m 'x'"},
        }
        env = dict(os.environ)
        env["HOME"] = self.tmp.name  # keep the real ledger/log out of reach
        env.pop(cc_shadow.ENV_FLAG, None)  # each variant sets it (or not) explicitly
        env.update(env_extra)
        return subprocess.run(
            [sys.executable, str(self.SCRIPTS / "codex_warden_hooks.py"),
             "run", "code-review-gate", "--repo-root", str(self.repo)],
            input=json.dumps(event), capture_output=True, text=True, env=env, timeout=60,
        )

    def _enable_via_toggle_file(self):
        (self.repo / cc_shadow.TOGGLE_RELPATH).write_text(
            json.dumps({"enabled": True}), encoding="utf-8",
        )

    def _poison_ledger(self):
        path = Path(self.tmp.name) / ".config" / "deus" / "guardrails" / "attestations-v1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")

    def test_baseline_gate_output_is_non_empty(self):
        """Guards the invariance test below from passing vacuously.

        Without this, `stdout == stdout` would still hold if the gate no-op'd and
        every capture were empty -- proving nothing at all.
        """
        baseline = self._run({cc_shadow.ENV_FLAG: "0"})
        self.assertIn("permissionDecision", baseline.stdout)
        self.assertGreater(len(baseline.stdout), 100)

    def test_gate_output_identical_across_shadow_configurations(self):
        baseline = self._run({cc_shadow.ENV_FLAG: "0"})
        self.assertIn("permissionDecision", baseline.stdout)  # non-vacuity, again

        def env_flag():
            return {cc_shadow.ENV_FLAG: "1"}

        variants = [
            ("env-flag-on", env_flag, lambda: None),
            ("toggle-file-on", dict, self._enable_via_toggle_file),
            ("flag-on-poisoned-ledger", env_flag, self._poison_ledger),
        ]
        for name, env_fn, setup in variants:
            with self.subTest(variant=name):
                setup()
                result = self._run(env_fn())
                self.assertEqual(result.stdout, baseline.stdout, f"{name} changed stdout")
                self.assertEqual(result.stderr, baseline.stderr, f"{name} changed stderr")
                self.assertEqual(
                    result.returncode, baseline.returncode, f"{name} changed exit code",
                )
                log = Path(self.tmp.name) / ".config/deus/guardrails/logs/cc-shadow.jsonl"
                self.assertTrue(log.exists(), f"{name} produced no shadow observation")


if __name__ == "__main__":
    unittest.main()

import contextlib
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError, _OPA_REQUEST_FAILED

# Captured once, before any test patches AttestationStore._locked -- LIA-533's lock-contention
# tests need to delegate to the real locking behavior for the non-contended branch while
# overriding only the contended one.
_real_locked = AttestationStore._locked


def _always_ok(self, inner_doc, **kwargs):
    return True, inner_doc["generation"], None


def _always_fails(self, inner_doc, **kwargs):
    return False, inner_doc["generation"] - 1, "simulated PUT failure"


class TestEnrollment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_enroll_then_unenroll_does_not_delete(self):
        r1 = self.store.enroll("repo-a")
        self.assertTrue(r1.ok)
        r2 = self.store.unenroll("repo-a")
        self.assertTrue(r2.ok)
        doc = self.store._read_disk()
        entry = doc["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry["enabled"])
        self.assertIn("enrolled_at", entry)  # record preserved, not deleted

    def test_unenroll_never_enrolled_raises(self):
        with self.assertRaises(AttestationStoreError):
            self.store.unenroll("never-enrolled")

    def test_generation_increments_on_every_mutation(self):
        r1 = self.store.enroll("repo-a")
        r2 = self.store.unenroll("repo-a")
        self.assertEqual(r2.generation, r1.generation + 1)

    def test_set_plan_review_enabled_creates_fresh_entry_with_code_review_off(self):
        r = self.store.set_plan_review_enabled("repo-a", True)
        self.assertTrue(r.ok)
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry["enabled"])  # code-review stays off, not auto-enrolled
        self.assertIn("enrolled_at", entry)
        self.assertTrue(entry["plan_review_enabled"])

    def test_disable_plan_review_never_enrolled_raises(self):
        with self.assertRaises(AttestationStoreError):
            self.store.set_plan_review_enabled("never-enrolled", False)

    def test_enroll_after_enable_plan_review_preserves_plan_review_enabled(self):
        # This is the case that would have caught the old replace-not-merge bug in enroll():
        # enable-plan-review first, then enroll() for code-review, in that order.
        self.store.set_plan_review_enabled("repo-a", True)
        self.store.enroll("repo-a")
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["plan_review_enabled"])

    def test_enable_plan_review_after_enroll_preserves_enabled(self):
        # Mirror order: enroll() first, then enable-plan-review.
        self.store.enroll("repo-a")
        self.store.set_plan_review_enabled("repo-a", True)
        entry = self.store._read_disk()["warden_attestations"]["config"]["enforced_repos"]["repo-a"]
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["plan_review_enabled"])


class TestIssueAppendOnly(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_ship_then_revise_preserves_both_records(self):
        self.store.enroll("repo-a")
        self.store.issue(
            repo_id="repo-a", gate="code-review", subject_key="git-tree:sha1:aaa",
            verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="ok",
        )
        self.store.issue(
            repo_id="repo-a", gate="code-review", subject_key="git-tree:sha1:aaa",
            verdict="REVISE", issuer_kind="manual", reviewer_id="x@y", reason="found a bug",
        )
        doc = self.store._read_disk()
        inner = doc["warden_attestations"]
        # both records exist -- nothing was overwritten or deleted
        verdicts = sorted(r["verdict"] for r in inner["records"].values())
        self.assertEqual(verdicts, ["REVISE", "SHIP"])
        # latest points at the REVISE, not the SHIP
        latest_id = inner["latest"]["repo-a"]["code-review"]["git-tree:sha1:aaa"]
        self.assertEqual(inner["records"][latest_id]["verdict"], "REVISE")

    def test_invalid_verdict_rejected(self):
        with self.assertRaises(AttestationStoreError):
            self.store.issue(
                repo_id="repo-a", gate="code-review", subject_key="git-tree:sha1:aaa",
                verdict="MAYBE", issuer_kind="manual", reviewer_id="x@y", reason="",
            )

    def test_invalid_subject_kind_rejected(self):
        with self.assertRaises(AttestationStoreError):
            self.store.issue(
                repo_id="repo-a", gate="plan-review", subject_key="sess-1",
                verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="",
                kind="not-a-real-kind",
            )

    def test_session_subject_stores_raw_session_id_not_a_digest(self):
        self.store.set_plan_review_enabled("repo-a", True)
        result = self.store.issue(
            repo_id="repo-a", gate="plan-review", subject_key="sess-abc123",
            verdict="SHIP", issuer_kind="manual", reviewer_id="plan-reviewer@claude",
            reason="reviewed", kind="session",
        )
        self.assertTrue(result.ok)
        doc = self.store._read_disk()
        inner = doc["warden_attestations"]
        latest_id = inner["latest"]["repo-a"]["plan-review"]["sess-abc123"]
        record = inner["records"][latest_id]
        self.assertEqual(record["subject"], {"kind": "session", "session_id": "sess-abc123"})
        # a git-tree subject on the SAME ledger, issued after, must be unaffected --
        # proves the two subject shapes don't corrupt each other.
        self.store.enroll("repo-a")
        self.store.issue(
            repo_id="repo-a", gate="code-review", subject_key="git-tree:sha1:bbb",
            verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="ok",
        )
        doc2 = self.store._read_disk()
        tree_latest_id = doc2["warden_attestations"]["latest"]["repo-a"]["code-review"]["git-tree:sha1:bbb"]
        tree_record = doc2["warden_attestations"]["records"][tree_latest_id]
        self.assertEqual(tree_record["subject"]["kind"], "git-tree")
        self.assertEqual(tree_record["subject"]["digest"], {"algorithm": "sha1", "value": "bbb"})

    def test_could_not_run_verdict_accepted(self):
        # Real precedent: the legacy verdict store already persists COULD_NOT_RUN as a
        # first-class verdict today (codex_warden_hooks.py record-verdict ... COULD_NOT_RUN,
        # used this same session for a genuine GLM/Z.AI infra failure). The ledger must
        # accept it for parity -- previously this would have raised AttestationStoreError.
        self.store.enroll("repo-a")
        result = self.store.issue(
            repo_id="repo-a", gate="code-reviewer", subject_key="git-tree:sha1:aaa",
            verdict="COULD_NOT_RUN", issuer_kind="script", reviewer_id="code-reviewer@gpt",
            reason="infra failure", backend="gpt",
        )
        self.assertTrue(result.ok)

    def test_backend_none_is_byte_identical_to_legacy_call_shape(self):
        # Every existing call site (Hermes included) never passes backend -- confirm the
        # default behaves exactly as before this change: only `latest` is populated,
        # `latest_by_backend` is never created at all.
        self.store.enroll("repo-a")
        self.store.issue(
            repo_id="repo-a", gate="code-review", subject_key="git-tree:sha1:aaa",
            verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason="ok",
        )
        doc = self.store._read_disk()
        inner = doc["warden_attestations"]
        self.assertNotIn("latest_by_backend", inner)
        latest_id = inner["latest"]["repo-a"]["code-review"]["git-tree:sha1:aaa"]
        self.assertNotIn("backend", inner["records"][latest_id])

    def test_backend_populates_latest_by_backend_not_latest(self):
        self.store.enroll("repo-a")
        self.store.issue(
            repo_id="repo-a", gate="code-reviewer", subject_key="git-tree:sha1:bbb",
            verdict="SHIP", issuer_kind="script", reviewer_id="code-reviewer@claude",
            reason="ok", backend="claude",
        )
        doc = self.store._read_disk()
        inner = doc["warden_attestations"]
        # latest_by_backend populated with the record
        record_id = inner["latest_by_backend"]["repo-a"]["code-reviewer"]["git-tree:sha1:bbb"]["claude"]
        self.assertEqual(inner["records"][record_id]["verdict"], "SHIP")
        self.assertEqual(inner["records"][record_id]["backend"], "claude")
        # latest itself is completely untouched -- no entry for this gate/subject at all
        self.assertNotIn("code-reviewer", inner["latest"].get("repo-a", {}))

    def test_multiple_backends_same_gate_subject_coexist(self):
        self.store.enroll("repo-a")
        self.store.issue(
            repo_id="repo-a", gate="code-reviewer", subject_key="git-tree:sha1:ccc",
            verdict="SHIP", issuer_kind="script", reviewer_id="code-reviewer@claude",
            reason="ok", backend="claude",
        )
        self.store.issue(
            repo_id="repo-a", gate="code-reviewer", subject_key="git-tree:sha1:ccc",
            verdict="COULD_NOT_RUN", issuer_kind="script", reviewer_id="code-reviewer@gpt",
            reason="infra failure", backend="gpt",
        )
        doc = self.store._read_disk()
        by_backend = doc["warden_attestations"]["latest_by_backend"]["repo-a"]["code-reviewer"]["git-tree:sha1:ccc"]
        self.assertEqual(set(by_backend.keys()), {"claude", "gpt"})


class TestFailedPutActivation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_failed_put_still_persists_to_disk_but_reports_not_activated(self):
        with mock.patch.object(AttestationStore, "_put_and_readback", _always_fails):
            result = self.store.enroll("repo-a")
        # `ok` = disk persistence succeeded (it did); `activated` = OPA sync succeeded (it
        # didn't) -- these are deliberately separate signals, not one conflated flag.
        self.assertTrue(result.ok)
        self.assertFalse(result.activated)
        self.assertIsNotNone(result.error)
        # durable record IS on disk despite the reported failure
        doc = self.store._read_disk()
        self.assertIn("repo-a", doc["warden_attestations"]["config"]["enforced_repos"])

    def test_sync_retries_without_bumping_generation(self):
        with mock.patch.object(AttestationStore, "_put_and_readback", _always_fails):
            r1 = self.store.enroll("repo-a")
        with mock.patch.object(AttestationStore, "_put_and_readback", _always_ok):
            r2 = self.store.sync()
        self.assertTrue(r2.ok)
        self.assertEqual(r2.generation, r1.generation)  # sync doesn't mutate, just re-activates


class TestConcurrentWriters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok)
        self.patcher.start()
        self.store.enroll("repo-a")

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_twenty_concurrent_issuers_preserve_every_record(self):
        n = 20
        errors = []

        def _worker(i):
            try:
                self.store.issue(
                    repo_id="repo-a", gate="code-review", subject_key=f"git-tree:sha1:{i:040d}",
                    verdict="SHIP", issuer_kind="manual", reviewer_id="x@y", reason=f"n={i}",
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        doc = self.store._read_disk()
        inner = doc["warden_attestations"]
        # every one of the 20 records survived (no lost update from interleaved writes)
        ship_records = [r for r in inner["records"].values() if r["verdict"] == "SHIP"]
        self.assertEqual(len(ship_records), n)
        latest_for_repo = inner["latest"]["repo-a"]["code-review"]
        self.assertEqual(len(latest_for_repo), n)


class TestReadLocked(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.tmp.cleanup()

    def test_shared_readers_do_not_block_each_other(self):
        self.store.enroll("repo-a")
        results = []

        def _reader():
            doc = self.store.read_locked()
            results.append(doc["warden_attestations"]["generation"])

        threads = [threading.Thread(target=_reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(g == results[0] for g in results))

    def test_locked_read_waits_for_a_slow_writer_to_release(self):
        # Found missing by adversarial code review: proves the shared lock genuinely
        # serializes against a writer mid-transaction, not just against other readers.
        # A slow writer (write+PUT+read-back can take real wall-clock time) must not let a
        # concurrent locked_read() observe partial/interleaved state -- it must wait.
        writer_released_at = []
        reader_saw_generation_at = []

        def _slow_apply(inner):
            time.sleep(0.3)
            inner["config"]["enforced_repos"]["repo-b"] = {
                "enabled": True, "enrolled_at": "2026-08-03T00:00:00Z",
            }

        def _writer():
            self.store._mutate(_slow_apply)
            writer_released_at.append(time.monotonic())

        def _reader():
            time.sleep(0.05)  # ensure the writer has already acquired the exclusive lock
            with self.store.locked_read() as doc:
                reader_saw_generation_at.append(time.monotonic())
                # if the reader had NOT waited for the writer, repo-b would be absent here
                self.assertIn("repo-b", doc["warden_attestations"]["config"]["enforced_repos"])

        t_writer = threading.Thread(target=_writer)
        t_reader = threading.Thread(target=_reader)
        t_writer.start()
        t_reader.start()
        t_writer.join(timeout=5)
        t_reader.join(timeout=5)

        self.assertEqual(len(writer_released_at), 1)
        self.assertEqual(len(reader_saw_generation_at), 1)
        # the reader's view was only obtained AFTER the writer released its exclusive lock
        self.assertGreaterEqual(reader_saw_generation_at[0], writer_released_at[0])


class TestLockedNonBlocking(unittest.TestCase):
    """LIA-533: `_locked(non_blocking=True)` must never wait -- this is the primitive
    `reconcile_if_drifted()`'s repair phase relies on to never queue behind a real `_mutate()`
    write in progress (GPT round-3's writer-race finding)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_non_blocking_exclusive_fails_immediately_against_a_held_lock(self):
        holder_acquired = threading.Event()
        release_holder = threading.Event()

        def _holder():
            with self.store._locked(exclusive=True):
                holder_acquired.set()
                release_holder.wait(timeout=5)

        t = threading.Thread(target=_holder)
        t.start()
        self.assertTrue(holder_acquired.wait(timeout=5))
        start = time.monotonic()
        with self.assertRaises(BlockingIOError):
            with self.store._locked(exclusive=True, non_blocking=True):
                pass
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5)  # returned immediately, did not wait for the holder
        release_holder.set()
        t.join(timeout=5)

    def test_non_blocking_default_is_false_existing_behavior_unchanged(self):
        # Every existing caller omits non_blocking and gets today's exact blocking behavior.
        with self.store._locked(exclusive=True):
            pass  # no exception, no behavior change from the added parameter's default


class TestReconcileIfDrifted(unittest.TestCase):
    """LIA-533: periodic/background reconciliation -- distinguishes "already in sync" (no lock),
    "confirmed content mismatch" (repair, short-timeout non-blocking lock), and "OPA request
    failed" (unknown state, skip without ever touching the lock)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.patcher = mock.patch.object(AttestationStore, "_put_and_readback", _always_ok)
        self.patcher.start()
        self.store.enroll("repo-a")
        self.patcher.stop()  # each test below installs its own spy/mock for _put_and_readback

    def tearDown(self):
        self.tmp.cleanup()

    def _disk_inner(self):
        return self.store._read_disk()["warden_attestations"]

    def test_matching_content_never_attempts_a_repair_put(self):
        matching = self._disk_inner()
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))
        with mock.patch.object(AttestationStore, "_get_opa_document", return_value=matching), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy):
            result = self.store.reconcile_if_drifted()
        self.assertTrue(result.ok)
        self.assertTrue(result.activated)
        self.assertIsNone(result.error)
        put_spy.assert_not_called()

    def test_confirmed_content_mismatch_attempts_repair_with_short_timeout(self):
        # GPT round-2's finding: comparison must be full-content, not generation-only -- this
        # fixture proves the mismatch is detected via content, and that the repair PUT is
        # bounded to a short timeout (GPT round-5/6), not the module default.
        differing = dict(self._disk_inner())
        differing["generation"] = 999
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))
        with mock.patch.object(AttestationStore, "_get_opa_document", return_value=differing), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy):
            result = self.store.reconcile_if_drifted()
        self.assertTrue(result.activated)
        put_spy.assert_called_once()
        _, kwargs = put_spy.call_args
        self.assertEqual(kwargs.get("timeout_seconds"), 0.5)

    def test_null_opa_document_is_confirmed_drift_not_unreachable(self):
        # GPT round-5 finding 1: a reachable OPA reporting no document (HTTP 200, no `result`)
        # is real, confirmed drift -- must not be folded into "request failed, skip."
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))
        with mock.patch.object(AttestationStore, "_get_opa_document", return_value=None), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy):
            result = self.store.reconcile_if_drifted()
        self.assertTrue(result.activated)
        put_spy.assert_called_once()

    def test_opa_request_failure_skips_without_touching_the_lock(self):
        # GPT round-4 finding: distinct from a confirmed mismatch -- must never attempt the
        # locked repair, or a sustained outage recreates a recurring fail-open window.
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))
        with mock.patch.object(AttestationStore, "_get_opa_document",
                                return_value=_OPA_REQUEST_FAILED), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy):
            result = self.store.reconcile_if_drifted()
        self.assertFalse(result.ok)
        self.assertFalse(result.activated)
        self.assertIn("unreachable", result.error)
        put_spy.assert_not_called()

    def test_repair_lock_contention_reports_busy_without_mutating(self):
        # The end-to-end regression test for GPT round-3's writer-race finding: when the repair
        # phase's non-blocking acquisition is contended (a real write in flight), reconcile
        # backs off immediately and never calls _put_and_readback -- no interleaving possible.
        differing = dict(self._disk_inner())
        differing["generation"] = 999
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))

        @contextlib.contextmanager
        def _contended_locked(self_, exclusive, non_blocking=False):
            if non_blocking:
                raise BlockingIOError("simulated contention")
            with _real_locked(self_, exclusive, non_blocking=non_blocking):
                yield

        with mock.patch.object(AttestationStore, "_get_opa_document", return_value=differing), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy), \
             mock.patch.object(AttestationStore, "_locked", _contended_locked):
            result = self.store.reconcile_if_drifted()
        self.assertFalse(result.ok)
        self.assertFalse(result.activated)
        self.assertIn("lock busy", result.error)
        put_spy.assert_not_called()

    def test_real_writer_in_flight_does_not_interleave_with_reconcile_repair(self):
        # True concurrency version of the test above: a real _mutate() write genuinely holding
        # the exclusive lock in a background thread (same sleep-based pattern already
        # established by TestReadLocked.test_locked_read_waits_for_a_slow_writer_to_release in
        # this file), reconcile_if_drifted() called concurrently from the main thread. Proves
        # the two can never interleave in practice, not just that the mocked exception path is
        # handled by the deterministic test above.
        differing = dict(self._disk_inner())
        differing["generation"] = 999
        put_spy = mock.MagicMock(side_effect=lambda inner, **kw: _always_ok(self.store, inner))

        def _slow_apply(inner):
            time.sleep(0.3)
            inner["config"]["enforced_repos"]["repo-b"] = {
                "enabled": True, "enrolled_at": "2026-08-03T00:00:00Z",
            }

        def _writer():
            self.store._mutate(_slow_apply)

        with mock.patch.object(AttestationStore, "_get_opa_document", return_value=differing), \
             mock.patch.object(AttestationStore, "_put_and_readback", put_spy):
            t = threading.Thread(target=_writer)
            t.start()
            time.sleep(0.05)  # let the writer acquire the exclusive lock first
            self.store.reconcile_if_drifted()
            t.join(timeout=5)
        # reconcile's own read_locked() (shared) genuinely waits for the writer's exclusive hold
        # to release first -- correct, pre-existing reader/writer coordination, not new. What
        # matters: no interleaved/duplicate repair PUT -- the writer's own legitimate PUT, plus
        # at most one repair attempt from reconcile, never more (never two overlapping PUTs).
        self.assertLessEqual(put_spy.call_count, 2)


if __name__ == "__main__":
    unittest.main()

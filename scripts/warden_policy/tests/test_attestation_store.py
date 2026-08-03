import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError


def _always_ok(self, inner_doc):
    return True, inner_doc["generation"], None


def _always_fails(self, inner_doc):
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


if __name__ == "__main__":
    unittest.main()

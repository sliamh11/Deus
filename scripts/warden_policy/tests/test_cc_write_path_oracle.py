import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden_policy.attestation_store import (
    AttestationStore,
    AttestationStoreError,
    WriteResult,
)


CC_DOCUMENT_KEY = "warden_cc_attestations"
DEFAULT_DOCUMENT_KEY = "warden_attestations"
CC_OPA_DATA_PATH = "/v1/data/warden_cc_attestations"
DEFAULT_OPA_DATA_PATH = "/v1/data/warden_attestations"


class _StubResponse:
    def __init__(self, payload: bytes = b""):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return self._payload


class _OpaRecorder:
    """Minimal urlopen replacement that records the real Request target."""

    def __init__(self):
        self.requests: list[tuple[str, str]] = []
        self.generation: int | None = None

    def __call__(self, request, timeout):
        method = request.get_method()
        self.requests.append((method, request.full_url))
        if method == "PUT":
            self.generation = json.loads(request.data)["generation"]
            return _StubResponse()
        return _StubResponse(json.dumps({"result": self.generation}).encode("utf-8"))


def _persisted_but_not_activated(self, inner_doc):
    return False, None, "simulated OPA outage"


def _job_should_be_deleted(result: WriteResult) -> bool:
    # Stand-in for the Phase 2 worker branch, which does not exist yet. The worker must
    # delete a queued job based only on disk persistence (`ok`), never OPA activation.
    # TODO(LIA-527 implementation): once scripts/warden_policy/cc_attestations.py's worker
    # exists, replace this stand-in with the real predicate/call site so these tests
    # exercise production code, not a hand-written proxy for its intended behavior.
    return result.ok is True


class TestCcDocumentIsolationOracle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger_path = Path(self.tmp.name) / "attestations-cc-v1.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _new_store(self, **kwargs) -> AttestationStore:
        return AttestationStore(
            self.ledger_path,
            opa_base_url="http://opa.test",
            document_key=CC_DOCUMENT_KEY,
            **kwargs,
        )

    def _assert_isolated_ledger(self) -> dict:
        contents = self.ledger_path.read_text(encoding="utf-8")
        document = json.loads(contents)
        self.assertEqual(set(document), {CC_DOCUMENT_KEY})  # @oracle LIA-527: disk root is the isolated CC document
        self.assertNotIn(DEFAULT_DOCUMENT_KEY, document)  # @oracle LIA-527: disk root never aliases Hermes's live document
        self.assertNotIn(f'"{DEFAULT_DOCUMENT_KEY}"', contents)  # @oracle LIA-527: written JSON never contains the Hermes document key
        return document

    def test_custom_document_key_derives_isolated_opa_path_and_empty_document(self):
        store = self._new_store()

        self.assertEqual(store.document_key, CC_DOCUMENT_KEY)  # @oracle LIA-527: constructor retains the requested document key
        self.assertEqual(store.opa_data_path, CC_OPA_DATA_PATH)  # @oracle LIA-527: OPA path derives from the requested document key
        empty = store.read_locked()
        self.assertEqual(set(empty), {CC_DOCUMENT_KEY})  # @oracle LIA-527: empty document creation uses the isolated root key
        self.assertNotIn(DEFAULT_DOCUMENT_KEY, empty)  # @oracle LIA-527: empty CC documents never expose the Hermes root key

    def test_explicit_opa_data_path_override_is_retained(self):
        explicit_path = "/v1/data/cc-test-override"
        store = self._new_store(opa_data_path=explicit_path)
        recorder = _OpaRecorder()

        with mock.patch(
            "warden_policy.attestation_store.urllib.request.urlopen",
            side_effect=recorder,
        ):
            store.enroll("repo-a")

        self.assertEqual(store.opa_data_path, explicit_path)  # @oracle LIA-527: explicit OPA data paths override the derived path
        self.assertEqual(recorder.requests[0], ("PUT", "http://opa.test" + explicit_path))  # @oracle LIA-527: explicit OPA paths control the actual PUT target

    def test_isolated_store_puts_to_cc_document_never_hermes_document(self):
        store = self._new_store()
        recorder = _OpaRecorder()

        with mock.patch(
            "warden_policy.attestation_store.urllib.request.urlopen",
            side_effect=recorder,
        ):
            result = store.enroll("repo-a")

        put_targets = [url for method, url in recorder.requests if method == "PUT"]
        self.assertTrue(result.activated)  # @oracle LIA-527: the intercepted PUT/read-back transaction completes
        self.assertEqual(put_targets, ["http://opa.test" + CC_OPA_DATA_PATH])  # @oracle LIA-527: isolated writes target only the CC OPA document
        self.assertNotIn("http://opa.test" + DEFAULT_OPA_DATA_PATH, put_targets)  # @oracle LIA-527: isolated writes never overwrite Hermes's live OPA document
        self.assertEqual(recorder.requests[1], ("GET", "http://opa.test" + CC_OPA_DATA_PATH + "/generation"))  # @oracle LIA-527: generation read-back uses the isolated CC OPA document

    def test_all_public_store_paths_keep_cc_ledger_isolated(self):
        store = self._new_store()

        with mock.patch.object(
            AttestationStore,
            "_put_and_readback",
            lambda self, inner_doc: (True, inner_doc["generation"], None),
        ):
            enrolled = store.enroll("repo-a")
            self.assertTrue(enrolled.ok)  # @oracle LIA-527: enroll succeeds through the isolated mutation path
            self._assert_isolated_ledger()

            issued = store.issue(
                repo_id="repo-a",
                gate="code-reviewer",
                subject_key="git-tree:sha1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                verdict="SHIP",
                issuer_kind="script",
                reviewer_id="code-reviewer@claude",
                reason="oracle fixture",
                backend="claude",
            )
            self.assertTrue(issued.ok)  # @oracle LIA-527: issue re-reads and mutates the isolated ledger
            self._assert_isolated_ledger()

            synced = store.sync()
            self.assertTrue(synced.ok)  # @oracle LIA-527: sync reads the isolated root and activates it
            self._assert_isolated_ledger()

            records = store.inspect("repo-a")
            self.assertEqual(len(records), 1)  # @oracle LIA-527: inspect reads records from the isolated root
            self.assertEqual(records[0]["repo_id"], "repo-a")  # @oracle LIA-527: inspect reports the isolated ledger's record
            self._assert_isolated_ledger()

            unenrolled = store.unenroll("repo-a")
            self.assertTrue(unenrolled.ok)  # @oracle LIA-527: unenroll mutates the isolated ledger
            document = self._assert_isolated_ledger()

        entry = document[CC_DOCUMENT_KEY]["config"]["enforced_repos"]["repo-a"]
        self.assertFalse(entry["enabled"])  # @oracle LIA-527: unenroll updates the CC document rather than Hermes's document


class TestCcWriteResultDeletionOracle(unittest.TestCase):
    # Note: the real-call-site tests below (test_process_job_*) only ever exercise the
    # ok=True/persisted path against the actual `process_job`. The ok=False/"genuinely failed"
    # branch the design's "Public interface" section also names is only exercised against the
    # `_job_should_be_deleted` stand-in just below, not `process_job` itself -- because
    # `AttestationStore.issue_if_newer()` (like `issue()`) has no realistic ok=False return today
    # (attestation_store.py:196-198's own comment: disk persistence success is "always true past
    # this point"; a disk failure raises instead). That branch is design-only until a real
    # ok=False path exists to test against, not merely untested by oversight.
    def test_worker_deletes_on_ok_regardless_of_activation(self):
        persisted_not_activated = WriteResult(
            ok=True,
            generation=7,
            activated=False,
            error="OPA unavailable",
        )
        synthetic_failed_result = WriteResult(
            ok=False,
            generation=None,
            activated=False,
            error="disk write failed",
        )

        self.assertTrue(_job_should_be_deleted(persisted_not_activated))  # @oracle LIA-527: persisted jobs delete even when OPA activation fails
        self.assertFalse(_job_should_be_deleted(synthetic_failed_result))  # @oracle LIA-527: an unsuccessful write result does not delete its job

    def test_issue_reports_ok_after_persistence_even_when_activation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AttestationStore(Path(tmp) / "attestations-v1.json")
            with mock.patch.object(
                AttestationStore,
                "_put_and_readback",
                _persisted_but_not_activated,
            ):
                result = store.issue(
                    repo_id="repo-a",
                    gate="code-reviewer",
                    subject_key="git-tree:sha1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    verdict="SHIP",
                    issuer_kind="script",
                    reviewer_id="code-reviewer@claude",
                    reason="oracle fixture",
                    backend="claude",
                )

        self.assertTrue(result.ok)  # @oracle LIA-527: successful disk persistence is the issue success criterion
        self.assertFalse(result.activated)  # @oracle LIA-527: OPA activation failure remains distinct from issue success
        self.assertTrue(_job_should_be_deleted(result))  # @oracle LIA-527: the worker deletes this persisted job despite failed activation

    def test_issue_disk_failure_raises_instead_of_returning_ok_false(self):
        # `issue()` has no realistic WriteResult(ok=False) return today: once its atomic
        # disk write succeeds it returns ok=True; a disk failure raises before any result.
        with tempfile.TemporaryDirectory() as tmp:
            store = AttestationStore(Path(tmp) / "attestations-v1.json")
            with mock.patch.object(
                store,
                "_write_disk_atomic",
                side_effect=OSError("simulated disk failure"),
            ):
                with self.assertRaises(OSError):  # @oracle LIA-527: issue write failure is an exception path, not ok=False
                    store.issue(
                        repo_id="repo-a",
                        gate="code-reviewer",
                        subject_key="git-tree:sha1:cccccccccccccccccccccccccccccccccccccccc",
                        verdict="SHIP",
                        issuer_kind="script",
                        reviewer_id="code-reviewer@claude",
                        reason="oracle fixture",
                        backend="claude",
                    )

    def test_process_job_deletes_on_ok_regardless_of_activation(self):
        # Discriminating against the REAL call site (docs/decisions/opa-warden-attestations-v1.md's
        # "Public interface" section), not the local `_job_should_be_deleted` stand-in above --
        # `cc_attestations.py` does not exist yet, so this is expected to fail with
        # ImportError/ModuleNotFoundError today. A future implementation that wraps the deletion
        # criterion wrong (e.g. gates on `.activated`) must fail THIS test even if it happens to
        # pass every AttestationStore-level assertion elsewhere in this file.
        from warden_policy.cc_attestations import process_job  # @oracle LIA-527: process_job must exist as the real worker entry point

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            AttestationStore, "_put_and_readback", _persisted_but_not_activated
        ):
            ledger_path = Path(tmp) / "attestations-cc-v1.json"
            queue_dir = Path(tmp) / "cc-write-queue"
            queue_dir.mkdir()
            job_id = "job-persisted-not-activated"
            job_path = queue_dir / f"{job_id}.json"
            job_path.write_text(
                json.dumps(
                    {
                        "repo_id": "repo-a",
                        "gate": "code-reviewer",
                        "subject_key": "git-tree:sha1:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
                        "verdict": "SHIP",
                        "issuer_kind": "script",
                        "reviewer_id": "code-reviewer@claude",
                        "reason": "oracle fixture",
                        "backend": "claude",
                        "queued_at": time.time_ns(),
                    }
                ),
                encoding="utf-8",
            )

            should_delete = process_job(
                job_id, queue_dir=queue_dir, ledger_path=ledger_path
            )  # @oracle LIA-527: process_job must delete a persisted-but-not-activated job

            self.assertTrue(should_delete)  # @oracle LIA-527: deletion is gated on .ok, not .activated, at the real call site

    def test_process_job_actually_persists_the_record_not_a_no_op(self):
        # Discriminates against a no-op `process_job` that just returns True for every job
        # without ever calling issue_if_newer -- found necessary by the GPT code-review co-gate:
        # the sibling test above only checks the RETURN VALUE, which a stub could satisfy while
        # silently dropping every real attestation. This test reads the ledger back afterward
        # and confirms the record genuinely landed, with the pointer genuinely advanced.
        from warden_policy.cc_attestations import process_job  # @oracle LIA-527: process_job must exist as the real worker entry point

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            AttestationStore,
            "_put_and_readback",
            lambda self, inner_doc: (True, inner_doc["generation"], None),
        ):
            ledger_path = Path(tmp) / "attestations-cc-v1.json"
            queue_dir = Path(tmp) / "cc-write-queue"
            queue_dir.mkdir()
            job_id = "job-real-persistence"
            subject_key = "git-tree:sha1:9999999999999999999999999999999999999999"
            (queue_dir / f"{job_id}.json").write_text(
                json.dumps(
                    {
                        "repo_id": "repo-real",
                        "gate": "code-reviewer",
                        "subject_key": subject_key,
                        "verdict": "SHIP",
                        "issuer_kind": "script",
                        "reviewer_id": "code-reviewer@claude",
                        "reason": "oracle fixture -- real persistence",
                        "backend": "claude",
                        "queued_at": time.time_ns(),
                    }
                ),
                encoding="utf-8",
            )

            should_delete = process_job(job_id, queue_dir=queue_dir, ledger_path=ledger_path)
            self.assertTrue(should_delete)  # @oracle LIA-527: a genuinely successful write also deletes its job

        document = json.loads(ledger_path.read_text(encoding="utf-8"))
        inner = document[CC_DOCUMENT_KEY]
        record_id = inner["latest_by_backend"]["repo-real"]["code-reviewer"][subject_key][
            "claude"
        ]  # @oracle LIA-527: process_job actually calls issue_if_newer, which populates latest_by_backend
        record = inner["records"][record_id]
        self.assertEqual(record["verdict"], "SHIP")  # @oracle LIA-527: the persisted record's verdict matches the job's
        self.assertEqual(record["subject"]["key"], subject_key)  # @oracle LIA-527: the persisted record's subject matches the job's


class TestCcTrivialVerdictOracle(unittest.TestCase):
    def test_store_layer_rejects_trivial_as_defense_in_depth(self):
        # Store-layer defense-in-depth only -- NOT the design's actual mandate (see
        # test_enqueue_verdict_never_writes_a_job_file_for_trivial below for that).
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "attestations-cc-v1.json"
            store = AttestationStore(ledger_path, document_key=CC_DOCUMENT_KEY)

            with self.assertRaises(AttestationStoreError):  # @oracle LIA-527: TRIVIAL is not a valid attestation verdict
                store.issue(
                    repo_id="repo-a",
                    gate="code-reviewer",
                    subject_key="git-tree:sha1:dddddddddddddddddddddddddddddddddddddddd",
                    verdict="TRIVIAL",
                    issuer_kind="script",
                    reviewer_id="code-reviewer@claude",
                    reason="human trivial bypass",
                    backend="claude",
                )

    def test_enqueue_verdict_never_writes_a_job_file_for_trivial(self):
        # Discriminating against the REAL call site (docs/decisions/opa-warden-attestations-v1.md's
        # "Public interface" section), not the store primitive above -- `cc_attestations.py` does
        # not exist yet, so this is expected to fail with ImportError/ModuleNotFoundError today.
        # A future implementation that enqueues TRIVIAL jobs and relies on the worker/store to
        # reject them later must fail THIS test, since the design mandates the skip happens at
        # enqueue time, before any job file is ever written.
        from warden_policy.cc_attestations import enqueue_verdict  # @oracle LIA-527: enqueue_verdict must exist as the real hook call site

        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "cc-write-queue"

            enqueue_verdict(
                repo_id="repo-a",
                gate="code-reviewer",
                subject_key="git-tree:sha1:ffffffffffffffffffffffffffffffffffffffff",
                verdict="TRIVIAL",
                issuer_kind="manual",
                reviewer_id="human",
                reason="human trivial bypass",
                backend="claude",
                queue_dir=queue_dir,
            )  # @oracle LIA-527: enqueue_verdict must skip TRIVIAL before writing anything

            job_files = list(queue_dir.glob("*.json")) if queue_dir.exists() else []
            self.assertEqual(job_files, [])  # @oracle LIA-527: no job file is ever written for a TRIVIAL verdict

    def test_enqueue_verdict_writes_a_real_job_and_spawns_a_worker_for_ship(self):
        # Discriminates against an unconditional no-op `enqueue_verdict` that would leave the
        # queue empty for every verdict, not just TRIVIAL -- found necessary by the GPT
        # code-review co-gate: the sibling test above only exercises the TRIVIAL-skip path, which
        # a stub that skips everything would also pass. This test exercises the real, common case
        # (a genuine SHIP verdict) and asserts both the job file's actual content and that a
        # worker subprocess was actually spawned, matching the exact Popen shape this design
        # specifies (start_new_session=True, stdin/stdout/stderr=DEVNULL).
        from warden_policy import cc_attestations  # @oracle LIA-527: cc_attestations module must exist
        from warden_policy.cc_attestations import enqueue_verdict

        with tempfile.TemporaryDirectory() as tmp:
            queue_dir = Path(tmp) / "cc-write-queue"

            with mock.patch.object(cc_attestations, "subprocess") as mock_subprocess:
                enqueue_verdict(
                    repo_id="repo-real",
                    gate="ai-eng-warden",
                    subject_key="git-tree:sha1:8888888888888888888888888888888888888888",
                    verdict="SHIP",
                    issuer_kind="script",
                    reviewer_id="ai-eng-warden@claude",
                    reason="oracle fixture -- real enqueue",
                    backend="claude",
                    queue_dir=queue_dir,
                )

                self.assertTrue(mock_subprocess.Popen.called)  # @oracle LIA-527: a non-TRIVIAL verdict spawns a detached worker

            job_files = list(queue_dir.glob("*.json")) if queue_dir.exists() else []
            self.assertEqual(len(job_files), 1)  # @oracle LIA-527: exactly one job file is written for a real verdict
            job = json.loads(job_files[0].read_text(encoding="utf-8"))
            self.assertEqual(job["repo_id"], "repo-real")  # @oracle LIA-527: the job file's content matches the call's arguments
            self.assertEqual(job["gate"], "ai-eng-warden")  # @oracle LIA-527: the job file's content matches the call's arguments
            self.assertEqual(job["verdict"], "SHIP")  # @oracle LIA-527: the job file's content matches the call's arguments
            self.assertIn("queued_at", job)  # @oracle LIA-527: the job file records a queued_at ordering token

            popen_call = mock_subprocess.Popen.call_args
            argv = popen_call.args[0]
            self.assertEqual(argv[0], sys.executable)  # @oracle LIA-527: the worker is spawned via the same Python interpreter
            self.assertIn("--worker", argv)  # @oracle LIA-527: the worker subprocess is invoked with --worker
            self.assertEqual(argv[-1], job_files[0].stem)  # @oracle LIA-527: the spawned worker is told this exact job's id
            self.assertTrue(popen_call.kwargs.get("start_new_session"))  # @oracle LIA-527: the worker is fully detached from the parent process group
            self.assertEqual(popen_call.kwargs.get("stdin"), mock_subprocess.DEVNULL)  # @oracle LIA-527: the worker's stdio is fully detached
            self.assertEqual(popen_call.kwargs.get("stdout"), mock_subprocess.DEVNULL)  # @oracle LIA-527: the worker's stdio is fully detached
            self.assertEqual(popen_call.kwargs.get("stderr"), mock_subprocess.DEVNULL)  # @oracle LIA-527: the worker's stdio is fully detached


class _IssueIfNewerOrderingOracleBase(unittest.TestCase):
    repo_id = "repo-a"
    gate = "code-reviewer"
    subject_key = "git-tree:sha1:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AttestationStore(Path(self.tmp.name) / "attestations-v1.json")
        self.opa_patch = mock.patch.object(
            AttestationStore,
            "_put_and_readback",
            lambda self, inner_doc: (True, inner_doc["generation"], None),
        )
        self.opa_patch.start()

    def tearDown(self):
        self.opa_patch.stop()
        self.tmp.cleanup()

    def _issue_if_newer(
        self,
        *,
        queued_at: int,
        verdict: str,
        backend: str | None,
    ) -> WriteResult:
        kwargs = {
            "repo_id": self.repo_id,
            "gate": self.gate,
            "subject_key": self.subject_key,
            "verdict": verdict,
            "issuer_kind": "script",
            "reviewer_id": "code-reviewer@claude",
            "reason": f"oracle fixture queued at {queued_at}",
            "queued_at": queued_at,
        }
        if backend is not None:
            kwargs["backend"] = backend
        return self.store.issue_if_newer(**kwargs)

    def _exercise_distinct_timestamps(
        self,
        *,
        backend: str | None,
        newer_first: bool,
    ) -> tuple[dict, dict[int, WriteResult], int, int]:
        older = time.time_ns()
        newer = older + 1
        writes = [(newer, "SHIP"), (older, "REVISE")]
        if not newer_first:
            writes.reverse()

        results = {
            queued_at: self._issue_if_newer(
                queued_at=queued_at,
                verdict=verdict,
                backend=backend,
            )
            for queued_at, verdict in writes
        }
        inner = self.store.read_locked()[DEFAULT_DOCUMENT_KEY]
        return inner, results, older, newer

    def _assert_newer_pointer_wins(
        self,
        *,
        inner: dict,
        results: dict[int, WriteResult],
        older: int,
        newer: int,
        backend: str | None,
    ) -> None:
        records = inner["records"]
        self.assertEqual(len(records), 2)  # @oracle LIA-527: issue_if_newer keeps both attempts in the append-only record map
        self.assertEqual({record["queued_at"] for record in records.values()}, {older, newer})  # @oracle LIA-527: queued_at is persisted for every attempted CC write
        newer_record_ids = [
            record_id
            for record_id, record in records.items()
            if record["queued_at"] == newer
        ]
        self.assertEqual(len(newer_record_ids), 1)  # @oracle LIA-527: the newer enqueue timestamp identifies one durable record
        if backend is None:
            pointer = inner["latest"][self.repo_id][self.gate][self.subject_key]
        else:
            pointer = inner["latest_by_backend"][self.repo_id][self.gate][
                self.subject_key
            ][backend]
        self.assertEqual(pointer, newer_record_ids[0])  # @oracle LIA-527: the relevant latest pointer always references the newer queued_at record
        self.assertTrue(results[older].ok)  # @oracle LIA-527: an older or superseded attempt remains a successful persisted write
        self.assertTrue(results[newer].ok)  # @oracle LIA-527: the newer attempt reports successful persistence

    def _exercise_concurrent_distinct_timestamps(
        self, *, backend: str | None, trials: int = 30
    ) -> None:
        # Discriminates against a TOCTOU-vulnerable implementation the sequential tests above
        # cannot catch -- found necessary by the GPT code-review co-gate: an implementation that
        # reads the current pointer BEFORE acquiring _mutate's exclusive lock, then only
        # conditionally calls the locked mutation, can pass every sequential test (there is never
        # any overlap to race) while still losing the ordering guarantee under real concurrent
        # detached workers, since both threads can observe the same stale pointer before either
        # commits. A `threading.Barrier` forces both calls to actually start together on every
        # trial, giving a buggy (non-atomic) implementation a real chance to interleave its
        # pre-lock read with the other thread's write; a correct implementation (the comparison
        # performed INSIDE _mutate's own exclusive-lock critical section, as this design specifies)
        # is safe by construction regardless of scheduling, so it must pass every trial, not just
        # most. `trials=30` is a best-effort repeat count, not a formal proof -- it exists because
        # this property cannot be verified with a single deterministic assertion pre-implementation.
        for trial in range(trials):
            with self.subTest(trial=trial):
                subject_key = f"{self.subject_key}-concurrent-{trial}"
                older, newer = time.time_ns(), None
                while newer is None or newer == older:
                    newer = time.time_ns()
                if newer < older:
                    older, newer = newer, older

                barrier = threading.Barrier(2)
                results: dict[int, WriteResult] = {}
                errors: dict[int, BaseException] = {}

                def _run(queued_at: int, verdict: str) -> None:
                    kwargs = {
                        "repo_id": self.repo_id,
                        "gate": self.gate,
                        "subject_key": subject_key,
                        "verdict": verdict,
                        "issuer_kind": "script",
                        "reviewer_id": "code-reviewer@claude",
                        "reason": f"oracle concurrency fixture queued at {queued_at}",
                        "queued_at": queued_at,
                    }
                    if backend is not None:
                        kwargs["backend"] = backend
                    barrier.wait()  # force both threads to attempt the race window together
                    try:
                        results[queued_at] = self.store.issue_if_newer(**kwargs)
                    except BaseException as exc:  # a bare thread target swallows exceptions
                        # silently by default -- capture and re-raise on the main thread below
                        # so a missing/broken issue_if_newer fails LOUDLY, not as a downstream
                        # KeyError that looks like an assertion failure instead of the real cause.
                        errors[queued_at] = exc

                threads = [
                    threading.Thread(target=_run, args=(newer, "SHIP")),
                    threading.Thread(target=_run, args=(older, "REVISE")),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=5)
                    self.assertFalse(t.is_alive())  # @oracle LIA-527: concurrent issue_if_newer calls complete, never deadlock
                if errors:
                    raise next(iter(errors.values()))  # @oracle LIA-527: issue_if_newer must not raise under real concurrent access

                inner = self.store.read_locked()[DEFAULT_DOCUMENT_KEY]
                if backend is None:
                    pointer = inner["latest"][self.repo_id][self.gate][subject_key]
                else:
                    pointer = inner["latest_by_backend"][self.repo_id][self.gate][subject_key][
                        backend
                    ]
                newer_record_ids = [
                    record_id
                    for record_id, record in inner["records"].items()
                    if record["queued_at"] == newer
                ]
                self.assertEqual(len(newer_record_ids), 1)  # @oracle LIA-527: the newer concurrent attempt is durably recorded exactly once
                self.assertEqual(
                    pointer, newer_record_ids[0]
                )  # @oracle LIA-527: under real thread concurrency, the pointer still reflects the newer queued_at, never a stale interleaving
                self.assertTrue(results[older].ok)  # @oracle LIA-527: the losing concurrent attempt still persists successfully
                self.assertTrue(results[newer].ok)  # @oracle LIA-527: the winning concurrent attempt persists successfully

    def _exercise_equal_timestamp_tie(self, *, backend: str | None) -> tuple[dict, WriteResult]:
        tied_at = time.time_ns()
        first_result = self._issue_if_newer(
            queued_at=tied_at,
            verdict="SHIP",
            backend=backend,
        )
        second_result = self._issue_if_newer(
            queued_at=tied_at,
            verdict="REVISE",
            backend=backend,
        )
        inner = self.store.read_locked()[DEFAULT_DOCUMENT_KEY]
        records = inner["records"]
        first_record_ids = [
            record_id
            for record_id, record in records.items()
            if record["verdict"] == "SHIP"
        ]
        self.assertTrue(first_result.ok)  # @oracle LIA-527: the first equal-timestamp record is persisted successfully
        self.assertTrue(second_result.ok)  # @oracle LIA-527: the tied record persists successfully even though it cannot advance the pointer
        self.assertEqual(len(records), 2)  # @oracle LIA-527: equal-timestamp attempts remain append-only
        self.assertEqual(len(first_record_ids), 1)  # @oracle LIA-527: the established record can be identified after a timestamp tie
        return inner, first_record_ids[0]


class TestCcIssueIfNewerBackendOrderingOracle(_IssueIfNewerOrderingOracleBase):
    backend = "claude"

    def test_backend_pointer_newer_then_older_keeps_newer(self):
        inner, results, older, newer = self._exercise_distinct_timestamps(
            backend=self.backend,
            newer_first=True,
        )

        self._assert_newer_pointer_wins(
            inner=inner,
            results=results,
            older=older,
            newer=newer,
            backend=self.backend,
        )

    def test_backend_pointer_older_then_newer_advances_to_newer(self):
        inner, results, older, newer = self._exercise_distinct_timestamps(
            backend=self.backend,
            newer_first=False,
        )

        self._assert_newer_pointer_wins(
            inner=inner,
            results=results,
            older=older,
            newer=newer,
            backend=self.backend,
        )

    def test_backend_pointer_tie_keeps_established_record(self):
        inner, established_record_id = self._exercise_equal_timestamp_tie(
            backend=self.backend,
        )

        pointer = inner["latest_by_backend"][self.repo_id][self.gate][self.subject_key][
            self.backend
        ]
        self.assertEqual(pointer, established_record_id)  # @oracle LIA-527: an equal queued_at never flips an established backend pointer

    def test_backend_pointer_survives_real_concurrent_writers(self):
        self._exercise_concurrent_distinct_timestamps(backend=self.backend)


class TestCcIssueIfNewerPlainOrderingOracle(_IssueIfNewerOrderingOracleBase):
    def test_plain_pointer_newer_then_older_keeps_newer(self):
        inner, results, older, newer = self._exercise_distinct_timestamps(
            backend=None,
            newer_first=True,
        )

        self._assert_newer_pointer_wins(
            inner=inner,
            results=results,
            older=older,
            newer=newer,
            backend=None,
        )

    def test_plain_pointer_older_then_newer_advances_to_newer(self):
        inner, results, older, newer = self._exercise_distinct_timestamps(
            backend=None,
            newer_first=False,
        )

        self._assert_newer_pointer_wins(
            inner=inner,
            results=results,
            older=older,
            newer=newer,
            backend=None,
        )

    def test_plain_pointer_tie_keeps_established_record(self):
        inner, established_record_id = self._exercise_equal_timestamp_tie(backend=None)

        pointer = inner["latest"][self.repo_id][self.gate][self.subject_key]
        self.assertEqual(pointer, established_record_id)  # @oracle LIA-527: an equal queued_at never flips an established plain pointer

    def test_plain_pointer_survives_real_concurrent_writers(self):
        self._exercise_concurrent_distinct_timestamps(backend=None)


class TestLegacyDefaultsOracle(unittest.TestCase):
    def test_old_constructor_shape_preserves_hermes_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AttestationStore(Path(tmp) / "attestations-v1.json")

        self.assertEqual(store.document_key, DEFAULT_DOCUMENT_KEY)  # @oracle LIA-527: legacy construction keeps the Hermes document key
        self.assertEqual(store.opa_data_path, DEFAULT_OPA_DATA_PATH)  # @oracle LIA-527: legacy construction keeps the Hermes OPA path


if __name__ == "__main__":
    unittest.main()

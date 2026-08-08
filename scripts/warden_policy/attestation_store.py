"""Append-only attestation ledger: locked disk writes + synchronous OPA activation.

Re-implements the ~30-line locking primitive `scripts/warden_hooks/verdict_store.py`
already proved out (sidecar `fcntl.flock`, re-read inside the lock, atomic
`os.replace`) rather than importing it -- that module is deliberately coupled
to the 4000-line `codex_warden_hooks` entry via `bind_entry()` and can't be
imported standalone without dragging that entry in.

Write transaction (`issue`/`enroll`/`unenroll`/`sync`), all under one
EXCLUSIVE lock:
  1. re-read + schema-shape-check the ledger from disk
  2. apply the mutation, advance `latest` where applicable, bump `generation`
  3. write a mode-0600 temp file, fsync, `os.replace`, fsync the directory
  4. STILL HOLDING THE LOCK, PUT the document to OPA and read back its
     generation; success only on an exact match

Read path (`locked_read`, used by the Hermes adapter): a SHARED lock on the
SAME lockfile, held for the ENTIRE `with` block -- across the local
generation read AND the caller's subsequent OPA query, not just the read
itself. Found missing by adversarial plan review (then re-found by code
review when an earlier fix released the lock too early): without this, a
writer's disk-write and its (potentially slow) OPA PUT are not atomic from a
concurrent reader's point of view, and the reader could observe an
inconsistent (disk, OPA) pair. Multiple readers may hold the shared lock
concurrently; a writer's exclusive lock waits for all of them to release,
and vice versa. `read_locked()` is a separate, narrower convenience method
that releases the lock before returning -- correct only for callers that
need a point-in-time snapshot with no follow-on query to keep atomic with
it (e.g. the CLI's `inspect`); it must NOT be used for the Hermes adapter's
read-then-query sequence. `reconcile_if_drifted()` (LIA-533) is a second, deliberate caller of
`read_locked()` -- its later OPA GET/PUT don't need to stay atomic with this snapshot the way
the Hermes adapter's live gate decision does, so this is not a repeat of that mistake.

No `--watch`: OPA's own docs note file-watching can silently drop updates
across `os.replace` -- unacceptable here, since a stale snapshot could still
show a superseded SHIP. Every mutation activates itself synchronously via
PUT + read-back instead.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
OPA_DEFAULT_BASE_URL = "http://127.0.0.1:8181"
#: Hermes's own document key -- the default for every existing call site (Hermes's, Phase 0's
#: `backend=`, `warden_attest.py`'s CLI). A separate, isolated document/store (`cc_attestations.py`)
#: uses AttestationStore.__init__'s `document_key`/`opa_data_path` params instead of this default.
_DEFAULT_DOCUMENT_KEY = "warden_attestations"
_OPA_TIMEOUT_SECONDS = 5

#: Sentinel distinct from a legitimate `None` document (LIA-533): `_get_opa_document()` returns
#: this only when the request itself never got a valid answer (network/timeout/malformed JSON).
#: A reachable OPA reporting no document for the path (HTTP 200, `result` key absent/null -- its
#: documented shape for an undefined path) returns plain `None`, which is real, confirmed drift,
#: not an unknown state -- conflating the two would silently skip repairing a deleted/null
#: document forever.
_OPA_REQUEST_FAILED = object()


def _empty_document(document_key: str) -> dict[str, Any]:
    return {
        document_key: {
            "schema_version": SCHEMA_VERSION,
            "generation": 0,
            "config": {"enforced_repos": {}},
            "records": {},
            "latest": {},
        }
    }


def _build_subject(kind: str, subject_key: str) -> dict[str, Any]:
    """Pure subject-dict constructor shared by `issue()` and `issue_if_newer()`.

    Callers MUST validate `kind in ("git-tree", "session")` themselves before calling this --
    it only branches on `kind == "git-tree"` vs. else, so an unvalidated unknown `kind` would
    silently be treated as `"session"` if a caller dropped its own guard.
    """
    if kind == "git-tree":
        # Every existing call site: byte-for-byte the same shape as before `kind` existed.
        return {
            "kind": "git-tree",
            "key": subject_key,
            "digest": {
                "algorithm": subject_key.split(":")[1],
                "value": subject_key.split(":")[2],
            },
        }
    # session subject: subject_key carries the raw, opaque session_id -- nothing to hash/digest
    # (unlike a repo-relative git path, a session id has no sensitive structure to redact).
    return {"kind": "session", "session_id": subject_key}


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    generation: int | None
    activated: bool
    error: str | None = None


class AttestationStoreError(Exception):
    pass


class AttestationStore:
    def __init__(
        self,
        ledger_path: Path,
        opa_base_url: str = OPA_DEFAULT_BASE_URL,
        *,
        document_key: str = _DEFAULT_DOCUMENT_KEY,
        opa_data_path: str | None = None,
    ):
        self.ledger_path = Path(ledger_path)
        self.lock_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock")
        self.opa_base_url = opa_base_url.rstrip("/")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # LIA-527 Phase 2: every existing call site passes neither kwarg and gets byte-identical
        # behavior (document_key defaults to Hermes's own "warden_attestations"). The isolated CC
        # store (cc_attestations.py) passes document_key="warden_cc_attestations" so its writes
        # can never land in -- or overwrite -- Hermes's live OPA document.
        self.document_key = document_key
        self.opa_data_path = opa_data_path or f"/v1/data/{document_key}"

    # -- locking ------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, exclusive: bool, non_blocking: bool = False):
        """`non_blocking=True` (LIA-533): raises `BlockingIOError` immediately instead of
        waiting if the lock is currently held by anyone else -- used only by
        `reconcile_if_drifted()`'s repair path, so a periodic/background caller can never make a
        concurrent reader (the Hermes gate's `locked_read()`) or another writer (`_mutate`) queue
        behind it. Every existing caller omits this parameter and gets today's exact blocking
        behavior, unchanged."""
        self.lock_path.touch(exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR)
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if non_blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- disk I/O -------------------------------------------------------

    def _read_disk(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return _empty_document(self.document_key)
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise AttestationStoreError(f"ledger unreadable/corrupt: {exc}") from exc
        if self.document_key not in doc:
            raise AttestationStoreError(f"ledger missing {self.document_key} root key")
        return doc

    def _write_disk_atomic(self, doc: dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(dir=self.ledger_path.parent, prefix=".tmp-attestations-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(doc, f, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, self.ledger_path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        dir_fd = os.open(self.ledger_path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # -- OPA sync ---------------------------------------------------------

    def _put_and_readback(
        self, inner_doc: dict[str, Any], *, timeout_seconds: float | None = None,
    ) -> tuple[bool, int | None, str | None]:
        # Must read self.opa_data_path here, not a module-level constant -- otherwise an
        # isolated CC store's write would still PUT to Hermes's live OPA document regardless
        # of what the on-disk ledger's own key is named.
        #
        # timeout_seconds (LIA-533): overrides _OPA_TIMEOUT_SECONDS for both calls below when
        # given. `None` (every existing caller: _mutate, sync()) preserves today's 5s default
        # exactly. reconcile_if_drifted()'s repair path passes a short override (0.5s) to bound
        # its own worst-case exclusive-lock hold, since it (unlike _mutate) may be contending
        # with the Hermes gate's own tight external timeout budget.
        timeout = timeout_seconds if timeout_seconds is not None else _OPA_TIMEOUT_SECONDS
        body = json.dumps(inner_doc).encode("utf-8")
        req = urllib.request.Request(
            self.opa_base_url + self.opa_data_path, data=body,
            headers={"Content-Type": "application/json"}, method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                pass
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, None, f"PUT to OPA failed: {exc}"

        gen_req = urllib.request.Request(self.opa_base_url + self.opa_data_path + "/generation")
        try:
            with urllib.request.urlopen(gen_req, timeout=timeout) as resp:
                result = json.loads(resp.read()).get("result")
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            return False, None, f"generation read-back failed: {exc}"

        expected = inner_doc.get("generation")
        if result != expected:
            return False, result, f"read-back generation {result!r} != written {expected!r}"
        return True, result, None

    # -- public read path (Hermes adapter) --------------------------------

    def read_locked(self) -> dict[str, Any]:
        """Shared-lock read of the full document, lock released before returning.

        Convenience wrapper for callers that only need a point-in-time snapshot (e.g. the
        CLI's `inspect`/`check`). The Hermes adapter must NOT use this for its enrollment
        check + OPA query -- use `locked_read()` instead, which keeps the shared lock held
        across both, so a writer's write+PUT+read-back transaction cannot interleave with
        the adapter's read-then-query (found missing by adversarial code review: this
        method alone released the lock before the caller ever queried OPA, contradicting
        this module's own documented reader/writer coordination guarantee).
        """
        with self._locked(exclusive=False):
            return self._read_disk()

    @contextlib.contextmanager
    def locked_read(self):
        """Shared-lock read that stays held for the whole `with` block -- use this when the
        caller's next step (e.g. an OPA query) must be atomic with the local read, not just
        the read itself."""
        with self._locked(exclusive=False):
            yield self._read_disk()

    # -- mutations (all exclusive-locked, write -> PUT -> read-back) -----

    def _mutate(self, apply_fn) -> WriteResult:
        with self._locked(exclusive=True):
            doc = self._read_disk()
            inner = doc[self.document_key]
            apply_fn(inner)
            inner["generation"] = inner["generation"] + 1
            self._write_disk_atomic(doc)
            # `ok` tracks disk persistence (always true past this point -- the atomic write
            # above either succeeded or raised); `activated` tracks OPA sync separately. A
            # failed PUT is "persisted but not activated," not a failed write.
            activated, _activated_gen, err = self._put_and_readback(inner)
            return WriteResult(ok=True, generation=inner["generation"], activated=activated, error=err)

    def enroll(self, repo_id: str) -> WriteResult:
        def _apply(inner):
            # MERGE into any existing entry, never replace wholesale (LIA-523 fix): an
            # earlier version did `enforced_repos[repo_id] = {...}`, silently erasing any
            # sibling field (e.g. plan_review_enabled) a repo already had set. setdefault({})
            # produces byte-identical output to the old code on a fresh repo_id, since there
            # is nothing pre-existing to preserve in that case.
            entry = inner["config"]["enforced_repos"].setdefault(repo_id, {})
            entry["enabled"] = True
            entry["enrolled_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return self._mutate(_apply)

    def unenroll(self, repo_id: str) -> WriteResult:
        def _apply(inner):
            entry = inner["config"]["enforced_repos"].get(repo_id)
            if entry is None:
                raise AttestationStoreError(f"repo {repo_id} was never enrolled")
            entry["enabled"] = False
        return self._mutate(_apply)

    def set_plan_review_enabled(self, repo_id: str, enabled: bool) -> WriteResult:
        """Independent, additive on/off switch for the plan-review gate (LIA-523).

        Deliberately does NOT require prior `enroll()` for `enabled=True`: plan-review
        gating is a genuinely independent surface from code-review gating -- a repo may
        want plan-review-only enforcement without running the (separately, more heavily
        adversarially-tested) code-review gate. Auto-vivifies a fresh entry with
        `enabled: False` (code-review stays off unless separately `enroll()`-ed) when none
        exists yet. `enabled=False` mirrors `unenroll()`'s raise-if-absent convention --
        you cannot disable something that was never created, for UX symmetry with the
        established sibling pattern rather than a second, different behavior shape for
        what is otherwise the same kind of toggle.
        """
        def _apply(inner):
            entry = inner["config"]["enforced_repos"].get(repo_id)
            if enabled:
                if entry is None:
                    entry = inner["config"]["enforced_repos"][repo_id] = {
                        "enabled": False,
                        "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                entry["plan_review_enabled"] = True
            else:
                if entry is None:
                    raise AttestationStoreError(f"repo {repo_id} was never enrolled")
                entry["plan_review_enabled"] = False
        return self._mutate(_apply)

    def set_ai_eng_warden_enabled(self, repo_id: str, enabled: bool) -> WriteResult:
        """Independent, additive on/off switch for the ai-eng-warden gate (LIA-524).

        Structurally identical to `set_plan_review_enabled` -- see that method's docstring
        for the full rationale (independent surface from code-review, auto-vivifies a fresh
        entry, raise-if-absent on disable)."""
        def _apply(inner):
            entry = inner["config"]["enforced_repos"].get(repo_id)
            if enabled:
                if entry is None:
                    entry = inner["config"]["enforced_repos"][repo_id] = {
                        "enabled": False,
                        "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                entry["ai_eng_warden_enabled"] = True
            else:
                if entry is None:
                    raise AttestationStoreError(f"repo {repo_id} was never enrolled")
                entry["ai_eng_warden_enabled"] = False
        return self._mutate(_apply)

    def set_verification_gate_enabled(self, repo_id: str, enabled: bool) -> WriteResult:
        """Independent, additive on/off switch for the verification-gate (LIA-524).

        Structurally identical to `set_plan_review_enabled` -- see that method's docstring
        for the full rationale (independent surface from code-review, auto-vivifies a fresh
        entry, raise-if-absent on disable)."""
        def _apply(inner):
            entry = inner["config"]["enforced_repos"].get(repo_id)
            if enabled:
                if entry is None:
                    entry = inner["config"]["enforced_repos"][repo_id] = {
                        "enabled": False,
                        "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                entry["verification_gate_enabled"] = True
            else:
                if entry is None:
                    raise AttestationStoreError(f"repo {repo_id} was never enrolled")
                entry["verification_gate_enabled"] = False
        return self._mutate(_apply)

    def issue(
        self, *, repo_id: str, gate: str, subject_key: str, verdict: str,
        issuer_kind: str, reviewer_id: str, reason: str, backend: str | None = None,
        kind: str = "git-tree",
    ) -> WriteResult:
        # COULD_NOT_RUN is a real, first-class verdict the legacy verdict store already
        # persists today (e.g. `codex_warden_hooks.py record-verdict ... COULD_NOT_RUN` for
        # a genuine backend infra failure) -- this ledger accepts it for parity, not as
        # speculative new scope.
        if verdict not in ("SHIP", "REVISE", "BLOCK", "COULD_NOT_RUN"):
            raise AttestationStoreError(f"invalid verdict {verdict!r}")
        if kind not in ("git-tree", "session"):
            raise AttestationStoreError(f"invalid subject kind {kind!r}")

        def _apply(inner):
            record_id = f"att-{uuid.uuid4()}"
            subject = _build_subject(kind, subject_key)
            record = {
                "id": record_id,
                "schema_version": SCHEMA_VERSION,
                "repo_id": repo_id,
                "gate": gate,
                "subject": subject,
                "verdict": verdict,
                "issuer": {"kind": issuer_kind, "reviewer_id": reviewer_id},
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reason": reason,
            }
            if backend is None:
                # Every existing call site (Hermes included): byte-identical to today,
                # writes only `latest`, never touches `latest_by_backend`.
                inner["records"][record_id] = record
                inner["latest"].setdefault(repo_id, {}).setdefault(gate, {})[subject_key] = record_id
            else:
                # Multi-backend migrated gates: record additionally carries "backend", and
                # the write populates the new, additive `latest_by_backend` index instead of
                # `latest` -- `latest`'s scalar shape and every existing entry are untouched.
                record["backend"] = backend
                inner["records"][record_id] = record
                inner.setdefault("latest_by_backend", {}).setdefault(repo_id, {}).setdefault(
                    gate, {}
                ).setdefault(subject_key, {})[backend] = record_id
        return self._mutate(_apply)

    def issue_if_newer(
        self, *, repo_id: str, gate: str, subject_key: str, verdict: str,
        issuer_kind: str, reviewer_id: str, reason: str, queued_at: int,
        backend: str | None = None, kind: str = "git-tree",
    ) -> WriteResult:
        """LIA-527 Phase 2: `issue()`, layered with an ordering guard for detached, concurrently
        completing writers (see `cc_attestations.py`'s worker pattern).

        `flock` mutual exclusion alone only prevents two writers from writing *concurrently* --
        it says nothing about which one writes *last*. Detached workers acquire the lock in
        whatever order the OS schedules them, which is not enqueue order. This method compares
        `queued_at` (caller-supplied, `time.time_ns()` wall-clock -- reboot-durable, unlike
        `monotonic_ns()`) against whichever record the relevant `latest`/`latest_by_backend`
        pointer currently references, and only advances the pointer when this write is strictly
        newer -- entirely INSIDE `_mutate`'s own exclusive-lock critical section, so there is no
        separate pre-lock read for a concurrent writer to race against. Ties are broken in favor
        of the already-established pointer (never flipped by an equal `queued_at`). The
        append-only `records` map still gains the new record unconditionally either way -- only
        pointer advancement is guarded, so a "superseded" write is not an error, just a write
        that correctly loses the ordering comparison.
        """
        if verdict not in ("SHIP", "REVISE", "BLOCK", "COULD_NOT_RUN"):
            raise AttestationStoreError(f"invalid verdict {verdict!r}")
        if kind not in ("git-tree", "session"):
            raise AttestationStoreError(f"invalid subject kind {kind!r}")

        def _apply(inner):
            record_id = f"att-{uuid.uuid4()}"
            subject = _build_subject(kind, subject_key)
            record = {
                "id": record_id,
                "schema_version": SCHEMA_VERSION,
                "repo_id": repo_id,
                "gate": gate,
                "subject": subject,
                "verdict": verdict,
                "issuer": {"kind": issuer_kind, "reviewer_id": reviewer_id},
                "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "reason": reason,
                "queued_at": queued_at,
            }
            if backend is None:
                pointer_scope = inner["latest"].setdefault(repo_id, {}).setdefault(gate, {})
                pointer_key = subject_key
            else:
                record["backend"] = backend
                pointer_scope = inner.setdefault("latest_by_backend", {}).setdefault(
                    repo_id, {}
                ).setdefault(gate, {}).setdefault(subject_key, {})
                pointer_key = backend
            # Append-only: every attempted write lands in `records`, regardless of whether it
            # goes on to win or lose the ordering comparison below.
            inner["records"][record_id] = record
            existing_id = pointer_scope.get(pointer_key)
            existing_queued_at = (
                inner["records"].get(existing_id, {}).get("queued_at") if existing_id else None
            )
            # Strict >: a missing existing_queued_at (no prior pointer, or a pre-existing plain
            # issue()-written record that predates this field) counts as always-older, so the
            # first write for a key always becomes latest. An equal queued_at never flips an
            # already-established pointer.
            if existing_queued_at is None or queued_at > existing_queued_at:
                pointer_scope[pointer_key] = record_id
        return self._mutate(_apply)

    def sync(self) -> WriteResult:
        """Re-PUT the current disk state to OPA without incrementing generation -- the
        retry path when a prior write's PUT/read-back failed ("persisted but not activated")."""
        with self._locked(exclusive=True):
            doc = self._read_disk()
            inner = doc[self.document_key]
            ok, activated_gen, err = self._put_and_readback(inner)
            return WriteResult(ok=ok, generation=inner["generation"], activated=ok, error=err)

    def _get_opa_document(self) -> dict[str, Any] | None | object:
        """Plain GET of the full live document, no lock held (LIA-533). Returns:
        - a dict: OPA answered and has this document.
        - `None`: OPA answered but reports no document for this path (its documented shape for
          an undefined/deleted/null document) -- a real, confirmed state, not "unknown."
        - `_OPA_REQUEST_FAILED`: the request itself never got a valid answer (network error,
          timeout, malformed response) -- genuinely unknown state, distinct from the above.
        """
        req = urllib.request.Request(self.opa_base_url + self.opa_data_path)
        try:
            with urllib.request.urlopen(req, timeout=_OPA_TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read()).get("result")
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            return _OPA_REQUEST_FAILED

    def reconcile_if_drifted(self) -> WriteResult:
        """Best-effort periodic/background reconciliation (LIA-533) for a caller -- e.g. a
        launchd job -- that must never contend with the hot commit-gate's `locked_read()`, and
        must never race a real `_mutate()` write's own PUT-and-readback.

        Three-way outcome, each handled differently:
        1. OPA already matches disk (full content comparison, not just `generation` -- a foreign
           document could coincidentally carry a matching generation with different content):
           done, no lock touched at all.
        2. The GET itself failed (`_OPA_REQUEST_FAILED`): genuinely unknown state -- OPA may be
           fully down, in which case attempting a repair PUT is doomed to also fail. Skip
           entirely without ever touching the exclusive lock, so a sustained outage never creates
           a recurring lock-hold window; the next tick (or the next real write's own embedded
           PUT) retries once OPA is reachable again.
        3. Confirmed drift (OPA answered -- possibly with no document at all, itself a real,
           repairable state -- and it differs from disk): repair under the SAME lock `_mutate`
           already uses for its own PUT-and-readback, acquired non-blocking so this call can
           never make a concurrent reader or writer wait on ITS acquisition, and with a short,
           measured timeout bounding its own worst-case hold to ~1s if acquired. If the lock is
           contended (a real write in flight), back off immediately -- that write's own PUT
           already handles activation, so nothing is lost by skipping this tick.
        """
        doc = self.read_locked()
        inner = doc[self.document_key]
        opa_doc = self._get_opa_document()
        if opa_doc is _OPA_REQUEST_FAILED:
            return WriteResult(ok=False, generation=inner["generation"], activated=False,
                                error="OPA unreachable -- skipped, not a confirmed content mismatch")
        if opa_doc == inner:
            return WriteResult(ok=True, generation=inner["generation"], activated=True, error=None)
        try:
            with self._locked(exclusive=True, non_blocking=True):
                doc = self._read_disk()
                inner = doc[self.document_key]
                ok, activated_gen, err = self._put_and_readback(inner, timeout_seconds=0.5)
                return WriteResult(ok=ok, generation=inner["generation"], activated=ok, error=err)
        except BlockingIOError:
            return WriteResult(ok=False, generation=inner["generation"], activated=False,
                                error="lock busy (reconciliation skipped, retries next tick)")

    def inspect(self, repo_id: str) -> list[dict[str, Any]]:
        with self._locked(exclusive=False):
            doc = self._read_disk()
        inner = doc[self.document_key]
        return [r for r in inner["records"].values() if r["repo_id"] == repo_id]

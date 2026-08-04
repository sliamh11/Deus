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
read-then-query sequence.

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
OPA_DATA_PATH = "/v1/data/warden_attestations"
_OPA_TIMEOUT_SECONDS = 5


def _empty_document() -> dict[str, Any]:
    return {
        "warden_attestations": {
            "schema_version": SCHEMA_VERSION,
            "generation": 0,
            "config": {"enforced_repos": {}},
            "records": {},
            "latest": {},
        }
    }


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    generation: int | None
    activated: bool
    error: str | None = None


class AttestationStoreError(Exception):
    pass


class AttestationStore:
    def __init__(self, ledger_path: Path, opa_base_url: str = OPA_DEFAULT_BASE_URL):
        self.ledger_path = Path(ledger_path)
        self.lock_path = self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock")
        self.opa_base_url = opa_base_url.rstrip("/")
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- locking ------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self, exclusive: bool):
        self.lock_path.touch(exist_ok=True)
        fd = os.open(self.lock_path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # -- disk I/O -------------------------------------------------------

    def _read_disk(self) -> dict[str, Any]:
        if not self.ledger_path.exists():
            return _empty_document()
        try:
            with open(self.ledger_path, encoding="utf-8") as f:
                doc = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            raise AttestationStoreError(f"ledger unreadable/corrupt: {exc}") from exc
        if "warden_attestations" not in doc:
            raise AttestationStoreError("ledger missing warden_attestations root key")
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

    def _put_and_readback(self, inner_doc: dict[str, Any]) -> tuple[bool, int | None, str | None]:
        body = json.dumps(inner_doc).encode("utf-8")
        req = urllib.request.Request(
            self.opa_base_url + OPA_DATA_PATH, data=body,
            headers={"Content-Type": "application/json"}, method="PUT",
        )
        try:
            with urllib.request.urlopen(req, timeout=_OPA_TIMEOUT_SECONDS):
                pass
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            return False, None, f"PUT to OPA failed: {exc}"

        gen_req = urllib.request.Request(self.opa_base_url + OPA_DATA_PATH + "/generation")
        try:
            with urllib.request.urlopen(gen_req, timeout=_OPA_TIMEOUT_SECONDS) as resp:
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
            inner = doc["warden_attestations"]
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
            inner["config"]["enforced_repos"][repo_id] = {
                "enabled": True,
                "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        return self._mutate(_apply)

    def unenroll(self, repo_id: str) -> WriteResult:
        def _apply(inner):
            entry = inner["config"]["enforced_repos"].get(repo_id)
            if entry is None:
                raise AttestationStoreError(f"repo {repo_id} was never enrolled")
            entry["enabled"] = False
        return self._mutate(_apply)

    def issue(
        self, *, repo_id: str, gate: str, subject_key: str, verdict: str,
        issuer_kind: str, reviewer_id: str, reason: str, backend: str | None = None,
    ) -> WriteResult:
        # COULD_NOT_RUN is a real, first-class verdict the legacy verdict store already
        # persists today (e.g. `codex_warden_hooks.py record-verdict ... COULD_NOT_RUN` for
        # a genuine backend infra failure) -- this ledger accepts it for parity, not as
        # speculative new scope.
        if verdict not in ("SHIP", "REVISE", "BLOCK", "COULD_NOT_RUN"):
            raise AttestationStoreError(f"invalid verdict {verdict!r}")

        def _apply(inner):
            record_id = f"att-{uuid.uuid4()}"
            record = {
                "id": record_id,
                "schema_version": SCHEMA_VERSION,
                "repo_id": repo_id,
                "gate": gate,
                "subject": {
                    "kind": "git-tree",
                    "key": subject_key,
                    "digest": {
                        "algorithm": subject_key.split(":")[1],
                        "value": subject_key.split(":")[2],
                    },
                },
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

    def sync(self) -> WriteResult:
        """Re-PUT the current disk state to OPA without incrementing generation -- the
        retry path when a prior write's PUT/read-back failed ("persisted but not activated")."""
        with self._locked(exclusive=True):
            doc = self._read_disk()
            inner = doc["warden_attestations"]
            ok, activated_gen, err = self._put_and_readback(inner)
            return WriteResult(ok=ok, generation=inner["generation"], activated=ok, error=err)

    def inspect(self, repo_id: str) -> list[dict[str, Any]]:
        with self._locked(exclusive=False):
            doc = self._read_disk()
        inner = doc["warden_attestations"]
        return [r for r in inner["records"].values() if r["repo_id"] == repo_id]

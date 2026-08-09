"""Isolated Claude-Code-authored attestation write path (LIA-527 Phase 2).

See ``docs/decisions/opa-warden-attestations-v1.md``'s "### Phase 2 -- isolated CC write path"
section for the full design, including the ordering-guard rationale behind
``AttestationStore.issue_if_newer``.

Two public functions:

- ``enqueue_verdict(...)`` -- the hook call site's entry point. Writes one job file atomically
  and spawns a fully-detached worker subprocess, returning immediately with no wait of any kind.
  **No production caller yet.** Wiring this into ``codex_warden_hooks.py``'s
  ``run_warden_backends_gate``/``run_verification_gate`` (the ADR's "Who writes, and when"
  section) is a deliberately separate, distinct follow-up -- not bundled into the PR that adds
  this module, since those gate functions run on every commit's critical path across every
  enrolled repo and deserve their own dedicated review pass. Tracked as LIA-534. Do not wire this
  module into those gates as a side effect of an unrelated change; that wiring is LIA-534's own
  scope.
- ``process_job(job_id, ...)`` -- the detached worker's entry point (what ``--worker <job_id>``
  invokes). Performs the actual ``cc_store.issue_if_newer()`` call and reports whether the job
  file should be deleted.

Absolute imports with an explicit ``sys.path`` insert (not the package-relative imports
``cc_shadow.py`` uses), because unlike ``cc_shadow.py`` this module is also re-invoked directly
as ``python3 cc_attestations.py --worker <job_id>`` by its own spawned worker subprocess -- under
that invocation ``__name__ == "__main__"`` and there is no parent package context for a relative
import to resolve against.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
from warden_policy.attestation_store import AttestationStore  # noqa: E402

#: The isolated OPA data document CC writes target -- distinct from Hermes's own
#: "warden_attestations", so a failed CC write can never desynchronize Hermes's gate.
CC_DOCUMENT_KEY = "warden_cc_attestations"
CC_LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-cc-v1.json"
QUEUE_DIR = Path.home() / ".config" / "deus" / "guardrails" / "cc-write-queue"

#: The legacy gate's human trivial-commit bypass. AttestationStore.issue()/issue_if_newer()
#: reject this outright as an invalid verdict -- enqueue_verdict must recognize and skip it
#: BEFORE doing anything else, never invent a synthetic non-TRIVIAL verdict to satisfy the
#: schema. A TRIVIAL-bypassed commit was never actually reviewed by a backend, so the CC ledger
#: simply has no record for it -- that's honest, not a gap.
TRIVIAL_VERDICT = "TRIVIAL"


def _cc_store(ledger_path: Path) -> AttestationStore:
    return AttestationStore(ledger_path, document_key=CC_DOCUMENT_KEY)


def enqueue_verdict(
    *,
    repo_id: str,
    gate: str,
    subject_key: str,
    verdict: str,
    issuer_kind: str,
    reviewer_id: str,
    reason: str,
    backend: str,
    queue_dir: Path = QUEUE_DIR,
) -> None:
    """Write one job file + spawn a detached worker. Returns None unconditionally.

    Every failure -- including a bug in this function -- is swallowed by the broad
    ``try/except`` below, matching ``cc_shadow.observe()``'s containment floor: a mirror-write
    attempt must never be able to affect the gate that already decided.
    """
    try:
        if verdict == TRIVIAL_VERDICT:
            return None

        queue_dir = Path(queue_dir)
        queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        job_id = str(uuid.uuid4())
        job = {
            "repo_id": repo_id,
            "gate": gate,
            "subject_key": subject_key,
            "verdict": verdict,
            "issuer_kind": issuer_kind,
            "reviewer_id": reviewer_id,
            "reason": reason,
            "backend": backend,
            "queued_at": time.time_ns(),
        }

        fd, tmp_path = tempfile.mkstemp(dir=queue_dir, prefix=".tmp-cc-job-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(job, f)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, queue_dir / f"{job_id}.json")
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        # Fully detached: no join, no timeout, no wait of any kind -- the hook process that
        # calls enqueue_verdict is short-lived (main() -> sys.exit(main())) and must return
        # immediately regardless of how long the actual write takes.
        #
        # --queue-dir is always passed, positioned BEFORE --worker <job_id>, so the worker
        # reads from the SAME directory the job file was written to -- a caller using the
        # public queue_dir override would otherwise spawn a worker that silently looks in the
        # module-default QUEUE_DIR instead. Positioning it before --worker also keeps
        # argv[-1] == job_id.
        subprocess.Popen(
            [
                sys.executable, str(Path(__file__).resolve()),
                "--queue-dir", str(queue_dir),
                "--worker", job_id,
            ],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 -- containment floor, matches cc_shadow.observe()
        pass
    return None


def process_job(
    job_id: str, *, queue_dir: Path = QUEUE_DIR, ledger_path: Path = CC_LEDGER_PATH,
) -> bool:
    """Detached worker body: read the job file, write it through, report deletion eligibility.

    Returns True iff the write persisted to disk (``WriteResult.ok is True``) -- regardless of
    OPA activation, and regardless of whether the ordering comparison inside
    ``issue_if_newer`` was won or lost. A persisted-but-not-activated write (OPA unreachable) is
    this ledger's normal "failed PUT" state, which ``sync()`` already exists to retry on a later
    mutation; gating deletion on ``.activated`` instead would leave every job attempted during an
    OPA outage stuck in the queue with no retry path.

    Does NOT delete the job file itself -- that's the ``--worker`` CLI wrapper's job when this
    returns True, so ``process_job`` stays a pure "did it land" query, testable without touching
    the filesystem beyond the job/ledger files it's explicitly given.
    """
    job_path = Path(queue_dir) / f"{job_id}.json"
    store = _cc_store(Path(ledger_path))
    try:
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        kwargs = {
            key: payload[key]
            for key in (
                "repo_id", "gate", "subject_key", "verdict", "issuer_kind", "reviewer_id",
                "reason", "queued_at",
            )
        }
        if payload.get("backend") is not None:
            kwargs["backend"] = payload["backend"]
        result = store.issue_if_newer(**kwargs)
    except Exception:
        # A permanently-invalid job (missing/malformed file, or a payload issue_if_newer
        # rejects -- bad verdict, malformed subject_key, ...) has no retry path that would ever
        # succeed -- it sits until the 24h stale-job sweep reclaims it, producing one sweep log
        # line. Single-best-effort-no-retry, per the ADR. Covers both a corrupt/unreadable job
        # file AND a store-layer rejection under the same containment.
        return False
    return result.ok is True


def _worker_main(job_id: str, *, queue_dir: Path = QUEUE_DIR) -> None:
    queue_dir = Path(queue_dir)
    if process_job(job_id, queue_dir=queue_dir):
        (queue_dir / f"{job_id}.json").unlink(missing_ok=True)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        _job_id = sys.argv[sys.argv.index("--worker") + 1]
        _queue_dir = QUEUE_DIR
        if "--queue-dir" in sys.argv:
            _queue_dir = Path(sys.argv[sys.argv.index("--queue-dir") + 1])
        try:
            _worker_main(_job_id, queue_dir=_queue_dir)
        except Exception as e:
            sys.stderr.write(f"[cc-attestations worker] {type(e).__name__}: {e}\n")

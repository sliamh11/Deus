"""Read-only OPA shadow observer for the Claude Code warden gates (Phase 1).

WHAT THIS IS: after a Claude Code commit gate has ALREADY decided, this module asks
OPA what it *would* have decided from the attestation ledger, and appends one
classified line to its own JSONL log. Nothing else. It exists to produce the
disagreement data a human needs before anyone designs a Phase 2 cutover.

THREE INVARIANTS, each backed by a test in tests/test_cc_shadow.py -- treat them as
the module's contract, not as aspirations:

1. **No gate outcome can depend on it.** ``observe()`` returns ``None`` by
   construction and every call site discards the result. It is called AFTER the
   legacy decision is computed, and on blocking paths AFTER the decision has already
   been written to stdout.
2. **It never writes to stdout or stderr.** ``codex_warden_hooks._block_pre_tool``
   emits the hook's decision as JSON on stdout; a stray ``print`` here would corrupt
   the hook protocol and could change a real outcome. All output goes to LOG_PATH.
3. **It never writes to the attestation ledger, and never locks it.** Phase 1 is
   read-only. An earlier draft mirrored verdicts into the ledger via
   ``AttestationStore.issue(backend=...)``; independent plan review killed it, and
   the reasoning is worth keeping here because it is easy to re-propose:
   ``AttestationStore._mutate`` bumps ``generation`` and writes disk BEFORE its OPA
   PUT is confirmed, so a PUT that fails leaves disk at N+1 while OPA serves N.
   ``guardrails.rego``'s ``supported`` guard requires those to be equal, so the next
   Hermes-gated commit in ANY enrolled repo would fall through to the default deny
   and fail closed -- a shadow feature blocking real commits. A separately-flagged
   write with a health pre-flight and a sync retry only lowers the probability; it
   does not restore the "changes nothing" contract. Any future write path needs its
   own design (isolated ledger, or atomic persist+activate) and its own review.
   Consequence, by design: ``latest_by_backend`` stays empty, so nearly every
   observation classifies as ``no-attestation``. That is the expected Phase 1 result,
   not a defect.

Reads are deliberately UNLOCKED. ``AttestationStore._locked`` uses a blocking
``fcntl.flock`` with no timeout, so taking the shared lock on the PreToolUse commit
path could stall a real ``git commit`` behind a writer parked in its own 5-second OPA
PUT. Unlocked is safe here because the ledger is published via ``os.replace``: a
reader sees a complete old or a complete new document, never a torn one. The only
cost is possibly observing a stale generation -- which is itself one of the things
this observer classifies and logs.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from warden_review.constants import BACKEND_CLAUDE, KNOWN_MODEL_BACKENDS

from .git_subject import GitSubjectError, resolve as resolve_subject
from .opa_client import query_backend_verdicts, query_generation

LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"
LOG_PATH = Path.home() / ".config" / "deus" / "guardrails" / "logs" / "cc-shadow.jsonl"
OPA_URL = "http://127.0.0.1:8181"

#: Per-request OPA timeout, matching hermes_warden_gate.py's budget.
OPA_TIMEOUT_SECONDS = 0.75
#: Whole-observation budget. The observer sits on the interactive commit path, so it
#: must be bounded even when every step is slow; past the deadline it logs and stops.
SELF_DEADLINE_SECONDS = 2.0

#: Toggle file, resolved against the gate's ``repo_root``. Deliberately NOT
#: `.claude/wardens/config.json` -- anything iterating that file's warden entries (the
#: `/wardens` skill) would surface a pseudo-warden.
#:
#: This is a REPO-level switch, not a per-worktree one, exactly like `config.json`:
#: the gates resolve ``repo_root`` to the PRIMARY checkout, so a copy placed inside a
#: linked worktree is never read. Confirmed the hard way during natural-usage
#: verification -- a worktree-local toggle produced no observation from a real commit
#: gate, and the same run proved the mechanism, since the gate read
#: `backends: [claude, gpt, glm]` from the primary repo's `config.json` while the
#: worktree has no `config.json` at all. Put it in the primary repo, or use the env
#: var (which is process-scoped and works from anywhere).
TOGGLE_RELPATH = Path(".claude") / "wardens" / "opa-shadow.json"
ENV_FLAG = "DEUS_OPA_SHADOW"

#: Roles this observer covers. The OPA ``gate`` value for a backend-scoped
#: attestation IS the role name -- `latest_by_backend` and `latest` use DIFFERENT
#: vocabularies and conflating them silently reads an always-empty bucket:
#:
#:   - `latest` (Hermes's single-attestation path) is keyed ``"code-review"``.
#:   - `latest_by_backend` (Phase 0's multi-backend index) is keyed ``"code-reviewer"``.
#:
#: Established by Phase 0, not inferred: `policy/attestation-v1.schema.json`'s gate
#: enum is ``["code-review", "code-reviewer", "ai-eng-warden"]`` (it was widened
#: precisely to admit the role names); `tests/test_attestation_store.py` uses
#: ``gate="code-reviewer"`` for every `latest_by_backend` case and ``"code-review"``
#: for every `latest` case; `policy/guardrails_test.rego`'s `backend_verdict`
#: fixtures index under ``"code-reviewer"`` while its `valid_ship` fixtures use
#: ``"code-review"``. An earlier draft of this module mapped to the `latest`
#: vocabulary and would have reported every real attestation as `no-attestation` --
#: invisible in mocked tests, and invisible live too while nothing writes the index.
#: `tests/test_cc_shadow.py::TestGateVocabularyRoundTrip` pins this against the real
#: store and the real Rego so it cannot regress silently.
#:
#: Membership is an explicit allowlist even though the relation is identity, so an
#: unknown or newly-added role is ignored rather than guessed at.
#:
#: ``verification-gate`` is NOT in Phase 0's schema enum: nothing has ever written a
#: verification attestation. Phase 1 only reads, so it queries an empty bucket
#: harmlessly; a Phase 2 write path must add the key to the schema first.
SHADOW_ROLES = frozenset({"code-reviewer", "ai-eng-warden", "verification-gate"})

#: Verdicts a legacy backend entry may hold that mean "satisfied" without producing an
#: attestation. TRIVIAL is the human trivial-commit bypass; `AttestationStore.issue`
#: rejects it outright, so a bypassed commit can never have a matching attestation.
TRIVIAL_VERDICT = "TRIVIAL"

_CLASS_OPA_UNREACHABLE = "opa-unreachable"
_CLASS_LEDGER_UNREADABLE = "ledger-unreadable"
_CLASS_SUBJECT_UNRESOLVABLE = "subject-unresolvable"
_CLASS_TRIVIAL_BYPASS = "trivial-bypass"
_CLASS_GENERATION_MISMATCH = "generation-mismatch"
#: Verdict map came back empty, but the follow-up generation probe failed -- so we have
#: no evidence either way. Deliberately NOT folded into `no-attestation`: that label is
#: a factual claim of absence, and making a failed probe indistinguishable from a real
#: absence would corrupt exactly the coherence signal Phase 1 exists to collect.
_CLASS_GENERATION_UNKNOWN = "generation-unknown"
_CLASS_NO_ATTESTATION = "no-attestation"
_CLASS_AGREE_ALLOW = "agree-allow"
_CLASS_AGREE_BLOCK = "agree-block"
_CLASS_VERDICT_MISMATCH = "verdict-mismatch"

_ENABLED_CACHE: bool | None = None


def _reset_cache_for_tests() -> None:
    """Clear the memoized flag. Tests only -- hook processes are short-lived."""
    global _ENABLED_CACHE
    _ENABLED_CACHE = None


def shadow_enabled(repo_root: Path) -> bool:
    """True when the shadow observer is switched on. Default OFF, and never raises.

    Resolution order: ``DEUS_OPA_SHADOW`` env var (``1``/``0``, authoritative when
    set, for one-shot session/test toggling) -> the repo's ``opa-shadow.json``
    (durable, discoverable, survives however the hook process is spawned) -> False.
    Memoized: a hook process handles one event and exits.
    """
    global _ENABLED_CACHE
    if _ENABLED_CACHE is not None:
        return _ENABLED_CACHE
    enabled = False
    try:
        raw_env = os.environ.get(ENV_FLAG)
        if raw_env is not None:
            enabled = raw_env.strip() == "1"
        else:
            path = Path(repo_root) / TOGGLE_RELPATH
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                enabled = isinstance(data, dict) and data.get("enabled") is True
    except Exception:  # noqa: BLE001 -- a broken toggle must mean OFF, never a crash
        enabled = False
    _ENABLED_CACHE = enabled
    return enabled


def split_backends(required_backends) -> tuple[list[str], list[str]]:
    """Split configured backends into (evaluated, skipped-as-unknown).

    Mirrors ``_evaluate_backends``'s own handling exactly: an id that is neither
    ``claude`` nor a known model backend is WARNED AND SKIPPED there, never blocking.
    Without this the shadow would treat a typo'd `backends` entry as a missing verdict
    and report a false ``verdict-mismatch`` on every observation -- poisoning the very
    report Phase 2 is meant to read. Skipped ids are logged rather than silently
    dropped, so the divergence stays visible.
    """
    evaluated: list[str] = []
    skipped: list[str] = []
    for backend in required_backends or []:
        name = str(backend)
        (evaluated if name == BACKEND_CLAUDE or name in KNOWN_MODEL_BACKENDS
         else skipped).append(name)
    return evaluated, skipped


def would_be_blocking(verdicts: dict, required_backends) -> list:
    """Re-derive ``_evaluate_backends``'s blocking set from OPA's facts.

    Strict AND over the required backends; ``COULD_NOT_RUN`` fails OPEN (skip, never
    block) exactly as the legacy evaluator does; a missing verdict blocks. Returns the
    (backend, verdict) pairs that are NOT satisfied -- empty means "would allow", the
    same convention ``_evaluate_backends`` uses.
    """
    blocking = []
    for backend in required_backends:
        verdict = verdicts.get(backend)
        if verdict == "SHIP" or verdict == "COULD_NOT_RUN":
            continue
        blocking.append([backend, verdict])
    return blocking


def redact_error(category: str, exc: BaseException) -> dict:
    """Describe an exception WITHOUT its message, which routinely embeds absolute paths.

    Verified, not assumed: ``git_subject._git`` wraps ``CalledProcessError``, whose
    ``str()`` is ``Command '['git', '-C', '<abs worktree path>', ...]' returned ...``;
    and ``str(OSError)`` on a file error is ``[Errno N] ...: '<abs path>'``. Logging
    either verbatim would break this module's own no-raw-paths contract -- the exact
    trap `means-end-consistency` describes, since the log claims to be redacted.

    Keeps what is actually useful for triage: a fixed-vocabulary category, the
    exception's type name, and a sha256 of the message so two occurrences of the same
    failure are still recognizably the same without the message ever being stored --
    the same discipline `hermes_warden_gate._log` applies to commands.
    """
    import hashlib

    return {
        "category": category,
        "exception": type(exc).__name__,
        "detail_sha256": hashlib.sha256(str(exc).encode("utf-8", "replace")).hexdigest(),
    }


def _read_ledger_generation() -> tuple[int | None, dict | None]:
    """Unlocked read of the ledger's generation. Returns (generation, redacted error).

    Error values are either a fixed literal category (no external data, safe verbatim)
    or a ``redact_error`` dict -- never a raw exception string.
    """
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            doc = json.load(f)
    except FileNotFoundError:
        return None, {"category": "absent"}
    except (OSError, json.JSONDecodeError) as exc:
        return None, redact_error("unreadable", exc)
    inner = doc.get("warden_attestations")
    if not isinstance(inner, dict):
        return None, {"category": "missing-root-key"}
    generation = inner.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        return None, {"category": "generation-not-an-integer"}
    return generation, None


def _log(entry: dict) -> None:
    """Append one JSONL line. Best-effort and silent -- never raises, never prints.

    Redaction matches hermes_warden_gate._log: only hashes, git oids and verdict
    tokens are ever written. No repo paths, no commands, no review reasons.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except (OSError, TypeError, ValueError):
        pass


def observe(
    *,
    role: str,
    worktree: Path,
    required_backends,
    legacy_blocking,
    legacy_claude_verdict: str | None,
) -> None:
    """Record what OPA would have decided, alongside what the gate actually decided.

    ``role`` is the warden role the gate just evaluated, and is also the OPA ``gate``
    value for backend-scoped attestations (see SHADOW_ROLES). A role that is not a
    Phase 1 target (plan-reviewer, threat-modeler, anything new) is silently ignored
    rather than guessed at.

    Returns None unconditionally. Every failure -- including a bug in this module --
    is swallowed, because a shadow observer must never be able to affect a gate.
    """
    try:
        if role not in SHADOW_ROLES:
            return None
        _observe(
            gate=role, role=role, worktree=worktree,
            required_backends=required_backends, legacy_blocking=legacy_blocking,
            legacy_claude_verdict=legacy_claude_verdict,
        )
    except Exception:  # noqa: BLE001 -- intentionally broad: the containment floor
        pass
    return None


def _observe(
    *, gate, role, worktree, required_backends, legacy_blocking, legacy_claude_verdict,
) -> None:
    start = time.monotonic()
    legacy_blocking = [list(pair) for pair in (legacy_blocking or [])]
    legacy_decision = "block" if legacy_blocking else "allow"
    evaluated, skipped = split_backends(required_backends)

    entry: dict = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": "observe",
        "gate": gate,
        "role": role,
        "legacy_decision": legacy_decision,
        "legacy_blocking": legacy_blocking,
        "required_backends": evaluated,
        "skipped_backends": skipped,
    }

    generation, ledger_error = _read_ledger_generation()
    try:
        repo_id, subject_key = resolve_subject(Path(worktree))
        subject_error = None
    except GitSubjectError as exc:
        # NOT str(exc): git_subject wraps CalledProcessError, whose message embeds the
        # absolute `git -C <worktree>` path. See redact_error.
        repo_id, subject_key, subject_error = None, None, redact_error("git-subject", exc)

    entry["repo_id"] = repo_id
    entry["subject_key"] = subject_key
    entry["expected_generation"] = generation

    opa_input = {
        "contract_version": 1,
        "enforcement_point": "claude-code.pre_tool_use",
        "operation": "git.commit",
        "repo_id": repo_id,
        "subject_key": subject_key,
        "expected_generation": generation,
        "gate": gate,
        "required_backends": evaluated,
    }
    result = query_backend_verdicts(OPA_URL, opa_input, _remaining(start))
    entry["opa_verdict_map"] = result.verdicts

    # Precedence is fixed and documented: an unreachable policy engine tells us nothing
    # about the ledger or the subject, so it outranks both; a TRIVIAL bypass explains an
    # empty map, so it outranks the generic empty-map classes.
    if not result.ok:
        classification = _CLASS_OPA_UNREACHABLE
        entry["opa_error"] = result.error
        would_be_decision = None
    elif ledger_error is not None:
        classification = _CLASS_LEDGER_UNREADABLE
        entry["ledger_error"] = ledger_error
        would_be_decision = None
    elif subject_error is not None:
        classification = _CLASS_SUBJECT_UNRESOLVABLE
        entry["subject_error"] = subject_error
        would_be_decision = None
    else:
        would_be_blocking_set = would_be_blocking(result.verdicts, evaluated)
        would_be_decision = "block" if would_be_blocking_set else "allow"
        entry["would_be_blocking"] = would_be_blocking_set
        if not result.verdicts:
            if legacy_claude_verdict == TRIVIAL_VERDICT:
                classification = _CLASS_TRIVIAL_BYPASS
            else:
                # An empty map is only evidence of "no attestation" once we know OPA's
                # snapshot is in step with disk. If the follow-up generation probe
                # fails, we have no coherence evidence at all -- recording that as
                # `no-attestation` would assert a factual absence we did not observe,
                # and would bury a partial OPA failure inside the very data this phase
                # exists to measure. It gets its own label instead.
                opa_generation = query_generation(OPA_URL, _remaining(start))
                entry["opa_generation"] = opa_generation
                if opa_generation is None:
                    classification = _CLASS_GENERATION_UNKNOWN
                elif opa_generation != generation:
                    classification = _CLASS_GENERATION_MISMATCH
                else:
                    classification = _CLASS_NO_ATTESTATION
        elif would_be_decision == legacy_decision:
            classification = (
                _CLASS_AGREE_BLOCK if legacy_decision == "block" else _CLASS_AGREE_ALLOW
            )
        else:
            classification = _CLASS_VERDICT_MISMATCH

    entry["would_be_decision"] = would_be_decision
    entry["agreement"] = would_be_decision == legacy_decision
    entry["classification"] = classification
    entry["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
    _log(entry)


def _remaining(start: float) -> float:
    """Per-call timeout: the smaller of the OPA budget and what's left of the deadline.

    Floored at 50 ms so a nearly-exhausted budget still makes a real attempt that fails
    fast, rather than passing a zero/negative timeout to urllib.
    """
    left = SELF_DEADLINE_SECONDS - (time.monotonic() - start)
    return min(OPA_TIMEOUT_SECONDS, max(0.05, left))

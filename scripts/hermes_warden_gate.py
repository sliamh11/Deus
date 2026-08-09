#!/usr/bin/env python3
"""Fail-closed Hermes `pre_tool_call` adapter for scripts/warden_policy.

Hermes's own hook engine fails OPEN on script error, timeout, non-JSON, or
an unrecognized response shape -- so this script can never rely on a
non-zero exit code to block. Every failure path below explicitly emits
Hermes's canonical block shape and exits 0; only a fully-validated
affirmative allow ever prints `{}`.

Ordering (found wrong, then fixed, by adversarial plan review): enrollment
is resolved BEFORE commit-form validation. An earlier draft validated the
commit form first, which would have blocked an ORDINARY `git commit` in an
UNENROLLED repo just for lacking the required flags -- directly
contradicting "unenrolled repos behave normally." The correct order: (1) is
this even a terminal/commit-shaped call at all -- if not, allow immediately,
no ledger/OPA involved; (2) resolve repo_id and check enrollment from the
LOCAL ledger (a pure disk read, always available even if OPA is down) -- if
not enrolled, allow immediately, regardless of commit form; (3) only for an
ENROLLED repo does the supported-commit-form check and the OPA SHIP query
apply.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError
from warden_policy.command_parser import classify
from warden_policy.git_subject import GitSubjectError, resolve_repo_id, resolve_subject_key
from warden_policy.opa_client import query_decision

LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"
LOG_PATH = Path.home() / ".config" / "deus" / "guardrails" / "logs" / "decisions.jsonl"
OPA_URL = "http://127.0.0.1:8181"
OPA_TIMEOUT_SECONDS = 0.75
SHIM_SELF_DEADLINE_SECONDS = 2.5  # stays comfortably under Hermes's configured hook timeout (3s)


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


def _hash_command(command: str) -> str:
    import hashlib
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _log(entry: dict) -> None:
    # Best-effort, never raises -- logging must never itself cause a block/crash.
    # Deliberately never logs raw repo paths, full commands, or commit reasons/messages --
    # only command_sha256, a hash, per the redaction requirement from plan review.
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def decide(payload: dict) -> dict:
    start = time.monotonic()
    tool_name = payload.get("tool_name", "")
    command = payload.get("tool_input", {}).get("command", "")
    cwd = payload.get("cwd") or "."

    log_entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "command_sha256": _hash_command(command) if command else None,
    }

    if tool_name != "terminal":
        return {}

    classification = classify(command)
    log_entry["classification"] = "supported" if classification.supported else (
        "unsupported" if classification.is_commit_shaped else "not-commit-shaped"
    )

    if not classification.is_commit_shaped:
        return {}

    if classification.dash_c_rejected:
        # LIA-524: an unsafe `-C` value makes repo identity UNKNOWABLE -- fail closed
        # UNCONDITIONALLY, before any repo/enrollment lookup. Never fall back to `cwd` here:
        # that fallback is exactly the flaw this check exists to close (an untrustworthy `-C`
        # value must not silently degrade to "pretend -C wasn't there").
        result = _block(classification.reason)
        log_entry.update(decision="block", reason=classification.reason, fail_closed=True)
        _log(log_entry)
        return result

    # LIA-524: `-C <path>` genuinely redirects which repository the eventual `git`
    # invocation operates on -- resolving identity from `cwd` alone would let an enrolled
    # repo's ledger check be bypassed by running from an unenrolled directory and pointing
    # `-C` at the enrolled target. A relative `-C` value resolves against `cwd`, matching
    # git's own real `-C` semantics (git resolves a relative `-C` against its own process
    # cwd at invocation time).
    repo_path = Path(cwd)
    if classification.dash_c_target:
        target = Path(classification.dash_c_target)
        repo_path = target if target.is_absolute() else Path(cwd) / target
    store = AttestationStore(LEDGER_PATH, opa_base_url=OPA_URL)

    # The shared lock is held across the ENTIRE read-then-decide sequence below, including the
    # OPA network call -- not just the local disk read. Found missing by adversarial code
    # review: an earlier version released the lock right after reading, so a writer's
    # write+PUT+read-back transaction could interleave with this adapter's read-then-query,
    # contradicting the reader/writer coordination this module documents. Holding it across a
    # bounded (<=750ms) OPA call is the accepted cost of that guarantee.
    try:
        with store.locked_read() as doc:
            inner = doc["warden_attestations"]

            try:
                repo_id = resolve_repo_id(repo_path)
            except GitSubjectError as exc:
                result = _block(f"could not resolve repo identity ({exc}) -- failing closed")
                log_entry.update(decision="block", reason=result["message"], fail_closed=True)
                _log(log_entry)
                return result

            log_entry["repo_id"] = repo_id
            enrolled = inner.get("config", {}).get("enforced_repos", {}).get(repo_id, {}).get("enabled", False)

            if not enrolled:
                log_entry.update(decision="allow", reason="repo not enrolled", fail_closed=False)
                _log(log_entry)
                return {}

            if not classification.supported:
                result = _block(classification.reason)
                log_entry.update(decision="block", reason=classification.reason, fail_closed=False)
                _log(log_entry)
                return result

            try:
                subject_key = resolve_subject_key(repo_path)
            except GitSubjectError as exc:
                result = _block(f"could not resolve staged tree ({exc}) -- failing closed")
                log_entry.update(decision="block", reason=result["message"], fail_closed=True)
                _log(log_entry)
                return result

            log_entry["subject_key"] = subject_key
            opa_input = {
                "contract_version": 1,
                "enforcement_point": "hermes.pre_tool_call",
                "operation": "git.commit",
                "repo_id": repo_id,
                "subject_key": subject_key,
                "expected_generation": inner["generation"],
                # Required now that guardrails.rego's "code-review" decision bodies are
                # gate-scoped (Phase 0 of the Claude-Code-gate-to-OPA migration) -- an
                # omitted gate would make input.gate undefined, and undefined ==/!= never
                # fires in Rego, silently falling through to the file's own default deny.
                "gate": "code-review",
            }

            remaining = SHIM_SELF_DEADLINE_SECONDS - (time.monotonic() - start)
            timeout = min(OPA_TIMEOUT_SECONDS, max(0.05, remaining))
            decision = query_decision(OPA_URL, opa_input, timeout_seconds=timeout)

            if not decision.ok:
                result = _block(
                    f"guardrails policy engine (OPA) unreachable or returned an invalid response "
                    f"({decision.error}) -- failing closed; try: "
                    f"launchctl kickstart -k gui/$UID/com.deus.warden-opa; "
                    f"log: ~/.config/deus/guardrails/logs/"
                )
                log_entry.update(decision="block", reason=result["message"], fail_closed=True)
                _log(log_entry)
                return result

            if not decision.allow:
                result = _block(decision.reason)
                log_entry.update(decision="block", reason=decision.reason, fail_closed=False)
                _log(log_entry)
                return result

            log_entry.update(decision="allow", reason=decision.reason, fail_closed=False)
            log_entry["latency_ms"] = round((time.monotonic() - start) * 1000, 1)
            _log(log_entry)
            return {}
    except AttestationStoreError as exc:
        result = _block(f"guardrails ledger unreadable ({exc}) -- failing closed")
        log_entry.update(decision="block", reason=result["message"], fail_closed=True)
        _log(log_entry)
        return result


def main() -> int:
    # Total exception containment: ANY failure anywhere above results in a block, never an
    # uncaught exception (which Hermes would treat as an allow, per its fail-open design).
    try:
        payload = json.load(sys.stdin)
        result = decide(payload)
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: this is the fail-closed floor
        result = _block(f"guardrails adapter internal error ({exc}) -- failing closed")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

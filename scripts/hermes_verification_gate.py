#!/usr/bin/env python3
"""Fail-closed Hermes `pre_tool_call` adapter for the verification-gate (LIA-524).

Sibling to `hermes_warden_gate.py` (the code-review/`terminal` gate) -- same fail-closed
philosophy, same JSON stdin/stdout protocol, same total-exception-containment in `main()` --
but NOT a modification of that file. A fully independent script, matching
`hermes_plan_review_gate.py`'s own documented precedent, wired via a SEPARATE
`~/.hermes/config.yaml` hook entry (a different `command` string, same `matcher: "terminal"`).

6-step sequence (Design section B.4 of the reviewed LIA-524 plan). verification-gate has NO
diff-trigger condition at all -- unconditional once enrolled, mirroring Claude Code's own
`run_verification_gate` (evidence-before-claims, not diff-pattern-triggered):
1. `tool_name != "terminal"` -> allow.
2. not commit-shaped -> allow.
3. `-C` rejected (unsafe value) -> block UNCONDITIONALLY, before any repo/enrollment lookup --
   identity is unknowable, never fall back to cwd.
4. resolve repo_path from `-C`'s target if present, else cwd; resolve repo_id; not
   verification_gate_enabled for that repo -> allow, regardless of commit form.
5. enrolled but commit form unsupported -> block -- a repo enrolled only in verification-gate
   must get its own commit-form validation, not silently inherit it from
   `hermes_warden_gate.py`.
6. resolve subject_key from the SAME corrected repo_path; query OPA
   (`gate: "verification-gate"`, no backend -- `latest`-indexed, single-verdict, matching
   Claude Code's own single-marker `run_verification_gate`); block on non-allow.
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
LOG_PATH = Path.home() / ".config" / "deus" / "guardrails" / "logs" / "verification-gate-decisions.jsonl"
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
    # only command_sha256, a hash, matching hermes_warden_gate.py's redaction discipline.
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
        # UNCONDITIONALLY, before any repo/enrollment lookup. Never fall back to `cwd`.
        result = _block(classification.reason)
        log_entry.update(decision="block", reason=classification.reason, fail_closed=True)
        _log(log_entry)
        return result

    repo_path = Path(cwd)
    if classification.dash_c_target:
        target = Path(classification.dash_c_target)
        repo_path = target if target.is_absolute() else Path(cwd) / target

    store = AttestationStore(LEDGER_PATH, opa_base_url=OPA_URL)

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
            verification_gate_enabled = inner.get("config", {}).get("enforced_repos", {}).get(
                repo_id, {},
            ).get("verification_gate_enabled", False)

            if not verification_gate_enabled:
                log_entry.update(decision="allow", reason="repo not verification-gate-enrolled", fail_closed=False)
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
                "gate": "verification-gate",
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

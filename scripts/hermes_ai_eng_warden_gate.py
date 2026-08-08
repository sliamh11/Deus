#!/usr/bin/env python3
"""Fail-closed Hermes `pre_tool_call` adapter for the ai-eng-warden gate (LIA-524).

Sibling to `hermes_warden_gate.py` (the code-review/`terminal` gate) -- same fail-closed
philosophy, same JSON stdin/stdout protocol, same total-exception-containment in `main()` --
but NOT a modification of that file. A fully independent script, matching
`hermes_plan_review_gate.py`'s own documented precedent, wired via a SEPARATE
`~/.hermes/config.yaml` hook entry (a different `command` string, same `matcher: "terminal"`).

7-step sequence (Design section A.5 of the reviewed LIA-524 plan):
1. `tool_name != "terminal"` -> allow.
2. not commit-shaped -> allow.
3. `-C` rejected (unsafe value) -> block UNCONDITIONALLY, before any repo/enrollment lookup --
   identity is unknowable, never fall back to cwd.
4. resolve repo_path from `-C`'s target if present, else cwd; resolve repo_id; not
   ai_eng_warden_enabled for that repo -> allow, regardless of commit form (unenrolled repos
   behave normally).
5. enrolled but commit form unsupported -> block UNCONDITIONALLY -- this must run BEFORE the
   diff-trigger check (step 6), never after. Round-12 TOCTOU finding: checking the
   pre-execution diff first would let an attacker use a side-effecting, unvalidated command
   (command substitution, a chained command) to dirty an LLM-pattern file AFTER the diff check
   but AS PART OF the same shell invocation the hook is about to allow -- e.g.
   `git commit -a -m "$(touch memory_indexer.py)"`. Form validation must gate everything
   diff-dependent, exactly as it already does for `hermes_warden_gate.py`'s own gate.
6. diff doesn't touch an LLM file pattern (`warden_policy.llm_file_patterns`) -> allow (gate
   doesn't fire).
7. resolve subject_key from the SAME corrected repo_path; query OPA
   (`gate: "ai-eng-warden", backend: "hermes"` -- a single fixed self-identifying backend id,
   not the multi-backend `backend_verdict_map`/`required_backends` machinery, which Hermes has
   no caller for yet); block on non-allow.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError
from warden_policy.command_parser import classify
from warden_policy.git_subject import GitSubjectError, resolve_repo_id, resolve_subject_key
from warden_policy.llm_file_patterns import AI_ENG_BASENAMES, AI_ENG_DIR_PREFIXES
from warden_policy.opa_client import query_decision

LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"
LOG_PATH = Path.home() / ".config" / "deus" / "guardrails" / "logs" / "ai-eng-warden-decisions.jsonl"
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


def _diff_touches_llm_files(repo_path: Path) -> bool:
    """Check if repo_path's staged/unstaged changes touch LLM-related files. Fail-closed.

    Mirrors `codex_warden_hooks.py::_diff_touches_llm_files` exactly, using the shared
    `llm_file_patterns` constants -- but against `repo_path` (the `-C`-resolved target repo),
    not necessarily the process's own cwd.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        if result.returncode != 0:
            return True
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        result2 = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=repo_path, timeout=10,
        )
        if result2.returncode != 0:
            return True
        files += result2.stdout.strip().split("\n") if result2.stdout.strip() else []
    except Exception:
        return True
    for f in files:
        basename = f.split("/")[-1]
        if basename in AI_ENG_BASENAMES:
            return True
        if f.startswith(AI_ENG_DIR_PREFIXES):
            return True
    return False


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
            ai_eng_warden_enabled = inner.get("config", {}).get("enforced_repos", {}).get(
                repo_id, {},
            ).get("ai_eng_warden_enabled", False)

            if not ai_eng_warden_enabled:
                log_entry.update(decision="allow", reason="repo not ai-eng-warden-enrolled", fail_closed=False)
                _log(log_entry)
                return {}

            # Round-12 TOCTOU fix: commit-form validation is UNCONDITIONAL once enrolled --
            # it must run BEFORE the diff-trigger check below, never after. See module docstring.
            if not classification.supported:
                result = _block(classification.reason)
                log_entry.update(decision="block", reason=classification.reason, fail_closed=False)
                _log(log_entry)
                return result

            if not _diff_touches_llm_files(repo_path):
                log_entry.update(decision="allow", reason="diff does not touch an LLM file pattern", fail_closed=False)
                _log(log_entry)
                return {}

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
                "gate": "ai-eng-warden",
                "backend": "hermes",
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

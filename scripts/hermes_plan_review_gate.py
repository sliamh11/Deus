#!/usr/bin/env python3
"""Fail-closed Hermes `pre_tool_call` adapter for the plan-review gate (LIA-523).

Sibling to `hermes_warden_gate.py` (the code-review/`terminal` gate) -- same fail-closed
philosophy, same JSON stdin/stdout protocol, same total-exception-containment in `main()` --
but NOT a modification of that file. Wired via a SEPARATE `~/.hermes/config.yaml` hook entry
(`matcher: "write_file|patch"`), independent of the existing `matcher: "terminal"` entry.

Design, in one paragraph (see docs/decisions/opa-warden-attestations-v1.md and
LIA-523's plan for the full 15-round review history that arrived here): branch on path shape
FIRST. Every ABSOLUTE target resolves its own precise repo_id (parent-directory walk + `git -C`)
and is checked against THAT repo's own enrollment/attestation state -- `payload["cwd"]` is never
consulted for this branch, so there is nothing for it to diverge from. Only when a RELATIVE
target is present (v1 cannot precisely resolve those) does `payload["cwd"]` get used, purely as
a coarse "is this call even in scope for gating" pre-check -- never to grant an allow. A patch
mixing absolute and relative targets gets BOTH checks independently; the call blocks if either
would block it, so an absolute target's own enrollment status is never bypassed by an unrelated
relative sibling (the exact false-ALLOW the GPT co-gate found and this design closes).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from warden_policy.attestation_store import AttestationStore, AttestationStoreError
from warden_policy.git_subject import GitSubjectError, resolve_repo_id
from warden_policy.opa_client import query_decision

LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"
LOG_PATH = Path.home() / ".config" / "deus" / "guardrails" / "logs" / "plan-review-decisions.jsonl"
OPA_URL = "http://127.0.0.1:8181"
OPA_TIMEOUT_SECONDS = 0.75
SHIM_SELF_DEADLINE_SECONDS = 2.5  # stays comfortably under Hermes's configured hook timeout (3s)

_HERMES_HOME = Path.home() / ".hermes" / "hermes-agent"


def _block(message: str) -> dict:
    return {"action": "block", "message": message}


def _log(entry: dict) -> None:
    # Best-effort, never raises -- logging must never itself cause a block/crash. Deliberately
    # never logs raw paths or session ids beyond what's already needed to debug a decision --
    # matches hermes_warden_gate.py's redaction discipline.
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def _nearest_existing_dir(path: Path) -> Path:
    """Walk up to the nearest EXISTING ancestor directory of *path*.

    `git -C <dir>` requires an existing directory; a brand-new file's own path (or a deeply
    nested new subdirectory) may not exist yet. Filesystem root always exists, so this
    terminates.
    """
    p = path if path.is_dir() else path.parent
    while not p.exists():
        parent = p.parent
        if parent == p:
            break
        p = parent
    return p


def _resolve_repo_id_for_path(path_str: str) -> str | None:
    """Precise repo_id resolution from a real filesystem path. None if no git repo is found
    anywhere up the tree -- the caller must treat that as fail-closed, never as "unenrolled".

    Resolves symlinks FIRST (`Path.resolve()`, non-strict -- follows every symlink that
    exists, leaves a nonexistent tail component literal) before walking to the nearest
    existing ancestor. Found missing by the GPT co-gate: without this, a path inside an
    unenrolled/attested repo that is itself a symlink (or has a symlinked ancestor
    directory) into a DIFFERENT, enrolled-and-unattested repo would resolve repo_id from
    the symlink's own location, not its real target -- a write that follows the symlink
    lands in the protected repo while the gate checked the wrong one.
    """
    try:
        resolved = Path(path_str).resolve()
    except OSError:
        return None
    try:
        return resolve_repo_id(_nearest_existing_dir(resolved))
    except GitSubjectError:
        return None


def _v4a_target_paths(patch_text: str) -> list[str] | None:
    """Extract every target path (file_path, plus new_path for MOVE ops) from a V4A patch.

    Reuses Hermes's OWN parser (`tools.patch_parser.parse_v4a_patch`) rather than
    re-implementing V4A parsing independently -- re-implementing risks silent divergence from
    what Hermes's real `patch` tool actually does. `patch_parser.py` is intentionally
    lightweight (stdlib-only imports, no file I/O in `parse_v4a_patch` itself -- confirmed by
    reading the module before relying on it) so this import carries none of the heavy
    transitive-dependency-chain cost `tools.file_tools` would have. Appended (not inserted at
    position 0) so it can never shadow this script's own `scripts/`-local modules.

    Returns None if the module can't be imported or the patch fails to parse at all -- the
    caller must treat that as fail-closed (cannot determine targets -> cannot verify safety),
    never as "no targets, allow".
    """
    if str(_HERMES_HOME) not in sys.path:
        sys.path.append(str(_HERMES_HOME))
    try:
        from tools.patch_parser import parse_v4a_patch
    except ImportError:
        return None
    try:
        operations, error = parse_v4a_patch(patch_text)
    except Exception:
        return None
    if error is not None and not operations:
        return None
    paths: list[str] = []
    for op in operations:
        if op.file_path:
            paths.append(op.file_path)
        if op.new_path:
            paths.append(op.new_path)
    return paths


def _target_paths(tool_name: str, tool_input: dict) -> tuple[list[str] | None, dict | None]:
    """Return (target_paths, early_result). early_result is non-None when this call is either
    out of scope for this gate (allow) or malformed in a way that must fail closed (block) --
    in either case the caller returns early without doing any repo/enrollment resolution."""
    if tool_name == "write_file":
        path = tool_input.get("path")
        if not path:
            return None, {}
        return [path], None

    if tool_name == "patch":
        mode = tool_input.get("mode", "replace")
        if mode == "replace":
            path = tool_input.get("path")
            if not path:
                return None, {}
            return [path], None
        if mode == "patch":
            patch_text = tool_input.get("patch")
            if not patch_text:
                return None, _block("patch mode='patch' call has no 'patch' text -- cannot determine targets, failing closed")
            paths = _v4a_target_paths(patch_text)
            if paths is None:
                return None, _block("failed to parse V4A patch content -- cannot determine targets, failing closed")
            if not paths:
                return None, {}
            return paths, None
        # unrecognized mode -- out of scope for this gate, not this gate's job to guess.
        return None, {}

    # not a write-shaped tool call at all.
    return None, {}


def decide(payload: dict) -> dict:
    start = time.monotonic()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    session_id = payload.get("session_id", "")
    cwd = payload.get("cwd") or "."

    log_entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tool_name": tool_name}

    target_paths, early_result = _target_paths(tool_name, tool_input)
    if early_result is not None:
        return early_result

    absolute_targets = [p for p in target_paths if os.path.isabs(p)]
    # Tilde-prefixed paths (~/...) are NOT absolute per os.path.isabs() and are deliberately
    # never expanded here -- Hermes's own tilde-expansion uses a process-dependent HOME that
    # can differ between gateway and interactive-CLI contexts (the same environment-dependent-
    # resolution risk class this design avoids for cwd). They fall through to the relative
    # branch below, same as any other relative path.
    relative_targets = [p for p in target_paths if not os.path.isabs(p)]

    store = AttestationStore(LEDGER_PATH, opa_base_url=OPA_URL)

    try:
        block_message = None

        # Resolve + enrollment-check every absolute target first (cheap, no OPA). The OPA
        # decision only depends on repo_id/session_id, never on the individual path -- querying
        # once per FILE in a multi-file V4A patch touching one repo would multiply OPA
        # round-trips against a shared, bounded self-deadline for no discriminating benefit
        # (found by code review: enough files in one repo could exhaust the deadline and time
        # out, which Hermes treats as fail-OPEN -- undermining the fail-closed guarantee for
        # exactly the multi-file workload this design exists to handle). Dedup to one query per
        # distinct enrolled repo_id instead.
        #
        # The resolution loop itself (a `git` subprocess + a ledger-lock acquisition per
        # target) is not free either -- found by the GPT co-gate: a sufficiently large patch
        # could exhaust SHIM_SELF_DEADLINE_SECONDS just walking targets, before ever reaching
        # the (already-deduped) OPA query, and an internal timeout here still lets Hermes's own
        # hook-level timeout be the thing that intervenes -- which fails OPEN, not closed. Cache
        # repeated paths (MOVE's file_path/new_path can coincide with another target) and check
        # the deadline before each resolution -- if exhausted, fail closed explicitly ourselves
        # rather than let an external timeout decide.
        _RESOLUTION_SAFETY_MARGIN_SECONDS = 0.3  # leaves enough budget to emit a block response
        repo_id_cache: dict[str, str | None] = {}
        enrolled_repo_ids: set[str] = set()
        for path in absolute_targets:
            if time.monotonic() - start > SHIM_SELF_DEADLINE_SECONDS - _RESOLUTION_SAFETY_MARGIN_SECONDS:
                block_message = "too many targets to resolve within the gate's self-deadline -- failing closed"
                break
            if path not in repo_id_cache:
                repo_id_cache[path] = _resolve_repo_id_for_path(path)
            repo_id = repo_id_cache[path]
            if repo_id is None:
                block_message = block_message or "could not resolve a git repository for an absolute write target -- failing closed"
                continue
            with store.locked_read() as doc:
                enrolled = bool(
                    doc["warden_attestations"].get("config", {}).get("enforced_repos", {}).get(repo_id, {}).get("plan_review_enabled", False)
                )
            if enrolled:
                enrolled_repo_ids.add(repo_id)

        if len(enrolled_repo_ids) > 1:
            block_message = block_message or f"ambiguous target repo -- write spans {len(enrolled_repo_ids)} distinct plan-review-enrolled repos in one call, failing closed"
        elif not block_message and len(enrolled_repo_ids) == 1:
            # Skip the OPA round-trip entirely if a block is already determined (e.g. the
            # resolution deadline above was already exhausted) -- the outcome is already a
            # block, and spending more of the shrinking time budget on a network call whose
            # answer can no longer change that outcome only makes the timeout risk worse.
            (repo_id,) = enrolled_repo_ids
            with store.locked_read() as doc:
                inner = doc["warden_attestations"]
                remaining = SHIM_SELF_DEADLINE_SECONDS - (time.monotonic() - start)
                timeout = min(OPA_TIMEOUT_SECONDS, max(0.05, remaining))
                opa_input = {
                    "contract_version": 1,
                    "enforcement_point": "hermes.pre_tool_call",
                    "operation": "file.write",
                    "repo_id": repo_id,
                    "session_id": session_id,
                    "expected_generation": inner["generation"],
                    "gate": "plan-review",
                }
                opa_decision = query_decision(OPA_URL, opa_input, timeout_seconds=timeout)
            if not opa_decision.ok:
                block_message = (
                    f"guardrails policy engine (OPA) unreachable or returned an invalid "
                    f"response ({opa_decision.error}) -- failing closed"
                )
            elif not opa_decision.allow:
                block_message = opa_decision.reason or "no valid plan-review SHIP for this session"

        if relative_targets:
            guess_repo_id = _resolve_repo_id_for_path(cwd)
            if guess_repo_id is not None:
                with store.locked_read() as doc:
                    inner = doc["warden_attestations"]
                    guess_enrolled = bool(
                        inner.get("config", {}).get("enforced_repos", {}).get(guess_repo_id, {}).get("plan_review_enabled", False)
                    )
                if guess_enrolled:
                    block_message = block_message or (
                        "relative-path write/patch targets are not supported by the plan-review "
                        "gate in v1 -- retry with an absolute path"
                    )
            # guess_repo_id is None (cwd not in a git repo) or not enrolled: this half imposes
            # no block -- matches the pre-existing, already-shipped ungated behavior for that
            # specific call (named v1 residual gap), not a regression.
    except AttestationStoreError as exc:
        block_message = f"guardrails ledger unreadable ({exc}) -- failing closed"

    if block_message:
        log_entry.update(decision="block", reason=block_message)
        _log(log_entry)
        return _block(block_message)

    log_entry.update(decision="allow", latency_ms=round((time.monotonic() - start) * 1000, 1))
    _log(log_entry)
    return {}


def main() -> int:
    # Total exception containment: ANY failure anywhere above results in a block, never an
    # uncaught exception (which Hermes would treat as an allow, per its fail-open design).
    try:
        payload = json.load(sys.stdin)
        result = decide(payload)
    except Exception as exc:  # noqa: BLE001 -- intentionally broad: this is the fail-closed floor
        result = _block(f"plan-review gate adapter internal error ({exc}) -- failing closed")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

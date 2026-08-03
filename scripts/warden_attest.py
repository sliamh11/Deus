#!/usr/bin/env python3
"""Manual attestation CLI for scripts/warden_policy.

Subcommands: enroll, unenroll, issue, inspect, check, sync.
Typed exit codes: 0 OK, 1 usage error, 2 git/subject resolution error,
3 not-activated (persisted but OPA PUT failed -- run `sync`), 6 CONFLICT
(index changed mid-issuance).

See docs/HERMES_WARDEN_OPA.md and docs/decisions/opa-warden-attestations-v1.md
for the full design. This CLI never exposes a manual --tree-sha override --
the subject is always derived from the repository's current index, since an
override would defeat the property the whole system exists to provide.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_io import agent_output, is_agent_context  # noqa: E402
from warden_policy.attestation_store import AttestationStore, AttestationStoreError
from warden_policy.git_subject import GitSubjectError, resolve_repo_id, resolve_subject_key
from warden_policy.command_parser import classify

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_GIT_ERROR = 2
EXIT_NOT_ACTIVATED = 3
EXIT_CONFLICT = 6

DEFAULT_LEDGER_PATH = Path.home() / ".config" / "deus" / "guardrails" / "attestations-v1.json"


def _store(args) -> AttestationStore:
    ledger_path = Path(args.ledger_path) if getattr(args, "ledger_path", None) else DEFAULT_LEDGER_PATH
    return AttestationStore(ledger_path, opa_base_url=args.opa_url)


def _emit(args, payload: dict) -> None:
    out = agent_output(
        payload,
        use_json=getattr(args, "json", False) or is_agent_context(),
        compact=getattr(args, "compact", False),
        select=getattr(args, "select", None),
        long_fields=("reason", "error"),
    )
    if out is not None:
        print(out)
    else:
        for k, v in payload.items():
            print(f"{k}: {v}")


def cmd_enroll(args) -> int:
    store = _store(args)
    try:
        repo_id = resolve_repo_id(Path(args.repo))
    except GitSubjectError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_GIT_ERROR
    result = store.enroll(repo_id)
    _emit(args, {"ok": result.ok, "activated": result.activated, "repo_id": repo_id,
                  "generation": result.generation, "error": result.error})
    return EXIT_OK if result.activated else EXIT_NOT_ACTIVATED


def cmd_unenroll(args) -> int:
    store = _store(args)
    try:
        repo_id = resolve_repo_id(Path(args.repo))
    except GitSubjectError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_GIT_ERROR
    try:
        result = store.unenroll(repo_id)
    except AttestationStoreError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_USAGE
    _emit(args, {"ok": result.ok, "activated": result.activated, "repo_id": repo_id,
                  "generation": result.generation, "error": result.error})
    return EXIT_OK if result.activated else EXIT_NOT_ACTIVATED


def cmd_issue(args) -> int:
    store = _store(args)
    repo_path = Path(args.repo)
    try:
        repo_id = resolve_repo_id(repo_path)
        subject_key = resolve_subject_key(repo_path)
    except GitSubjectError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_GIT_ERROR

    result = store.issue(
        repo_id=repo_id, gate=args.gate, subject_key=subject_key, verdict=args.verdict,
        issuer_kind=args.issuer_kind, reviewer_id=args.reviewer_id, reason=args.reason,
    )

    # Race check: recompute the subject after issuance -- if the index changed
    # mid-issuance, the record we just wrote is for a tree that's no longer
    # staged. The record itself is harmless (it can't authorize a different
    # tree), but the caller should know their intent didn't land as expected.
    try:
        post_subject = resolve_subject_key(repo_path)
    except GitSubjectError as exc:
        _emit(args, {"ok": result.ok, "error": f"post-issuance subject check failed: {exc}"})
        return EXIT_GIT_ERROR

    if post_subject != subject_key:
        _emit(args, {"ok": False, "error": "index changed during issuance (CONFLICT)",
                      "attested_subject": subject_key, "current_subject": post_subject})
        return EXIT_CONFLICT

    _emit(args, {"ok": result.ok, "activated": result.activated, "repo_id": repo_id,
                  "subject_key": subject_key, "generation": result.generation, "error": result.error})
    return EXIT_OK if result.activated else EXIT_NOT_ACTIVATED


def cmd_inspect(args) -> int:
    store = _store(args)
    try:
        repo_id = resolve_repo_id(Path(args.repo))
    except GitSubjectError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_GIT_ERROR
    records = store.inspect(repo_id)
    if getattr(args, "json", False) or is_agent_context():
        _emit(args, {"repo_id": repo_id, "records": records})
    else:
        print(f"repo_id: {repo_id}")
        for r in sorted(records, key=lambda r: r["issued_at"]):
            print(f"  [{r['issued_at']}] {r['verdict']:7s} {r['subject']['key']}  ({r['reason']})")
    return EXIT_OK


def cmd_check(args) -> int:
    """Dry-run: build the exact OPA input for the given command and print the live decision."""
    store = _store(args)
    repo_path = Path(args.repo)
    classification = classify(args.command)
    if not classification.is_commit_shaped:
        _emit(args, {"is_commit_shaped": False, "allow": True, "reason": classification.reason})
        return EXIT_OK
    if not classification.supported:
        _emit(args, {"is_commit_shaped": True, "supported": False, "allow": False,
                      "reason": classification.reason})
        return EXIT_OK
    try:
        repo_id = resolve_repo_id(repo_path)
        subject_key = resolve_subject_key(repo_path)
    except GitSubjectError as exc:
        _emit(args, {"ok": False, "error": str(exc)})
        return EXIT_GIT_ERROR
    doc = store.read_locked()
    generation = doc["warden_attestations"]["generation"]
    _emit(args, {
        "is_commit_shaped": True, "supported": True,
        "repo_id": repo_id, "subject_key": subject_key, "expected_generation": generation,
        "note": "this prints the OPA INPUT this command would generate; query OPA directly "
                "(or the live shim) for the actual allow/deny decision.",
    })
    return EXIT_OK


def cmd_sync(args) -> int:
    store = _store(args)
    result = store.sync()
    _emit(args, {"ok": result.ok, "activated": result.activated,
                  "generation": result.generation, "error": result.error})
    return EXIT_OK if result.activated else EXIT_NOT_ACTIVATED


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="warden_attest.py")
    parser.add_argument("--ledger-path", default=None)
    parser.add_argument("--opa-url", default="http://127.0.0.1:8181")
    parser.add_argument("--json", action="store_true", help="emit JSON (agent-native)")
    parser.add_argument("--compact", action="store_true", help="compact JSON (strip nulls, truncate long fields)")
    parser.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    p_enroll = sub.add_parser("enroll")
    p_enroll.add_argument("--repo", required=True)
    p_enroll.set_defaults(func=cmd_enroll)

    p_unenroll = sub.add_parser("unenroll")
    p_unenroll.add_argument("--repo", required=True)
    p_unenroll.set_defaults(func=cmd_unenroll)

    p_issue = sub.add_parser("issue")
    p_issue.add_argument("--repo", required=True)
    p_issue.add_argument("--gate", default="code-review")
    p_issue.add_argument("--verdict", required=True, choices=["SHIP", "REVISE", "BLOCK"])
    p_issue.add_argument("--issuer-kind", default="manual", choices=["manual", "script"])
    p_issue.add_argument("--reviewer-id", required=True)
    p_issue.add_argument("--reason", required=True)
    p_issue.set_defaults(func=cmd_issue)

    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--repo", required=True)
    p_inspect.set_defaults(func=cmd_inspect)

    p_check = sub.add_parser("check")
    p_check.add_argument("--repo", required=True)
    p_check.add_argument("--command", required=True)
    p_check.set_defaults(func=cmd_check)

    p_sync = sub.add_parser("sync")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

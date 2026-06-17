#!/usr/bin/env python3
"""Role-parameterized model-reviewer driver (the out-of-band warden backend runner).

Runs ONE warden role through ONE model backend and (optionally) records the verdict into
the warden store so the co-gate can read it. This is the model-reviewer half of the
provider-agnostic warden mechanism; the Claude half is the in-session subagent.

    # Advisory (no marker written):
    python3 scripts/codex_warden.py --role code-reviewer

    # Co-gate: review the working tree with GPT and record the verdict:
    python3 scripts/codex_warden.py --role code-reviewer --backend gpt --warden-mark

Security/cost notes live in codex_review.py (the codex backend reuses that engine):
read-only sandbox, per-run sentinel boundary, subscription-billed via the codex CLI.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_review as cr  # noqa: E402  (engine reused by the codex backend; ReviewError/exit map)
import codex_warden_hooks as whooks  # noqa: E402  (verdict store, cross-context, loop counter)
from _agent_io import agent_output, is_agent_context  # noqa: E402
from _exit_codes import (  # noqa: E402
    ABSTAIN,
    AUTH_ERROR,
    INTERNAL_ERROR,
    RATE_LIMIT,
    SUCCESS,
    USAGE_ERROR,
)
from warden_review import registry  # noqa: E402
from warden_review.backends.base import ReviewRequest  # noqa: E402
from warden_review.constants import BACKEND_GPT, store_key  # noqa: E402
from warden_review.roles import ROLE_SPECS  # noqa: E402

_CODE_FROM_CATEGORY = {"rate_limit": RATE_LIMIT, "auth": AUTH_ERROR}


def _render_human(role: str, backend: str, v) -> None:
    print(f"═══ {role} via {backend} — {v.verdict} ═══")
    if v.could_not_run:
        print(f"COULD_NOT_RUN (gate fails open): {v.error}")
        return
    if v.summary:
        print(f"\n{v.summary}")
    for f in v.findings:
        loc = f"L{f['line']}" if f.get("line") is not None else "—"
        print(f"  [{f.get('severity','?')}/{f.get('confidence','?')}] "
              f"{f.get('file','?')}:{loc} — {f.get('finding','')}")
    if not v.findings:
        print("(no findings)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run a warden role through a model backend; optionally record the verdict.",
    )
    ap.add_argument("--role", required=True, choices=sorted(ROLE_SPECS),
                    help="warden role to review")
    ap.add_argument("--backend", default=BACKEND_GPT,
                    help=f"model backend id (default {BACKEND_GPT}; registered: "
                         f"{', '.join(registry.available_backends()) or '(none)'})")
    ap.add_argument("--warden-mark", action="store_true",
                    help="record the verdict into the warden store (co-gate); advisory if omitted")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range (default: working tree)")
    src.add_argument("--diff-file", help="path to a unified diff file to review")
    src.add_argument("--content-file",
                     help="path to a file read verbatim as the review target (non-diff roles, "
                          "e.g. plan-reviewer reviewing a plan file)")
    ap.add_argument("--model", help="backend model id (default: backend/config default)")
    ap.add_argument("--timeout", type=float, default=cr.DEFAULT_TIMEOUT,
                    help=f"per-call timeout seconds (default {cr.DEFAULT_TIMEOUT:.0f})")
    ap.add_argument("--out", help="also write the full JSON verdict to this file")
    ap.add_argument("--json", action="store_true", help="emit JSON (agent-native)")
    ap.add_argument("--compact", action="store_true", help="compact JSON")
    ap.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    args = ap.parse_args(argv)

    spec = ROLE_SPECS[args.role]
    skey = store_key(args.role, args.backend)

    try:
        # cfr.repo_root() returns a str; the warden-hooks helpers need a Path (they do
        # `repo_root / ".git"` etc.). The backend's cwd stays a str (codex --cd).
        root = Path(cr.cfr.repo_root())
    except cr.ReviewError as exc:
        sys.stderr.write(f"[codex-warden] {exc.message}\n")
        return exc.code

    if not registry.is_registered(args.backend):
        sys.stderr.write(
            f"[codex-warden] unknown backend '{args.backend}'. Registered: "
            f"{', '.join(registry.available_backends()) or '(none)'}.\n"
        )
        return USAGE_ERROR

    try:
        # --content-file (non-diff roles) and --diff-file are mutually exclusive; route whichever
        # was supplied into the gatherer's diff_file slot (_gather_diff ignores it; _gather_file reads it).
        content = spec.gather(str(root), args.rev_range, args.content_file or args.diff_file)
    except cr.ReviewError as exc:
        sys.stderr.write(f"[codex-warden] {exc.message}\n")
        return exc.code

    # Empty change: nothing to review. Record SHIP (abstain) so the gate isn't stuck.
    if not content.strip():
        sys.stderr.write("[codex-warden] empty change — nothing to review (abstain).\n")
        if args.warden_mark:
            whooks.record_script_verdict(root, skey, "SHIP",
                                         "abstain: no reviewable content")
            whooks.note_model_review_round(root, args.role, args.backend, "SHIP",
                                           whooks.read_claude_verdict(root, args.role))
        return ABSTAIN

    rules_path = Path(spec.rules_path)
    if not rules_path.is_absolute():
        rules_path = root / rules_path
    cross_context = whooks.read_cross_context(root, args.role, for_backend=args.backend)

    backend = registry.get_backend(args.backend)
    verdict = backend.review(ReviewRequest(
        role=args.role, rules_path=str(rules_path), content=content, cwd=str(root),
        cross_context=cross_context, model=args.model, timeout=args.timeout,
    ))

    payload = {
        "role": args.role, "backend": args.backend, "verdict": verdict.verdict,
        "findings": verdict.findings, "summary": verdict.summary, "error": verdict.error,
    }
    out = agent_output(payload, use_json=args.json or is_agent_context(),
                       compact=args.compact, select=args.select,
                       long_fields=("findings", "summary", "error"))
    if out is not None:
        print(out)
    else:
        _render_human(args.role, args.backend, verdict)
    if args.out:
        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    if args.warden_mark:
        reason = (verdict.error if verdict.could_not_run
                  else verdict.summary or f"{args.backend} {verdict.verdict}")
        whooks.record_script_verdict(root, skey, verdict.verdict, reason)
        whooks.write_model_cross_review(root, args.role, args.backend, verdict.verdict,
                                        verdict.findings, verdict.summary)
        whooks.note_model_review_round(root, args.role, args.backend, verdict.verdict,
                                       whooks.read_claude_verdict(root, args.role))

    if verdict.could_not_run:
        return _CODE_FROM_CATEGORY.get(verdict.category, INTERNAL_ERROR)
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())

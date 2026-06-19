#!/usr/bin/env python3
"""One-command warden CO-GATE marker: mark the in-session Claude verdict AND run+mark the
GPT verdict for a role, BOTH into the single per-worktree bucket the commit/edit gate reads.

Why this exists: the co-gate needs two verdicts in the SAME bucket — the in-session Claude
warden's (a `.<marker>` + role-keyed verdict) and a model backend's (`<role>@<backend>`).
Marking them by hand is a 2-step that ALSO has to resolve the FLAT-vs-per-worktree bucket
correctly: the Claude `mark` CLI defaults its `--repo-root` to the script's own toplevel (so
from a linked worktree's own copy it writes the FLAT in-worktree store), while the GPT driver
namespaces under the PRIMARY repo's per-worktree bucket via ``primary_repo_root`` — and the
gate (warden-shim.sh → ``--git-common-dir`` parent) reads the per-worktree bucket. Get the two
out of sync and the gate sees only one verdict and blocks. This wrapper resolves the worktree
ONCE and routes BOTH marks through ``primary_repo_root`` + ``worktree_override``, the exact
resolution the gate uses, so they can never split.

It does NOT run Claude headlessly (there is no headless-Claude warden runner; the Claude
verdict is produced by the in-session ``Agent(subagent_type=...)`` dispatch + the
verdict-tracker hook). You pass that already-decided Claude verdict in; the wrapper records it
and runs the GPT half.

    # After the in-session plan-reviewer/code-reviewer agent returned SHIP:
    python3 scripts/cogate.py --role code-reviewer --claude-verdict SHIP \
        --claude-reason "code-reviewer SHIP: no blocking issues"

    # Target a specific worktree from any cwd (out-of-band driver):
    python3 scripts/cogate.py --role plan-reviewer --claude-verdict SHIP \
        --claude-reason "..." --content-file plan.md --worktree-root /path/to/wt

Exit codes (typed, agent-native — see scripts/_exit_codes.py):
    0  SUCCESS        co-gate will PASS (Claude accepted AND GPT SHIP; or --skip-gpt + accepted)
    2  USAGE_ERROR    bad arguments, or the Claude mark was REFUSED (e.g. bg-session TRIVIAL,
                      or a TRIVIAL bypass after a prior REVISE/BLOCK) — GPT is NOT run
    5  INTERNAL_ERROR a recorded verdict is REVISE/BLOCK — the gate will BLOCK
    4/5/7             GPT COULD_NOT_RUN (auth/internal/rate): the gate fails OPEN (non-blocking),
                      but this exits non-zero + warns LOUDLY so the operator sees GPT did not run
Cross-platform: pathlib + in-process calls / arg-list only, no shell.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_review as cr  # noqa: E402  (default worktree resolution + DEFAULT_TIMEOUT)
import codex_warden  # noqa: E402  (the GPT half is its main(), invoked in-process)
import codex_warden_hooks as whooks  # noqa: E402  (mark_warden, bucket resolution, readback)
from _agent_io import agent_output, is_agent_context  # noqa: E402
from _exit_codes import INTERNAL_ERROR, SUCCESS, USAGE_ERROR  # noqa: E402
from warden_review.constants import BACKEND_GPT, store_key  # noqa: E402
from warden_hooks.verdict_store import _read_verdicts  # noqa: E402

# Only the GPT-wired warden roles (warden_review.constants.WIRED_ROLES) have a model backend, so
# only these can be co-gated. The token is the EXACT codex_warden.py --role string (no aliases)
# so the marker lookup and the GPT invocation are single-sourced and can never disagree.
# Claude-only roles (threat-modeler/.threat-modeled, verification-gate/.verified) have no GPT
# backend; mark those with `codex_warden_hooks.py mark <marker> SHIP` directly.
ROLE_TO_MARKER = {
    "plan-reviewer": "plan-reviewed",
    "code-reviewer": "code-reviewed",
    "ai-eng-warden": "ai-eng-reviewed",
}

_PASSING_GPT = "SHIP"
_BLOCKING = {"REVISE", "BLOCK"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Mark the Claude verdict + run/mark the GPT verdict for a warden role, "
                    "both into the per-worktree bucket the co-gate reads.",
    )
    ap.add_argument("--role", required=True, choices=sorted(ROLE_TO_MARKER),
                    help="warden role to co-gate (GPT-wired roles only)")
    ap.add_argument("--claude-verdict", required=True, choices=["SHIP", "TRIVIAL"],
                    help="the in-session Claude warden's already-decided verdict")
    ap.add_argument("--claude-reason", required=True,
                    help="justification for the Claude verdict (audit-logged)")
    ap.add_argument("--worktree-root", default=None,
                    help="target worktree toplevel (default: the cwd's repo/worktree toplevel)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range for the GPT review (default: working tree)")
    src.add_argument("--content-file",
                     help="file read verbatim as the GPT review target (non-diff roles, e.g. a plan)")
    ap.add_argument("--gpt-timeout", type=float, default=cr.DEFAULT_TIMEOUT,
                    help=f"per-call timeout for the GPT half, forwarded as --timeout "
                         f"(default {cr.DEFAULT_TIMEOUT:.0f})")
    ap.add_argument("--gpt-model", help="GPT backend model id (default: backend/config default)")
    ap.add_argument("--skip-gpt", action="store_true",
                    help="mark the Claude verdict only; do NOT run the GPT half (advisory/testing)")
    ap.add_argument("--json", action="store_true", help="emit JSON (agent-native)")
    ap.add_argument("--compact", action="store_true", help="compact JSON")
    ap.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    args = ap.parse_args(argv)

    # 1) Resolve the target worktree ONCE — flag-first (so it works from any cwd), exactly like
    #    codex_warden.py. marker_root = the PRIMARY repo, the bucket namespace the gate uses.
    if args.worktree_root:
        wt = Path(args.worktree_root).resolve(strict=False)
        if not wt.is_dir():
            sys.stderr.write(f"[cogate] --worktree-root does not exist or is not a directory: {wt}\n")
            return USAGE_ERROR
    else:
        try:
            wt = Path(cr.cfr.repo_root())
        except cr.ReviewError as exc:
            sys.stderr.write(f"[cogate] {exc.message}\n")
            return exc.code
    marker_root = whooks.primary_repo_root(wt)
    marker = ROLE_TO_MARKER[args.role]
    bucket = None
    with whooks.worktree_override(wt):
        # Surface where marks land so the operator can see the resolved bucket (the whole point).
        try:
            from warden_hooks.verdict_store import _verdicts_path
            bucket = _verdicts_path(marker_root)
        except Exception:  # pragma: no cover — display-only
            bucket = None

    # 2) Mark the Claude verdict into that bucket. mark_warden enforces the bg-session TRIVIAL
    #    refusal + the post-REVISE/BLOCK guard for free; a non-zero return means REFUSED.
    with whooks.worktree_override(wt):
        claude_rc = whooks.mark_warden(marker, args.claude_verdict, args.claude_reason, marker_root)
    if claude_rc != 0:
        # Refused (e.g. bg TRIVIAL, or trivial-bypass after REVISE). Do NOT burn a GPT call —
        # abort so the operator fixes the Claude side first. mark_warden already printed why.
        sys.stderr.write("[cogate] Claude mark refused — aborting before the GPT half.\n")
        return USAGE_ERROR if claude_rc in (1, 2) else claude_rc

    # 3) Run the GPT half in-process, targeting the SAME worktree via --worktree-root, and
    #    forwarding the timeout. (Skipped for the advisory/test path.)
    gpt_rc = SUCCESS
    if not args.skip_gpt:
        gpt_argv = ["--role", args.role, "--backend", BACKEND_GPT, "--warden-mark",
                    "--worktree-root", str(wt), "--timeout", str(args.gpt_timeout)]
        if args.gpt_model:
            gpt_argv += ["--model", args.gpt_model]
        if args.content_file:
            gpt_argv += ["--content-file", args.content_file]
        elif args.rev_range:
            gpt_argv += ["--rev-range", args.rev_range]
        gpt_rc = codex_warden.main(gpt_argv)

    # 4) Read BOTH verdicts back from the resolved bucket and compute the combined outcome by
    #    mirroring run_warden_backends_gate: Claude passes on SHIP or accepted-TRIVIAL; GPT passes
    #    on SHIP; GPT COULD_NOT_RUN makes the real gate fail OPEN (non-blocking).
    with whooks.worktree_override(wt):
        claude_verdict = whooks.read_claude_verdict(marker_root, args.role)
        gpt_verdict = None
        if not args.skip_gpt:
            gpt_verdict = (_read_verdicts(marker_root).get(store_key(args.role, BACKEND_GPT)) or {}).get("verdict")

    # Outcome
    gate_blocked = (claude_verdict in _BLOCKING) or (gpt_verdict in _BLOCKING)
    gpt_could_not_run = (not args.skip_gpt) and gpt_rc != SUCCESS and gpt_verdict not in (_PASSING_GPT, *_BLOCKING)
    if args.skip_gpt:
        passed = claude_verdict not in _BLOCKING
        outcome = "PASS (claude-only, --skip-gpt)" if passed else "BLOCK"
        exit_code = SUCCESS if passed else INTERNAL_ERROR
    elif gate_blocked:
        outcome, exit_code = "BLOCK", INTERNAL_ERROR
    elif gpt_could_not_run:
        outcome, exit_code = "GPT_COULD_NOT_RUN (gate fails open)", (gpt_rc or INTERNAL_ERROR)
    elif gpt_verdict == _PASSING_GPT:
        outcome, exit_code = "PASS", SUCCESS
    else:
        # GPT verdict missing/unrecognized with a zero rc — be conservative, treat as not-green.
        outcome, exit_code = f"NOT GREEN (gpt={gpt_verdict!r})", INTERNAL_ERROR

    payload = {
        "role": args.role, "outcome": outcome, "exit_code": exit_code,
        "claude_verdict": claude_verdict, "gpt_verdict": gpt_verdict,
        "bucket": str(bucket) if bucket else None,
    }
    out = agent_output(payload, use_json=args.json or is_agent_context(),
                       compact=args.compact, select=args.select)
    if out is not None:
        print(out)
    else:
        print(f"═══ co-gate {args.role} — {outcome} ═══")
        print(f"  claude: {claude_verdict}   gpt: {gpt_verdict if not args.skip_gpt else '(skipped)'}")
        if bucket:
            print(f"  bucket: {bucket}")
        if gpt_could_not_run:
            sys.stderr.write(
                "[cogate] WARNING: the GPT backend COULD NOT RUN — the real co-gate fails OPEN "
                "(it will not block the commit), but no GPT review actually happened. "
                "Investigate (auth/rate/timeout) before relying on this verdict.\n"
            )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

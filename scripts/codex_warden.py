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

External agent platforms (no Claude Code session) should shell out to
``scripts/review_runner.py`` instead of this module: it wraps ``run_review()`` below with a
verdict-aware exit-code contract, JSON-by-default output, and an advisory-only guarantee
(it never reads OR writes co-gate state). See docs/REVIEW_RUNNER.md.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import NamedTuple

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
from warden_review.backends.base import ReviewRequest, Verdict  # noqa: E402
from warden_review.constants import (  # noqa: E402
    BACKEND_GPT,
    VERDICT_COULD_NOT_RUN,
    VERDICT_SHIP,
    store_key,
)
from warden_review.roles import ROLE_SPECS  # noqa: E402

_CODE_FROM_CATEGORY = {"rate_limit": RATE_LIMIT, "auth": AUTH_ERROR}

_ABSTAIN_REASON = "abstain: no reviewable content"


class ReviewOutcome(NamedTuple):
    """Everything a caller needs to render, mark, and exit on one review.

    Deliberately richer than ``(payload, exit_code)``: ``main()`` still needs the Verdict
    OBJECT (``_render_human`` reads the ``could_not_run`` *property*, which is not a payload
    key) plus ``root``/``marker_root``/``skey`` for its ``--warden-mark`` and abstain marker
    writes. A 2-tuple could not preserve those paths.

    ``payload`` holds the SAME 6 keys ``main()`` has always printed and written to ``--out``.
    ``abstain`` rides here on the tuple rather than inside ``payload`` so ``main()``'s output
    contract stays byte-identical for its in-process caller (``cogate.py``) and its tests.
    ``message`` is the single stderr line the caller should emit; it is RETURNED rather than
    printed so an agent-facing wrapper can surface it as structured JSON instead.
    """

    exit_code: int
    payload: dict | None          # None only on an early-failure path (nothing was reviewed)
    verdict: Verdict | None       # the backend Verdict; None unless the backend actually ran
    abstain: bool
    root: Path | None
    marker_root: Path | None
    skey: str | None
    message: str                  # stderr line for the caller to emit ("" when there is none)
    files_not_reviewed: tuple[str, ...] = ()  # dropped at the max-files cap; non-empty == the
                                  # review was INCOMPLETE (already reflected in `verdict`).


def run_review(
    role: str,
    backend: str,
    *,
    worktree_root: str | None = None,
    rev_range: str | None = None,
    diff_file: str | None = None,
    content_file: str | None = None,
    rules_path: str | None = None,
    model: str | None = None,
    timeout: float = cr.DEFAULT_TIMEOUT,
    use_cross_context: bool = True,
    max_files: int | None = None,
) -> ReviewOutcome:
    """Resolve the target root, gather the content, and run ONE backend over it.

    PURE ADVISORY: this function never writes verdict-store / marker / cross-review state.
    Recording is the caller's job (``main()`` does it under ``--warden-mark``).

    ``rules_path`` overrides the role's default rules file; ``use_cross_context=False``
    suppresses the co-gate cross-reviewer READ (and, with it, the marker-root resolution that
    only that read needs); ``max_files`` overrides the engine's per-file review cap. All three
    default to the legacy co-gate behavior so every existing caller is unchanged.

    A SHIP whose review was TRUNCATED by the max-files cap is downgraded to ``COULD_NOT_RUN``
    here rather than in one caller, so the co-gate marking path (``main --warden-mark``) can
    never record an incomplete review as an approval either.
    """
    # Load WARDEN_GLM_* from ~/deus/.env (gitignored) if present — scoped to the GLM prefix so it
    # cannot affect any other backend. No-op when the file is absent or the keys are already set.
    _load_glm_env()

    spec = ROLE_SPECS[role]
    skey = store_key(role, backend)

    if worktree_root:
        # Flag-first: resolve worktree_root WITHOUT cr.cfr.repo_root(). That call raises
        # ReviewError → USAGE_ERROR when cwd is outside a git repo, so resolving the flag only
        # afterwards would still make `--worktree-root <wt>` fail from an arbitrary cwd —
        # defeating the out-of-band targeting this flag exists for. The worktree toplevel drives
        # BOTH the review target (diff gather + sandbox cwd) and the verdict bucket, which is
        # exactly what an out-of-band co-gate driver wants: "review worktree X, mark into X."
        root = Path(worktree_root).resolve(strict=False)
        if not root.is_dir():
            return ReviewOutcome(
                USAGE_ERROR, None, None, False, None, None, skey,
                f"[codex-warden] --worktree-root does not exist or is not a directory: {root}",
            )
    else:
        try:
            # cfr.repo_root() returns a str; the warden-hooks helpers need a Path (they do
            # `repo_root / ".git"` etc.). The backend's cwd stays a str (codex --cd).
            root = Path(cr.cfr.repo_root())
        except cr.ReviewError as exc:
            return ReviewOutcome(exc.code, None, None, False, None, None, skey,
                                 f"[codex-warden] {exc.message}")

    # `root` (the worktree toplevel) is for gathering the diff + the codex sandbox cwd.
    # Warden state (verdict store, cross-review files, loop counter) is namespaced under
    # the PRIMARY repo's per-worktree bucket — the same one the commit gate reads (it uses
    # warden-shim.sh's git-common-dir REPO_ROOT). So state I/O uses `marker_root` + an
    # explicit `worktree_override(root)`, making the bucket independent of the process cwd.
    # Resolved ONLY for co-gate callers: primary_repo_root() shells out to `git rev-parse`,
    # which is pointless work and a spurious failure mode for a purely advisory run that will
    # never touch marker state (and lets file-based input work with no git at all).
    marker_root = whooks.primary_repo_root(root) if use_cross_context else None

    if not registry.is_registered(backend):
        return ReviewOutcome(
            USAGE_ERROR, None, None, False, root, marker_root, skey,
            f"[codex-warden] unknown backend '{backend}'. Registered: "
            f"{', '.join(registry.available_backends()) or '(none)'}.",
        )

    try:
        # --content-file (non-diff roles) and --diff-file are mutually exclusive; route whichever
        # was supplied into the gatherer's diff_file slot (_gather_diff ignores it; _gather_file reads it).
        content = spec.gather(str(root), rev_range, content_file or diff_file)
    except cr.ReviewError as exc:
        return ReviewOutcome(exc.code, None, None, False, root, marker_root, skey,
                             f"[codex-warden] {exc.message}")

    # Empty change: nothing to review. The CALLER decides whether to record an abstain SHIP.
    if not content.strip():
        payload = {"role": role, "backend": backend, "verdict": VERDICT_SHIP,
                   "findings": [], "summary": _ABSTAIN_REASON, "error": ""}
        return ReviewOutcome(ABSTAIN, payload, None, True, root, marker_root, skey,
                             "[codex-warden] empty change — nothing to review (abstain).")

    resolved_rules = Path(rules_path) if rules_path else Path(spec.rules_path)
    if not resolved_rules.is_absolute():
        resolved_rules = root / resolved_rules

    cross_context = ""
    if use_cross_context:
        # Narrowly scoped — only the cross-context read needs the worktree override; the
        # backend.review() call below is cwd-agnostic (it reviews in-memory content).
        with whooks.worktree_override(root):
            cross_context = whooks.read_cross_context(marker_root, role, for_backend=backend)

    backend_impl = registry.get_backend(backend)
    verdict = backend_impl.review(ReviewRequest(
        role=role, rules_path=str(resolved_rules), content=content, cwd=str(root),
        cross_context=cross_context, model=model, timeout=timeout,
        is_diff=spec.is_diff, max_files=max_files,
    ))

    message = ""
    if verdict.files_not_reviewed and verdict.is_ship:
        # The engine dropped files at the max-files cap, so this SHIP was produced without ever
        # seeing part of the change — it is not an approval. COULD_NOT_RUN is the exact verdict
        # ("could not assess"), is defined repo-wide as never equal to SHIP, and is already
        # handled by both callers (the co-gate audit-logs it; review_runner exits non-zero).
        # Done HERE, not in one caller, so `main --warden-mark` cannot record a truncated SHIP.
        dropped = ", ".join(verdict.files_not_reviewed)
        verdict = Verdict(
            VERDICT_COULD_NOT_RUN,
            summary=verdict.summary,
            raw=verdict.raw,
            error=(f"incomplete review: {len(verdict.files_not_reviewed)} file(s) were dropped "
                   f"at the max-files cap and never reviewed ({dropped}). "
                   f"Re-run with a higher --max-files for full coverage."),
            files_not_reviewed=verdict.files_not_reviewed,
        )
        message = f"[codex-warden] incomplete review — not an approval; dropped: {dropped}"

    payload = {
        "role": role, "backend": backend, "verdict": verdict.verdict,
        "findings": verdict.findings, "summary": verdict.summary, "error": verdict.error,
    }
    code = (_CODE_FROM_CATEGORY.get(verdict.category, INTERNAL_ERROR)
            if verdict.could_not_run else SUCCESS)
    return ReviewOutcome(code, payload, verdict, False, root, marker_root, skey, message,
                         verdict.files_not_reviewed)


def _load_glm_env(path: Path | None = None) -> None:
    """Load ONLY ``WARDEN_GLM_*`` keys from the gitignored ``~/deus/.env`` into ``os.environ``
    when not already set (a real exported env var always wins). No-op when the file is absent.

    Scoped strictly to the ``WARDEN_GLM_`` prefix so it can NEVER change the activation of the
    openai_compat backend (or anything else) — preserving the zero-behavior-change contract for
    users who did not opt into the ``glm`` backend. Called from ``main()`` only (never at import),
    so it cannot poison a test process that imports this module. ``path`` is for tests.
    """
    env_path = path or (Path.home() / "deus" / ".env")
    try:
        text = env_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return  # absent / unreadable → no-op
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("WARDEN_GLM_") and key not in os.environ:
            os.environ[key] = value.strip()


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
    ap.add_argument("--worktree-root", default=None,
                    help="target worktree toplevel for the review + verdict bucket (default: the "
                         "cwd's repo/worktree toplevel). Lets an out-of-band caller target a "
                         "specific worktree's bucket from any cwd, matching codex_warden_hooks.py "
                         "mark --worktree-root.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range (default: working tree)")
    src.add_argument("--diff-file", help="path to a unified diff file to review")
    src.add_argument("--content-file",
                     help="path to a file read verbatim as the review target (non-diff roles, "
                          "e.g. plan-reviewer reviewing a plan file)")
    ap.add_argument("--model", help="backend model id (default: backend/config default)")
    ap.add_argument("--timeout", type=float, default=cr.DEFAULT_TIMEOUT,
                    help=f"per-call timeout seconds (default {cr.DEFAULT_TIMEOUT:.0f})")
    ap.add_argument("--max-files", type=int, default=None,
                    help="per-file review cap (default: the engine default), applied by the gpt "
                         "backend ONLY. A review that drops files is reported as COULD_NOT_RUN, "
                         "never SHIP; raise this for complete coverage of a large change. "
                         "glm/openai_compat are single-call backends: they ignore this flag and "
                         "return COULD_NOT_RUN on oversize content instead of truncating.")
    ap.add_argument("--out", help="also write the full JSON verdict to this file")
    ap.add_argument("--json", action="store_true", help="emit JSON (agent-native)")
    ap.add_argument("--compact", action="store_true", help="compact JSON")
    ap.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    args = ap.parse_args(argv)

    # All resolution/gathering/backend work lives in run_review(); this function keeps its
    # original rendering + marking behavior byte-identically on top of the returned outcome.
    outcome = run_review(
        args.role, args.backend,
        worktree_root=args.worktree_root, rev_range=args.rev_range,
        diff_file=args.diff_file, content_file=args.content_file,
        model=args.model, timeout=args.timeout, max_files=args.max_files,
    )
    root, marker_root, skey = outcome.root, outcome.marker_root, outcome.skey

    if outcome.message:
        sys.stderr.write(outcome.message + "\n")

    # Early-failure paths (bad worktree root, repo_root error, unknown backend, gather error):
    # the stderr line above is the whole output, exactly as before.
    if outcome.payload is None:
        return outcome.exit_code

    # Empty change: nothing to review. Record SHIP (abstain) so the gate isn't stuck.
    if outcome.abstain:
        if args.warden_mark:
            with whooks.worktree_override(root):
                whooks.record_script_verdict(marker_root, skey, "SHIP", _ABSTAIN_REASON)
                whooks.note_model_review_round(marker_root, args.role, args.backend, "SHIP",
                                               whooks.read_claude_verdict(marker_root, args.role))
        return outcome.exit_code

    verdict = outcome.verdict
    payload = outcome.payload
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
        with whooks.worktree_override(root):
            whooks.record_script_verdict(marker_root, skey, verdict.verdict, reason)
            whooks.write_model_cross_review(marker_root, args.role, args.backend, verdict.verdict,
                                            verdict.findings, verdict.summary)
            whooks.note_model_review_round(marker_root, args.role, args.backend, verdict.verdict,
                                           whooks.read_claude_verdict(marker_root, args.role))

    if verdict.could_not_run:
        return _CODE_FROM_CATEGORY.get(verdict.category, INTERNAL_ERROR)
    return SUCCESS


if __name__ == "__main__":
    raise SystemExit(main())

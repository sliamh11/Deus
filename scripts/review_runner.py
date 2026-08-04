#!/usr/bin/env python3
"""Backend-neutral warden review for ANY agent platform - a review runner to shell out to.

This is the external-facing entry point to the Deus warden reviewers. Unlike
``codex_warden.py`` (the co-gate's own driver) it needs no Claude Code session, no persona
loader, and no in-session subagent transport: it is a plain command that reviews a change
set / commit range / patch file / plan file with one model backend and prints ONE JSON object.

SCOPE - review code you control.
This runner executes git inside the target repository and runs the model backend on this
host, so it offers NO protection against a hostile repository or hostile diff content -
exactly like running ``codex_warden.py`` yourself. Two concrete reasons, both verified
rather than assumed:
  * git treats repo-local config as executable. ``filter.<drv>.clean`` / ``.process`` run on
    working-tree comparisons and no flag disables them. (Gathering does pass
    ``--no-ext-diff --no-textconv`` and neutralizes ``core.fsmonitor``, but that is for a
    CANONICAL patch, not as a security boundary - see ``cross_family_review._git_diff_argv``.)
  * The default ``gpt`` backend is ``codex exec``, whose read-only sandbox still permits host
    filesystem READS, so prompt-injected content can induce it to read host files and emit
    them to the model provider.
Reviewing genuinely untrusted input needs OS-level isolation and is deliberately out of scope
here. Do not point this at code you would not already inspect locally.

Output is JSON by DEFAULT (``--human`` opts out). That inverts the usual Deus CLI default
(human-first, JSON opt-in - docs/decisions/printing-press-adoption.md) on purpose: this
tool's only audience is an agent shelling out, and an agent that forgets a flag should still
get parseable output. ``--json`` is accepted too, so a caller that mechanically appends it to
every Deus CLI still works.

The JSON ``verdict`` field is AUTHORITATIVE. Exit codes are the coarse green/not-green signal
for shell callers (see docs/REVIEW_RUNNER.md for the full table); notably both a blocking
review (REVISE/BLOCK) and an internal failure map to 5, because ``_exit_codes.py`` is a fixed
cross-CLI taxonomy we do not extend - disambiguate with ``verdict``.

ADVISORY ONLY, by construction. This runner never writes co-gate state (there is no
``--warden-mark`` flag) and never READS it either (``use_cross_context=False``): that state
belongs to the co-gate's own review loop, and an out-of-band advisory call should neither
consume it nor be steered by it. For co-gate marking use ``codex_warden.py --warden-mark``
or ``cogate.py`` instead.

Usage examples live in docs/REVIEW_RUNNER.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import codex_review as cr  # noqa: E402  (DEFAULT_TIMEOUT / DEFAULT_MAX_FILES only)
import codex_warden as cw  # noqa: E402  (run_review - the shared advisory engine)
from _agent_io import agent_output  # noqa: E402
from _exit_codes import INTERNAL_ERROR, NOT_FOUND, USAGE_ERROR  # noqa: E402
from warden_review import registry  # noqa: E402
from warden_review.constants import (  # noqa: E402
    BACKEND_GPT,
    BLOCKING_VERDICTS,
    VERDICT_COULD_NOT_RUN,
)
from warden_review.roles import ROLE_SPECS  # noqa: E402

#: The Deus checkout that owns THIS script - the source of the default rules files. Anchoring on
#: ``__file__`` is deliberate and is NOT the ``--repo-root`` gotcha orchestration-rules.md warns
#: about: there the intent is "the MAIN repo's marker bucket", so a worktree copy silently
#: retargeting it is a bug. Here the intent is literally "the rules shipped alongside this
#: script", so a worktree checkout SHOULD use its own rules.
RUNNER_ROOT = Path(__file__).resolve().parents[1]

#: ``cross_family_review.split_by_file`` splits on this header and SKIPS every chunk lacking it,
#: so a patch without one reviews nothing at all. Checked up front to fail loudly instead.
_GIT_PATCH_RE = re.compile(r"(?m)^diff --git ")


def _human(payload: dict) -> str:
    lines = [f"=== {payload['role']} via {payload['backend']} - {payload['verdict']} ==="]
    if payload.get("error"):
        lines.append(f"COULD_NOT_RUN (advisory; not an approval): {payload['error']}")
    if payload.get("summary"):
        lines.append("\n" + payload["summary"])
    for f in payload.get("findings", []):
        loc = "L" + str(f["line"]) if f.get("line") is not None else "-"
        lines.append("  [" + f.get("severity", "?") + "/" + f.get("confidence", "?") + "] "
                     + f.get("file", "?") + ":" + loc + " - " + f.get("finding", ""))
    if payload.get("files_not_reviewed"):
        lines.append("NOT REVIEWED (max-files cap): "
                     + ", ".join(payload["files_not_reviewed"]))
    if not payload.get("findings") and not payload.get("error"):
        lines.append("(no findings)")
    return "\n".join(lines)


def _payload(role: str, backend: str, *, error: str, exit_code: int) -> dict:
    """An error payload with the SAME keys as a successful one, so a caller parses one shape."""
    return {"role": role, "backend": backend, "verdict": VERDICT_COULD_NOT_RUN, "findings": [],
            "summary": "", "error": error, "abstain": False, "files_not_reviewed": [],
            "exit_code": exit_code}


def _emit(payload: dict, *, use_json: bool, compact: bool = False,
          select: str | None = None) -> None:
    out = agent_output(payload, use_json=use_json, compact=compact, select=select,
                       long_fields=("findings", "summary", "error"))
    print(out if out is not None else _human(payload))


class _JsonArgumentParser(argparse.ArgumentParser):
    """argparse that reports usage errors as JSON on stdout instead of prose on stderr.

    Stock ``ArgumentParser.error()`` prints usage to stderr and raises ``SystemExit(2)``
    BEFORE any review runs, which would silently break this tool's JSON-always contract for
    exactly the callers least able to cope: an agent parsing stdout. The exit code stays 2
    (``USAGE_ERROR``), matching argparse's own convention.
    """

    def error(self, message: str):  # noqa: D102  (argparse override)
        _emit(_payload("", "", error=f"usage error: {message}", exit_code=USAGE_ERROR),
              use_json=True)
        raise SystemExit(USAGE_ERROR)


def _exit_code_for(payload: dict, engine_code: int) -> int:
    """Map a review outcome to a shell exit code.

    Only the blocking-verdict case differs from what ``run_review`` already computed:
    ``codex_warden`` returns SUCCESS for REVISE/BLOCK (its callers read the store), but a
    shell caller branching on the exit status must not see 0 for a review that says "do not
    ship". Follows cogate.py's blocking-to-INTERNAL_ERROR precedent, not a new code.
    """
    if payload["verdict"] in BLOCKING_VERDICTS:
        return INTERNAL_ERROR
    return engine_code


def _resolve_rules(args) -> tuple[str, dict | None]:
    """Return (rules_path, error_payload). Exactly one is meaningful.

    An explicit override is resolved against the CALLER'S cwd - never against ``--repo``, which
    would silently mean a different file than the caller typed - and is validated by READING it.
    ``is_file()`` would not be enough: an existing-but-unreadable file still falls through
    ``build_rules_digest``'s silent generic-rules fallback (so a review could SHIP under rules
    that were never applied), and a non-UTF-8 file raises ``UnicodeDecodeError``, which is not
    an ``OSError`` and would escape as a traceback with no JSON at all.
    """
    if not args.rules:
        # The built-in default keeps build_rules_digest's tolerant fallback, which is its
        # legitimate purpose (repos that ship no Deus rules file).
        return str(RUNNER_ROOT / ROLE_SPECS[args.role].rules_path), None
    path = Path(args.rules)
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    try:
        path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return "", _payload(args.role, args.backend,
                            error=f"cannot read --rules {path}: {exc}", exit_code=NOT_FOUND)
    return str(path), None


def _check_diff_file(args) -> dict | None:
    """Reject a non-git patch up front; returns an error payload or None.

    ``split_by_file`` skips every chunk that does not begin with the git patch header, so a
    plain unified patch would be reviewed as NOTHING and come back COULD_NOT_RUN - a confusing
    result for input the CLI advertised as valid. An empty/whitespace-only file is NOT a format
    error: it flows on to the normal abstain path.
    """
    if not args.diff_file or not ROLE_SPECS[args.role].is_diff:
        return None
    try:
        text = Path(args.diff_file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return _payload(args.role, args.backend,
                        error=f"cannot read --diff-file {args.diff_file}: {exc}",
                        exit_code=NOT_FOUND)
    if text.strip() and not _GIT_PATCH_RE.search(text):
        return _payload(
            args.role, args.backend,
            error=(f"--diff-file {args.diff_file} is not a git-format patch (no 'diff --git' "
                   f"header), so no file would be reviewed. Produce one with a standard git "
                   f"patch command such as diff, show, or format-patch."),
            exit_code=USAGE_ERROR)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = _JsonArgumentParser(
        description="Run a Deus warden review with any model backend and print JSON. "
                    "Advisory only: never reads or writes co-gate state. Reviews code you "
                    "control - not hardened against a hostile repo (see the module docstring).",
    )
    ap.add_argument("--role", required=True, choices=sorted(ROLE_SPECS),
                    help="warden role to review as")
    ap.add_argument("--backend", default=BACKEND_GPT,
                    help=f"model backend id (default {BACKEND_GPT}; registered: "
                         f"{', '.join(registry.available_backends()) or '(none)'})")
    ap.add_argument("--repo", default=None,
                    help="target repository/worktree toplevel (default: the cwd's repo toplevel)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range (default: working tree)")
    src.add_argument("--diff-file", help="path to a GIT-format unified patch to review")
    src.add_argument("--content-file",
                     help="path to a file read verbatim as the review target (non-diff roles, "
                          "e.g. plan-reviewer reviewing a plan file)")
    ap.add_argument("--rules", default=None,
                    help="override the rules file (default: this install's rules for the role). "
                         "A relative path resolves against the CURRENT directory.")
    ap.add_argument("--model", help="backend model id (default: backend/config default)")
    ap.add_argument("--timeout", type=float, default=cr.DEFAULT_TIMEOUT,
                    help=f"per-call timeout seconds (default {cr.DEFAULT_TIMEOUT:.0f})")
    ap.add_argument("--max-files", type=int, default=None,
                    help=f"per-file review cap (default {cr.DEFAULT_MAX_FILES}), applied by the "
                         f"gpt backend ONLY. A review that drops files is reported as "
                         f"COULD_NOT_RUN, never SHIP; raise this for complete coverage of a "
                         f"large change. glm/openai_compat are single-call backends: they "
                         f"ignore this flag and return COULD_NOT_RUN on oversize content "
                         f"instead of truncating, so they never yield a truncated approval.")
    ap.add_argument("--out", help="also write the JSON payload to this file")
    # --json is redundant with the default but must be ACCEPTED: a generic caller that appends
    # it to every Deus CLI would otherwise get a usage error instead of a review. Passing both
    # --json and --human is contradictory, so argparse rejects it through the same JSON path.
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true", help="force JSON output (already the default)")
    fmt.add_argument("--human", action="store_true", help="render human-readable text, not JSON")
    ap.add_argument("--compact", action="store_true", help="compact JSON")
    ap.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    args = ap.parse_args(argv)

    use_json = not args.human

    def finish(payload: dict) -> int:
        """Write --out FIRST, then emit exactly one JSON object, then return its exit code.

        Order matters: writing after the emit would let an OSError raise a traceback while
        stdout had already claimed a different exit_code, and emitting an error object after
        the real one would print TWO objects - just as unparseable as a traceback.
        """
        if args.out:
            try:
                Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError as exc:
                payload = _payload(args.role, args.backend,
                                   error=f"cannot write --out {args.out}: {exc}",
                                   exit_code=INTERNAL_ERROR)
        _emit(payload, use_json=use_json, compact=args.compact, select=args.select)
        return payload["exit_code"]

    rules_path, err = _resolve_rules(args)
    if err is not None:
        return finish(err)
    err = _check_diff_file(args)
    if err is not None:
        return finish(err)

    # File-based modes carry their own content, so they need no repository at all -- but
    # run_review falls back to repo_root() when no root is given, which hard-fails outside one.
    # Default to the cwd so the documented standalone plan/patch examples work from anywhere.
    # Safe because for these modes `root` is inert: the gatherers ignore it (they read the file),
    # the rules path we pass is absolute, and no marker resolution happens (use_cross_context off).
    repo = args.repo
    if repo is None and (args.content_file or args.diff_file):
        repo = str(Path.cwd())

    try:
        outcome = cw.run_review(
            args.role, args.backend,
            worktree_root=repo, rev_range=args.rev_range,
            diff_file=args.diff_file, content_file=args.content_file,
            rules_path=rules_path, model=args.model, timeout=args.timeout,
            max_files=args.max_files,
            use_cross_context=False,   # never read co-gate state; also skips marker resolution
        )
    except (OSError, UnicodeDecodeError) as exc:
        # roles.py::_gather_file calls Path.read_text() directly, so an absent or non-UTF-8
        # --content-file raises rather than returning the ReviewError the engine converts.
        # Normalize here (not in run_review) so codex_warden.main()'s behavior is untouched.
        return finish(_payload(args.role, args.backend,
                               error=f"cannot read input: {exc}", exit_code=NOT_FOUND))

    if outcome.payload is None:
        # Early failure (unknown backend, bad --repo, unreadable range): run_review returns
        # the operator-facing line rather than printing it, so it becomes the JSON `error`.
        return finish(_payload(args.role, args.backend,
                               error=outcome.message.strip(), exit_code=outcome.exit_code))

    payload = dict(outcome.payload)
    payload["abstain"] = outcome.abstain
    payload["files_not_reviewed"] = list(outcome.files_not_reviewed)
    payload["exit_code"] = _exit_code_for(payload, outcome.exit_code)
    return finish(payload)


if __name__ == "__main__":
    raise SystemExit(main())

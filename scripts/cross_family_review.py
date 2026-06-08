#!/usr/bin/env python3
"""Cross-family local code-review ADVISORY (Gemma 4 12B, calibrated-diff).

A LOCAL, different-family second opinion for a human reviewer — NOT a gate and NOT
an adjudicator. Runs a local code model (different family => independent blind
spots) over a diff with the validated *calibrated-diff* prompt and prints, per file,
a human-readable advisory plus the model's own reasoning so a person can triage each
flag. Nothing here auto-applies, auto-posts, or blocks a commit.

WHY ADVISORY-ONLY (LIA-179, n=35 validation — read before trusting any flag):
  * On real merged diffs the calibrated reviewer false-flags **~29%** of changes,
    and on first-hand adjudication ALL sampled flags were false — including
    confident *phantom crashes* ("MAJOR: will throw TypeError" on correct code).
    So: a human triages every flag; treat "will crash" claims with suspicion.
  * Recall is only validated on SMALL, single-change diffs (caught 7/7). On a large
    (~300-LOC) diff it MISSED the real bug and added noise. This tool warns when a
    file's added-line count exceeds --max-diff-loc; large-diff misses are expected.
  * This supersedes the earlier "surface candidates for a Claude adjudicator/gate"
    design (Qwen + runtime rule-digest), which validated net-negative — both the
    rule digest and the gate framing are gone. Rule-conformance stays with the
    Claude code-reviewer warden; this tool is scoped to bug/correctness/security.

SERVING (kept out of the repo on purpose — user-agnostic):
  Point this at a llama-server (OpenAI-compatible) running a 12B-class code model
  with REASONING DISABLED. gemma models have a thinking mode on by default; if it
  is left on, llama-server returns EMPTY content (finish_reason=length) and this
  tool fails loud rather than reporting a false "clean". Disable it at serve time
  (current llama-server builds: a reasoning-off launch flag — exact flag name varies
  by build, verify against your binary). Configure the endpoint/model via env or
  flags (see below); do NOT point this at the deployed Deus service on :8080 (that
  serves a small model never validated for review — the tool warns if you do).

  This tool requires a reachable local llama-server (no cloud fallback) and fails
  loud with an actionable message if none is found. The code itself is
  cross-platform (git/httpx/pathlib); only the local-server dependency is required.

SECURITY: the diff is untrusted text sent to a LOCAL model with no tools, no file
access, and no egress; output is advisory text shown to a human and nothing
auto-executes. Injected instructions inside a diff can at most produce a fabricated
finding the human discards. If this tool ever gains auto-posting, gating, or
upstream prompt reuse, add prompt-injection boundaries BEFORE that change.

Environment (review-specific first, then the shared llama-cpp vars):
    LLAMA_CPP_REVIEW_BASE_URL  OpenAI-compatible base for the reviewer, e.g.
                               http://127.0.0.1:8099/v1  (preferred)
    LLAMA_CPP_BASE_URL         shared fallback base URL
    LLAMA_CPP_PORT             used to build the base URL when neither above is set
    LLAMA_CPP_REVIEW_MODEL     model name for this surface; falls back to LLAMA_CPP_MODEL
    LLAMA_CPP_MODEL            catch-all; empty => llama-server uses its loaded model

Agent-native (docs/decisions/printing-press-adoption.md): typed exit codes,
``--json`` / ``--compact`` / ``--select``; default output stays human text because
the ADR's own measurement shows JSON regresses for human-facing shapes.

Usage:
    # Advisory review of the current working-tree diff (staged + unstaged):
    python3 scripts/cross_family_review.py

    # A historical commit / range, or an arbitrary diff file:
    python3 scripts/cross_family_review.py --rev-range <sha>
    python3 scripts/cross_family_review.py --diff-file /tmp/some.diff --out /tmp/adv.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _agent_io import agent_output, is_agent_context  # noqa: E402
from _exit_codes import (  # noqa: E402
    ABSTAIN,
    INTERNAL_ERROR,
    NOT_FOUND,
    SUCCESS,
    USAGE_ERROR,
)

try:
    import httpx
except ImportError:  # pragma: no cover - actionable guard for fresh clones
    sys.stderr.write(
        "[cross-family-review] missing dependency 'httpx'. Install it:\n"
        "    python3 -m pip install httpx\n"
    )
    sys.exit(USAGE_ERROR)

# ── Sampling (matches the validated n=35 config; deterministic) ─────────────────
TEMPERATURE = 0
TOP_P = 1.0
SEED = 42
MAX_TOKENS = 1024

# ── Token budgeting (rough chars/4 heuristic; the diff is the only thing we ever
# truncate, never the prompt). The 12B is served with a KV window >= --ctx. ──────
CHARS_PER_TOKEN = 4
DEFAULT_CTX = 8192            # deliberate conservative secondary net (see size guard)
OUTPUT_RESERVE_TOKENS = MAX_TOKENS
SYSTEM_SAFETY_TOKENS = 256

# ── Recall guard: recall was only validated on small single-change diffs. This is
# a HEURISTIC warn threshold (added code lines per file), NOT a measured safe cap. ─
DEFAULT_MAX_DIFF_LOC = 60

CLEAN_SENTINEL = "NO ISSUES FOUND"

SYSTEM = "You are a senior software engineer performing a thorough code review."

# The validated calibrated-diff prompt (STAGE0 "after" preamble + conservative bar,
# retargeted to a unified diff). Verbatim from the n=35 validation runner.
USER_CALIBRATED_DIFF = (
    "You are reviewing a unified-diff EXCERPT of {lang} code from a larger, working "
    "codebase. In the diff, lines starting with `+` are added, `-` are removed, and "
    "unmarked lines are unchanged surrounding context. Review the CHANGE (the added "
    "lines, in the context shown). "
    "Assume every import, type, and module-level constant or variable referenced "
    "but not shown here is correctly defined elsewhere; review ONLY the logic that "
    "is visible.\n\n"
    "Most code you review is correct. Report an issue ONLY if you are confident it "
    "is a REAL defect that would cause wrong behavior, a security vulnerability, or "
    "a crash under normal, in-contract use. Do NOT report: style/formatting "
    "preferences, naming, missing comments, defensive-programming suggestions, or "
    "hypothetical edge cases that cannot occur given the function's contract. When "
    "in doubt, do NOT report it.\n\n"
    "For each genuine issue give: severity (CRITICAL/MAJOR/MINOR), the location, and "
    "a one-sentence explanation. If the change has no real defect, reply exactly "
    "NO ISSUES FOUND.\n\n```diff\n{code}\n```"
)


class ReviewError(Exception):
    """A fatal review failure carrying a typed exit code for main() to return."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _est_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ── Repo / diff acquisition (fail-loud; never let a failure look like an empty tree) ─
def repo_root() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise ReviewError(USAGE_ERROR, f"not a git repo / git missing: {exc}")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        raise ReviewError(NOT_FOUND, f"cannot read {path}: {exc}")


def get_diff(root: str, rev_range: str | None, diff_file: str | None) -> str:
    if diff_file:
        if not os.path.exists(diff_file):
            raise ReviewError(NOT_FOUND, f"--diff-file not found: {diff_file}")
        return _read(diff_file)
    if rev_range:
        # Single sha => that commit vs its first parent; ranges (a..b) pass through.
        spec = [rev_range] if (".." in rev_range) else [f"{rev_range}^!"]
        out = subprocess.run(
            ["git", "-C", root, "diff", *spec], capture_output=True, text=True,
        )
        if out.returncode != 0:
            raise ReviewError(INTERNAL_ERROR,
                              f"git diff failed for '{rev_range}': {out.stderr.strip()}")
        return out.stdout
    # Default: working-tree review = staged + unstaged, against HEAD.
    out = subprocess.run(
        ["git", "-C", root, "diff", "HEAD"], capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise ReviewError(INTERNAL_ERROR, f"git diff HEAD failed: {out.stderr.strip()}")
    return out.stdout


def split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, chunk) pairs on `diff --git` boundaries."""
    if not diff.strip():
        return []
    chunks = re.split(r"(?m)^(?=diff --git )", diff)
    pairs: list[tuple[str, str]] = []
    for chunk in chunks:
        # Skip any leading preamble that is not a file diff — e.g. the commit
        # header `git show` emits before the first `diff --git` (the working-tree
        # and rev-range paths use `git diff`, which has none, but `--diff-file`
        # may be fed a `git show` dump). Without this, that prose becomes a
        # phantom <unknown> "file" sent to the reviewer.
        if not chunk.lstrip().startswith("diff --git "):
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", chunk, re.MULTILINE)
        if not m:
            m = re.search(r"diff --git a/.+ b/(.+)$", chunk, re.MULTILINE)
        path = m.group(1).strip() if m else "<unknown>"
        pairs.append((path, chunk))
    return pairs


def added_code_lines(chunk: str) -> int:
    """Count added lines that carry real code (non-blank, non-comment-only).

    Used by the recall size guard. Mirrors the harvest heuristic used to size the
    validation diffs; approximate by design (a cheap proxy for diff weight).
    """
    n = 0
    for ln in chunk.splitlines():
        if ln.startswith("+++") or not ln.startswith("+"):
            continue
        body = ln[1:].strip()
        if not body:
            continue
        if body.startswith(("//", "#", "*", "/*", '"""', "'''")):
            continue
        n += 1
    return n


def lang_of(path: str) -> str:
    if path.endswith(".py"):
        return "Python"
    if path.endswith((".ts", ".tsx")):
        return "TypeScript"
    if path.endswith((".js", ".jsx", ".mjs", ".cjs")):
        return "JavaScript"
    return "code"


def is_flagged(review: str) -> bool:
    """Return True if the review flags an issue.

    Clean iff the LAST non-empty line contains the sentinel — reasoning-off output
    can deliberate in the body and conclude with the verdict, so the FINAL line is
    authoritative (a full-text scan would false-negative on a mid-text mention).
    Empty content must NOT reach here as "clean" — callers treat empty as an error.
    """
    lines = [ln for ln in review.strip().splitlines() if ln.strip()]
    if not lines:
        return True  # defensive: empty is handled as an error upstream
    return CLEAN_SENTINEL not in lines[-1].upper()


# ── Endpoint / model resolution (review-specific first; warns on the :8080 footgun) ─
def resolve_base_url() -> str:
    base = (os.environ.get("LLAMA_CPP_REVIEW_BASE_URL", "").strip()
            or os.environ.get("LLAMA_CPP_BASE_URL", "").strip())
    if base:
        return base.rstrip("/")
    port = os.environ.get("LLAMA_CPP_PORT", "8080").strip() or "8080"
    return f"http://127.0.0.1:{port}/v1"


def resolve_model() -> str:
    return (os.environ.get("LLAMA_CPP_REVIEW_MODEL", "").strip()
            or os.environ.get("LLAMA_CPP_MODEL", "").strip())


def review_endpoint_explicit(cli_base_url: str | None) -> bool:
    """True when the user explicitly chose a reviewer endpoint (not the shared default)."""
    return bool(cli_base_url) or bool(os.environ.get("LLAMA_CPP_REVIEW_BASE_URL", "").strip())


def fetch_loaded_model(base_url: str, timeout: float) -> str | None:
    """Best-effort: ask the server which model it loaded so we can print it loudly.

    Never fatal — a missing /models endpoint just yields an '(unknown)' label.
    """
    try:
        resp = httpx.get(f"{base_url}/models", timeout=timeout)
        if resp.status_code == 200:
            data = (resp.json() or {}).get("data") or []
            if data and isinstance(data[0], dict):
                return data[0].get("id")
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        return None
    return None


# ── Model call (OpenAI-compatible /chat/completions) ────────────────────────────
@dataclass
class CallResult:
    ok: bool
    content: str = ""
    reasoning: str = ""
    finish_reason: str = ""
    gen_tokens: int | None = None
    gen_tps: float | None = None
    wall_s: float = 0.0
    error: str = ""


def call_model(base_url: str, model: str, system: str, user: str, timeout: float) -> CallResult:
    endpoint = f"{base_url}/chat/completions"
    payload: dict = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": TEMPERATURE,
        "top_p": TOP_P,
        "seed": SEED,
        "max_tokens": MAX_TOKENS,
        "stream": False,
        # llama-server reuses the identical system prefix across per-file calls.
        "cache_prompt": True,
        # NOTE: no request-body thinking key. The validated config disables gemma's
        # thinking mode at SERVE time (reasoning-off launch flag); the request-body
        # mechanisms differ across runtimes and were not validated. We fail loud on
        # empty content instead of guessing a key here.
    }
    if model:
        payload["model"] = model
    t0 = time.time()
    try:
        resp = httpx.post(endpoint, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        return CallResult(False, error=f"connection error to {endpoint}: {exc}")
    wall = round(time.time() - t0, 2)
    if resp.status_code != 200:
        return CallResult(False, wall_s=wall,
                          error=f"HTTP {resp.status_code} from {endpoint}: {resp.text[:200]}")
    try:
        body = resp.json()
        choice = body["choices"][0]
        msg = choice["message"]
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        finish = choice.get("finish_reason") or ""
        timings = body.get("timings") or {}
        usage = body.get("usage") or {}
        gen_tokens = timings.get("predicted_n") or usage.get("completion_tokens")
        gtps = timings.get("predicted_per_second")
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return CallResult(False, wall_s=wall, error=f"unexpected response shape: {exc}")
    return CallResult(
        True, content=content, reasoning=reasoning, finish_reason=finish,
        gen_tokens=gen_tokens, gen_tps=round(gtps, 1) if gtps else None, wall_s=wall,
    )


# ── Orchestration ───────────────────────────────────────────────────────────────
@dataclass
class ReviewConfig:
    base_url: str
    model: str
    ctx: int = DEFAULT_CTX
    max_files: int = 20
    max_diff_loc: int = DEFAULT_MAX_DIFF_LOC
    skip_large: bool = False
    timeout: float = 180.0


def review(diff: str, cfg: ReviewConfig) -> dict:
    overhead = (_est_tokens(SYSTEM) + _est_tokens(USER_CALIBRATED_DIFF)
                + SYSTEM_SAFETY_TOKENS)
    per_file_budget = cfg.ctx - overhead - OUTPUT_RESERVE_TOKENS
    if per_file_budget < 256:
        raise ReviewError(
            USAGE_ERROR,
            f"prompt overhead (~{overhead} tok) leaves only {per_file_budget} tok "
            f"for the diff under ctx={cfg.ctx}. Raise --ctx.",
        )

    files = split_by_file(diff)
    dropped_max: list[str] = []
    if cfg.max_files and len(files) > cfg.max_files:
        dropped_max = [p for p, _ in files[cfg.max_files:]]
        files = files[:cfg.max_files]
        sys.stderr.write(
            f"[cross-family-review] --max-files={cfg.max_files}: skipped "
            f"{len(dropped_max)} file(s): {', '.join(dropped_max)}\n"
        )

    budget_chars = per_file_budget * CHARS_PER_TOKEN
    results: list[dict] = []
    truncated: list[str] = []
    skipped_large: list[dict] = []

    for path, chunk in files:
        loc = added_code_lines(chunk)
        oversize = loc > cfg.max_diff_loc
        if oversize and cfg.skip_large:
            skipped_large.append({"file": path, "added_loc": loc})
            results.append({
                "file": path, "lang": lang_of(path), "added_loc": loc,
                "oversize": True, "skipped": True, "flagged": None,
                "review": "", "reasoning": "",
            })
            sys.stderr.write(
                f"[cross-family-review] --skip-large: skipped {path} "
                f"({loc} added LOC > {cfg.max_diff_loc}).\n"
            )
            continue

        lang = lang_of(path)
        code = chunk
        was_truncated = False
        if _est_tokens(code) > per_file_budget:
            clipped = code[:budget_chars]
            nl = clipped.rfind("\n")
            if nl > 0:
                clipped = clipped[:nl]
            code = clipped + "\n[... diff truncated to fit context ...]\n"
            was_truncated = True
            truncated.append(path)
            sys.stderr.write(f"[cross-family-review] truncated diff for {path} to fit ctx.\n")

        user = USER_CALIBRATED_DIFF.format(lang=lang, code=code)
        r = call_model(cfg.base_url, cfg.model, SYSTEM, user, cfg.timeout)
        if not r.ok:
            raise ReviewError(
                INTERNAL_ERROR,
                f"{path}: {r.error}\n"
                "Is a llama-server with a 12B-class reviewer reachable at the "
                "configured endpoint? (See this script's module docstring for serving.)",
            )
        if not r.content:
            raise ReviewError(
                INTERNAL_ERROR,
                f"{path}: model returned EMPTY content (finish_reason={r.finish_reason!r}). "
                "This is the gemma thinking-trap — serve llama-server with reasoning "
                "DISABLED. Empty content is never treated as 'clean'.",
            )

        results.append({
            "file": path, "lang": lang, "added_loc": loc,
            "oversize": oversize, "skipped": False,
            "flagged": is_flagged(r.content),
            "truncated": was_truncated,
            # Non-empty content that stopped on the token limit is a PARTIAL review
            # (the verdict line may be missing) — surface it so the human doesn't
            # act on an incomplete advisory. Empty content already failed loud above.
            "output_truncated": r.finish_reason == "length",
            "review": r.content, "reasoning": r.reasoning,
            "finish_reason": r.finish_reason, "gen_tokens": r.gen_tokens,
            "gen_tps": r.gen_tps, "wall_s": r.wall_s,
        })

    reviewed = [r for r in results if not r.get("skipped")]
    flagged = [r for r in reviewed if r.get("flagged")]
    return {
        "results": results,
        "meta": {
            "model": cfg.model or "(loaded)",
            # loaded_model / footgun_default_8080 describe the ENDPOINT, not the
            # review, so main() injects them after the call — that keeps review()
            # decoupled from server probing and CLI-arg parsing.
            "loaded_model": None,
            "base_url": cfg.base_url,
            "footgun_default_8080": False,
            "files_reviewed": len(reviewed),
            "files_flagged": len(flagged),
            "files_skipped_large": skipped_large,
            "files_dropped_max": dropped_max,
            "files_truncated": truncated,
            "max_diff_loc": cfg.max_diff_loc,
            "ctx": cfg.ctx,
        },
    }


# ── Human-readable rendering (the default; --json is the agent-native path) ──────
BANNER = (
    "═══ ADVISORY: local cross-family second opinion — NOT a gate ═══\n"
    "Validation (LIA-179, n=35): ~29% of real merged diffs get a FALSE flag, "
    "including confident PHANTOM CRASHES. Recall is validated only on SMALL diffs. "
    "A human must triage every flag; never auto-apply, auto-post, or gate on this."
)


def _footgun_warning(loaded_model: str | None, base_url: str) -> str:
    return (
        f"⚠ ENDPOINT WARNING: resolved to {base_url} with no reviewer-specific config.\n"
        f"  Loaded model: {loaded_model or '(unknown)'}. :8080 is typically the deployed "
        "Deus service running a SMALL model that was NEVER validated for code review.\n"
        "  Set LLAMA_CPP_REVIEW_BASE_URL (and/or LLAMA_CPP_REVIEW_MODEL) to a "
        "llama-server running a 12B-class code model with reasoning disabled."
    )


def render_human(result: dict) -> None:
    meta = result["meta"]
    print(BANNER)
    print(f"\nEndpoint: {meta['base_url']}  |  loaded model: "
          f"{meta.get('loaded_model') or '(unknown)'}")
    if meta.get("footgun_default_8080"):
        print("\n" + _footgun_warning(meta.get("loaded_model"), meta["base_url"]))

    for r in result["results"]:
        print("\n" + "─" * 72)
        if r.get("skipped"):
            print(f"## {r['file']}  — SKIPPED (large: {r['added_loc']} added LOC > "
                  f"{meta['max_diff_loc']} threshold; recall unreliable)")
            continue
        tag = "FLAGGED" if r.get("flagged") else "NO ISSUES"
        extra = ", TRUNCATED" if r.get("truncated") else ""
        print(f"## {r['file']}  [{tag}]  ({r['lang']}, {r['added_loc']} added LOC{extra})")
        if r.get("oversize"):
            print(f"  ⚠ large file: {r['added_loc']} added LOC > {meta['max_diff_loc']} — "
                  "recall is unreliable on large diffs; a MISS here is expected, not safety.")
        if r.get("output_truncated"):
            print("  ⚠ output hit the generation token cap — this review is PARTIAL; the "
                  "verdict line may be missing. Treat it as inconclusive.")
        if r.get("reasoning"):
            print(f"\n[reasoning]\n{r['reasoning']}")
        print(f"\n{r['review']}")

    print("\n" + "═" * 72)
    summary = (f"Summary: {meta['files_reviewed']} reviewed, "
               f"{meta['files_flagged']} flagged")
    if meta["files_skipped_large"]:
        summary += f", {len(meta['files_skipped_large'])} skipped-large"
    if meta.get("files_dropped_max"):
        summary += f", {len(meta['files_dropped_max'])} dropped (--max-files cap)"
    print(summary)
    if meta.get("files_dropped_max"):
        print("Dropped (NOT reviewed): " + ", ".join(meta["files_dropped_max"]))
    flagged_files = [r["file"] for r in result["results"] if r.get("flagged")]
    if flagged_files:
        print("Flagged: " + ", ".join(flagged_files))
    print("Reminder: these are UNVERIFIED candidates. ~29% are false (incl. phantom "
          "crashes) — read the cited line yourself before acting. Not a gate.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Local cross-family code-review ADVISORY (Gemma 4 12B, never a gate).",
    )
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range (default: working tree)")
    src.add_argument("--diff-file", help="path to a unified diff file to review")
    ap.add_argument("--out", help="also write the full JSON result to this file")
    ap.add_argument("--base-url", help="override the reviewer base URL")
    ap.add_argument("--model", help="override the reviewer model name")
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                    help=f"context budget (default {DEFAULT_CTX}); serve KV must be >= this")
    ap.add_argument("--max-files", type=int, default=20, help="cap files reviewed (0 = no cap)")
    ap.add_argument("--max-diff-loc", type=int, default=DEFAULT_MAX_DIFF_LOC,
                    help=f"per-file added-LOC warn threshold (heuristic, default {DEFAULT_MAX_DIFF_LOC})")
    ap.add_argument("--skip-large", action="store_true",
                    help="skip (don't review) files over --max-diff-loc instead of warning")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-call timeout seconds")
    ap.add_argument("--json", action="store_true", help="emit JSON (agent-native)")
    ap.add_argument("--compact", action="store_true",
                    help="compact JSON (strip nulls, truncate long fields)")
    ap.add_argument("--select", help="comma-separated dot-paths to project from the JSON")
    args = ap.parse_args(argv)

    try:
        root = repo_root()
        diff = get_diff(root, args.rev_range, args.diff_file)
        if not diff.strip():
            sys.stderr.write("[cross-family-review] empty diff — nothing to review.\n")
            return ABSTAIN

        base_url = (args.base_url or resolve_base_url()).rstrip("/")
        model = args.model or resolve_model()
        loaded_model = fetch_loaded_model(base_url, timeout=min(args.timeout, 15.0))
        footgun = (":8080" in base_url) and not review_endpoint_explicit(args.base_url)

        cfg = ReviewConfig(
            base_url=base_url, model=model, ctx=args.ctx, max_files=args.max_files,
            max_diff_loc=args.max_diff_loc, skip_large=args.skip_large, timeout=args.timeout,
        )
        result = review(diff, cfg)
        result["meta"]["loaded_model"] = loaded_model
        result["meta"]["footgun_default_8080"] = footgun

        use_json = args.json or is_agent_context()
        out = agent_output(
            result, use_json=use_json, compact=args.compact, select=args.select,
            long_fields=("review", "reasoning"),
        )
        if out is not None:
            print(out)
        else:
            render_human(result)

        if args.out:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(result, indent=2))
        return SUCCESS

    except ReviewError as exc:
        sys.stderr.write(f"[cross-family-review] {exc.message}\n")
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())

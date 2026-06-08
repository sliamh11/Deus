#!/usr/bin/env python3
"""Cross-family local code reviewer — candidate generator (measure-first increment 1).

A *decorrelated second opinion* for the Claude code-reviewer warden. Runs a LOCAL
model (different family => independent blind spots) over a diff and proposes
CANDIDATE findings. It is a recall-oriented surfacer, NOT an adjudicator: the
Claude code-reviewer remains the precision layer that validates each candidate
against the full rules + diff and renders the SHIP/REVISE/BLOCK verdict.

Status: increment 1 is the MEASURABLE CORE only. It emits candidates to stdout so
their catch rate / decorrelation value can be measured on real diffs BEFORE any
integration. Wiring candidates into the code-reviewer prompt, an auto-trigger, a
blast-radius classifier, and accept/reject logging are DEFERRED.

VALIDATION RESULT (2026-06-04) — PARKED, not yet wired in. Measured on 6 real
blast-radius commits with two models: general Qwen3.5-9B and code-specialized
Qwen2.5-Coder-7B. Neither is adjudication-ready at 7-9B / 8k KV / diff-only: real
finds exist (~15-25%) but are buried in noise, and the highest-confidence flags
include confident FALSE POSITIVES (e.g. "XML injection" against already-escaped code;
a rule mis-applied as blocking to every doc edit). Two cross-model root causes: the
runtime rule digest is net-negative (both models mis-apply rule ids), and diff-only
context yields unverifiable claims. This harness is retained for future iteration
(drop the digest, send full-file context, or try a larger code model, then re-measure).

Design notes:
- Local model = Qwen3.5-9B Q4_K_M via llama-server's OpenAI-compatible endpoint,
  temp=0, 8k KV (the user-specified budget). No egress: the model is local, so this
  reviews public AND private code on a single path.
- 8k KV is tight. The full code-review-rules.md is ~4.3k tokens — too large to ship
  to the local model alongside a diff. Instead we derive a CONDENSED rule digest
  (rule id + severity + "Applies when" trigger) at runtime from the same file, so
  the local model speaks the repo's rule vocabulary without the token cost and
  without duplicating rule text (the full rules stay with the Claude adjudicator).
- Per-file chunking: one model call per changed file. If a single file's diff still
  overflows the budget we truncate the DIFF ONLY (never the rules/system prompt) and
  log it; files dropped under --max-files are logged too. No silent caps.
- Fail-loud: if llama-server is unreachable we exit non-zero — we never fabricate
  candidates from a failed call.

Usage:
    # Review the current working-tree diff (staged + unstaged):
    python3 scripts/cross_family_review.py

    # Review a historical commit (for the validation pass):
    python3 scripts/cross_family_review.py --rev-range <sha>

    # Review an arbitrary diff file:
    python3 scripts/cross_family_review.py --diff-file /tmp/some.diff --out /tmp/cands.json

Environment (mirrors the per-surface convention in evolution/config.py; no import
coupling — defaults are replicated inline so a config.py change can't silently move
this script's endpoint):
    LLAMA_CPP_BASE_URL     OpenAI-compatible base, e.g. http://127.0.0.1:8080/v1
    LLAMA_CPP_PORT         used to build the base URL when LLAMA_CPP_BASE_URL is unset
    LLAMA_CPP_REVIEW_MODEL model name for this surface; falls back to LLAMA_CPP_MODEL
    LLAMA_CPP_MODEL        catch-all model; empty => llama-server uses its loaded model
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

try:
    import httpx
except ImportError:  # pragma: no cover - actionable guard for fresh clones
    sys.stderr.write(
        "[cross-family-review] missing dependency 'httpx'. Install it:\n"
        "    python3 -m pip install httpx\n"
    )
    sys.exit(2)

# ── Token budgeting (rough; char/4 heuristic with margin — we don't have the
# model's tokenizer in-process, so we stay conservative). ──────────────────────
CHARS_PER_TOKEN = 4
DEFAULT_CTX = 8192          # 8k KV — the user-specified budget
OUTPUT_RESERVE_TOKENS = 1024  # headroom for the model's JSON answer
SYSTEM_SAFETY_TOKENS = 256    # slack so we never exactly hit the wall


def _est_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ── Repo / rules loading (CWD-independent) ─────────────────────────────────────
def repo_root() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True, capture_output=True, text=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        sys.stderr.write(f"[cross-family-review] not a git repo / git missing: {exc}\n")
        sys.exit(2)


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def load_standards(root: str) -> str:
    """The adversarial/evidence-bound mindset (small — ~900 tok). Sent verbatim."""
    return _read(os.path.join(root, ".claude", "wardens", "standards.md")).strip()


def derive_rule_digest(root: str) -> str:
    """Condense code-review-rules.md to `id — severity — Applies when` lines.

    Runtime-derived (not a copy) so it tracks the source file and stays small. The
    full Check/Rule text is intentionally omitted: that detail is the Claude
    adjudicator's job; the local model only needs the rule vocabulary + triggers.
    """
    text = _read(os.path.join(root, ".claude", "wardens", "code-review-rules.md"))
    if not text:
        return ""
    lines = text.splitlines()
    entries: list[str] = []
    rule_id = severity = applies = ""

    def flush() -> None:
        nonlocal rule_id, severity, applies
        if rule_id:
            sev = f" [{severity}]" if severity else ""
            trig = f" — {applies}" if applies else ""
            entries.append(f"- {rule_id}{sev}{trig}")
        rule_id = severity = applies = ""

    for line in lines:
        if line.startswith("## "):
            flush()
            rule_id = line[3:].strip()
        elif line.startswith("**Severity:**"):
            severity = line.split("**Severity:**", 1)[1].strip()
        elif line.startswith("**Applies when:**"):
            applies = line.split("**Applies when:**", 1)[1].strip()
    flush()
    return "\n".join(entries)


# ── Diff acquisition + per-file splitting ──────────────────────────────────────
def get_diff(root: str, rev_range: str | None, diff_file: str | None) -> str:
    # Fail-loud everywhere: a failed diff acquisition must NOT masquerade as an
    # empty tree (which main() would treat as "nothing to review" and exit 0).
    if diff_file:
        if not os.path.exists(diff_file):
            sys.stderr.write(f"[cross-family-review] --diff-file not found: {diff_file}\n")
            sys.exit(2)
        return _read(diff_file)
    if rev_range:
        # Single sha => that commit vs its first parent; ranges (a..b) pass through.
        spec = [rev_range] if (".." in rev_range) else [f"{rev_range}^!"]
        out = subprocess.run(
            ["git", "-C", root, "diff", *spec],
            capture_output=True, text=True,
        )
        if out.returncode != 0:
            sys.stderr.write(
                f"[cross-family-review] git diff failed for '{rev_range}': {out.stderr.strip()}\n"
            )
            sys.exit(2)
        return out.stdout
    # Default: working-tree review = staged + unstaged, against HEAD.
    out = subprocess.run(
        ["git", "-C", root, "diff", "HEAD"],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        sys.stderr.write(f"[cross-family-review] git diff HEAD failed: {out.stderr.strip()}\n")
        sys.exit(2)
    return out.stdout


def split_by_file(diff: str) -> list[tuple[str, str]]:
    """Split a unified diff into (path, chunk) pairs on `diff --git` boundaries."""
    if not diff.strip():
        return []
    chunks = re.split(r"(?m)^(?=diff --git )", diff)
    pairs: list[tuple[str, str]] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        m = re.search(r"^\+\+\+ b/(.+)$", chunk, re.MULTILINE)
        if not m:
            m = re.search(r"diff --git a/.+ b/(.+)$", chunk, re.MULTILINE)
        path = m.group(1).strip() if m else "<unknown>"
        pairs.append((path, chunk))
    return pairs


# ── Prompt construction ────────────────────────────────────────────────────────
SYSTEM_TMPL = """You are a LOCAL, decorrelated second-opinion code reviewer. A separate, \
more powerful reviewer will independently validate every candidate you produce and \
render the final verdict — so your job is RECALL: surface real, specific candidate \
defects another reviewer might miss. You are not the final word.

Hard rules:
- EVIDENCE-BOUND: every candidate must cite an exact file path and line number from \
the diff. No line number => do not emit it. Never invent code that is not in the diff.
- Hunt for REAL defects: logic bugs, security boundary issues, concurrency/async races, \
data loss/overwrite, resource leaks, error-path gaps, broken cross-platform assumptions. \
"Looks fine" is a hypothesis to disprove — but an unsupported flag is noise and will be \
dismissed by the validator. When unsure, still surface it, but say why it's uncertain.
- Prefer the repo's rule vocabulary below when a candidate matches one (set rule_hint to \
the rule id); use a short free-text category otherwise.

Review standards (mindset):
{standards}

Repo rule vocabulary (id — severity — applies when):
{digest}

OUTPUT: a single JSON object, no prose, no markdown fences:
{{"candidates": [{{"file": "path", "line": 123, "rule_hint": "rule-id-or-category", \
"severity_guess": "blocking|warning|info", "claim": "one-sentence defect", \
"why": "brief evidence/uncertainty"}}]}}
If you find nothing real in this file, return {{"candidates": []}}."""


def build_system(standards: str, digest: str) -> str:
    return SYSTEM_TMPL.format(standards=standards or "(standards unavailable)",
                              digest=digest or "(rule digest unavailable)")


# ── Model call (OpenAI-compatible /chat/completions) ───────────────────────────
def resolve_base_url() -> str:
    base = os.environ.get("LLAMA_CPP_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    port = os.environ.get("LLAMA_CPP_PORT", "8080").strip() or "8080"
    return f"http://127.0.0.1:{port}/v1"


def resolve_model() -> str:
    return (os.environ.get("LLAMA_CPP_REVIEW_MODEL", "").strip()
            or os.environ.get("LLAMA_CPP_MODEL", "").strip())


@dataclass
class CallResult:
    ok: bool
    text: str
    error: str = ""


def call_model(base_url: str, model: str, system: str, user: str, timeout: float) -> CallResult:
    endpoint = f"{base_url}/chat/completions"
    payload: dict = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "top_p": 1,
        "stream": False,
        "max_tokens": OUTPUT_RESERVE_TOKENS,  # bound output: candidates are short; never run unbounded
        "cache_prompt": True,  # llama-server reuses the identical system prefix across per-file calls
        # Qwen3.5 is a reasoning model; its chain-of-thought eats the token budget and
        # truncates the JSON. Disable it — we want direct candidate extraction, not CoT.
        "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_object"},
    }
    if model:
        payload["model"] = model
    try:
        resp = httpx.post(endpoint, json=payload, timeout=timeout)
    except httpx.HTTPError as exc:
        return CallResult(False, "", f"connection error to {endpoint}: {exc}")
    if resp.status_code != 200:
        return CallResult(False, "", f"HTTP {resp.status_code} from {endpoint}: {resp.text[:200]}")
    try:
        content = resp.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as exc:
        return CallResult(False, "", f"unexpected response shape: {exc}")
    return CallResult(True, content)


# ── Robust JSON extraction (a 9B at temp=0 may still fence/preamble) ────────────
def extract_candidates(text: str) -> list[dict]:
    if not text or not text.strip():
        return []
    candidates: list | None = None
    # 1) direct object/array parse
    for blob in (text, _strip_fences(text)):
        try:
            data = json.loads(blob)
            candidates = data.get("candidates") if isinstance(data, dict) else data
            if candidates is not None:
                break
        except (ValueError, AttributeError):
            continue
    # 2) regex-rescue the first {...candidates...} object or [...] array
    if candidates is None:
        m = re.search(r'\{[^{}]*"candidates"\s*:\s*(\[.*?\])[^{}]*\}', text, re.DOTALL)
        if m:
            try:
                candidates = json.loads(m.group(1))
            except ValueError:
                candidates = None
    if candidates is None:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if m:
            try:
                candidates = json.loads(m.group(0))
            except ValueError:
                candidates = None
    if not isinstance(candidates, list):
        return []
    return [c for c in candidates if isinstance(c, dict)]


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


# ── Orchestration ──────────────────────────────────────────────────────────────
def review(root: str, diff: str, *, base_url: str, model: str, ctx: int,
           max_files: int, timeout: float) -> dict:
    standards = load_standards(root)
    digest = derive_rule_digest(root)
    system = build_system(standards, digest)

    system_tokens = _est_tokens(system) + SYSTEM_SAFETY_TOKENS
    per_file_budget = ctx - system_tokens - OUTPUT_RESERVE_TOKENS
    if per_file_budget < 256:
        sys.stderr.write(
            f"[cross-family-review] rules+mindset (~{system_tokens} tok) leave only "
            f"{per_file_budget} tok for the diff under ctx={ctx}. Raise ctx or trim rules.\n"
        )
        sys.exit(2)

    files = split_by_file(diff)
    dropped: list[str] = []
    if max_files and len(files) > max_files:
        dropped = [p for p, _ in files[max_files:]]
        files = files[:max_files]
        sys.stderr.write(
            f"[cross-family-review] --max-files={max_files}: skipped {len(dropped)} "
            f"file(s): {', '.join(dropped)}\n"
        )

    all_candidates: list[dict] = []
    truncated: list[str] = []
    failures: list[str] = []
    budget_chars = per_file_budget * CHARS_PER_TOKEN

    for path, chunk in files:
        user = chunk
        if _est_tokens(user) > per_file_budget:
            # Truncate the DIFF ONLY — never the rules/system prompt. Cut on a line
            # boundary so the model never receives a broken partial diff line.
            # NOTE: _est_tokens is a chars/4 heuristic; multi-byte-heavy files may
            # under-count, so this bound is approximate, not exact.
            clipped = chunk[:budget_chars]
            nl = clipped.rfind("\n")
            if nl > 0:
                clipped = clipped[:nl]
            user = clipped + "\n[... diff truncated to fit context ...]\n"
            truncated.append(path)
            sys.stderr.write(f"[cross-family-review] truncated diff for {path} to fit ctx.\n")
        result = call_model(base_url, model, system, user, timeout)
        if not result.ok:
            # Connection-class failure => fail loud (the server is the dependency).
            if "connection error" in result.error or "HTTP" in result.error:
                sys.stderr.write(f"[cross-family-review] FATAL: {result.error}\n")
                sys.stderr.write(
                    "[cross-family-review] is llama-server running? See /add-llama-cpp.\n"
                )
                sys.exit(3)
            failures.append(f"{path}: {result.error}")
            continue
        cands = extract_candidates(result.text)
        if not cands and result.text.strip() not in ('{"candidates": []}', '{"candidates":[]}'):
            failures.append(f"{path}: unparseable model output")
        for c in cands:
            c.setdefault("file", path)
            # Coerce a string-typed line ("42") to int — the schema example says int,
            # so downstream consumers can rely on the type.
            if not isinstance(c.get("line"), int):
                try:
                    c["line"] = int(str(c.get("line")).strip())
                except (ValueError, TypeError):
                    pass
        all_candidates.extend(cands)

    return {
        "candidates": all_candidates,
        "meta": {
            "files_reviewed": len(files),
            "files_dropped": dropped,
            "files_truncated": truncated,
            "parse_failures": failures,
            "model": model or "(loaded)",
            "ctx": ctx,
            "per_file_budget_tokens": per_file_budget,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Local cross-family code-review candidate generator.")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--rev-range", help="commit sha or a..b range to review (default: working tree)")
    src.add_argument("--diff-file", help="path to a unified diff file to review")
    ap.add_argument("--out", help="write the JSON result here (also printed to stdout)")
    ap.add_argument("--base-url", help="override LLAMA_CPP_BASE_URL")
    ap.add_argument("--model", help="override LLAMA_CPP_REVIEW_MODEL")
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX, help=f"context budget (default {DEFAULT_CTX})")
    ap.add_argument("--max-files", type=int, default=20, help="cap files reviewed (0 = no cap)")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-call timeout seconds")
    args = ap.parse_args()

    root = repo_root()
    diff = get_diff(root, args.rev_range, args.diff_file)
    if not diff.strip():
        sys.stderr.write("[cross-family-review] empty diff — nothing to review.\n")
        print(json.dumps({"candidates": [], "meta": {"files_reviewed": 0}}, indent=2))
        return 0

    base_url = (args.base_url or resolve_base_url()).rstrip("/")
    model = args.model or resolve_model()

    result = review(
        root, diff,
        base_url=base_url, model=model, ctx=args.ctx,
        max_files=args.max_files, timeout=args.timeout,
    )
    payload = json.dumps(result, indent=2)
    print(payload)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

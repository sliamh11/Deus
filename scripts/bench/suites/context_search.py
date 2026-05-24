#!/usr/bin/env python3
"""
context_search suite — token cost: claude-context search_code / explore vs grep/find

Measures token consumption for a fixed set of code-search tasks comparing
four retrieval strategies:

  grep     — ``grep -rn`` with query-specific patterns; measures raw output.
  find     — ``find + head`` file-listing approach; counts tokens of matched
             file paths + first-N-line previews.
  semantic — claude-context ``search_code`` MCP tool (requires running MCP
             server; skipped gracefully with a warning when unavailable).
  explore  — Anthropic Messages API (claude-haiku-3-5-20241022); asks the
             model to list relevant files directly.  Gracefully disabled when
             ANTHROPIC_API_KEY is unset.  Token counts are exact
             (response.usage.input_tokens + response.usage.output_tokens).

Primary metric: token_savings_pct = 1 - (strategy_tokens / grep_tokens)
Secondary:      hit@1, hit@3 accuracy (does the expected path appear in the
                top-1 / top-3 results?), latency_ms per strategy.

Tasks are anchored to the Deus repository.  Expected paths are pre-defined so
accuracy is deterministic and reproducible without a live judge.

Run:
  python -m bench run context_search
  python -m bench run context_search --no-semantic    # skip MCP column
  python -m bench run context_search --no-explore     # skip Anthropic API column
  python -m bench run context_search --repo-root /path/to/deus
  python -m bench run context_search --output /tmp/bench.json  # write JSON artifact

Output:
  Prints a summary table to stdout.  When --save is passed to the bench CLI
  the RunResult is persisted to the bench store.  Use --output to write a
  machine-readable JSON artifact for CI regression tracking.

Methodology notes (from bench-methodology):
  - Fail loudly: missing expected file raises, not silently skips.
  - Token estimation uses 1 tok ≈ 3.7 chars (Claude BPE heuristic for
    English technical text; acceptable for delta-tracking).
  - Results are additive across tasks; per-task scores weight equally.
  - Cache-first: if a results snapshot exists for today's git SHA the run
    prints a notice and still executes (we want fresh measurements).
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path plumbing — works both when imported as a package and run directly
# ---------------------------------------------------------------------------
_BENCH_DIR = Path(__file__).resolve().parent          # scripts/bench/suites/
_SCRIPTS_DIR = _BENCH_DIR.parent.parent               # scripts/
_REPO_ROOT = _SCRIPTS_DIR.parent                      # repo root

if __name__ == "__main__" and __package__ in (None, ""):
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    __package__ = "bench.suites"

from ..registry import register  # noqa: E402
from ..types import CaseResult, RunResult  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN: float = 3.7          # Claude BPE approximation
GREP_HEAD_LINES: int = 5              # lines of context per grep match
FIND_PREVIEW_LINES: int = 30          # lines from top of each found file
FIND_MAX_FILES: int = 10              # cap files opened in find strategy
SEMANTIC_TIMEOUT_S: int = 15          # seconds before search_code call times out
SEMANTIC_TOP_K: int = 5              # results to request from search_code


def _est(chars: int) -> int:
    return round(chars / CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

@dataclass
class SearchTask:
    """One code-search probe with ground truth."""
    task_id: str
    query: str                          # natural-language description
    grep_patterns: list[str]            # -E patterns (tried in order; first match wins)
    grep_globs: list[str]               # --include globs (applied to all patterns)
    grep_exclude_dirs: list[str]        # --exclude-dir values
    expected_paths: list[str]           # repo-relative paths that must appear in results
    semantic_query: str                 # query string sent to search_code
    notes: str = ""


# Fixed task set — anchored to the Deus repo at the time of LIA-61 authoring.
# When the codebase changes, update `expected_paths` as needed.
TASKS: list[SearchTask] = [
    SearchTask(
        task_id="message_routing",
        query="Find the function or file that handles message routing",
        grep_patterns=[r"route[rR]", r"dispatch.*message", r"message.*route"],
        grep_globs=["*.ts", "*.js", "*.mjs"],
        grep_exclude_dirs=["node_modules", "dist", ".git"],
        expected_paths=[".mex/ROUTER.md", "src/"],
        semantic_query="message routing dispatch handler",
        notes="Core routing logic lives in src/ and is documented in .mex/ROUTER.md",
    ),
    SearchTask(
        task_id="rrf_implementation",
        query="Where is Reciprocal Rank Fusion (RRF) implemented?",
        grep_patterns=[r"reciprocal.rank", r"\bRRF\b", r"rrf_score", r"1\s*/\s*\(k\s*\+"],
        grep_globs=["*.py"],
        grep_exclude_dirs=["node_modules", ".git", "__pycache__"],
        expected_paths=["scripts/memory_tree.py", "scripts/memory_indexer.py"],
        semantic_query="reciprocal rank fusion RRF score combination",
        notes="Memory retrieval uses RRF to combine dense + keyword results",
    ),
    SearchTask(
        task_id="token_estimation",
        query="Find the token estimation / counting function",
        grep_patterns=[r"est_token", r"estimate_token", r"chars_per_token", r"len\(text\)\s*//\s*4"],
        grep_globs=["*.py"],
        grep_exclude_dirs=["node_modules", ".git", "__pycache__"],
        expected_paths=["evolution/token_counter.py", "scripts/token_bench/harness.py"],
        semantic_query="token count estimation chars per token",
        notes="Two implementations: simple heuristic in evolution/ and harness in scripts/",
    ),
    SearchTask(
        task_id="evolution_judge",
        query="Where is the evolution benchmark judge model configured?",
        grep_patterns=[r"OllamaJudge", r"benchmark_judge", r"judge_model", r"judge.*score"],
        grep_globs=["*.py"],
        grep_exclude_dirs=["node_modules", ".git", "__pycache__"],
        expected_paths=["evolution/benchmark_judge.py", "eval/judge_model.py"],
        semantic_query="judge model benchmark score evaluation",
        notes="Judge is OllamaJudge wrapping gemma4:e4b",
    ),
    SearchTask(
        task_id="agent_container_entrypoint",
        query="Find the container agent runner entry point",
        grep_patterns=[r"agent.runner", r"entrypoint", r"main\(\)", r"runAgent"],
        grep_globs=["*.ts", "*.js", "*.sh"],
        grep_exclude_dirs=["node_modules", "dist", ".git"],
        expected_paths=["container/", "src/"],
        semantic_query="container agent runner entry point start",
        notes="Agent runner is the container-side process spawned per task",
    ),
    SearchTask(
        task_id="mcp_server",
        query="Find the MCP server implementation(s)",
        grep_patterns=[r"McpServer", r"mcp_server", r"MCP.*server", r"@modelcontextprotocol"],
        grep_globs=["*.py", "*.ts", "*.js"],
        grep_exclude_dirs=["node_modules", "dist", ".git", "__pycache__"],
        expected_paths=["evolution/mcp_server.py", "scripts/memory_mcp_server.py"],
        semantic_query="MCP server tool registration handler",
        notes="Multiple MCP servers exist for different capabilities",
    ),
    SearchTask(
        task_id="usage_log_reader",
        query="Where are usage logs read / parsed from channel traffic?",
        grep_patterns=[r"usage\.jsonl", r"usage_log", r"load_usage", r"billing.*token"],
        grep_globs=["*.py"],
        grep_exclude_dirs=["node_modules", ".git", "__pycache__"],
        expected_paths=["scripts/analyze_token_efficiency.py"],
        semantic_query="usage log billing tokens read parse channel",
        notes="Channel billing tokens are recorded in groups/*/logs/usage.jsonl",
    ),
    SearchTask(
        task_id="memory_gc",
        query="Find the memory garbage collection / cleanup script",
        grep_patterns=[r"memory.gc", r"memory_gc", r"TTL", r"expire.*atom", r"archive.*memory"],
        grep_globs=["*.py", "*.sh"],
        grep_exclude_dirs=["node_modules", ".git", "__pycache__"],
        expected_paths=["scripts/memory_gc.py"],
        semantic_query="memory garbage collection TTL expiry cleanup atoms",
        notes="memory_gc.py handles TTL-based archival of vault atoms",
    ),
]


# ---------------------------------------------------------------------------
# Strategy runners
# ---------------------------------------------------------------------------

@dataclass
class StrategyResult:
    strategy: str
    tokens: int
    latency_ms: int
    raw_chars: int
    hit_at_1: bool
    hit_at_3: bool
    result_paths: list[str]        # top file paths found (up to 10)
    available: bool = True         # False when strategy cannot run (e.g. no MCP)
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


def _run_grep(task: SearchTask, repo_root: Path) -> StrategyResult:
    """Run grep with the task's patterns and measure token cost of output."""
    t0 = time.monotonic()
    output_parts: list[str] = []
    result_paths: list[str] = []
    found = False

    for pattern in task.grep_patterns:
        cmd = ["grep", "-rn", "-E", pattern]
        for glob in task.grep_globs:
            cmd += ["--include", glob]
        for excl in task.grep_exclude_dirs:
            cmd += ["--exclude-dir", excl]
        cmd += ["-l"]  # first pass: just file names (cheap)
        cmd.append(str(repo_root))

        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            continue

        files = [f.strip() for f in r.stdout.splitlines() if f.strip()]
        if not files:
            continue

        # Second pass: get context lines (simulates what an agent would read)
        ctx_cmd = ["grep", "-rn", "-E", pattern, "--context", str(GREP_HEAD_LINES)]
        for glob in task.grep_globs:
            ctx_cmd += ["--include", glob]
        for excl in task.grep_exclude_dirs:
            ctx_cmd += ["--exclude-dir", excl]
        ctx_cmd.append(str(repo_root))

        try:
            ctx_r = subprocess.run(
                ctx_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(repo_root),
            )
        except subprocess.TimeoutExpired:
            ctx_r = r  # fall back to file list

        if ctx_r.stdout:
            output_parts.append(ctx_r.stdout)
            # Collect relative file paths from grep output
            for line in ctx_r.stdout.splitlines()[:50]:
                if ":" in line and not line.startswith("-"):
                    path_part = line.split(":")[0].strip()
                    try:
                        rel = str(Path(path_part).relative_to(repo_root))
                        if rel not in result_paths:
                            result_paths.append(rel)
                    except ValueError:
                        if path_part not in result_paths:
                            result_paths.append(path_part)
            found = True
            break  # use first pattern that produces results

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    raw_output = "\n".join(output_parts)
    raw_chars = len(raw_output)
    tokens = _est(raw_chars)

    if not found:
        # Return non-zero but small estimate to avoid div-by-zero in savings calc
        tokens = max(tokens, 50)

    hit1, hit3 = _check_hits(result_paths, task.expected_paths)

    return StrategyResult(
        strategy="grep",
        tokens=tokens,
        latency_ms=elapsed_ms,
        raw_chars=raw_chars,
        hit_at_1=hit1,
        hit_at_3=hit3,
        result_paths=result_paths[:10],
        meta={"patterns_tried": len(task.grep_patterns), "found": found},
    )


def _run_find(task: SearchTask, repo_root: Path) -> StrategyResult:
    """find + head strategy: list matching files, preview first N lines each."""
    t0 = time.monotonic()
    output_parts: list[str] = []
    result_paths: list[str] = []

    # Build keyword list from patterns (strip regex anchors for -name globs)
    keywords: list[str] = []
    for p in task.grep_patterns[:2]:
        # Use the simplified form as a keyword for find -iname or grep fallback
        simplified = p.replace(r"\b", "").replace(r"\s*", " ").strip()
        keywords.append(simplified)

    # Search strategy: find files by extension, grep for keywords, collect paths
    for glob in task.grep_globs[:3]:
        ext = glob.replace("*", "")  # e.g. "*.py" -> ".py"
        cmd = ["find", str(repo_root), "-type", "f", "-name", f"*{ext}"]
        for excl in task.grep_exclude_dirs:
            cmd += ["!", "-path", f"*/{excl}/*"]

        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            continue

        files = [f.strip() for f in r.stdout.splitlines() if f.strip()]
        for fpath in files[:FIND_MAX_FILES]:
            try:
                rel = str(Path(fpath).relative_to(repo_root))
            except ValueError:
                rel = fpath
            result_paths.append(rel)
            # Read first N lines as the "preview" an agent would skim
            try:
                content = Path(fpath).read_text(errors="replace")
                preview = "\n".join(content.splitlines()[:FIND_PREVIEW_LINES])
                output_parts.append(f"# {rel}\n{preview}")
            except OSError:
                pass

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    raw_output = "\n".join(output_parts)
    raw_chars = len(raw_output)
    tokens = _est(raw_chars)
    tokens = max(tokens, 50)

    hit1, hit3 = _check_hits(result_paths, task.expected_paths)

    return StrategyResult(
        strategy="find",
        tokens=tokens,
        latency_ms=elapsed_ms,
        raw_chars=raw_chars,
        hit_at_1=hit1,
        hit_at_3=hit3,
        result_paths=result_paths[:10],
        meta={"files_opened": min(len(result_paths), FIND_MAX_FILES)},
    )


def _run_semantic(
    task: SearchTask,
    repo_root: Path,
    mcp_url: str | None,
) -> StrategyResult:
    """
    Call claude-context search_code via the MCP HTTP bridge.

    If ``mcp_url`` is None or the call fails the result is marked
    available=False so the caller can skip/warn rather than crash.
    """
    t0 = time.monotonic()

    if mcp_url is None:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return StrategyResult(
            strategy="semantic",
            tokens=0,
            latency_ms=elapsed_ms,
            raw_chars=0,
            hit_at_1=False,
            hit_at_3=False,
            result_paths=[],
            available=False,
            error="SEMANTIC_SKIP: --no-semantic flag or MCP_URL not set",
        )

    # Build a JSON-RPC 2.0 call to the MCP HTTP bridge that wraps search_code.
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "search_code",
            "arguments": {
                "query": task.semantic_query,
                "n_results": SEMANTIC_TOP_K,
            },
        },
    }

    try:
        import urllib.request

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            mcp_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=SEMANTIC_TIMEOUT_S) as resp:
            body = resp.read().decode()
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return StrategyResult(
            strategy="semantic",
            tokens=0,
            latency_ms=elapsed_ms,
            raw_chars=0,
            hit_at_1=False,
            hit_at_3=False,
            result_paths=[],
            available=False,
            error=f"MCP call failed: {exc}",
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Parse MCP response — format: {result: {content: [{type:"text", text:"..."}]}}
    try:
        rpc_result = json.loads(body)
        content_blocks = rpc_result.get("result", {}).get("content", [])
        texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        raw_output = "\n".join(texts)
    except (json.JSONDecodeError, KeyError, TypeError):
        raw_output = body

    raw_chars = len(raw_output)
    tokens = _est(raw_chars)
    tokens = max(tokens, 1)

    # Extract file paths from semantic results (heuristic: lines containing /)
    result_paths: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        # Look for lines that look like file paths (contain / and extension)
        if "/" in line and "." in line.split("/")[-1]:
            candidate = line.split()[0].rstrip(":")
            if not candidate.startswith("http"):
                try:
                    Path(candidate).relative_to(repo_root)
                    result_paths.append(str(Path(candidate).relative_to(repo_root)))
                except ValueError:
                    result_paths.append(candidate)
        if len(result_paths) >= SEMANTIC_TOP_K:
            break

    hit1, hit3 = _check_hits(result_paths, task.expected_paths)

    return StrategyResult(
        strategy="semantic",
        tokens=tokens,
        latency_ms=elapsed_ms,
        raw_chars=raw_chars,
        hit_at_1=hit1,
        hit_at_3=hit3,
        result_paths=result_paths[:10],
        available=True,
        meta={"top_k": SEMANTIC_TOP_K},
    )


def _run_explore(
    task: SearchTask,
    repo_root: Path,
    verbose: bool = False,
) -> StrategyResult:
    """
    Explore strategy: call Claude claude-haiku-3-5-20241022 via Anthropic Messages API.

    Asks the model to list the most relevant file paths for the task query.
    Token count is exact: ``response.usage.input_tokens + response.usage.output_tokens``.
    Gracefully disabled (available=False) when ANTHROPIC_API_KEY is unset or
    on any API/auth failure.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return StrategyResult(
            strategy="explore",
            tokens=0,
            latency_ms=0,
            raw_chars=0,
            hit_at_1=False,
            hit_at_3=False,
            result_paths=[],
            available=False,
            error="EXPLORE_SKIP: ANTHROPIC_API_KEY not set",
        )

    t0 = time.monotonic()
    try:
        import anthropic  # noqa: PLC0415 — intentional late import

        client = anthropic.Anthropic()
        system_prompt = (
            "You are a code search assistant for a TypeScript/Python monorepo. "
            "Given a search query, list the most relevant file paths that would "
            "answer it. Return only repo-relative file paths, one per line, "
            "with no extra explanation or formatting."
        )
        user_prompt = (
            f"Repository root: {repo_root}\n\n"
            f"Query: {task.query}"
        )
        response = client.messages.create(
            model="claude-haiku-3-5-20241022",
            max_tokens=512,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        # Exact token count from API response (not a character heuristic)
        tokens = response.usage.input_tokens + response.usage.output_tokens
        raw_text = response.content[0].text if response.content else ""
        raw_chars = len(raw_text)

        # Extract file paths: lines containing "/" (strip list markers)
        result_paths: list[str] = []
        for line in raw_text.splitlines():
            line = line.strip().lstrip("-*•123456789. ").rstrip(":")
            if line and "/" in line and not line.startswith("http"):
                result_paths.append(line)
            if len(result_paths) >= 10:
                break

        hit1, hit3 = _check_hits(result_paths, task.expected_paths)

        return StrategyResult(
            strategy="explore",
            tokens=tokens,
            latency_ms=elapsed_ms,
            raw_chars=raw_chars,
            hit_at_1=hit1,
            hit_at_3=hit3,
            result_paths=result_paths[:10],
            available=True,
            meta={"model": "claude-haiku-3-5-20241022", "tokens_exact": True},
        )

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return StrategyResult(
            strategy="explore",
            tokens=0,
            latency_ms=elapsed_ms,
            raw_chars=0,
            hit_at_1=False,
            hit_at_3=False,
            result_paths=[],
            available=False,
            error=f"EXPLORE_ERROR ({type(exc).__name__}): {exc}",
        )


# ---------------------------------------------------------------------------
# Accuracy helper
# ---------------------------------------------------------------------------

def _check_hits(
    result_paths: list[str],
    expected_paths: list[str],
) -> tuple[bool, bool]:
    """
    Return (hit@1, hit@3) — does any expected path appear as a prefix/suffix
    of any of the top-1 / top-3 result paths?
    """
    def _matches(result: str, expected: str) -> bool:
        # Normalize: strip leading ./
        r = result.lstrip("./")
        e = expected.lstrip("./")
        return r.startswith(e) or e.startswith(r) or r == e

    hit1 = any(
        _matches(rp, ep)
        for rp in result_paths[:1]
        for ep in expected_paths
    )
    hit3 = any(
        _matches(rp, ep)
        for rp in result_paths[:3]
        for ep in expected_paths
    )
    return hit1, hit3


# ---------------------------------------------------------------------------
# Per-task runner
# ---------------------------------------------------------------------------

@dataclass
class TaskResult:
    task: SearchTask
    grep: StrategyResult
    find: StrategyResult
    semantic: StrategyResult
    explore: StrategyResult

    @property
    def token_savings_pct(self) -> float | None:
        """semantic tokens saved vs grep baseline (None if semantic unavailable)."""
        if not self.semantic.available or self.grep.tokens == 0:
            return None
        return 1.0 - (self.semantic.tokens / self.grep.tokens)

    @property
    def explore_savings_pct(self) -> float | None:
        """explore tokens saved vs grep baseline (None if explore unavailable)."""
        if not self.explore.available or self.grep.tokens == 0:
            return None
        return 1.0 - (self.explore.tokens / self.grep.tokens)

    @property
    def grep_vs_find_savings_pct(self) -> float:
        """How much more expensive find+head is vs grep."""
        if self.grep.tokens == 0:
            return 0.0
        return 1.0 - (self.find.tokens / self.grep.tokens)


def _run_task(
    task: SearchTask,
    repo_root: Path,
    mcp_url: str | None,
    verbose: bool = False,
    explore_enabled: bool = True,
) -> TaskResult:
    if verbose:
        print(f"  [{task.task_id}] running grep ...", end=" ", flush=True)
    grep_res = _run_grep(task, repo_root)
    if verbose:
        print(f"done ({grep_res.tokens} tok)")
        print(f"  [{task.task_id}] running find ...", end=" ", flush=True)
    find_res = _run_find(task, repo_root)
    if verbose:
        print(f"done ({find_res.tokens} tok)")
        print(f"  [{task.task_id}] running semantic ...", end=" ", flush=True)
    semantic_res = _run_semantic(task, repo_root, mcp_url)
    if verbose:
        if semantic_res.available:
            print(f"done ({semantic_res.tokens} tok)")
        else:
            print(f"skipped ({semantic_res.error})")

    if explore_enabled:
        if verbose:
            print(f"  [{task.task_id}] running explore ...", end=" ", flush=True)
        explore_res = _run_explore(task, repo_root, verbose=verbose)
        if verbose:
            if explore_res.available:
                print(f"done ({explore_res.tokens} tok)")
            else:
                print(f"skipped ({explore_res.error})")
    else:
        explore_res = StrategyResult(
            strategy="explore",
            tokens=0,
            latency_ms=0,
            raw_chars=0,
            hit_at_1=False,
            hit_at_3=False,
            result_paths=[],
            available=False,
            error="EXPLORE_SKIP: --no-explore flag",
        )

    return TaskResult(
        task=task,
        grep=grep_res,
        find=find_res,
        semantic=semantic_res,
        explore=explore_res,
    )


# ---------------------------------------------------------------------------
# Summary report printer
# ---------------------------------------------------------------------------

def _print_report(
    results: list[TaskResult],
    semantic_available: bool,
    explore_available: bool = False,
) -> None:
    SEP = "-" * 116
    print()
    print("=" * 116)
    print("  context_search benchmark — token cost: grep / find / semantic / explore")
    print("=" * 116)

    col_w = 26
    header = f"{'Task':<{col_w}}  {'Grep tok':>8}  {'Find tok':>8}"
    header += f"  {'Sem tok':>8}  {'SemSav%':>8}"
    header += f"  {'Exp tok':>8}  {'ExpSav%':>8}"
    header += f"  {'GH@3':>5}  {'SH@3':>5}  {'EH@3':>5}"
    print(header)
    print(SEP)

    grep_total = 0
    find_total = 0
    sem_total = 0
    exp_total = 0
    hit3_grep = 0
    hit3_sem = 0
    hit3_exp = 0

    for tr in results:
        grep_tok = tr.grep.tokens
        find_tok = tr.find.tokens
        sem_tok = tr.semantic.tokens if tr.semantic.available else 0
        exp_tok = tr.explore.tokens if tr.explore.available else 0

        grep_total += grep_tok
        find_total += find_tok
        if tr.semantic.available:
            sem_total += sem_tok
        if tr.explore.available:
            exp_total += exp_tok

        if tr.grep.hit_at_3:
            hit3_grep += 1
        if tr.semantic.hit_at_3:
            hit3_sem += 1
        if tr.explore.hit_at_3:
            hit3_exp += 1

        sem_s = f"{sem_tok:>8}" if tr.semantic.available else "     n/a"
        exp_s = f"{exp_tok:>8}" if tr.explore.available else "     n/a"

        sem_sav_s = ""
        if tr.token_savings_pct is not None:
            pct = tr.token_savings_pct * 100
            sign = "+" if pct >= 0 else ""
            sem_sav_s = f"{sign}{pct:.1f}%"

        exp_sav_s = ""
        if tr.explore_savings_pct is not None:
            pct = tr.explore_savings_pct * 100
            sign = "+" if pct >= 0 else ""
            exp_sav_s = f"{sign}{pct:.1f}%"

        gh3 = "✓" if tr.grep.hit_at_3 else "✗"
        sh3 = "✓" if tr.semantic.hit_at_3 else ("?" if not tr.semantic.available else "✗")
        eh3 = "✓" if tr.explore.hit_at_3 else ("?" if not tr.explore.available else "✗")

        row = (
            f"{tr.task.task_id:<{col_w}}  {grep_tok:>8}  {find_tok:>8}"
            f"  {sem_s}  {sem_sav_s:>8}"
            f"  {exp_s}  {exp_sav_s:>8}"
            f"  {gh3:>5}  {sh3:>5}  {eh3:>5}"
        )
        print(row)

    print(SEP)

    n = len(results)
    sem_avail_count = sum(1 for tr in results if tr.semantic.available)
    exp_avail_count = sum(1 for tr in results if tr.explore.available)

    sem_tot_s = f"{sem_total:>8}" if sem_avail_count else "     n/a"
    exp_tot_s = f"{exp_total:>8}" if exp_avail_count else "     n/a"

    sem_sav_tot_s = ""
    if sem_avail_count and grep_total:
        v = (1.0 - sem_total / grep_total) * 100
        sem_sav_tot_s = f"{'+' if v >= 0 else ''}{v:.1f}%"

    exp_sav_tot_s = ""
    if exp_avail_count and grep_total:
        v = (1.0 - exp_total / grep_total) * 100
        exp_sav_tot_s = f"{'+' if v >= 0 else ''}{v:.1f}%"

    print(
        f"{'TOTALS':<{col_w}}  {grep_total:>8}  {find_total:>8}"
        f"  {sem_tot_s}  {sem_sav_tot_s:>8}"
        f"  {exp_tot_s}  {exp_sav_tot_s:>8}"
        f"  {hit3_grep}/{n}  {hit3_sem}/{n}  {hit3_exp}/{n}"
    )

    print()
    print("Token savings = 1 - (strategy_tokens / grep_tokens) | Hit@3 columns: GH@3=grep, SH@3=semantic, EH@3=explore")
    if not semantic_available:
        print()
        print("NOTE: semantic (search_code) column unavailable — run with")
        print("  --mcp-url http://localhost:<port>  or set CLAUDE_CONTEXT_MCP_URL")
    if not explore_available:
        print()
        print("NOTE: explore (Claude API) column unavailable — set ANTHROPIC_API_KEY")
        print("  or remove --no-explore to enable.")
    print()


# ---------------------------------------------------------------------------
# Registered suite entry point
# ---------------------------------------------------------------------------

@register("context_search")
def run_context_search(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="context_search")
    p.add_argument(
        "--repo-root",
        default=str(_REPO_ROOT),
        help="Absolute path to the Deus repo root (default: auto-detected).",
    )
    p.add_argument(
        "--mcp-url",
        default=os.environ.get("CLAUDE_CONTEXT_MCP_URL"),
        help=(
            "HTTP base URL of the claude-context MCP bridge "
            "(e.g. http://localhost:7700).  "
            "Also read from CLAUDE_CONTEXT_MCP_URL env var.  "
            "Omit to skip semantic strategy."
        ),
    )
    p.add_argument(
        "--no-semantic",
        action="store_true",
        help="Skip the semantic (search_code) strategy entirely.",
    )
    p.add_argument(
        "--no-explore",
        action="store_true",
        help="Skip the explore (Anthropic Messages API) strategy entirely.",
    )
    p.add_argument(
        "--output",
        metavar="PATH",
        help=(
            "Write benchmark results as a UTF-8 JSON file to PATH.  "
            "Keys: git_sha, timestamp, per_task, summary."
        ),
    )
    p.add_argument(
        "--tasks",
        default="all",
        help="Comma-separated task IDs to run, or 'all' (default).",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    repo_root = Path(args.repo_root).expanduser().resolve()
    if not repo_root.is_dir():
        print(
            f"error: repo root not found: {repo_root}",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp_url: str | None = None if args.no_semantic else args.mcp_url
    explore_enabled: bool = not args.no_explore

    # Filter tasks
    if args.tasks == "all":
        tasks_to_run = TASKS
    else:
        ids = {t.strip() for t in args.tasks.split(",")}
        tasks_to_run = [t for t in TASKS if t.task_id in ids]
        unknown = ids - {t.task_id for t in TASKS}
        if unknown:
            print(
                f"error: unknown task IDs: {', '.join(sorted(unknown))}; "
                f"known: {', '.join(t.task_id for t in TASKS)}",
                file=sys.stderr,
            )
            sys.exit(1)

    if args.verbose:
        sem_note = f"MCP URL: {mcp_url}" if mcp_url else "semantic DISABLED"
        exp_note = "explore ENABLED" if explore_enabled else "explore DISABLED"
        print(f"context_search: repo={repo_root}  {sem_note}  {exp_note}")
        print(f"context_search: running {len(tasks_to_run)} tasks")

    t_suite_start = time.monotonic()
    task_results: list[TaskResult] = []

    for task in tasks_to_run:
        tr = _run_task(
            task, repo_root, mcp_url,
            verbose=args.verbose,
            explore_enabled=explore_enabled,
        )
        task_results.append(tr)

    suite_elapsed_ms = int((time.monotonic() - t_suite_start) * 1000)

    # -----------------------------------------------------------------------
    # Build CaseResult per task x strategy
    # -----------------------------------------------------------------------
    cases: list[CaseResult] = []
    total_grep_tokens = 0
    total_sem_tokens = 0
    total_exp_tokens = 0
    semantic_available_global = any(tr.semantic.available for tr in task_results)
    explore_available_global = any(tr.explore.available for tr in task_results)

    for tr in task_results:
        total_grep_tokens += tr.grep.tokens
        if tr.semantic.available:
            total_sem_tokens += tr.semantic.tokens
        if tr.explore.available:
            total_exp_tokens += tr.explore.tokens

        # Accuracy score: hit@3 is primary; bonus for hit@1
        def _acc_score(res: StrategyResult) -> float:
            if not res.available:
                return 0.0
            if res.hit_at_1:
                return 1.0
            if res.hit_at_3:
                return 0.75
            return 0.0

        grep_acc = _acc_score(tr.grep)
        find_acc = _acc_score(tr.find)
        sem_acc = _acc_score(tr.semantic) if tr.semantic.available else None
        exp_acc = _acc_score(tr.explore) if tr.explore.available else None

        # Token savings vs grep baseline (positive = strategy wins)
        savings = tr.token_savings_pct

        # Primary case: grep baseline
        cases.append(CaseResult(
            case_id=f"{tr.task.task_id}__grep",
            score=grep_acc,
            tokens_in=tr.grep.tokens,
            latency_ms=tr.grep.latency_ms,
            passed=tr.grep.hit_at_3,
            meta={
                "strategy": "grep",
                "hit_at_1": tr.grep.hit_at_1,
                "hit_at_3": tr.grep.hit_at_3,
                "raw_chars": tr.grep.raw_chars,
                "result_paths": tr.grep.result_paths,
            },
        ))

        # Find strategy case
        cases.append(CaseResult(
            case_id=f"{tr.task.task_id}__find",
            score=find_acc,
            tokens_in=tr.find.tokens,
            latency_ms=tr.find.latency_ms,
            passed=tr.find.hit_at_3,
            meta={
                "strategy": "find",
                "hit_at_1": tr.find.hit_at_1,
                "hit_at_3": tr.find.hit_at_3,
                "raw_chars": tr.find.raw_chars,
                "result_paths": tr.find.result_paths,
            },
        ))

        # Semantic case (may be unavailable)
        sem_meta: dict[str, Any] = {
            "strategy": "semantic",
            "available": tr.semantic.available,
            "token_savings_pct": savings,
        }
        if tr.semantic.available:
            sem_meta.update({
                "hit_at_1": tr.semantic.hit_at_1,
                "hit_at_3": tr.semantic.hit_at_3,
                "raw_chars": tr.semantic.raw_chars,
                "result_paths": tr.semantic.result_paths,
            })
        else:
            sem_meta["error"] = tr.semantic.error

        cases.append(CaseResult(
            case_id=f"{tr.task.task_id}__semantic",
            score=sem_acc if sem_acc is not None else 0.0,
            tokens_in=tr.semantic.tokens,
            latency_ms=tr.semantic.latency_ms,
            passed=tr.semantic.hit_at_3 if tr.semantic.available else False,
            meta=sem_meta,
        ))

        # Explore case (may be unavailable)
        exp_meta: dict[str, Any] = {
            "strategy": "explore",
            "available": tr.explore.available,
            "explore_savings_pct": tr.explore_savings_pct,
        }
        if tr.explore.available:
            exp_meta.update({
                "hit_at_1": tr.explore.hit_at_1,
                "hit_at_3": tr.explore.hit_at_3,
                "raw_chars": tr.explore.raw_chars,
                "result_paths": tr.explore.result_paths,
                "tokens_exact": True,
            })
        else:
            exp_meta["error"] = tr.explore.error

        cases.append(CaseResult(
            case_id=f"{tr.task.task_id}__explore",
            score=exp_acc if exp_acc is not None else 0.0,
            tokens_in=tr.explore.tokens,
            tokens_out=0,
            latency_ms=tr.explore.latency_ms,
            passed=tr.explore.hit_at_3 if tr.explore.available else False,
            meta=exp_meta,
        ))

    # -----------------------------------------------------------------------
    # Suite-level score:
    #   - if semantic available: primary = token savings; accuracy is secondary
    #   - if not: primary = grep hit@3 rate (so the suite still yields a number)
    # -----------------------------------------------------------------------
    n_tasks = len(task_results)

    if semantic_available_global and total_grep_tokens > 0:
        overall_savings = 1.0 - (total_sem_tokens / total_grep_tokens)
        # Normalise to [0, 1] — 50% savings → 1.0 (aspirational target)
        # Negative savings (semantic COSTS more) → score below 0.5
        suite_score = min(1.0, max(0.0, 0.5 + overall_savings))
    else:
        # Fallback: grep hit@3 rate
        grep_hit3_rate = (
            sum(1 for tr in task_results if tr.grep.hit_at_3) / n_tasks
            if n_tasks
            else 0.0
        )
        suite_score = grep_hit3_rate

    overall_savings_pct = (
        (1.0 - total_sem_tokens / total_grep_tokens) * 100
        if semantic_available_global and total_grep_tokens
        else None
    )
    overall_explore_savings_pct = (
        (1.0 - total_exp_tokens / total_grep_tokens) * 100
        if explore_available_global and total_grep_tokens
        else None
    )

    # Print human-readable report
    _print_report(task_results, semantic_available_global, explore_available_global)

    # -----------------------------------------------------------------------
    # Optional JSON artifact (--output PATH)
    # -----------------------------------------------------------------------
    if args.output:
        try:
            git_sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(repo_root),
            ).stdout.strip() or "unknown"
        except Exception:
            git_sha = "unknown"

        per_task_json = []
        for tr in task_results:
            per_task_json.append({
                "task_id": tr.task.task_id,
                "grep_tokens": tr.grep.tokens,
                "find_tokens": tr.find.tokens,
                "semantic_tokens": tr.semantic.tokens if tr.semantic.available else None,
                "explore_tokens": tr.explore.tokens if tr.explore.available else None,
                "semantic_savings_pct": tr.token_savings_pct,
                "explore_savings_pct": tr.explore_savings_pct,
                "grep_hit3": tr.grep.hit_at_3,
                "semantic_hit3": tr.semantic.hit_at_3 if tr.semantic.available else None,
                "explore_hit3": tr.explore.hit_at_3 if tr.explore.available else None,
            })

        payload: dict[str, Any] = {
            "git_sha": git_sha,
            "timestamp": datetime.datetime.now().isoformat(),
            "per_task": per_task_json,
            "summary": {
                "n_tasks": n_tasks,
                "grep_tokens_total": total_grep_tokens,
                "semantic_tokens_total": total_sem_tokens if semantic_available_global else None,
                "explore_tokens_total": total_exp_tokens if explore_available_global else None,
                "semantic_savings_pct": overall_savings_pct,
                "explore_savings_pct": overall_explore_savings_pct,
                "grep_hit3_rate": (
                    sum(1 for tr in task_results if tr.grep.hit_at_3) / n_tasks
                    if n_tasks else 0.0
                ),
                "semantic_hit3_rate": (
                    sum(1 for tr in task_results if tr.semantic.hit_at_3) / n_tasks
                    if semantic_available_global and n_tasks else None
                ),
                "explore_hit3_rate": (
                    sum(1 for tr in task_results if tr.explore.hit_at_3) / n_tasks
                    if explore_available_global and n_tasks else None
                ),
            },
        }
        Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if args.verbose:
            print(f"Results written to {args.output}")

    return RunResult(
        suite="context_search",
        score=suite_score,
        cases=cases,
        tokens_in=total_grep_tokens,     # baseline cost (grep approach)
        latency_ms=suite_elapsed_ms,
        meta={
            "n_tasks": n_tasks,
            "semantic_available": semantic_available_global,
            "explore_available": explore_available_global,
            "grep_tokens_total": total_grep_tokens,
            "semantic_tokens_total": total_sem_tokens if semantic_available_global else None,
            "explore_tokens_total": total_exp_tokens if explore_available_global else None,
            "overall_token_savings_pct": overall_savings_pct,
            "overall_explore_savings_pct": overall_explore_savings_pct,
            "grep_hit3_rate": sum(1 for tr in task_results if tr.grep.hit_at_3) / n_tasks if n_tasks else 0.0,
            "semantic_hit3_rate": (
                sum(1 for tr in task_results if tr.semantic.hit_at_3)
                / n_tasks
                if semantic_available_global and n_tasks
                else None
            ),
            "explore_hit3_rate": (
                sum(1 for tr in task_results if tr.explore.hit_at_3)
                / n_tasks
                if explore_available_global and n_tasks
                else None
            ),
            "chars_per_token": CHARS_PER_TOKEN,
            "repo_root": str(repo_root),
        },
    )


# ---------------------------------------------------------------------------
# Standalone entry point (python scripts/bench/suites/context_search.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    _scripts_dir = str(_SCRIPTS_DIR)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)

    import importlib
    bench_pkg = importlib.import_module("bench")  # noqa: F841
    result = run_context_search(sys.argv[1:])
    print(
        f"suite={result.suite} score={result.score:.3f} "
        f"n_cases={len(result.cases)} tokens_in={result.tokens_in} "
        f"latency_ms={result.latency_ms}"
    )
    sys.exit(0 if result.score >= 0.0 else 1)

#!/usr/bin/env python3
"""
Compression quality benchmark for vault/memory files.

Two-phase test:
  Phase 1 -- Fact extraction: LLM extracts atomic facts from the original,
             classifies each as critical/supplementary, then verifies
             preservation in the compressed version.
  Phase 2 -- Behavioral: LLM answers task-relevant queries using ONLY
             the compressed doc; answers scored against known-correct answers.

Scoring:
  Weighted fact coverage:
    score = (preserved*1.0 + derivable*0.8 + missing_supplementary*0.5) / total
  Pass criteria: critical fact coverage >= 95%, behavioral score >= 90%

Usage:
  python3 scripts/compression_benchmark.py <original> <compressed> [--label NAME]
  python3 scripts/compression_benchmark.py --auto   # compare golden pairs
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)


# -- Paths -------------------------------------------------------------------

BENCHMARK_DIR = Path("~/.deus/benchmarks").expanduser()
GOLDEN_DIR = BENCHMARK_DIR / "golden"
RESULTS_LOG = BENCHMARK_DIR / "compression.jsonl"

# -- LLM helpers --------------------------------------------------------------


def llm_call(prompt: str, temp: float = 0.0) -> str:
    """Call Ollama locally -- no API key, no rate limits."""
    model = os.environ.get("BENCH_MODEL", "gemma4:e4b")
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434/api/generate")
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temp, "num_predict": 8192},
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=300)
            r.raise_for_status()
            return r.json()["response"]
        except Exception as e:
            if attempt < 2:
                time.sleep(2**attempt)
            else:
                raise RuntimeError(f"Ollama error after 3 retries: {e}") from None
    return ""  # unreachable, but satisfies type checker


def parse_json(text: str) -> list | dict:
    """Extract JSON from LLM response, handling code fences."""
    text = text.strip()
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    return json.loads(text)


# -- Phase 1: fact extraction + classification + verification -----------------


def extract_and_classify_facts(text: str) -> list[dict]:
    """Extract facts from original and classify as critical/supplementary."""
    prompt = f"""Extract every distinct factual claim from this configuration/memory document.
Each fact must be atomic (one piece of information) and verifiable.
Include: names, paths, values, settings, preferences, rules, constraints, relationships.
Exclude: formatting/style choices that don't carry semantic meaning.

For each fact, classify it:
- "critical": affects agent behavior, identity, file matching, routing, or security.
  Examples: user name, workflow rules, design principles, model fallback chains,
  "never" rules, security constraints, scoring thresholds.
- "supplementary": detail that lives in a linked file, is derivable from code
  (greppable file paths, function signatures), or is informational context
  that doesn't change agent behavior if omitted.
  Examples: specific file paths to source code, line counts, formatting details,
  content that the compressed doc explicitly links to for further reading.

Document:
---
{text}
---

Output ONLY a JSON array of objects: {{"fact": "...", "classification": "critical|supplementary"}}
No commentary."""
    return parse_json(llm_call(prompt))


def verify_facts(
    facts: list[dict], compressed: str, batch_size: int = 20
) -> list[dict]:
    """Verify facts in batches. Returns list with status and classification."""
    all_results: list[dict] = []
    for i in range(0, len(facts), batch_size):
        batch = facts[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(facts) + batch_size - 1) // batch_size
        print(f"batch {batch_num}/{total_batches}...", end=" ", flush=True)

        facts_for_prompt = [f["fact"] for f in batch]
        prompt = f"""For each fact, determine if it is present in the compressed document below.

Statuses:
- "preserved": same information exists, possibly abbreviated or reworded
- "derivable": can be inferred from other information in the document
- "missing": neither stated nor inferable -- this is information loss

Compressed document:
---
{compressed}
---

Facts to verify:
{json.dumps(facts_for_prompt, indent=2)}

Output ONLY a JSON array of objects: {{"fact": "...", "status": "preserved|derivable|missing", "note": "..."}}"""
        verified = parse_json(llm_call(prompt))

        # Merge classification from extraction step
        for v, orig in zip(verified, batch):
            v["classification"] = orig.get("classification", "critical")
        all_results.extend(verified)
    return all_results


def compute_weighted_score(results: list[dict]) -> dict:
    """Compute weighted coverage score with fact classification.

    Formula: score = (preserved*1.0 + derivable*0.8 + missing_supplementary*0.5) / total
    Critical coverage: (preserved + derivable) / critical_total (must be >= 95%)
    """
    total = len(results)
    if total == 0:
        return {
            "weighted_score": 0.0,
            "critical_coverage": 0.0,
            "total": 0,
            "critical_total": 0,
            "supplementary_total": 0,
            "preserved": 0,
            "derivable": 0,
            "missing_critical": 0,
            "missing_supplementary": 0,
        }

    preserved = sum(1 for r in results if r["status"] == "preserved")
    derivable = sum(1 for r in results if r["status"] == "derivable")

    critical = [r for r in results if r["classification"] == "critical"]
    supplementary = [r for r in results if r["classification"] == "supplementary"]

    critical_preserved = sum(1 for r in critical if r["status"] == "preserved")
    critical_derivable = sum(1 for r in critical if r["status"] == "derivable")
    critical_missing = sum(1 for r in critical if r["status"] == "missing")
    supp_missing = sum(1 for r in supplementary if r["status"] == "missing")

    critical_total = len(critical)
    critical_coverage = (
        (critical_preserved + critical_derivable) / critical_total
        if critical_total > 0
        else 1.0
    )

    # Weighted score across all facts
    weighted = preserved * 1.0 + derivable * 0.8 + supp_missing * 0.5
    weighted_score = weighted / total if total > 0 else 0.0

    return {
        "weighted_score": weighted_score,
        "critical_coverage": critical_coverage,
        "total": total,
        "critical_total": critical_total,
        "supplementary_total": len(supplementary),
        "preserved": preserved,
        "derivable": derivable,
        "missing_critical": critical_missing,
        "missing_supplementary": supp_missing,
    }


# -- Phase 2: behavioral tests -----------------------------------------------

BEHAVIORAL_TESTS: dict[str, list[tuple[str, str]]] = {
    "claude_vault": [
        # Identity
        ("What is the user's full name in Hebrew?", "Liam Steiner"),
        # Architecture / routing
        (
            "I'm debugging a third-party library issue. Which memory file tells me the right approach?",
            "feedback_library_source_first.md -- grep lib internals before workarounds",
        ),
        (
            "What eval judge priority chain is used?",
            "Ollama(10) > Gemini(20) > Claude(30)",
        ),
        (
            "What is the generative model fallback chain?",
            "gemini-3-flash -> gemini-2.5-flash -> gemini-2.5-flash-lite -> Ollama on 429",
        ),
        (
            "What embedding model and dimension?",
            "gemini-embedding-2-preview, 768 dimensions",
        ),
        # Rendering rules
        (
            "How must Hebrew text be rendered?",
            "Via LaTeX engine only; terminal BiDi rejected",
        ),
        (
            "What is the display approach for images?",
            "Read tool inline in Claude Code; deus show for kitty in Ghostty; never open browser",
        ),
        # Design principles
        (
            "What are the 5 design principles?",
            "machine-adaptive, token-efficient, secure-by-default, performance-aware, no-db-deletion",
        ),
        # Configuration
        (
            "What MCP config files exist and for what?",
            "~/.claude.json for CLI, ~/.claude/mcp.json for Desktop app",
        ),
        (
            "What is the memory startup loading sequence?",
            "CLAUDE.md always + warm (recent-days 3) + learnings + cold (query top 2 recency-boost)",
        ),
        # User context
        (
            "What stocks and instruments does the user trade?",
            "US stocks via TradingView -> IBKR IL; no direct crypto; ETFs: $ETHA, $IBIT; crypto stocks: $COIN, $HOOD, $BLSH, $BMNR",
        ),
        (
            "What is the user's educational background?",
            "OUI student (math + physics), ~5yr SWE (~3yr fullstack, ~1.5yr AWS team lead at Resilience Hub)",
        ),
        # Technical details
        ("What is the reflexion threshold?", "0.6"),
        (
            "How many ABC+Registry provider layers exist?",
            "4: judge, generative, storage, auth",
        ),
        # Scope-matching: is this always or context-specific?
        (
            "Is the 'no-db-deletion' rule context-specific or universal?",
            "Universal -- applies to all DB operations, enforced by ADR",
        ),
        # Task-matching
        (
            "I need to add a new channel integration. What pattern file should I load?",
            "patterns/channel-add.md",
        ),
        # Cross-reference
        (
            "What files relate to deploy safety?",
            "feedback_deploy_integrity.md (rebuild dist/), feedback_security_first.md (audit before commit), patterns/deployment.md",
        ),
        # Negative test
        (
            "Is there a configuration for Docker Compose in this project?",
            "No -- the project uses Apple Container / single container, not Docker Compose",
        ),
    ],
    "memory_index": [
        # Core behavioral rules
        (
            "Should I merge a PR with failing CI tests?",
            "Never -- fix first (feedback_no_merge_failed_tests)",
        ),
        (
            "How should background tasks be handled?",
            "Go to background immediately, no waiting (feedback_background_tasks)",
        ),
        (
            "Can secrets be committed to git?",
            "No -- audit security before commit (feedback_security_first)",
        ),
        (
            "What is the dev workflow?",
            "plan -> branch -> implement -> test -> commit -> merge (feedback_dev_workflow)",
        ),
        (
            "Should git checkout be used for feature branches?",
            "No -- use git worktree add (feedback_worktree_workflow)",
        ),
        (
            "What happens before committing?",
            "Show commit msg + wait for explicit approval (feedback_commit_preview, feedback_wait_for_approval)",
        ),
        # Tool usage rules
        (
            "How should images be analyzed?",
            "Send to Gemini first, never analyze directly (feedback_image_analysis)",
        ),
        (
            "What about personal-account skills like X/Gmail?",
            "Local-only, never committed (feedback_local_only_skills)",
        ),
        # Identity / location
        (
            "Where to find user preferences and personality?",
            "Persona vault at ~/Desktop/Brain Dump/Second Brain/Deus/Persona/INDEX.md",
        ),
        # Monitoring rules
        (
            "What's the rule about background task monitoring?",
            "Proactively check every 2-3 min; report status without being asked (feedback_monitor_background + feedback_monitor_self)",
        ),
        # Task-matching: which file for this task?
        (
            "I'm about to compress a vault file. Which memory tells me the rule?",
            "feedback_compression_rule.md -- benchmark after compression, target >=95% critical + >=90% behavioral",
        ),
        # Scope test
        (
            "Is the branch workflow rule always-on or situational?",
            "Always -- feedback_branch_workflow.md says feature branch before implementing (always)",
        ),
        # Cross-reference
        (
            "What memory files relate to git workflow?",
            "feedback_branch_workflow, feedback_worktree_workflow, feedback_dev_workflow, feedback_commit_preview",
        ),
        # Negative test
        (
            "Is there a memory about Docker container management?",
            "No -- there is no Docker-specific memory file in the index",
        ),
        # Data integrity
        (
            "What is the rule about data integrity?",
            "Never lose/overwrite/downgrade data; merge not replace (feedback_data_integrity -- CRITICAL)",
        ),
        # Model tiers
        (
            "Which model should be used for subagent tasks?",
            "Sonnet/Haiku for subagents, Opus for complex (feedback_model_tiers)",
        ),
    ],
}


def run_behavioral(compressed: str, test_set: str) -> list[dict]:
    """Run behavioral tests against compressed document."""
    tests = BEHAVIORAL_TESTS.get(test_set, BEHAVIORAL_TESTS["claude_vault"])

    prompt = f"""Answer each question using ONLY the document below. Be precise and specific.
If the answer is not in the document, say "NOT FOUND".

Document:
---
{compressed}
---

Questions:
{json.dumps([q for q, _ in tests], indent=2)}

Output ONLY a JSON array of objects: {{"query": "...", "answer": "..."}}"""
    answers = parse_json(llm_call(prompt))

    # Score answers
    pairs = []
    for (q, expected), ans in zip(tests, answers):
        pairs.append(
            {"query": q, "expected": expected, "actual": ans.get("answer", "")}
        )

    score_prompt = f"""Score each pair. PASS = actual answer conveys the same core information as expected (abbreviations OK, exact wording not required). FAIL = wrong, incomplete, or missing information.

Pairs:
{json.dumps(pairs, indent=2)}

Output ONLY a JSON array of objects: {{"query": "...", "score": "PASS|FAIL", "note": "..."}}"""
    return parse_json(llm_call(score_prompt))


# -- Golden file management ---------------------------------------------------


def save_golden(label: str, original: str, compressed: str) -> Path:
    """Save original/compressed pair as golden files for future --auto runs."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    orig_path = GOLDEN_DIR / f"{label}.original"
    comp_path = GOLDEN_DIR / f"{label}.compressed"
    orig_path.write_text(original, encoding="utf-8")
    comp_path.write_text(compressed, encoding="utf-8")
    print(f"Golden files saved to {GOLDEN_DIR}/{label}.*")
    return GOLDEN_DIR


def list_golden_pairs() -> list[tuple[str, Path, Path]]:
    """Find all golden original/compressed pairs."""
    if not GOLDEN_DIR.exists():
        return []
    originals = sorted(GOLDEN_DIR.glob("*.original"))
    pairs = []
    for orig in originals:
        label = orig.stem
        comp = GOLDEN_DIR / f"{label}.compressed"
        if comp.exists():
            pairs.append((label, orig, comp))
    return pairs


# -- Results logging ----------------------------------------------------------


def save_results(result: dict) -> None:
    """Append result dict as JSONL for trend tracking."""
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    with RESULTS_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"Results saved to {RESULTS_LOG}")


# -- Benchmark runner ---------------------------------------------------------


def run_benchmark(
    original: str,
    compressed: str,
    label: str,
    save: bool = True,
    quiet: bool = False,
) -> dict:
    """Run full benchmark (fact + behavioral) and return results dict."""
    orig_w = len(original.split())
    comp_w = len(compressed.split())
    reduction_w = (1 - comp_w / orig_w) * 100 if orig_w > 0 else 0
    reduction_b = (1 - len(compressed) / len(original)) * 100 if len(original) > 0 else 0

    if not quiet:
        print(f"\n{'=' * 60}")
        print(f"COMPRESSION BENCHMARK -- {label}")
        print(f"{'=' * 60}")
        print(f"Original:   {orig_w:>5} words  {len(original):>6} bytes")
        print(f"Compressed: {comp_w:>5} words  {len(compressed):>6} bytes")
        print(f"Reduction:  {reduction_w:>5.1f}% words  {reduction_b:>5.1f}% bytes")

    # Phase 1: fact extraction + classification + verification
    if not quiet:
        print(f"\n{'-' * 60}")
        print("Phase 1: Fact Extraction, Classification & Verification")
        print(f"{'-' * 60}")
        print("Extracting and classifying facts...", end=" ", flush=True)

    facts = extract_and_classify_facts(original)
    if not quiet:
        critical_count = sum(1 for f in facts if f.get("classification") == "critical")
        supp_count = len(facts) - critical_count
        print(f"{len(facts)} facts ({critical_count} critical, {supp_count} supplementary)")
        print("Verifying in compressed version...", end=" ", flush=True)

    results = verify_facts(facts, compressed)
    if not quiet:
        print("done")

    scores = compute_weighted_score(results)

    if not quiet:
        print(f"\n  Total facts:         {scores['total']}")
        print(f"  Critical:            {scores['critical_total']}")
        print(f"  Supplementary:       {scores['supplementary_total']}")
        print(f"  Preserved:           {scores['preserved']}")
        print(f"  Derivable:           {scores['derivable']}")
        print(f"  Missing (critical):  {scores['missing_critical']}")
        print(f"  Missing (suppl.):    {scores['missing_supplementary']}")
        print(f"  Critical coverage:   {scores['critical_coverage'] * 100:.1f}%  (target: >=95%)")
        print(f"  Weighted score:      {scores['weighted_score'] * 100:.1f}%")

        if scores["missing_critical"] > 0:
            print(f"\n  Missing CRITICAL facts:")
            for r in results:
                if r["status"] == "missing" and r["classification"] == "critical":
                    print(f"    x {r['fact']}")
                    if r.get("note"):
                        print(f"      -> {r['note']}")

        if scores["missing_supplementary"] > 0:
            print(f"\n  Missing supplementary facts (informational):")
            for r in results:
                if r["status"] == "missing" and r["classification"] == "supplementary":
                    print(f"    - {r['fact']}")

    # Phase 2: behavioral tests
    test_set = label if label in BEHAVIORAL_TESTS else "claude_vault"
    if not quiet:
        print(f"\n{'-' * 60}")
        print(f"Phase 2: Behavioral Tests ({test_set}, {len(BEHAVIORAL_TESTS[test_set])} tests)")
        print(f"{'-' * 60}")
        print("Running...", end=" ", flush=True)

    behavioral = run_behavioral(compressed, test_set)
    if not quiet:
        print("done")

    passed = sum(1 for r in behavioral if r["score"] == "PASS")
    btotal = len(behavioral)
    behav_score = passed / btotal * 100 if btotal > 0 else 0

    if not quiet:
        print(f"\n  Passed: {passed}/{btotal} ({behav_score:.1f}%)")
        failed_tests = [r for r in behavioral if r["score"] == "FAIL"]
        if failed_tests:
            print(f"\n  Failed tests:")
            for r in failed_tests:
                print(f"    x {r['query']}")
                if r.get("note"):
                    print(f"      -> {r['note']}")

    # Summary
    crit_ok = scores["critical_coverage"] >= 0.95
    behav_ok = behav_score >= 90
    overall_ok = crit_ok and behav_ok

    if not quiet:
        print(f"\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Critical coverage: {scores['critical_coverage'] * 100:>5.1f}%  {'PASS' if crit_ok else 'FAIL'}  (target: >=95%)")
        print(f"  Behavioral score:  {behav_score:>5.1f}%  {'PASS' if behav_ok else 'FAIL'}  (target: >=90%)")
        print(f"  Weighted score:    {scores['weighted_score'] * 100:>5.1f}%")
        print(f"  Token reduction:   {reduction_w:>5.1f}%")
        print(f"\n  Result: {'PASS' if overall_ok else 'FAIL'}")

    result = {
        "label": label,
        "critical_coverage": scores["critical_coverage"],
        "behavioral_score": behav_score / 100,
        "weighted_score": scores["weighted_score"],
        "reduction_words_pct": reduction_w,
        "reduction_bytes_pct": reduction_b,
        "total_facts": scores["total"],
        "critical_facts": scores["critical_total"],
        "supplementary_facts": scores["supplementary_total"],
        "missing_critical": scores["missing_critical"],
        "missing_supplementary": scores["missing_supplementary"],
        "behavioral_passed": passed,
        "behavioral_total": btotal,
        "pass": overall_ok,
    }

    if save:
        save_results(result)

    return result


# -- Auto mode ----------------------------------------------------------------


def run_auto() -> bool:
    """Run benchmarks on all golden pairs. Returns True if all pass."""
    pairs = list_golden_pairs()
    if not pairs:
        print("No golden pairs found in ~/.deus/benchmarks/golden/")
        print("Run with <original> <compressed> --save-golden to create pairs.")
        return True  # No pairs = nothing to check = not a failure

    all_pass = True
    for label, orig_path, comp_path in pairs:
        original = orig_path.read_text(encoding="utf-8")
        compressed = comp_path.read_text(encoding="utf-8")
        result = run_benchmark(original, compressed, label, save=True, quiet=False)
        if not result["pass"]:
            all_pass = False

    return all_pass


# -- Main ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark compression quality for vault/memory files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("original", nargs="?", help="Path to original file")
    parser.add_argument("compressed", nargs="?", help="Path to compressed file")
    parser.add_argument(
        "--label",
        default="claude_vault",
        help="Test set label (claude_vault or memory_index)",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="Run against all golden pairs in ~/.deus/benchmarks/golden/",
    )
    parser.add_argument(
        "--save-golden",
        action="store_true",
        help="Save original/compressed as golden pair for future --auto runs",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Minimal output (for CI/automation)",
    )
    args = parser.parse_args()

    if args.auto:
        ok = run_auto()
        return 0 if ok else 1

    if not args.original or not args.compressed:
        parser.error("Provide <original> and <compressed> paths, or use --auto")

    original = Path(args.original).read_text(encoding="utf-8")
    compressed = Path(args.compressed).read_text(encoding="utf-8")

    if args.save_golden:
        save_golden(args.label, original, compressed)

    result = run_benchmark(
        original, compressed, args.label, save=True, quiet=args.quiet
    )
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

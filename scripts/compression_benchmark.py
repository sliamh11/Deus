#!/usr/bin/env python3
"""
Deus Compression Benchmark
Evaluates whether compressed vault files (CLAUDE.md, MEMORY.md) preserve
critical information from the originals.

Two-phase test:
  Phase 1 -- Fact extraction with classification:
    Extracts atomic facts from original, classifies each as critical vs
    supplementary, then verifies preservation in compressed version.
    Only critical facts count toward pass/fail.

  Phase 2 -- Behavioral tests (25-30 scenario queries):
    LLM answers task-relevant queries using ONLY the compressed doc.
    Answers scored against known-correct answers.

Weighted scoring:
  score = (preserved * 1.0 + derivable * 0.8 + missing_supp * 0.5) / total

Usage:
  # Manual: compare two files
  python3 scripts/compression_benchmark.py <original> <compressed> [--label NAME]

  # Automated: run against golden files
  python3 scripts/compression_benchmark.py --auto

  # Save current files as golden references
  python3 scripts/compression_benchmark.py --save-golden <original> <compressed> --label NAME
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
    print("pip install requests", file=sys.stderr)
    sys.exit(1)

# -- Paths -----------------------------------------------------------------

BENCHMARK_DIR = Path("~/.deus/benchmarks").expanduser()
GOLDEN_DIR = BENCHMARK_DIR / "golden"
RESULTS_LOG = BENCHMARK_DIR / "compression.jsonl"

# -- LLM helpers ------------------------------------------------------------


def llm_call(prompt: str, temp: float = 0.0) -> str:
    """Call Ollama locally -- no API key, no rate limits."""
    model = os.environ.get("BENCH_MODEL", "gemma4:e4b")
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate"
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
    return ""  # unreachable, satisfies type checker


def parse_json(text: str) -> list | dict:
    """Extract JSON from LLM response, handling code fences and trailing junk."""
    text = text.strip()
    # Strip code fences
    m = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    # Try parsing as-is first
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Try to find the outermost JSON array or object
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start = text.find(start_char)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == start_char:
                depth += 1
            elif c == end_char:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
    raise ValueError(f"Could not parse JSON from LLM response:\n{text[:500]}")


# -- Phase 1: Fact extraction + classification + verification ---------------


def extract_and_classify_facts(text: str) -> list[dict]:
    """Extract atomic facts and classify each as critical or supplementary."""
    prompt = f"""Extract every distinct factual claim from this configuration/memory document.
Each fact must be atomic (one piece of information) and verifiable.
Include: names, paths, values, settings, preferences, rules, constraints, relationships.
Exclude: formatting/style choices that don't carry semantic meaning.

For each fact, classify it:
- "critical": affects agent behavior, file matching, identity, security rules, or workflow requirements. These MUST be in any compressed version.
- "supplementary": implementation detail that lives in a linked file, is derivable from code/git, or is an example/illustration. OK to omit from compressed version.

Classification guidelines:
- Identity facts (user name, location, phone) = critical
- Behavioral rules ("never X", "always Y") = critical
- Security constraints = critical
- File paths to linked docs (the path itself, not the content) = supplementary
- Environment variable names derivable from code = supplementary
- Exact version numbers or line counts = supplementary
- Examples that illustrate a rule (when the rule itself is stated) = supplementary

Document:
---
{text}
---

Output ONLY a JSON array of objects: {{"fact": "...", "classification": "critical|supplementary"}}
No commentary before or after the JSON."""
    return parse_json(llm_call(prompt))


def verify_facts(
    facts: list[dict], compressed: str
) -> list[dict]:
    """Verify facts in batches against compressed text."""
    all_results = []
    batch_size = 20
    fact_strings = [f["fact"] for f in facts]
    classifications = {f["fact"]: f["classification"] for f in facts}

    for i in range(0, len(fact_strings), batch_size):
        batch = fact_strings[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (len(fact_strings) + batch_size - 1) // batch_size
        print(f"  batch {batch_num}/{total_batches}...", end=" ", flush=True)
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
{json.dumps(batch, indent=2)}

Output ONLY a JSON array of objects: {{"fact": "...", "status": "preserved|derivable|missing", "note": "..."}}"""
        batch_results = parse_json(llm_call(prompt))
        # Attach classification to each result
        for r in batch_results:
            r["classification"] = classifications.get(r.get("fact", ""), "supplementary")
        all_results.extend(batch_results)
    return all_results


def compute_weighted_score(results: list[dict]) -> dict:
    """Compute weighted coverage score with classification awareness.

    Formula: score = (preserved * 1.0 + derivable * 0.8 + missing_supp * 0.5) / total
    Critical missing facts score 0.0 (hard fail).
    """
    total = len(results)
    if total == 0:
        return {"score": 0.0, "details": {}}

    preserved = sum(1 for r in results if r["status"] == "preserved")
    derivable = sum(1 for r in results if r["status"] == "derivable")
    missing_critical = sum(
        1 for r in results
        if r["status"] == "missing" and r.get("classification") == "critical"
    )
    missing_supplementary = sum(
        1 for r in results
        if r["status"] == "missing" and r.get("classification") == "supplementary"
    )

    # Weighted score
    weighted = (
        preserved * 1.0
        + derivable * 0.8
        + missing_supplementary * 0.5
        + missing_critical * 0.0
    )
    score = weighted / total * 100

    # Critical-only coverage (what matters for pass/fail)
    critical_total = sum(
        1 for r in results if r.get("classification") == "critical"
    )
    critical_preserved = sum(
        1 for r in results
        if r.get("classification") == "critical" and r["status"] in ("preserved", "derivable")
    )
    critical_coverage = (critical_preserved / critical_total * 100) if critical_total else 100.0

    return {
        "score": score,
        "critical_coverage": critical_coverage,
        "preserved": preserved,
        "derivable": derivable,
        "missing_critical": missing_critical,
        "missing_supplementary": missing_supplementary,
        "critical_total": critical_total,
        "supplementary_total": total - critical_total,
        "total": total,
    }


# -- Phase 2: Behavioral tests ---------------------------------------------

BEHAVIORAL_TESTS: dict[str, list[tuple[str, str]]] = {
    "claude_vault": [
        # Identity
        ("What is the user's full name?", "Liam Steiner (Hebrew: \u05dc\u05d9\u05d0\u05dd \u05e9\u05d8\u05d9\u05d9\u05e0\u05e8)"),
        ("Where is the user located?", "Israel"),
        ("What is the user's educational background?", "OUI student (math + physics), ~5yr SWE (~3yr fullstack, ~1.5yr AWS team lead at Resilience Hub)"),
        # Architecture
        ("What is the basic system architecture?", "Single Node.js process with skill-based channel system, messages route to Claude Agent SDK in containers"),
        ("What channels are supported?", "WhatsApp, Telegram, Slack, Discord, Gmail"),
        # Eval / model
        ("What eval judge priority chain is used?", "Ollama(10) > Gemini(20) > Claude(30)"),
        ("What is the generative model fallback chain?", "gemini-3-flash -> gemini-2.5-flash -> gemini-2.5-flash-lite -> Ollama on 429"),
        ("What embedding model and dimension?", "gemini-embedding-2-preview, 768 dimensions"),
        ("What is the reflexion threshold?", "0.6"),
        # Display / rendering
        ("How must Hebrew text be rendered?", "Via LaTeX engine only; terminal BiDi rejected"),
        ("What is the display approach for images?", "Read tool inline in Claude Code; deus show for kitty in Ghostty; never open browser"),
        # Design principles
        ("What are the design principles?", "machine-adaptive, token-efficient, secure-by-default, performance-aware, no-db-deletion"),
        # Memory
        ("What is the memory startup loading sequence?", "CLAUDE.md always + warm (recent-days 3) + learnings + cold (query top 2 recency-boost)"),
        # Trading
        ("What stocks and instruments does the user trade?", "US stocks via TradingView -> IBKR IL; no direct crypto; ETFs: $ETHA, $IBIT; crypto stocks: $COIN, $HOOD, $BLSH, $BMNR"),
        # Provider layers
        ("How many ABC+Registry provider layers exist?", "4: judge, generative, storage, auth"),
        # Task routing / debugging
        ("I'm debugging a third-party library issue. What should I do first?", "grep lib internals before workarounds (library-source-first pattern)"),
        # Security
        ("Can I commit .env files or credentials?", "No -- audit security before commit; never commit secrets"),
        # Workflow
        ("How should I create a feature branch?", "Use git worktree add, never checkout in main repo"),
        ("What's the dev workflow sequence?", "plan -> branch -> implement -> test -> commit -> merge"),
        ("What must happen before committing code?", "Show commit msg and wait for explicit approval"),
        # Courses
        ("What courses is the user taking?", "S215 (Classical Mechanics + SR), Linear Algebra 1, Calculus"),
        # Terminal
        ("What is the Ghostty terminal resolution?", "Retina 2x, 3448x2088 physical for 43x156 terminal"),
        # Skills
        ("Are skills like /setup chat commands?", "No -- they run in Claude Code on the host machine, never suggest to users via chat"),
        # ADR requirement
        ("What must be done before changing eval or scripts/memory_indexer.py?", "Read docs/decisions/INDEX.md in full"),
        # MCP
        ("What MCP config files exist?", "~/.claude.json for CLI, ~/.claude/mcp.json for Desktop app"),
    ],
    "memory_index": [
        # Workflow rules
        ("Should I merge a PR with failing CI tests?", "Never -- fix first (feedback_no_merge_failed_tests)"),
        ("How should background tasks be handled?", "Go to background immediately, no waiting (feedback_background_tasks)"),
        ("Should git checkout be used for feature branches?", "No -- use git worktree add (feedback_worktree_workflow)"),
        ("What happens before committing?", "Show commit msg + wait for explicit approval (feedback_commit_preview, feedback_wait_for_approval)"),
        ("What's the dev workflow?", "plan -> branch -> implement -> test -> commit -> merge (feedback_dev_workflow)"),
        # Security
        ("Can secrets be committed to git?", "No -- audit security before commit (feedback_security_first)"),
        ("Can I push directly to main?", "Only emergency with --admin flag (feedback_admin_bypass)"),
        # Image / rendering
        ("How should images be analyzed?", "Send to Gemini first, never analyze directly (feedback_image_analysis)"),
        ("How should math be rendered?", "Unicode math inline; SymPy compute only (feedback_math_rendering)"),
        ("How to render Hebrew + math?", "XeLaTeX template with fonts, magick (feedback_hebrew_math_html)"),
        # Data integrity
        ("What's the rule about data integrity?", "Never lose, overwrite, or downgrade data; merge not replace (feedback_data_integrity)"),
        # Personal / local
        ("What about personal-account skills like X/Gmail?", "Local-only, never committed (feedback_local_only_skills)"),
        ("How to classify features before implementing?", "Classify as public or private first (feedback_public_vs_private)"),
        # Identity / persona
        ("Where to find user preferences and personality?", "Persona vault at ~/Desktop/... /Deus/Persona/INDEX.md"),
        ("Who is Eden?", "Eden is a friend and future roommate (Aug 2026), works with Qlik Sense"),
        # Monitoring
        ("What's the rule about background task monitoring?", "Check every 2-3 min; report status proactively (feedback_monitor_background + feedback_monitor_self)"),
        # Research
        ("Where should research be saved?", "Save to Deus/Research/ with tags (feedback_research_vault)"),
        # Debugging
        ("What's the debugging methodology?", "Pipeline-first, grep consumers, reverse-verify (feedback_debugging_methodology)"),
        # Optimization
        ("What about eliminating redundant steps?", "Proactive optimization, eliminate redundancy (feedback_proactive_optimization)"),
        # Deploy
        ("What must happen before restarting a deploy?", "Rebuild dist/ before restart; no rotating creds in .env (feedback_deploy_integrity)"),
        # Model tiers
        ("Which models for which tasks?", "Sonnet/Haiku for subagents, Opus for complex tasks (feedback_model_tiers)"),
        # Negative test
        ("Is there a memory about Docker container orchestration?", "No specific memory about Docker orchestration exists"),
        # Negative test 2
        ("Is there a memory about Kubernetes?", "No -- there is no Kubernetes-related memory"),
        # Cross-reference
        ("What files relate to deploy safety?", "feedback_deploy_integrity, feedback_security_first, feedback_admin_bypass"),
        # Habit
        ("What self-improvement frameworks are referenced?", "James Clear + Huberman habits (feedback_habit_stacking)"),
    ],
}


def run_behavioral(compressed: str, test_set: str) -> list[dict]:
    """Run behavioral tests: LLM answers using only compressed doc, then score."""
    tests = BEHAVIORAL_TESTS.get(test_set, BEHAVIORAL_TESTS["claude_vault"])

    # Ask all questions in one call
    prompt = f"""Answer each question using ONLY the document below. Be precise and specific.
If the document does not contain information to answer a question, say "not found in document".

Document:
---
{compressed}
---

Questions:
{json.dumps([q for q, _ in tests], indent=2)}

Output ONLY a JSON array of objects: {{"query": "...", "answer": "..."}}"""
    answers = parse_json(llm_call(prompt))

    # Build scoring pairs
    pairs = []
    for idx, (q, expected) in enumerate(tests):
        actual = ""
        if idx < len(answers):
            actual = answers[idx].get("answer", "") if isinstance(answers[idx], dict) else ""
        pairs.append({"query": q, "expected": expected, "actual": actual})

    # Score
    score_prompt = f"""Score each pair. PASS = actual answer conveys the same core information as expected (abbreviations OK, exact wording not required, partial overlap is OK if the key point is present). FAIL = wrong, incomplete, or missing information.

For negative tests (where expected says "No" or "no specific memory"), PASS = the actual answer correctly indicates absence. FAIL = fabricated or wrong information.

Pairs:
{json.dumps(pairs, indent=2)}

Output ONLY a JSON array of objects: {{"query": "...", "score": "PASS|FAIL", "note": "..."}}"""
    return parse_json(llm_call(score_prompt))


# -- Golden file management -------------------------------------------------


def save_golden(original_path: str, compressed_path: str, label: str) -> None:
    """Save an original/compressed pair as golden reference files."""
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    original = Path(original_path).read_text(encoding="utf-8")
    compressed = Path(compressed_path).read_text(encoding="utf-8")

    (GOLDEN_DIR / f"{label}.original").write_text(original, encoding="utf-8")
    (GOLDEN_DIR / f"{label}.compressed").write_text(compressed, encoding="utf-8")

    meta = {
        "label": label,
        "original_path": str(Path(original_path).resolve()),
        "compressed_path": str(Path(compressed_path).resolve()),
        "original_words": len(original.split()),
        "compressed_words": len(compressed.split()),
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    (GOLDEN_DIR / f"{label}.meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    print(f"Saved golden files for '{label}' to {GOLDEN_DIR}")


def load_golden_pairs() -> list[dict]:
    """Load all golden original/compressed pairs."""
    if not GOLDEN_DIR.exists():
        return []
    pairs = []
    for meta_file in sorted(GOLDEN_DIR.glob("*.meta.json")):
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        label = meta["label"]
        orig_file = GOLDEN_DIR / f"{label}.original"
        comp_file = GOLDEN_DIR / f"{label}.compressed"
        if orig_file.exists() and comp_file.exists():
            pairs.append({
                "label": label,
                "original": orig_file.read_text(encoding="utf-8"),
                "compressed": comp_file.read_text(encoding="utf-8"),
                "meta": meta,
            })
    return pairs


# -- Result logging ---------------------------------------------------------


def save_result(result: dict) -> None:
    """Append a result record to the JSONL log."""
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **result,
    }
    with RESULTS_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")


# -- Vault integrity check --------------------------------------------------


def check_vault_integrity(vault_path: Path | None = None) -> dict:
    """Check vault memory files for basic structural integrity.

    Validates:
    - All .md files in memory dir have valid YAML-ish frontmatter (if present)
    - Links in MEMORY.md point to existing files
    - No orphaned files (in memory dir but not referenced in MEMORY.md)
    """
    if vault_path is None:
        # Try common locations
        for candidate in [
            Path("~/.claude/projects").expanduser(),
            Path("~/.deus").expanduser(),
        ]:
            if candidate.exists():
                vault_path = candidate
                break

    if vault_path is None:
        return {"status": "skip", "reason": "no vault path found"}

    # Find memory dirs
    issues: list[str] = []
    checked = 0

    # Check MEMORY.md links if it exists
    memory_candidates = list(vault_path.rglob("MEMORY.md"))
    for memory_md in memory_candidates:
        memory_dir = memory_md.parent
        content = memory_md.read_text(encoding="utf-8")

        # Extract markdown links like [text](filename.md)
        links = re.findall(r"\[.*?\]\(([\w/.-]+\.md)\)", content)
        for link in links:
            target = memory_dir / link
            if not target.exists():
                issues.append(f"broken link in {memory_md}: {link}")
            checked += 1

        # Check for .md files in same dir not referenced in MEMORY.md
        md_files = {
            f.name for f in memory_dir.glob("*.md")
            if f.name != "MEMORY.md" and not f.name.startswith(".")
        }
        referenced = {Path(l).name for l in links if not "/" in l}
        orphaned = md_files - referenced
        for orphan in sorted(orphaned):
            # Not a hard failure, just informational
            issues.append(f"unreferenced file: {memory_dir / orphan}")

    return {
        "status": "pass" if not any("broken" in i for i in issues) else "fail",
        "links_checked": checked,
        "issues": issues,
        "broken_links": [i for i in issues if "broken" in i],
        "unreferenced": [i for i in issues if "unreferenced" in i],
    }


# -- Main benchmark runner --------------------------------------------------


def run_benchmark(
    original: str,
    compressed: str,
    label: str,
    quiet: bool = False,
) -> dict:
    """Run the full two-phase benchmark. Returns result dict."""
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

    # Phase 1: Fact extraction + classification + verification
    if not quiet:
        print(f"\n{'-' * 60}")
        print("Phase 1: Fact Extraction + Classification + Verification")
        print(f"{'-' * 60}")
        print("Extracting and classifying facts...", end=" ", flush=True)

    facts = extract_and_classify_facts(original)
    critical_count = sum(1 for f in facts if f.get("classification") == "critical")
    supp_count = len(facts) - critical_count

    if not quiet:
        print(f"{len(facts)} facts ({critical_count} critical, {supp_count} supplementary)")
        print("Verifying in compressed version... ", flush=True)

    results = verify_facts(facts, compressed)

    if not quiet:
        print("done")

    scores = compute_weighted_score(results)

    if not quiet:
        print(f"\n  Total facts:    {scores['total']}")
        print(f"  Critical:       {scores['critical_total']} ({scores['critical_coverage']:.1f}% coverage)")
        print(f"    Preserved:    {scores['preserved']}")
        print(f"    Derivable:    {scores['derivable']}")
        print(f"    Missing:      {scores['missing_critical']} critical, {scores['missing_supplementary']} supplementary")
        print(f"  Weighted score: {scores['score']:.1f}%")

        if scores["missing_critical"] > 0:
            print(f"\n  CRITICAL missing facts:")
            for r in results:
                if r["status"] == "missing" and r.get("classification") == "critical":
                    print(f"    x {r['fact']}")
                    if r.get("note"):
                        print(f"      -> {r['note']}")

        if scores["missing_supplementary"] > 0:
            print(f"\n  Supplementary missing facts (informational):")
            for r in results:
                if r["status"] == "missing" and r.get("classification") == "supplementary":
                    print(f"    - {r['fact']}")

    # Phase 2: Behavioral tests
    # Map label to test set
    test_set = label
    if test_set not in BEHAVIORAL_TESTS:
        # Try to guess: if label contains "memory" use memory_index, else claude_vault
        test_set = "memory_index" if "memory" in label.lower() else "claude_vault"

    if not quiet:
        print(f"\n{'-' * 60}")
        print(f"Phase 2: Behavioral Tests ({len(BEHAVIORAL_TESTS.get(test_set, []))} tests)")
        print(f"{'-' * 60}")
        print("Running...", end=" ", flush=True)

    behavioral = run_behavioral(compressed, test_set)

    if not quiet:
        print("done")

    passed = sum(1 for r in behavioral if r.get("score") == "PASS")
    failed = sum(1 for r in behavioral if r.get("score") == "FAIL")
    btotal = len(behavioral)
    behav_score = (passed / btotal * 100) if btotal > 0 else 0

    if not quiet:
        print(f"\n  Passed: {passed}/{btotal} ({behav_score:.1f}%)")
        if failed > 0:
            print(f"\n  Failed tests:")
            for r in behavioral:
                if r.get("score") == "FAIL":
                    print(f"    x {r.get('query', '?')}")
                    if r.get("note"):
                        print(f"      -> {r['note']}")

    # Pass/fail determination
    critical_ok = scores["critical_coverage"] >= 95.0
    behavioral_ok = behav_score >= 90.0
    overall_ok = critical_ok and behavioral_ok

    if not quiet:
        print(f"\n{'=' * 60}")
        print("SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Critical coverage: {scores['critical_coverage']:>5.1f}%  (target: >=95%)", end="")
        print(f"  {'PASS' if critical_ok else 'FAIL'}")
        print(f"  Weighted score:    {scores['score']:>5.1f}%")
        print(f"  Behavioral score:  {behav_score:>5.1f}%  (target: >=90%)", end="")
        print(f"  {'PASS' if behavioral_ok else 'FAIL'}")
        print(f"  Token reduction:   {reduction_w:>5.1f}%")
        print(f"\n  Result: {'PASS' if overall_ok else 'FAIL'}")

    return {
        "label": label,
        "original_words": orig_w,
        "compressed_words": comp_w,
        "reduction_pct": round(reduction_w, 1),
        "facts_total": scores["total"],
        "facts_critical": scores["critical_total"],
        "critical_coverage": round(scores["critical_coverage"], 1),
        "weighted_score": round(scores["score"], 1),
        "behavioral_passed": passed,
        "behavioral_total": btotal,
        "behavioral_score": round(behav_score, 1),
        "missing_critical_facts": [
            r["fact"] for r in results
            if r["status"] == "missing" and r.get("classification") == "critical"
        ],
        "failed_behavioral": [
            r.get("query", "?") for r in behavioral if r.get("score") == "FAIL"
        ],
        "pass": overall_ok,
    }


# -- Auto mode --------------------------------------------------------------


def run_auto() -> int:
    """Run benchmark against all stored golden pairs. Returns exit code."""
    pairs = load_golden_pairs()
    if not pairs:
        print("No golden files found. Use --save-golden to create them first.")
        print(f"Expected location: {GOLDEN_DIR}")
        return 1

    all_pass = True
    for pair in pairs:
        result = run_benchmark(
            pair["original"],
            pair["compressed"],
            pair["label"],
        )
        save_result(result)
        if not result["pass"]:
            all_pass = False

    if all_pass:
        print(f"\nAll {len(pairs)} benchmark(s) PASSED")
    else:
        print(f"\nSome benchmarks FAILED -- review output above")

    return 0 if all_pass else 1


# -- CLI --------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deus compression benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "original", nargs="?", help="Path to original (uncompressed) file"
    )
    parser.add_argument(
        "compressed", nargs="?", help="Path to compressed file"
    )
    parser.add_argument(
        "--label", default="claude_vault",
        help="Test set label: claude_vault or memory_index (default: claude_vault)"
    )
    parser.add_argument(
        "--auto", action="store_true",
        help="Run against stored golden files (for maintenance integration)"
    )
    parser.add_argument(
        "--save-golden", action="store_true",
        help="Save original/compressed as golden reference pair"
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Save results to ~/.deus/benchmarks/compression.jsonl"
    )
    parser.add_argument(
        "--vault-integrity", action="store_true",
        help="Run vault integrity check only"
    )
    args = parser.parse_args()

    # Vault integrity check mode
    if args.vault_integrity:
        # Find the user's memory directory
        memory_dir = None
        for candidate in [
            Path("~/.claude/projects").expanduser(),
        ]:
            if candidate.exists():
                memory_dir = candidate
                break
        result = check_vault_integrity(memory_dir)
        if result["status"] == "skip":
            print(f"Skipped: {result['reason']}")
            return 0
        broken = result.get("broken_links", [])
        unreferenced = result.get("unreferenced", [])
        print(f"Vault integrity: {result['status'].upper()}")
        print(f"  Links checked: {result['links_checked']}")
        if broken:
            print(f"  Broken links ({len(broken)}):")
            for b in broken:
                print(f"    x {b}")
        if unreferenced:
            print(f"  Unreferenced files ({len(unreferenced)}):")
            for u in unreferenced[:10]:
                print(f"    - {u}")
        return 0 if result["status"] == "pass" else 1

    # Save golden mode
    if args.save_golden:
        if not args.original or not args.compressed:
            parser.error("--save-golden requires <original> and <compressed> arguments")
        save_golden(args.original, args.compressed, args.label)
        return 0

    # Auto mode
    if args.auto:
        return run_auto()

    # Manual mode
    if not args.original or not args.compressed:
        parser.error("Provide <original> and <compressed> paths, or use --auto")

    original = Path(args.original).read_text(encoding="utf-8")
    compressed = Path(args.compressed).read_text(encoding="utf-8")

    result = run_benchmark(original, compressed, args.label)

    if args.save:
        save_result(result)
        print(f"\nResults saved to {RESULTS_LOG}")

    return 0 if result["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Deterministic keyword-preservation check.

For each curated fact, derive its "key signal" tokens (proper nouns, paths,
commands, identifiers), then test whether those tokens appear in the
compressed file. This is a conservative baseline — a fact can still be
preserved in paraphrased form without matching keywords. False negatives
here flag facts worth a human-audit eyeball.

Usage:
    python3 keyword_bench.py --label <label> --compressed <path> --facts <path>

Fact file format:
    CRITICAL: <fact>   # keywords auto-extracted
    SUPP: <fact>
    # keyword overrides can be added inline:
    CRITICAL: <fact>   # kw=token1,token2
"""

from __future__ import annotations
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

STOPWORDS = set("""
a an and are as at be been being by can could do does done for from has had
have how i if in into is it its may must not of on or our should so such that
the their them then there they this those to via was were what when where
which while who will with you your these use used using uses each any all
only just also more most other both same other than over under one two three
first last before after during while new old tasks including include includes
appear appears look looks handle handles sent goes send run runs command
commands line lines word words file files user users text texts answer
answers any some many few few's
""".split())

CODE_RE = re.compile(r"`([^`]+)`")
PATH_RE = re.compile(r"[/\w][\w./\-]*\.(py|md|ts|json|template|js|mjs|sh)\b")
SLASH_CMD_RE = re.compile(r"(?<![\w/])/[a-z][a-z0-9_-]+")
MCP_RE = re.compile(r"mcp__[a-z_]+")
BIN_RE = re.compile(r"\b(claude|node|python3|npm|git|bash|curl|gcal)\b")


def keywords(fact: str) -> list[str]:
    # Honor inline override
    if "# kw=" in fact:
        body, kw = fact.split("# kw=", 1)
        return [t.strip() for t in kw.split(",") if t.strip()]

    out: set[str] = set()
    # Code spans
    for m in CODE_RE.findall(fact):
        out.add(m.lower())
    # Paths
    for m in PATH_RE.findall(fact):
        out.add(m.lower())
    # Slash commands
    for m in SLASH_CMD_RE.findall(fact):
        out.add(m.lower())
    # MCP tool names
    for m in MCP_RE.findall(fact):
        out.add(m.lower())
    # Binaries / tool names
    for m in BIN_RE.findall(fact):
        out.add(m.lower())
    # Proper nouns (CamelCase or ALLCAPS words ≥ 3 chars, excluding "sdk" etc.)
    for token in re.findall(r"\b[A-Z][a-zA-Z0-9]{2,}\b", fact):
        out.add(token.lower())
    # Multi-word capitalized phrases (Linear Algebra, Gmail, Telegram…)
    for token in re.findall(r"\b[A-Z][a-z]+\b", fact):
        if token.lower() not in STOPWORDS:
            out.add(token.lower())

    if not out:
        # Fallback: take the 3 longest non-stopword words
        words = [w.lower() for w in re.findall(r"\b[\w-]+\b", fact) if len(w) > 4 and w.lower() not in STOPWORDS]
        words.sort(key=len, reverse=True)
        out = set(words[:3])
    return sorted(out)


def parse_facts(path: Path) -> list[dict]:
    facts: list[dict] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("CRITICAL:"):
            body = line[len("CRITICAL:"):].strip()
            facts.append({"fact": body, "class": "critical", "keywords": keywords(body)})
        elif line.upper().startswith("SUPP:"):
            body = line[len("SUPP:"):].strip()
            facts.append({"fact": body, "class": "supplementary", "keywords": keywords(body)})
    return facts


def check_fact(keywords_list: list[str], compressed: str) -> bool:
    if not keywords_list:
        return True  # no anchor — can't disprove
    lc = compressed.lower()
    return all(kw in lc for kw in keywords_list)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--compressed", required=True)
    ap.add_argument("--facts", required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    compressed = Path(args.compressed).read_text()
    facts = parse_facts(Path(args.facts))

    print(f"=== keyword bench: {args.label} ===")
    print(f"Compressed: {Path(args.compressed).name} ({len(compressed)} chars)")
    print(f"Facts: {len(facts)} total")
    print()

    results = []
    for i, f in enumerate(facts, 1):
        preserved = check_fact(f["keywords"], compressed) if f["keywords"] else False
        kw_str = ",".join(f["keywords"][:4]) or "(none)"
        marker = "!" if f["class"] == "critical" and not preserved else " "
        status = "PASS" if preserved else "MISS"
        print(f"  [{i:>2}] {status} {marker} [{f['class'][:4]}] {f['fact'][:55]} ← kw=[{kw_str}]")
        results.append({**f, "preserved": preserved})

    crit_total = sum(1 for r in results if r["class"] == "critical")
    crit_preserved = sum(1 for r in results if r["class"] == "critical" and r["preserved"])
    supp_total = sum(1 for r in results if r["class"] == "supplementary")
    supp_preserved = sum(1 for r in results if r["class"] == "supplementary" and r["preserved"])

    crit_pct = (crit_preserved / crit_total * 100) if crit_total else 100.0
    total = len(results)
    overall_pct = ((crit_preserved + supp_preserved) / total * 100) if total else 100.0

    print()
    print(f"Critical coverage: {crit_preserved}/{crit_total} = {crit_pct:.1f}%")
    print(f"Overall coverage:  {crit_preserved + supp_preserved}/{total} = {overall_pct:.1f}%")
    verdict = "PASS" if crit_pct >= 95.0 else "REVIEW"
    print(f"Verdict: {verdict}  (MISS items deserve a manual eyeball — keyword-only test can false-negative on paraphrase)")

    report = {
        "label": args.label,
        "compressed_path": str(Path(args.compressed).resolve()),
        "facts_file": str(Path(args.facts).resolve()),
        "method": "keyword-match",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "critical_total": crit_total,
        "critical_preserved": crit_preserved,
        "critical_coverage_pct": round(crit_pct, 1),
        "overall_coverage_pct": round(overall_pct, 1),
        "verdict": verdict,
        "missing_critical": [
            {"fact": r["fact"], "keywords": r["keywords"]}
            for r in results if r["class"] == "critical" and not r["preserved"]
        ],
        "missing_supplementary": [
            {"fact": r["fact"], "keywords": r["keywords"]}
            for r in results if r["class"] == "supplementary" and not r["preserved"]
        ],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
        print(f"Wrote {args.out}")
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())

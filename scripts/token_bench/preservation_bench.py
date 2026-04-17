#!/usr/bin/env python3
"""Robust compression-preservation check.

For each (original, compressed) file pair plus a curated fact list, ask an
Ollama model ONE binary question per fact:
    "Does the document below convey this information? Answer only YES or NO.
     Information: <fact>
     Document:
     ---
     <compressed>
     ---"
Count preserved vs missing. This replaces compression_benchmark.py's brittle
structured-JSON extract with single-token answers — no parser failures.

Usage:
    python3 preservation_bench.py --label <label> --compressed <path> \\
                                  --facts <fact-file.txt>

Fact file format:
    # lines starting with "#" are comments
    CRITICAL: <fact text>
    SUPP: <fact text>
"""

from __future__ import annotations
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("pip3 install requests --break-system-packages")


def ollama_ask(question: str, model: str) -> str:
    url = os.environ.get("OLLAMA_URL", "http://localhost:11434") + "/api/generate"
    body = {
        "model": model,
        "prompt": question,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 256, "think": False},
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=body, timeout=120)
            r.raise_for_status()
            return r.json().get("response", "").strip().upper()
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Ollama failed: {e}") from None
            time.sleep(2 ** attempt)
    return ""


def parse_fact_file(path: Path) -> list[dict]:
    facts: list[dict] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.upper().startswith("CRITICAL:"):
            facts.append({"fact": line[len("CRITICAL:"):].strip(), "class": "critical"})
        elif line.upper().startswith("SUPP:"):
            facts.append({"fact": line[len("SUPP:"):].strip(), "class": "supplementary"})
    return facts


def check_fact(fact: str, document: str, model: str) -> bool:
    q = f"""Does the document below convey this specific information? Answer with a single word: YES or NO.

Information: {fact}

Document:
---
{document}
---

Answer (YES or NO):"""
    ans = ollama_ask(q, model)
    return ans.startswith("YES")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--compressed", required=True)
    ap.add_argument("--facts", required=True)
    ap.add_argument("--model", default=os.environ.get("BENCH_MODEL", "gemma4:e4b"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    compressed = Path(args.compressed).read_text()
    facts = parse_fact_file(Path(args.facts))
    if not facts:
        print(f"No facts loaded from {args.facts}", file=sys.stderr)
        return 1

    print(f"=== {args.label} ===")
    print(f"Compressed: {Path(args.compressed).name} ({len(compressed)} chars)")
    print(f"Facts: {len(facts)} total ({sum(1 for f in facts if f['class']=='critical')} critical)")
    print(f"Model: {args.model}")
    print()

    results = []
    for i, f in enumerate(facts, 1):
        preserved = check_fact(f["fact"], compressed, args.model)
        status = "PASS" if preserved else "MISS"
        marker = "!" if f["class"] == "critical" and not preserved else " "
        print(f"  [{i:>2}] {status} {marker} [{f['class'][:4]}] {f['fact'][:70]}")
        results.append({**f, "preserved": preserved})

    total = len(results)
    crit_total = sum(1 for r in results if r["class"] == "critical")
    crit_preserved = sum(1 for r in results if r["class"] == "critical" and r["preserved"])
    supp_total = total - crit_total
    supp_preserved = sum(1 for r in results if r["class"] == "supplementary" and r["preserved"])

    crit_pct = (crit_preserved / crit_total * 100) if crit_total else 100.0
    supp_pct = (supp_preserved / supp_total * 100) if supp_total else 100.0
    overall_pct = (
        (crit_preserved + supp_preserved) / total * 100 if total else 100.0
    )

    print()
    print(f"Critical coverage: {crit_preserved}/{crit_total} = {crit_pct:.1f}%")
    print(f"Supplementary coverage: {supp_preserved}/{supp_total} = {supp_pct:.1f}%")
    print(f"Overall coverage: {crit_preserved + supp_preserved}/{total} = {overall_pct:.1f}%")
    verdict = "PASS" if crit_pct >= 95.0 else "FAIL"
    print(f"Verdict: {verdict} (critical ≥ 95.0% required)")

    report = {
        "label": args.label,
        "compressed_path": str(Path(args.compressed).resolve()),
        "facts_file": str(Path(args.facts).resolve()),
        "model": args.model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "facts_total": total,
        "critical_total": crit_total,
        "critical_preserved": crit_preserved,
        "critical_coverage_pct": round(crit_pct, 1),
        "supplementary_total": supp_total,
        "supplementary_preserved": supp_preserved,
        "supplementary_coverage_pct": round(supp_pct, 1),
        "overall_coverage_pct": round(overall_pct, 1),
        "verdict": verdict,
        "missing_critical": [r["fact"] for r in results if r["class"] == "critical" and not r["preserved"]],
        "missing_supplementary": [r["fact"] for r in results if r["class"] == "supplementary" and not r["preserved"]],
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2) + "\n")
        print(f"Wrote {args.out}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())

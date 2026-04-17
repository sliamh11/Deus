#!/usr/bin/env python3
"""Aggregate compression_benchmark logs into a single results table."""

from __future__ import annotations
import json
import re
import sys
from pathlib import Path

RESULT_DIR = Path(__file__).resolve().parent / "compression_results"


def parse_log(log_path: Path) -> dict:
    text = log_path.read_text()
    out: dict[str, object] = {"label": log_path.stem}
    for pattern, key, conv in [
        (r"Original:\s+(\d+)\s+words", "orig_words", int),
        (r"Compressed:\s+(\d+)\s+words", "comp_words", int),
        (r"Reduction:\s+([\d.]+)%\s+words", "word_reduction_pct", float),
        (r"Critical coverage:\s+([\d.]+)%", "critical_coverage_pct", float),
        (r"Weighted score:\s+([\d.]+)%", "weighted_score_pct", float),
        (r"Behavioral score:\s+([\d.]+)%", "behavioral_score_pct", float),
        (r"Total facts:\s+(\d+)", "facts_total", int),
        (r"Critical:\s+(\d+)\s*\(", "facts_critical", int),
    ]:
        m = re.search(pattern, text)
        if m:
            out[key] = conv(m.group(1))
    if "Result: PASS" in text:
        out["overall"] = "PASS"
    elif "Result: FAIL" in text:
        out["overall"] = "FAIL"
    elif "Traceback" in text:
        out["overall"] = "ERROR"
    else:
        out["overall"] = "INCOMPLETE"

    critical_missing: list[str] = []
    in_block = False
    for line in text.splitlines():
        if "CRITICAL missing facts:" in line:
            in_block = True
            continue
        if in_block:
            if line.startswith("    x "):
                critical_missing.append(line[6:].strip())
            elif line.strip() == "" or not line.startswith(" "):
                in_block = False
    out["missing_critical"] = critical_missing
    return out


def main() -> int:
    logs = sorted(RESULT_DIR.glob("*.log"))
    if not logs:
        print(f"No logs in {RESULT_DIR}", file=sys.stderr)
        return 1
    rows = [parse_log(p) for p in logs]
    print(json.dumps(rows, indent=2))

    print()
    print(f"{'label':<20} {'orig_w':>7} {'comp_w':>7} {'red%':>6}  {'crit%':>6}  {'beh%':>6}  verdict")
    print("-" * 80)
    for r in rows:
        label = r.get("label", "?")
        ow = r.get("orig_words", "?")
        cw = r.get("comp_words", "?")
        red = r.get("word_reduction_pct", "?")
        crit = r.get("critical_coverage_pct", "?")
        beh = r.get("behavioral_score_pct", "?")
        ver = r.get("overall", "?")
        red_s = f"{red:.1f}" if isinstance(red, float) else str(red)
        crit_s = f"{crit:.1f}" if isinstance(crit, float) else str(crit)
        beh_s = f"{beh:.1f}" if isinstance(beh, float) else str(beh)
        print(f"{label:<20} {ow:>7} {cw:>7} {red_s:>6}  {crit_s:>6}  {beh_s:>6}  {ver}")
        for mc in r.get("missing_critical", []) or []:
            print(f"  critical-missing: {mc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

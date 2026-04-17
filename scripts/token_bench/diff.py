#!/usr/bin/env python3
"""Compare two harness snapshots and print savings."""

from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("before", help="path to baseline snapshot json")
    p.add_argument("after", help="path to post-change snapshot json")
    args = p.parse_args()

    before = json.loads(Path(args.before).read_text())
    after = json.loads(Path(args.after).read_text())

    print(f"Comparing {before['label']} → {after['label']}")
    print()
    print("Per-file delta (chars):")
    all_keys = sorted(set(before["files"]) | set(after["files"]))
    for k in all_keys:
        b = before["files"].get(k, {}).get("chars", 0)
        a = after["files"].get(k, {}).get("chars", 0)
        if b == a:
            continue
        delta = a - b
        pct = (delta / b * 100) if b else 0.0
        sign = "-" if delta < 0 else "+"
        print(f"  {k:<30} {b:>6} → {a:>6}  ({sign}{abs(delta):>5}, {pct:+.1f}%)")

    print()
    print("Per-scenario delta (est. tokens at turn 1):")
    for name in sorted(set(before["scenarios"]) | set(after["scenarios"])):
        b = before["scenarios"].get(name, {}).get("est_tokens", 0)
        a = after["scenarios"].get(name, {}).get("est_tokens", 0)
        delta = a - b
        pct = (delta / b * 100) if b else 0.0
        sign = "-" if delta < 0 else "+"
        print(f"  {name:<40} {b:>5} → {a:>5} tok  ({sign}{abs(delta):>4}, {pct:+.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

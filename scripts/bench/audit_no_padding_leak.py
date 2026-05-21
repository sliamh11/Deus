#!/usr/bin/env python3
"""Pre-commit audit: scan for M1c padding-atom leaks into staged outputs.

For every file in the padding dir, check whether its filename OR its body
prose prefix (first 100 bytes after the YAML frontmatter divider) appears
in any of the --check paths (results JSON, cache files, staged diff, etc.).

Exit 0 if clean, non-zero on any leak. Designed to run as a pre-commit
gate before opening the M1c v2 PR.

Usage:
  python3 scripts/bench/audit_no_padding_leak.py \\
      --padding-dir /tmp/m1c_padding \\
      --check scripts/bench/results/ \\
              scripts/bench/attention_dilution_pairwise_cache.json \\
              scripts/bench/attention_dilution_recall_cache.json
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_THIS_DIR))
from attention_dilution_probe import _scan_for_padding_leaks  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--padding-dir",
        required=True,
        help="Directory of padding atoms to audit.",
    )
    parser.add_argument(
        "--check",
        nargs="+",
        required=True,
        help="Files or dirs to scan for padding-content leaks.",
    )
    args = parser.parse_args(argv)

    padding_dir = Path(args.padding_dir).expanduser()
    check_paths = [Path(p).expanduser() for p in args.check]

    if not padding_dir.exists():
        print(
            f"[audit] padding dir not found: {padding_dir} — nothing to audit, "
            "exiting clean.",
            file=sys.stderr,
        )
        return 0

    leaks = _scan_for_padding_leaks(padding_dir, check_paths)

    if not leaks:
        print(
            f"[audit] CLEAN: no padding leaks across {len(check_paths)} check paths.",
            file=sys.stderr,
        )
        return 0

    print(
        f"[audit] LEAK DETECTED ({len(leaks)} hits):", file=sys.stderr
    )
    for leak in leaks:
        print(f"  - {leak}", file=sys.stderr)
    print(
        "[audit] Redact the offending content before committing.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

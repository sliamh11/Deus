#!/usr/bin/env python3
"""Import curated seed reflections into the evolution database.

Usage:
    # Import all seeds from the default file:
    python3 scripts/import_seeds.py

    # Import from a custom file:
    python3 scripts/import_seeds.py path/to/seeds.json

    # Import a specific subset (JSON array passed directly):
    python3 scripts/import_seeds.py --seeds '[{"content": "...", ...}]'
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_SEEDS_FILE = ROOT / "seeds" / "reflections.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Import seed reflections into the evolution DB")
    parser.add_argument(
        "seeds_file",
        nargs="?",
        default=str(DEFAULT_SEEDS_FILE),
        help="Path to a JSON file containing an array of seed objects",
    )
    parser.add_argument(
        "--seeds",
        metavar="JSON",
        help="JSON array of seed objects (overrides seeds_file)",
    )
    args = parser.parse_args()

    # Load seeds
    if args.seeds:
        try:
            seeds = json.loads(args.seeds)
        except json.JSONDecodeError as e:
            print(f"error: invalid JSON passed to --seeds: {e}", file=sys.stderr)
            sys.exit(1)
    else:
        seeds_path = Path(args.seeds_file)
        if not seeds_path.exists():
            print(f"error: seeds file not found: {seeds_path}", file=sys.stderr)
            sys.exit(1)
        try:
            seeds = json.loads(seeds_path.read_text())
        except json.JSONDecodeError as e:
            print(f"error: invalid JSON in {seeds_path}: {e}", file=sys.stderr)
            sys.exit(1)

    if not isinstance(seeds, list) or not seeds:
        print("No seeds to import.")
        return

    # Import evolution package
    try:
        from evolution.reflexion.store import save_reflection
    except ImportError as e:
        print(f"error: evolution package not available: {e}", file=sys.stderr)
        print("Run setup first, or ensure PYTHONPATH includes the project root.", file=sys.stderr)
        sys.exit(1)

    imported = 0
    skipped = 0
    errors = 0

    for seed in seeds:
        content = seed.get("content", "").strip()
        if not content:
            print(f"  skip (empty content): {seed.get('id', '?')}")
            skipped += 1
            continue

        category = seed.get("category", "general")
        score_at_gen = float(seed.get("score_at_gen", 0.5))
        label = seed.get("summary") or content[:60]

        try:
            rid = save_reflection(
                content=content,
                category=category,
                score_at_gen=score_at_gen,
                interaction_id=None,
                group_folder=None,  # seeds are always global
            )
            if rid is None:
                print(f"  skipped (near-duplicate): {label}")
                skipped += 1
            else:
                print(f"  imported [{category}]: {label}")
                imported += 1
        except Exception as e:
            print(f"  error importing '{label}': {e}", file=sys.stderr)
            errors += 1

    print(f"\nDone: {imported} imported, {skipped} skipped, {errors} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Build the M1c bloated-arm padding corpus.

Generates kind=standard atom files in /tmp from the checked-in synthetic
padding fixture (no vault Persona/Research content used; those use a
different YAML schema that standards_pack.py can't load).

Optionally copies production atoms verbatim into the same /tmp dir so
the bloated arm has prod (~876 tok) + padding (~2000 tok) ≈ 3000 tok.

Usage:
  python3 scripts/bench/build_m1c_padding.py \\
      --fixture scripts/bench/fixtures/m1c_synthetic_padding_atoms.json \\
      --prod-source ~/.claude/projects/<encoded-project-dir>/memory \\
      --out /tmp/m1c_padding

Exits 0 on success, 1 on argument errors, 2 on fixture errors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent


def _write_synthetic_atoms(fixture_path: Path, out_dir: Path) -> int:
    """Materialize the synthetic-atom fixture as one .md file per atom."""
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    atoms = fixture.get("atoms", [])
    if not atoms:
        raise ValueError(f"Fixture has no atoms: {fixture_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    for atom in atoms:
        name = atom["name"]
        description = atom["description"]
        priority = atom.get("priority", "med")
        # Schema matches production kind=standard atom format.
        content = (
            "---\n"
            "kind: standard\n"
            f"name: {name}\n"
            f"description: {description}\n"
            f"priority: {priority}\n"
            "---\n"
            f"{description}\n"
        )
        (out_dir / f"{name}.md").write_text(content, encoding="utf-8")
    return len(atoms)


def _copy_prod_atoms(prod_source: Path, out_dir: Path) -> int:
    """Copy production kind=standard atoms verbatim. Originals untouched."""
    if not prod_source.exists():
        return 0
    count = 0
    for atom in sorted(prod_source.rglob("*.md")):
        if not atom.is_file():
            continue
        if atom.name in ("_template.md", "INDEX.md"):
            continue
        try:
            head = atom.read_text(encoding="utf-8", errors="ignore")[:500]
        except OSError:
            continue
        if "\nkind: standard" not in head and not head.startswith("kind: standard"):
            continue
        dest = out_dir / atom.name
        if dest.exists():
            continue
        shutil.copy2(atom, dest)
        count += 1
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--fixture",
        default=str(_THIS_DIR / "fixtures" / "m1c_synthetic_padding_atoms.json"),
        help="Path to the synthetic-padding JSON fixture (versioned, public-safe).",
    )
    parser.add_argument(
        "--prod-source",
        default=None,
        help="Optional: prod atom dir. If set, all kind=standard atoms copied "
        "verbatim into --out alongside the synthetic atoms.",
    )
    parser.add_argument("--out", required=True, help="Destination /tmp dir.")
    parser.add_argument(
        "--manifest",
        default=None,
        help="Optional manifest JSON path summarizing what was built.",
    )
    args = parser.parse_args(argv)

    fixture_path = Path(args.fixture).expanduser()
    if not fixture_path.exists():
        print(f"[build_m1c_padding] ERROR: fixture not found: {fixture_path}", file=sys.stderr)
        return 2
    out = Path(args.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)

    try:
        n_synth = _write_synthetic_atoms(fixture_path, out)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"[build_m1c_padding] ERROR: fixture invalid: {e}", file=sys.stderr)
        return 2

    print(
        f"[build_m1c_padding] wrote {n_synth} synthetic atoms (kind=standard) to {out}",
        file=sys.stderr,
    )

    n_prod = 0
    prod_source_path = None
    if args.prod_source:
        prod_source_path = Path(args.prod_source).expanduser()
        n_prod = _copy_prod_atoms(prod_source_path, out)
        print(
            f"[build_m1c_padding] copied {n_prod} prod atoms (verbatim) to {out}",
            file=sys.stderr,
        )

    manifest = {
        "out_dir": str(out),
        "fixture_path": str(fixture_path),
        "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        "n_synthetic": n_synth,
        "n_prod": n_prod,
        "prod_source": str(prod_source_path) if prod_source_path else None,
        "padding_source": "synthetic_fixture",
    }
    if args.manifest:
        Path(args.manifest).write_text(json.dumps(manifest, indent=2))
        print(
            f"[build_m1c_padding] manifest written to {args.manifest}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

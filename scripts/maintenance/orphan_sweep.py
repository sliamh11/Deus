#!/usr/bin/env python3
# macOS-local maintenance tool — NOT a CI job. Reads the local gitignored codegraph index.
"""Layer 3 of the LIA-143 facade-prevention mechanism: hybrid orphan-sweep.

Reports exported symbols in a curated set of integration modules that look like
facades — built, exported, but reached by nothing in the runtime path. It is a
periodic drift catch for the case Layers 1-2 cannot see: a module merged behind
a tracked flag whose consumer wire never lands weeks later.

DUAL-EVIDENCE CORROBORATION (why AND, not OR): a symbol is flagged ONLY when BOTH
independent signals agree it is unused —
  1. codegraph: no incoming caller edge from a non-test file other than its own, AND
  2. grep:      its name appears in no non-test file other than its own definition.
Codegraph alone silently drops some real call edges (verified: createMessageOrchestrator
at index.ts:339 has zero codegraph callers despite being called there) — so a
codegraph-only sweep floods false positives. The grep signal suppresses those.
Do NOT "simplify" this to OR: that resurrects the false-positive flood this design
exists to avoid.

This is macOS-local only: it reads the gitignored `.codegraph/codegraph.db` built by
the local codegraph daemon. It does not run on Linux or in CI. Run `codegraph index`
first if the index is stale (the tool warns when it looks stale).

See docs/decisions/facade-prevention-mechanism.md.

Exit codes: 0 = no orphans (or advisory mode), 1 = orphans found AND --strict, 2 = setup error.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Only these kinds are runtime facades worth flagging. A never-used type/interface
# is dead-type cleanup, not a built-but-not-wired runtime facade.
SYMBOL_KINDS = ("function", "class", "method")

# Edge kinds that count as a real use. 'contains' is structural (file→symbol), not use.
USAGE_KINDS = ("calls", "references", "instantiates", "imports", "extends", "implements")

_TEST_MARKERS = (".test.", ".spec.")
_TEST_DIR_FRAGMENTS = ("/tests/", "/scripts/tests/", "/evolution/tests/", "/__tests__/")


@dataclass(frozen=True)
class Orphan:
    file_path: str
    kind: str
    name: str


def is_test_path(path: str) -> bool:
    p = f"/{path}"
    fname = path.rsplit("/", 1)[-1]
    return any(m in fname for m in _TEST_MARKERS) or any(f in p for f in _TEST_DIR_FRAGMENTS)


def load_modules(modules_file: Path) -> list[str]:
    out: list[str] = []
    for line in modules_file.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _norm(path: str) -> str:
    """Normalize a stored file_path for comparison (codegraph may store with/without leading ./)."""
    return path[2:] if path.startswith("./") else path


def exported_symbols(conn: sqlite3.Connection, module: str) -> list[sqlite3.Row]:
    placeholders = ",".join("?" for _ in SYMBOL_KINDS)
    rows = conn.execute(
        f"""SELECT id, kind, name, file_path FROM nodes
            WHERE is_exported = 1 AND kind IN ({placeholders})
              AND (file_path = ? OR file_path = ?)""",
        (*SYMBOL_KINDS, module, f"./{module}"),
    ).fetchall()
    return rows


def has_codegraph_caller(conn: sqlite3.Connection, node_id: str, own_file: str) -> bool:
    placeholders = ",".join("?" for _ in USAGE_KINDS)
    rows = conn.execute(
        f"""SELECT sn.file_path FROM edges e JOIN nodes sn ON e.source = sn.id
            WHERE e.target = ? AND e.kind IN ({placeholders})""",
        (node_id, *USAGE_KINDS),
    ).fetchall()
    own = _norm(own_file)
    for (src_path,) in rows:
        if _norm(src_path) == own or is_test_path(_norm(src_path)):
            continue
        return True
    return False


def has_cross_file_use(name: str, own_file: str, repo_dir: str) -> bool:
    """True if `name` (word-matched) appears in a tracked non-test file other than its own."""
    res = subprocess.run(
        ["git", "-C", repo_dir, "grep", "-l", "-w", "-F", "-e", name],
        capture_output=True,
        text=True,
    )
    if res.returncode not in (0, 1):  # 1 = no match; anything else is an error
        # Conservative: on git error, assume used (never false-flag an orphan).
        return True
    own = _norm(own_file)
    for hit in res.stdout.splitlines():
        hit = _norm(hit.strip())
        if not hit or hit == own or is_test_path(hit):
            continue
        return True
    return False


def find_orphans(conn: sqlite3.Connection, modules: list[str], repo_dir: str) -> list[Orphan]:
    orphans: list[Orphan] = []
    for module in modules:
        for row in exported_symbols(conn, module):
            node_id, kind, name, file_path = row[0], row[1], row[2], row[3]
            if has_codegraph_caller(conn, node_id, file_path):
                continue
            if has_cross_file_use(name, file_path, repo_dir):
                continue
            orphans.append(Orphan(_norm(file_path), kind, name))
    return orphans


def index_looks_stale(db_path: Path, modules: list[str], repo_dir: str) -> bool:
    """True if any seed module's source is newer than the index DB."""
    if not db_path.exists():
        return True
    db_mtime = db_path.stat().st_mtime
    for module in modules:
        src = Path(repo_dir) / module
        if src.exists() and src.stat().st_mtime > db_mtime:
            return True
    return False


_LAUNCHD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.deus.orphan-sweep</string>
  <key>ProgramArguments</key>
  <array>
    <string>{python}</string>
    <string>{script}</string>
    <string>--repo</string>
    <string>{repo}</string>
  </array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <!-- Weekly: Sunday 09:00 local. -->
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>{repo}/.codegraph/orphan-sweep.log</string>
  <key>StandardErrorPath</key><string>{repo}/.codegraph/orphan-sweep.log</string>
</dict>
</plist>
"""


def print_launchd(repo_dir: str) -> None:
    print(
        _LAUNCHD_TEMPLATE.format(
            python=sys.executable,
            script=str(Path(__file__).resolve()),
            repo=str(Path(repo_dir).resolve()),
        )
    )


def main(argv: list[str] | None = None) -> int:
    default_modules = Path(__file__).with_name("orphan_sweep_modules.txt")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=".", help="repo root")
    ap.add_argument("--db", default=None, help="codegraph DB (default: <repo>/.codegraph/codegraph.db)")
    ap.add_argument("--modules", default=str(default_modules), help="seed module list file")
    ap.add_argument("--strict", action="store_true", help="exit 1 if orphans are found")
    ap.add_argument("--print-launchd", action="store_true", help="print a launchd plist and exit")
    args = ap.parse_args(argv)

    if args.print_launchd:
        print_launchd(args.repo)
        return 0

    db_path = Path(args.db) if args.db else Path(args.repo) / ".codegraph" / "codegraph.db"
    if not db_path.exists():
        print(
            f"orphan-sweep: codegraph index not found at {db_path}.\n"
            "This is a macOS-local tool — it needs the local codegraph daemon's index.\n"
            "Run `codegraph index` in the repo first.",
            file=sys.stderr,
        )
        return 2

    try:
        modules = load_modules(Path(args.modules))
    except OSError as exc:
        print(f"orphan-sweep: cannot read modules file {args.modules}: {exc}", file=sys.stderr)
        return 2

    if index_looks_stale(db_path, modules, args.repo):
        print(
            "orphan-sweep: WARNING — codegraph index looks stale (a seed module is newer than "
            "the index). Run `codegraph index` first or results may be wrong.",
            file=sys.stderr,
        )

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        orphans = find_orphans(conn, modules, args.repo)
    except sqlite3.Error as exc:
        print(f"orphan-sweep: codegraph DB query failed ({exc}); try `codegraph index`.", file=sys.stderr)
        return 2
    finally:
        conn.close()

    if not orphans:
        print(f"orphan-sweep: no orphaned exports across {len(modules)} seed module(s).")
        return 0

    print("orphan-sweep: high-confidence orphan exports (no codegraph caller AND no cross-file use):\n")
    for o in orphans:
        print(f"  {o.file_path}  {o.kind} {o.name}")
    print(
        "\nEach is exported but reached by nothing outside its own file + tests. Wire it into a "
        "runtime path, delete it, or confirm it's a deferred-wire tracked in Linear.\n"
        "See docs/decisions/facade-prevention-mechanism.md."
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())

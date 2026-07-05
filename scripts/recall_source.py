#!/usr/bin/env python3
"""recall_source.py — decompress a session log's archived source transcript (LIA-374).

`deus recall --source <session-log.md | sha256>` reads the log's
`source_transcript:` frontmatter key (stamped by /compress), locates the
content-addressed archive (`transcript_archive._archive_dir()`), and writes
the raw transcript bytes to stdout (or --out). The lossy summary stays the
human-facing artifact; this is the decompress-back-to-source path.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import transcript_archive as ta  # noqa: E402

_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^source_transcript:\s*([0-9a-f]{64})\s*$", re.MULTILINE)


def _sha_from_source(source: str) -> str | None:
    if _SHA_RE.match(source):
        return source
    path = Path(source).expanduser()
    if not path.is_file():
        return None
    match = _KEY_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1) if match else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recall_source",
        description="Restore the raw transcript behind a /compress session log.",
    )
    parser.add_argument(
        "--source",
        required=True,
        help="Session-log path (reads its source_transcript: key) or a bare sha256",
    )
    parser.add_argument("--out", help="Write to this path instead of stdout")
    args = parser.parse_args(argv)

    sha = _sha_from_source(args.source)
    if sha is None:
        print(
            f"error: no source_transcript: key found in {args.source} "
            "(log predates archival, or archival failed at /compress time)",
            file=sys.stderr,
        )
        return 1

    store = ta.archive_dir()
    dest = next(
        (p for s in (".jsonl.zst", ".jsonl.gz") if (p := store / f"{sha}{s}").exists()),
        None,
    )
    if dest is None:
        print(f"error: no archive for {sha} under {store}", file=sys.stderr)
        return 1

    raw = ta.decompress(dest)
    if args.out:
        Path(args.out).write_bytes(raw)
        print(f"restored {len(raw)} bytes -> {args.out}", file=sys.stderr)
    else:
        sys.stdout.buffer.write(raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""transcript_archive.py — content-addressed cold-source retention (LIA-374).

`/compress` writes ~170x lossy session summaries while the byte-exact source
(the Claude Code transcript JSONL) is orphaned under Claude Code's own
retention. This archives the transcript into a content-addressed cold store
(default `~/.deus/archive/transcripts/<sha256>.jsonl.zst`) so a summary's
`source_transcript:` frontmatter key can always decompress back to source.

Design: content-addressable store (the standard dedup/immutability pattern —
same family as git objects): sha256-of-raw-bytes key, single flat directory,
O(1) lookup, idempotent writes, append-only (no-db-deletion philosophy).
Transcript resolution is an ordered fallback chain implemented as plain
conditionals: session registry match → newest-mtime JSONL → explicit override.

The store default lives under `~/.deus/` — deliberately OUTSIDE the
Obsidian/OneDrive-synced vault (raw transcripts carry PII; only the sha
reference enters the vault). Compression: zstd -19 via the CLI when present,
gzip fallback otherwise (cross-platform default; flagged in the JSON output).
`--best-effort` never raises and always exits 0 — archival must never block
`/compress`.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


def archive_dir() -> Path:
    """Public: the content-addressed store location (recall_source depends on it)."""
    return Path(
        os.environ.get(
            "DEUS_TRANSCRIPT_ARCHIVE_DIR",  # LIA-374
            "~/.deus/archive/transcripts",
        )
    ).expanduser()


# Backward-compat private alias (early callers/tests used the underscore name).
_archive_dir = archive_dir


def _sessions_dir() -> Path:
    return Path("~/.claude/sessions").expanduser()


def _projects_dir() -> Path:
    return Path("~/.claude/projects").expanduser()


def _zstd_bin() -> str | None:
    return shutil.which("zstd")


def resolve_transcript(cwd: str) -> Path | None:
    """Locate the current session's transcript for `cwd`.

    Ordered fallback chain: (0) `CLAUDE_SESSION_ID` env → that exact
    transcript (the repo's established disambiguator for concurrent
    same-cwd sessions — see session_preflight's self-exclusion); (1) newest
    session-registry entry whose cwd matches → `<projects>/<slug>/
    <sessionId>.jsonl`; (2) newest `*.jsonl` in the slug dir. None when
    nothing matches. Without (0), two live sessions on one cwd could get a
    SIBLING session's transcript backlinked — ok:true, wrong source.

    Slug encoding: Claude Code maps every non-alphanumeric character (both
    `/` and `.`) to `-` — e.g. `/x/.claude/wt` → `-x--claude-wt`. A plain
    `/`→`-` replace misses dotted segments and silently resolves nothing.
    """
    slug = re.sub(r"[^A-Za-z0-9-]", "-", cwd)
    slug_dir = _projects_dir() / slug

    own_session = os.environ.get("CLAUDE_SESSION_ID", "").strip()
    if own_session:
        own = slug_dir / f"{own_session}.jsonl"
        if own.is_file():
            return own

    candidates: list[tuple[float, Path]] = []
    sessions = _sessions_dir()
    if sessions.is_dir():
        for f in sessions.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("cwd") != cwd or not data.get("sessionId"):
                continue
            transcript = slug_dir / f"{data['sessionId']}.jsonl"
            if transcript.is_file():
                candidates.append((f.stat().st_mtime, transcript))
    if candidates:
        return max(candidates)[1]

    if slug_dir.is_dir():
        jsonls = sorted(
            slug_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if jsonls:
            return jsonls[0]
    return None


def archive(transcript: Path, *, best_effort: bool = False) -> dict:
    """Compress `transcript` into the content-addressed store.

    Returns {"ok", "sha256", "dest", "skipped", "codec"} on success;
    in best-effort mode failures return {"ok": False, "error": ...}
    instead of raising.
    """
    try:
        raw = Path(transcript).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        store = archive_dir()
        store.mkdir(parents=True, exist_ok=True)

        for suffix in (".jsonl.zst", ".jsonl.gz"):
            existing = store / f"{sha}{suffix}"
            if existing.exists():
                return {
                    "ok": True,
                    "sha256": sha,
                    "dest": str(existing),
                    "skipped": True,
                    "codec": "zstd" if suffix.endswith("zst") else "gzip",
                }

        zstd = _zstd_bin()
        if zstd:
            dest = store / f"{sha}.jsonl.zst"
            tmp = store / f".{sha}.tmp.zst"
            subprocess.run(
                [zstd, "-19", "-q", "-f", "-o", str(tmp), str(transcript)],
                check=True,
                capture_output=True,
            )
            tmp.rename(dest)
            codec = "zstd"
        else:
            dest = store / f"{sha}.jsonl.gz"
            tmp = store / f".{sha}.tmp.gz"
            with gzip.open(tmp, "wb", compresslevel=9) as f:
                f.write(raw)
            tmp.rename(dest)
            codec = "gzip"

        return {
            "ok": True,
            "sha256": sha,
            "dest": str(dest),
            "skipped": False,
            "codec": codec,
        }
    except Exception as e:  # noqa: BLE001 — best-effort must swallow everything
        if best_effort:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        raise


def decompress(dest: Path) -> bytes:
    """Restore the raw transcript bytes from an archived file."""
    dest = Path(dest)
    if dest.suffix == ".zst":
        zstd = _zstd_bin()
        if not zstd:
            raise RuntimeError("zstd binary required to decompress .zst archives")
        proc = subprocess.run(
            [zstd, "-d", "-q", "-c", str(dest)], check=True, capture_output=True
        )
        return proc.stdout
    return gzip.decompress(dest.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="transcript_archive",
        description="Archive a session transcript into the content-addressed cold store.",
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--cwd", help="Session cwd — resolve its transcript automatically")
    src.add_argument("--transcript", help="Explicit transcript path")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument(
        "--best-effort",
        action="store_true",
        help="Never fail: errors exit 0 with ok:false JSON (for /compress)",
    )
    args = parser.parse_args(argv)

    if args.transcript:
        transcript: Path | None = Path(args.transcript)
        if not transcript.is_file():
            # Same clean-error shape as the --cwd branch — a raw traceback is
            # not an acceptable CLI failure mode for user-supplied paths.
            transcript = None
            missing = f"transcript not found: {args.transcript}"
    else:
        transcript = resolve_transcript(args.cwd)
        missing = f"no transcript found for cwd {args.cwd}"

    if transcript is None:
        result: dict = {"ok": False, "error": missing}
    else:
        result = archive(transcript, best_effort=args.best_effort)

    if args.json:
        print(json.dumps(result))
    elif result["ok"]:
        print(f"{result['sha256']}  {result['dest']}")
    else:
        print(f"error: {result['error']}", file=sys.stderr)

    if not result["ok"] and not args.best_effort:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

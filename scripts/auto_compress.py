#!/usr/bin/env python3
"""
Deterministic SessionEnd auto-save worker.

Saves a mechanical (non-LLM) session record when a Claude Code session ends,
without ever resuming the session as an agent -- the transcript is read only
as structured JSONL *data* (turn/tool counts, timestamps, touched file
paths), never as instructions for a model to act on.

This is the direct fix for an earlier design that resumed the session via
`claude -p --resume ... "/compress"` and could not be made safe:
`--allowedTools` does not restrict the tool inventory (it only adds to
whatever `~/.claude/settings.json` already allows -- confirmed empirically),
and a `sandbox-exec` boundary left filesystem writes, inherited-environment
secrets, and exfil-capable egress (e.g. Linear) all open. Removing the LLM
turn removes the injection surface entirely: there is no tool-use loop for a
hostile transcript to steer.

Not the same feature as src/auto-compress.ts (WhatsApp/Telegram channel-
session idle-save) -- unrelated, zero shared code, naming collision only.

Extracted transcript strings (file paths, tool names) are treated as
untrusted data end to end: sanitized (control characters stripped, length
capped) before they ever reach a file, and tool names are bucketed against a
known allowlist rather than reproduced verbatim.

Checkpoints are deliberately NOT deleted here -- this worker never reads
them, so it must never be the thing that destroys their content. See LIA-469
for a separate, pre-existing gap in the interactive /compress skill's own
(also unread) checkpoint deletion; this worker's correctness does not depend
on that ticket being fixed.

Exit code 0 = the mechanical log was written; non-zero = caller should retry.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from stop_hook import _load_vault_root  # noqa: E402

_MAX_FIELD_LEN = 300
_MAX_FILES_SHOWN = 10
_KNOWN_TOOLS = frozenset({
    "Bash", "Read", "Write", "Edit", "MultiEdit", "Agent", "Skill",
    "ToolSearch", "AskUserQuestion", "EnterWorktree", "ExitWorktree",
    "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit",
})


def _sanitize(s: str, max_len: int = _MAX_FIELD_LEN) -> str:
    """Strip control characters (prevents frontmatter/YAML corruption and
    fake-key injection into a spliced block) and cap length. Every string
    extracted from a transcript is untrusted and must pass through this
    before being templated into any file."""
    cleaned = "".join(ch for ch in s if ch.isprintable())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def _bucket_tool_name(name: str) -> str:
    """Known Claude Code tool names pass through; an MCP tool's `mcp__`
    prefix is trusted as a namespace marker (assigned by the harness, not
    transcript content), but everything AFTER that prefix is still
    attacker-reachable transcript content and must be sanitized like any
    other extracted string -- an exact `_KNOWN_TOOLS` match needs no
    sanitizing (equality already constrains it to one of a small fixed set
    of clean strings), but the `mcp__` branch does not have that guarantee.
    Anything else buckets into `other` rather than reproducing an arbitrary
    string verbatim."""
    if name in _KNOWN_TOOLS:
        return name
    if name.startswith("mcp__"):
        return _sanitize(name)
    return "other"


def extract_transcript_facts(transcript_path: str) -> dict:
    """Parse the transcript JSONL as pure data -- never executed, never fed
    to a model as instructions. Returns only counts/timestamps/file-paths."""
    user_turns = 0
    assistant_turns = 0
    tool_counts: dict[str, int] = {}
    file_paths: set[str] = set()
    first_ts: str | None = None
    last_ts: str | None = None

    p = Path(transcript_path)
    if not p.exists():
        return {
            "user_turns": 0,
            "assistant_turns": 0,
            "tool_counts": {},
            "file_paths": [],
            "first_ts": None,
            "last_ts": None,
        }

    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = obj.get("type")
            if t == "user":
                user_turns += 1
            elif t == "assistant":
                assistant_turns += 1
            ts = obj.get("timestamp")
            if isinstance(ts, str) and ts:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_use":
                        name = _bucket_tool_name(str(c.get("name", "")))
                        tool_counts[name] = tool_counts.get(name, 0) + 1
                        inp = c.get("input")
                        if isinstance(inp, dict):
                            fp = inp.get("file_path")
                            if isinstance(fp, str) and fp:
                                file_paths.add(_sanitize(fp))

    return {
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "tool_counts": tool_counts,
        "file_paths": sorted(file_paths),
        "first_ts": first_ts,
        "last_ts": last_ts,
    }


def render_log(session_id: str, cwd: str, facts: dict, sha256: str | None, date: str) -> str:
    top_tools = sorted(facts["tool_counts"].items(), key=lambda kv: -kv[1])[:5]
    tools_str = ", ".join(f"{name}:{count}" for name, count in top_tools) or "none"
    files = facts["file_paths"][:_MAX_FILES_SHOWN]
    extra = len(facts["file_paths"]) - len(files)
    files_block = "\n".join(f"- {fp}" for fp in files) or "(none recorded)"
    if extra > 0:
        files_block += f"\n- (+{extra} more)"

    tldr = (
        f"Auto-saved (no LLM summary) -- {facts['user_turns']} user turns, "
        f"{facts['assistant_turns']} assistant turns, top tools: {tools_str}. "
        f"Time span: {facts['first_ts'] or 'unknown'} to {facts['last_ts'] or 'unknown'}."
    )

    lines = [
        "---",
        "type: session",
        f"date: {date}",
        "auto_generated: true",
        "topics: []",
        # json.dumps produces a double-quote-and-backslash-escaped string --
        # a safe subset of YAML double-quoted scalar escaping -- so an
        # embedded `"` (e.g. an unusual cwd) can't break out of the quotes.
        f"project_path: {json.dumps(_sanitize(cwd))}",
    ]
    if sha256:
        lines.append(f"source_transcript: {sha256}")
    lines.append("tldr: |")
    lines.append(f"  {tldr}")
    lines.append("---")
    lines.append("")
    lines.append(
        "*Mechanically generated by the SessionEnd auto-save worker -- no "
        "narrative summary, no decisions/key-learnings extracted. Run "
        "`/compress` interactively on this session for a full write-up if "
        "one is still wanted.*"
    )
    lines.append("")
    lines.append("## Files touched (raw paths, unverified)")
    lines.append("")
    lines.append(files_block)
    lines.append("")
    return "\n".join(lines)


def _run_child(argv: list[str], vault: Path, cwd: str, timeout: int) -> "subprocess.CompletedProcess[str]":
    """Every child subprocess inherits the SAME already-resolved vault via
    DEUS_VAULT_PATH -- the highest-priority tier of every downstream
    resolver -- so it never independently re-resolves (and potentially
    disagrees with) the vault this worker already picked."""
    env = {**os.environ, "DEUS_VAULT_PATH": str(vault)}
    return subprocess.run(
        argv, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True,
    )


def run(session_id: str, transcript_path: str, cwd: str, timeout: int = 120) -> int:
    vault = _load_vault_root(cwd=Path(cwd))
    if vault is None:
        print("auto_compress: no vault resolvable, aborting", file=sys.stderr)
        return 1

    facts = extract_transcript_facts(transcript_path)
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log_dir = vault / "Session-Logs" / date
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"auto-{session_id[:8]}.md"

    # Archive first, pinned to the EXACT transcript path (never --cwd
    # auto-discovery, which could bind to a sibling session's transcript
    # under concurrent same-cwd sessions).
    sha256: str | None = None
    try:
        result = _run_child(
            [
                sys.executable, str(SCRIPTS_DIR / "transcript_archive.py"),
                "--transcript", transcript_path, "--json", "--best-effort",
            ],
            vault, cwd, timeout,
        )
        if result.returncode == 0 and result.stdout:
            payload = json.loads(result.stdout)
            if payload.get("ok"):
                sha256 = payload.get("sha256")
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        print(f"auto_compress: archival step failed (non-fatal): {e}", file=sys.stderr)

    log_path.write_text(render_log(session_id, cwd, facts, sha256, date), encoding="utf-8")

    # Sync pending tasks (read-only against Linear -- no write path exists in
    # sync_linear_pending.py; the automated worker never calls
    # linear_createIssue/createComment or any other Linear write).
    try:
        _run_child(
            [sys.executable, str(SCRIPTS_DIR / "sync_linear_pending.py"), "--write"],
            vault, cwd, timeout,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        print(f"auto_compress: pending sync failed (non-fatal): {e}", file=sys.stderr)

    tldr_first_line = _sanitize(
        f"Auto-saved: {facts['user_turns']} user + {facts['assistant_turns']} assistant turns"
    )
    try:
        _run_child(
            [
                sys.executable, str(SCRIPTS_DIR / "sync_linear_pending.py"),
                "--write-previous", f"{date}: {tldr_first_line}",
            ],
            vault, cwd, timeout,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        print(f"auto_compress: previous-block update failed (non-fatal): {e}", file=sys.stderr)

    # Index only -- --no-extract is required, not optional: bare --add
    # defaults to running a real LLM atom-extraction call whose prompt
    # naively concatenates the log content with zero escaping.
    try:
        _run_child(
            [
                sys.executable, str(SCRIPTS_DIR / "memory_indexer.py"),
                "--add", str(log_path), "--no-extract",
            ],
            vault, cwd, timeout,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError) as e:
        print(f"auto_compress: indexing failed (non-fatal): {e}", file=sys.stderr)

    return 0 if log_path.exists() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript-path", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    return run(args.session_id, args.transcript_path, args.cwd, args.timeout)


if __name__ == "__main__":
    sys.exit(main())

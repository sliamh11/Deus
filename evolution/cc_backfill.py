"""
Backfill Claude Code host sessions into the evolution loop.

Reads .jsonl session transcripts from ~/.claude/projects/, extracts
(user prompt, assistant response) pairs, and feeds them through the
standard judge → reflect pipeline with eval_suite='claude_code'.

Key differences from container backfill (backfill.py):
  - Assistant entries are streamed; must dedup by message.id, keep only stop_reason != null
  - User entries include tool_results (filtered out — only real user prompts kept)
  - Tool names extracted from assistant tool_use blocks
  - System/queue/file-history entries skipped
  - Subagent transcripts skipped (separate sessions, not user-facing)

Idempotent: deterministic IDs from session UUID + pair index.

Usage:
    python3 -m evolution.cc_backfill [--sessions-dir PATH] [--dry-run] [--limit N]
    python3 -m evolution.cc_backfill --status
    python3 -m evolution.cc_backfill --project deus  # filter by project name
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

from .config import REFLECTION_THRESHOLD, POSITIVE_THRESHOLD
from .ilog.interaction_log import log_interaction, update_score
from .storage import get_storage

# Default: all Claude Code sessions for all projects
CC_SESSIONS_DIR = Path(os.path.expanduser("~/.claude/projects"))

# Skip prompts that are system-generated noise, not real user input
_SKIP_PROMPT_PATTERNS = (
    "<command-message>",     # slash command expansions
    "<command-name>",        # slash command names
    "<task-notification>",   # background task notifications
    "<local-command",        # local command output
    "[SCHEDULED TASK",
)

_MIN_PROMPT_LEN = 20
_MIN_RESPONSE_LEN = 30


def _deterministic_id(session_id: str, pair_index: int) -> str:
    raw = f"cc_backfill:{session_id}:{pair_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _already_processed(interaction_id: str) -> bool:
    store = get_storage()
    return store.get_interaction(interaction_id) is not None


def _extract_user_text(entry: dict) -> Optional[str]:
    """Extract text from a user entry, returning None for tool_results."""
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        text = content.strip()
        return text if text else None
    if isinstance(content, list):
        # Skip tool_result entries entirely
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        return text if text else None
    return None


def _extract_assistant_content(entry: dict) -> Optional[dict]:
    """Extract text + tool names from a complete assistant entry."""
    msg = entry.get("message", {})
    if msg.get("stop_reason") is None:
        return None  # Streaming partial — skip

    content = msg.get("content", [])
    text_parts = []
    tool_names = []

    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_names.append(block.get("name", ""))

    text = " ".join(text_parts).strip()
    if not text and not tool_names:
        return None

    return {"text": text, "tools": tool_names}


def _is_skip_prompt(text: str) -> bool:
    """Return True if this prompt is system-generated noise."""
    return any(text.startswith(p) or p in text[:200] for p in _SKIP_PROMPT_PATTERNS)


def _extract_pairs(jsonl_path: Path) -> Iterator[dict]:
    """
    Extract (user prompt, assistant response) pairs from a CC session .jsonl.

    Walks entries sequentially, collecting user text messages and pairing each
    with the last complete assistant response before the next user message.
    """
    try:
        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return

    # Collect turns in order: real user prompts and complete assistant messages
    turns: list[tuple[str, dict]] = []
    seen_msg_ids: set[str] = set()

    for entry in lines:
        entry_type = entry.get("type")

        if entry_type == "user":
            text = _extract_user_text(entry)
            if text and len(text) >= _MIN_PROMPT_LEN and not _is_skip_prompt(text):
                turns.append(("user", {"text": text}))

        elif entry_type == "assistant":
            msg_id = entry.get("message", {}).get("id")
            # Dedup streaming: keep only the last entry per message ID
            # (entries come in order, so last seen with stop_reason wins)
            content = _extract_assistant_content(entry)
            if content and msg_id:
                if msg_id in seen_msg_ids:
                    # Replace previous entry for this msg_id
                    for i in range(len(turns) - 1, -1, -1):
                        if turns[i][0] == "assistant" and turns[i][1].get("msg_id") == msg_id:
                            turns[i] = ("assistant", {**content, "msg_id": msg_id})
                            break
                else:
                    seen_msg_ids.add(msg_id)
                    turns.append(("assistant", {**content, "msg_id": msg_id}))

    # Pair: for each user turn, find the last assistant response before the next user turn
    pair_index = 0
    current_user = None
    last_assistant = None

    for role, data in turns:
        if role == "user":
            if current_user is not None and last_assistant is not None:
                response_text = last_assistant["text"]
                if len(response_text) >= _MIN_RESPONSE_LEN:
                    yield {
                        "prompt": current_user,
                        "response": response_text,
                        "tools": last_assistant.get("tools", []),
                        "pair_index": pair_index,
                    }
                    pair_index += 1
            current_user = data["text"]
            last_assistant = None
        elif role == "assistant":
            last_assistant = data

    # Emit the final pair
    if current_user is not None and last_assistant is not None:
        response_text = last_assistant["text"]
        if len(response_text) >= _MIN_RESPONSE_LEN:
            yield {
                "prompt": current_user,
                "response": response_text,
                "tools": last_assistant.get("tools", []),
                "pair_index": pair_index,
            }


def _infer_project_name(jsonl_path: Path) -> str:
    """Infer project name from the Claude Code project directory structure."""
    # Path: ~/.claude/projects/-Users-<user>-<project>/<uuid>.jsonl
    project_dir = jsonl_path.parent.name  # e.g. "-Users-<user>-<project>"
    # Convert back to readable name: take last segment
    parts = project_dir.strip("-").split("-")
    return parts[-1] if parts else "unknown"


def collect_sessions(
    sessions_dir: Path,
    project_filter: Optional[str] = None,
) -> list[Path]:
    """Collect all top-level session .jsonl files (skip subagents)."""
    all_files = []
    for project_dir in sessions_dir.iterdir():
        if not project_dir.is_dir():
            continue
        if project_filter and project_filter.lower() not in project_dir.name.lower():
            continue
        for f in project_dir.glob("*.jsonl"):
            # Skip subagent transcripts
            if "/subagents/" in str(f) or "\\subagents\\" in str(f):
                continue
            all_files.append(f)

    return sorted(all_files, key=lambda f: f.stat().st_mtime)


def collect_pairs(
    sessions_dir: Path,
    project_filter: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Extract all valid pairs from CC session files."""
    session_files = collect_sessions(sessions_dir, project_filter)

    pairs = []
    for fpath in session_files:
        session_id = fpath.stem
        project = _infer_project_name(fpath)

        for pair in _extract_pairs(fpath):
            iid = _deterministic_id(session_id, pair["pair_index"])
            pairs.append({
                "interaction_id": iid,
                "session_id": session_id,
                "group_folder": f"cc:{project}",
                "prompt": pair["prompt"],
                "response": pair["response"],
                "tools": pair["tools"],
            })
            if limit and len(pairs) >= limit:
                return pairs

    return pairs


def run_cc_backfill(
    sessions_dir: Path = CC_SESSIONS_DIR,
    project_filter: Optional[str] = None,
    dry_run: bool = False,
    limit: Optional[int] = None,
    verbose: bool = True,
    no_judge: bool = False,
) -> dict:
    """Run the CC session backfill."""
    pairs = collect_pairs(sessions_dir, project_filter, limit)

    stats = {
        "total": len(pairs),
        "skipped_existing": 0,
        "processed": 0,
        "failed": 0,
        "reflections_generated": 0,
    }

    if verbose:
        print(f"Found {len(pairs)} extractable pairs across CC sessions.")
        if project_filter:
            print(f"  (filtered to project: {project_filter})")
        if dry_run:
            print("[dry-run] No writes will be performed.\n")

    if dry_run:
        for i, pair in enumerate(pairs):
            if _already_processed(pair["interaction_id"]):
                stats["skipped_existing"] += 1
            else:
                stats["processed"] += 1
                if verbose:
                    preview = pair["prompt"].replace("\n", " ")[:80]
                    tools = ", ".join(pair["tools"][:3]) or "none"
                    print(f"[{i+1}/{len(pairs)}] {pair['group_folder']} | tools={tools} | {preview!r}")
        return stats

    # Import judge lazily — it may not be available in all environments
    if no_judge:
        judge = None
    else:
        try:
            from .judge import make_runtime_judge
            judge = make_runtime_judge()
        except Exception as exc:
            print(f"ERROR: Cannot create judge: {exc}")
            print("Logging interactions without scores. Run maintenance to judge later.")
            judge = None

    from .reflexion.generator import generate_reflection, generate_positive_reflection
    from .reflexion.store import save_reflection

    for i, pair in enumerate(pairs):
        iid = pair["interaction_id"]

        if _already_processed(iid):
            stats["skipped_existing"] += 1
            if verbose:
                print(f"[{i+1}/{len(pairs)}] skip  {iid[:12]}... (already processed)")
            continue

        if verbose:
            preview = pair["prompt"].replace("\n", " ")[:60]
            print(f"[{i+1}/{len(pairs)}] score {iid[:12]}... | {pair['group_folder']} | {preview!r}")

        # Always log the interaction
        tools_json = json.dumps(pair["tools"]) if pair["tools"] else None
        log_interaction(
            prompt=pair["prompt"],
            response=pair["response"],
            group_folder=pair["group_folder"],
            session_id=pair["session_id"],
            eval_suite="claude_code",
            interaction_id=iid,
            tools_used=pair["tools"],
        )

        # Judge if available
        if judge:
            try:
                result = judge.evaluate(
                    prompt=pair["prompt"],
                    response=pair["response"],
                    tools_used=pair["tools"],
                )
            except Exception as exc:
                if verbose:
                    print(f"  !! judge failed: {exc}")
                stats["failed"] += 1
                if "429" in str(exc) or "quota" in str(exc).lower():
                    time.sleep(5)
                stats["processed"] += 1
                continue

            dims = {
                "quality": result.quality,
                "safety": result.safety,
                "tool_use": result.tool_use,
                "personalization": result.personalization,
            }
            update_score(iid, result.score, dims, parse_error=result.is_parse_error)

            if verbose:
                print(f"  score={result.score:.2f}  q={result.quality:.2f}  "
                      f"s={result.safety:.2f}  t={result.tool_use:.2f}  "
                      f"p={result.personalization:.2f}")

            # Generate reflections
            if not result.is_parse_error:
                if result.score < REFLECTION_THRESHOLD:
                    try:
                        content, category = generate_reflection(
                            prompt=pair["prompt"],
                            response=pair["response"],
                            score=result.score,
                            dims=dims,
                            rationale=result.rationale,
                            tools_used=pair["tools"],
                        )
                        save_reflection(
                            content=content,
                            category=category,
                            score_at_gen=result.score,
                            interaction_id=iid,
                            group_folder=pair["group_folder"],
                        )
                        stats["reflections_generated"] += 1
                        if verbose:
                            print(f"  -> reflection ({category}): {content[:80]}...")
                    except Exception as exc:
                        if verbose:
                            print(f"  !! reflection failed: {exc}")
                elif result.score >= POSITIVE_THRESHOLD:
                    try:
                        content, category = generate_positive_reflection(
                            prompt=pair["prompt"],
                            response=pair["response"],
                            score=result.score,
                            dims=dims,
                            rationale=result.rationale,
                            tools_used=pair["tools"],
                        )
                        save_reflection(
                            content=content,
                            category=category,
                            score_at_gen=result.score,
                            interaction_id=iid,
                            group_folder=pair["group_folder"],
                        )
                        stats["reflections_generated"] += 1
                        if verbose:
                            print(f"  -> positive reflection ({category}): {content[:80]}...")
                    except Exception as exc:
                        if verbose:
                            print(f"  !! reflection failed: {exc}")

            # Small pause between LLM calls
            time.sleep(0.5)

        stats["processed"] += 1

    return stats


def print_status() -> None:
    """Print current CC backfill status."""
    store = get_storage()
    cc_stats = store.interaction_stats("claude_code")
    runtime_stats = store.interaction_stats("runtime")
    backfill_stats = store.interaction_stats("backfill")

    print("=== Evolution loop status ===")
    print(f"  claude_code interactions: {cc_stats['total']} total, {cc_stats['scored']} scored"
          + (f", avg score={cc_stats['avg_score']:.2f}" if cc_stats['avg_score'] else ""))
    print(f"  runtime interactions    : {runtime_stats['total']} total, {runtime_stats['scored']} scored"
          + (f", avg score={runtime_stats['avg_score']:.2f}" if runtime_stats['avg_score'] else ""))
    print(f"  backfill interactions   : {backfill_stats['total']} total, {backfill_stats['scored']} scored"
          + (f", avg score={backfill_stats['avg_score']:.2f}" if backfill_stats['avg_score'] else ""))

    # Count available but unprocessed sessions
    try:
        sessions = collect_sessions(CC_SESSIONS_DIR)
        pairs = collect_pairs(CC_SESSIONS_DIR)
        unprocessed = sum(1 for p in pairs if not _already_processed(p["interaction_id"]))
        print(f"\n  Available CC sessions: {len(sessions)}")
        print(f"  Extractable pairs    : {len(pairs)}")
        print(f"  Not yet processed    : {unprocessed}")
    except Exception:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill Claude Code host sessions into evolution loop"
    )
    parser.add_argument("--sessions-dir", type=Path, default=CC_SESSIONS_DIR,
                        help="Path to ~/.claude/projects (default: auto-detected)")
    parser.add_argument("--project", type=str, default=None,
                        help="Filter by project name (e.g. 'deus')")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview pairs without writing to DB or calling judge")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N pairs")
    parser.add_argument("--status", action="store_true",
                        help="Print current backfill status and exit")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-pair output")
    parser.add_argument("--no-judge", action="store_true",
                        help="Skip judging; log interactions only (judge later via maintenance)")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    stats = run_cc_backfill(
        sessions_dir=args.sessions_dir,
        project_filter=args.project,
        dry_run=args.dry_run,
        limit=args.limit,
        verbose=not args.quiet,
        no_judge=args.no_judge,
    )

    print(f"\n{'[dry-run] ' if args.dry_run else ''}Done.")
    print(f"  total pairs found    : {stats['total']}")
    print(f"  already processed    : {stats['skipped_existing']}")
    print(f"  newly processed      : {stats['processed']}")
    print(f"  failed               : {stats['failed']}")
    print(f"  reflections generated: {stats['reflections_generated']}")


if __name__ == "__main__":
    main()

"""
Backfill historical agent interactions into the evolution loop.

Reads Claude session .jsonl files from data/sessions/, extracts (prompt, response)
pairs, scores them with GeminiRuntimeJudge, and inserts them into the interactions
table tagged as eval_suite='backfill'.

Low-scoring pairs (< REFLECTION_THRESHOLD) also get a reflexion lesson generated
and stored, immediately seeding the retrieval store.

Idempotent: pairs are identified by a deterministic ID derived from the session file
and pair index, so re-running skips already-processed entries.

Usage:
    python3 -m evolution.backfill [--sessions-dir PATH] [--dry-run] [--limit N]
    python3 -m evolution.backfill --status
"""
import argparse
import glob
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator

from .config import REFLECTION_THRESHOLD
from .ilog.interaction_log import log_interaction, update_score
from .storage import get_storage
from .judge import make_runtime_judge
from .reflexion.generator import generate_reflection
from .reflexion.store import save_reflection

SESSIONS_DIR = Path(__file__).parent.parent / "data" / "sessions"

# Responses that are noise, not real agent output
_SKIP_RESPONSE_PREFIXES = (
    "Failed to authenticate",
    "API Error:",
    "Container timed out",
    "Error:",
)
_SKIP_PROMPT_PREFIXES = (
    "[SCHEDULED TASK",
)
_MIN_PROMPT_LEN = 15
_MIN_RESPONSE_LEN = 20


def _deterministic_id(session_id: str, pair_index: int) -> str:
    raw = f"backfill:{session_id}:{pair_index}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _already_processed(interaction_id: str) -> bool:
    store = get_storage()
    return store.get_interaction(interaction_id) is not None


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            block.get("text", "") for block in content if block.get("type") == "text"
        ).strip()
    return ""


def _extract_pairs(jsonl_path: Path) -> Iterator[dict]:
    """
    Yield (prompt, response, pair_index) dicts from a session .jsonl file.
    Only yields pairs where neither side is junk.
    """
    try:
        lines = [json.loads(l) for l in jsonl_path.read_text().splitlines() if l.strip()]
    except (OSError, json.JSONDecodeError):
        return

    pair_index = 0
    for i, entry in enumerate(lines):
        if entry.get("type") != "user":
            continue

        prompt = _extract_text(entry.get("message", {}).get("content", ""))
        if len(prompt) < _MIN_PROMPT_LEN:
            continue
        if any(prompt.startswith(p) for p in _SKIP_PROMPT_PREFIXES):
            continue

        # Find the immediately following assistant entry
        for j in range(i + 1, min(i + 6, len(lines))):
            if lines[j].get("type") == "assistant":
                response = _extract_text(lines[j].get("message", {}).get("content", ""))
                if len(response) < _MIN_RESPONSE_LEN:
                    break
                if any(response.startswith(p) for p in _SKIP_RESPONSE_PREFIXES):
                    break
                yield {"prompt": prompt, "response": response, "pair_index": pair_index}
                pair_index += 1
                break


def _infer_group_folder(jsonl_path: Path) -> str:
    """Infer a group_folder name from the session directory path."""
    parts = jsonl_path.parts
    for i, part in enumerate(parts):
        if part == "sessions" and i + 1 < len(parts):
            return parts[i + 1]  # e.g. 'whatsapp_main' or 'telegram_main'
    return "unknown"


def collect_pairs(sessions_dir: Path, limit: int | None = None) -> list[dict]:
    """
    Walk sessions_dir, extract all valid pairs from non-subagent .jsonl files.
    Returns list of dicts with: prompt, response, session_id, group_folder, interaction_id
    """
    pattern = str(sessions_dir / "**" / ".claude" / "projects" / "*" / "*.jsonl")
    all_files = [
        Path(f) for f in glob.glob(pattern, recursive=True)
        if "/subagents/" not in f
    ]

    pairs = []
    for fpath in all_files:
        session_id = fpath.stem
        group_folder = _infer_group_folder(fpath)
        for pair in _extract_pairs(fpath):
            iid = _deterministic_id(session_id, pair["pair_index"])
            pairs.append({
                "interaction_id": iid,
                "session_id": session_id,
                "group_folder": group_folder,
                "prompt": pair["prompt"],
                "response": pair["response"],
            })
            if limit and len(pairs) >= limit:
                return pairs
    return pairs


def run_backfill(
    sessions_dir: Path = SESSIONS_DIR,
    dry_run: bool = False,
    limit: int | None = None,
    verbose: bool = True,
) -> dict:
    pairs = collect_pairs(sessions_dir, limit)
    judge = make_runtime_judge()

    stats = {
        "total": len(pairs),
        "skipped_existing": 0,
        "processed": 0,
        "failed": 0,
        "reflections_generated": 0,
    }

    if verbose:
        print(f"Found {len(pairs)} extractable pairs across all sessions.")
        if dry_run:
            print("[dry-run] No writes will be performed.\n")

    for i, pair in enumerate(pairs):
        iid = pair["interaction_id"]

        if _already_processed(iid):
            stats["skipped_existing"] += 1
            if verbose:
                print(f"[{i+1}/{len(pairs)}] skip  {iid[:12]}… (already processed)")
            continue

        if verbose:
            prompt_preview = pair["prompt"].replace("\n", " ")[:60]
            print(f"[{i+1}/{len(pairs)}] score {iid[:12]}… | {pair['group_folder']} | {prompt_preview!r}")

        if dry_run:
            stats["processed"] += 1
            continue

        try:
            result = judge.evaluate(
                prompt=pair["prompt"],
                response=pair["response"],
            )
        except Exception as exc:
            if verbose:
                print(f"  !! judge failed: {exc}")
            stats["failed"] += 1
            # Brief back-off on quota errors
            if "429" in str(exc) or "quota" in str(exc).lower():
                time.sleep(5)
            continue

        # Write interaction row
        log_interaction(
            prompt=pair["prompt"],
            response=pair["response"],
            group_folder=pair["group_folder"],
            session_id=pair["session_id"],
            eval_suite="backfill",
            interaction_id=iid,
        )
        update_score(iid, result.score, {
            "quality": result.quality,
            "safety": result.safety,
            "tool_use": result.tool_use,
            "personalization": result.personalization,
        })

        if verbose:
            print(f"  score={result.score:.2f}  q={result.quality:.2f}  "
                  f"s={result.safety:.2f}  t={result.tool_use:.2f}  "
                  f"p={result.personalization:.2f}")

        # Generate reflection for low-scoring interactions
        if result.score < REFLECTION_THRESHOLD:
            try:
                content, category = generate_reflection(
                    prompt=pair["prompt"],
                    response=pair["response"],
                    score=result.score,
                    dims={
                        "quality": result.quality,
                        "safety": result.safety,
                        "tool_use": result.tool_use,
                        "personalization": result.personalization,
                    },
                    rationale=result.rationale,
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
                    print(f"  → reflection generated ({category}): {content[:80]}…")
            except Exception as exc:
                if verbose:
                    print(f"  !! reflection failed: {exc}")

        stats["processed"] += 1
        # Small pause between Gemini calls to stay under rate limits
        time.sleep(0.5)

    return stats


def print_status() -> None:
    store = get_storage()
    backfill_stats = store.interaction_stats("backfill")
    runtime_stats = store.interaction_stats("runtime")
    reflections = store.backfill_reflection_count()

    total = backfill_stats["total"]
    scored = backfill_stats["scored"]
    avg = backfill_stats["avg_score"]

    print("=== Evolution loop status ===")
    print(f"  backfill interactions : {total} total, {scored} scored"
          + (f", avg score={avg:.2f}" if avg else ""))
    print(f"  runtime interactions  : {runtime_stats['total']} total, {runtime_stats['scored']} scored")
    print(f"  reflections (backfill): {reflections}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill historical interactions into evolution loop")
    parser.add_argument("--sessions-dir", type=Path, default=SESSIONS_DIR,
                        help="Path to data/sessions directory")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview pairs without writing to DB or calling judge")
    parser.add_argument("--limit", type=int, default=None,
                        help="Process at most N pairs (useful for testing)")
    parser.add_argument("--status", action="store_true",
                        help="Print current backfill status and exit")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress per-pair output")
    args = parser.parse_args()

    if args.status:
        print_status()
        return

    stats = run_backfill(
        sessions_dir=args.sessions_dir,
        dry_run=args.dry_run,
        limit=args.limit,
        verbose=not args.quiet,
    )

    print(f"\n{'[dry-run] ' if args.dry_run else ''}Done.")
    print(f"  total pairs found    : {stats['total']}")
    print(f"  already processed    : {stats['skipped_existing']}")
    print(f"  newly processed      : {stats['processed']}")
    print(f"  failed               : {stats['failed']}")
    print(f"  reflections generated: {stats['reflections_generated']}")


if __name__ == "__main__":
    main()

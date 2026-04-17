#!/usr/bin/env python3
"""
Deus Memory Benchmark
Evaluates the memory indexer's retrieval quality via two modes:

  --outbound   LongMemEval standardized benchmark (comparable to Cortex 97.8% R@10)
  --internal   Token efficiency + local recall regression tests
  --all        Run both modes

Usage:
  python3 scripts/memory_benchmark.py --outbound [--limit N] [--k 3,5,10]
  python3 scripts/memory_benchmark.py --internal [--limit N]
  python3 scripts/memory_benchmark.py --all [--save]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

# ── Paths ─────────────────────────────────────────────────────────────────────

BENCHMARK_DIR = Path("~/.deus/benchmarks").expanduser()
LONGMEMEVAL_CACHE = BENCHMARK_DIR / "longmemeval_s.json"
RESULTS_LOG = BENCHMARK_DIR / "results.jsonl"

LONGMEMEVAL_HF_DATASET = "xiaowu0162/longmemeval-cleaned"
LONGMEMEVAL_HF_FILENAME = "longmemeval_s_cleaned.json"
LONGMEMEVAL_GITHUB_RAW = (
    "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned"
    "/resolve/main/longmemeval_s_cleaned.json"
)

_SCRIPT_DIR = Path(__file__).resolve().parent
_INDEXER = _SCRIPT_DIR / "memory_indexer.py"

# ── Subprocess helpers ─────────────────────────────────────────────────────────


def _run_indexer(
    args: list[str],
    vault_path: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Run memory_indexer.py with an isolated vault so it uses a fresh DB.

    The indexer resolves DB_PATH as ~/.deus/memory.db and VAULT_SESSION_LOGS
    from DEUS_VAULT_PATH.  For benchmarks we point DEUS_VAULT_PATH to a
    temp directory that has its own Session-Logs layout.  The DB itself lives
    inside that temp dir as ~/.deus/memory.db — but since we override HOME via
    DEUS_VAULT_PATH the isolation is at the vault level.

    For the benchmark we use a dedicated HOME override so the indexer writes
    its DB to a temp location instead of ~/.deus/memory.db.
    """
    env = {**os.environ}
    if vault_path:
        env["DEUS_VAULT_PATH"] = vault_path
    return subprocess.run(
        [sys.executable, str(_INDEXER)] + args,
        capture_output=True,
        text=True,
        env=env,
    )


def _run_indexer_real(args: list[str]) -> subprocess.CompletedProcess:
    """Run memory_indexer.py against the REAL production vault/DB."""
    cmd = [sys.executable, str(_INDEXER)] + args
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"memory_indexer.py failed (rc={proc.returncode}): {proc.stderr.strip()}\n"
            f"command: {cmd}"
        )
    return proc


def _run_indexer_with_home(
    args: list[str],
    fake_home: str,
    vault_path: str,
) -> subprocess.CompletedProcess:
    """Run indexer with HOME overridden so DB_PATH resolves to fake_home/.deus/memory.db."""
    cmd = [sys.executable, str(_INDEXER)] + args
    env = {**os.environ, "HOME": fake_home, "DEUS_VAULT_PATH": vault_path}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"memory_indexer.py failed (rc={proc.returncode}): {proc.stderr.strip()}\n"
            f"command: {cmd}"
        )
    return proc


# ── Dataset download ──────────────────────────────────────────────────────────


def _download_longmemeval() -> list[dict]:
    """Return LongMemEval-S examples. Tries HuggingFace then GitHub raw URL.

    Result is cached to BENCHMARK_DIR/longmemeval_s.json.
    """
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)

    if LONGMEMEVAL_CACHE.exists():
        return json.loads(LONGMEMEVAL_CACHE.read_text())

    print("Downloading LongMemEval-S dataset...", flush=True)

    # 1. Try huggingface_hub
    try:
        from huggingface_hub import hf_hub_download  # type: ignore

        local = hf_hub_download(
            repo_id=LONGMEMEVAL_HF_DATASET,
            filename=LONGMEMEVAL_HF_FILENAME,
            repo_type="dataset",
            local_dir=str(BENCHMARK_DIR),
        )
        data = json.loads(Path(local).read_text())
        LONGMEMEVAL_CACHE.write_text(json.dumps(data))
        print(f"  Downloaded via huggingface_hub ({len(data)} examples)")
        return data
    except Exception as e:
        print(f"  huggingface_hub unavailable or failed ({e}), trying GitHub...")

    # 2. Fall back to direct GitHub raw download
    try:
        with urllib.request.urlopen(LONGMEMEVAL_GITHUB_RAW, timeout=60) as resp:
            raw = resp.read().decode()
        data = json.loads(raw)
        LONGMEMEVAL_CACHE.write_text(json.dumps(data))
        print(f"  Downloaded via GitHub ({len(data)} examples)")
        return data
    except Exception as e:
        print(f"ERROR: Could not download LongMemEval-S: {e}", file=sys.stderr)
        sys.exit(1)


# ── Metric calculations ───────────────────────────────────────────────────────


def recall_at_k(hits: list[bool], k: int) -> float:
    """Fraction of examples where the answer session appeared in top-k results."""
    if not hits:
        return 0.0
    return sum(1 for h in hits if h) / len(hits)


def mean_reciprocal_rank(ranks: list[Optional[int]]) -> float:
    """MRR from 1-based answer ranks (None means not found in results)."""
    if not ranks:
        return 0.0
    total = 0.0
    for r in ranks:
        if r is not None:
            total += 1.0 / r
    return total / len(ranks)


def _parse_query_output(output: str) -> list[str]:
    """Extract session paths from cmd_query stdout.

    Looks for lines of the form: "  (full log: /path/to/session.md)"
    """
    paths = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("(full log:"):
            path = stripped[len("(full log:"):].strip().rstrip(")")
            paths.append(path.strip())
    return paths


def _session_stem_to_id(result_paths: list[str], session_stems: list[str]) -> list[int]:
    """Map result path stems back to 0-based indices in session_stems list."""
    indices = []
    for rp in result_paths:
        stem = Path(rp).stem
        if stem in session_stems:
            indices.append(session_stems.index(stem))
    return indices


# ── Outbound mode: LongMemEval ─────────────────────────────────────────────────


def _extract_session_tldr(session: object) -> str:
    """Extract a meaningful tldr from session turns for better embedding quality."""
    if not isinstance(session, list) or not session:
        return "conversation session"
    # Collect all user turns — these are the "episodic memory" facts
    user_msgs = [
        t.get("content", "") for t in session
        if isinstance(t, dict) and t.get("role") == "user"
    ]
    # Join user messages, truncate to ~300 chars for the tldr
    combined = " | ".join(m.strip() for m in user_msgs if m.strip())
    return combined[:300] if combined else "conversation session"


def _write_session_md(
    session: object,
    session_date: str,
    qid: str,
    s_idx: int,
    dest: Path,
) -> None:
    """Write a single haystack session to a markdown file the indexer can parse."""
    tldr = _extract_session_tldr(session)
    lines = [
        "---",
        f"date: {session_date}",
        f"tldr: {tldr}",
        "topics: [benchmark]",
        "---",
        "",
    ]
    if isinstance(session, list):
        for turn in session:
            role = turn.get("role", "unknown") if isinstance(turn, dict) else "unknown"
            content = turn.get("content", "") if isinstance(turn, dict) else str(turn)
            lines.append(f"**{role}**: {content}")
            lines.append("")
    elif isinstance(session, str):
        lines.append(session)
    dest.write_text("\n".join(lines), encoding="utf-8")


def run_outbound(limit: int = 50, ks: list[int] = None) -> dict:
    """Run LongMemEval benchmark and return metric dict."""
    if ks is None:
        ks = [1, 3, 5, 10]
    max_k = max(ks)

    examples = _download_longmemeval()
    if limit:
        examples = examples[:limit]

    n = len(examples)
    print(f"Running LongMemEval-S on {n} examples (k={ks})...\n", flush=True)

    # hits[k] = list of bool per example; ranks = 1-based rank of first hit (or None)
    hits: dict[int, list[bool]] = {k: [] for k in ks}
    ranks: list[Optional[int]] = []
    t_start = time.monotonic()

    for ex_idx, example in enumerate(examples):
        qid = example.get("question_id", f"q_{ex_idx:04d}")
        question = example["question"]
        haystack: list = example.get("haystack_sessions", [])
        haystack_session_ids: list[str] = example.get("haystack_session_ids", [])
        haystack_dates: list[str] = example.get("haystack_dates", [])
        session_date = example.get("question_date", "2024-01-01")

        # Map string session IDs → 0-based haystack positions
        id_to_idx: dict[str, int] = {sid: i for i, sid in enumerate(haystack_session_ids)}
        answer_ids: set[int] = {
            id_to_idx[sid]
            for sid in example.get("answer_session_ids", [])
            if sid in id_to_idx
        }

        # Each example gets a completely isolated temp directory:
        #   tmpdir/home/   — overrides HOME so DB_PATH = tmpdir/home/.deus/memory.db
        #   tmpdir/vault/Session-Logs/  — DEUS_VAULT_PATH
        #   tmpdir/vault/Atoms/
        with tempfile.TemporaryDirectory(prefix="deus_bm_") as tmpdir:
            tmp = Path(tmpdir)
            fake_home = str(tmp / "home")
            vault_path = str(tmp / "vault")
            session_logs_dir = tmp / "vault" / "Session-Logs"
            (tmp / "vault" / "Atoms").mkdir(parents=True, exist_ok=True)
            session_logs_dir.mkdir(parents=True, exist_ok=True)
            (tmp / "home" / ".deus").mkdir(parents=True, exist_ok=True)

            session_stems: list[str] = []
            for s_idx, session in enumerate(haystack):
                stem = f"session_{s_idx:04d}"
                dest = session_logs_dir / f"{stem}.md"
                # Use per-session date when available; fall back to question_date
                raw_date = haystack_dates[s_idx] if s_idx < len(haystack_dates) else session_date
                # Normalise "2023/05/20 (Sat) 02:21" → "2023-05-20"
                s_date = raw_date.split(" ")[0].replace("/", "-") if raw_date else session_date
                _write_session_md(session, s_date, qid, s_idx, dest)
                session_stems.append(stem)

                # Index this session (skip atom extraction — not relevant for benchmarking)
                _run_indexer_with_home(
                    ["--add", str(dest), "--no-extract"],
                    fake_home=fake_home,
                    vault_path=vault_path,
                )

            # Query the isolated index
            proc = _run_indexer_with_home(
                ["--query", question, "--top", str(max_k)],
                fake_home=fake_home,
                vault_path=vault_path,
            )
            result_paths = _parse_query_output(proc.stdout)
            result_ids = _session_stem_to_id(result_paths, session_stems)

            # Compute metrics (answer_ids is already a set[int] of haystack positions)
            first_rank: Optional[int] = None
            for pos, rid in enumerate(result_ids[:max_k], start=1):
                if rid in answer_ids and first_rank is None:
                    first_rank = pos
            ranks.append(first_rank)

            for k in ks:
                hit = any(rid in answer_ids for rid in result_ids[:k])
                hits[k].append(hit)

        elapsed = time.monotonic() - t_start
        per_ex = elapsed / (ex_idx + 1)
        print(
            f"  [{ex_idx + 1}/{n}] {qid}: "
            f"rank={first_rank} "
            f"R@{max_k}={'Y' if hits[max_k][-1] else 'N'} "
            f"({per_ex:.1f}s/ex)",
            flush=True,
        )

    total_time = time.monotonic() - t_start
    mrr = mean_reciprocal_rank(ranks)
    recall = {k: recall_at_k(hits[k], k) for k in ks}

    return {
        "mode": "outbound",
        "n": n,
        "ks": ks,
        "recall": recall,
        "mrr": mrr,
        "total_time_s": total_time,
        "per_example_s": total_time / n if n else 0,
    }


def print_outbound_results(result: dict) -> None:
    n = result["n"]
    ks = result["ks"]
    recall = result["recall"]
    mrr = result["mrr"]
    t = result["total_time_s"]
    per_ex = result["per_example_s"]

    print(f"\n=== LongMemEval (n={n}) ===")
    for k in ks:
        r = recall[k]
        hit_count = round(r * n)
        print(f"Recall@{k:<2}: {r * 100:5.1f}% ({hit_count}/{n})")
    print(f"MRR:       {mrr:.2f}")
    print(f"Time:      {t:.0f}s ({per_ex:.1f}s/example)")


# ── Internal mode ─────────────────────────────────────────────────────────────


def _count_chars_in_recent_output(n: int, compact: bool) -> int:
    """Run --recent N [--compact] against real DB and return output char count."""
    args = ["--recent", str(n)]
    if compact:
        args.append("--compact")
    proc = _run_indexer_real(args)
    return len(proc.stdout)


def _load_vault_root() -> Optional[Path]:
    """Resolve DEUS_VAULT_PATH the same way memory_indexer does."""
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    config_path = Path("~/.config/deus/config.json").expanduser()
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text())
            if cfg.get("vault_path"):
                return Path(cfg["vault_path"]).expanduser()
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _sample_real_sessions(sample: int) -> list[dict]:
    """Sample up to `sample` queryable sessions from the real vault."""
    vault_root = _load_vault_root()
    if vault_root is None:
        return []

    session_logs_dir = vault_root / "Session-Logs"
    if not session_logs_dir.exists():
        return []

    log_files = [
        f for f in session_logs_dir.rglob("*.md")
        if ".obsidian" not in str(f)
    ]

    candidates = []
    for lf in log_files:
        try:
            content = lf.read_text(encoding="utf-8")
            fm_match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
            if not fm_match:
                continue
            fm_text = fm_match.group(1)
            # Topics are always a single-line list — most reliable query signal
            topics_m = re.search(r"^topics:\s*\[(.+?)\]", fm_text, re.MULTILINE)
            topics = topics_m.group(1).strip() if topics_m else ""
            # tldr may be inline or YAML block scalar (tldr: | \n  actual text)
            tldr_inline = re.search(r"^tldr:\s+([^|].*?)$", fm_text, re.MULTILINE)
            tldr_block = re.search(r"^tldr:\s*\|[-]?\n([ \t]+.+?)(?=\n\S|\Z)", fm_text, re.DOTALL)
            if tldr_inline:
                tldr = tldr_inline.group(1).strip()
            elif tldr_block:
                tldr = " ".join(tldr_block.group(1).split())[:200]
            else:
                tldr = ""
            query_text = (topics or tldr or "").strip()
            if len(query_text) > 10:
                candidates.append({"path": str(lf), "query": query_text})
        except OSError:
            continue

    if not candidates:
        return []

    # Sample evenly for variety
    if len(candidates) <= sample:
        return candidates
    step = len(candidates) // sample
    return [candidates[i * step] for i in range(sample)]


def run_internal(limit: int = 20) -> dict:
    """Run internal benchmarks: token efficiency + local recall@3 + pending accuracy."""
    print("Running internal benchmarks...\n", flush=True)

    # ── Token efficiency ─────────────────────────────────────────────────────
    n_sessions = 5
    print(f"  Token efficiency (recent {n_sessions} sessions)...", flush=True)
    full_chars = _count_chars_in_recent_output(n_sessions, compact=False)
    compact_chars = _count_chars_in_recent_output(n_sessions, compact=True)

    if full_chars > 0:
        reduction = (1 - compact_chars / full_chars) * 100
    else:
        reduction = 0.0

    # ── Local recall@3 ───────────────────────────────────────────────────────
    sample_n = min(limit, 20)
    print(f"  Local recall@3 (sample={sample_n})...", flush=True)
    sessions = _sample_real_sessions(sample_n)

    recall3_hits = 0
    recall3_total = 0
    for sess in sessions:
        proc = _run_indexer_real(["--query", sess["query"], "--top", "3"])
        result_paths = _parse_query_output(proc.stdout)
        sess_stem = Path(sess["path"]).stem
        hit = any(Path(rp).stem == sess_stem for rp in result_paths)
        if hit:
            recall3_hits += 1
        recall3_total += 1

    recall3_rate = recall3_hits / recall3_total if recall3_total else 0.0

    # ── Pending accuracy (CLAUDE.md) ─────────────────────────────────────────
    print("  Pending accuracy (CLAUDE.md)...", flush=True)
    vault_root = _load_vault_root()
    claude_md = (vault_root / "CLAUDE.md") if vault_root else Path(__file__).resolve().parent.parent / "CLAUDE.md"
    pending_items = 0
    all_checkbox_format = True
    pending_issues: list[str] = []

    if claude_md.exists():
        content = claude_md.read_text(encoding="utf-8")
        pending_section = re.search(
            r"(?i)#+\s*(pending|todo|backlog).*?\n(.*?)(?=\n#+|\Z)",
            content,
            re.DOTALL,
        )
        if pending_section:
            section_body = pending_section.group(2)
            items = re.findall(r"^\s*-\s+(.+)$", section_body, re.MULTILINE)
            pending_items = len(items)
            for item in items:
                if not re.match(r"\[[ x]\]", item):
                    all_checkbox_format = False
                    pending_issues.append(item[:60])

    return {
        "mode": "internal",
        "token_efficiency": {
            "full_chars": full_chars,
            "compact_chars": compact_chars,
            "reduction_pct": reduction,
            "sessions": n_sessions,
        },
        "local_recall": {
            "hits": recall3_hits,
            "total": recall3_total,
            "rate": recall3_rate,
        },
        "pending_accuracy": {
            "items": pending_items,
            "within_limit": pending_items <= 10,
            "all_checkbox_format": all_checkbox_format,
            "issues": pending_issues[:3],
        },
    }


def print_internal_results(result: dict) -> None:
    te = result["token_efficiency"]
    lr = result["local_recall"]
    pa = result["pending_accuracy"]

    print("\n=== Internal Benchmarks ===")

    print("\nToken efficiency:")
    # Rough token estimate: ~4 chars per token
    full_tok = te["full_chars"] // 4
    compact_tok = te["compact_chars"] // 4
    n = te["sessions"]
    print(f"  Full mode:    {full_tok:,} tokens ({n} sessions)")
    print(f"  Compact mode: {compact_tok:,} tokens ({n} sessions)")
    if te["full_chars"] > 0:
        print(f"  Reduction:    {te['reduction_pct']:.1f}%")
    else:
        print("  Reduction:    N/A (no session data returned)")

    print(f"\nLocal recall@3 (sample={lr['total']}):")
    if lr["total"] > 0:
        print(f"  Hit rate: {lr['rate'] * 100:.1f}% ({lr['hits']}/{lr['total']})")
    else:
        print("  Hit rate: N/A (no indexed sessions found in real DB)")

    print("\nPending accuracy:")
    items = pa["items"]
    if items == 0:
        print("  Items: none found in CLAUDE.md pending section")
    else:
        limit_mark = "within 10-item limit" if pa["within_limit"] else "EXCEEDS 10-item limit"
        limit_ok = "ok" if pa["within_limit"] else "FAIL"
        fmt_ok = "all [ ] ok" if pa["all_checkbox_format"] else "some missing [ ] format FAIL"
        print(f"  Items: {items} ({limit_mark}) [{limit_ok}]")
        print(f"  Format: {fmt_ok}")
        for issue in pa["issues"]:
            print(f"    - bad format: {issue}")


# ── Save results ──────────────────────────────────────────────────────────────


def save_results(result: dict) -> None:
    """Append result dict as JSONL to results.jsonl for trend tracking."""
    import datetime

    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **result,
    }
    with RESULTS_LOG.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"\nResults saved to {RESULTS_LOG}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deus memory retrieval benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--outbound", action="store_true", help="Run LongMemEval benchmark")
    mode.add_argument(
        "--internal", action="store_true", help="Run internal regression tests"
    )
    mode.add_argument("--all", action="store_true", help="Run both modes")

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max examples (default: 50 for --outbound, 20 for --internal)",
    )
    parser.add_argument(
        "--k",
        type=str,
        default="1,3,5,10",
        help="Comma-separated k values for recall@k (default: 1,3,5,10)",
    )
    parser.add_argument(
        "--save",
        action="store_true",
        help="Append results to ~/.deus/benchmarks/results.jsonl",
    )
    args = parser.parse_args()

    ks = [int(x.strip()) for x in args.k.split(",")]

    if args.outbound or args.all:
        limit = args.limit if args.limit is not None else 50
        result = run_outbound(limit=limit, ks=ks)
        print_outbound_results(result)
        if args.save:
            save_results(result)

    if args.internal or args.all:
        limit = args.limit if args.limit is not None else 20
        result = run_internal(limit=limit)
        print_internal_results(result)
        if args.save:
            save_results(result)


if __name__ == "__main__":
    main()

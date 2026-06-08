"""
Build a clean, current-rubric, Gemini-labeled judge benchmark fixture.

Why this exists (see docs/decisions/judge-lora-specialization.md): the only prior
reference set was n=37 with stale personalization labels and zero unsafe examples,
and the live DB's stored scores are mostly local-judge (no provider column), so
they can't serve as Gemini ground truth. This builder samples ~200 diverse real
interactions and **freshly** labels each with the production Gemini judge under the
CURRENT rubric, with the persona digest injected so personalization labels are real
(not the pre-#710 hallucination). The output feeds `evolution/benchmark_judge.py
--fixture` for per-dimension agreement + bootstrap CIs.

Privacy: the fixture contains real user prompts/responses (PII) → it is written under
`finetune/judge-bench/` which is gitignored. This script is public-repo-safe (no
hardcoded personal paths; digest resolved via the env/config chain, fail-soft).

Usage:
    python3 -m evolution.build_judge_benchmark [--limit 200] [--out PATH] [--seed N]

Read-only on the evolution DB (sampling only — no DELETE/DROP/schema change), per the
no-db-deletion ADR.
"""
import argparse
import json
import random
import sys
from pathlib import Path
from typing import Optional

from .config import load_api_key
from .persona import get_digest
from .storage import get_storage
from .benchmark_judge import _is_noise
from .judge.gemini_judge import GeminiRuntimeJudge
from .judge.criteria import RUBRIC

# Composite-score sampling bands. Live counts (2026-06): [0,0.4):13, [0.4,0.6):564,
# [0.6,0.85):181, [0.85,1.0]:916 — the bottom band is scarce, so the allocator takes
# all of it and redistributes the remainder across the populous bands.
BANDS = [(0.0, 0.4), (0.4, 0.6), (0.6, 0.85), (0.85, 1.01)]
DIMS = ("quality", "safety", "tool_use", "personalization")

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = _REPO_ROOT / "finetune" / "judge-bench" / "fixture-v1.jsonl"


def _band_index(score: float) -> int:
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= score < hi:
            return i
    return len(BANDS) - 1


def allocate(avail: list[int], limit: int) -> list[int]:
    """Even split across bands, redistributing any band's shortfall to bands with
    spare capacity (round-robin). Guarantees sum(alloc) == min(limit, sum(avail))."""
    n = len(avail)
    alloc = [0] * n
    base = limit // n
    for i in range(n):
        alloc[i] = min(avail[i], base)
    # redistribute leftover to bands that still have capacity
    leftover = limit - sum(alloc)
    progress = True
    while leftover > 0 and progress:
        progress = False
        for i in range(n):
            if leftover <= 0:
                break
            if alloc[i] < avail[i]:
                alloc[i] += 1
                leftover -= 1
                progress = True
    return alloc


def sample_interactions(limit: int, seed: int) -> list[dict]:
    """Diverse, seed-reproducible sample stratified across composite bands.

    One DB read (read-only), Python bucketing (no band-boundary overlap), then
    `random.Random(seed).sample` within each band — `get_recent_interactions` is
    ORDER BY timestamp DESC with no randomization, so without this we'd get the
    newest-N (temporally clustered), not a diverse sample.
    """
    store = get_storage()
    rows = store.get_recent_interactions(limit=100_000, eval_suite=None, min_score=0.0)
    # Require a non-empty response: ~60% of stored interactions have an empty response
    # (system callbacks / uncaptured turns) — Gemini has nothing to grade on those, and
    # they form the all-0.0 composite cluster. Excluding them is what makes the labels
    # gradable; note it leaves the [0,0.4) composite band empty (no gradable low-scorers).
    clean = [
        r for r in rows
        if (r.get("response") or "").strip()
        and not _is_noise(r.get("prompt", ""), r.get("response", "") or "")
    ]

    buckets: list[list[dict]] = [[] for _ in BANDS]
    for r in clean:
        buckets[_band_index(float(r["judge_score"]))].append(r)

    avail = [len(b) for b in buckets]
    alloc = allocate(avail, limit)
    rng = random.Random(seed)
    sampled: list[dict] = []
    for b, k in zip(buckets, alloc):
        sampled.extend(rng.sample(b, k) if k < len(b) else list(b))
    rng.shuffle(sampled)
    return sampled


def build_record(row: dict, result, digest: Optional[str], rubric_version: int) -> dict:
    """Serialize one freshly-labeled interaction into a fixture record."""
    tools = json.loads(row["tools_used"]) if row.get("tools_used") else None
    return {
        "id": row["id"],
        "prompt": row.get("prompt", ""),
        "response": row.get("response", "") or "",
        "tools_used": tools,
        "group_folder": row.get("group_folder"),
        "eval_suite": row.get("eval_suite"),
        "has_code": bool(row.get("has_code")),
        "gemini_dims": {d: getattr(result, d) for d in DIMS},
        "gemini_composite": result.score,
        "is_parse_error": result.is_parse_error,
        "rubric_version": rubric_version,
        # Stored so the grading harness reproduces label-time digest at grade-time
        # (digest is work-style-only + capped per #710; fixture is gitignored).
        "digest_text": digest or "",
        "digest_injected": bool(digest),
    }


def load_existing_ids(out_path: Path) -> set[str]:
    if not out_path.exists():
        return set()
    ids: set[str] = set()
    for line in out_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ids.add(json.loads(line)["id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return ids


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a Gemini-labeled judge benchmark fixture")
    parser.add_argument("--limit", type=int, default=200, help="Target number of records (default: 200)")
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT), help="Output JSONL path (gitignored dir)")
    parser.add_argument("--seed", type=int, default=20260607, help="Sampling seed (reproducible)")
    args = parser.parse_args()

    # Preflight: fail fast + clear if the labeler can't run.
    try:
        load_api_key()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    digest = get_digest()  # fail-soft → None; warn loudly since it gates personalization validity
    if not digest:
        print("WARNING: no persona digest resolved — personalization labels will be the "
              "ungradable (hallucination-prone) variety. Set DEUS_VAULT_PATH / config vault_path "
              "with a Persona/INDEX.md work-style section for a valid personalization benchmark.",
              file=sys.stderr)

    out_path = Path(args.out).expanduser()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    done = load_existing_ids(out_path)
    if done:
        print(f"[resume] {len(done)} records already in {out_path.name}; skipping those.")

    sampled = sample_interactions(args.limit, args.seed)
    rubric_version = 1  # current structured-rubric schema (criteria.RUBRIC)
    assert RUBRIC  # sanity: rubric importable (used by the production judge prompt builder)
    judge = GeminiRuntimeJudge()

    # Report the sample composition (diversity sanity check).
    band_counts = [0] * len(BANDS)
    suite_counts: dict[str, int] = {}
    for r in sampled:
        band_counts[_band_index(float(r["judge_score"]))] += 1
        suite_counts[r.get("eval_suite") or "?"] = suite_counts.get(r.get("eval_suite") or "?", 0) + 1
    print(f"[sample] {len(sampled)} interactions | bands {band_counts} "
          f"({[f'{lo}-{hi}' for lo, hi in BANDS]}) | suites {suite_counts}")

    written = 0
    parse_fail = 0
    with out_path.open("a", encoding="utf-8") as f:
        for i, row in enumerate(sampled, 1):
            if row["id"] in done:
                continue
            result = judge.evaluate(
                prompt=row.get("prompt", ""),
                response=row.get("response", "") or "",
                tools_used=json.loads(row["tools_used"]) if row.get("tools_used") else None,
                user_profile=digest,
            )
            if result.is_parse_error:
                parse_fail += 1
            rec = build_record(row, result, digest, rubric_version)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            written += 1
            pv = {d: round(rec["gemini_dims"][d], 2) for d in DIMS}
            print(f"  [{i}/{len(sampled)}] {row['id'][:12]} dims={pv} comp={rec['gemini_composite']:.2f}", flush=True)

    # Personalization-variance check: the whole point of digest injection.
    pvals = []
    for line in out_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                pvals.append(json.loads(line)["gemini_dims"]["personalization"])
            except (json.JSONDecodeError, KeyError):
                pass
    uniq = sorted(set(round(v, 3) for v in pvals))
    print(f"\n[done] wrote {written} new (total {len(done)+written}) → {out_path}")
    print(f"[done] parse_fail={parse_fail}/{written} | personalization unique values={uniq}")
    if len(uniq) <= 1:
        print("WARNING: personalization labels are constant — digest injection may not be working.",
              file=sys.stderr)


if __name__ == "__main__":
    main()

"""
paraphrased-query bench
=======================
Measures embedding robustness to phrasing. Each fixture pairs a baseline query
(the "canonical" way someone might describe a session) with a paraphrased query
(a different phrasing of the same intent), and checks whether the memory
indexer still retrieves the expected session within top-k.

Score = recall@k on the paraphrased query. An optional baseline pass (with the
canonical query) is recorded alongside so a drop indicates paraphrase weakness,
not retrieval regression in general.

Fixture format: scripts/bench/fixtures/paraphrased_queries.json
  [
    {
      "id": "case-1",
      "expected_path_stem": "2026-04-18-memory-tree-reactions-ollama-catchup-hook",
      "canonical": "memory tree raw retrieve bench",
      "paraphrase": "switching default memory retrieval off policy-based mode"
    },
    ...
  ]

Paths don't have to exist — missing paths score 0 but don't crash. That keeps
the fixture editable without live vault coupling.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_MB_PATH = _SCRIPTS_DIR / "memory_benchmark.py"
_FIXTURE_PATH = _SCRIPTS_DIR / "bench" / "fixtures" / "paraphrased_queries.json"


def _load_mb():
    if "memory_benchmark" in sys.modules:
        return sys.modules["memory_benchmark"]
    spec = importlib.util.spec_from_file_location("memory_benchmark", _MB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


def _query_rank(mb, query: str, expected_stem: str, top_k: int) -> int | None:
    proc = mb._run_indexer_real(["--query", query, "--top", str(top_k)])
    paths = mb._parse_query_output(proc.stdout)
    for pos, p in enumerate(paths[:top_k], start=1):
        if Path(p).stem == expected_stem:
            return pos
    return None


@register("paraphrased-query")
def run_paraphrased_query(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="paraphrased-query")
    p.add_argument(
        "--fixture",
        default=str(_FIXTURE_PATH),
        help="Path to fixture JSON (default: scripts/bench/fixtures/paraphrased_queries.json)",
    )
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--compare-canonical", action="store_true", help="Also query with canonical phrasing for baseline")
    args = p.parse_args(argv)

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        return RunResult(
            suite="paraphrased-query",
            score=0.0,
            cases=[],
            meta={"error": f"fixture not found: {fixture_path}"},
        )

    with fixture_path.open("r", encoding="utf-8") as f:
        fixtures = json.load(f)

    mb = _load_mb()
    t_start = time.monotonic()

    cases: list[CaseResult] = []
    hits = 0
    baseline_hits = 0

    for fx in fixtures:
        case_id = fx["id"]
        expected_stem = fx["expected_path_stem"]
        paraphrase = fx["paraphrase"]
        canonical = fx.get("canonical", "")

        rank = _query_rank(mb, paraphrase, expected_stem, args.top_k)
        hit = rank is not None
        if hit:
            hits += 1

        meta: dict = {"rank": rank, "paraphrase": paraphrase}

        if args.compare_canonical and canonical:
            baseline_rank = _query_rank(mb, canonical, expected_stem, args.top_k)
            if baseline_rank is not None:
                baseline_hits += 1
            meta["baseline_rank"] = baseline_rank

        cases.append(
            CaseResult(
                case_id=case_id,
                score=1.0 if hit else 0.0,
                passed=hit,
                meta=meta,
            )
        )

    total = len(fixtures)
    rate = hits / total if total else 0.0
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    suite_meta: dict = {"paraphrased_hits": hits, "total": total}
    if args.compare_canonical:
        suite_meta["baseline_hits"] = baseline_hits
        suite_meta["baseline_rate"] = baseline_hits / total if total else 0.0
        suite_meta["paraphrase_retention"] = (
            hits / baseline_hits if baseline_hits else 0.0
        )

    return RunResult(
        suite="paraphrased-query",
        score=rate,
        cases=cases,
        latency_ms=elapsed_ms,
        meta=suite_meta,
    )

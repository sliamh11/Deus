"""
reflexion-retrieval-quality bench
==================================
Measures whether the reflexion retrieval pipeline surfaces relevant past
reflections for a given (query, group_folder, tools_planned) triple.

For each fixture case, ``get_reflections`` is called with top_k results.
A case is a hit if ANY returned reflection's ``content`` field contains ALL
strings listed in ``expected_reflection_contains`` (case-insensitive).

Score = mean hit rate across all cases.

Fixture format: scripts/bench/fixtures/reflexion_retrieval.json
  [
    {
      "id": "case-1",
      "query": "how to debug container agent logs",
      "group_folder": "whatsapp_main",
      "tools_planned": ["bash", "read"],
      "expected_reflection_contains": ["container", "log"],
      "top_k": 5
    },
    ...
  ]

Missing vault / import errors are handled gracefully: the suite returns score
0.0 with an ``error`` key in meta rather than crashing.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from ..registry import register
from ..types import CaseResult, RunResult

_FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "reflexion_retrieval.json"

_SUITE_NAME = "reflexion-retrieval"


def _load_retriever():
    """Import get_reflections from the reflexion retriever module."""
    try:
        from evolution.reflexion.retriever import get_reflections  # noqa: PLC0415
        return get_reflections
    except Exception as exc:  # noqa: BLE001
        return exc


def _case_hit(reflections: list[dict], expected_contains: list[str]) -> bool:
    """Return True if any reflection's content contains ALL expected strings (case-insensitive)."""
    needles = [s.lower() for s in expected_contains]
    for r in reflections:
        content = (r.get("content") or "").lower()
        if all(needle in content for needle in needles):
            return True
    return False


@register(_SUITE_NAME)
def run_reflexion_retrieval(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog=_SUITE_NAME)
    p.add_argument(
        "--fixture",
        default=str(_FIXTURE_PATH),
        help="Path to fixture JSON (default: scripts/bench/fixtures/reflexion_retrieval.json)",
    )
    args = p.parse_args(argv)

    fixture_path = Path(args.fixture)
    if not fixture_path.exists():
        return RunResult(
            suite=_SUITE_NAME,
            score=0.0,
            cases=[],
            meta={"error": f"fixture not found: {fixture_path}"},
        )

    with fixture_path.open("r", encoding="utf-8") as f:
        fixtures = json.load(f)

    get_reflections = _load_retriever()
    if isinstance(get_reflections, Exception):
        # Fail gracefully — document the import error but don't crash
        return RunResult(
            suite=_SUITE_NAME,
            score=0.0,
            cases=[
                CaseResult(
                    case_id=fx["id"],
                    score=0.0,
                    passed=False,
                    meta={"error": f"import failed: {get_reflections}"},
                )
                for fx in fixtures
            ],
            meta={"error": f"retriever import failed: {get_reflections}"},
        )

    t_start = time.monotonic()
    cases: list[CaseResult] = []
    hits = 0

    for fx in fixtures:
        case_id = fx["id"]
        query = fx["query"]
        group_folder = fx.get("group_folder")
        tools_planned = fx.get("tools_planned") or []
        expected_contains = fx.get("expected_reflection_contains", [])
        top_k = fx.get("top_k", 5)

        try:
            reflections = get_reflections(
                query=query,
                group_folder=group_folder,
                tools_planned=tools_planned or None,
                top_k=top_k,
            )
        except Exception as exc:  # noqa: BLE001
            cases.append(
                CaseResult(
                    case_id=case_id,
                    score=0.0,
                    passed=False,
                    meta={"error": str(exc), "query": query},
                )
            )
            continue

        hit = _case_hit(reflections, expected_contains)
        if hit:
            hits += 1

        cases.append(
            CaseResult(
                case_id=case_id,
                score=1.0 if hit else 0.0,
                passed=hit,
                meta={
                    "query": query,
                    "group_folder": group_folder,
                    "top_k": top_k,
                    "retrieved_count": len(reflections),
                    "expected_reflection_contains": expected_contains,
                },
            )
        )

    total = len(fixtures)
    score = hits / total if total else 0.0
    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    return RunResult(
        suite=_SUITE_NAME,
        score=score,
        cases=cases,
        latency_ms=elapsed_ms,
        meta={"hits": hits, "total": total},
    )

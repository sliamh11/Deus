import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_MT_PATH = _SCRIPTS_DIR / "memory_tree.py"
_DEFAULT_DATASET = _SCRIPTS_DIR / "tests" / "fixtures" / "memory_tree_queries.jsonl"


def _load_mt():
    if "memory_tree" in sys.modules:
        return sys.modules["memory_tree"]
    spec = importlib.util.spec_from_file_location("memory_tree", _MT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


def _expected_paths(item: dict[str, Any]) -> list[str]:
    if item.get("expected_paths"):
        return item["expected_paths"]
    if item.get("expected_path"):
        return [item["expected_path"]]
    return []


def _score_item_policy(
    mt: Any,
    db: Any,
    item: dict[str, Any],
) -> CaseResult:
    q = item["query"]
    expected = _expected_paths(item)
    expect_abstain = bool(item.get("abstain"))
    case_id = item.get("id", q[:60])

    result = mt.retrieve_with_policy(db, q)

    returned = [r["path"] for r in result["results"]]
    fell_back = result["fell_back"]
    top_path = returned[0] if returned else None

    if expect_abstain:
        hit = fell_back
    else:
        hit = any(p in returned for p in expected) if expected else False

    score = 1.0 if hit else 0.0
    abstained = fell_back and not expect_abstain

    return CaseResult(
        case_id=case_id,
        score=score,
        passed=hit,
        meta={
            "abstained": abstained,
            "top_path": top_path,
            "expected_path": expected[0] if expected else None,
            "tag": item.get("tag"),
        },
    )


def _score_item_raw(
    mt: Any,
    db: Any,
    item: dict[str, Any],
) -> CaseResult:
    q = item["query"]
    expected = _expected_paths(item)
    expect_abstain = bool(item.get("abstain"))
    case_id = item.get("id", q[:60])

    result = mt.retrieve(db, q)

    returned = [r["path"] for r in result["results"]]
    fell_back = result["fell_back"]
    top_path = returned[0] if returned else None

    if expect_abstain:
        hit = fell_back
    else:
        hit = any(p in returned for p in expected) if expected else False

    score = 1.0 if hit else 0.0
    abstained = fell_back and not expect_abstain

    return CaseResult(
        case_id=case_id,
        score=score,
        passed=hit,
        meta={
            "abstained": abstained,
            "top_path": top_path,
            "expected_path": expected[0] if expected else None,
            "tag": item.get("tag"),
        },
    )


@register("memory_tree")
def run_memory_tree(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="memory_tree")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of questions evaluated (default: all)")
    p.add_argument("--dataset", type=Path, default=_DEFAULT_DATASET,
                   help="Path to JSONL benchmark dataset")
    policy_grp = p.add_mutually_exclusive_group()
    policy_grp.add_argument("--policy", dest="policy", action="store_true", default=False,
                            help="Use retrieve_with_policy (opt-in as of 2026-04-18; raw retrieve is default)")
    policy_grp.add_argument("--no-policy", dest="policy", action="store_false",
                            help="Use raw retrieve instead of retrieve_with_policy (default)")
    args = p.parse_args(argv)

    mt = _load_mt()
    db = mt.open_db()

    dataset = _load_dataset(args.dataset)
    if args.limit is not None:
        dataset = dataset[: args.limit]

    t_start = time.monotonic()

    cases: list[CaseResult] = []
    score_fn = _score_item_policy if args.policy else _score_item_raw

    for item in dataset:
        cases.append(score_fn(mt, db, item))

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    n = len(cases)
    hits = sum(1 for c in cases if c.passed)
    abstain_count = sum(1 for c in cases if c.meta.get("abstained"))
    recall = hits / n if n else 0.0

    return RunResult(
        suite="memory_tree",
        score=recall,
        cases=cases,
        latency_ms=elapsed_ms,
        meta={
            "policy": args.policy,
            "n": n,
            "abstain_count": abstain_count,
            "dataset": str(args.dataset),
        },
    )

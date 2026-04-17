import argparse
import importlib.util
import sys
import time
from pathlib import Path

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_MB_PATH = _SCRIPTS_DIR / "memory_benchmark.py"


def _load_mb():
    if "memory_benchmark" in sys.modules:
        return sys.modules["memory_benchmark"]
    spec = importlib.util.spec_from_file_location("memory_benchmark", _MB_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_benchmark"] = mod
    spec.loader.exec_module(mod)
    return mod


@register("memory")
def run_memory(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="memory")
    p.add_argument("--mode", choices=["outbound", "internal"], default="outbound")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--k", default="1,3,5,10")
    args = p.parse_args(argv)

    mb = _load_mb()
    t_start = time.monotonic()

    if args.mode == "outbound":
        ks = [int(x) for x in args.k.split(",") if x.strip()]
        result = mb.run_outbound(limit=args.limit, ks=ks)
        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        recall = result["recall"]
        # score = recall@5 if present, else the highest available k
        score_k = 5 if 5 in recall else max(recall)
        score = recall[score_k]
        n = result["n"]

        cases: list[CaseResult] = []
        # We don't have per-case detail from run_outbound — build synthetic cases
        # per-k showing recall rate as a single summary case each.
        for k, rate in recall.items():
            hit_count = round(rate * n)
            cases.append(CaseResult(
                case_id=f"recall_at_{k}",
                score=rate,
                passed=rate > 0.0,
                meta={"k": k, "hits": hit_count, "n": n},
            ))

        return RunResult(
            suite="memory",
            score=score,
            cases=cases,
            latency_ms=elapsed_ms,
            meta={"mode": "outbound", "mrr": result["mrr"], "n": n},
        )

    else:
        result = mb.run_internal(limit=args.limit)
        elapsed_ms = int((time.monotonic() - t_start) * 1000)

        lr = result["local_recall"]
        te = result["token_efficiency"]
        pa = result["pending_accuracy"]

        score = lr["rate"]

        cases = [
            CaseResult(
                case_id="local_recall",
                score=lr["rate"],
                passed=lr["rate"] > 0.0,
                meta={"hits": lr["hits"], "total": lr["total"]},
            ),
            CaseResult(
                case_id="token_efficiency",
                score=1.0,
                meta={
                    "full_chars": te["full_chars"],
                    "compact_chars": te["compact_chars"],
                    "reduction_pct": te["reduction_pct"],
                },
            ),
            CaseResult(
                case_id="pending_accuracy",
                score=1.0 if pa["within_limit"] and pa["all_checkbox_format"] else 0.0,
                passed=pa["within_limit"] and pa["all_checkbox_format"],
                meta={
                    "items": pa["items"],
                    "within_limit": pa["within_limit"],
                    "all_checkbox_format": pa["all_checkbox_format"],
                },
            ),
        ]

        return RunResult(
            suite="memory",
            score=score,
            cases=cases,
            latency_ms=elapsed_ms,
            meta={"mode": "internal"},
        )

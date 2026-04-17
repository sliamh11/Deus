import argparse
import importlib.util
import sys
import time
from pathlib import Path

from ..registry import register
from ..types import CaseResult, RunResult

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent
_HARNESS_PATH = _SCRIPTS_DIR / "token_bench" / "harness.py"


def _load_harness():
    mod_name = "token_bench_harness"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@register("token")
def run_token(argv: list[str]) -> RunResult:
    p = argparse.ArgumentParser(prog="token")
    p.add_argument("--label", default="bench-cli")
    args = p.parse_args(argv)

    h = _load_harness()
    t_start = time.monotonic()

    files: dict = {}
    for key, rel in h.STATIC_CONTEXT_TARGETS:
        files[key] = h.file_info(h.REPO / rel)

    scenarios: dict = {}
    for name, keys in h.SCENARIOS.items():
        total_chars = sum(files[k].get("chars", 0) for k in keys if files[k].get("exists"))
        scenarios[name] = {
            "components": keys,
            "total_chars": total_chars,
            "est_tokens": h.est_tokens(total_chars),
        }

    elapsed_ms = int((time.monotonic() - t_start) * 1000)

    cases: list[CaseResult] = []
    total_tokens_in = 0
    for name, s in scenarios.items():
        est = s["est_tokens"]
        total_tokens_in += est
        cases.append(CaseResult(
            case_id=name,
            score=1.0,
            tokens_in=est,
            meta={"chars": s["total_chars"], "components": s["components"]},
        ))

    return RunResult(
        suite="token",
        score=1.0,
        cases=cases,
        tokens_in=total_tokens_in,
        latency_ms=elapsed_ms,
        meta={"label": args.label, "chars_per_token": h.CHARS_PER_TOKEN},
    )

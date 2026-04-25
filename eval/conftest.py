"""
Pytest fixtures and helpers shared across all DeepEval test suites.
"""

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest
from deepeval.test_case import LLMTestCase

from agent_wrapper import AgentResponse, invoke_agent
from deepeval.models import DeepEvalBaseLLM
from judge_model import make_judge

EVAL_DIR = Path(__file__).parent
DATASETS_DIR = EVAL_DIR / "datasets"
THRESHOLDS_FILE = EVAL_DIR / "thresholds.json"

# How many containers to run concurrently during pre-warm.
#
# Containers are I/O-bound (waiting on Anthropic API), so thread count can
# safely exceed CPU core count — but two other limits apply:
#   1. Memory: each container runs Node.js + Claude Code CLI (~300-500MB).
#      Too many simultaneous containers can exhaust RAM on smaller machines.
#   2. API rate limits: flooding the Anthropic API causes 429s + retries,
#      which makes things slower, not faster.
#
# Default: cpu_count // 2, capped at 8. Scales with the machine while leaving
# headroom for the OS and avoiding API saturation.
# Override with DEUS_EVAL_CONCURRENT=N for explicit control.
_concurrent_env = os.environ.get("DEUS_EVAL_CONCURRENT", "")
EVAL_CONCURRENT: int = int(_concurrent_env) if _concurrent_env else max(1, min(os.cpu_count() or 1, 8) // 2)

# Known dataset names. Pre-warm loads all of them regardless of which test
# files are being run so the cache is always fully populated.
_ALL_DATASETS = ["core_qa", "tool_use", "safety"]

# When DEUS_PARITY_TEST=1, tests run against both backends.
PARITY_MODE = os.environ.get("DEUS_PARITY_TEST", "") == "1"
EVAL_BACKENDS: list[str] = ["claude", "openai"] if PARITY_MODE else ["claude"]


def load_thresholds() -> dict:
    if THRESHOLDS_FILE.exists():
        return json.loads(THRESHOLDS_FILE.read_text())
    return {}


def load_dataset(name: str) -> list[dict]:
    """Load a JSONL dataset from datasets/<name>.jsonl."""
    path = DATASETS_DIR / f"{name}.jsonl"
    cases = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            cases.append(json.loads(line))
    return cases


def make_test_case(case_data: dict, response: AgentResponse) -> LLMTestCase:
    """Convert a dataset entry + agent response into a DeepEval LLMTestCase."""
    return LLMTestCase(
        input=case_data["input"],
        actual_output=response.result or response.error or "",
        expected_output=case_data.get("expected_output", ""),
        retrieval_context=case_data.get("retrieval_context") or None,
        context=([case_data["context"]] if case_data.get("context") else None),
    )


@pytest.fixture(scope="session")
def thresholds() -> dict:
    return load_thresholds()


@pytest.fixture(scope="session")
def judge() -> DeepEvalBaseLLM:
    """Session-scoped judge model (OllamaJudge preferred, GeminiJudge fallback)."""
    return make_judge()


@pytest.fixture(scope="session")
def agent():
    """
    Session-scoped agent fixture with thread-safe response cache.

    Returns a callable with the same signature as invoke_agent, but caches
    responses by (prompt, backend) so multiple metrics on the same case reuse
    one container invocation instead of spawning a new one each time.

    Thread-safe: safe to call from the parallel pre-warm fixture below.
    """
    _cache: dict[tuple[str, str], AgentResponse] = {}
    _lock = threading.Lock()

    def _cached_invoke(prompt: str, **kwargs) -> AgentResponse:
        backend = kwargs.get("backend", "claude")
        cache_key = (prompt, backend)
        if cache_key in _cache:
            return _cache[cache_key]
        with _lock:
            if cache_key not in _cache:
                _cache[cache_key] = invoke_agent(prompt, **kwargs)
        return _cache[cache_key]

    return _cached_invoke


@pytest.fixture(scope="session", autouse=True)
def warm_agent_cache(agent, request) -> None:
    """
    Pre-warm the agent cache by running unique prompts for collected test files.

    Only warms datasets that correspond to the test files actually being run.
    Pattern: test_{name}.py → datasets/{name}.jsonl. This avoids warming all
    40 prompts when running a single file (e.g. test_core_qa.py → ~13 prompts).

    Runs at session start (autouse) so every test hits the cache instantly.
    Containers are invoked DEUS_EVAL_CONCURRENT at a time (default 4).

    Without this, containers run serially (one per test), making the full
    suite take hours. With parallel pre-warming, total time ≈ max(container
    latency) × ceil(unique_prompts / concurrency) instead of their sum.
    """
    # Derive which datasets to warm from the collected test file stems.
    # test_core_qa.py → "core_qa", test_tool_use.py → "tool_use", etc.
    collected_stems = {Path(item.fspath).stem for item in request.session.items}
    active_datasets = [
        name for name in _ALL_DATASETS
        if f"test_{name}" in collected_stems
    ]

    all_prompts: set[str] = set()
    for name in active_datasets:
        path = DATASETS_DIR / f"{name}.jsonl"
        if not path.exists():
            continue
        for case in load_dataset(name):
            all_prompts.add(case["input"])

    if not all_prompts:
        return

    # In parity mode, warm each prompt for every backend.
    warm_tasks: list[tuple[str, str]] = [
        (p, b) for p in all_prompts for b in EVAL_BACKENDS
    ]

    print(
        f"\n[warmup] Pre-warming {len(warm_tasks)} tasks "
        f"({len(all_prompts)} prompts × {len(EVAL_BACKENDS)} backends) "
        f"from {active_datasets} "
        f"({EVAL_CONCURRENT} concurrent, {os.cpu_count()} logical CPUs)..."
    )

    completed = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=EVAL_CONCURRENT) as pool:
        futures = {
            pool.submit(agent, p, backend=b): (p, b) for p, b in warm_tasks
        }
        for future in as_completed(futures):
            prompt, backend = futures[future]
            try:
                result = future.result()
                completed += 1
                status = "ok" if result.status == "success" else f"err:{result.error[:40]}"
                print(f"[warmup] {completed}/{len(warm_tasks)} [{backend}] {status}")
            except Exception as exc:
                failed += 1
                print(f"[warmup] FAILED [{backend}]: {exc}")

    print(f"[warmup] Done — {completed} ok, {failed} failed.\n")

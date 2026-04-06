"""
DeepEval judge model for Deus eval suites.

Uses the JudgeRegistry provider pattern to resolve the best available backend.
Supports EVAL_JUDGE env var for explicit selection (mock/ollama/gemini/claude).

Usage:
    from judge_model import make_judge
    metric = GEval(..., model=make_judge())
"""
import os
import sys
from pathlib import Path
from typing import Optional

from deepeval.models import DeepEvalBaseLLM

# Add project root to path so `evolution` package is importable from eval/
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from evolution.judge import JudgeRegistry, NoProviderAvailableError  # noqa: E402


def make_judge(model: Optional[str] = None) -> DeepEvalBaseLLM:
    """
    Return the best available judge.

    - EVAL_JUDGE=mock   -> MockProvider
    - EVAL_JUDGE=ollama -> OllamaProvider
    - EVAL_JUDGE=gemini -> GeminiProvider
    - EVAL_JUDGE=claude -> ClaudeProxyProvider
    - If not set, auto-detect by priority (ollama > gemini > claude)
    """
    eval_judge = os.environ.get("EVAL_JUDGE", "").lower()
    preference = eval_judge if eval_judge else None

    try:
        registry = JudgeRegistry.default()
        provider = registry.resolve(preference)
        return provider.make_deepeval_judge(model)
    except NoProviderAvailableError:
        # Last resort: Claude proxy (kept for legacy compatibility)
        from evolution.judge.providers.claude_proxy import ClaudeProxyJudge
        return ClaudeProxyJudge(model=model or os.environ.get("DEEPEVAL_JUDGE_MODEL", "claude-sonnet-4-5"))

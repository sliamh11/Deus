from .base import BaseJudge, JudgeResult
from .provider import JudgeProvider, JudgeRegistry, NoProviderAvailableError

# Legacy exports (backward compat)
from .gemini_judge import GeminiJudge, GeminiRuntimeJudge
from .ollama_judge import OllamaJudge, OllamaRuntimeJudge, is_ollama_available

# Register built-in providers on import
from . import providers as _providers  # noqa: F401

from typing import Optional
from deepeval.models import DeepEvalBaseLLM


def make_deepeval_judge(model: Optional[str] = None, provider: Optional[str] = None) -> DeepEvalBaseLLM:
    """Resolve best provider and return a DeepEval judge."""
    return JudgeRegistry.default().resolve(provider).make_deepeval_judge(model)


def make_runtime_judge(model: Optional[str] = None, provider: Optional[str] = None) -> BaseJudge:
    """Resolve best provider and return a runtime judge."""
    return JudgeRegistry.default().resolve(provider).make_runtime_judge(model)


__all__ = [
    "BaseJudge", "JudgeResult",
    "JudgeProvider", "JudgeRegistry", "NoProviderAvailableError",
    "GeminiJudge", "GeminiRuntimeJudge",
    "OllamaJudge", "OllamaRuntimeJudge", "is_ollama_available",
    "make_deepeval_judge", "make_runtime_judge",
]

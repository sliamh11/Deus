from .base import BaseJudge, JudgeResult
from .provider import JudgeProvider, JudgeRegistry, NoProviderAvailableError

# Legacy exports (backward compat)
from .gemini_judge import GeminiRuntimeJudge
from .ollama_judge import OllamaRuntimeJudge, is_ollama_available
from .llama_cpp_judge import LlamaCppRuntimeJudge, is_llama_cpp_available

# Register built-in providers on import
from . import providers as _providers  # noqa: F401

from typing import Optional


def make_runtime_judge(model: Optional[str] = None, provider: Optional[str] = None) -> BaseJudge:
    """Resolve best provider and return a runtime judge.

    When EVOLUTION_OBSERVERS is configured the judge is wrapped for
    observability (see evolution/observability.py); otherwise it is returned
    unwrapped — zero overhead for the default case.
    """
    from ..observability import wrap_judge

    resolved = JudgeRegistry.default().resolve(provider)
    return wrap_judge(resolved.make_runtime_judge(model), provider_name=resolved.name)


__all__ = [
    "BaseJudge", "JudgeResult",
    "JudgeProvider", "JudgeRegistry", "NoProviderAvailableError",
    "GeminiRuntimeJudge",
    "OllamaRuntimeJudge", "is_ollama_available",
    "LlamaCppRuntimeJudge", "is_llama_cpp_available",
    "make_runtime_judge",
]

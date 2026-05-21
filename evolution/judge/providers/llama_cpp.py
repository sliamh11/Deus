"""Llama.cpp judge provider — thin wrapper around LlamaCppRuntimeJudge."""
from typing import Optional

from ..base import BaseJudge
from ..provider import JudgeProvider


class LlamaCppProvider(JudgeProvider):
    """Local llama-server judge — opt-in alternative to Ollama. Free, no quota.

    Priority 15 = less preferred than Ollama (10). Use
    EVOLUTION_JUDGE_PROVIDER=llama-cpp to force explicit selection.
    """

    @property
    def name(self) -> str:
        return "llama-cpp"

    @property
    def priority(self) -> int:
        return 15  # Less preferred than Ollama (10). User-stated opt-in semantics.

    @property
    def default_model(self) -> str:
        # Phase 3: LLAMA_CPP_JUDGE_MODEL falls back to LLAMA_CPP_MODEL when unset
        from ...config import LLAMA_CPP_JUDGE_MODEL
        return LLAMA_CPP_JUDGE_MODEL

    def is_available(self) -> bool:
        from ..llama_cpp_judge import is_llama_cpp_available
        return is_llama_cpp_available()

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        from ..llama_cpp_judge import LlamaCppRuntimeJudge
        return LlamaCppRuntimeJudge(model=model or self.default_model)

"""Ollama judge provider."""
from typing import Optional

from deepeval.models import DeepEvalBaseLLM

from ..base import BaseJudge
from ..provider import JudgeProvider


class OllamaProvider(JudgeProvider):
    """Local Ollama — preferred when available (free, no quota)."""

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def priority(self) -> int:
        return 10

    @property
    def default_model(self) -> str:
        from ...config import OLLAMA_MODEL
        return OLLAMA_MODEL

    def is_available(self) -> bool:
        from ..ollama_judge import is_ollama_available
        return is_ollama_available()

    def make_deepeval_judge(self, model: Optional[str] = None) -> DeepEvalBaseLLM:
        from ..ollama_judge import OllamaJudge
        return OllamaJudge(model=model or self.default_model)

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        from ..ollama_judge import OllamaRuntimeJudge
        return OllamaRuntimeJudge(model=model or self.default_model)

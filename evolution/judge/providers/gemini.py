"""Gemini judge provider."""
from typing import Optional

from ..base import BaseJudge
from ..provider import JudgeProvider


class GeminiProvider(JudgeProvider):
    """Gemini API — fallback when Ollama is unavailable."""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def priority(self) -> int:
        return 20

    @property
    def default_model(self) -> str:
        from ...config import JUDGE_MODEL
        return JUDGE_MODEL

    def is_available(self) -> bool:
        try:
            from ...config import load_api_key
            load_api_key()
            return True
        except (RuntimeError, ImportError):
            return False

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        from ..gemini_judge import GeminiRuntimeJudge
        return GeminiRuntimeJudge(model=model or self.default_model)

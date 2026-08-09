"""OpenAI judge provider — opt-in only (see is_available)."""
import os
from typing import Optional

from ..base import BaseJudge
from ..provider import JudgeProvider


class OpenAIProvider(JudgeProvider):
    """OpenAI API (e.g. GPT-5.6 Luna/Terra/Sol) — opt-in benchmark/eval alternative.

    Priority 20 places it below gemini(5)/ollama(10)/llama-cpp(15) in auto-detect
    preference (claude_proxy is 30, still lower-preferred than this). Availability
    additionally requires EVOLUTION_OPENAI_JUDGE_ENABLED to be explicitly set —
    key-presence alone is NOT enough. Without this second gate, JudgeRegistry's
    production auto-detect (resolve()) would silently start routing real
    interactions to an unvalidated paid model the instant Ollama/Gemini simply
    aren't running, even though nothing has validated its judge quality yet.
    """

    @property
    def name(self) -> str:
        return "openai"

    @property
    def priority(self) -> int:
        return 20

    @property
    def default_model(self) -> str:
        from ...config import OPENAI_JUDGE_MODEL
        return OPENAI_JUDGE_MODEL

    def is_available(self) -> bool:
        if os.environ.get("EVOLUTION_OPENAI_JUDGE_ENABLED", "").lower() not in ("1", "true", "yes"):
            return False
        from ..openai_judge import is_openai_available
        return is_openai_available()

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        from ..openai_judge import OpenAIRuntimeJudge
        return OpenAIRuntimeJudge(model=model or self.default_model)

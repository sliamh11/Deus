"""Mock judge provider — for CI smoke tests."""
import os
from typing import Any, Optional, Tuple

from deepeval.models import DeepEvalBaseLLM

from ..base import BaseJudge, JudgeResult
from ..provider import JudgeProvider


class MockJudge(DeepEvalBaseLLM):
    """Returns a fixed score without calling any external API."""

    def __init__(self, score: float = 0.75):
        self._score = score

    def load_model(self):
        return None

    def generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        return str(self._score), self._score

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        return str(self._score), self._score

    def get_model_name(self) -> str:
        return f"mock:{self._score}"


class MockRuntimeJudge(BaseJudge):
    """Returns fixed scores for runtime evaluation."""

    def __init__(self, score: float = 0.75):
        self._score = score

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        return JudgeResult(
            score=self._score,
            quality=self._score,
            safety=1.0,
            tool_use=1.0,
            personalization=self._score,
            rationale="Mock judge — fixed score",
        )


class MockProvider(JudgeProvider):
    """Mock provider — only available when explicitly requested via EVAL_JUDGE=mock."""

    @property
    def name(self) -> str:
        return "mock"

    @property
    def priority(self) -> int:
        return 0

    @property
    def default_model(self) -> str:
        return "mock:0.75"

    def is_available(self) -> bool:
        return os.environ.get("EVAL_JUDGE", "").lower() == "mock"

    def make_deepeval_judge(self, model: Optional[str] = None) -> DeepEvalBaseLLM:
        return MockJudge()

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        return MockRuntimeJudge()

"""
Abstract base class for Evolution judges.
Swap implementations (Gemini, Claude, local model) without changing callers.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class JudgeResult:
    """Holistic quality assessment of a single agent interaction."""
    score: float                          # 0.0–1.0 composite score
    quality: float                        # helpfulness, completeness, clarity
    safety: float                         # no toxicity/bias/harmful content
    tool_use: float                       # correct tool selection + evidence
    personalization: float                # aligned with user style and context
    rationale: str                        # brief explanation of scores
    raw_response: Optional[str] = None   # full judge LLM output for debugging
    is_parse_error: bool = False          # True if score is a fallback due to JSON parse failure


class BaseJudge(ABC):
    """Evaluate a single agent interaction and return a JudgeResult."""

    @abstractmethod
    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        ...

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        """Async variant — default falls back to sync evaluate."""
        return self.evaluate(prompt, response, tools_used, context)

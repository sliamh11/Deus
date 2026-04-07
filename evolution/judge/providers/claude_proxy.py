"""Claude proxy judge provider — last-resort fallback."""
import os
import urllib.request
import urllib.error
from typing import Optional

from ..base import BaseJudge, JudgeResult
from ..provider import JudgeProvider

PROXY_BASE_URL = os.environ.get("CREDENTIAL_PROXY_URL", "http://localhost:3001")
CLAUDE_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "claude-sonnet-4-5")


class ClaudeProxyRuntimeJudge(BaseJudge):
    """Runtime judge via Claude proxy — returns neutral scores (blocked)."""

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Claude proxy judge — blocked, returning neutral score",
        )


class ClaudeProxyProvider(JudgeProvider):
    """Claude proxy — last resort, currently blocked."""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def priority(self) -> int:
        return 30

    @property
    def default_model(self) -> str:
        return CLAUDE_JUDGE_MODEL

    def is_available(self) -> bool:
        try:
            req = urllib.request.Request(f"{PROXY_BASE_URL}/health")
            urllib.request.urlopen(req, timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            return False

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        return ClaudeProxyRuntimeJudge()

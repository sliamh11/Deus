"""Claude proxy judge provider — last-resort fallback."""
import os
import urllib.request
import urllib.error
from typing import Any, Optional, Tuple

from deepeval.models import DeepEvalBaseLLM

from ..base import BaseJudge, JudgeResult
from ..provider import JudgeProvider

PROXY_BASE_URL = os.environ.get("CREDENTIAL_PROXY_URL", "http://localhost:3001")
CLAUDE_JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "claude-sonnet-4-5")


class ClaudeProxyJudge(DeepEvalBaseLLM):
    """
    Claude judge model routed through the Deus credential proxy.
    NOTE: currently blocked — Anthropic messages API rejects OAuth Bearer auth.
    Kept as fallback.
    """

    def __init__(self, model: str = CLAUDE_JUDGE_MODEL):
        self.model = model
        self._client = None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(
                auth_token="placeholder",
                base_url=PROXY_BASE_URL,
            )
        return self._client

    def load_model(self):
        return self._get_client()

    def generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        client = self._get_client()
        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text, 0.0

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        import anthropic
        client = anthropic.AsyncAnthropic(
            auth_token="placeholder",
            base_url=PROXY_BASE_URL,
        )
        response = await client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text, 0.0

    def get_model_name(self) -> str:
        return f"claude-proxy:{self.model}"


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

    def make_deepeval_judge(self, model: Optional[str] = None) -> DeepEvalBaseLLM:
        return ClaudeProxyJudge(model=model or self.default_model)

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        return ClaudeProxyRuntimeJudge()

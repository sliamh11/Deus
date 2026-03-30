"""
DeepEval judge model for Deus eval suites.

Priority order:
1. GeminiJudge  — uses GEMINI_API_KEY; same Gemini client as memory_indexer.
2. ClaudeProxyJudge — OAuth Bearer via localhost:3001 credential proxy.
                      Blocked on Anthropic API (returns 401); kept as fallback
                      in case the auth issue is resolved.

Usage:
    from judge_model import make_judge
    metric = GEval(..., model=make_judge())
"""
import os
import sys
from pathlib import Path
from typing import Any, Optional, Tuple

from deepeval.models import DeepEvalBaseLLM

# ── Gemini judge ───────────────────────────────────────────────────────────────

# Add project root to path so `evolution` package is importable from eval/
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

try:
    from evolution.judge.gemini_judge import GeminiJudge
    _GEMINI_AVAILABLE = True
except ImportError:
    _GEMINI_AVAILABLE = False
    GeminiJudge = None  # type: ignore

try:
    from evolution.judge.ollama_judge import OllamaJudge, is_ollama_available
    _OLLAMA_IMPORTABLE = True
except ImportError:
    _OLLAMA_IMPORTABLE = False
    OllamaJudge = None  # type: ignore
    def is_ollama_available() -> bool:  # type: ignore
        return False

# ── Claude proxy judge (fallback) ─────────────────────────────────────────────

PROXY_BASE_URL = os.environ.get("CREDENTIAL_PROXY_URL", "http://localhost:3001")
JUDGE_MODEL = os.environ.get("DEEPEVAL_JUDGE_MODEL", "claude-sonnet-4-5")


class ClaudeProxyJudge(DeepEvalBaseLLM):
    """
    Claude judge model routed through the Deus credential proxy.
    NOTE: currently blocked — Anthropic messages API rejects OAuth Bearer auth.
    Kept as fallback; use GeminiJudge for working evaluations.
    """

    def __init__(self, model: str = JUDGE_MODEL):
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


# ── Factory ────────────────────────────────────────────────────────────────────

def make_judge(model: Optional[str] = None) -> DeepEvalBaseLLM:
    """
    Return the best available judge:
    - EVAL_JUDGE=ollama → OllamaJudge
    - EVAL_JUDGE=gemini → GeminiJudge
    - If not set, auto-detect: Ollama if reachable, then Gemini, then ClaudeProxy
    """
    eval_judge = os.environ.get("EVAL_JUDGE", "").lower()

    if eval_judge == "ollama":
        if not _OLLAMA_IMPORTABLE:
            raise RuntimeError("OllamaJudge not importable — check evolution package")
        return OllamaJudge(model=model or os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"))

    if eval_judge == "gemini":
        if not _GEMINI_AVAILABLE:
            raise RuntimeError("GeminiJudge not importable — check google-genai package")
        from evolution.config import JUDGE_MODEL as GEMINI_JUDGE_MODEL
        return GeminiJudge(model=model or GEMINI_JUDGE_MODEL)

    # Auto-detect: try Ollama first, then Gemini, then Claude proxy
    if _OLLAMA_IMPORTABLE and is_ollama_available():
        return OllamaJudge(model=model or os.environ.get("OLLAMA_MODEL", "qwen3.5:4b"))

    if _GEMINI_AVAILABLE:
        from evolution.config import JUDGE_MODEL as GEMINI_JUDGE_MODEL
        return GeminiJudge(model=model or GEMINI_JUDGE_MODEL)

    return ClaudeProxyJudge(model=model or JUDGE_MODEL)

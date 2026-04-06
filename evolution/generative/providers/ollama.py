"""Ollama generative provider — local model via HTTP API."""
from typing import Optional

from ..provider import GenerativeProvider


class OllamaGenerativeProvider(GenerativeProvider):
    """Local Ollama — free, no quota, but generation quality varies by model."""

    @property
    def name(self) -> str:
        return "ollama"

    @property
    def priority(self) -> int:
        return 20

    @property
    def default_model(self) -> str:
        from ...config import OLLAMA_MODEL
        return OLLAMA_MODEL

    def is_available(self) -> bool:
        try:
            import httpx

            from ...config import OLLAMA_HOST
            resp = httpx.get(f"{OLLAMA_HOST}/api/tags", timeout=2.0)
            return resp.status_code == 200
        except Exception:
            return False

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        import httpx

        from ...config import OLLAMA_HOST

        effective_model = model or self.default_model
        resp = httpx.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": effective_model, "prompt": prompt, "stream": False},
            timeout=120.0,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        if not text:
            raise RuntimeError(f"Ollama returned empty response for model {effective_model}")
        return text

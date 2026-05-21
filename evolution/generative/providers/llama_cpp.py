"""Llama.cpp generative provider — local llama-server via OpenAI-compat /v1/chat/completions."""
from typing import Optional

from ..provider import GenerativeProvider


class LlamaCppGenerativeProvider(GenerativeProvider):
    """Local llama-server — opt-in alternative to Ollama. Free, no quota.

    Targets the OpenAI-compatible /v1/chat/completions endpoint exposed by
    llama-server. Wraps the raw prompt as a single user message. The "model"
    field is omitted when LLAMA_CPP_MODEL is empty (llama-server uses whatever
    model it has loaded).

    Priority 25 = less preferred than Ollama (20). Use
    EVOLUTION_GEN_PROVIDER=llama-cpp to force explicit selection.
    """

    @property
    def name(self) -> str:
        return "llama-cpp"

    @property
    def priority(self) -> int:
        return 25  # Less preferred than Ollama (20). User-stated opt-in semantics.

    @property
    def default_model(self) -> str:
        # Phase 3: LLAMA_CPP_GEN_MODEL falls back to LLAMA_CPP_MODEL when unset
        from ...config import LLAMA_CPP_GEN_MODEL
        return LLAMA_CPP_GEN_MODEL

    def is_available(self) -> bool:
        try:
            import httpx

            from ...config import LLAMA_CPP_BASE_URL
            if not LLAMA_CPP_BASE_URL:
                return False
            resp = httpx.get(
                f"{LLAMA_CPP_BASE_URL.rstrip('/')}/models",
                timeout=2.0,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        import httpx

        from ...config import LLAMA_CPP_BASE_URL

        effective_model = model or self.default_model
        body: dict = {
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if effective_model:
            body["model"] = effective_model

        resp = httpx.post(
            f"{LLAMA_CPP_BASE_URL.rstrip('/')}/chat/completions",
            json=body,
            headers={"Authorization": "Bearer placeholder"},
            timeout=120.0,
        )
        resp.raise_for_status()
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(
                f"llama-cpp returned no choices for model {effective_model or '<server-default>'}"
            )
        text = (choices[0].get("message", {}).get("content") or "").strip()
        if not text:
            raise RuntimeError(
                f"llama-cpp returned empty response for model {effective_model or '<server-default>'}"
            )
        return text

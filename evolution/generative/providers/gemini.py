"""Gemini generative provider — uses Google GenAI SDK with model fallback."""
from typing import Optional

from ..provider import GenerativeProvider


class GeminiGenerativeProvider(GenerativeProvider):
    """Google Gemini API — preferred for generation quality."""

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def priority(self) -> int:
        return 10

    @property
    def default_model(self) -> str:
        from ...config import GEN_MODEL
        return GEN_MODEL

    def is_available(self) -> bool:
        try:
            from ...config import load_api_key
            load_api_key()
            return True
        except (RuntimeError, ImportError):
            return False

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        from google import genai

        from ...config import GEN_MODELS, load_api_key

        client = genai.Client(api_key=load_api_key())
        preferred = model or self.default_model
        models_to_try = [preferred] + [m for m in GEN_MODELS if m != preferred]

        last_exc: Optional[Exception] = None
        for m in models_to_try:
            try:
                resp = client.models.generate_content(model=m, contents=prompt)
                return resp.text.strip()
            except Exception as exc:
                last_exc = exc
                exc_str = str(exc)
                if any(s in exc_str for s in ("429", "quota", "503", "unavailable", "UNAVAILABLE")):
                    continue
                raise

        raise RuntimeError(f"All Gemini models failed. Last: {last_exc}")

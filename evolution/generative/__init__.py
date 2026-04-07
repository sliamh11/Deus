"""
Generative text provider — abstracts Gemini, Ollama, and other backends.

Usage:
    from evolution.generative import generate
    text = generate("Write a haiku about AI")

    # Explicit provider:
    text = generate("Write a haiku", provider="ollama")
"""
from typing import Optional

from .provider import GenerativeProvider, GenerativeRegistry, NoGenerativeProviderError

# Auto-register built-in providers on import
from . import providers as _providers  # noqa: F401


def generate(prompt: str, model: Optional[str] = None, provider: Optional[str] = None) -> str:
    """
    Generate text using the best available provider.

    Falls back to the next available provider on quota/rate-limit errors.

    Args:
        prompt: The input prompt.
        model: Optional model override.
        provider: Optional provider name override.

    Returns:
        Generated text.
    """
    registry = GenerativeRegistry.default()

    # If explicit provider requested, no fallback
    if provider:
        return registry.resolve(provider).generate(prompt, model)

    # Try providers in priority order, fall back on quota/transient errors
    last_exc = None
    is_first = True
    for name in registry.list_providers():
        p = registry._providers[name]
        if not p.is_available():
            continue
        try:
            # Only pass model override to the primary provider — fallback
            # providers use their own default (model names aren't portable)
            effective_model = model if is_first else None
            return p.generate(prompt, effective_model)
        except (RuntimeError, Exception) as exc:
            exc_str = str(exc)
            if any(s in exc_str for s in ("429", "quota", "RESOURCE_EXHAUSTED", "503", "unavailable", "404", "timed out")):
                last_exc = exc
                is_first = False
                continue
            raise

    if last_exc:
        raise last_exc
    raise NoGenerativeProviderError("No generative provider available")


__all__ = [
    "generate",
    "GenerativeProvider",
    "GenerativeRegistry",
    "NoGenerativeProviderError",
]

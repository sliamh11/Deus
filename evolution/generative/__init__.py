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

    Args:
        prompt: The input prompt.
        model: Optional model override.
        provider: Optional provider name override.

    Returns:
        Generated text.
    """
    return GenerativeRegistry.default().resolve(provider).generate(prompt, model)


__all__ = [
    "generate",
    "GenerativeProvider",
    "GenerativeRegistry",
    "NoGenerativeProviderError",
]

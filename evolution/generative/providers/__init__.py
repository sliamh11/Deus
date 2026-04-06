"""Built-in generative providers. Importing this package registers them all."""
from ..provider import GenerativeRegistry

from .gemini import GeminiGenerativeProvider
from .ollama import OllamaGenerativeProvider
from .mock import MockGenerativeProvider

_registry = GenerativeRegistry.default()
_registry.register(GeminiGenerativeProvider())
_registry.register(OllamaGenerativeProvider())
_registry.register(MockGenerativeProvider())

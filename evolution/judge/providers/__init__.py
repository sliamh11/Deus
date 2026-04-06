"""Built-in judge providers. Importing this package registers them all."""
from ..provider import JudgeRegistry

from .ollama import OllamaProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .claude_proxy import ClaudeProxyProvider

_registry = JudgeRegistry.default()
_registry.register(OllamaProvider())
_registry.register(GeminiProvider())
_registry.register(MockProvider())
_registry.register(ClaudeProxyProvider())

__all__ = ["OllamaProvider", "GeminiProvider", "MockProvider", "ClaudeProxyProvider"]

"""
Provider/strategy pattern for generative text backends.

Each backend (Gemini, Ollama, Mock) implements GenerativeProvider.
GenerativeRegistry resolves the best available backend at runtime.
"""
from abc import ABC, abstractmethod
from typing import Optional


class NoGenerativeProviderError(RuntimeError):
    """Raised when no generative provider is available."""
    pass


class GenerativeProvider(ABC):
    """
    A backend that can generate text from a prompt.

    Subclass this for each backend (Gemini, Ollama, Mock).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name, e.g. 'gemini', 'ollama', 'mock'."""
        ...

    @property
    @abstractmethod
    def priority(self) -> int:
        """Lower = preferred during auto-detection."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this backend can serve requests right now."""
        ...

    @abstractmethod
    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        """
        Generate text from a prompt.

        Args:
            prompt: The input prompt.
            model: Optional model override. If None, uses the provider's default.

        Returns:
            The generated text, stripped of leading/trailing whitespace.

        Raises:
            RuntimeError: If all model attempts fail.
        """
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        """The default model identifier for this backend."""
        ...


class GenerativeRegistry:
    """
    Central registry of generative providers.

    Usage:
        registry = GenerativeRegistry.default()
        provider = registry.resolve()              # auto-detect best
        provider = registry.resolve("ollama")      # explicit choice
        text = provider.generate("Write a haiku")
    """

    _instance: Optional["GenerativeRegistry"] = None

    def __init__(self):
        self._providers: dict[str, GenerativeProvider] = {}

    @classmethod
    def default(cls) -> "GenerativeRegistry":
        """Return the singleton registry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — for testing only."""
        cls._instance = None

    def register(self, provider: GenerativeProvider) -> None:
        """Register a provider. Last-write-wins for same name."""
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        """Remove a provider by name."""
        self._providers.pop(name, None)

    def get(self, name: str) -> GenerativeProvider:
        """Get a provider by exact name. Raises KeyError if not found."""
        return self._providers[name]

    def list_providers(self) -> list[str]:
        """Return registered provider names sorted by priority."""
        return [
            p.name for p in sorted(self._providers.values(), key=lambda p: p.priority)
        ]

    def resolve(self, preference: Optional[str] = None) -> GenerativeProvider:
        """
        Resolve the best available provider.

        Resolution order:
        1. EVOLUTION_GEN_PROVIDER env var (if set)
        2. Explicit preference argument
        3. Auto-detect: lowest priority number among available providers

        Raises NoGenerativeProviderError if nothing works.
        """
        import os

        # 1. Env var override
        env_pref = os.environ.get("EVOLUTION_GEN_PROVIDER", "").lower()
        effective = env_pref or (preference.lower() if preference else None)

        # 2. Explicit preference
        if effective:
            if effective not in self._providers:
                raise NoGenerativeProviderError(
                    f"Provider '{effective}' not registered. "
                    f"Available: {self.list_providers()}"
                )
            provider = self._providers[effective]
            if not provider.is_available():
                raise NoGenerativeProviderError(
                    f"Provider '{effective}' is registered but not available."
                )
            return provider

        # 3. Auto-detect by priority
        candidates = sorted(self._providers.values(), key=lambda p: p.priority)
        for provider in candidates:
            if provider.is_available():
                return provider

        raise NoGenerativeProviderError(
            f"No generative provider available. Registered: {self.list_providers()}"
        )

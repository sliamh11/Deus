"""
Provider/strategy pattern for judge backends.

Each backend (Ollama, Gemini, Mock, ClaudeProxy) implements JudgeProvider.
JudgeRegistry resolves the best available backend at runtime.
"""
from abc import ABC, abstractmethod
from typing import Optional

from .base import BaseJudge


class NoProviderAvailableError(RuntimeError):
    """Raised when no judge provider is available."""
    pass


class JudgeProvider(ABC):
    """
    A backend that can produce both DeepEval and runtime judges.

    Subclass this for each backend (Ollama, Gemini, Mock, ClaudeProxy).
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name, e.g. 'ollama', 'gemini', 'mock'."""
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
    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        """Create a runtime judge instance for scoring interactions."""
        ...

    @property
    @abstractmethod
    def default_model(self) -> str:
        """The default model identifier for this backend."""
        ...


class JudgeRegistry:
    """
    Central registry of judge providers.

    Usage:
        registry = JudgeRegistry.default()
        provider = registry.resolve()              # auto-detect best
        provider = registry.resolve("ollama")      # explicit choice
        judge = provider.make_runtime_judge()
    """

    _instance: Optional["JudgeRegistry"] = None

    def __init__(self):
        self._providers: dict[str, JudgeProvider] = {}

    @classmethod
    def default(cls) -> "JudgeRegistry":
        """Return the singleton registry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — for testing only."""
        cls._instance = None

    def register(self, provider: JudgeProvider) -> None:
        """Register a provider. Last-write-wins for same name."""
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> None:
        """Remove a provider by name."""
        self._providers.pop(name, None)

    def get(self, name: str) -> JudgeProvider:
        """Get a provider by exact name. Raises KeyError if not found."""
        return self._providers[name]

    def list_providers(self) -> list[str]:
        """Return registered provider names sorted by priority."""
        return [
            p.name for p in sorted(self._providers.values(), key=lambda p: p.priority)
        ]

    def resolve(self, preference: Optional[str] = None) -> JudgeProvider:
        """
        Resolve the best available provider.

        Resolution order:
        1. EVOLUTION_JUDGE_PROVIDER env var (if set)
        2. Explicit preference argument
        3. Auto-detect: lowest priority number among available providers

        Raises NoProviderAvailableError if nothing works.
        """
        import os

        # 1. Env var override
        env_pref = os.environ.get("EVOLUTION_JUDGE_PROVIDER", "").lower()
        effective = env_pref or (preference.lower() if preference else None)

        # 2. Explicit preference
        if effective:
            if effective not in self._providers:
                raise NoProviderAvailableError(
                    f"Provider '{effective}' not registered. "
                    f"Available: {self.list_providers()}"
                )
            provider = self._providers[effective]
            if not provider.is_available():
                raise NoProviderAvailableError(
                    f"Provider '{effective}' is registered but not available."
                )
            return provider

        # 3. Auto-detect by priority
        candidates = sorted(self._providers.values(), key=lambda p: p.priority)
        for provider in candidates:
            if provider.is_available():
                return provider

        raise NoProviderAvailableError(
            f"No judge provider available. Registered: {self.list_providers()}"
        )

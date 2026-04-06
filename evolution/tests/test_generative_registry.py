"""Tests for the GenerativeProvider / GenerativeRegistry pattern."""
import os
from typing import Optional
from unittest.mock import patch

import pytest

from evolution.generative.provider import (
    GenerativeProvider,
    GenerativeRegistry,
    NoGenerativeProviderError,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


class FakeProvider(GenerativeProvider):
    """Configurable provider for testing."""

    def __init__(self, name: str, priority: int, available: bool = True):
        self._name = name
        self._priority = priority
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    @property
    def default_model(self) -> str:
        return f"{self._name}:default"

    def is_available(self) -> bool:
        return self._available

    def generate(self, prompt: str, model: Optional[str] = None) -> str:
        return f"[{self._name}] {prompt[:20]}"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test gets a fresh registry."""
    GenerativeRegistry.reset()
    yield
    GenerativeRegistry.reset()


# ── Registry unit tests ──────────────────────────────────────────────────────


class TestGenerativeRegistry:
    def test_register_and_get(self):
        reg = GenerativeRegistry.default()
        p = FakeProvider("test", priority=10)
        reg.register(p)
        assert reg.get("test") is p

    def test_get_unknown_raises_keyerror(self):
        reg = GenerativeRegistry.default()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_unregister(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("tmp", priority=10))
        reg.unregister("tmp")
        with pytest.raises(KeyError):
            reg.get("tmp")

    def test_unregister_missing_is_noop(self):
        reg = GenerativeRegistry.default()
        reg.unregister("nonexistent")  # should not raise

    def test_list_providers_sorted_by_priority(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("high", priority=30))
        reg.register(FakeProvider("low", priority=5))
        reg.register(FakeProvider("mid", priority=15))
        assert reg.list_providers() == ["low", "mid", "high"]

    def test_singleton(self):
        a = GenerativeRegistry.default()
        b = GenerativeRegistry.default()
        assert a is b

    def test_reset_creates_fresh_instance(self):
        a = GenerativeRegistry.default()
        a.register(FakeProvider("x", priority=1))
        GenerativeRegistry.reset()
        b = GenerativeRegistry.default()
        assert a is not b
        assert b.list_providers() == []


class TestResolve:
    def test_resolve_auto_detect_by_priority(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("slow", priority=20, available=True))
        reg.register(FakeProvider("fast", priority=5, available=True))
        assert reg.resolve().name == "fast"

    def test_resolve_skips_unavailable(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("down", priority=1, available=False))
        reg.register(FakeProvider("up", priority=10, available=True))
        assert reg.resolve().name == "up"

    def test_resolve_explicit_preference(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        assert reg.resolve("b").name == "b"

    def test_resolve_env_var_override(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"EVOLUTION_GEN_PROVIDER": "b"}):
            assert reg.resolve().name == "b"

    def test_resolve_env_var_takes_precedence_over_preference(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"EVOLUTION_GEN_PROVIDER": "b"}):
            assert reg.resolve("a").name == "b"

    def test_resolve_no_providers_raises(self):
        reg = GenerativeRegistry.default()
        with pytest.raises(NoGenerativeProviderError, match="No generative provider available"):
            reg.resolve()

    def test_resolve_all_unavailable_raises(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("x", priority=1, available=False))
        reg.register(FakeProvider("y", priority=2, available=False))
        with pytest.raises(NoGenerativeProviderError, match="No generative provider available"):
            reg.resolve()

    def test_resolve_unknown_preference_raises(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        with pytest.raises(NoGenerativeProviderError, match="not registered"):
            reg.resolve("nonexistent")

    def test_resolve_preference_unavailable_raises(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=False))
        with pytest.raises(NoGenerativeProviderError, match="not available"):
            reg.resolve("a")

    def test_resolve_case_insensitive(self):
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("gemini", priority=10, available=True))
        assert reg.resolve("GEMINI").name == "gemini"


class TestProviderGenerate:
    def test_generate_returns_text(self):
        p = FakeProvider("test", priority=1)
        result = p.generate("hello world")
        assert "[test]" in result

    def test_default_model_accessible(self):
        p = FakeProvider("test", priority=1)
        assert p.default_model == "test:default"


class TestBuiltInProviders:
    """Smoke tests that the real providers can be instantiated."""

    def test_gemini_provider_has_correct_name(self):
        from evolution.generative.providers.gemini import GeminiGenerativeProvider
        p = GeminiGenerativeProvider()
        assert p.name == "gemini"
        assert p.priority == 10

    def test_ollama_provider_has_correct_name(self):
        from evolution.generative.providers.ollama import OllamaGenerativeProvider
        p = OllamaGenerativeProvider()
        assert p.name == "ollama"
        assert p.priority == 20

    def test_mock_provider_has_correct_name(self):
        from evolution.generative.providers.mock import MockGenerativeProvider
        p = MockGenerativeProvider()
        assert p.name == "mock"
        assert p.priority == 0

    def test_mock_provider_available_only_when_env_set(self):
        from evolution.generative.providers.mock import MockGenerativeProvider
        p = MockGenerativeProvider()
        assert not p.is_available()
        with patch.dict(os.environ, {"EVOLUTION_GEN_PROVIDER": "mock"}):
            assert p.is_available()

    def test_mock_provider_returns_canned_response(self):
        from evolution.generative.providers.mock import MockGenerativeProvider
        p = MockGenerativeProvider()
        text = p.generate("anything")
        assert "Mock response" in text
        assert "Category:" in text

    def test_gemini_default_model_from_config(self):
        from evolution.generative.providers.gemini import GeminiGenerativeProvider
        p = GeminiGenerativeProvider()
        assert "gemini" in p.default_model or "models/" in p.default_model

    def test_ollama_default_model_from_config(self):
        from evolution.generative.providers.ollama import OllamaGenerativeProvider
        p = OllamaGenerativeProvider()
        assert p.default_model == "gemma4:e4b"  # current default in config


class TestPublicAPI:
    """Test the top-level generate() convenience function."""

    def test_generate_with_mock_provider(self):
        from evolution.generative import generate

        # Register a fake provider for testing
        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("testprov", priority=1, available=True))
        result = generate("hello", provider="testprov")
        assert "[testprov]" in result

    def test_generate_resolves_best_available(self):
        from evolution.generative import generate

        reg = GenerativeRegistry.default()
        reg.register(FakeProvider("best", priority=1, available=True))
        reg.register(FakeProvider("worst", priority=99, available=True))
        result = generate("hello")
        assert "[best]" in result

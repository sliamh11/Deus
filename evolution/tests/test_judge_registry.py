"""Tests for the JudgeProvider / JudgeRegistry pattern."""
import os
from typing import Optional
from unittest.mock import patch

import pytest

from evolution.judge.base import BaseJudge, JudgeResult
from evolution.judge.provider import (
    JudgeProvider,
    JudgeRegistry,
    NoProviderAvailableError,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


class FakeRuntimeJudge(BaseJudge):
    def evaluate(self, prompt, response, tools_used=None, context=None):
        return JudgeResult(
            score=0.5, quality=0.5, safety=1.0, tool_use=1.0,
            personalization=0.5, rationale="fake",
        )


class FakeProvider(JudgeProvider):
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

    def make_runtime_judge(self, model: Optional[str] = None) -> BaseJudge:
        return FakeRuntimeJudge()


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test gets a fresh registry."""
    JudgeRegistry.reset()
    yield
    JudgeRegistry.reset()


# ── Registry unit tests ──────────────────────────────────────────────────────


class TestJudgeRegistry:
    def test_register_and_get(self):
        reg = JudgeRegistry.default()
        p = FakeProvider("test", priority=10)
        reg.register(p)
        assert reg.get("test") is p

    def test_get_unknown_raises_keyerror(self):
        reg = JudgeRegistry.default()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_unregister(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("tmp", priority=10))
        reg.unregister("tmp")
        with pytest.raises(KeyError):
            reg.get("tmp")

    def test_unregister_missing_is_noop(self):
        reg = JudgeRegistry.default()
        reg.unregister("nonexistent")  # should not raise

    def test_list_providers_sorted_by_priority(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("high", priority=30))
        reg.register(FakeProvider("low", priority=5))
        reg.register(FakeProvider("mid", priority=15))
        assert reg.list_providers() == ["low", "mid", "high"]

    def test_singleton(self):
        a = JudgeRegistry.default()
        b = JudgeRegistry.default()
        assert a is b

    def test_reset_creates_fresh_instance(self):
        a = JudgeRegistry.default()
        a.register(FakeProvider("x", priority=1))
        JudgeRegistry.reset()
        b = JudgeRegistry.default()
        assert a is not b
        assert b.list_providers() == []


class TestResolve:
    def test_resolve_auto_detect_by_priority(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("slow", priority=20, available=True))
        reg.register(FakeProvider("fast", priority=5, available=True))
        assert reg.resolve().name == "fast"

    def test_resolve_skips_unavailable(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("down", priority=1, available=False))
        reg.register(FakeProvider("up", priority=10, available=True))
        assert reg.resolve().name == "up"

    def test_resolve_explicit_preference(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        assert reg.resolve("b").name == "b"

    def test_resolve_env_var_override(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"EVOLUTION_JUDGE_PROVIDER": "b"}):
            assert reg.resolve().name == "b"

    def test_resolve_env_var_takes_precedence_over_preference(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        reg.register(FakeProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"EVOLUTION_JUDGE_PROVIDER": "b"}):
            assert reg.resolve("a").name == "b"

    def test_resolve_no_providers_raises(self):
        reg = JudgeRegistry.default()
        with pytest.raises(NoProviderAvailableError, match="No judge provider available"):
            reg.resolve()

    def test_resolve_all_unavailable_raises(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("x", priority=1, available=False))
        reg.register(FakeProvider("y", priority=2, available=False))
        with pytest.raises(NoProviderAvailableError, match="No judge provider available"):
            reg.resolve()

    def test_resolve_unknown_preference_raises(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=True))
        with pytest.raises(NoProviderAvailableError, match="not registered"):
            reg.resolve("nonexistent")

    def test_resolve_preference_unavailable_raises(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("a", priority=1, available=False))
        with pytest.raises(NoProviderAvailableError, match="not available"):
            reg.resolve("a")

    def test_resolve_case_insensitive(self):
        reg = JudgeRegistry.default()
        reg.register(FakeProvider("ollama", priority=10, available=True))
        assert reg.resolve("OLLAMA").name == "ollama"


class TestProviderFactoryMethods:
    def test_make_runtime_judge_returns_base_judge(self):
        p = FakeProvider("test", priority=1)
        judge = p.make_runtime_judge()
        assert isinstance(judge, BaseJudge)


class TestBuiltInProviders:
    """Smoke tests that the real providers can be instantiated."""

    def test_ollama_provider_has_correct_name(self):
        from evolution.judge.providers.ollama import OllamaProvider
        p = OllamaProvider()
        assert p.name == "ollama"
        assert p.priority == 10

    def test_gemini_provider_has_correct_name(self):
        from evolution.judge.providers.gemini import GeminiProvider
        p = GeminiProvider()
        assert p.name == "gemini"
        assert p.priority == 20

    def test_mock_provider_has_correct_name(self):
        from evolution.judge.providers.mock import MockProvider
        p = MockProvider()
        assert p.name == "mock"
        assert p.priority == 0

    def test_mock_provider_available_only_when_env_set(self):
        from evolution.judge.providers.mock import MockProvider
        p = MockProvider()
        assert not p.is_available()
        with patch.dict(os.environ, {"EVAL_JUDGE": "mock"}):
            assert p.is_available()

    def test_claude_proxy_provider_has_correct_name(self):
        from evolution.judge.providers.claude_proxy import ClaudeProxyProvider
        p = ClaudeProxyProvider()
        assert p.name == "claude"
        assert p.priority == 30

    def test_default_model_reads_from_config(self):
        from evolution.judge.providers.ollama import OllamaProvider
        p = OllamaProvider()
        assert p.default_model == "gemma4:e4b"  # current default in config

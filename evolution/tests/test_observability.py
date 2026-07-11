"""Tests for evolution/observability.py — the eval-layer observer seam."""
import asyncio

import pytest

import evolution.observability as obs_mod
from evolution.observability import (
    JudgeEvent,
    ObservedJudge,
    ReflectionEvent,
    emit_reflection,
    get_observers,
    wrap_judge,
)


@pytest.fixture(autouse=True)
def reset_observer_cache(monkeypatch):
    """Each test starts with an unloaded observer cache and a clean env."""
    monkeypatch.setattr(obs_mod, "_observers", None)
    monkeypatch.delenv("EVOLUTION_OBSERVERS", raising=False)


class StubResult:
    score = 0.7
    quality = 0.8
    safety = 1.0
    tool_use = 0.5
    personalization = 0.6
    rationale = "fine"
    is_parse_error = False
    raw_response = "{}"


class StubJudge:
    model = "stub-model"

    def evaluate(self, prompt, response, tools_used=None, context=None, user_profile=None):
        return StubResult()

    async def a_evaluate(self, prompt, response, tools_used=None, context=None, user_profile=None):
        return StubResult()


class ExplodingJudge:
    def evaluate(self, prompt, response, tools_used=None, context=None, user_profile=None):
        raise RuntimeError("judge died")


class CapturingObserver:
    def __init__(self):
        self.judge_events = []
        self.reflection_events = []

    def on_judge_evaluate(self, event):
        self.judge_events.append(event)

    def on_reflection(self, event):
        self.reflection_events.append(event)


class BrokenObserver:
    def on_judge_evaluate(self, event):
        raise RuntimeError("observer bug")


def _install(monkeypatch, observer):
    """Bypass env loading — install an observer instance directly."""
    monkeypatch.setattr(obs_mod, "_observers", [observer])


def test_no_env_no_observers_and_unwrapped():
    assert get_observers() == []
    judge = StubJudge()
    assert wrap_judge(judge) is judge  # fast path: identity, not a proxy


def test_broken_spec_is_skipped(monkeypatch):
    monkeypatch.setenv("EVOLUTION_OBSERVERS", "nonexistent.module:factory")
    assert get_observers() == []


def test_file_path_spec_loads(tmp_path, monkeypatch):
    observer_file = tmp_path / "my_observer.py"
    observer_file.write_text(
        "events = []\n"
        "class Obs:\n"
        "    def on_judge_evaluate(self, event):\n"
        "        events.append(event)\n"
        "def factory():\n"
        "    return Obs()\n"
    )
    monkeypatch.setenv("EVOLUTION_OBSERVERS", f"{observer_file}:factory")
    observers = get_observers()
    assert len(observers) == 1
    assert hasattr(observers[0], "on_judge_evaluate")


def test_observed_judge_emits_event(monkeypatch):
    cap = CapturingObserver()
    _install(monkeypatch, cap)

    wrapped = wrap_judge(StubJudge(), provider_name="stub")
    assert isinstance(wrapped, ObservedJudge)
    result = wrapped.evaluate("p", "r", tools_used=["Read"], context="ctx")

    assert result.score == 0.7  # result passes through unchanged
    assert len(cap.judge_events) == 1
    ev = cap.judge_events[0]
    assert isinstance(ev, JudgeEvent)
    assert ev.score == 0.7
    assert ev.model == "stub-model"
    assert ev.provider == "stub"
    assert ev.context_present is True
    assert ev.latency_ms >= 0
    assert ev.error is None


def test_observed_judge_async_emits_once(monkeypatch):
    cap = CapturingObserver()
    _install(monkeypatch, cap)

    wrapped = wrap_judge(StubJudge())
    result = asyncio.run(wrapped.a_evaluate("p", "r"))

    assert result.score == 0.7
    assert len(cap.judge_events) == 1


def test_judge_exception_emits_error_event_and_reraises(monkeypatch):
    cap = CapturingObserver()
    _install(monkeypatch, cap)

    wrapped = wrap_judge(ExplodingJudge())
    with pytest.raises(RuntimeError, match="judge died"):
        wrapped.evaluate("p", "r")

    assert len(cap.judge_events) == 1
    assert "judge died" in cap.judge_events[0].error
    assert cap.judge_events[0].score is None


def test_broken_observer_never_breaks_evaluate(monkeypatch):
    _install(monkeypatch, BrokenObserver())

    wrapped = wrap_judge(StubJudge())
    result = wrapped.evaluate("p", "r")
    assert result.score == 0.7  # observer bug swallowed


def test_proxy_delegates_unknown_attributes(monkeypatch):
    _install(monkeypatch, CapturingObserver())
    wrapped = wrap_judge(StubJudge())
    assert wrapped.model == "stub-model"


def test_reflection_emit_from_generator(monkeypatch):
    cap = CapturingObserver()
    _install(monkeypatch, cap)

    from evolution.reflexion import generator as gen_mod

    monkeypatch.setattr(
        gen_mod, "generate",
        lambda prompt, model=None: "- What went wrong: x\n- Next time: y\n- Category: style",
    )
    content, category = gen_mod.generate_reflection(prompt="p", response="r", score=0.2)

    assert category == "style"
    assert len(cap.reflection_events) == 1
    ev = cap.reflection_events[0]
    assert isinstance(ev, ReflectionEvent)
    assert ev.kind == "reflection"
    assert ev.category == "style"
    assert ev.error is None


def test_reflection_error_emits_and_reraises(monkeypatch):
    cap = CapturingObserver()
    _install(monkeypatch, cap)

    from evolution.reflexion import generator as gen_mod

    def boom(prompt, model=None):
        raise RuntimeError("generate died")

    monkeypatch.setattr(gen_mod, "generate", boom)
    with pytest.raises(RuntimeError, match="generate died"):
        gen_mod.generate_reflection(prompt="p", response="r", score=0.2)

    assert len(cap.reflection_events) == 1
    assert cap.reflection_events[0].output is None
    assert "generate died" in cap.reflection_events[0].error


def test_emit_reflection_with_no_observers_is_noop():
    emit_reflection(ReflectionEvent(
        kind="reflection", prompt_excerpt="p", output="o",
        category="style", model=None, latency_ms=1.0,
    ))  # must not raise


def test_first_dispatch_failure_warns_then_debug(monkeypatch, caplog):
    """A broken observer surfaces at WARNING once, then drops to DEBUG."""
    import logging

    broken = BrokenObserver()
    _install(monkeypatch, broken)
    monkeypatch.setattr(obs_mod, "_warned_observers", set())

    wrapped = wrap_judge(StubJudge())
    with caplog.at_level(logging.DEBUG, logger="evolution.observability"):
        wrapped.evaluate("p", "r")
        wrapped.evaluate("p", "r")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1
    assert len(debugs) == 1


def test_concurrent_first_load_runs_factories_once(tmp_path, monkeypatch):
    """Threaded first calls must not run observer factories more than once."""
    from concurrent.futures import ThreadPoolExecutor

    observer_file = tmp_path / "counting_observer.py"
    counter_file = tmp_path / "count.txt"
    observer_file.write_text(
        "from pathlib import Path\n"
        f"COUNT = Path({str(counter_file)!r})\n"
        "class Obs:\n"
        "    def on_judge_evaluate(self, event):\n"
        "        pass\n"
        "def factory():\n"
        "    import time\n"
        "    time.sleep(0.05)\n"
        "    n = int(COUNT.read_text()) if COUNT.exists() else 0\n"
        "    COUNT.write_text(str(n + 1))\n"
        "    return Obs()\n"
    )
    monkeypatch.setenv("EVOLUTION_OBSERVERS", f"{observer_file}:factory")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: get_observers(), range(8)))

    assert counter_file.read_text() == "1"

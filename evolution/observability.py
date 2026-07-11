"""
Eval-layer observer seam — dependency-free hooks into the judge + reflexion pipeline.

External tooling (dashboards, tracing platforms, log sinks) can watch the eval
loop WITHOUT the evolution package taking any dependency on them. Observers are
loaded lazily from the EVOLUTION_OBSERVERS environment variable — a
comma-separated list of specs, each either:

    package.module:factory          (importable module)
    /abs/path/to/file.py:factory    (standalone file, e.g. personal glue
                                     outside site-packages)

Each factory() returns an object that may implement any subset of:

    on_judge_evaluate(event: JudgeEvent) -> None
    on_reflection(event: ReflectionEvent) -> None

Contract: observability must never break or slow the eval loop beyond the
observer's own runtime. All load errors are logged once (WARNING) and skipped;
all dispatch errors are swallowed (first failure per observer at WARNING, then
DEBUG). With EVOLUTION_OBSERVERS unset the seam is a no-op and judges are
returned unwrapped — zero overhead.

Content boundary: JudgeEvent carries the full prompt/response/raw judge output
UNREDACTED (context content and user_profile never cross the seam). An observer
that forwards to a REMOTE sink ships eval content off-box — point observers at
local/self-hosted sinks unless that is explicitly intended.
"""
import importlib
import importlib.util
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, List, Optional

log = logging.getLogger(__name__)

_ENV_VAR = "EVOLUTION_OBSERVERS"
_observers: Optional[List[Any]] = None
# Judges run inside ThreadPoolExecutors (e.g. batch judging) — guard the lazy
# init so concurrent first calls can't run observer factories twice.
_observers_lock = threading.Lock()
_warned_observers: set = set()


@dataclass
class JudgeEvent:
    """One judge evaluation: inputs, verdict, timing, and failure state."""
    prompt: str
    response: str
    tools_used: Optional[list]
    context_present: bool
    score: Optional[float]
    quality: Optional[float]
    safety: Optional[float]
    tool_use: Optional[float]
    personalization: Optional[float]
    rationale: Optional[str]
    is_parse_error: bool
    raw_response: Optional[str]
    model: Optional[str]
    provider: Optional[str]
    latency_ms: float
    error: Optional[str] = None


@dataclass
class ReflectionEvent:
    """One reflection generation: kind, output, timing, and failure state."""
    kind: str  # "reflection" | "positive"
    prompt_excerpt: str
    output: Optional[str]
    category: Optional[str]
    model: Optional[str]  # the model argument exactly as passed by the caller
    latency_ms: float
    error: Optional[str] = None


def _load_spec(spec: str) -> Optional[Any]:
    """Load one observer factory spec; None on any failure (logged once)."""
    try:
        target, _, attr = spec.strip().rpartition(":")
        if not target or not attr:
            raise ValueError(f"observer spec must be 'module_or_path:factory', got {spec!r}")
        if "/" in target or target.endswith(".py"):
            mod_spec = importlib.util.spec_from_file_location("_evolution_observer", target)
            if mod_spec is None or mod_spec.loader is None:
                raise ImportError(f"cannot load observer file {target!r}")
            module = importlib.util.module_from_spec(mod_spec)
            mod_spec.loader.exec_module(module)
        else:
            module = importlib.import_module(target)
        return getattr(module, attr)()
    except Exception as exc:
        log.warning("EVOLUTION_OBSERVERS: skipping %r (%s: %s)", spec, type(exc).__name__, exc)
        return None


def get_observers() -> List[Any]:
    """Lazily load observers from EVOLUTION_OBSERVERS (cached after first call)."""
    global _observers
    if _observers is None:
        with _observers_lock:
            if _observers is None:  # double-checked: factories must run exactly once
                raw = os.environ.get(_ENV_VAR, "")
                _observers = [
                    obs for spec in raw.split(",") if spec.strip()
                    for obs in [_load_spec(spec)] if obs is not None
                ]
    return _observers


def _dispatch(method: str, event: Any) -> None:
    for obs in get_observers():
        handler = getattr(obs, method, None)
        if handler is None:
            continue
        try:
            handler(event)
        except Exception as exc:
            # First failure per observer is WARNING — a silently-broken observer
            # is invisible rot; repeats drop to DEBUG to avoid log flooding.
            key = id(obs)
            if key not in _warned_observers:
                _warned_observers.add(key)
                log.warning("observer %r failed in %s: %s", obs, method, exc, exc_info=True)
            else:
                log.debug("observer %r failed in %s: %s", obs, method, exc)


def emit_judge(event: JudgeEvent) -> None:
    _dispatch("on_judge_evaluate", event)


def emit_reflection(event: ReflectionEvent) -> None:
    _dispatch("on_reflection", event)


class ObservedJudge:
    """Duck-typed judge proxy: delegates everything, emits a JudgeEvent per evaluate.

    Not a BaseJudge subclass on purpose — proxying via __getattr__ keeps this
    module free of judge imports (no cycles) and transparently preserves any
    provider-specific attributes callers may touch.
    """

    def __init__(self, judge: Any, provider_name: Optional[str] = None):
        self._judge = judge
        self._provider_name = provider_name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._judge, name)

    def _event_from(self, prompt, response, tools_used, context, result, latency_ms, error):
        return JudgeEvent(
            prompt=prompt,
            response=response,
            tools_used=tools_used,
            context_present=bool(context),
            score=getattr(result, "score", None),
            quality=getattr(result, "quality", None),
            safety=getattr(result, "safety", None),
            tool_use=getattr(result, "tool_use", None),
            personalization=getattr(result, "personalization", None),
            rationale=getattr(result, "rationale", None),
            is_parse_error=bool(getattr(result, "is_parse_error", False)),
            raw_response=getattr(result, "raw_response", None),
            model=getattr(self._judge, "model", None),
            provider=self._provider_name,
            latency_ms=latency_ms,
            error=error,
        )

    def evaluate(self, prompt, response, tools_used=None, context=None, user_profile=None):
        t0 = time.perf_counter()
        try:
            result = self._judge.evaluate(prompt, response, tools_used, context, user_profile)
        except Exception as exc:
            emit_judge(self._event_from(
                prompt, response, tools_used, context, None,
                (time.perf_counter() - t0) * 1000.0, repr(exc)))
            raise
        emit_judge(self._event_from(
            prompt, response, tools_used, context, result,
            (time.perf_counter() - t0) * 1000.0, None))
        return result

    async def a_evaluate(self, prompt, response, tools_used=None, context=None, user_profile=None):
        t0 = time.perf_counter()
        try:
            result = await self._judge.a_evaluate(prompt, response, tools_used, context, user_profile)
        except Exception as exc:
            emit_judge(self._event_from(
                prompt, response, tools_used, context, None,
                (time.perf_counter() - t0) * 1000.0, repr(exc)))
            raise
        emit_judge(self._event_from(
            prompt, response, tools_used, context, result,
            (time.perf_counter() - t0) * 1000.0, None))
        return result


def wrap_judge(judge: Any, provider_name: Optional[str] = None) -> Any:
    """Wrap a judge for observation — returns it UNWRAPPED when no observers are configured."""
    if not get_observers():
        return judge
    return ObservedJudge(judge, provider_name=provider_name)

"""Backend registry — backend id → factory. Mirrors evolution/judge/provider.py.

Adding a provider = implement ``ModelReviewerBackend`` in ``backends/<name>.py`` and add
one ``register(...)`` line here. ``"claude"`` is intentionally NOT registered: it is the
in-session subagent transport (driven by the Claude Code session + the verdict-tracker
hook), not an out-of-band model call this registry can invoke.
"""
from __future__ import annotations

from collections.abc import Callable

from .backends.base import ModelReviewerBackend
from .constants import BACKEND_GLM, BACKEND_GPT, BACKEND_OPENAI_COMPAT

_FACTORIES: dict[str, Callable[[], ModelReviewerBackend]] = {}


def register(backend_id: str, factory: Callable[[], ModelReviewerBackend]) -> None:
    _FACTORIES[backend_id] = factory


def available_backends() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def is_registered(backend_id: str) -> bool:
    return backend_id in _FACTORIES


def get_backend(backend_id: str) -> ModelReviewerBackend:
    factory = _FACTORIES.get(backend_id)
    if factory is None:
        raise KeyError(
            f"unknown review backend '{backend_id}'. "
            f"Registered: {', '.join(available_backends()) or '(none)'}."
        )
    return factory()


def _register_builtins() -> None:
    # Called once at import. Cheap: it only stores factory closures — the heavy backend
    # module (and its deps like httpx via codex_review) is imported lazily INSIDE the
    # factory, so importing this registry never pulls in a backend's dependencies.
    def _codex() -> ModelReviewerBackend:
        from .backends.codex import CodexBackend

        return CodexBackend()

    def _openai_compat() -> ModelReviewerBackend:
        from .backends.openai_compat import OpenAICompatBackend

        return OpenAICompatBackend()

    def _glm() -> ModelReviewerBackend:
        from .backends.glm import GLMBackend

        return GLMBackend()

    register(BACKEND_GPT, _codex)
    register(BACKEND_OPENAI_COMPAT, _openai_compat)
    register(BACKEND_GLM, _glm)


_register_builtins()

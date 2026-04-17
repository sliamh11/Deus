from typing import Callable, Protocol

from .types import RunResult


class SuiteFn(Protocol):
    def __call__(self, argv: list[str]) -> RunResult: ...


_SUITES: dict[str, SuiteFn] = {}


def register(name: str) -> Callable[[SuiteFn], SuiteFn]:
    def _decorator(fn: SuiteFn) -> SuiteFn:
        if name in _SUITES:
            raise ValueError(f"suite {name!r} already registered")
        _SUITES[name] = fn
        return fn
    return _decorator


def get(name: str) -> SuiteFn:
    if name not in _SUITES:
        known = ", ".join(sorted(_SUITES))
        raise KeyError(f"unknown suite {name!r}; known suites: {known or '(none)'}")
    return _SUITES[name]


def list_names() -> list[str]:
    return sorted(_SUITES)

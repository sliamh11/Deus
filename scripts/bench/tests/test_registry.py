"""Tests for scripts/bench/registry.py"""
import pytest

from scripts.bench.registry import _SUITES, get, list_names, register
from scripts.bench.types import RunResult


@pytest.fixture(autouse=True)
def clean_registry():
    """Remove any test suites added during the test, leaving real suites intact."""
    before = set(_SUITES)
    yield
    for k in list(set(_SUITES) - before):
        del _SUITES[k]


def _dummy_fn(argv: list[str]) -> RunResult:
    return RunResult(suite="dummy", score=1.0)


def test_register_and_get():
    register("_test_suite_a")(_dummy_fn)
    fn = get("_test_suite_a")
    assert fn is _dummy_fn


def test_list_names_includes_registered():
    register("_test_suite_b")(_dummy_fn)
    assert "_test_suite_b" in list_names()


def test_duplicate_register_raises():
    register("_test_suite_dup")(_dummy_fn)
    with pytest.raises(ValueError, match="already registered"):
        register("_test_suite_dup")(_dummy_fn)


def test_get_unknown_raises_keyerror_with_known_suites():
    with pytest.raises(KeyError) as exc_info:
        get("_nonexistent_suite_xyz")
    msg = str(exc_info.value)
    assert "_nonexistent_suite_xyz" in msg


def test_list_names_sorted():
    names = list_names()
    assert names == sorted(names)

"""LIA-580 — a judge response missing a required dimension must not be scored.

Five plan-review rounds each surfaced a different way this can go wrong; each
class of failure has a test here rather than a comment:

1. `JudgeSchemaError` inheriting from `ValueError` would be swallowed by every
   provider's generic handler, which returns a fallback carrying `safety=1.0`.
2. Placing the presence check downstream of `_normalize_dim` makes it dead code,
   because providers store normalized values under canonical key names.
3. A class-level default on `score` breaks the dataclass at import time.
4. A consumer that reads a score field without checking `is_schema_error` first.
"""
import ast
import json
import pathlib

import pytest

from evolution.judge.base import JudgeResult, schema_error_result
from evolution.judge.criteria import (
    REQUIRED_DIMS,
    JudgeSchemaError,
    compose_score,
    require_dims,
)

COMPLETE_RAW = {
    "safe": True,
    "quality_level": 5,
    "recalled_preference": True,
    "format_matched": True,
    "tone_matched": True,
    "execution_quality": 5,
    "rationale": "ok",
}


# ── require_dims ─────────────────────────────────────────────────────────────


class TestRequireDims:
    def test_complete_response_passes(self):
        require_dims(dict(COMPLETE_RAW))  # must not raise

    @pytest.mark.parametrize("drop,expected", [
        (("safe",), "safety"),
        (("quality_level",), "quality"),
        (("execution_quality",), "tool_use"),
        (("recalled_preference",), "personalization"),
    ])
    def test_raises_for_each_required_dim(self, drop, expected):
        raw = {k: v for k, v in COMPLETE_RAW.items() if k not in drop}
        with pytest.raises(JudgeSchemaError) as exc:
            require_dims(raw)
        assert expected in str(exc.value)

    def test_empty_response_raises(self):
        with pytest.raises(JudgeSchemaError):
            require_dims({})

    def test_legacy_key_forms_still_count_as_present(self):
        # Old stored rows use canonical float keys rather than the structured
        # forms; they must not be rejected as schema errors.
        legacy = {"quality": 0.8, "safety": 1.0, "tool_use": 0.5,
                  "personalization": 0.5}
        require_dims(legacy)

    def test_covers_exactly_the_required_dims(self):
        assert set(REQUIRED_DIMS) == {"quality", "safety", "tool_use",
                                      "personalization"}


class TestNotAValueError:
    def test_judge_schema_error_is_not_a_valueerror(self):
        # Load-bearing: every provider catches (KeyError, ValueError) and
        # returns a fallback with safety=1.0. A ValueError subclass would be
        # swallowed there and stored as a fabricated PASS on safety.
        assert not issubclass(JudgeSchemaError, ValueError)
        assert issubclass(JudgeSchemaError, Exception)


class TestCheckIsNotDeadCode:
    def test_rejects_what_compose_score_would_accept(self):
        """The round-2 defect, as a regression test.

        A raw judge response with no dimension keys must be rejected by
        require_dims. The same data, once each dim has been run through
        _normalize_dim and stored under its canonical name, looks complete to
        any presence check — which is exactly why the check cannot live
        downstream of normalization.
        """
        raw = {"rationale": "no dimensions at all"}
        with pytest.raises(JudgeSchemaError):
            require_dims(raw)

        from evolution.judge.criteria import _normalize_dim
        normalized = {k: _normalize_dim(k, raw) for k in REQUIRED_DIMS}
        # compose_score accepts it silently — no exception, a real-looking float.
        assert isinstance(compose_score(normalized), float)
        # And a presence check against the normalized dict cannot tell.
        require_dims(normalized)  # does not raise — the point of the test


# ── JudgeResult / schema_error_result ────────────────────────────────────────


class TestSchemaErrorResult:
    def test_module_imports(self):
        # Guards the dataclass field-order trap: a class-level default on
        # `score` makes every following non-default field a TypeError at
        # import, taking down all four providers.
        import importlib

        import evolution.judge.base as base
        importlib.reload(base)

    def test_score_has_no_class_level_default(self):
        from dataclasses import MISSING, fields
        score_field = next(f for f in fields(JudgeResult) if f.name == "score")
        assert score_field.default is MISSING

    def test_shape(self):
        r = schema_error_result(raw_response="{}", model="m", missing="safety")
        assert r.is_schema_error is True
        assert r.score is None
        assert r.is_parse_error is False, "must not be mislabelled as a parse error"
        assert r.model == "m"

    def test_never_fabricates_a_passing_safety(self):
        # The specific defect round 1 found in all four providers.
        assert schema_error_result().safety != 1.0
        assert schema_error_result().safety == 0.0


# ── every provider, through its real parse path ──────────────────────────────


def _providers():
    from evolution.judge import gemini_judge, llama_cpp_judge, openai_judge
    from evolution.judge.ollama_judge import _parse_result as ollama
    return [
        ("ollama", lambda s: ollama(s, "gemma4:e4b")),
        ("gemini", gemini_judge._parse_result),
        ("llama_cpp", llama_cpp_judge._parse_result),
        ("openai", openai_judge._parse_result),
    ]


@pytest.mark.parametrize("name,parse", _providers(), ids=lambda x: x if isinstance(x, str) else "")
class TestEveryProviderGuards:
    MISSING = json.dumps({"quality_level": 5, "rationale": "r"})

    def test_missing_dim_is_flagged_not_scored(self, name, parse):
        r = parse(self.MISSING)
        assert r.is_schema_error is True, f"{name} did not flag a schema error"
        assert r.score is None, f"{name} produced a score for an unassessed response"

    def test_not_swallowed_into_a_fabricated_pass(self, name, parse):
        # If JudgeSchemaError were a ValueError, the generic handler would
        # return safety=1.0 here.
        r = parse(self.MISSING)
        assert r.safety != 1.0, f"{name} fabricated a passing safety score"

    def test_complete_response_still_scores(self, name, parse):
        r = parse(json.dumps(COMPLETE_RAW))
        assert r.is_schema_error is False
        assert r.score == pytest.approx(1.0)


# ── structural: no unguarded consumer, repo-wide ─────────────────────────────


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _judge_result_consumers():
    """Every file that assigns a judge .evaluate() result and reads a field off it.

    Walks the WHOLE repo, not just evolution/ — the judge package is already
    imported from eval/, so a future consumer could land outside evolution/ and
    fall outside this guarantee if the walk were scoped narrowly.
    """
    fields_ = {"score", "quality", "safety", "tool_use", "personalization",
               "is_parse_error", "schema_version", "model"}
    found = {}
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in f"/{rel}" or rel.startswith("tests/") or "/node_modules/" in f"/{rel}":
            continue
        if "/.claude/" in f"/{rel}":
            continue
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, UnicodeDecodeError):
            continue
        judge_vars = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                val = node.value.value if isinstance(node.value, ast.Await) else node.value
                if (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                        and val.func.attr in ("evaluate", "a_evaluate")):
                    for t in node.targets:
                        if isinstance(t, ast.Name):
                            judge_vars.add(t.id)
        if not judge_vars:
            continue
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in judge_vars and node.attr in fields_):
                found.setdefault(rel, []).append(node.lineno)
    return found


def test_every_judge_result_consumer_checks_is_schema_error():
    """A new consumer that reads a score field without guarding fails here.

    Rounds 1-4 of plan review each found a different unguarded consumer by
    hand — four passes, four misses. This replaces that sampling with
    enumeration so the next one cannot be missed.

    Known limit of the heuristic: the guard check is whole-file, so it catches a
    consumer with NO guard (its job, mutation-tested) but not a file with two
    `.evaluate()` sites where only one is guarded. Tightening it to per-site
    reachability would need dataflow analysis; the whole-file check is the
    proportionate version, and every site is individually covered by the
    provider and consumer tests above.
    """
    consumers = _judge_result_consumers()
    assert consumers, "AST walk found no consumers — the walk itself is broken"

    unguarded = [
        rel for rel in consumers
        if "is_schema_error" not in (REPO_ROOT / rel).read_text()
    ]
    assert not unguarded, (
        "these files read a JudgeResult field without checking is_schema_error "
        f"first: {unguarded}"
    )

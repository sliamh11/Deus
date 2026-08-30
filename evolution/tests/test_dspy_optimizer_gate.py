"""Tests for LIA-131 Phase 1: judge-backed GEPA metric + ship-if-better gate.

The gate and program-scorer are pure helpers with no dspy dependency, so they
are unit-tested directly. The activation seam is tested against a real SQLite
temp DB.

`_make_judge_metric` DOES depend on dspy as of the optimizer's activation: it
must return `dspy.Prediction`, not a dict, per the `GEPAFeedbackMetric` protocol.
EVERY test that reaches it therefore carries `importorskip("dspy")`, including
the older metric/scorer tests that predate the dependency -- without the guard
they fail with ModuleNotFoundError instead of skipping (measured on an
interpreter without dspy: 7 failed, 7 passed, 9 skipped).

CI does install dspy, so the guards are not what keeps CI green; they are what
makes this suite behave correctly anywhere else.
"""
import json

import pytest

from evolution.judge.base import JudgeResult
from evolution.optimizer import artifacts
from evolution.optimizer.dspy_optimizer import (
    _make_judge_metric,
    _score_program,
    _should_activate,
)
from evolution.storage.providers.sqlite import SQLiteStorageProvider


# ── Fakes (dspy-free) ───────────────────────────────────────────────────────


class FakeExample(dict):
    """Stands in for a dspy.Example: dict field access + inputs() listing."""

    def __init__(self, input_keys, **fields):
        super().__init__(fields)
        self._input_keys = list(input_keys)

    def inputs(self):
        return self._input_keys


class FakePrediction:
    def __init__(self, **fields):
        self.__dict__.update(fields)


class ConfigurableJudge:
    """A judge whose evaluate() can return a fixed score, flag a parse error,
    or raise — to exercise every metric branch."""

    def __init__(self, score=0.8, is_parse_error=False, raises=False):
        self._score = score
        self._is_parse_error = is_parse_error
        self._raises = raises

    def evaluate(self, prompt, response, tools_used=None, context=None):
        if self._raises:
            raise RuntimeError("judge boom")
        return JudgeResult(
            score=self._score,
            quality=self._score,
            safety=1.0,
            tool_use=1.0,
            personalization=self._score,
            rationale="fake",
            is_parse_error=self._is_parse_error,
        )


# ── Metric: judge-backed, not length-based ──────────────────────────────────


def test_metric_uses_judge_score_not_length():
    """A short answer with a high judge score scores high; a long answer with a
    low judge score scores low — proving length is no longer the signal."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query"], query="q", context="")
    short_good = FakePrediction(answer="yes")          # 3 chars
    long_bad = FakePrediction(answer="x" * 500)         # very long

    high = _make_judge_metric(ConfigurableJudge(score=0.95), "qa")
    low = _make_judge_metric(ConfigurableJudge(score=0.10), "qa")

    assert high(ex, short_good)["score"] == pytest.approx(0.95)
    assert low(ex, long_bad)["score"] == pytest.approx(0.10)


def test_metric_parse_error_scored_zero():
    """A parse-error result must score 0.0, not its fallback neutral score."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query"], query="q")
    pred = FakePrediction(answer="anything")
    metric = _make_judge_metric(
        ConfigurableJudge(score=0.5, is_parse_error=True), "qa"
    )
    assert metric(ex, pred)["score"] == 0.0


def test_metric_exception_scored_zero():
    """A judge that raises yields 0.0 with a failure feedback string."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query"], query="q")
    pred = FakePrediction(answer="x")
    metric = _make_judge_metric(ConfigurableJudge(raises=True), "qa")
    out = metric(ex, pred)
    assert out["score"] == 0.0
    assert "error" in out["feedback"].lower()


def test_metric_module_field_mapping():
    """The module arg selects which prediction field is the response."""
    pytest.importorskip("dspy")
    judge = ConfigurableJudge(score=0.7)
    # tool_selection reads prediction.selected_tools, not .answer
    ex = FakeExample(["query"], query="q", available_tools="a, b")
    pred = FakePrediction(selected_tools="a", answer="ignored")
    metric = _make_judge_metric(judge, "tool_selection")
    assert metric(ex, pred)["score"] == pytest.approx(0.7)


# ── Ship-if-better gate ──────────────────────────────────────────────────────


def test_gate_activates_when_clear_improvement():
    assert _should_activate(0.90, baseline=0.80, active_artifact=None, margin=0.02)


def test_gate_shelves_when_below_margin():
    assert not _should_activate(0.81, baseline=0.80, active_artifact=None, margin=0.02)


def test_gate_shelves_when_active_artifact_is_better():
    """Optimized beats baseline but not the existing active artifact + margin."""
    active = {"optimized_score": 0.90}
    assert not _should_activate(0.85, baseline=0.70, active_artifact=active, margin=0.02)


def test_gate_activates_over_active_artifact():
    active = {"optimized_score": 0.80}
    assert _should_activate(0.90, baseline=0.70, active_artifact=active, margin=0.02)


def test_gate_handles_none_active_score_without_typeerror():
    """A legacy active row with optimized_score=None must not crash the gate."""
    active = {"optimized_score": None}
    # Falls back to baseline=0.70; 0.90 >= 0.72 → activate.
    assert _should_activate(0.90, baseline=0.70, active_artifact=active, margin=0.02)


# ── Program scorer (holdout) ─────────────────────────────────────────────────


def test_score_program_averages_and_survives_failures():
    """A 2-example holdout where one example fails contributes 0.0, no crash."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query"], query="q")
    devset = [ex, ex]
    metric = _make_judge_metric(ConfigurableJudge(score=0.9), "qa")
    # First example returns a good prediction; second raises in forward().
    program = _Program([FakePrediction(answer="good"), RuntimeError("boom")])
    score = _score_program(program, devset, metric)
    assert score == pytest.approx((0.9 + 0.0) / 2)  # 0.45 — not activatable


def test_score_program_empty_holdout_returns_none():
    pytest.importorskip("dspy")
    metric = _make_judge_metric(ConfigurableJudge(), "qa")
    assert _score_program(_Program([]), [], metric) is None


class _Program:
    """Fake DSPy program: yields queued predictions; an Exception is raised."""

    def __init__(self, predictions):
        self._preds = list(predictions)
        self._i = 0

    def forward(self, **kw):
        p = self._preds[self._i]
        self._i += 1
        if isinstance(p, BaseException):
            raise p
        return p


class _ParseErrorOnContentJudge:
    """Flags a parse error when the response contains 'BAD' — lets one sample in
    a holdout return a genuine is_parse_error result via the metric."""

    def __init__(self, good_score=0.9):
        self._good = good_score

    def evaluate(self, prompt, response, tools_used=None, context=None):
        parse_err = "BAD" in response
        return JudgeResult(
            score=0.5 if parse_err else self._good,  # parse-error fallback would
            quality=self._good, safety=1.0, tool_use=1.0,  # look like 0.5 if not
            personalization=self._good, rationale="x",     # clamped to 0.0
            is_parse_error=parse_err,
        )


def test_min_n_holdout_with_parse_error_does_not_activate():
    """Reviewer case: a 2-example holdout where one sample is a genuine judge
    parse-error must not crash and must not clear the ship margin. The metric
    clamps the parse-error sample to 0.0, so mean = (0.9 + 0.0)/2 = 0.45."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query"], query="q")
    devset = [ex, ex]
    metric = _make_judge_metric(_ParseErrorOnContentJudge(good_score=0.9), "qa")
    program = _Program([FakePrediction(answer="ok"), FakePrediction(answer="BAD")])
    optimized_score = _score_program(program, devset, metric)
    assert optimized_score == pytest.approx(0.45)
    assert not _should_activate(optimized_score, baseline=0.80,
                                active_artifact=None, margin=0.02)


# ── Activation seam: real SQLite temp DB ─────────────────────────────────────


@pytest.fixture
def temp_storage(tmp_path, monkeypatch):
    """Point artifacts.get_storage at a temp SQLite DB and artifacts.ARTIFACTS_DIR
    at a temp dir so file writes are isolated."""
    store = SQLiteStorageProvider(db_path=tmp_path / "evo.db")
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir()
    monkeypatch.setattr(artifacts, "get_storage", lambda: store)
    monkeypatch.setattr(artifacts, "ARTIFACTS_DIR", art_dir)
    return store, art_dir


def test_save_artifact_activate_false_persists_inactive(temp_storage):
    store, art_dir = temp_storage
    mod = "qa"

    aid_a = artifacts.save_artifact(mod, "PROMPT A", optimized_score=0.80, activate=True)
    aid_b = artifacts.save_artifact(mod, "PROMPT B", optimized_score=0.79, activate=False)

    # A is still the active artifact; B was shelved.
    active = store.get_active_artifact(mod)
    assert active["id"] == aid_a

    rows = {r["id"]: r for r in store.list_artifacts(module=mod)}
    assert rows[aid_a]["active"] == 1
    assert rows[aid_b]["active"] == 0

    # latest.json must still point at A (the shelved B must not clobber it).
    latest = json.loads((art_dir / f"{mod}-latest.json").read_text())
    assert latest["id"] == aid_a
    # B still gets a versioned audit copy.
    versioned = list(art_dir.glob(f"{mod}-*.json"))
    assert any(json.loads(p.read_text())["id"] == aid_b
               for p in versioned if p.name != f"{mod}-latest.json")


def test_save_artifact_activate_true_updates_latest_and_active(temp_storage):
    store, art_dir = temp_storage
    mod = "qa"

    artifacts.save_artifact(mod, "PROMPT A", optimized_score=0.70, activate=True)
    aid_b = artifacts.save_artifact(mod, "PROMPT B", optimized_score=0.90, activate=True)

    assert store.get_active_artifact(mod)["id"] == aid_b
    latest = json.loads((art_dir / f"{mod}-latest.json").read_text())
    assert latest["id"] == aid_b


# ── optimize()-level: judge-unavailable abort (needs dspy) ───────────────────


def test_optimize_aborts_when_no_judge(monkeypatch):
    pytest.importorskip("dspy")
    from evolution.judge import NoProviderAvailableError
    from evolution.optimizer import dspy_optimizer

    def _raise(*a, **kw):
        raise NoProviderAvailableError("no provider")

    monkeypatch.setattr(dspy_optimizer, "make_runtime_judge", _raise)
    # Should return None without raising and without producing an artifact.
    assert dspy_optimizer.optimize(module="qa") is None


# ── GEPA protocol conformance (LIA-556 activation) ───────────────────────────
#
# GEPAFeedbackMetric returns `float | ScoreWithFeedback`, where
# ScoreWithFeedback subclasses dspy.Prediction with `score: float` and
# `feedback: str`. A plain dict is NOT accepted. These pin that contract at the
# boundary, because the failure it guards against is silent: GEPA rejects the
# return type deep inside compile(), and cli.py:174 swallows the exception.


def _assert_is_gepa_feedback(out):
    """A GEPA-protocol return: a dspy.Prediction carrying float + str."""
    import dspy

    assert isinstance(out, dspy.Prediction), f"expected dspy.Prediction, got {type(out)}"
    assert type(out.score) is float, f"score must be float, got {type(out.score)}"
    assert type(out.feedback) is str, f"feedback must be str, got {type(out.feedback)}"


def test_metric_returns_prediction_not_dict_on_normal_result():
    pytest.importorskip("dspy")
    ex = FakeExample(["query", "context"], query="q", context="c")
    metric = _make_judge_metric(ConfigurableJudge(score=0.75), "qa")
    out = metric(ex, FakePrediction(answer="a"))
    _assert_is_gepa_feedback(out)
    assert out.score == pytest.approx(0.75)


def test_metric_returns_prediction_on_parse_error():
    pytest.importorskip("dspy")
    ex = FakeExample(["query", "context"], query="q", context="c")
    metric = _make_judge_metric(ConfigurableJudge(score=0.9, is_parse_error=True), "qa")
    out = metric(ex, FakePrediction(answer="a"))
    _assert_is_gepa_feedback(out)
    assert out.score == 0.0


def test_metric_returns_prediction_on_exception():
    pytest.importorskip("dspy")
    ex = FakeExample(["query", "context"], query="q", context="c")
    metric = _make_judge_metric(ConfigurableJudge(raises=True), "qa")
    out = metric(ex, FakePrediction(answer="a"))
    _assert_is_gepa_feedback(out)
    assert out.score == 0.0
    # The exception branch must not be reachable via a NameError on `dspy`:
    # that would report a real judge score as a plumbing fault (LIA-580).
    assert "NameError" not in out.feedback


def test_metric_return_is_subscriptable_for_score_program():
    """_score_program does `metric(ex, pred)["score"]`. dspy.Prediction supports
    subscript, so that call site stays correct without being touched. Pinned
    here because a future switch to a plain namespace would break it silently."""
    pytest.importorskip("dspy")
    ex = FakeExample(["query", "context"], query="q", context="c")
    metric = _make_judge_metric(ConfigurableJudge(score=0.42), "qa")
    assert metric(ex, FakePrediction(answer="a"))["score"] == pytest.approx(0.42)


def test_metric_returns_prediction_on_schema_error():
    """is_schema_error takes its own branch before is_parse_error. Set the flag
    explicitly rather than relying on a mock's fabricated attribute: a MagicMock
    answers truthy for ANY attribute, which would make this pass regardless of
    the branch actually taken."""
    pytest.importorskip("dspy")

    class _SchemaErrorJudge:
        def evaluate(self, prompt, response, tools_used=None, context=None):
            r = JudgeResult(
                score=0.9, quality=0.9, safety=1.0, tool_use=1.0,
                personalization=0.9, rationale="fake",
            )
            r.is_schema_error = True
            r.is_parse_error = False
            return r

    ex = FakeExample(["query", "context"], query="q", context="c")
    out = _make_judge_metric(_SchemaErrorJudge(), "qa")(ex, FakePrediction(answer="a"))
    _assert_is_gepa_feedback(out)
    assert out.score == 0.0
    assert "schema error" in out.feedback


# ── _setup_dspy returns the LM, and GEPA receives it as reflection_lm ─────────


def _stub_dspy_lm(monkeypatch):
    """Replace dspy.LM/configure with sentinels so no network call or API key is
    needed, and the returned object is identifiable per branch."""
    import dspy

    made = []

    def _fake_lm(target, **kw):
        obj = object()
        made.append((target, obj))
        return obj

    monkeypatch.setattr(dspy, "LM", _fake_lm)
    monkeypatch.setattr(dspy, "configure", lambda **kw: None)
    return made


def _stub_ollama(monkeypatch, available):
    """Point JudgeRegistry at a provider whose availability we control.
    Patched on evolution.judge.provider because _setup_dspy imports the symbol
    inside the function body, so a module-level patch would not be seen."""
    from evolution.judge import provider as prov

    class _Provider:
        def is_available(self):
            return available

    class _Registry:
        @staticmethod
        def default():
            return _Registry()

        def get(self, name):
            return _Provider()

    monkeypatch.setattr(prov, "JudgeRegistry", _Registry)


def test_setup_dspy_returns_lm_on_ollama_branch(monkeypatch):
    """dspy.GEPA.__init__ asserts reflection_lm is not None, so _setup_dspy
    returning None is what made optimize() raise AssertionError.

    Every boundary is stubbed: no live Ollama probe, no API key. An earlier
    version of this test called the real _setup_dspy() and passed only because
    Ollama happened to be running on the dev box -- in a clean environment it
    would have fallen through to the Gemini branch and died in load_api_key().
    """
    pytest.importorskip("dspy")
    from evolution.optimizer import dspy_optimizer

    made = _stub_dspy_lm(monkeypatch)
    _stub_ollama(monkeypatch, available=True)

    lm = dspy_optimizer._setup_dspy()

    assert lm is not None
    assert made, "dspy.LM was never constructed"
    target, obj = made[-1]
    assert target.startswith("ollama/"), f"expected the Ollama branch, got {target}"
    assert lm is obj, "the returned LM is not the one that was configured"


def test_setup_dspy_returns_lm_on_gemini_fallback(monkeypatch):
    """The fallback branch must also return its LM — a None here would restore
    the AssertionError for anyone without a local Ollama."""
    pytest.importorskip("dspy")
    from evolution.optimizer import dspy_optimizer

    made = _stub_dspy_lm(monkeypatch)
    _stub_ollama(monkeypatch, available=False)
    monkeypatch.setattr(dspy_optimizer, "load_api_key", lambda: "test-key-not-real")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")

    lm = dspy_optimizer._setup_dspy()

    assert lm is not None
    target, obj = made[-1]
    assert target.startswith("gemini/"), f"expected the Gemini branch, got {target}"
    assert lm is obj


class _StopAtCompile(Exception):
    """Ends optimize() the instant GEPA is built, before any real GEPA work."""


def test_gepa_is_constructed_with_reflection_lm(test_db, monkeypatch):
    """Regression guard for the AssertionError at dspy.GEPA construction.

    Captures the kwargs GEPA is built with and asserts `reflection_lm` is the
    very object `_setup_dspy` returned - identity, not merely non-None, so a
    future refactor that passes some other truthy LM still fails here.

    `test_db` is mandatory: optimize() reads the interaction store, and
    conftest's guard refuses the real ~/.deus/evolution.db (LIA-551 zeroed a
    genuine failure streak that way).
    """
    pytest.importorskip("dspy")
    import dspy
    from evolution.optimizer import dspy_optimizer

    sentinel = object()
    captured = {}

    class _StubGEPA:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def compile(self, program, trainset=None):
            raise _StopAtCompile()

    # Enough scored rows to clear DSPY_MIN_SAMPLES without touching real data.
    rows = [
        {"prompt": f"q{i}", "response": f"a{i}", "judge_score": 0.8, "judge_dims": "{}"}
        for i in range(40)
    ]

    monkeypatch.setattr(dspy, "GEPA", _StubGEPA)
    monkeypatch.setattr(dspy_optimizer, "_setup_dspy", lambda *a, **kw: sentinel)
    monkeypatch.setattr(dspy_optimizer, "get_recent", lambda **kw: rows)
    monkeypatch.setattr(
        dspy_optimizer, "make_runtime_judge", lambda *a, **kw: ConfigurableJudge(score=0.8)
    )
    # _score_program would run the un-optimized program against a real LM.
    monkeypatch.setattr(dspy_optimizer, "_score_program", lambda *a, **kw: 0.5)

    with pytest.raises(_StopAtCompile):
        dspy_optimizer.optimize(module="qa")

    assert captured, "GEPA was never constructed — the test never reached the seam"
    assert captured["reflection_lm"] is sentinel

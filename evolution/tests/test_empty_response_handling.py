"""LIA-558: an absent agent response must not be read as evidence.

Two halves, both regression-critical:

1. Every prompt builder renders an absent response explicitly, and renders a
   present one byte-identically. The byte-identity assertion is what makes it
   safe to skip a fixture re-measurement: if the prompt for a non-empty response
   is unchanged, its score cannot move.

2. Reflection paths whose trigger is the JUDGE's own score withhold the learning
   signal for an absent response. Paths triggered by a HUMAN signal do not — a
   person supplied the evidence there.
"""
import pytest

from evolution.judge.criteria import (
    RUBRIC,
    EMPTY_RESPONSE_SENTINEL,
    render_response,
)
from evolution.judge.ollama_judge import _build_eval_prompt as ollama_build
from evolution.judge.gemini_judge import _build_eval_prompt as gemini_build
from evolution.judge.llama_cpp_judge import _build_eval_prompt as llama_build
from evolution.judge.openai_judge import _build_eval_prompt as openai_build
from evolution.training.build_judge_lora_dataset import build_eval_prompt as lora_build
from evolution.reflexion.generator import response_supports_reflection


def _call(build, prompt, response):
    """Call a builder with no tools and no context.

    All five accept (prompt, response, tools_used, context) positionally — the
    judge builders require the last two, the LoRA builder defaults them — so one
    call shape covers every builder without swallowing a real TypeError.
    """
    return build(prompt, response, None, None)


ALL_BUILDERS = [
    pytest.param(ollama_build, id="ollama"),
    pytest.param(gemini_build, id="gemini"),
    pytest.param(llama_build, id="llama_cpp"),
    pytest.param(openai_build, id="openai"),
    pytest.param(lora_build, id="lora_dataset"),
]


# ── Half 1: every builder renders an absent response explicitly ───────────────


@pytest.mark.parametrize("build", ALL_BUILDERS)
@pytest.mark.parametrize("absent", [None, "", "   ", "\n\t "])
def test_every_builder_marks_an_absent_response(build, absent):
    out = _call(build, "Do the thing.", absent)
    assert EMPTY_RESPONSE_SENTINEL in out
    # And never leaves the section blank, which is the actual defect.
    assert "**Agent response:**\n\n" not in out


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_every_builder_passes_a_real_response_through(build):
    out = _call(build, "Do the thing.", "the thing is done, here is the output")
    assert "the thing is done, here is the output" in out
    assert EMPTY_RESPONSE_SENTINEL not in out


# ── The regression test that removes the need for a fixture re-measurement ────
# Frozen literal, assembled by hand rather than by calling render_response, so
# the assertion cannot go tautological if render_response itself regresses.


def _frozen(prompt, response):
    """The prompt as it was constructed before this change, assembled by hand.

    Not built via render_response, so the assertion cannot go tautological if
    render_response itself regresses.
    """
    return "\n".join([
        RUBRIC,
        "\n## Interaction to evaluate\n",
        f"**User prompt:**\n{prompt}\n",
        f"**Agent response:**\n{response}\n",
    ])


# Parametrised across all five builders rather than frozen once for ollama: with
# no tools, context or profile they must emit the SAME bytes, so a single literal
# pins each builder AND proves they still agree with each other — the "same shape
# as the production judge eval prompt" promise build_judge_lora_dataset.py makes.


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_non_empty_prompt_is_byte_identical_to_the_pre_change_construction(build):
    prompt, response = "What is 2+2?", "4"
    assert _call(build, prompt, response) == _frozen(prompt, response)


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_byte_identity_holds_with_whitespace_padded_content(build):
    # The one case a naive `response.strip()` implementation would break.
    prompt, response = "Echo it.", "  padded  "
    assert _call(build, prompt, response) == _frozen(prompt, response)


@pytest.mark.parametrize("build", ALL_BUILDERS)
def test_absent_response_prompt_is_byte_identical_across_builders(build):
    # The empty case pinned the same way, so no builder can drift to a different
    # marker or a different placement without failing here.
    assert _call(build, "Do the thing.", "") == _frozen(
        "Do the thing.", EMPTY_RESPONSE_SENTINEL
    )


# ── Half 2: the judge-driven reflection guard ─────────────────────────────────


@pytest.mark.parametrize("absent", [None, "", "   ", "\n\t "])
def test_absent_response_does_not_support_a_reflection(absent):
    assert response_supports_reflection(absent) is False


@pytest.mark.parametrize("present", ["x", " x ", "Done.", EMPTY_RESPONSE_SENTINEL])
def test_present_response_supports_a_reflection(present):
    # Including the sentinel itself: if an agent really emitted that text, it is
    # a response and the guard must not swallow it.
    assert response_supports_reflection(present) is True


@pytest.mark.parametrize(
    "fn_name, score",
    [("generate_reflection", 0.1), ("generate_positive_reflection", 0.95)],
    ids=["corrective_template", "positive_template"],
)
def test_generator_prompts_mark_an_absent_response(monkeypatch, fn_name, score):
    """Human-signal paths still reach the generator with an empty response.

    The human's trigger stays authoritative — this asserts the generator is NOT
    guarded — but it must not be handed a blank `Assistant:` section to
    confabulate from. Both templates, since both are reachable from a human path
    (thumbs-down and thumbs-up respectively).
    """
    from evolution.reflexion import generator as gen

    seen = {}

    def fake_generate(prompt, model=None):
        seen["prompt"] = prompt
        return "1. Some lesson learned about the interaction."

    monkeypatch.setattr(gen, "generate", fake_generate)
    content, _category = getattr(gen, fn_name)(
        prompt="Do the thing.", response="", score=score
    )

    # It generated — the human path is not suppressed.
    assert content
    # And it saw the marker, not a blank section.
    assert EMPTY_RESPONSE_SENTINEL in seen["prompt"]
    assert "Assistant: \n" not in seen["prompt"]


def test_extract_principles_skips_when_every_candidate_is_empty(monkeypatch):
    """The pool filter must run BEFORE the `< 3` count check.

    Filtering afterwards would let an all-empty pool satisfy the count, render
    "(none)", and store principles hallucinated from nothing — worse than the
    defect being fixed.
    """
    from evolution.reflexion import principles as pr

    rows = [
        {"id": f"i{i}", "prompt": "p" * 50, "response": "", "judge_score": 0.9}
        for i in range(5)
    ]
    monkeypatch.setattr(pr, "get_recent", lambda **kw: rows)
    monkeypatch.setattr(pr, "_count_new_scored", lambda domain: 999)

    called = {"generate": 0, "saved": 0}

    def fake_generate(prompt, model=None):
        called["generate"] += 1
        # Digit-prefixed so it WOULD be stored — principles.py keeps only lines
        # whose first character isdigit(). A non-digit mock would make this test
        # pass for the wrong reason.
        return "1. A principle that should never be extracted from nothing."

    monkeypatch.setattr(pr, "generate", fake_generate)
    monkeypatch.setattr(pr, "save_reflection", lambda **kw: called.__setitem__("saved", called["saved"] + 1))

    assert pr.extract_principles(force=True) is None
    assert called["generate"] == 0
    assert called["saved"] == 0


def test_reflect_single_skips_an_empty_response_without_calling_the_generator(monkeypatch):
    """Defence in depth behind the caller-side filter.

    The batch path filters empties out of needs_reflection so the health
    denominator stays honest; this guard stops a future caller reintroducing one.
    """
    from evolution import maintenance as mt

    called = {"n": 0}

    def boom(**kw):
        called["n"] += 1
        raise AssertionError("generator must not be reached for an empty response")

    monkeypatch.setattr("evolution.reflexion.generator.generate_reflection", boom)
    monkeypatch.setattr("evolution.reflexion.generator.generate_positive_reflection", boom)

    def scored_with(response, score):
        return {
            "row": {"id": "x", "prompt": "p", "response": response, "metrics": None},
            "score": score,
            "dims": {},
            "rationale": "r",
        }

    config = {"reflection_threshold": 0.6, "positive_threshold": 0.85}

    assert mt._reflect_single(scored_with("   ", score=0.05), config) is True
    assert called["n"] == 0

    # BOTH polarities. Guarding only the corrective half would half-apply the
    # rule: a blank response supports "this was exemplary" no better than it
    # supports "this failed", and 305 live rows currently clear 0.85.
    assert mt._reflect_single(scored_with("", score=0.95), config) is True
    assert called["n"] == 0

    # Positive control: without it this test would pass even if the monkeypatch
    # never intercepted, since the assertion is that nothing was called. A real
    # response must reach the (exploding) generator — on each polarity.
    assert mt._reflect_single(scored_with("a real answer", score=0.05), config) is False
    assert called["n"] == 1
    assert mt._reflect_single(scored_with("a real answer", score=0.95), config) is False
    assert called["n"] == 2


def test_extract_principles_still_runs_on_real_responses(monkeypatch, test_db):
    """The guard must not disable principle extraction outright.

    Takes test_db because a successful extraction records its own bookkeeping —
    the conftest guard correctly refuses to let that reach ~/.deus.
    """
    from evolution.reflexion import principles as pr

    rows = [
        {"id": f"i{i}", "prompt": "p" * 50, "response": f"a real answer {i}", "judge_score": 0.9}
        for i in range(5)
    ]
    monkeypatch.setattr(pr, "get_recent", lambda **kw: rows)
    monkeypatch.setattr(pr, "_count_new_scored", lambda domain: 999)

    saved = []
    monkeypatch.setattr(pr, "generate", lambda prompt, model=None: "1. A real principle.")
    monkeypatch.setattr(pr, "save_reflection", lambda **kw: saved.append(kw))

    assert pr.extract_principles(force=True) is not None
    assert len(saved) == 1

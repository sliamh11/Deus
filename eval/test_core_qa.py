"""
Suite 1: Core QA & Reasoning

Tests factual accuracy, multi-step reasoning, and instruction following.
Metrics: AnswerRelevancyMetric, GEval (correctness), GEval (instruction following),
         EfficiencyMetric.
"""

import sys
from pathlib import Path

import pytest
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, GEval
from deepeval.test_case import LLMTestCaseParams

sys.path.insert(0, str(Path(__file__).parent))

from conftest import load_dataset, make_test_case, EVAL_BACKENDS
from metrics.efficiency_metric import EfficiencyMetric

DATASET = load_dataset("core_qa")


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize("case_data", DATASET, ids=[c["id"] for c in DATASET])
def test_answer_relevancy(case_data, backend, agent, judge, thresholds):
    """Response must be relevant to the question asked."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("core_qa", {}).get("answer_relevancy", 0.7)
    assert_test(test_case, [AnswerRelevancyMetric(threshold=threshold, model=judge)])


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize("case_data", DATASET, ids=[c["id"] for c in DATASET])
def test_correctness(case_data, backend, agent, judge, thresholds):
    """Response must contain factually accurate information."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("core_qa", {}).get("correctness", 0.7)
    metric = GEval(
        name="Correctness",
        criteria=(
            "Does the response contain factually accurate information that aligns "
            "with the expected output? Award full marks if key facts match even when "
            "phrasing differs. Penalize hallucinated or contradictory facts."
        ),
        evaluation_params=[
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
        threshold=threshold,
        model=judge,
    )
    assert_test(test_case, [metric])


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize(
    "case_data",
    [c for c in DATASET if c["metadata"].get("category") == "instruction_following"],
    ids=[c["id"] for c in DATASET if c["metadata"].get("category") == "instruction_following"],
)
def test_instruction_following(case_data, backend, agent, judge, thresholds):
    """Response must strictly follow format/structural constraints in the prompt."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("core_qa", {}).get("instruction_following", 0.8)
    metric = GEval(
        name="InstructionFollowing",
        criteria=(
            "Does the response follow all format and structural instructions given "
            "in the input? Check constraints such as number of bullet points, word "
            "limits, and output structure. Award full marks only if all constraints "
            "are satisfied."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
        ],
        threshold=threshold,
        model=judge,
    )
    assert_test(test_case, [metric])


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize("case_data", DATASET, ids=[c["id"] for c in DATASET])
def test_latency(case_data, backend, agent, thresholds):
    """Response must arrive within the latency budget."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    difficulty = case_data["metadata"].get("difficulty", "medium")
    max_ms_key = "max_latency_hard_ms" if difficulty == "hard" else "max_latency_ms"
    max_ms = thresholds.get("core_qa", {}).get(max_ms_key, 120_000)

    assert_test(test_case, [EfficiencyMetric(max_latency_ms=max_ms, latency_ms=response.latency_ms)])

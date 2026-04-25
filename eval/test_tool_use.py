"""
Suite 2: Tool Use & Multi-Step Tasks

Tests correct tool selection, MCP tool call evidence via IPC file inspection,
and multi-step planning quality.
Metrics: GEval (tool selection), ToolUseMetric (IPC evidence), GEval (plan quality),
         EfficiencyMetric.
"""

import sys
from pathlib import Path

import pytest
from deepeval import assert_test
from deepeval.metrics import GEval
from deepeval.test_case import LLMTestCaseParams

sys.path.insert(0, str(Path(__file__).parent))

from conftest import load_dataset, make_test_case, EVAL_BACKENDS
from metrics.tool_use_metric import ToolUseMetric
from metrics.efficiency_metric import EfficiencyMetric

DATASET = load_dataset("tool_use")


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize("case_data", DATASET, ids=[c["id"] for c in DATASET])
def test_tool_selection(case_data, backend, agent, judge, thresholds):
    """Agent should confirm the appropriate action was taken for the task."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("tool_use", {}).get("tool_selection", 0.8)
    metric = GEval(
        name="ToolSelection",
        criteria=(
            "Based on the task in the input, did the response indicate that the "
            "appropriate action was taken? For message tasks, confirm a message was "
            "sent. For scheduling tasks, confirm a task was scheduled with correct "
            "parameters. For listing tasks, confirm a list was returned."
        ),
        evaluation_params=[
            LLMTestCaseParams.INPUT,
            LLMTestCaseParams.ACTUAL_OUTPUT,
            LLMTestCaseParams.EXPECTED_OUTPUT,
        ],
        threshold=threshold,
        model=judge,
    )
    assert_test(test_case, [metric])


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize("case_data", DATASET, ids=[c["id"] for c in DATASET])
def test_tool_evidence(case_data, backend, agent, thresholds):
    """Verify tool calls via IPC file evidence from the mounted temp directory."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("tool_use", {}).get("tool_evidence", 0.8)
    metric = ToolUseMetric(
        expected_tools=case_data.get("expected_tools", []),
        ipc_messages=response.ipc_messages,
        ipc_tasks=response.ipc_tasks,
        threshold=threshold,
    )
    assert_test(test_case, [metric])


@pytest.mark.parametrize("backend", EVAL_BACKENDS)
@pytest.mark.parametrize(
    "case_data",
    [c for c in DATASET if c["metadata"].get("category") == "multi_tool"],
    ids=[c["id"] for c in DATASET if c["metadata"].get("category") == "multi_tool"],
)
def test_plan_quality(case_data, backend, agent, judge, thresholds):
    """Multi-step tasks should show logical sequencing and correct handling of dependencies."""
    response = agent(case_data["input"], backend=backend)
    test_case = make_test_case(case_data, response)

    threshold = thresholds.get("tool_use", {}).get("plan_quality", 0.7)
    metric = GEval(
        name="PlanQuality",
        criteria=(
            "For this multi-step task, did the agent demonstrate good planning? "
            "Did it address all sub-tasks, execute them in a logical order, "
            "and handle any dependencies correctly (e.g., scheduling before confirming)?"
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

    max_ms = thresholds.get("tool_use", {}).get("max_latency_ms", 120_000)
    assert_test(test_case, [EfficiencyMetric(max_latency_ms=max_ms, latency_ms=response.latency_ms)])

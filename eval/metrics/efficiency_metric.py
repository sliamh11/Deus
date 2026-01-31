"""
Custom DeepEval metric for tracking response latency.

Latency is captured as wall-clock time by agent_wrapper.py. Token counting
can be added later once the agent exposes usage data.
"""

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase


class EfficiencyMetric(BaseMetric):
    """
    Passes if wall-clock latency is within the configured budget.
    Score is 1.0 when within budget, decreasing proportionally when over.
    """

    def __init__(
        self,
        max_latency_ms: float,
        latency_ms: float,
        threshold: float = 1.0,
    ):
        self.max_latency_ms = max_latency_ms
        self.latency_ms = latency_ms
        self.threshold = threshold
        self.score = 0.0
        self.reason = ""
        self.success = False

    async def a_measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        return self.measure(test_case)

    def measure(self, test_case: LLMTestCase) -> float:
        if self.latency_ms <= self.max_latency_ms:
            self.score = 1.0
            self.reason = (
                f"Latency {self.latency_ms:.0f}ms ≤ budget {self.max_latency_ms:.0f}ms"
            )
        else:
            overshoot = self.latency_ms / self.max_latency_ms
            self.score = max(0.0, 1.0 - (overshoot - 1.0))
            self.reason = (
                f"Latency {self.latency_ms:.0f}ms exceeds budget "
                f"{self.max_latency_ms:.0f}ms ({overshoot:.1%} of budget)"
            )
        self.success = self.score >= self.threshold
        return self.score

    def is_successful(self) -> bool:
        return self.success

    @property
    def __name__(self):
        return "EfficiencyMetric"

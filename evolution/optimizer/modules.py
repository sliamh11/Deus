"""
DSPy module definitions for Deus task types.
Each module maps to a learnable prompt that the optimizer can improve.
"""
try:
    import dspy
    _DSPY_AVAILABLE = True
except ImportError:
    _DSPY_AVAILABLE = False
    dspy = None  # type: ignore


def _require_dspy():
    if not _DSPY_AVAILABLE:
        raise ImportError(
            "dspy-ai is required for prompt optimization. "
            "Install with: pip install dspy-ai"
        )


class QAModule(dspy.Module if _DSPY_AVAILABLE else object):
    """
    General question answering with optional context and reflexion lessons.
    Signature: query, context, reflections → answer
    """
    def __init__(self):
        _require_dspy()
        super().__init__()
        self._predict = dspy.Predict(
            "query, context, reflections -> answer",
        )

    def forward(self, query: str, context: str = "", reflections: str = "") -> dspy.Prediction:
        return self._predict(
            query=query,
            context=context,
            reflections=reflections,
        )


class ToolSelectionModule(dspy.Module if _DSPY_AVAILABLE else object):
    """
    Given a user query and a list of available tools, select the right ones.
    Signature: query, available_tools → selected_tools, rationale
    """
    def __init__(self):
        _require_dspy()
        super().__init__()
        self._predict = dspy.Predict(
            "query, available_tools -> selected_tools, rationale",
        )

    def forward(self, query: str, available_tools: str = "") -> dspy.Prediction:
        return self._predict(query=query, available_tools=available_tools)


class SummarizationModule(dspy.Module if _DSPY_AVAILABLE else object):
    """
    Summarize a conversation history for memory write-back.
    Signature: conversation_history → summary
    """
    def __init__(self):
        _require_dspy()
        super().__init__()
        self._predict = dspy.Predict(
            "conversation_history -> summary",
        )

    def forward(self, conversation_history: str) -> dspy.Prediction:
        return self._predict(conversation_history=conversation_history)


MODULE_REGISTRY = {
    "qa": QAModule,
    "tool_selection": ToolSelectionModule,
    "summarization": SummarizationModule,
}

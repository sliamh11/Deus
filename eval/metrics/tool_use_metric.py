"""
Custom DeepEval metric that inspects IPC files to verify tool call evidence.

The Deus MCP server (ipc-mcp-stdio.ts) writes JSON files to:
  /workspace/ipc/messages/*.json  — for send_message calls
  /workspace/ipc/tasks/*.json     — for schedule_task, pause_task, etc.

By mounting a temp dir at /workspace/ipc during eval, we capture exact evidence
of which tools the agent called without modifying any agent code.
"""

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase


# Maps tool name -> (ipc directory key, expected "type" field in JSON)
TOOL_IPC_MAP: dict[str, tuple[str, str]] = {
    "send_message":   ("messages", "message"),
    "schedule_task":  ("tasks",    "schedule_task"),
    "pause_task":     ("tasks",    "pause_task"),
    "resume_task":    ("tasks",    "resume_task"),
    "cancel_task":    ("tasks",    "cancel_task"),
    "update_task":    ("tasks",    "update_task"),
    "register_group": ("tasks",    "register_group"),
}

# Read-only tools that don't produce IPC files — inferred from response text
READ_ONLY_TOOLS: dict[str, str] = {
    "list_tasks": "task",  # keyword to check in response
    "get_task":   "task",
    "list_groups": "group",
}


class ToolUseMetric(BaseMetric):
    """
    Checks whether expected MCP tools were actually invoked by inspecting
    the IPC evidence captured from the container's temp IPC directory.
    """

    def __init__(
        self,
        expected_tools: list[str],
        ipc_messages: list[dict],
        ipc_tasks: list[dict],
        threshold: float = 0.8,
    ):
        self.expected_tools = expected_tools
        self.ipc_messages = ipc_messages
        self.ipc_tasks = ipc_tasks
        self.threshold = threshold
        self.score = 0.0
        self.reason = ""
        self.success = False

    def measure(self, test_case: LLMTestCase) -> float:
        if not self.expected_tools:
            self.score = 1.0
            self.reason = "No tools expected."
            self.success = True
            return self.score

        found: set[str] = set()

        for msg in self.ipc_messages:
            if msg.get("type") == "message":
                found.add("send_message")

        for task in self.ipc_tasks:
            task_type = task.get("type", "")
            for tool, (_, ipc_type) in TOOL_IPC_MAP.items():
                if task_type == ipc_type:
                    found.add(tool)

        actual = (test_case.actual_output or "").lower()
        for tool, keyword in READ_ONLY_TOOLS.items():
            if tool in self.expected_tools and keyword in actual:
                found.add(tool)

        expected = set(self.expected_tools)
        matched = expected & found
        self.score = len(matched) / len(expected) if expected else 1.0
        self.reason = (
            f"Expected: {sorted(expected)} | "
            f"Found evidence for: {sorted(found)} | "
            f"Match: {self.score:.0%}"
        )
        self.success = self.score >= self.threshold
        return self.score

    def is_successful(self) -> bool:
        return self.success

    @property
    def __name__(self):
        return "ToolUseMetric"

#!/usr/bin/env python3
"""UserPromptSubmit hook: semantic auto-retrieval with session concept expansion."""
from __future__ import annotations

import json
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

MIN_PROMPT_LEN = 10
TOP_K = 3
MAX_CONTEXT_CHARS = 4096


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return

    prompt = data.get("prompt", "")
    if not prompt or len(prompt) < MIN_PROMPT_LEN:
        return

    session_id = data.get("session_id", "")

    # Deferred: avoid ~200ms Ollama import on early bail-out paths above.
    import session_concepts as sc
    import memory_query as mq

    concepts: list[str] | None = None
    if session_id:
        new_terms = sc.extract_terms(prompt)
        concepts = sc.update_concepts(session_id, new_terms) or None

    # Threshold resolution belongs to the library: memory_query falls back to
    # mt.DEFAULT_ABSTAIN_THRESHOLD (env var -> learned artifact -> provider
    # default). A hook-level default would override learned artifacts and
    # provider-aware calibration.
    result = mq.recall(
        prompt,
        k=TOP_K,
        source="repo-hook",
        concepts=concepts,
    )

    context = result["context"]
    if not context:
        return

    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n=== [truncated] ==="

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": context,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"[deus hook] {e}\n")
    sys.exit(0)

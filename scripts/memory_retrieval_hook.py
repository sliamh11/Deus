#!/usr/bin/env python3
"""UserPromptSubmit hook: semantic auto-retrieval with session concept expansion."""
from __future__ import annotations

import json
import os
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

    # Let the library own threshold resolution (env -> learned artifact ->
    # provider-aware default). Pass None unless DEUS_TREE_ABSTAIN is set to a
    # non-empty value, so memory_query.recall falls back to
    # memory_tree.DEFAULT_ABSTAIN_THRESHOLD instead of a hook-local hardcode.
    # (Empty/whitespace is treated as unset, not float("") -> ValueError.)
    _env_abstain = os.environ.get("DEUS_TREE_ABSTAIN", "").strip()
    abstain = float(_env_abstain) if _env_abstain else None

    # LIA-334: procedure-memory surfacing is opt-in. recall() is dormant-by-
    # default (its default excludes {"standard","procedure"}), so passing None
    # keeps procedures hidden — the kill-switch. To OPT IN we pass {"standard"}
    # (procedures eligible, "standard" still excluded as usual). Measured neutral
    # on the 136-query benchmark (recall/abstain_acc identical on/off; LIA-334).
    # Strict binary: only "1" enables. "true"/"yes"/"on" are intentionally NOT
    # accepted — keep the kill-switch unambiguous for future maintainers.
    _proc_on = os.environ.get("DEUS_PROCEDURE_MEMORY", "").strip() == "1"
    exclude_kinds: set[str] | None = {"standard"} if _proc_on else None

    result = mq.recall(
        prompt,
        k=TOP_K,
        abstain_threshold=abstain,
        source="repo-hook",
        concepts=concepts,
        exclude_kinds=exclude_kinds,
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

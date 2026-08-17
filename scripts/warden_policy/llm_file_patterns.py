"""Shared file-pattern constants for detecting LLM/prompt-sensitive diffs.

Single source of truth for both the Claude Code ai-eng-warden gate
(``codex_warden_hooks.py``'s ``run_ai_eng_gate``) and the Hermes-side
ai-eng-warden gate (``hermes_ai_eng_warden_gate.py``) — extracted so the two
harnesses can never silently drift on what counts as an LLM-sensitive file.
"""
from __future__ import annotations

# Files that assemble prompts or call LLM APIs directly
AI_ENG_BASENAMES = {
    "linear-dispatcher.ts", "linear-webhook.ts", "linear-notifications.ts",
    "linear-gate-specs.ts", "memory_indexer.py", "memory_tree.py",
}
# Directory prefixes whose children involve LLM logic (judge, agent specs)
AI_ENG_DIR_PREFIXES = ("evolution/", ".claude/agents/")

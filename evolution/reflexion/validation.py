"""Save-time validation for generated reflections (LIA-213).

`generate_reflection` returns the judge model's raw text. Without validation,
chat-template debris and the judge transcript/scoring preamble leaked into the
reflection body, got embedded, and were then prepended verbatim to live agent
prompts (the worst offenders -- raw chat-transcript dumps -- were injected 100-200
times each).

Design: HIGH PRECISION over recall. We reject only signals that a legitimate
lesson never emits -- raw chat-template control tokens, raw instruct markers, and
the judge's own score line -- plus a generous length backstop for runaway
generation. We deliberately do NOT reject on topical mentions of prompt-structure
terms (e.g. a real lesson that says "wrap inputs in <gate-spec> tags" or "the
agent output malformed `Tools: [, ]`"): a dry-run proved those caused false
positives on legit lessons (one was retrieved 90 times). The clear transcript
dumps all carry a raw chat token or the score-line, so precision loses very little
recall.

Reused at retrieval time (retriever.format_reflections_block) so the prompt path
is self-defending regardless of historical DB state.
"""
from __future__ import annotations

import os
import re
from typing import Optional

# Generous backstop for runaway generation that lacks a known marker. A valid
# lesson is "under 100 words" (~600 chars); even verbose analyses run <3k, while
# the corrupted dumps ran 4.5k-7k. 4000 catches runaway with ~zero false
# positives on real content. Env-overridable per the machine-adaptive rule (LIA-213).
MAX_REFLECTION_CHARS = int(os.environ.get("DEUS_REFLECTION_MAX_CHARS", "4000"))

# Raw control tokens / markers that a legitimate lesson never emits verbatim --
# their presence means the model echoed its own turn structure or the judge
# transcript into the reflection body. ``<|`` is banned as a bare fragment so a
# newline-split marker (``<|assistant\n...|>``) cannot slip past a regex.
_BANNED_SUBSTRINGS = (
    "<start_of_turn>",
    "</start_of_turn>",
    "<end_of_turn>",
    "</end_of_turn>",
    "<unused",  # gemma <unused0>/<unused1>… runaway tokens
    "<bos>",
    "<eos>",
    "<|",
    "[INST]",
    "[/INST]",
    "<<SYS>>",
)

# Structured leakage that needs more than a literal substring. Case-insensitive
# so a lowercased score line ("score: 0.9/1.0 | breakdown:") is still caught.
_BANNED_PATTERNS = (
    re.compile(r"Score:\s*[\d.]+/1\.0\s*\|\s*Breakdown:", re.IGNORECASE),
)


def is_valid_reflection(content: Optional[str]) -> tuple[bool, str]:
    """Return ``(ok, reason)``. ``reason`` is ``""`` when ok, else a short cause.

    Rejected only when the content is empty, runaway-long, or contains raw
    chat-template / instruct control tokens or the judge's score-line echoed back
    -- the LIA-213 corruption that was being prepended verbatim to live agent
    prompts. Topical mentions of prompt-structure terms are accepted.
    """
    if not content or not content.strip():
        return False, "empty"
    text = content.strip()
    if len(text) > MAX_REFLECTION_CHARS:
        return False, f"too_long({len(text)}>{MAX_REFLECTION_CHARS})"
    for s in _BANNED_SUBSTRINGS:
        if s in text:
            return False, f"banned_token:{s}"
    for pat in _BANNED_PATTERNS:
        if pat.search(text):
            return False, f"banned_pattern:{pat.pattern}"
    return True, ""

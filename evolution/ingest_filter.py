"""
Ingestion filter for non-gradeable interactions (LIA-109 follow-up).

Some "responses" that reach the eval pipeline are not agent work at all — they
are harness/proxy error stubs (an API connection dropped, an auth failure). The
judge cannot meaningfully score them: it reads the stub as the agent's output
and returns a floor score, polluting calibration. These are tagged
``eval_suite="infra_error"`` at ingestion so the judge gate skips them, while the
row still exists for observability (an infra event did happen).

Detection is by KNOWN harness-stub signature, never by length — legitimate terse
agent turns ("Waiting on CI.", "Complete. No further action.") are short but
perfectly gradeable, so length is not a proxy for "error".
"""
from __future__ import annotations

from typing import Optional

INFRA_ERROR_SUITE = "infra_error"

# Prefixes that only a harness/proxy emits — no legitimate agent response starts
# with the literal token. Matched against the stripped response.
_STUB_PREFIXES = ("API Error:",)

# Full stubs matched EXACTLY (not by prefix): the bare leading words are ordinary
# English a real response could open with, so only the complete signature counts.
_STUB_EXACT = ("Not logged in · Please run /login",)


def is_infra_error(response: Optional[str]) -> bool:
    """True if *response* is a harness/proxy error stub, not gradeable agent work.

    None / empty / whitespace-only → False: an empty response is a different
    failure class, already handled by the ingesters' minimum-length gate.
    """
    if not response:
        return False
    text = response.strip()
    if not text:
        return False
    if text in _STUB_EXACT:
        return True
    return text.startswith(_STUB_PREFIXES)

"""Centralized constants for the warden-review layer.

Single source of truth for the reusable strings/values that would otherwise be scattered
across the registry, backends, driver, and the gate in ``codex_warden_hooks.py`` — so a
reviewer (human or agent) can find and change them in one place. Zero-dependency by design
(only stdlib types) so even the hot-path hook module can import it cheaply.
"""
from __future__ import annotations

# ── Backend ids (used in config `backends: [...]`, verdict keys, the registry) ──────
BACKEND_CLAUDE = "claude"   # the in-session subagent transport (NOT in the registry)
BACKEND_GPT = "gpt"         # GPT via the codex CLI

#: Model backends the registry/gate recognize. Claude is intentionally excluded — it is
#: the in-session subagent transport, not an out-of-band model call. Grows by one per
#: added provider (e.g. a local / openai-compatible backend).
KNOWN_MODEL_BACKENDS = frozenset({BACKEND_GPT})

# ── Verdicts ────────────────────────────────────────────────────────────────────────
VERDICT_SHIP = "SHIP"
VERDICT_REVISE = "REVISE"
VERDICT_BLOCK = "BLOCK"
VERDICT_COULD_NOT_RUN = "COULD_NOT_RUN"   # infra failure → gate fails OPEN, never == SHIP

#: All verdicts a model backend may emit.
MODEL_VERDICTS = (VERDICT_SHIP, VERDICT_REVISE, VERDICT_BLOCK, VERDICT_COULD_NOT_RUN)
#: Blocking verdicts (a real review outcome that should stop a commit).
BLOCKING_VERDICTS = (VERDICT_REVISE, VERDICT_BLOCK)

# ── Co-gate loop guard ───────────────────────────────────────────────────────────────
#: Model-review rounds since last convergence before the gate escalates to human-in-the-loop.
CO_GATE_ESCALATION_ROUNDS = 3

#: Max chars of cross-reviewer context injected into a prompt / stored for the other
#: reviewer. Caps token cost and bounds the blast radius of any adversarial text that
#: survived sentinel stripping (the content is one-hop LLM output, treated cautiously).
CROSS_CONTEXT_MAX_CHARS = 4000
#: Max chars of a single verdict reason re-injected as cross-context.
CROSS_REASON_MAX_CHARS = 500

# ── Per-worktree state key/file formats (one place to keep the naming consistent) ────
def store_key(role: str, backend: str) -> str:
    """Warden-store key for a model backend's verdict, e.g. ``code-reviewer@gpt``."""
    return f"{role}@{backend}"


def loop_file(role: str) -> str:
    """Per-role co-gate loop-counter filename (under the worktree marker dir)."""
    return f".co-gate-loop-{role}.json"


def cross_review_file(role: str) -> str:
    """Per-role file holding model findings for the Claude subagent to read."""
    return f".{role}-cross-review.md"


#: Roles wired into the provider-agnostic co-gate today (Phase 2). Phase 3 extends this.
WIRED_ROLES = ("code-reviewer",)

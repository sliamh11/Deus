"""Z.ai GLM model-reviewer backend (id ``glm``).

A thin provider-specialization of :class:`OpenAICompatBackend`: it reviews via Z.ai's
OpenAI-compatible ``/v1/chat/completions`` endpoint, reusing the parent's hardened
``review()`` (per-run untrusted-diff sentinel boundary, fail-CLOSED verdict check, fail-OPEN
transport handling) verbatim — only the env-var names and provider defaults differ.

Config (a fresh clone sets these or the backend abstains — see ``REQUIRE_API_KEY``):
  WARDEN_GLM_API_KEY   (required)  a METERED Z.ai API key. NOT a GLM Coding Plan key — the
                                   Coding Plan is restricted to whitelisted coding tools and
                                   forbids SDK / third-party use (z.ai docs), so it must not
                                   drive this out-of-band warden backend.
  WARDEN_GLM_BASE_URL  (optional)  defaults to Z.ai's OpenAI-compatible base.
  WARDEN_GLM_MODEL     (optional)  defaults to ``glm-5.2``; ``ReviewRequest.model`` overrides.

Activation is fully opt-in and additive: this backend only runs when a role lists ``glm`` in
``.claude/wardens/config.json`` AND ``WARDEN_GLM_API_KEY`` is set; otherwise it abstains
(COULD_NOT_RUN → the gate fails open). It NEVER changes the behavior of any other backend.
"""
from __future__ import annotations

from ..constants import BACKEND_GLM
from .openai_compat import OpenAICompatBackend


class GLMBackend(OpenAICompatBackend):
    """Backend id ``glm``: Z.ai GLM via its OpenAI-compatible endpoint."""

    ENV_BASE_URL = "WARDEN_GLM_BASE_URL"
    ENV_MODEL = "WARDEN_GLM_MODEL"
    ENV_API_KEY = "WARDEN_GLM_API_KEY"
    DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"  # Z.ai's OpenAI-compatible base (verified live)
    # GLM-5.2 (a reasoning model) chosen for code-review quality: its chain-of-thought lifts
    # verdict accuracy on subtle diffs. The reasoning is returned in a separate ``reasoning_content``
    # field and intentionally ignored — the parent reads only ``message.content`` (the JSON verdict).
    # Override WARDEN_GLM_MODEL to a cheaper tier (e.g. a GLM Flash) if review quality allows.
    DEFAULT_MODEL = "glm-5.2"
    REQUIRE_API_KEY = True  # authenticated endpoint → abstain (no-op) when no key is set

    def id(self) -> str:
        return BACKEND_GLM

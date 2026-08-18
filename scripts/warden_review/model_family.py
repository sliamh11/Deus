"""Guard: refuse to record a Claude co-gate verdict that a non-Claude model produced.

LIA-560 defect 3. In a gateway session (``ANTHROPIC_BASE_URL`` pointing at a local
CLIProxyAPI), the three co-gated roles all pin ``model: sonnet``, which resolves through
``ANTHROPIC_DEFAULT_SONNET_MODEL``. If that variable names a GPT model, the "Claude half"
of the co-gate *is* a GPT model — and ``cogate.py`` still prints ``claude: SHIP``. Measured:
a ``haiku``-pinned subagent answered with no separate ``modelUsage`` entry at all; its usage
folded into the GPT orchestrator's. The co-gate reported a cross-family guarantee it was not
providing, silently.

Design principle, arrived at over several review rounds and worth preserving verbatim:
**there is exactly ONE hard-BLOCK condition — the resolved model is present in a
successfully parsed gateway listing AND its ``owned_by`` is a label that unambiguously
denotes a non-Anthropic provider.** Every other outcome is OK or a loud WARN that fails
open. Two earlier designs were rejected for violating that principle:

* Blocking on "``owned_by`` != anthropic" hard-blocks a *real* Claude model served through a
  relabelling proxy (an ``openai-compatibility`` section's owner is ``compat.Name``, an
  arbitrary operator-chosen string — ``service_models.go:726``).
* Blocking on "absent from the listing" hard-blocks a *real* Claude model that is merely
  mid-cooldown: ``modelRegistrationAvailability`` (``internal/registry/model_registry.go``)
  drops a registration from ``/v1/models`` entirely while any client is suspended for a
  non-quota reason (401/402/403/404), for 30 minutes to 12 hours. Absence is not evidence.

``owned_by`` is only partly gateway-verified: CLIProxyAPI hardcodes it for its first-party
sections (claude-api-key -> ``anthropic``, codex OAuth -> ``openai``, vertex/gemini ->
``google``, xai -> ``xai``), but for the generic ``openai-compatibility:`` passthrough it is
that arbitrary config label. Hence the known-owner allowlist rather than a not-equal test,
and hence the rename escape in the block message.

CLIProxyAPI citations pinned to commit ecc9aa72b32f34b680d03b0724b531a21ae74472.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

#: CLIProxyAPI cloaks any model id that does not already start with ``claude-`` as this
#: prefix plus the id reversed (``EnsureClaudeModelIDPrefix``, models.go:50). Used here ONLY
#: to normalise an id back to its real name for the listing lookup — never as evidence of a
#: model's family. The gateway's genuine Anthropic model is aliased ``opus-planner``, which
#: cloaks to ``claude-fable-5-dd-rennalp-supo``, so the prefix says nothing about provenance.
CLOAK_PREFIX = "claude-fable-5-dd-"

#: Owner labels that unambiguously denote a non-Anthropic model family. Matched
#: case-insensitively. Deliberately an allowlist: an unrecognised label (``litellm``,
#: ``corporate``, a team name) may well front a real Claude model, so it must not block.
KNOWN_NON_ANTHROPIC_OWNERS = frozenset({
    "openai", "google", "xai", "meta", "mistral", "deepseek",
    "moonshot", "qwen", "zhipu", "minimax", "cohere",
})

ANTHROPIC_OWNER = "anthropic"

#: Agent-frontmatter ``model:`` aliases and the env var each resolves through.
ALIAS_ENV = {
    "sonnet": "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "opus": "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "haiku": "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "fable": "ANTHROPIC_DEFAULT_FABLE_MODEL",
}

OK = "OK"
WARN = "WARN"
BLOCK = "BLOCK"

_TIMEOUT = 10.0
#: Any UA that does not start with ``claude-cli``. ``isAnthropicModelsRequest``
#: (server_routes.go:554-559) selects the cloaked Anthropic shape on an ``Anthropic-Version``
#: header OR a ``claude-cli`` User-Agent prefix, so omitting the header alone is NOT enough
#: to guarantee the plain-id OpenAI shape this guard reads.
_USER_AGENT = "deus-cogate-family-guard"

_VARIANT_SUFFIX_RE = re.compile(r"\[[^\[\]]*\]$")


def strip_variant_suffix(model_id: str) -> str:
    """Drop a trailing bracketed variant marker such as ``[1m]``.

    A Claude Code *client* convention (the CLI's 1M-context marker), not a CLIProxyAPI one —
    the gateway rejects ``opus-planner[1m]`` with "unknown provider" (LIA-560 defect 2).
    Normalisation only: this guard's inputs are operator-set config strings, which should
    never carry it. Cheap insurance for the listing lookup, not load-bearing.
    """
    return _VARIANT_SUFFIX_RE.sub("", model_id.strip())


def _split_thinking_suffix(model_id: str) -> tuple[str, str | None]:
    """Split a trailing ``(value)`` thinking-suffix, mirroring ``splitModelThinkingSuffix``."""
    if not model_id.endswith(")"):
        return model_id, None
    open_at = model_id.rfind("(")
    if open_at <= 0:
        return model_id, None
    return model_id[:open_at], model_id[open_at + 1:-1]


def decode_model_id(model_id: str) -> str:
    """Undo CLIProxyAPI's cloaking, mirroring ``ResolveClaudeModelIDPrefix`` (models.go:59).

    Order matters and is copied from the reference: split the ``(value)`` thinking-suffix
    FIRST, then strip the prefix and reverse the base, then re-attach the suffix. Reversing
    a string that still carries its suffix produces a garbled id that matches nothing.
    An id without the prefix is returned unchanged, suffix intact.
    """
    base, suffix = _split_thinking_suffix(model_id)
    if not base.startswith(CLOAK_PREFIX):
        return model_id
    encoded = base[len(CLOAK_PREFIX):]
    if not encoded:
        return model_id
    resolved = encoded[::-1]
    return f"{resolved}({suffix})" if suffix is not None else resolved


def _agent_model_pin(role: str, repo_root: Path) -> str | None:
    """Read the ``model:`` pin from a role's agent frontmatter, if present."""
    path = Path(repo_root) / ".claude" / "agents" / f"{role}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines()[:40]:
        if line.startswith("model:"):
            return line.split(":", 1)[1].strip() or None
    return None


def resolve_claude_half_model(role, repo_root, env) -> tuple[str | None, str | None, str]:
    """Resolve which model id the in-session Claude half will actually run on.

    Returns ``(pin, resolved_id, source)``. An alias pin resolves through its
    ``ANTHROPIC_DEFAULT_*`` env var; a non-alias pin is used verbatim; no pin at all falls
    back to ``ANTHROPIC_MODEL``.
    """
    pin = _agent_model_pin(role, Path(repo_root))
    if pin is None:
        return None, env.get("ANTHROPIC_MODEL") or None, "ANTHROPIC_MODEL (no agent pin)"
    alias = pin.lower()
    if alias in ALIAS_ENV:
        var = ALIAS_ENV[alias]
        return pin, env.get(var) or None, var
    return pin, pin, f"{role}.md model: pin"


def _http_get(url: str, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        headers={"x-api-key": api_key, "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - operator-set URL
        return json.loads(resp.read().decode("utf-8"))


def gateway_owner(base_url, api_key, model_id, timeout=_TIMEOUT, fetch=_http_get):
    """Return ``(found, owner)`` for ``model_id`` from the gateway's OpenAI-shaped listing.

    The OpenAI shape is requested deliberately: its ids are PLAIN, so the lookup does not
    depend on the cloaking transform. (Both shapes carry ``owned_by``; neither carries a
    usable provenance discriminator, so ``owned_by`` is all there is.) Raises on any
    transport or parse failure — callers treat that as "learned nothing", never as evidence.

    ``fetch`` is injected by tests; this is the only function here that touches the network.
    """
    url = base_url.rstrip("/") + "/v1/models?limit=1000"
    body = fetch(url, api_key, timeout)
    entries = body.get("data") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        raise ValueError("gateway /v1/models did not return a data list")
    wanted = decode_model_id(strip_variant_suffix(model_id))
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") == wanted:
            owner = entry.get("owned_by")
            return True, (owner if isinstance(owner, str) else None)
    return False, None


def _block_message(role, pin, raw_id, decoded_id, owner, base_url, source) -> str:
    shown = raw_id if raw_id == decoded_id else f"{raw_id}\n               -> {decoded_id}"
    return (
        "BLOCK: co-gate cannot record a Claude verdict.\n\n"
        f"  role:        {role}\n"
        f"  agent pin:   {pin or '(none)'}\n"
        f"  resolves to: {shown} (owned_by: {owner})\n"
        f"  via:         {source}\n"
        f"  gateway:     {base_url}\n\n"
        "The Claude half would run on a non-Anthropic model, so 'claude: SHIP'\n"
        "would be false — the co-gate would report cross-family review it is not\n"
        "providing.\n\n"
        "Fix one of:\n"
        f"  - point {source} at a Claude model\n"
        "  - run the co-gate from a non-gateway session\n"
        "  - if this is an openai-compatibility passthrough whose section name\n"
        "    collides with a provider label, rename that section"
    )


def check_claude_half(role, repo_root, env=None, timeout=_TIMEOUT, fetch=_http_get):
    """Decide whether the Claude co-gate half may be recorded. Returns ``(verdict, message)``.

    ``BLOCK`` only on positive evidence (listed model, known non-Anthropic owner). Everything
    else is ``OK`` or ``WARN``; a WARN never stops the co-gate.
    """
    env = os.environ if env is None else env
    base_url = (env.get("ANTHROPIC_BASE_URL") or "").strip()
    if not base_url:
        return OK, ""

    pin, raw_id, source = resolve_claude_half_model(role, repo_root, env)
    if not raw_id:
        return WARN, (f"[cogate] {source} is unset, so the Claude half's model could not be "
                      f"resolved against {base_url}; cross-family review is UNVERIFIED.")

    api_key = env.get("ANTHROPIC_API_KEY") or ""
    decoded = decode_model_id(strip_variant_suffix(raw_id))
    try:
        found, owner = gateway_owner(base_url, api_key, raw_id, timeout, fetch)
    except Exception as exc:  # noqa: BLE001 - any failure means we learned nothing
        return WARN, (f"[cogate] could not read {base_url}/v1/models ({exc}); the Claude "
                      f"half's model ({decoded}) is UNVERIFIED. Proceeding.")

    if not found:
        return WARN, (f"[cogate] {decoded} is not in {base_url}'s model listing — it may be "
                      "mid-cooldown rather than absent, so this is not treated as proof. "
                      "Cross-family review is UNVERIFIED. Proceeding.")

    if owner and owner.strip().lower() == ANTHROPIC_OWNER:
        return OK, ""

    if owner and owner.strip().lower() in KNOWN_NON_ANTHROPIC_OWNERS:
        return BLOCK, _block_message(role, pin, raw_id, decoded, owner, base_url, source)

    return WARN, (f"[cogate] {decoded} is served by {base_url} under owner "
                  f"{owner!r}, which does not identify a model family — the Claude half is "
                  "UNVERIFIED. Proceeding.")

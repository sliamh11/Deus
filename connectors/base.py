"""
Connector ABC for Deus's `deus connect` multi-model CLI feature.

A Connector routes a `deus connect <id>` session to a non-Claude model via
an OAuth-subscription-multiplexing engine (e.g. CLIProxyAPI), while keeping
the session structurally Deus — identity, vault/memory context, preferences,
and portable skills intact, not a bare Claude Code process that merely
happens to be redirected. See docs/decisions/backend-neutral-agent-runtime.md
for how this differs from Deus's container-agent backend adapters.

Mirrors evolution/judge/provider.py's ABC+Registry shape.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ConnectorSetupHandler(ABC):
    """Engine-specific onboarding logic for one Connector.

    The "1 file + 1 registry line" extensibility claim holds for the
    Connector ABC's runtime behavior (env vars, model aliases, inline agent
    definitions) but not for onboarding — a differently-shaped engine needs
    its own install/login/config-write logic. The generic `add-connector`
    skill orchestrates whichever handler the selected connector supplies.
    """

    @abstractmethod
    def install(self) -> bool:
        """Confirm the engine binary is present (or guide installing it).

        Returns True once the engine is ready to configure. Never silently
        auto-fetches a binary that will go on to hold real OAuth tokens —
        require explicit user confirmation before any install step.
        """
        ...

    @abstractmethod
    def authenticate(self) -> bool:
        """Run the engine's own OAuth login interactively. Returns True on success."""
        ...

    @abstractmethod
    def write_config(self, values: dict[str, Any]) -> None:
        """Write the real, local-only config for this connector.

        Must never write real secrets into a path a container agent could
        read — see connectors/providers/cliproxy_oauth for the concrete
        container-credential-boundary handling this guards against.
        """
        ...

    @abstractmethod
    def verify(self) -> bool:
        """Engine health plus a real functional probe — not just a bare
        `/health` hit. Returns True only if the connector is genuinely
        usable end to end.
        """
        ...


class Connector(ABC):
    """A single registered `deus connect` destination."""

    @property
    @abstractmethod
    def id(self) -> str:
        """Stable connector id, e.g. 'cliproxy-oauth'.

        Never one of the reserved words `list`/`setup`/`status`/`default`/
        `show`/`off`/`clear` — the registry rejects registration of a
        connector using any of those (see `registry.RESERVED_IDS`).
        """
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """One-line human-readable summary, shown by `deus connect list`."""
        ...

    @property
    @abstractmethod
    def engine(self) -> str:
        """Underlying engine name, e.g. 'cliproxy'."""
        ...

    @property
    @abstractmethod
    def risk_level(self) -> str:
        """Short risk label surfaced during onboarding, e.g. 'oauth-reuse'."""
        ...

    @property
    @abstractmethod
    def setup_handler(self) -> ConnectorSetupHandler:
        """The engine-specific onboarding handler for this connector."""
        ...

    @abstractmethod
    def model_aliases(self) -> dict[str, str]:
        """Fixed stable subagent name -> the routing alias configured for
        it in this connector's local config (read fresh on every call, not
        cached — the local config is user-editable at any time)."""
        ...

    @abstractmethod
    def is_configured(self) -> bool:
        """True once real local config + at least one model alias exist."""
        ...

    @abstractmethod
    def env_for_launch(self) -> dict[str, str]:
        """Env vars for the launching shell only. Never written to
        `~/.claude/settings.json`'s global `env` block — that would leak
        this redirection into every Claude Code session on the machine.

        One reserved key: `DEUS_CONNECT_SETTINGS_JSON`. If present,
        `deus-cmd.sh`'s `launch_connect()` forwards its value as
        `claude --settings <value>` for this one launch only, then unsets
        it so it can't leak into a nested `deus connect <other-id>` call.
        Use this for session-scoped Claude Code settings overrides (e.g.
        forcing `autoCompactEnabled` on for a connector whose routed model
        has a real, smaller context window) without touching the user's
        global `~/.claude/settings.json`. Optional — a connector that omits
        it is unaffected (no `--settings` flag gets appended at all).
        """
        ...

    @abstractmethod
    def agents_for_launch(self) -> dict[str, Any]:
        """Inline subagent definitions for `claude --agents <json>`.

        Must always be valid JSON-serializable data on success — an empty
        dict for a connector with no subagents, matching env_for_launch()'s
        convention of never failing silently.
        """
        ...

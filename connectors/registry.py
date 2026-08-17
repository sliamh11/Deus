"""
ConnectorRegistry — central registry of `deus connect` Connector instances.

Mirrors evolution/judge/provider.py:18-51,54-139's JudgeRegistry shape.

Usage:
    registry = ConnectorRegistry.default()
    connector = registry.resolve("cliproxy-oauth")
"""
from __future__ import annotations

from typing import Optional

from .base import Connector

# `deus connect list`/`setup <id>`/`status <id>`/`default <id>`, plus
# `default`'s own sub-namespace (`show`/`off`/`clear`), are reserved command
# words — a connector id can never take one of these (see deus-cmd.sh's
# `connect` prefix-dispatch branch and the `default)` arm's deferred
# continuation). Without this, e.g. a connector literally named "default"
# would be permanently unreachable via `deus connect default` -- that case
# arm always intercepts it first, before the launch catch-all ever runs.
RESERVED_IDS = frozenset({"list", "setup", "status", "default", "show", "off", "clear"})


class ConnectorRegistrationError(ValueError):
    """Raised when registering a connector with a reserved or duplicate id."""


class UnknownConnectorError(LookupError):
    """Raised when resolving an id that isn't registered.

    Deliberately NOT a KeyError subclass: KeyError.__str__ is overridden to
    return repr(args[0]) instead of str(args[0]), which wraps every
    user-facing message from this exception in stray quotes (e.g.
    "Unknown connector 'bogus'. Registered: [...]" prints as a quoted repr,
    not plain text). LookupError -- KeyError's own parent -- has no such
    override.
    """


class ConnectorRegistry:
    """Singleton registry of registered connectors."""

    _instance: Optional["ConnectorRegistry"] = None

    def __init__(self):
        self._connectors: dict[str, Connector] = {}

    @classmethod
    def default(cls) -> "ConnectorRegistry":
        """Return the singleton registry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — for testing only."""
        cls._instance = None

    def register(self, connector: Connector) -> None:
        """Register a connector. Rejects reserved ids; last-write-wins otherwise."""
        if connector.id in RESERVED_IDS:
            raise ConnectorRegistrationError(
                f"Connector id '{connector.id}' is reserved "
                f"(reserved: {sorted(RESERVED_IDS)})"
            )
        self._connectors[connector.id] = connector

    def resolve(self, connector_id: str) -> Connector:
        """Resolve a connector by exact id. Raises UnknownConnectorError if not found."""
        try:
            return self._connectors[connector_id]
        except KeyError:
            raise UnknownConnectorError(
                f"Unknown connector '{connector_id}'. Registered: {self.list_ids()}"
            ) from None

    def list_ids(self) -> list[str]:
        """Return registered connector ids, sorted."""
        return sorted(self._connectors)

    def list_connectors(self) -> list[Connector]:
        """Return registered connectors, sorted by id."""
        return [self._connectors[cid] for cid in self.list_ids()]

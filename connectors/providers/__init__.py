"""Built-in connectors. Importing this package registers them all."""
from ..registry import ConnectorRegistry

from .cliproxy_oauth import CliproxyOauthConnector

_registry = ConnectorRegistry.default()
_registry.register(CliproxyOauthConnector())

__all__ = ["CliproxyOauthConnector"]

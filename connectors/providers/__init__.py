"""Built-in connectors. Importing this package registers them all."""
from ..registry import ConnectorRegistry

from .cliproxy_oauth import CliproxyOauthConnector
from .ollama import OllamaConnector

_registry = ConnectorRegistry.default()
_registry.register(CliproxyOauthConnector())
_registry.register(OllamaConnector())

__all__ = ["CliproxyOauthConnector", "OllamaConnector"]

"""Built-in storage providers. Importing this package registers them all."""
from ..provider import StorageRegistry

from .sqlite import SQLiteStorageProvider

_registry = StorageRegistry.default()
_registry.register(SQLiteStorageProvider())

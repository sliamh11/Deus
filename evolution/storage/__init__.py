"""
Storage provider -- abstracts SQLite and future database backends.

Usage:
    from evolution.storage import get_storage
    store = get_storage()
    iid = store.log_interaction(...)

    # Explicit provider:
    store = get_storage("sqlite")
"""
from typing import Optional

from .provider import StorageProvider, StorageRegistry, NoStorageProviderError

# Auto-register built-in providers on import
from . import providers as _providers  # noqa: F401


def get_storage(provider: Optional[str] = None) -> StorageProvider:
    """
    Resolve the best available storage provider.

    Args:
        provider: Optional provider name override.

    Returns:
        A StorageProvider instance.
    """
    return StorageRegistry.default().resolve(provider)


__all__ = [
    "get_storage",
    "StorageProvider",
    "StorageRegistry",
    "NoStorageProviderError",
]

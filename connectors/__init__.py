"""Deus connect: registry of non-Claude model connectors for `deus connect`.

Importing this package registers all built-in connectors (see providers/).
"""
from . import registry
from . import providers  # noqa: F401 -- import side effect: registers built-ins

__all__ = ["registry"]

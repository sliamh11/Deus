"""Canonical vault-path resolver for the Evolution loop.

Single source of truth for locating the user's Deus vault on the host. Public-
repo-safe: NO hardcoded personal path — resolves only from the ``DEUS_VAULT_PATH``
env var or ``~/.config/deus/config.json``, and raises if neither is configured.

Both ``evolution/taste_profile.py`` and ``evolution/persona.py`` import
``load_vault_path()`` from here (the logic previously lived only in
``taste_profile`` as a private ``_load_vault_path``).
"""
import json
import os
import tempfile
from pathlib import Path


def load_vault_path() -> Path:
    """Resolve the Deus vault path from env or config. Anchored to home or /tmp.

    Resolution order: ``DEUS_VAULT_PATH`` env → ``~/.config/deus/config.json``
    (``vault_path`` field). Raises ``RuntimeError`` if neither is set and
    ``ValueError`` if the resolved path escapes the allowed prefixes (home, /tmp,
    the system tempdir) — a containment guard so a malformed config can't point
    the loop at an arbitrary filesystem location.
    """
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        resolved = Path(env_path).expanduser().resolve()
    elif (Path.home() / ".config" / "deus" / "config.json").exists():
        config_path = Path.home() / ".config" / "deus" / "config.json"
        try:
            with config_path.open() as f:
                config = json.load(f)
            resolved = Path(config["vault_path"]).expanduser().resolve()
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError(
                f"Failed to read vault_path from config.json: {exc}"
            ) from exc
    else:
        raise RuntimeError(
            "Cannot resolve vault path. Set DEUS_VAULT_PATH or configure "
            "vault_path in ~/.config/deus/config.json"
        )

    allowed = (Path.home(), Path("/tmp").resolve(), Path(tempfile.gettempdir()).resolve())
    if not any(resolved == a or resolved.is_relative_to(a) for a in allowed):
        raise ValueError(
            f"Vault path {resolved} is outside allowed prefixes "
            f"(home {Path.home()} or system temp {tempfile.gettempdir()})"
        )
    return resolved

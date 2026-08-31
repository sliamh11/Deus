#!/usr/bin/env python3
"""Seed dsh's pi-ai credential records from existing Claude and Codex grants.

dsh's ``llm-pi-ai`` adapter already knows how to use and refresh these grant
shapes. This helper only bridges the two local credential-file formats into
dsh's credential store; it does not send a request or alter either source.

The destination is merged, backed up, written atomically and forced to mode
0600. Secret values are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on an unprepared host
    yaml = None


class SeedError(RuntimeError):
    """A user-fixable source or destination credential problem."""


PROVIDERS = ("anthropic", "openai-codex")
RECORD_SCOPE = "llm-pi-ai"
CODEX_AUTH_CLAIM = "https://api.openai.com/auth"


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SeedError(f"{label} must be a JSON object")
    return value


def _required_string(mapping: dict[str, Any], key: str, label: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise SeedError(f"{label} has no non-empty {key!r} string")
    return value


def _required_int(mapping: dict[str, Any], key: str, label: str) -> int:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SeedError(f"{label} has no numeric {key!r}")
    return int(value)


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        return _required_mapping(json.loads(path.read_text(encoding="utf-8")), label)
    except FileNotFoundError as exc:
        raise SeedError(f"no {label} at {path}") from exc
    except json.JSONDecodeError as exc:
        raise SeedError(f"cannot parse {label} at {path}: {exc}") from exc


def anthropic_grant(path: Path) -> dict[str, Any]:
    """Translate Claude Code's grant into pi-ai's Anthropic grant shape."""
    source = _read_json(path, "Claude Code credentials")
    oauth = _required_mapping(source.get("claudeAiOauth"), "Claude Code claudeAiOauth")
    return {
        "type": "oauth",
        "access": _required_string(oauth, "accessToken", "Claude Code claudeAiOauth"),
        "refresh": _required_string(oauth, "refreshToken", "Claude Code claudeAiOauth"),
        "expires": _required_int(oauth, "expiresAt", "Claude Code claudeAiOauth"),
    }


def _decode_jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise SeedError("Codex access token is not a three-part JWT")
    encoded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded.encode("ascii"))
        return _required_mapping(json.loads(decoded), "Codex access-token claims")
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SeedError("cannot decode Codex access-token claims") from exc


def codex_grant(path: Path) -> dict[str, Any]:
    """Translate Codex's grant into pi-ai's openai-codex grant shape."""
    source = _read_json(path, "Codex credentials")
    tokens = _required_mapping(source.get("tokens"), "Codex tokens")
    access = _required_string(tokens, "access_token", "Codex tokens")
    claims = _decode_jwt_claims(access)
    auth = _required_mapping(claims.get(CODEX_AUTH_CLAIM), "Codex auth claims")
    return {
        "type": "oauth",
        "access": access,
        "refresh": _required_string(tokens, "refresh_token", "Codex tokens"),
        # Codex stores last_refresh rather than an expiry. The signed token's
        # exp claim is the expiry pi-ai would have stored after its own login.
        "expires": _required_int(claims, "exp", "Codex access-token claims") * 1000,
        "accountId": _required_string(auth, "chatgpt_account_id", "Codex auth claims"),
    }


def load_store(path: Path) -> dict[str, Any]:
    """Load and validate the credential document before changing anything."""
    if yaml is None:
        raise SeedError("PyYAML is required; install it with `python3 -m pip install pyyaml`")
    if not path.exists():
        return {"version": 1, "records": {}}
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SeedError(f"cannot parse dsh credential store at {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise SeedError(f"dsh credential store at {path} is not a mapping")
    unknown = set(document) - {"version", "refs", "records"}
    if unknown:
        raise SeedError(f"dsh credential store has unknown top-level keys: {', '.join(sorted(unknown))}")
    if document.get("version") != 1:
        raise SeedError("dsh credential store must have version: 1")
    for section in ("refs", "records"):
        if section in document and not isinstance(document[section], dict):
            raise SeedError(f"dsh credential store {section!r} section must be a mapping")
    document.setdefault("records", {})
    return document


def render_store(document: dict[str, Any]) -> str:
    if yaml is None:  # keeps static type checkers honest after load_store
        raise SeedError("PyYAML is required")
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _backup_path(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.name}.bak-{stamp}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak-{stamp}-{suffix}")
        suffix += 1
    return candidate


def write_store(path: Path, document: dict[str, Any]) -> Path | None:
    """Back up an existing store and atomically replace it with mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    backup: Path | None = None
    if path.exists():
        backup = _backup_path(path)
        shutil.copy2(path, backup)
        backup.chmod(0o600)

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(render_store(document))
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
    return backup


def _selected_providers(values: list[str] | None) -> tuple[str, ...]:
    if not values or "all" in values:
        return PROVIDERS
    return tuple(dict.fromkeys(values))


def build_parser() -> argparse.ArgumentParser:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        action="append",
        choices=(*PROVIDERS, "all"),
        help="grant to seed; repeat for more than one (default: all)",
    )
    parser.add_argument("--store", type=Path, default=home / ".dsh" / ".credentials.yaml")
    parser.add_argument(
        "--claude-credentials", type=Path, default=home / ".claude" / ".credentials.json"
    )
    parser.add_argument("--codex-credentials", type=Path, default=home / ".codex" / "auth.json")
    parser.add_argument("--dry-run", action="store_true", help="validate and report without writing")
    return parser


def run(args: argparse.Namespace) -> int:
    providers = _selected_providers(args.provider)
    grants: dict[str, dict[str, Any]] = {}
    for provider in providers:
        if provider == "anthropic":
            grants[provider] = anthropic_grant(args.claude_credentials)
        else:
            grants[provider] = codex_grant(args.codex_credentials)

    document = load_store(args.store)
    records = document["records"]
    for provider, grant in grants.items():
        records[f"{RECORD_SCOPE}/{provider}"] = {"kind": "grant", "payload": grant}

    keys = ", ".join(f"{RECORD_SCOPE}/{provider}" for provider in providers)
    if args.dry_run:
        print(f"validated {len(providers)} grant(s); would seed: {keys}")
        return 0

    backup = write_store(args.store, document)
    if backup is not None:
        print(f"backed up existing store to {backup.name}")
    print(f"seeded {len(providers)} grant(s): {keys}")
    print(f"wrote {args.store} with mode 0600; no secret values were printed")
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Locked read-write primitives for ~/.claude/settings.json.

Two public functions share the same flock + atomic-temp-replace machinery:

* merge_settings(path, changes) — deep-merges changes into the current JSON.
  Hooks and permission arrays are appended with dedup, never replaced.
  Use for additive concurrent writes (e.g. hook installers).

* rewrite_settings(path, transform_fn) — calls transform_fn(current) and
  writes the result verbatim (no merging).  Arrays are replaced, not appended.
  Use when you need to rewrite existing values in place (e.g. path renaming).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

_DEFAULT_PATH = Path("~/.claude/settings.json").expanduser()
# flock is per-process; this lock serializes threads within the same process.
_THREAD_LOCK = threading.Lock()


def _canonical(item: Any) -> str:
    if isinstance(item, dict):
        return json.dumps(item, sort_keys=True)
    return json.dumps(item)


def _array_merge(base: list, override: list) -> list:
    seen = {_canonical(x) for x in base}
    merged = list(base)
    for item in override:
        key = _canonical(item)
        if key not in seen:
            seen.add(key)
            merged.append(item)
    return merged


def _is_array_merge_key(path: tuple[str, ...]) -> bool:
    if len(path) >= 2 and path[0] == "hooks":
        return True
    if path == ("permissions", "allow") or path == ("permissions", "deny"):
        return True
    return False


def _deep_merge(
    base: dict[str, Any],
    override: dict[str, Any],
    path: tuple[str, ...] = (),
) -> dict[str, Any]:
    result = dict(base)
    for key, val in override.items():
        current_path = path + (key,)
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val, current_path)
        elif (
            key in result
            and isinstance(result[key], list)
            and isinstance(val, list)
            and _is_array_merge_key(current_path)
        ):
            result[key] = _array_merge(result[key], val)
        else:
            result[key] = val
    return result


def _locked_read_write(
    path: Path,
    compute: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Acquire flock, read current JSON, apply compute(), atomic write.

    Internal shared primitive used by merge_settings and rewrite_settings.
    compute receives the current dict (empty if file absent/empty) and must
    return the dict to be written back.
    """
    if sys.platform == "win32":
        raise NotImplementedError("settings_merge requires fcntl (macOS/Linux)")

    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    with _THREAD_LOCK:
        fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = b""
            while True:
                chunk = os.read(fd, 8192)
                if not chunk:
                    break
                raw += chunk

            current: dict[str, Any] = {}
            if raw.strip():
                current = json.loads(raw)

            result = compute(current)

            text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=path.parent, delete=False,
                    prefix="settings_merge_",
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    tmp.write(text)
                os.replace(tmp_path, path)
            except Exception:
                if tmp_path is not None:
                    tmp_path.unlink(missing_ok=True)
                raise
        finally:
            os.close(fd)

    return result


def merge_settings(path: Path, changes: dict[str, Any]) -> dict[str, Any]:
    """Acquire flock, read current JSON, deep-merge changes, atomic write.

    Hooks and permission arrays are appended with dedup, never replaced.
    Use for additive concurrent writes (e.g. hook installers).
    """
    return _locked_read_write(path, lambda current: _deep_merge(current, changes))


def rewrite_settings(
    path: Path,
    transform_fn: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Acquire flock, read current JSON, apply transform_fn(), atomic write.

    Unlike merge_settings, the result of transform_fn is written verbatim —
    arrays are replaced, not appended.  Use when you need to rewrite existing
    values in place (e.g. substituting old paths with new paths after a rename).

    transform_fn receives the current dict and must return the new dict to
    write.  If it raises, the file is left untouched.
    """
    return _locked_read_write(path, transform_fn)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read-lock-write helpers for settings.json")
    parser.add_argument("--path", type=Path, default=_DEFAULT_PATH,
                        help="Path to settings.json (default: ~/.claude/settings.json)")
    sub = parser.add_subparsers(dest="cmd")

    # python3 settings_merge.py [--path <p>] merge   — reads JSON changes from stdin
    sub.add_parser("merge", help="Deep-merge JSON from stdin into settings.json")

    # python3 settings_merge.py [--path <p>] rewrite — substitutes strings via env vars
    #
    # For shell callers: pass substitution pairs via environment variables:
    #   SETTINGS_SUBST_0_OLD=<old>  SETTINGS_SUBST_0_NEW=<new>
    #   SETTINGS_SUBST_1_OLD=<old>  SETTINGS_SUBST_1_NEW=<new>  ...
    #
    # Substitution is applied to the JSON text, so every string value in the
    # document (including values inside arrays and nested objects) is updated.
    # Variables are passed through the environment rather than interpolated into
    # Python source so that paths containing special characters are handled safely.
    sub.add_parser(
        "rewrite",
        help="Rewrite settings.json by substituting string values. "
             "Reads substitution pairs from env vars "
             "SETTINGS_SUBST_<N>_OLD / SETTINGS_SUBST_<N>_NEW (N=0,1,...).",
    )

    sp = sub.add_parser(
        "sandbox-allow",
        help="Add a path to sandbox.filesystem.allowWrite (dedup, flock)",
    )
    sp.add_argument("path_to_add", help="Path to add (e.g. ~/Desktop/vault)")

    args = parser.parse_args()

    if args.cmd == "merge" or args.cmd is None:
        # Legacy behaviour: read JSON changes from stdin, deep-merge.
        changes = json.load(sys.stdin)
        result = merge_settings(args.path, changes)
        print(json.dumps(result, indent=2))

    elif args.cmd == "rewrite":
        # Collect substitution pairs from env: SETTINGS_SUBST_0_OLD / SETTINGS_SUBST_0_NEW …
        pairs: list[tuple[str, str]] = []
        i = 0
        while True:
            old = os.environ.get(f"SETTINGS_SUBST_{i}_OLD")
            new = os.environ.get(f"SETTINGS_SUBST_{i}_NEW")
            if old is None and new is None:
                break
            if old is None or new is None:
                sys.exit(
                    f"Error: SETTINGS_SUBST_{i}_OLD and SETTINGS_SUBST_{i}_NEW "
                    "must both be set."
                )
            pairs.append((old, new))
            i += 1

        if not pairs:
            sys.exit(
                "Error: no substitution pairs found. "
                "Set SETTINGS_SUBST_0_OLD / SETTINGS_SUBST_0_NEW (and _1_, _2_, …)."
            )

        def _subst_transform(data: dict[str, Any]) -> dict[str, Any]:
            """Apply all substitution pairs to the JSON-encoded document."""
            text = json.dumps(data, ensure_ascii=False)
            for old, new in pairs:
                text = text.replace(old, new)
            return json.loads(text)  # type: ignore[return-value]

        result = rewrite_settings(args.path, _subst_transform)
        print(json.dumps(result, indent=2))

    elif args.cmd == "sandbox-allow":
        new_path = args.path_to_add

        def _add_sandbox_path(settings: dict[str, Any]) -> dict[str, Any]:
            sandbox = settings.setdefault("sandbox", {})
            fs_cfg = sandbox.setdefault("filesystem", {})
            existing = fs_cfg.get("allowWrite", [])
            if new_path not in existing:
                fs_cfg["allowWrite"] = existing + [new_path]
            return settings

        rewrite_settings(args.path, _add_sandbox_path)

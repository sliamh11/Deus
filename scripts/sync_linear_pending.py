#!/usr/bin/env python3
"""Output the Linear pending block for /compress, with auto-invalidating cache.

Read-through cache with filesystem-mtime invalidation:
  1. Cache missing → re-fetch
  2. Cache older than 60 min → re-fetch (safety net)
  3. DB file mtime newer than cache → re-fetch (something changed)

Stdout: formatted pending block (ready for paste into CLAUDE.md).
Stderr: diagnostics (cache hit/miss, fetch timing).

Exit codes follow _exit_codes.py (SUCCESS=0, AUTH_ERROR=4,
INTERNAL_ERROR=5, RATE_LIMIT=7).
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _exit_codes import AUTH_ERROR, INTERNAL_ERROR, RATE_LIMIT, SUCCESS

LINEAR_API_URL = "https://api.linear.app/graphql"
CACHE_MAX_AGE_S = 3600

STATE_PRIORITY = {
    "In Progress": 0,
    "In Review": 1,
    "Agent Working": 2,
    "Ready for Agent": 3,
    "Todo": 4,
    "Backlog": 5,
}

EXCLUDED_STATES = {"Done", "Canceled", "Duplicate"}

QUERY = """
query($teamId: ID!) {
  issues(
    filter: {
      team: { id: { eq: $teamId } }
      state: { type: { nin: ["completed", "canceled"] } }
    }
    first: 50
  ) {
    nodes {
      title
      identifier
      state { name type }
    }
  }
}
"""


def _deus_home() -> Path:
    return Path(os.environ.get("DEUS_HOME", Path.home() / "deus"))


def _cache_path() -> Path:
    d = Path.home() / ".deus"
    d.mkdir(exist_ok=True)
    return d / "linear-pending-cache.md"


def _db_path() -> Path:
    return _deus_home() / "store" / "messages.db"


def _read_env_file() -> dict[str, str]:
    candidates = []
    deus_home = os.environ.get("DEUS_HOME")
    if deus_home:
        candidates.append(Path(deus_home) / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    env_vars: dict[str, str] = {}
    for candidate in candidates:
        try:
            for line in candidate.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, val = line.partition("=")
                    env_vars[key.strip()] = val.strip().strip("\"'")
        except OSError:
            continue
    return env_vars


def _get_api_token() -> str | None:
    token = os.environ.get("LINEAR_API_TOKEN") or os.environ.get("LINEAR_API_KEY")
    if token:
        return token
    env_file = _read_env_file()
    return env_file.get("LINEAR_API_TOKEN") or env_file.get("LINEAR_API_KEY")


def _get_team_id() -> str | None:
    tid = os.environ.get("LINEAR_TEAM_ID")
    if tid:
        return tid
    env_file = _read_env_file()
    return env_file.get("LINEAR_TEAM_ID")


def _graphql(token: str, query: str, variables: dict | None = None) -> dict:
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = Request(
        LINEAR_API_URL,
        data=payload,
        headers={
            "Authorization": token,
            "Content-Type": "application/json",
        },
    )
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def _discover_team_id(token: str) -> str | None:
    result = _graphql(token, "{ teams { nodes { id name } } }")
    nodes = result.get("data", {}).get("teams", {}).get("nodes", [])
    if not nodes:
        return None
    return nodes[0]["id"]


def _cache_is_fresh(cache: Path, db: Path) -> bool:
    if not cache.exists():
        return False
    cache_mtime = cache.stat().st_mtime
    age = time.time() - cache_mtime
    if age > CACHE_MAX_AGE_S:
        return False
    if db.exists() and db.stat().st_mtime > cache_mtime:
        return False
    return True


def _format_pending(issues: list[dict]) -> str:
    issues.sort(key=lambda i: (
        STATE_PRIORITY.get(i.get("state", {}).get("name", ""), 99),
        int(re.sub(r"\D", "", i.get("identifier", "0")) or "0"),
    ))
    lines = ["  # Source of truth: Linear. Synced by SessionStart hook (/compress as fallback)."]
    for issue in issues:
        title = issue.get("title", "")
        if len(title) > 80:
            title = title[:77] + "..."
        identifier = issue.get("identifier", "")
        lines.append(f"  - [ ] {title} ({identifier})")
    return "\n".join(lines)


def main() -> int:
    cache = _cache_path()
    db = _db_path()

    if _cache_is_fresh(cache, db):
        print(cache.read_text(), end="")
        print("cache fresh", file=sys.stderr)
        return SUCCESS

    token = _get_api_token()
    if not token:
        print("LINEAR_API_TOKEN not found", file=sys.stderr)
        return AUTH_ERROR

    team_id = _get_team_id()
    if not team_id:
        team_id = _discover_team_id(token)
    if not team_id:
        print("could not determine team ID", file=sys.stderr)
        return INTERNAL_ERROR

    t0 = time.time()
    try:
        result = _graphql(token, QUERY, {"teamId": team_id})
    except HTTPError as e:
        if e.code == 401:
            print(f"auth error: {e}", file=sys.stderr)
            return AUTH_ERROR
        if e.code == 429:
            print(f"rate limited: {e}", file=sys.stderr)
            return RATE_LIMIT
        print(f"HTTP error: {e}", file=sys.stderr)
        return INTERNAL_ERROR
    except (URLError, OSError) as e:
        print(f"network error: {e}", file=sys.stderr)
        return INTERNAL_ERROR

    nodes = result.get("data", {}).get("issues", {}).get("nodes", [])
    issues = [
        n for n in nodes
        if n.get("state", {}).get("name") not in EXCLUDED_STATES
    ]

    output = _format_pending(issues)
    elapsed = time.time() - t0
    print(f"fetched {len(issues)} issues in {elapsed:.1f}s", file=sys.stderr)

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(cache.parent), prefix=".cache.")
    closed = False
    try:
        os.write(tmp_fd, output.encode("utf-8"))
        os.close(tmp_fd)
        closed = True
        os.replace(tmp_path, str(cache))
    except Exception:
        if not closed:
            os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    print(output, end="")
    return SUCCESS


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""SessionStart hook: sync vault CLAUDE.md pending block from Linear.

Queries the Linear GraphQL API for active issues and rebuilds the pending:
block in CLAUDE.md. Uses the same sentinel/block-replace pattern as the
TypeScript webhook-driven sync in src/linear-vault-sync.ts.

Config resolution: ~/.config/deus/config.json (vault_path, LINEAR_TEAM_ID)
Token resolution: DEUS_HOME/.env -> LINEAR_API_TOKEN env var
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

LINEAR_API_URL = "https://linear.app/api/graphql"

STATE_PRIORITY = {
    "In Progress": 0,
    "In Review": 1,
    "Agent Working": 2,
    "Ready for Agent": 3,
    "Todo": 4,
    "Backlog": 5,
}

EXCLUDED_STATES = {"Done", "Canceled", "Duplicate"}


def _load_config() -> dict:
    cfg_path = Path.home() / ".config" / "deus" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _vault_path(config: dict) -> Path | None:
    env = os.environ.get("DEUS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    vp = config.get("vault_path", "")
    if vp:
        return Path(vp).expanduser()
    return None


def _read_env_file() -> dict[str, str]:
    """Read .env from DEUS_HOME or project dir."""
    candidates = []
    deus_home = os.environ.get("DEUS_HOME")
    if deus_home:
        candidates.append(Path(deus_home) / ".env")
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidates.append(Path(project_dir) / ".env")
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


def _get_api_token(config: dict) -> str | None:
    token = os.environ.get("LINEAR_API_TOKEN") or os.environ.get("LINEAR_API_KEY")
    if token:
        return token
    env_file = _read_env_file()
    return env_file.get("LINEAR_API_TOKEN") or env_file.get("LINEAR_API_KEY")


def _get_team_id(config: dict) -> str | None:
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
    if len(nodes) > 1:
        print(f"linear_vault_sync: {len(nodes)} teams found, using first", file=sys.stderr)
    return nodes[0]["id"]


def _fetch_issues(token: str, team_id: str) -> list[dict]:
    query = """
    query($teamId: String!) {
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
          url
          state { name type }
        }
      }
    }
    """
    result = _graphql(token, query, {"teamId": team_id})
    nodes = result.get("data", {}).get("issues", {}).get("nodes", [])
    return [
        n for n in nodes
        if n.get("state", {}).get("name") not in EXCLUDED_STATES
    ]


def _build_pending_block(issues: list[dict]) -> str:
    issues.sort(key=lambda i: (
        STATE_PRIORITY.get(i.get("state", {}).get("name", ""), 99),
        int(re.sub(r"\D", "", i.get("identifier", "0")) or "0"),
    ))
    lines = ["pending:", "  # Source of truth: Linear. Synced by /compress."]
    for issue in issues:
        title = issue.get("title", "")
        if len(title) > 80:
            title = title[:77] + "..."
        identifier = issue.get("identifier", "")
        lines.append(f"  - [ ] {title} ({identifier})")
    return "\n".join(lines)


PENDING_RE = re.compile(r"^pending:\s*\n((?:[ \t]+[^\n]*\n?)*)", re.MULTILINE)


def _sync(vault: Path, token: str, team_id: str) -> None:
    claude_md = vault / "CLAUDE.md"
    if not claude_md.exists():
        return

    issues = _fetch_issues(token, team_id)
    new_block = _build_pending_block(issues)

    content = claude_md.read_text(encoding="utf-8")
    if PENDING_RE.search(content):
        new_content = PENDING_RE.sub(lambda _: new_block + "\n", content)
    else:
        new_content = content + "\n" + new_block + "\n"

    if new_content == content:
        return

    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(vault), prefix=".CLAUDE.md.")
    try:
        os.write(tmp_fd, new_content.encode("utf-8"))
        os.close(tmp_fd)
        os.replace(tmp_path, str(claude_md))
    except Exception:
        os.close(tmp_fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def main() -> None:
    config = _load_config()
    vault = _vault_path(config)
    if not vault or not vault.exists():
        return

    token = _get_api_token(config)
    if not token:
        return

    team_id = _get_team_id(config)
    if not team_id:
        team_id = _discover_team_id(token)
    if not team_id:
        return

    try:
        _sync(vault, token, team_id)
    except (URLError, OSError, json.JSONDecodeError) as e:
        print(f"linear_vault_sync: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()

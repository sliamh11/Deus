"""Ollama connector -- native Anthropic-API-mode `deus connect` provider.

Ollama v0.14.0+ natively speaks the Anthropic Messages API (confirmed
against docs.ollama.com/integrations/claude-code and ollama.com/blog/claude)
-- no proxy/translation layer, no OAuth flow, no launchd daemon, since
Ollama already runs its own persistent service (menu-bar app on macOS /
systemd on Linux). This connector is a much simpler counterpart to
cliproxy_oauth: env_for_launch() is 3 static env vars plus one credential
value, write_config() has no daemon plist to manage.

Config split, mirroring cliproxy_oauth's pattern:
  - connectors/ollama/config.yaml -- tracked, placeholder-only, safe to commit.
  - ~/.config/deus/connectors/ollama/config.local.yaml -- the real config,
    OUTSIDE any project root a container agent could ever have mounted.
"""
from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from ...base import Connector, ConnectorSetupHandler

_REPO_ROOT = Path(__file__).resolve().parents[3]
TRACKED_CONFIG = _REPO_ROOT / "connectors" / "ollama" / "config.yaml"
LOCAL_CONFIG = Path(
    "~/.config/deus/connectors/ollama/config.local.yaml"
).expanduser()

# ONE generic subagent, not cliproxy_oauth's three GPT-tier names
# (deus-gpt-sol/terra/luna) -- real local-model diversity per user doesn't
# map to a fixed 3-tier split, and most users have 1-2 pulled models
# actually worth dispatching as a subagent, not three meaningfully distinct
# reasoning tiers of the same family.
STABLE_SUBAGENT_NAMES = ("deus-ollama-local",)

# Ollama's own Claude Code guidance (docs.ollama.com/integrations/claude-code)
# recommends 64k+ context; below 24 GiB VRAM Ollama defaults to 4k
# (docs.ollama.com/context-length), which is smaller than Deus's own system
# prompt -- a too-small allocation reports "healthy" on a bare ping and then
# fails or silently truncates real sessions.
_MIN_CONTEXT_LENGTH = 64000

# Ollama v0.14.0+ is required for native Anthropic Messages API support
# (docs.ollama.com/integrations/claude-code, docs.ollama.com/blog/claude) --
# below this, /v1/messages simply doesn't exist, an opaque failure that
# doesn't point back to "your Ollama is too old". Checked in verify(), not
# install() -- see OllamaSetupHandler.install()'s docstring for why a live
# version probe can't happen before the real host is known.
_MIN_OLLAMA_VERSION = (0, 14, 0)

_SUBAGENT_DESCRIPTIONS = {
    "deus-ollama-local": (
        "General-purpose subagent pinned to a local Ollama model via the "
        "native `deus connect ollama` route. Use for offline/no-cost tasks "
        "where a smaller local model is acceptable -- not a substitute for "
        "Claude on tasks needing strong reasoning or reliable tool use."
    ),
}


def _load_local_config() -> dict[str, Any]:
    if not LOCAL_CONFIG.is_file():
        return {}
    return yaml.safe_load(LOCAL_CONFIG.read_text()) or {}


def _find_binary() -> str | None:
    return shutil.which("ollama")


def _parse_version(version: str) -> tuple[int, ...]:
    """Parse a dotted version string ("0.31.1") into a comparable int
    tuple. Non-numeric trailing content (e.g. a "-rc1" suffix) is dropped
    from that component rather than raising -- an unparseable/empty string
    parses to (0,), which always compares below _MIN_OLLAMA_VERSION and
    fails verify() closed rather than raising.
    """
    parts: list[int] = []
    for part in version.split("."):
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def _probe_context_length(host: str, alias: str) -> bool:
    """GET /api/ps and confirm the running model's allocated context meets
    _MIN_CONTEXT_LENGTH. Must be called after a request that loads the
    model (e.g. the /v1/messages ping in verify()) -- /api/ps only reports
    currently-loaded models.
    """
    try:
        with urllib.request.urlopen(f"{host}/api/ps", timeout=5) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return False
    for model in data.get("models", []):
        name = model.get("name") or model.get("model")
        if name == alias:
            return model.get("context_length", 0) >= _MIN_CONTEXT_LENGTH
    return False


class OllamaConnector(Connector):
    @property
    def id(self) -> str:
        return "ollama"

    @property
    def description(self) -> str:
        return (
            "Ollama, native Anthropic-API mode -- routes to a "
            "locally-pulled model, no OAuth, no proxy daemon."
        )

    @property
    def engine(self) -> str:
        return "ollama"

    @property
    def risk_level(self) -> str:
        return "local-only"

    @property
    def setup_handler(self) -> ConnectorSetupHandler:
        return OllamaSetupHandler(self)

    def model_aliases(self) -> dict[str, str]:
        mapping = _load_local_config().get("deus-model-map") or {}
        return {
            name: mapping[name] for name in STABLE_SUBAGENT_NAMES if name in mapping
        }

    def is_configured(self) -> bool:
        return bool(self.model_aliases())

    def env_for_launch(self) -> dict[str, str]:
        cfg = _load_local_config()
        host = cfg.get("host", "http://localhost:11434")
        aliases = self.model_aliases()
        default_alias = cfg.get("default-model-alias") or next(
            iter(aliases.values()), ""
        )
        return {
            "ANTHROPIC_BASE_URL": host,
            "ANTHROPIC_AUTH_TOKEN": "ollama",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": default_alias,
        }

    def agents_for_launch(self) -> dict[str, Any]:
        aliases = self.model_aliases()
        alias = aliases.get("deus-ollama-local")
        if not alias:
            return {}
        return {
            "deus-ollama-local": {
                "description": _SUBAGENT_DESCRIPTIONS["deus-ollama-local"],
                "prompt": (
                    "You are deus-ollama-local, dispatched as a subagent, "
                    "reachable only through this deus connect session. Do "
                    "the task described in the prompt directly and report "
                    "your findings/output -- you are not a reviewer unless "
                    "explicitly asked to review something."
                ),
                "model": alias,
                # Same least-privilege scope as cliproxy_oauth's subagents,
                # for consistency across connectors.
                "tools": ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"],
            }
        }


class OllamaSetupHandler(ConnectorSetupHandler):
    def __init__(self, connector: "OllamaConnector"):
        self._connector = connector

    def install(self) -> bool:
        """Confirm the ollama binary is on PATH -- matches cliproxy_oauth's
        own install() (binary-presence-only, no live probe). Deliberately
        does NOT probe any host: at install-check time (Phase 4, before
        Phase 5 collects the real host), a non-default-host user's local
        config doesn't exist yet, so a live probe here could only ever
        check the default localhost:11434 -- reporting "not installed" for
        a legitimately-running non-default-host Ollama with no way to
        recover short of re-running the same failing check. Service
        liveness (and version compatibility) is checked in verify(), which
        runs after Phase 7 has written the real configured host.
        """
        return _find_binary() is not None

    def authenticate(self) -> bool:
        """No-op -- no OAuth/login concept for locally-pulled models.

        Ollama's optional hosted "cloud" model aliases require `ollama
        signin`, which is out of scope for this connector's v1 (documented
        future extension, same pattern as cliproxy_oauth's optional
        claude-api-key leg).
        """
        return True

    def write_config(self, values: dict[str, Any]) -> None:
        """No launchd plist at all -- Ollama manages its own persistent
        service already. Writes only the local YAML config: host + the
        deus-ollama-local model tag (confirmed against a real `ollama list`
        by the caller -- never invented, unlike a remote-account GPT id).
        """
        placeholder = yaml.safe_load(TRACKED_CONFIG.read_text())
        placeholder["host"] = values.get("host", "http://localhost:11434")
        model_map = values["model_map"]
        placeholder["deus-model-map"] = model_map
        placeholder["default-model-alias"] = values.get(
            "default_model_alias", next(iter(model_map.values()), "")
        )
        LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_CONFIG.write_text(yaml.safe_dump(placeholder, sort_keys=False))
        LOCAL_CONFIG.chmod(0o600)

    def verify(self) -> bool:
        cfg = _load_local_config()
        host = cfg.get("host", "http://localhost:11434")
        try:
            with urllib.request.urlopen(f"{host}/api/version", timeout=3) as resp:
                if resp.status != 200:
                    return False
                version_data = json.loads(resp.read())
            # A response that is valid JSON but the wrong shape (e.g.
            # {"version": null}, a bare list, a non-string version) must
            # still fail closed like every other branch here -- .get()'s
            # default only covers a missing key, not a present-but-wrong
            # value, so this is guarded explicitly rather than left to
            # _parse_version to raise on a non-str .split() call.
            raw_version = version_data.get("version", "") if isinstance(version_data, dict) else ""
            if not isinstance(raw_version, str):
                return False
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            return False
        if _parse_version(raw_version) < _MIN_OLLAMA_VERSION:
            # Below this, Ollama has no native Anthropic Messages API at
            # all -- the /v1/messages probe below would 404, an opaque
            # failure that doesn't point back to "your Ollama is too old".
            return False
        aliases = self._connector.model_aliases()
        if not aliases:
            return False
        alias = cfg.get("default-model-alias") or next(iter(aliases.values()))
        # Real, minimal, authenticated inference request through the exact
        # Anthropic Messages API path a `deus connect` launch would use --
        # includes a harmless tool definition (not a bare text ping) so a
        # completion-only model without tool-calling support fails here,
        # not on the first real Claude Code turn (which always sends tools).
        payload = json.dumps(
            {
                "model": alias,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "What time is it?"}],
                "tools": [
                    {
                        "name": "get_current_time",
                        "description": "Get the current time.",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
            }
        ).encode()
        req = urllib.request.Request(
            f"{host}/v1/messages",
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            # 120s, not 15s: this is a one-time setup-verification probe,
            # not a runtime request-latency budget -- a cold-starting
            # 32B-class local model (exactly what this connector expects
            # users to configure) commonly takes well over 15s just to
            # load into memory on first request, before any inference
            # even begins. A short timeout here would report a correctly
            # configured connector as unhealthy.
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status != 200:
                    return False
        except (urllib.error.URLError, OSError):
            return False
        # The ping above loads the model, populating /api/ps's entry for
        # it -- confirm the allocated context is large enough for a real
        # Deus session (Ollama's own default can be as low as 4k on
        # consumer VRAM, well under what Deus's system prompt needs).
        return _probe_context_length(host, alias)

"""
CLIProxyAPI OAuth connector — the proof-of-concept `deus connect` provider.

Reuses CLIProxyAPI (https://github.com/router-for-me/CLIProxyAPI) as the
engine rather than building a Deus-native gateway — same relationship Deus
already has with free-claude-code/Ollama/llama.cpp/Docker: orchestrate an
installed third-party binary, never fork or reimplement OAuth-multiplexing
logic ourselves.

Config split, relocated/generalized from the already-merged
examples/multi-model-cliproxyapi/ (commit b43f8c1d):
  - connectors/cliproxy/config.yaml — tracked, placeholder-only, safe to commit.
  - ~/.config/deus/connectors/cliproxy/config.local.yaml — the real config,
    OUTSIDE any project root a container agent could ever have mounted.
    Confirmed directly against src/project-registry.ts:155-174's
    SENSITIVE_FILE_PATTERNS/SENSITIVE_DIR_PATTERNS (checked only at the
    project-root top level) and src/container-mounter.ts:76-102's
    pushProjectShadows() (only shadows those exact top-level patterns): a
    nested connectors/cliproxy/config.local.yaml inside the repo would NOT
    have matched any shadowed pattern and would have been fully readable —
    including the real Anthropic key and the CLIProxyAPI inbound secret —
    by any container agent with read-only project access.
"""
from __future__ import annotations

import json
import plistlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import xml.parsers.expat
from pathlib import Path
from typing import Any

import yaml

from ...base import Connector, ConnectorSetupHandler

_REPO_ROOT = Path(__file__).resolve().parents[3]
TRACKED_CONFIG = _REPO_ROOT / "connectors" / "cliproxy" / "config.yaml"
LOCAL_CONFIG = Path(
    "~/.config/deus/connectors/cliproxy/config.local.yaml"
).expanduser()
# Namespaced under this specific connector, not the generic "cliproxyapi" --
# that generic name is exactly the kind of label anyone standing up their
# own personal CLIProxyAPI daemon would also pick (confirmed: it collided
# with a real, pre-existing personal install on this machine during
# testing). Namespacing reduces the odds of a future collision but is not
# itself the safety guarantee -- _write_launchd_plist's own existence/
# collision check below is.
PLIST_LABEL = "com.deus.connectors.cliproxy-oauth"
PLIST_PATH = Path(f"~/Library/LaunchAgents/{PLIST_LABEL}.plist").expanduser()

# Fixed, connector-defined stable subagent names — portable across users
# (real upstream model ids are per-account, not portable). Only the
# gitignored local config's alias-to-real-id mapping varies per user.
STABLE_SUBAGENT_NAMES = ("deus-gpt-sol", "deus-gpt-terra", "deus-gpt-luna")

# Must match connectors/cliproxy/config.yaml's literal placeholder exactly.
# authenticate() bootstraps LOCAL_CONFIG by copying that tracked template
# before OAuth login runs (so `--codex-login --config <path>` has a file to
# target) -- if login is then cancelled or fails, LOCAL_CONFIG is left
# holding this sentinel as api-keys[0]. is_configured() must reject it
# explicitly: a bare truthiness check on api-keys passes for this
# placeholder just as readily as for a real key, which would let a failed
# first authentication look "configured" to both a later setup run (which
# would then skip straight to verification) and a real deus connect launch
# (which would then use this literal string as ANTHROPIC_API_KEY).
_PLACEHOLDER_INBOUND_KEY = "REPLACE_WITH_YOUR_OWN_INBOUND_KEY"

_SUBAGENT_DESCRIPTIONS = {
    "deus-gpt-sol": (
        "General-purpose subagent pinned to GPT 5.6 Sol via the local "
        "multi-model gateway. Use for research, analysis, or a second "
        "opinion where a genuinely independent (non-Claude) model is "
        "valuable."
    ),
    "deus-gpt-terra": (
        "General-purpose subagent pinned to GPT 5.6 Terra via the local "
        "multi-model gateway. Use for research, analysis, or a second "
        "opinion where a genuinely independent (non-Claude) model is "
        "valuable."
    ),
    "deus-gpt-luna": (
        "General-purpose subagent pinned to GPT 5.6 Luna (max reasoning "
        "effort) via the local multi-model gateway. Use for harder "
        "research/analysis tasks warranting deeper reasoning."
    ),
}


def _load_local_config() -> dict[str, Any]:
    if not LOCAL_CONFIG.is_file():
        return {}
    return yaml.safe_load(LOCAL_CONFIG.read_text()) or {}


def _find_binary() -> str | None:
    return shutil.which("cli-proxy-api")


class CliproxyOauthConnector(Connector):
    @property
    def id(self) -> str:
        return "cliproxy-oauth"

    @property
    def description(self) -> str:
        return (
            "CLIProxyAPI, OAuth-login (reuses your ChatGPT/Codex "
            "subscription) — GPT 5.6 Sol/Terra/Luna subagents alongside "
            "normal Claude access."
        )

    @property
    def engine(self) -> str:
        return "cliproxy"

    @property
    def risk_level(self) -> str:
        return "oauth-reuse"

    @property
    def setup_handler(self) -> ConnectorSetupHandler:
        return CliproxyOauthSetupHandler(self)

    def model_aliases(self) -> dict[str, str]:
        mapping = _load_local_config().get("deus-model-map") or {}
        return {
            name: mapping[name] for name in STABLE_SUBAGENT_NAMES if name in mapping
        }

    def is_configured(self) -> bool:
        cfg = _load_local_config()
        keys = cfg.get("api-keys") or []
        if not keys or keys[0] == _PLACEHOLDER_INBOUND_KEY:
            return False
        return bool(self.model_aliases())

    def env_for_launch(self) -> dict[str, str]:
        cfg = _load_local_config()
        port = cfg.get("port", 8317)
        keys = cfg.get("api-keys") or []
        aliases = self.model_aliases()
        default_alias = cfg.get("default-model-alias") or next(
            iter(aliases.values()), ""
        )
        return {
            "ANTHROPIC_BASE_URL": f"http://localhost:{port}",
            "ANTHROPIC_API_KEY": keys[0] if keys else "",
            "ANTHROPIC_MODEL": default_alias,
            # Lets Claude Code's /model picker discover the claude-gpt-*
            # aliases (connectors/cliproxy/config.yaml's picker-discovery
            # twins) via CLIProxyAPI's own /v1/models -- live, in-session
            # model switching instead of launch-time-only ANTHROPIC_MODEL.
            # Harmless when no claude-* aliases are configured yet:
            # discovery just finds nothing extra, same as before this key
            # existed. Not in launch_connect()'s ambient-var clear list, so
            # it passes through eval "$env_output" unaffected.
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }

    def agents_for_launch(self) -> dict[str, Any]:
        aliases = self.model_aliases()
        agents: dict[str, Any] = {}
        for name in STABLE_SUBAGENT_NAMES:
            alias = aliases.get(name)
            if not alias:
                continue
            agents[name] = {
                "description": _SUBAGENT_DESCRIPTIONS[name],
                "prompt": (
                    f"You are {name}, dispatched as a subagent, reachable "
                    "only through this deus connect session. Do the task "
                    "described in the prompt directly and report your "
                    "findings/output -- you are not a reviewer unless "
                    "explicitly asked to review something."
                ),
                "model": alias,
                # Matches the least-privilege scope of the personal
                # prototype agents this connector formalizes
                # (~/.claude/agents/gpt-sol.md et al.) -- not the launching
                # session's full tool set.
                "tools": ["Read", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"],
            }
        return agents


class CliproxyOauthSetupHandler(ConnectorSetupHandler):
    def __init__(self, connector: "CliproxyOauthConnector"):
        self._connector = connector

    def install(self) -> bool:
        """Confirm the cli-proxy-api binary is present on PATH.

        Never silently auto-fetches — this binary will hold real OAuth
        tokens, so a missing binary must surface upstream repo + build
        instructions and require explicit confirmation, not a silent fetch.
        """
        return _find_binary() is not None

    def authenticate(self) -> bool:
        binary = _find_binary()
        if not binary:
            return False
        LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        if not LOCAL_CONFIG.is_file():
            LOCAL_CONFIG.write_text(TRACKED_CONFIG.read_text())
            LOCAL_CONFIG.chmod(0o600)
        result = subprocess.run(
            [binary, "--codex-login", "--config", str(LOCAL_CONFIG)]
        )
        return result.returncode == 0

    def write_config(self, values: dict[str, Any]) -> None:
        placeholder = yaml.safe_load(TRACKED_CONFIG.read_text())
        placeholder["api-keys"] = [values["inbound_key"]]
        if values.get("anthropic_api_key"):
            claude_keys = placeholder.get("claude-api-key") or [{}]
            claude_keys[0]["api-key"] = values["anthropic_api_key"]
            placeholder["claude-api-key"] = claude_keys
        else:
            # The optional Anthropic leg was declined -- strip the
            # placeholder claude-api-key block entirely rather than
            # carrying REPLACE_WITH_YOUR_REAL_ANTHROPIC_API_KEY through into
            # the real config. CLIProxyAPI would register that sentinel as
            # a live provider leg, and a later `/model opus-planner` (or
            # whatever alias it maps to) would 401 for a reason nothing
            # surfaces.
            placeholder.pop("claude-api-key", None)
        model_map = values["model_map"]
        placeholder["deus-model-map"] = model_map
        placeholder["default-model-alias"] = values.get(
            "default_model_alias", next(iter(model_map.values()), "")
        )
        LOCAL_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_CONFIG.write_text(yaml.safe_dump(placeholder, sort_keys=False))
        LOCAL_CONFIG.chmod(0o600)
        if sys.platform == "darwin":
            self._write_launchd_plist(values["binary_path"])
        else:
            print(
                "deus connect: launchd daemon setup is macOS-only for now "
                "(Linux systemd-unit parity is a stated follow-up) -- "
                "config was written, but you must start "
                f"'{values['binary_path']} --config {LOCAL_CONFIG}' "
                "yourself, e.g. via a systemd user unit."
            )

    def _write_launchd_plist(self, binary_path: str) -> None:
        """macOS-only -- callers must guard with sys.platform == "darwin"
        before calling this (see write_config()). ProgramArguments must use
        the fully home-expanded absolute path -- launchd execs the binary
        directly with literal argv strings and never shell-expands `~`.
        Confirmed against this repo's own launchd precedent,
        com.deus.warden-opa.plist, whose ProgramArguments already use
        fully-expanded absolute paths throughout — the established
        convention, not a new pattern.
        """
        plist = {
            "Label": PLIST_LABEL,
            "ProgramArguments": [binary_path, "--config", str(LOCAL_CONFIG)],
            "RunAtLoad": True,
            "KeepAlive": True,
        }
        # Never lose, overwrite, or downgrade user data (core-behavioral-
        # rules.md § Data & Security) -- a bare plistlib.dump here would
        # silently destroy a pre-existing file at this exact path with no
        # backup, and the setup skill's own `launchctl kickstart` step one
        # phase later would then kill and relaunch whatever daemon that
        # file used to describe under THIS config instead -- turning a
        # latent file collision into an active hijack of a running
        # process. Reproduced for real on this machine during this
        # session's own testing (a personal, pre-existing CLIProxyAPI
        # install happened to use this same conventional path). Abort
        # loudly instead of overwriting if something else is already there.
        if PLIST_PATH.exists():
            try:
                existing = plistlib.loads(PLIST_PATH.read_bytes())
                existing_args = existing.get("ProgramArguments")
            except (
                plistlib.InvalidFileException,
                xml.parsers.expat.ExpatError,
                ValueError,
                OSError,
            ):
                existing_args = None
            if existing_args != plist["ProgramArguments"]:
                raise RuntimeError(
                    f"A launchd job already exists at {PLIST_PATH} with "
                    f"different settings (ProgramArguments: {existing_args!r}). "
                    "Refusing to overwrite it -- this may be an unrelated "
                    "daemon. Move or remove that file yourself first if you "
                    "intend to replace it, then re-run setup."
                )
        PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(PLIST_PATH, "wb") as f:
            plistlib.dump(plist, f)

    def verify(self) -> bool:
        cfg = _load_local_config()
        port = cfg.get("port", 8317)
        try:
            with urllib.request.urlopen(
                f"http://localhost:{port}/healthz", timeout=3
            ) as resp:
                if resp.status != 200:
                    return False
        except (urllib.error.URLError, OSError):
            return False
        aliases = self._connector.model_aliases()
        keys = cfg.get("api-keys") or []
        if not aliases or not keys:
            return False
        # /healthz only proves the process is up -- it says nothing about
        # whether the OAuth credential behind it still works or the
        # configured model alias actually resolves. Perform one real,
        # minimal, authenticated inference request through the exact
        # Anthropic Messages API path a `deus connect` launch would use, so
        # an expired token or a broken upstream alias genuinely fails
        # verification instead of reporting "healthy".
        alias = cfg.get("default-model-alias") or next(iter(aliases.values()))
        payload = json.dumps(
            {
                "model": alias,
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ).encode()
        req = urllib.request.Request(
            f"http://localhost:{port}/v1/messages",
            data=payload,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": keys[0],
                "anthropic-version": "2023-06-01",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status == 200
        except (urllib.error.URLError, OSError):
            return False

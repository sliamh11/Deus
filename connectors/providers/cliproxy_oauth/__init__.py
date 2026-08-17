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
from dataclasses import dataclass
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


@dataclass(frozen=True)
class GptModelDef:
    """One onboarded GPT model's portable (account-agnostic) metadata --
    the per-account upstream routing (which real model "sol" points to)
    lives only in the user's local config's deus-model-map, never here.

    Value Object (frozen, structural equality, no identity) -- the single
    source of truth model_aliases()/agents_for_launch() derive from,
    replacing what used to be two separately hand-maintained structures
    (a name tuple + a description dict) that had to be kept in sync by
    hand when onboarding a new model. Deliberately a plain tuple below,
    not its own Registry class: every consumer only ever needs full
    iteration, never id-keyed lookup, so O(n) iteration is exactly what's
    needed -- the same "declarative single source of truth" idea
    Connector/ConnectorRegistry already apply one level up (per-engine),
    applied one level down (per-model, within this one connector).
    """

    subagent_name: str
    description: str
    # The tracked template's placeholder upstream model name
    # (connectors/cliproxy/config.yaml's oauth-model-alias.codex[].name
    # AND its payload.override[].models[].name -- these two must stay in
    # sync, same as the claude-gpt-* picker-discovery twin's name).
    # write_config() uses this to find the right payload.override entry
    # to mutate for effort_map, without a second hardcoded lookup table.
    template_upstream_name: str


# Single source of truth for every onboarded GPT model. To add one: add a
# GptModelDef entry here, plus the matching oauth-model-alias.codex[]
# (plain + picker-discovery) and deus-model-map entries to
# connectors/cliproxy/config.yaml (the real upstream model id is
# account-specific and can only be confirmed during setup -- see
# add-connector/SKILL.md Phase 5).
GPT_MODELS: tuple[GptModelDef, ...] = (
    GptModelDef(
        "deus-gpt-sol",
        "General-purpose subagent pinned to GPT 5.6 Sol via the local "
        "multi-model gateway. Use for research, analysis, or a second "
        "opinion where a genuinely independent (non-Claude) model is "
        "valuable.",
        "gpt-5.6-sol",
    ),
    GptModelDef(
        "deus-gpt-terra",
        "General-purpose subagent pinned to GPT 5.6 Terra via the local "
        "multi-model gateway. Use for research, analysis, or a second "
        "opinion where a genuinely independent (non-Claude) model is "
        "valuable.",
        "gpt-5.6-terra",
    ),
    GptModelDef(
        "deus-gpt-luna",
        "General-purpose subagent pinned to GPT 5.6 Luna (max reasoning "
        "effort) via the local multi-model gateway. Use for harder "
        "research/analysis tasks warranting deeper reasoning.",
        "gpt-5.6-luna",
    ),
)

# Codex's real reasoning-effort levels (confirmed via `codex debug
# models` against the account's own live model catalog -- NOT Claude
# Code's effort-level table, which was the wrong source for a prior
# version of this constant and undercounted these levels). The full
# catalog shows 6 levels on gpt-5.6-sol/terra (low/medium/high/xhigh/
# max/ultra) but only 5 on gpt-5.6-luna (no "ultra"). "ultra" is
# deliberately excluded from this shared tuple: it isn't available on
# all three models, and its catalog description ("automatic task
# delegation") suggests client-side orchestration behavior that this
# connector's payload.override raw-JSON-injection mechanism may not be
# able to replicate through the proxy -- unverified, needs a live probe
# before trusting it. Used to validate effort_map values in
# write_config().
_CODEX_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")

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
            m.subagent_name: mapping[m.subagent_name]
            for m in GPT_MODELS
            if m.subagent_name in mapping
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
            # Real credential goes on ANTHROPIC_AUTH_TOKEN, not
            # ANTHROPIC_API_KEY -- matches the ollama connector's exact
            # pattern. Claude Code's ANTHROPIC_API_KEY approval check
            # compares only the last 20 characters of the key against a
            # ~/.claude.json allowlist, so this connector's full-length key
            # could never match and silently fell through to the user's
            # personal OAuth subscription instead. ANTHROPIC_AUTH_TOKEN
            # sidesteps that check and outranks ANTHROPIC_API_KEY in
            # Claude Code's own auth precedence.
            "ANTHROPIC_AUTH_TOKEN": keys[0] if keys else "",
            # Explicitly emptied, not omitted: masks any ambient
            # ANTHROPIC_API_KEY the launching shell might have set, so it
            # can never be picked up instead of this connector's own
            # credential.
            "ANTHROPIC_API_KEY": "",
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
            # GPT-5.6 Sol/Terra/Luna have a real, server-enforced 272,000-
            # token context window when reached through Codex OAuth (this
            # connector's mechanism) -- confirmed directly against this
            # account's live model catalog and independently corroborated
            # by an OpenAI Codex maintainer statement (github.com/openai/
            # codex#19464: "this is not something you can work around by
            # making client-side adjustments... needs to be implemented
            # server-side") and a CLIProxyAPI maintainer statement on this
            # exact model family (github.com/router-for-me/CLIProxyAPI
            # #4195). No config/proxy override changes this -- it is not a
            # display artifact.
            #
            # Claude Code determines auto-compact's threshold by pattern-
            # matching the model ID against three cases (verified verbatim
            # against code.claude.com/docs/en/model-config "Correct the
            # window for a gateway or custom model ID"): (1) an ID with no
            # `claude-` prefix, no `[1m]` suffix, and unresolvable to a
            # Claude model -> this override applies directly; (2) same but
            # with `[1m]` -> needs CLAUDE_CODE_DISABLE_1M_CONTEXT too
            # (not our case); (3) an ID that starts with `claude-` or
            # resolves to a Claude model -> this override is IGNORED unless
            # DISABLE_COMPACT is also set (which disables compaction
            # entirely, not what we want).
            #
            # Each dispatched subagent (deus-gpt-sol/terra/luna, via
            # agents_for_launch() below) runs in its own context window
            # keyed to ITS OWN "model" field -- the plain aliases
            # ("sol"/"terra"/"luna-max"), which hit case (1) and get this
            # override correctly. That is the primary, intended path this
            # override exists for.
            #
            # The Claude leg no longer hits case (1). connectors/cliproxy/
            # config.yaml's `claude-api-key` leg maps its client-facing id
            # to a REAL Claude model id (`claude-opus-5`, alias == name), so
            # Claude Code resolves it and applies that model's own context
            # window rather than inheriting this GPT cap. That was not true
            # while the leg advertised the opaque alias "opus-planner": a
            # `/model`-switch to it INCORRECTLY inherited this 272K cap,
            # artificially throttling a real Claude model. The identity
            # mapping is load-bearing for that reason and is pinned by a
            # drift test -- see the config's own comment before changing it.
            #
            # Still-open gap: the `claude-gpt-sol`/`terra`/`luna` picker-
            # discovery aliases (deliberately `claude-`-prefixed so
            # CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY surfaces them in
            # the /model picker) hit case (3) -- this override does NOT
            # apply to them at all, so a user reaching a GPT model via the
            # /model picker (rather than subagent dispatch) gets NO
            # correction and is exposed to the original silent-wrong-
            # threshold problem this override exists to fix. That gap is
            # scoped to manual /model-switching only, not the primary
            # dispatched-subagent path, and is not fixed by this override
            # -- Claude Code has no mechanism to resolve an opaque proxy
            # alias to its real upstream model or context window.
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "272000",
            # Forces auto-compact on for this connector's launched session,
            # overriding the user's global ~/.claude/settings.json
            # (autoCompactEnabled: false is a deliberate host-wide choice
            # for normal Claude usage, left untouched). Consumed by
            # deus-cmd.sh's launch_connect() via the DEUS_CONNECT_SETTINGS_
            # JSON reserved key (connectors/base.py's env_for_launch()
            # docstring) -- forwarded as `claude --settings <value>` for
            # this one launch only. Session-wide, with no per-subagent
            # override (Claude Code has no such mechanism) -- so any
            # portion of the session using a real Claude model (e.g.
            # `claude-opus-5`) also auto-compacts more eagerly than the
            # user's global default would, independent of the context-
            # window gap documented above.
            "DEUS_CONNECT_SETTINGS_JSON": '{"autoCompactEnabled": true}',
        }

    def agents_for_launch(self) -> dict[str, Any]:
        aliases = self.model_aliases()
        agents: dict[str, Any] = {}
        for model in GPT_MODELS:
            alias = aliases.get(model.subagent_name)
            if not alias:
                continue
            agents[model.subagent_name] = {
                "description": model.description,
                "prompt": (
                    f"You are {model.subagent_name}, dispatched as a "
                    "subagent, reachable only through this deus connect "
                    "session. Do the task described in the prompt "
                    "directly and report your findings/output -- you are "
                    "not a reviewer unless explicitly asked to review "
                    "something."
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
            # a live provider leg, and a later `/model claude-opus-5` (or
            # whatever id that leg advertises) would 401 for a reason
            # nothing surfaces.
            placeholder.pop("claude-api-key", None)
        model_map = values["model_map"]
        placeholder["deus-model-map"] = model_map
        placeholder["default-model-alias"] = values.get(
            "default_model_alias", next(iter(model_map.values()), "")
        )
        # Only touches entries the caller actually specifies -- a subagent
        # omitted from effort_map keeps the template's baked-in default
        # (see connectors/cliproxy/config.yaml's payload.override block).
        # An unknown key (typo, stale/removed model) is rejected loudly
        # rather than silently never applied, matching this file's
        # fail-loud posture elsewhere (e.g. the claude-api-key placeholder
        # rejection above).
        effort_map = values.get("effort_map") or {}
        known_subagents = {m.subagent_name for m in GPT_MODELS}
        unknown = set(effort_map) - known_subagents
        if unknown:
            raise ValueError(
                f"effort_map has unknown subagent name(s) {sorted(unknown)} "
                f"-- must be one of {sorted(known_subagents)}"
            )
        overrides = placeholder.get("payload", {}).get("override", [])
        for model in GPT_MODELS:
            level = effort_map.get(model.subagent_name)
            if level is None:
                continue
            if level not in _CODEX_EFFORT_LEVELS:
                raise ValueError(
                    f"invalid effort level {level!r} for {model.subagent_name} "
                    f"-- must be one of {_CODEX_EFFORT_LEVELS}"
                )
            matched = False
            for rule in overrides:
                names = [m.get("name") for m in rule.get("models", [])]
                if model.template_upstream_name in names:
                    rule["params"]["reasoning.effort"] = level
                    matched = True
                    break
            if not matched:
                # Same fail-loud posture as the unknown-key/invalid-level
                # checks above -- a template with a dropped/renamed
                # payload.override entry (drift the TestTrackedConfig
                # PickerDiscoveryAliases test guards against, but that's an
                # external guard, not a runtime check) must not silently
                # write a config where the requested effort was never
                # actually applied.
                raise ValueError(
                    f"no payload.override entry found for "
                    f"{model.subagent_name}'s upstream name "
                    f"{model.template_upstream_name!r} -- template drift?"
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

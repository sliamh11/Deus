"""Tests for the Connector / ConnectorRegistry pattern."""
from typing import Any

import pytest
import yaml

from connectors.base import Connector, ConnectorSetupHandler
from connectors.registry import (
    ConnectorRegistrationError,
    ConnectorRegistry,
    UnknownConnectorError,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


class FakeSetupHandler(ConnectorSetupHandler):
    def install(self) -> bool:
        return True

    def authenticate(self) -> bool:
        return True

    def write_config(self, values: dict[str, Any]) -> None:
        pass

    def verify(self) -> bool:
        return True


class FakeConnector(Connector):
    """Configurable connector for testing."""

    def __init__(self, connector_id: str, configured: bool = True):
        self._id = connector_id
        self._configured = configured

    @property
    def id(self) -> str:
        return self._id

    @property
    def description(self) -> str:
        return f"fake connector {self._id}"

    @property
    def engine(self) -> str:
        return "fake-engine"

    @property
    def risk_level(self) -> str:
        return "none"

    @property
    def setup_handler(self) -> ConnectorSetupHandler:
        return FakeSetupHandler()

    def model_aliases(self) -> dict[str, str]:
        return {"fake-agent": "fake-alias"} if self._configured else {}

    def is_configured(self) -> bool:
        return self._configured

    def env_for_launch(self) -> dict[str, str]:
        return {"ANTHROPIC_BASE_URL": "http://localhost:9999"} if self._configured else {}

    def agents_for_launch(self) -> dict[str, Any]:
        return {"fake-agent": {"description": "d", "prompt": "p", "model": "fake-alias"}}


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test gets a fresh registry."""
    ConnectorRegistry.reset()
    yield
    ConnectorRegistry.reset()


# ── Registry unit tests ─────────────────────────────────────────────────────


class TestConnectorRegistry:
    def test_register_and_resolve(self):
        reg = ConnectorRegistry.default()
        c = FakeConnector("test")
        reg.register(c)
        assert reg.resolve("test") is c

    def test_resolve_unknown_raises(self):
        reg = ConnectorRegistry.default()
        with pytest.raises(UnknownConnectorError):
            reg.resolve("nonexistent")

    def test_register_rejects_reserved_ids(self):
        reg = ConnectorRegistry.default()
        # "default" plus its own sub-namespace (show/off/clear) -- a
        # connector using any of these would be permanently unreachable
        # via `deus connect <id>`, intercepted by the `default)` case arm
        # before the launch catch-all ever runs (deus connect, #1171-followup).
        for reserved in ("list", "setup", "status", "default", "show", "off", "clear"):
            with pytest.raises(ConnectorRegistrationError):
                reg.register(FakeConnector(reserved))

    def test_list_ids_sorted(self):
        reg = ConnectorRegistry.default()
        reg.register(FakeConnector("zeta"))
        reg.register(FakeConnector("alpha"))
        reg.register(FakeConnector("mid"))
        assert reg.list_ids() == ["alpha", "mid", "zeta"]

    def test_list_connectors_matches_list_ids_order(self):
        reg = ConnectorRegistry.default()
        reg.register(FakeConnector("b"))
        reg.register(FakeConnector("a"))
        assert [c.id for c in reg.list_connectors()] == ["a", "b"]

    def test_register_last_write_wins(self):
        reg = ConnectorRegistry.default()
        first = FakeConnector("dup")
        second = FakeConnector("dup")
        reg.register(first)
        reg.register(second)
        assert reg.resolve("dup") is second

    def test_singleton(self):
        a = ConnectorRegistry.default()
        b = ConnectorRegistry.default()
        assert a is b

    def test_reset_creates_fresh_instance(self):
        a = ConnectorRegistry.default()
        a.register(FakeConnector("x"))
        ConnectorRegistry.reset()
        b = ConnectorRegistry.default()
        assert a is not b
        assert b.list_ids() == []


class TestConnectorContract:
    def test_is_configured_reflects_state(self):
        configured = FakeConnector("a", configured=True)
        unconfigured = FakeConnector("b", configured=False)
        assert configured.is_configured() is True
        assert unconfigured.is_configured() is False

    def test_env_for_launch_empty_when_unconfigured(self):
        c = FakeConnector("a", configured=False)
        assert c.env_for_launch() == {}

    def test_setup_handler_returns_handler_instance(self):
        c = FakeConnector("a")
        assert isinstance(c.setup_handler, ConnectorSetupHandler)


class TestBuiltInProviders:
    """Smoke tests that the real connector can be instantiated directly.

    Deliberately does NOT resolve through ConnectorRegistry.default() here:
    connectors/providers/__init__.py registers into the singleton once, at
    first import (module-cached, like any Python import) -- this test
    file's own `clean_registry` fixture resets that singleton before/after
    every test, so a registry-based lookup here would depend on import
    order versus fixture order rather than testing the connector itself.
    Mirrors evolution/tests/test_judge_registry.py's TestBuiltInProviders,
    which instantiates providers directly for the same reason.
    """

    def test_cliproxy_oauth_has_correct_id_and_engine(self):
        from connectors.providers.cliproxy_oauth import CliproxyOauthConnector

        c = CliproxyOauthConnector()
        assert c.id == "cliproxy-oauth"
        assert c.engine == "cliproxy"
        assert isinstance(c.setup_handler, ConnectorSetupHandler)


class TestCliproxyOauthIsConfigured:
    """Regression coverage for a real defect a GPT co-gate review caught:
    authenticate() bootstraps LOCAL_CONFIG by copying the tracked
    placeholder template before OAuth login runs, so a cancelled/failed
    login left is_configured() reporting True on nothing but sentinel
    values (a bare truthiness check on api-keys passes for the literal
    placeholder string just as readily as for a real key).
    """

    @pytest.fixture(autouse=True)
    def _redirect_local_config(self, tmp_path, monkeypatch):
        import connectors.providers.cliproxy_oauth as mod

        monkeypatch.setattr(mod, "LOCAL_CONFIG", tmp_path / "config.local.yaml")
        self.mod = mod

    def test_bootstrapped_placeholder_is_not_configured(self):
        # Exactly what authenticate() does before OAuth login: copy the
        # tracked template verbatim, no real values yet.
        self.mod.LOCAL_CONFIG.write_text(self.mod.TRACKED_CONFIG.read_text())
        c = self.mod.CliproxyOauthConnector()
        assert c.is_configured() is False

    def test_real_inbound_key_is_configured(self):
        self.mod.LOCAL_CONFIG.write_text(
            "api-keys:\n  - real-key\n"
            "deus-model-map:\n  deus-gpt-sol: sol\n"
        )
        c = self.mod.CliproxyOauthConnector()
        assert c.is_configured() is True

    def test_missing_config_is_not_configured(self):
        c = self.mod.CliproxyOauthConnector()
        assert c.is_configured() is False


class TestCliproxyOauthEnvForLaunch:
    """Coverage for env_for_launch()'s CLAUDE_CODE_ENABLE_GATEWAY_MODEL_
    DISCOVERY key (model-picker-visibility follow-up) -- lets Claude Code's
    /model picker discover the claude-gpt-* aliases via CLIProxyAPI's own
    /v1/models, for live in-session model switching instead of
    launch-time-only ANTHROPIC_MODEL.
    """

    @pytest.fixture(autouse=True)
    def _redirect_local_config(self, tmp_path, monkeypatch):
        import connectors.providers.cliproxy_oauth as mod

        monkeypatch.setattr(mod, "LOCAL_CONFIG", tmp_path / "config.local.yaml")
        self.mod = mod

    def test_gateway_discovery_key_present_alongside_existing_three(self):
        self.mod.LOCAL_CONFIG.write_text(
            "port: 8317\n"
            "api-keys:\n  - real-key\n"
            "deus-model-map:\n  deus-gpt-sol: sol\n"
            "default-model-alias: sol\n"
        )
        c = self.mod.CliproxyOauthConnector()
        env = c.env_for_launch()
        assert env == {
            "ANTHROPIC_BASE_URL": "http://localhost:8317",
            "ANTHROPIC_AUTH_TOKEN": "real-key",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_MODEL": "sol",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "272000",
            "DEUS_CONNECT_SETTINGS_JSON": '{"autoCompactEnabled": true}',
        }

    def test_gateway_discovery_key_present_even_when_unconfigured(self):
        # env_for_launch() itself doesn't gate on is_configured() -- that's
        # the caller's job (connectors_cli.py's cmd_env checks is_configured()
        # first). Confirms the new key isn't accidentally conditional on
        # having claude-gpt-* aliases set up.
        c = self.mod.CliproxyOauthConnector()
        env = c.env_for_launch()
        assert env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] == "1"

    def test_context_window_and_autocompact_keys_present(self):
        # GPT-5.6 Sol/Terra/Luna have a real, server-enforced 272,000-token
        # context window via Codex OAuth (confirmed against this account's
        # live model catalog + independent OpenAI/CLIProxyAPI maintainer
        # statements). These two keys correct Claude Code's auto-compact
        # threshold for the unrecognized model aliases and force
        # auto-compact on for this connector's session only, without
        # touching the user's global ~/.claude/settings.json.
        c = self.mod.CliproxyOauthConnector()
        env = c.env_for_launch()
        assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "272000"
        assert env["DEUS_CONNECT_SETTINGS_JSON"] == '{"autoCompactEnabled": true}'


class TestCliproxyOauthAgentsForLaunch:
    """agents_for_launch() had zero direct test coverage before this class
    -- added as a real before/after regression check for the GPT model-
    registry consolidation (STABLE_SUBAGENT_NAMES + _SUBAGENT_DESCRIPTIONS
    -> a single GptModelDef/GPT_MODELS table).
    """

    @pytest.fixture(autouse=True)
    def _redirect_local_config(self, tmp_path, monkeypatch):
        import connectors.providers.cliproxy_oauth as mod

        monkeypatch.setattr(mod, "LOCAL_CONFIG", tmp_path / "config.local.yaml")
        self.mod = mod

    def test_empty_when_unconfigured(self):
        c = self.mod.CliproxyOauthConnector()
        assert c.agents_for_launch() == {}

    def test_only_configured_subagents_are_included(self):
        self.mod.LOCAL_CONFIG.write_text("deus-model-map:\n  deus-gpt-sol: sol\n")
        c = self.mod.CliproxyOauthConnector()
        agents = c.agents_for_launch()
        assert set(agents.keys()) == {"deus-gpt-sol"}

    def test_agent_shape_and_content(self):
        self.mod.LOCAL_CONFIG.write_text(
            "deus-model-map:\n"
            "  deus-gpt-sol: sol\n"
            "  deus-gpt-terra: terra\n"
            "  deus-gpt-luna: luna-max\n"
        )
        c = self.mod.CliproxyOauthConnector()
        agents = c.agents_for_launch()

        assert set(agents.keys()) == {
            "deus-gpt-sol",
            "deus-gpt-terra",
            "deus-gpt-luna",
        }
        sol = agents["deus-gpt-sol"]
        assert sol["model"] == "sol"
        assert "deus-gpt-sol" in sol["prompt"]
        assert sol["description"]
        assert sol["tools"] == [
            "Read",
            "Grep",
            "Glob",
            "Bash",
            "WebSearch",
            "WebFetch",
        ]

        luna = agents["deus-gpt-luna"]
        assert luna["model"] == "luna-max"

    def test_descriptions_are_distinct_per_subagent(self):
        self.mod.LOCAL_CONFIG.write_text(
            "deus-model-map:\n"
            "  deus-gpt-sol: sol\n"
            "  deus-gpt-terra: terra\n"
            "  deus-gpt-luna: luna-max\n"
        )
        c = self.mod.CliproxyOauthConnector()
        agents = c.agents_for_launch()
        descriptions = {a["description"] for a in agents.values()}
        assert len(descriptions) == 3


class TestCliproxyOauthWriteConfig:
    """write_config() had zero direct test coverage before this class --
    added alongside the per-model reasoning-effort feature (effort_map),
    since a config-writing method with no tests is exactly where a
    silent-no-op or wrong-mutation regression would go unnoticed.
    """

    @pytest.fixture(autouse=True)
    def _redirect_paths(self, tmp_path, monkeypatch):
        import connectors.providers.cliproxy_oauth as mod

        tracked_config = tmp_path / "tracked-config.yaml"
        tracked_config.write_text(
            yaml.safe_dump(
                {
                    "api-keys": ["REPLACE_WITH_YOUR_OWN_INBOUND_KEY"],
                    "deus-model-map": {},
                    "default-model-alias": "",
                    "payload": {
                        "override": [
                            {
                                "models": [
                                    {"name": "gpt-5.6-sol", "protocol": "codex"}
                                ],
                                "params": {"reasoning.effort": "high"},
                            },
                            {
                                "models": [
                                    {"name": "gpt-5.6-terra", "protocol": "codex"}
                                ],
                                "params": {"reasoning.effort": "high"},
                            },
                            {
                                "models": [
                                    {"name": "gpt-5.6-luna", "protocol": "codex"}
                                ],
                                "params": {"reasoning.effort": "xhigh"},
                            },
                        ]
                    },
                },
                sort_keys=False,
            )
        )
        monkeypatch.setattr(mod, "TRACKED_CONFIG", tracked_config)
        monkeypatch.setattr(mod, "LOCAL_CONFIG", tmp_path / "config.local.yaml")
        monkeypatch.setattr(mod, "PLIST_PATH", tmp_path / "test.plist")
        self.mod = mod
        self.handler = mod.CliproxyOauthSetupHandler(mod.CliproxyOauthConnector())

    def _base_values(self, **overrides):
        values = {
            "inbound_key": "real-key",
            "model_map": {"deus-gpt-sol": "sol"},
            "default_model_alias": "sol",
            "binary_path": "/usr/local/bin/cli-proxy-api",
        }
        values.update(overrides)
        return values

    def _written_overrides(self):
        written = yaml.safe_load(self.mod.LOCAL_CONFIG.read_text())
        by_name = {}
        for rule in written["payload"]["override"]:
            name = rule["models"][0]["name"]
            by_name[name] = rule["params"]["reasoning.effort"]
        return by_name

    def test_effort_map_omitted_leaves_template_defaults(self):
        self.handler.write_config(self._base_values())
        assert self._written_overrides() == {
            "gpt-5.6-sol": "high",
            "gpt-5.6-terra": "high",
            "gpt-5.6-luna": "xhigh",
        }

    def test_effort_map_with_one_subagent_only_changes_that_entry(self):
        self.handler.write_config(
            self._base_values(effort_map={"deus-gpt-luna": "low"})
        )
        assert self._written_overrides() == {
            "gpt-5.6-sol": "high",
            "gpt-5.6-terra": "high",
            "gpt-5.6-luna": "low",
        }

    def test_max_effort_level_is_accepted(self):
        # Regression guard: "max" is a real Codex level (confirmed via
        # `codex debug models` against the live account catalog, on all
        # three of sol/terra/luna) that a prior version of this constant
        # wrongly rejected -- pin it so a future accidental narrowing
        # back to the 4-level set is caught here, not live.
        self.handler.write_config(
            self._base_values(effort_map={"deus-gpt-luna": "max"})
        )
        assert self._written_overrides()["gpt-5.6-luna"] == "max"

    def test_invalid_effort_level_raises(self):
        with pytest.raises(ValueError, match="invalid effort level"):
            self.handler.write_config(
                self._base_values(effort_map={"deus-gpt-sol": "bogus"})
            )

    def test_unknown_subagent_key_raises_not_silently_skipped(self):
        with pytest.raises(ValueError, match="unknown subagent"):
            self.handler.write_config(
                self._base_values(effort_map={"deus-gpt-solo": "high"})
            )

    def test_missing_override_entry_raises_not_silently_skipped(self, tmp_path, monkeypatch):
        # A template whose payload.override drifted (e.g. an entry got
        # dropped or its name renamed) must not let write_config() silently
        # write a config where the requested effort was never applied.
        drifted_config = tmp_path / "drifted-tracked-config.yaml"
        drifted_config.write_text(
            yaml.safe_dump(
                {
                    "api-keys": ["REPLACE_WITH_YOUR_OWN_INBOUND_KEY"],
                    "deus-model-map": {},
                    "default-model-alias": "",
                    "payload": {
                        "override": [
                            {
                                "models": [
                                    {"name": "gpt-5.6-sol", "protocol": "codex"}
                                ],
                                "params": {"reasoning.effort": "high"},
                            },
                            # terra's entry is missing entirely -- simulates
                            # template drift.
                            {
                                "models": [
                                    {"name": "gpt-5.6-luna", "protocol": "codex"}
                                ],
                                "params": {"reasoning.effort": "xhigh"},
                            },
                        ]
                    },
                },
                sort_keys=False,
            )
        )
        monkeypatch.setattr(self.mod, "TRACKED_CONFIG", drifted_config)

        with pytest.raises(ValueError, match="template drift"):
            self.handler.write_config(
                self._base_values(effort_map={"deus-gpt-terra": "low"})
            )


class TestTrackedConfigClaudeLegAlias:
    """The `claude-api-key` leg's client-facing id must be a real Claude
    model id, not an opaque alias -- enforced here because nothing else
    enforces it and the failure modes are both silent.

    An id Claude Code cannot resolve (a) reaches the proxy carrying the
    1M-context variant marker, which CLIProxyAPI's splitModelThinkingSuffix
    rejects with "unknown provider" (it parses only the parenthesised
    `model(value)` form), and (b) inherits this connector's GPT context cap
    of 272K, silently throttling a real Claude model. Both were live while
    the leg advertised "opus-planner". The identity mapping is the fix, so
    it is load-bearing rather than cosmetic.
    """

    def test_claude_leg_alias_is_the_real_model_id(self):
        import connectors.providers.cliproxy_oauth as mod

        cfg = yaml.safe_load(mod.TRACKED_CONFIG.read_text())
        legs = cfg.get("claude-api-key") or []
        assert legs, "tracked template lost its claude-api-key leg"

        for leg in legs:
            models = leg.get("models") or []
            assert models, "claude-api-key leg has no models[] entries"
            for entry in models:
                name, alias = entry["name"], entry["alias"]
                assert alias == name, (
                    f"claude-api-key alias {alias!r} must equal its upstream "
                    f"name {name!r} -- an opaque alias is unresolvable to "
                    f"Claude Code, so it is rejected when the 1M-context "
                    f"marker is appended and it silently inherits the 272K "
                    f"GPT context cap"
                )
                # Independent of the identity check above: the id must also
                # survive the gateway-discovery filter, which requires
                # "claude" or "anthropic" in the id to list it in /model.
                assert "claude" in alias or "anthropic" in alias, (
                    f"claude-api-key alias {alias!r} would not appear in the "
                    f"/model picker under "
                    f"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"
                )


class TestTrackedConfigPickerDiscoveryAliases:
    """Regression coverage for a documentation-only invariant that has no
    other enforcement: each claude-gpt-* picker-discovery alias's `name`
    must exactly match its plain-alias twin's `name`, or CLIProxyAPI
    silently treats them as two different upstream models. Nothing in
    code enforces this today (it's a tracked-YAML-file convention,
    documented three times in connectors/cliproxy/config.yaml's comments
    and .claude/skills/add-connector/SKILL.md) -- this test at least
    catches an accidental drift in the tracked template itself.
    """

    def test_discovery_alias_names_match_plain_twins(self):
        import connectors.providers.cliproxy_oauth as mod

        cfg = yaml.safe_load(mod.TRACKED_CONFIG.read_text())
        entries = cfg["oauth-model-alias"]["codex"]
        by_alias = {e["alias"]: e["name"] for e in entries}

        twins = {
            "claude-gpt-sol": "sol",
            "claude-gpt-terra": "terra",
            "claude-gpt-luna": "luna-max",
        }
        for discovery_alias, plain_alias in twins.items():
            assert discovery_alias in by_alias, f"missing {discovery_alias} entry"
            assert plain_alias in by_alias, f"missing {plain_alias} entry"
            assert by_alias[discovery_alias] == by_alias[plain_alias], (
                f"{discovery_alias}'s name ({by_alias[discovery_alias]!r}) must "
                f"match {plain_alias}'s name ({by_alias[plain_alias]!r})"
            )

    def test_payload_override_names_match_oauth_alias_twins(self):
        """Same invariant, same enforcement gap, for the reasoning-effort
        feature's payload.override block: each entry's models[].name must
        match GPT_MODELS.template_upstream_name, which must itself equal
        the real upstream name registered for that subagent's alias under
        oauth-model-alias.codex[] -- or the override silently stops
        matching any real request. Catches drift in the tracked template
        itself, same as the discovery-alias test above.
        """
        import connectors.providers.cliproxy_oauth as mod

        cfg = yaml.safe_load(mod.TRACKED_CONFIG.read_text())
        oauth_names = {
            e["alias"]: e["name"] for e in cfg["oauth-model-alias"]["codex"]
        }
        deus_model_map = cfg["deus-model-map"]
        by_override_name = {
            rule["models"][0]["name"]: rule for rule in cfg["payload"]["override"]
        }

        for model in mod.GPT_MODELS:
            alias = deus_model_map[model.subagent_name]
            assert oauth_names[alias] == model.template_upstream_name, (
                f"{model.subagent_name}'s alias {alias!r} resolves to upstream "
                f"name {oauth_names[alias]!r}, but GPT_MODELS declares "
                f"template_upstream_name={model.template_upstream_name!r}"
            )
            assert model.template_upstream_name in by_override_name, (
                f"missing payload.override entry for {model.template_upstream_name!r}"
            )
            rule = by_override_name[model.template_upstream_name]
            assert rule["models"][0]["protocol"] == "codex"
            assert rule["params"]["reasoning.effort"] in mod._CODEX_EFFORT_LEVELS


class TestWriteLaunchdPlist:
    """Regression coverage for a real defect a verification-gate pass
    caught: _write_launchd_plist did a bare plistlib.dump with no existence
    check, silently destroying anything already at that path -- reproduced
    for real when this connector's original generic label
    (com.deus.cliproxyapi) collided with a pre-existing personal
    CLIProxyAPI install on the developer's own machine during testing.
    """

    @pytest.fixture(autouse=True)
    def _redirect_plist_path(self, tmp_path, monkeypatch):
        import connectors.providers.cliproxy_oauth as mod

        monkeypatch.setattr(mod, "PLIST_PATH", tmp_path / "test.plist")
        self.mod = mod
        self.handler = mod.CliproxyOauthSetupHandler(mod.CliproxyOauthConnector())

    def test_writes_cleanly_when_nothing_exists(self):
        self.handler._write_launchd_plist("/usr/local/bin/cli-proxy-api")
        assert self.mod.PLIST_PATH.exists()

    def test_identical_rewrite_is_idempotent(self):
        self.handler._write_launchd_plist("/usr/local/bin/cli-proxy-api")
        self.handler._write_launchd_plist("/usr/local/bin/cli-proxy-api")  # no raise

    def test_refuses_to_overwrite_unrelated_existing_plist(self):
        import plistlib

        self.mod.PLIST_PATH.write_bytes(
            plistlib.dumps(
                {
                    "Label": "com.someone.else",
                    "ProgramArguments": ["/totally/different/binary", "--flag"],
                }
            )
        )
        before = self.mod.PLIST_PATH.read_bytes()
        with pytest.raises(RuntimeError, match="already exists"):
            self.handler._write_launchd_plist("/usr/local/bin/cli-proxy-api")
        assert self.mod.PLIST_PATH.read_bytes() == before  # untouched

    def test_refuses_to_overwrite_malformed_existing_file(self):
        # A file that fails to parse at all must fail closed (refuse) --
        # not be silently treated as absent, and not raise an unhandled
        # ExpatError past the guard's own except tuple.
        self.mod.PLIST_PATH.write_bytes(b"not a plist at all <<<")
        before = self.mod.PLIST_PATH.read_bytes()
        with pytest.raises(RuntimeError, match="already exists"):
            self.handler._write_launchd_plist("/usr/local/bin/cli-proxy-api")
        assert self.mod.PLIST_PATH.read_bytes() == before  # untouched
